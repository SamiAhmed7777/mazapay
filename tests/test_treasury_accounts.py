"""Account maintenance: caps, policy, status — admin only, audit-logged.

Two layers: the service rules (who may change what, and when a change is refused
because requests are still in flight) and the HTTP surface (admin page and JSON
API), both exercised against the real router.
"""
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

from mazapay.treasury import auth, service, store  # noqa: E402
from mazapay.treasury.service import TreasuryError  # noqa: E402

requires_writable = _env.requires_writable
net_gate = _env.net_gate

store.init_db() if _env.WRITABLE else None
auth.init_db() if _env.WRITABLE else None


def _user(role):
    tag = uuid.uuid4().hex[:6]
    return {"id": f"u_{role}_{tag}", "email": f"{role}.{tag}@example.test",
            "name": f"{role} {tag}", "role": role}


def _account(**kw):
    code = "ACC-" + uuid.uuid4().hex[:8]
    return store.create_account(kw.pop("name", "Editable account"), code,
                                kw.pop("kind", "operating"), **kw)


def _request(acct, fin, usd="10.00"):
    return service.create_request(fin, account_id=acct["id"], kind="payout",
                                 purpose="Edit guard fixture",
                                 items=[{"payee_name": "P", "payee_address": "M" + uuid.uuid4().hex[:12],
                                         "usd_amount": usd}])


@requires_writable
class TestAccountEditing(unittest.TestCase):
    def setUp(self):
        self.admin = _user("admin")
        self.fin = _user("finance_officer")

    def test_admin_can_change_caps_and_policy(self):
        acct = _account()
        updated = service.update_account(self.admin, acct["id"], {
            "cap_per_request_usd": "2500", "cap_daily_usd": "9000.50",
            "signature_policy": "single", "notes": "raised after board approval",
        })
        self.assertEqual(updated["cap_per_request_usd"], "2500.00")
        self.assertEqual(updated["cap_daily_usd"], "9000.50")
        self.assertEqual(updated["signature_policy"], "single")
        rows = service.audit_rows(self.admin, object_type="account", object_id=acct["id"])
        self.assertEqual(rows[0]["action"], "account_updated")
        detail = json.loads(rows[0]["detail"])
        self.assertEqual(detail["after"]["cap_per_request_usd"], "2500.00")

    def test_blank_cap_clears_it(self):
        acct = _account(cap_daily_usd="100")
        updated = service.update_account(self.admin, acct["id"], {"cap_daily_usd": ""})
        self.assertIsNone(updated["cap_daily_usd"])

    def test_non_admin_is_refused(self):
        acct = _account()
        with self.assertRaises(TreasuryError) as ctx:
            service.update_account(self.fin, acct["id"], {"cap_daily_usd": "1"})
        self.assertEqual(ctx.exception.code, "insufficient_role")
        self.assertEqual(ctx.exception.status, 403)

    def test_validation_errors(self):
        acct = _account()
        for changes, code in [
            ({"signature_policy": "trifecta"}, "invalid_policy"),
            ({"status": "melted"}, "invalid_status"),
            ({"cap_daily_usd": "abc"}, "invalid_amount"),
            ({"cap_daily_usd": "-5"}, "invalid_amount"),
            ({"nope": "x"}, "unknown_field"),
            ({}, "no_changes"),
        ]:
            with self.assertRaises(TreasuryError) as ctx:
                service.update_account(self.admin, acct["id"], changes)
            self.assertEqual(ctx.exception.code, code, changes)

    def test_policy_change_refused_while_requests_are_in_flight(self):
        acct = _account()
        req = _request(acct, self.fin)  # status prepared
        with self.assertRaises(TreasuryError) as ctx:
            service.update_account(self.admin, acct["id"], {"signature_policy": "single"})
        self.assertEqual(ctx.exception.code, "account_busy")
        self.assertIn(req["ref"], str(ctx.exception))
        # A cap change is still allowed: caps are re-checked at completion.
        service.update_account(self.admin, acct["id"], {"cap_daily_usd": "500"})

    def test_closing_refused_while_requests_are_in_flight(self):
        acct = _account()
        _request(acct, self.fin)
        with self.assertRaises(TreasuryError) as ctx:
            service.update_account(self.admin, acct["id"], {"status": "closed"})
        self.assertEqual(ctx.exception.code, "account_busy")

    def test_no_op_change_writes_no_audit_row(self):
        acct = _account(cap_daily_usd="100")
        before = len(service.audit_rows(self.admin, object_type="account", object_id=acct["id"]))
        service.update_account(self.admin, acct["id"], {"cap_daily_usd": "100.00"})
        after = len(service.audit_rows(self.admin, object_type="account", object_id=acct["id"]))
        self.assertEqual(before, after)


@net_gate
class TestAccountEditingHttp(unittest.TestCase):
    """The admin page and the JSON API, against the real router."""

    @classmethod
    def setUpClass(cls):
        from mazapay.treasury import http as tr_http

        cls.srv = tr_http.make_server("127.0.0.1", 0)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def session(self, role):
        """Create a real user row, mint a session, and return token + CSRF."""
        seed_admin = {"id": "seed", "email": "seed@example.test", "name": "seed", "role": "admin"}
        tag = uuid.uuid4().hex[:6]
        created = auth.create_user(seed_admin, email=f"{role}.{tag}@example.test",
                                   name=role, role=role)
        token = auth.create_session(created["id"])
        resolved = auth.resolve_session(token)
        return None, token, resolved["session"]["csrf"]

    def test_admin_sees_edit_link_and_page(self):
        acct = _account()
        _opener, token, _csrf = self.session("admin")
        req = urllib.request.Request(self.base + "/treasury/accounts",
                                     headers={"Cookie": f"mazapay_tr={token}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            html = r.read().decode()
        self.assertIn(f"/treasury/accounts/{acct['id']}/edit", html)
        req = urllib.request.Request(self.base + f"/treasury/accounts/{acct['id']}/edit",
                                     headers={"Cookie": f"mazapay_tr={token}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            page = r.read().decode()
        self.assertIn("Edit account", page)
        self.assertIn('name="cap_daily_usd"', page)

    def test_api_update_requires_admin(self):
        acct = _account()
        opener, token, csrf = self.session("treasurer")
        body = json.dumps({"cap_daily_usd": "10"}).encode()
        req = urllib.request.Request(
            self.base + f"/api/accounts/{acct['id']}", data=body, method="POST",
            headers={"Content-Type": "application/json", "X-CSRF-Token": csrf,
                     "Cookie": f"mazapay_tr={token}"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 403)
        payload = json.loads(ctx.exception.read().decode())
        self.assertEqual(payload["error"]["code"], "insufficient_role")

    def test_api_update_applies_for_admin(self):
        acct = _account()
        opener, token, csrf = self.session("admin")
        body = json.dumps({"cap_per_request_usd": "1234.56", "notes": "via api"}).encode()
        req = urllib.request.Request(
            self.base + f"/api/accounts/{acct['id']}", data=body, method="POST",
            headers={"Content-Type": "application/json", "X-CSRF-Token": csrf,
                     "Cookie": f"mazapay_tr={token}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            payload = json.loads(r.read().decode())
        self.assertEqual(payload["account"]["cap_per_request_usd"], "1234.56")
        self.assertEqual(store.get_account(acct["id"])["notes"], "via api")


if __name__ == "__main__":
    unittest.main()
