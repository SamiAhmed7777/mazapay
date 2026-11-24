"""Tests for the treasury identity layer (mazapay/treasury/auth.py).

Covers contract §4 tables, §5 roles and §8 auth API: no-enumeration magic
links, single-use + TTL, sessions/CSRF, rate limiting, bootstrap idempotency
and the audit chain. unittest only; no network; temp DB per test.
"""
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest

try:
    import _sandbox
except ImportError:  # package-style invocation (python3 -m unittest tests.test_x)
    from tests import _sandbox  # type: ignore

from mazapay.treasury import auth, settings  # noqa: E402


class TreasuryAuthCase(unittest.TestCase):
    """Fresh sqlite file + fresh rate limiter for every test."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="tr-auth-", dir=_sandbox.TMP)
        self._saved = (settings.DB, settings.DEMO_MODE, settings.MAGIC_TTL,
                       settings.SESSION_TTL)
        self._saved_env = os.environ.get("MAZAPAY_TR_DB")
        settings.DB = os.path.join(self._tmp, "tr-auth.sqlite3")
        # lane 1's store resolves the path from the env first; set both so the
        # whole treasury stack agrees on this test's file.
        os.environ["MAZAPAY_TR_DB"] = settings.DB
        settings.DEMO_MODE = False
        settings.MAGIC_TTL = 900
        settings.SESSION_TTL = 8 * 3600
        auth._ready_paths.discard(auth._db_path())
        auth.reset_rate_limits()
        auth.init_db()

    def tearDown(self):
        settings.DB, settings.DEMO_MODE, settings.MAGIC_TTL, settings.SESSION_TTL = self._saved
        if self._saved_env is None:
            os.environ.pop("MAZAPAY_TR_DB", None)
        else:
            os.environ["MAZAPAY_TR_DB"] = self._saved_env
        auth._ready_paths.discard(auth._db_path())
        auth.reset_rate_limits()
        shutil.rmtree(self._tmp, ignore_errors=True)

    # -- helpers -------------------------------------------------------------
    def conn(self):
        conn = sqlite3.connect(auth._db_path())
        conn.row_factory = sqlite3.Row
        return conn

    def rows(self, sql, args=()):
        conn = self.conn()
        try:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]
        finally:
            conn.close()

    def count(self, table, where="1=1", args=()):
        return self.rows(f"SELECT COUNT(*) AS n FROM {table} WHERE {where}", args)[0]["n"]

    def audit(self, action=None):
        if action is None:
            return self.rows("SELECT * FROM tr_audit ORDER BY id")
        return self.rows("SELECT * FROM tr_audit WHERE action=? ORDER BY id", (action,))

    def admin(self, email="admin@example.org", name="Ada Admin"):
        return auth.bootstrap_admin(email, name)

    def make_user(self, role="viewer", email=None, name="Test User"):
        return auth.create_user(self.admin(), email=email or f"{role}@example.org",
                                name=name, role=role)


@_sandbox.requires_writable
class TestBootstrapAdmin(TreasuryAuthCase):
    def test_bootstrap_is_idempotent(self):
        first = auth.bootstrap_admin("Admin@Example.org", "Ada")
        again = auth.bootstrap_admin("admin@example.org", "Ada Again")
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(first["email"], "admin@example.org")
        self.assertEqual(first["role"], "admin")
        self.assertTrue(first["active"])
        self.assertEqual(self.count("tr_users"), 1)
        self.assertEqual(len(self.audit("bootstrap_admin")), 1)

    def test_bootstrap_returns_existing_admin_not_a_second_one(self):
        first = self.admin()
        other = auth.bootstrap_admin("second@example.org", "Second")
        self.assertEqual(other["id"], first["id"])
        self.assertEqual(self.count("tr_users", "role='admin'"), 1)

    def test_bootstrap_audit_row_is_auditor_visible(self):
        user = self.admin()
        rows = self.audit("bootstrap_admin")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["object_type"], "user")
        self.assertEqual(rows[0]["object_id"], user["id"])
        detail = json.loads(rows[0]["detail"])
        self.assertEqual(detail["role"], "admin")
        self.assertEqual(detail["email"], "admin@example.org")

    def test_bootstrap_rejects_invalid_email(self):
        with self.assertRaises(auth.TreasuryError) as ctx:
            auth.bootstrap_admin("not-an-email", "Nobody")
        self.assertEqual(ctx.exception.code, "invalid_email")
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(self.count("tr_users"), 0)


@_sandbox.requires_writable
class TestUserManagement(TreasuryAuthCase):
    def test_create_user_stores_role_and_audits(self):
        admin = self.admin()
        user = auth.create_user(admin, email="Treasurer@Example.org",
                                name="Tia Treasurer", role="treasurer")
        self.assertEqual(user["role"], "treasurer")
        self.assertEqual(user["email"], "treasurer@example.org")
        self.assertTrue(user["active"])
        listed = {u["id"]: u for u in auth.list_users(admin)}
        self.assertIn(user["id"], listed)
        self.assertNotIn("token_hash", listed[user["id"]])
        audit = self.audit("user_created")
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["actor_user_id"], admin["id"])
        self.assertEqual(json.loads(audit[0]["detail"])["role"], "treasurer")

    def test_unknown_role_refused(self):
        admin = self.admin()
        with self.assertRaises(auth.TreasuryError) as ctx:
            auth.create_user(admin, email="x@example.org", name="X", role="superuser")
        self.assertEqual(ctx.exception.code, "invalid_role")
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(self.count("tr_users"), 1)  # only the admin

    def test_duplicate_email_refused_case_insensitively(self):
        admin = self.admin()
        auth.create_user(admin, email="dup@example.org", name="Dup", role="viewer")
        with self.assertRaises(auth.TreasuryError) as ctx:
            auth.create_user(admin, email=" DUP@Example.org ", name="Dup2", role="viewer")
        self.assertEqual(ctx.exception.code, "email_exists")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(self.count("tr_users"), 2)
        self.assertEqual(len(self.audit("user_created")), 1)

    def test_role_matrix_cannot_manage_users(self):
        admin = self.admin()
        for role in ("executive", "council_treasurer", "treasurer",
                     "finance_officer", "viewer"):
            user = auth.create_user(admin, email=f"{role}@example.org",
                                    name=role, role=role)
            with self.assertRaises(auth.TreasuryError) as ctx:
                auth.create_user(user, email=f"new-{role}@example.org",
                                 name="N", role="viewer")
            self.assertEqual(ctx.exception.code, "insufficient_role")
            self.assertEqual(ctx.exception.status, 403)
            with self.assertRaises(auth.TreasuryError) as ctx:
                auth.deactivate_user(user, admin["id"])
            self.assertEqual(ctx.exception.code, "insufficient_role")

    def test_unauthenticated_refused(self):
        with self.assertRaises(auth.TreasuryError) as ctx:
            auth.create_user(None, email="a@example.org", name="A", role="viewer")
        self.assertEqual(ctx.exception.code, "not_authenticated")
        self.assertEqual(ctx.exception.status, 401)
        with self.assertRaises(auth.TreasuryError):
            auth.list_users(None)

    def test_deactivate_user_is_idempotent_and_kills_sessions(self):
        admin = self.admin()
        user = auth.create_user(admin, email="leaver@example.org", name="Lee",
                                role="finance_officer")
        token = auth.create_session(user["id"])
        self.assertIsNotNone(auth.resolve_session(token))

        off = auth.deactivate_user(admin, user["id"])
        self.assertFalse(off["active"])
        self.assertIsNone(auth.resolve_session(token))
        off_again = auth.deactivate_user(admin, user["id"])
        self.assertFalse(off_again["active"])
        self.assertEqual(len(self.audit("user_deactivated")), 1)

    def test_deactivate_unknown_user(self):
        with self.assertRaises(auth.TreasuryError) as ctx:
            auth.deactivate_user(self.admin(), "usr_doesnotexist")
        self.assertEqual(ctx.exception.code, "user_not_found")
        self.assertEqual(ctx.exception.status, 404)

    def test_permission_matrix(self):
        # contract §5: read everywhere, manage_users admin only, admin never signs
        for role in auth.ROLES:
            user = {"id": "u", "role": role, "active": True}
            self.assertTrue(auth.can(user, "read"), role)
        for role in ("executive", "council_treasurer", "treasurer",
                     "finance_officer", "viewer"):
            self.assertFalse(auth.can({"id": "u", "role": role, "active": True},
                                      "manage_users"), role)
        self.assertTrue(auth.can({"id": "u", "role": "admin", "active": True},
                                 "manage_users"))
        self.assertFalse(auth.can({"id": "u", "role": "admin", "active": True}, "sign"))
        self.assertFalse(auth.can({"id": "u", "role": "executive", "active": True},
                                  "complete"))
        self.assertFalse(auth.can({"id": "u", "role": "viewer", "active": True},
                                  "create_request"))
        self.assertFalse(auth.can({"id": "u", "role": "admin", "active": False},
                                  "read"))
        # signature policy eligibility
        self.assertTrue(auth.may_sign({"role": "treasurer", "active": True}, "single"))
        self.assertFalse(auth.may_sign({"role": "treasurer", "active": True}, "dual"))
        self.assertTrue(auth.may_sign({"role": "executive", "active": True}, "dual"))
        self.assertTrue(auth.may_sign({"role": "council_treasurer", "active": True}, "dual"))
        self.assertFalse(auth.may_sign({"role": "admin", "active": True}, "dual"))
        self.assertFalse(auth.may_sign({"role": "finance_officer", "active": True}, "dual"))


@_sandbox.requires_writable
class TestMagicLinks(TreasuryAuthCase):
    BASE = "https://treasury.example.org"

    def token_from(self, link):
        return link.rsplit("/", 1)[-1]

    def test_unknown_and_known_email_responses_identical(self):
        settings.DEMO_MODE = True
        user = self.make_user("treasurer")
        known = auth.request_magic_link(user["email"], self.BASE)
        unknown = auth.request_magic_link("ghost@example.org", self.BASE)
        self.assertEqual(sorted(known), sorted(unknown))
        self.assertEqual(sorted(known), ["link", "sent"])
        self.assertTrue(known["sent"])
        self.assertTrue(unknown["sent"])
        for resp in (known, unknown):
            self.assertTrue(resp["link"].startswith(self.BASE + "/treasury/auth/"))
            self.assertGreaterEqual(len(self.token_from(resp["link"])), 40)
        # the decoy is not redeemable; the real one is
        self.assertIsNone(auth.consume_magic_link(self.token_from(unknown["link"])))
        redeemed = auth.consume_magic_link(self.token_from(known["link"]))
        self.assertIsNotNone(redeemed)
        self.assertEqual(redeemed["user"]["id"], user["id"])

    def test_production_response_is_bare_sent(self):
        user = self.make_user("treasurer")
        with self.assertLogs("mazapay.treasury.auth", level="INFO"):
            known = auth.request_magic_link(user["email"], self.BASE)
            unknown = auth.request_magic_link("ghost@example.org", self.BASE)
        self.assertEqual(known, {"sent": True})
        self.assertEqual(unknown, {"sent": True})

    def test_unknown_email_still_writes_audit_row(self):
        auth.request_magic_link("ghost@example.org", self.BASE)
        rows = self.audit("login_requested")
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["actor_user_id"])
        detail = json.loads(rows[0]["detail"])
        self.assertEqual(detail["email"], "ghost@example.org")
        self.assertFalse(detail["known"])
        self.assertEqual(self.count("tr_login_tokens"), 0)

    def test_magic_link_works_once_and_only_once(self):
        settings.DEMO_MODE = True
        user = self.make_user("treasurer")
        resp = auth.request_magic_link(user["email"], self.BASE)
        token = self.token_from(resp["link"])

        first = auth.consume_magic_link(token)
        self.assertIsNotNone(first)
        self.assertEqual(first["user"]["id"], user["id"])
        self.assertIsNotNone(auth.resolve_session(first["session_token"]))

        second = auth.consume_magic_link(token)
        self.assertIsNone(second)
        self.assertEqual(self.count("tr_sessions"), 1)
        self.assertEqual(len(self.audit("login_ok")), 1)
        failed = [json.loads(r["detail"])["code"] for r in self.audit("login_failed")]
        self.assertEqual(failed, ["token_used"])

    def test_expired_link_refused(self):
        settings.DEMO_MODE = True
        settings.MAGIC_TTL = 0
        user = self.make_user("treasurer")
        resp = auth.request_magic_link(user["email"], self.BASE)
        token = self.token_from(resp["link"])
        self.assertIsNone(auth.consume_magic_link(token))
        self.assertEqual(self.count("tr_sessions"), 0)
        failed = [json.loads(r["detail"])["code"] for r in self.audit("login_failed")]
        self.assertEqual(failed, ["expired"])

    def test_link_for_deactivated_user_refused(self):
        settings.DEMO_MODE = True
        admin = self.admin()
        user = auth.create_user(admin, email="gone@example.org", name="G",
                                role="treasurer")
        auth.deactivate_user(admin, user["id"])
        resp = auth.request_magic_link(user["email"], self.BASE)
        self.assertEqual(sorted(resp), ["link", "sent"])
        self.assertIsNone(auth.consume_magic_link(self.token_from(resp["link"])))
        self.assertEqual(self.count("tr_login_tokens"), 0)

    def test_garbage_token_refused(self):
        self.assertIsNone(auth.consume_magic_link("not-a-real-token"))
        self.assertIsNone(auth.consume_magic_link(""))
        self.assertIsNone(auth.consume_magic_link(None))
        self.assertEqual(len(self.audit("login_failed")), 1)  # only the non-empty one

    def test_log_redacts_token_except_last_four(self):
        user = self.make_user("treasurer")
        with self.assertLogs("mazapay.treasury.auth", level="INFO") as logs:
            auth.request_magic_link(user["email"], self.BASE)
        line = "\n".join(logs.output)
        marker = self.BASE + "/treasury/auth/"
        self.assertIn(marker, line)
        tail = line.split(marker, 1)[1].split()[0].strip()
        self.assertTrue(tail.startswith("***"), tail)
        self.assertEqual(len(tail), 7)  # *** + 4 chars
        self.assertNotIn("***\n", line)

    def test_only_hash_is_stored(self):
        settings.DEMO_MODE = True
        user = self.make_user("treasurer")
        resp = auth.request_magic_link(user["email"], self.BASE)
        token = self.token_from(resp["link"])
        row = self.rows("SELECT * FROM tr_login_tokens")[0]
        self.assertEqual(row["token_hash"], hashlib.sha256(token.encode()).hexdigest())
        self.assertNotEqual(row["token_hash"], token)
        self.assertGreater(row["expires_ts"], row["created_ts"])


@_sandbox.requires_writable
class TestRateLimit(TreasuryAuthCase):
    def test_trips_after_five_requests_per_email(self):
        user = self.make_user("treasurer")
        for _ in range(5):
            self.assertEqual(auth.request_magic_link(user["email"], settings.BASE_URL),
                             {"sent": True})
        self.assertEqual(self.count("tr_login_tokens"), 5)
        sixth = auth.request_magic_link(user["email"], settings.BASE_URL)
        self.assertEqual(sixth, {"sent": True})  # indistinguishable answer
        self.assertEqual(self.count("tr_login_tokens"), 5)
        trips = self.audit("login_rate_limited")
        self.assertEqual(len(trips), 1)
        self.assertEqual(json.loads(trips[0]["detail"])["code"], "rate_limited")

    def test_trips_per_ip_across_emails(self):
        user = self.make_user("treasurer")
        for i in range(5):
            auth.request_magic_link(f"user{i}@example.org", settings.BASE_URL,
                                    ip="203.0.113.9")
        blocked = auth.request_magic_link(user["email"], settings.BASE_URL,
                                          ip="203.0.113.9")
        self.assertEqual(blocked, {"sent": True})
        self.assertEqual(len(self.audit("login_rate_limited")), 1)
        self.assertEqual(self.count("tr_login_tokens"), 0)
        # a different IP is unaffected
        ok = auth.request_magic_link(user["email"], settings.BASE_URL,
                                     ip="203.0.113.10")
        self.assertEqual(ok, {"sent": True})
        self.assertEqual(self.count("tr_login_tokens"), 1)

    def test_thread_safe_cap(self):
        user = self.make_user("treasurer")
        results = []
        lock = threading.Lock()

        def fire():
            out = auth.request_magic_link(user["email"], settings.BASE_URL,
                                          ip="198.51.100.7")
            with lock:
                results.append(out)

        threads = [threading.Thread(target=fire) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(len(results), 12)
        self.assertTrue(all(r == {"sent": True} for r in results))
        self.assertEqual(self.count("tr_login_tokens"), auth.RATE_LIMIT_MAX)
        self.assertEqual(len(self.audit("login_rate_limited")), 12 - auth.RATE_LIMIT_MAX)

    def test_window_expires(self):
        user = self.make_user("treasurer")
        now = time.time()
        with auth._rl_lock:
            auth._rl_hits[("email", user["email"])] = auth.collections.deque(
                [now - auth.RATE_LIMIT_WINDOW - 1] * auth.RATE_LIMIT_MAX
            )
        self.assertEqual(auth.request_magic_link(user["email"], settings.BASE_URL),
                         {"sent": True})
        self.assertEqual(self.count("tr_login_tokens"), 1)


@_sandbox.requires_writable
class TestSessions(TreasuryAuthCase):
    def test_create_session_returns_raw_token_and_stores_hash(self):
        user = self.make_user("viewer")
        token = auth.create_session(user["id"], ip="127.0.0.9", user_agent="pytest")
        self.assertGreaterEqual(len(token), 40)
        row = self.rows("SELECT * FROM tr_sessions")[0]
        self.assertEqual(row["token_hash"], hashlib.sha256(token.encode()).hexdigest())
        self.assertNotEqual(row["token_hash"], token)
        self.assertEqual(row["ip"], "127.0.0.9")
        self.assertEqual(row["user_agent"], "pytest")
        self.assertIsNone(row["revoked_ts"])

    def test_resolve_session_shape(self):
        user = self.make_user("executive")
        token = auth.create_session(user["id"])
        resolved = auth.resolve_session(token)
        self.assertEqual(resolved["user"]["id"], user["id"])
        self.assertEqual(resolved["user"]["role"], "executive")
        self.assertEqual(sorted(resolved["session"]),
                         ["created_ts", "csrf", "expires_ts", "id", "ip",
                          "user_agent", "user_id"])
        self.assertTrue(resolved["session"]["csrf"])

    def test_unknown_tokens_resolve_to_none(self):
        self.assertIsNone(auth.resolve_session("nope"))
        self.assertIsNone(auth.resolve_session(""))
        self.assertIsNone(auth.resolve_session(None))

    def test_expired_session_resolves_to_none(self):
        user = self.make_user("viewer")
        settings.SESSION_TTL = 0
        token = auth.create_session(user["id"])
        self.assertIsNone(auth.resolve_session(token))

    def test_session_ttl_from_settings(self):
        user = self.make_user("viewer")
        settings.SESSION_TTL = 120
        token = auth.create_session(user["id"])
        resolved = auth.resolve_session(token)
        delta = resolved["session"]["expires_ts"] - resolved["session"]["created_ts"]
        self.assertAlmostEqual(delta, 120, delta=2)

    def test_revoked_session_resolves_to_none_and_is_idempotent(self):
        user = self.make_user("treasurer")
        token = auth.create_session(user["id"])
        auth.revoke_session(token)
        self.assertIsNone(auth.resolve_session(token))
        auth.revoke_session(token)  # idempotent, no error
        auth.revoke_session("never-existed")
        auth.revoke_session(None)
        self.assertEqual(len(self.audit("session_revoked")), 1)

    def test_inactive_user_sessions_die(self):
        admin = self.admin()
        user = auth.create_user(admin, email="temp@example.org", name="T",
                                role="viewer")
        token = auth.create_session(user["id"])
        auth.deactivate_user(admin, user["id"])
        self.assertIsNone(auth.resolve_session(token))

    def test_session_dies_when_user_row_deactivates_out_of_band(self):
        user = self.make_user("viewer")
        token = auth.create_session(user["id"])
        conn = self.conn()
        try:
            conn.execute("UPDATE tr_users SET active=0 WHERE id=?", (user["id"],))
            conn.commit()
        finally:
            conn.close()
        self.assertIsNone(auth.resolve_session(token))


@_sandbox.requires_writable
class TestCsrf(TreasuryAuthCase):
    def setUp(self):
        super().setUp()
        self.user = self.make_user("treasurer")
        self.token = auth.create_session(self.user["id"])
        self.session = auth.resolve_session(self.token)["session"]
        self.good = self.session["csrf"]

    def test_accepts_matching_cookie_and_header(self):
        self.assertTrue(auth.check_csrf(self.session, self.good, self.good))

    def test_rejects_missing_header(self):
        self.assertFalse(auth.check_csrf(self.session, self.good, None))
        self.assertFalse(auth.check_csrf(self.session, self.good, ""))

    def test_rejects_missing_cookie(self):
        self.assertFalse(auth.check_csrf(self.session, None, self.good))
        self.assertFalse(auth.check_csrf(self.session, "", self.good))

    def test_rejects_both_missing(self):
        self.assertFalse(auth.check_csrf(self.session, None, None))

    def test_rejects_header_mismatch(self):
        self.assertFalse(auth.check_csrf(self.session, self.good, "nope"))

    def test_rejects_cookie_mismatch(self):
        self.assertFalse(auth.check_csrf(self.session, "nope", self.good))

    def test_rejects_equal_but_wrong_pair(self):
        self.assertFalse(auth.check_csrf(self.session, "same", "same"))

    def test_rejects_without_session(self):
        self.assertFalse(auth.check_csrf(None, self.good, self.good))
        self.assertFalse(auth.check_csrf({}, self.good, self.good))

    def test_csrf_differs_per_session_and_is_stable(self):
        other = auth.create_session(self.user["id"])
        other_session = auth.resolve_session(other)["session"]
        self.assertNotEqual(other_session["csrf"], self.good)
        self.assertEqual(auth.resolve_session(self.token)["session"]["csrf"], self.good)
        self.assertNotEqual(auth.new_csrf_token(), auth.new_csrf_token())


@_sandbox.requires_writable
class TestAuditTrail(TreasuryAuthCase):
    def test_every_auth_action_is_audited(self):
        settings.DEMO_MODE = True
        admin = self.admin()
        user = auth.create_user(admin, email="ops@example.org", name="Ops",
                                role="finance_officer")
        auth.deactivate_user(admin, user["id"])
        fresh = auth.create_user(admin, email="treas@example.org", name="T",
                                 role="treasurer")
        resp = auth.request_magic_link(fresh["email"], "https://t.example.org")
        session_token = auth.consume_magic_link(resp["link"].rsplit("/", 1)[-1])["session_token"]
        auth.consume_magic_link("bogus")
        auth.revoke_session(session_token)
        for _ in range(auth.RATE_LIMIT_MAX + 1):
            auth.request_magic_link(admin["email"], "https://t.example.org")

        actions = {r["action"] for r in self.audit()}
        for expected in ("bootstrap_admin", "user_created", "user_deactivated",
                         "login_requested", "login_ok", "login_failed",
                         "session_revoked", "login_rate_limited"):
            self.assertIn(expected, actions)

    def test_chain_links_and_first_row_is_genesis(self):
        settings.DEMO_MODE = True
        admin = self.admin()
        auth.create_user(admin, email="a@example.org", name="A", role="viewer")
        auth.request_magic_link("a@example.org", "https://t.example.org")
        auth.request_magic_link("ghost@example.org", "https://t.example.org")
        rows = self.audit()
        self.assertGreaterEqual(len(rows), 3)
        self.assertEqual(rows[0]["prev_hash"], "0" * 64)
        for prev, cur in zip(rows, rows[1:]):
            self.assertEqual(cur["prev_hash"], prev["hash"])
            self.assertEqual(len(cur["hash"]), 64)
        self.assertTrue(auth.verify_audit_chain())

    def test_audit_rows_are_json_detail(self):
        self.admin()
        for row in self.audit():
            detail = json.loads(row["detail"])
            self.assertIsInstance(detail, dict)
            self.assertIsNotNone(row["object_type"])
            self.assertIsNotNone(row["object_id"])


if __name__ == "__main__":
    unittest.main()
