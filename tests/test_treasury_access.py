"""Access tracking: who is signed in, last seen, revoke."""
import json
import threading
import unittest
import urllib.error
import urllib.request
import uuid

try:
    import _sandbox as _env
except ImportError:
    from tests import _sandbox as _env  # type: ignore

from mazapay.treasury import auth, store  # noqa: E402
from mazapay.treasury.service import TreasuryError  # noqa: E402

requires_writable = _env.requires_writable
net_gate = _env.net_gate

store.init_db() if _env.WRITABLE else None
auth.init_db() if _env.WRITABLE else None

ADMIN = {"id": "seed-admin", "email": "seed@example.test", "name": "seed", "role": "admin"}


def make_user(role="treasurer", email=None):
    tag = uuid.uuid4().hex[:6]
    return auth.create_user(ADMIN, email=email or f"{role}.{tag}@example.test",
                            name=f"{role} {tag}", role=role)


@requires_writable
class TestSessions(unittest.TestCase):
    def setUp(self):
        auth.reset_rate_limits()

    def test_resolving_a_session_records_last_seen(self):
        user = make_user()
        token = auth.create_session(user["id"], ip="203.0.113.9", user_agent="pytest/1.0")
        resolved = auth.resolve_session(token)
        self.assertIsNotNone(resolved)
        rows = [s for s in auth.list_sessions({"id": "x", "role": "admin"}) if s["user_id"] == user["id"]]
        self.assertTrue(rows)
        self.assertEqual(rows[0]["state"], "active")
        self.assertEqual(rows[0]["ip"], "203.0.113.9")
        self.assertTrue(rows[0]["last_seen_ts"], "last_seen must be stamped on use")

    def test_only_admins_can_list_or_revoke(self):
        user = make_user("treasurer")
        token = auth.create_session(user["id"])
        with self.assertRaises(TreasuryError) as ctx:
            auth.list_sessions({"id": user["id"], "role": "treasurer"})
        self.assertEqual(ctx.exception.code, "insufficient_role")
        with self.assertRaises(TreasuryError):
            auth.revoke_session_by_id({"id": user["id"], "role": "treasurer"}, "sess_x")
        self.assertIsNotNone(auth.resolve_session(token))

    def test_revoke_ends_the_session_and_is_audited(self):
        user = make_user()
        token = auth.create_session(user["id"])
        sessions = [s for s in auth.list_sessions({"id": "x", "role": "admin"}) if s["user_id"] == user["id"]]
        sid = sessions[0]["id"]
        auth.revoke_session_by_id({"id": "admin-x", "role": "admin"}, sid)
        self.assertIsNone(auth.resolve_session(token), "revoked session must not resolve")
        actions = [r["action"] for r in store.audit_rows(object_type="session", object_id=sid)]
        self.assertIn("session_revoked", actions)

    def test_revoke_unknown_session_is_not_found(self):
        with self.assertRaises(TreasuryError) as ctx:
            auth.revoke_session_by_id({"id": "admin-x", "role": "admin"}, "sess_missing")
        self.assertEqual(ctx.exception.code, "not_found")


@net_gate
class TestAccessHttp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from mazapay.treasury import http as tr_http

        cls.srv = tr_http.make_server("127.0.0.1", 0)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def login(self, role):
        user = make_user(role)
        token = auth.create_session(user["id"], ip="198.51.100.7", user_agent="access-test")
        return user, token, auth.resolve_session(token)["session"]["csrf"]

    def test_api_sessions_is_admin_only(self):
        _u, token, _csrf = self.login("treasurer")
        req = urllib.request.Request(self.base + "/api/sessions",
                                     headers={"Cookie": f"mazapay_tr={token}"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 403)

    def test_admin_sees_sessions_and_can_revoke(self):
        admin, atoken, acsrf = self.login("admin")
        victim, vtoken, _ = self.login("treasurer")
        req = urllib.request.Request(self.base + "/api/sessions",
                                     headers={"Cookie": f"mazapay_tr={atoken}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            payload = json.loads(r.read().decode())
        mine = [s for s in payload["sessions"] if s["user_id"] == victim["id"]]
        self.assertTrue(mine)
        sid = mine[0]["id"]

        body = f"csrf={acsrf}".encode()
        req = urllib.request.Request(
            self.base + f"/treasury/access/{sid}/revoke", data=body, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Cookie": f"mazapay_tr={atoken}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            self.assertEqual(r.status, 200)
        self.assertIsNone(auth.resolve_session(vtoken))

    def test_access_page_renders_for_admin(self):
        admin, atoken, _ = self.login("admin")
        req = urllib.request.Request(self.base + "/treasury/access",
                                     headers={"Cookie": f"mazapay_tr={atoken}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            page = r.read().decode()
        self.assertIn("Access", page)
        self.assertIn("Sessions", page)
        self.assertIn(admin["email"], page)
        self.assertIn('href="/treasury/access"', page, "admin navigation must link to Access")

    def test_access_link_is_hidden_from_non_admins(self):
        _u, token, _ = self.login("treasurer")
        req = urllib.request.Request(self.base + "/treasury/accounts",
                                     headers={"Cookie": f"mazapay_tr={token}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            page = r.read().decode()
        self.assertNotIn('href="/treasury/access"', page)


if __name__ == "__main__":
    unittest.main()
