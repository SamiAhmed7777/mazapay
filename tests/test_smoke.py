"""End-to-end smoke sweep over the FULL HTTP surface (token-auth mode).

One real HTTP server + MockChain, every route exercised twice:

- happy path  — the route's intended outcome (201/200 with the expected
  fields), including the late-payment accept/refund lifecycle, intent-first
  disbursement, reconcile-after-lost-broadcast, retry-after-proven-absence,
  watcher pass, and both MIP export kinds;
- auth path   — every tokened route refuses a missing AND an invalid bearer
  token with 401, while the public routes (checkout page, health, member
  collect) stay open without a token, and outbound payment IDs are hidden
  (404, never leaking existence) from unauthenticated reads;
- refusal     — POST /orders/{id}/simulate_payment must 403 the moment the
  chain adapter is not the MockChain (a real wallet RPC must never serve
  the demo funding endpoint).
"""
import json
import os
import time
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Optional, Tuple

try:
    import _sandbox as _env
except ImportError:  # package-style invocation (python3 -m unittest tests.test_x)
    from tests import _sandbox as _env  # type: ignore

net_gate = _env.net_gate

from mazapay import api, config, store, watcher  # noqa: E402
from mazapay.mazad import get_chain  # noqa: E402

TOKEN = "smoke-sweep-token"


@net_gate
class TestHttpSmokeSweep(unittest.TestCase):
    """Every route: happy path + auth-failure path, one shared server."""

    @classmethod
    def setUpClass(cls):
        store.init_db()
        cls._old_token = config.API_TOKEN
        config.API_TOKEN = TOKEN
        cls.srv = api.serve("127.0.0.1", 0, with_watcher=False)
        cls.port = cls.srv.server_address[1]
        cls.pool = ThreadPoolExecutor(max_workers=1)
        cls.pool.submit(cls.srv.serve_forever)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        api.shutdown_watcher()
        cls.pool.shutdown(wait=True)
        config.API_TOKEN = cls._old_token

    # -- helpers --------------------------------------------------------------
    def _req(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        token: Optional[str] = TOKEN,
    ) -> Tuple[int, Any]:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method, headers=headers
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode()
                try:
                    return resp.status, json.loads(raw)
                except ValueError:
                    return resp.status, raw  # non-JSON body (the checkout page)
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode() or "{}"
            exc.close()
            try:
                return exc.code, json.loads(payload)
            except ValueError:
                return exc.code, payload

    def _make_order(self, usd: float = 12.0) -> Dict[str, Any]:
        status, order = self._req("POST", "/orders", {"usd_amount": usd, "ref": "smoke"})
        self.assertEqual(status, 201, order)
        return order

    def _make_late_paid(self, usd: float) -> Dict[str, Any]:
        """Order whose quote expired and whose funds arrived anyway."""
        order = self._make_order(usd)
        store.update_order(order["id"], {"expires_ts": time.time() - 1}, "smoke_expire")
        chain, _mode = get_chain()
        chain.fund(order["address"], order["maza_amount"], config.CONFIRMATIONS_REQUIRED)
        counts = watcher.poll_once()
        self.assertGreaterEqual(
            counts["late_paid"], 1, f"expired+funded order did not flip late_paid: {counts}"
        )
        status, late = self._req("GET", f"/orders/{order['id']}", token=TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(late["status"], "late_paid", late)
        return late

    # -- public routes --------------------------------------------------------
    def test_index_and_health_open_in_every_mode(self):
        for token in (None, TOKEN):
            status, page = self._req("GET", "/", token=token)
            self.assertEqual(status, 200)
            self.assertIn("Pay with MAZA", page)
            status, health = self._req("GET", "/health", token=token)
            self.assertEqual(status, 200)
            self.assertTrue(health["ok"])
            self.assertEqual(health["chain_mode"], "mock")
            self.assertEqual(health["auth_mode"], "token")

    def test_collect_is_public_and_get_order_visible_to_member(self):
        status, order = self._req("POST", "/orders", {"usd_amount": 20.0, "ref": "smoke-collect"}, token=None)
        self.assertEqual(status, 201, order)
        self.assertEqual(order["status"], "awaiting")
        self.assertEqual(order["maza_amount"], 10000.0)
        self.assertEqual(order["rate"], 0.002)
        self.assertTrue(order["address"])
        status, fetched = self._req("GET", f"/orders/{order['id']}", token=None)
        self.assertEqual(status, 200)
        self.assertEqual(fetched["id"], order["id"])

    # -- tokened routes: 401 without/with-wrong token --------------------------
    def test_auth_failures_on_every_tokened_route(self):
        order = self._make_order()
        pay = self._req(
            "POST", "/payments", {"address": "MADDR-SMOKE", "usd_amount": 1.0}
        )[1]
        routes = [
            ("POST", "/vendors", {"name": "Smoke Vendor", "code": "SMOKEV"}),
            ("GET", "/vendors", None),
            ("POST", f"/orders/{order['id']}/simulate_payment", {"confirms": 6}),
            ("POST", f"/orders/{order['id']}/accept", {}),
            ("POST", f"/orders/{order['id']}/refund", {"address": "MADDR-R"}),
            ("POST", "/payments", {"address": "MADDR-SMOKE", "usd_amount": 1.0}),
            ("POST", f"/payments/{pay['id']}/reconcile", {}),
            ("POST", f"/payments/{pay['id']}/retry", {}),
            ("POST", "/watch", {}),
            ("GET", "/export/mip?kind=in", None),
            ("GET", "/export/mip?kind=out", None),
        ]
        for method, path, body in routes:
            status, err = self._req(method, path, body, token=None)
            self.assertEqual(status, 401, f"{method} {path}: {err}")
            self.assertIn("error", err)
            status, err = self._req(method, path, body, token="wrong-token")
            self.assertEqual(status, 401, f"{method} {path} (bad token): {err}")

    def test_outbound_payment_hidden_without_token(self):
        pay = self._req(
            "POST", "/payments", {"address": "MADDR-SMOKE", "usd_amount": 2.0}
        )[1]
        status, err = self._req("GET", f"/orders/{pay['id']}", token=None)
        self.assertEqual(status, 404)  # never leak existence to the public
        status, fetched = self._req("GET", f"/orders/{pay['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(fetched["kind"], "out")

    # -- happy paths -----------------------------------------------------------
    def test_vendor_roundtrip(self):
        status, vendor = self._req(
            "POST",
            "/vendors",
            {"name": "Smoke Supplier", "code": "SMKSUP", "maza_address": "MADDR-V", "notes": "n"},
        )
        self.assertEqual(status, 201, vendor)
        self.assertEqual(vendor["code"], "SMKSUP")
        status, listing = self._req("GET", "/vendors")
        self.assertEqual(status, 200)
        self.assertTrue(any(v["id"] == vendor["id"] for v in listing["vendors"]))

    def test_simulate_then_watch_confirms_payment(self):
        order = self._make_order(15.0)
        # below the confirmation threshold -> still awaiting
        status, sim = self._req(
            "POST", f"/orders/{order['id']}/simulate_payment", {"confirms": 1}
        )
        self.assertEqual(status, 200, sim)
        self.assertEqual(sim["confirms"], 1)
        self._req("POST", "/watch", {})
        _s, o1 = self._req("GET", f"/orders/{order['id']}")
        self.assertEqual(o1["status"], "awaiting")
        # full confirmations -> watcher flips paid with txid
        status, sim = self._req("POST", f"/orders/{order['id']}/simulate_payment", {})
        self.assertEqual(status, 200)
        self.assertEqual(sim["confirms"], config.CONFIRMATIONS_REQUIRED)
        status, counts = self._req("POST", "/watch", {})
        self.assertEqual(status, 200)
        self.assertGreaterEqual(counts["paid"], 1)
        _s, paid = self._req("GET", f"/orders/{order['id']}")
        self.assertEqual(paid["status"], "paid")
        self.assertTrue(paid["txid"])
        self.assertIsNotNone(paid["paid_ts"])

    def test_disburse_sent_with_txid(self):
        status, pay = self._req(
            "POST",
            "/payments",
            {"address": "MADDR-SMOKE", "usd_amount": 3.0, "ref": "smoke-disburse"},
        )
        self.assertEqual(status, 201, pay)
        self.assertEqual(pay["kind"], "out")
        self.assertEqual(pay["status"], "sent")
        self.assertTrue(pay["txid"])
        self.assertTrue(pay["nonce"])
        # invalid address is a clean 400, never a broadcast
        status, err = self._req("POST", "/payments", {"address": "garbage", "usd_amount": 1.0})
        self.assertEqual(status, 400)
        self.assertIn("invalid MAZA address", err["error"])

    def test_reconcile_binds_lost_broadcast(self):
        chain, _mode = get_chain()
        chain.fail_next_send(1, broadcast_first=True)  # tx relayed, response lost
        _status, pay = self._req(
            "POST", "/payments", {"address": "MADDR-SMOKE", "usd_amount": 4.0}
        )
        self.assertEqual(pay["status"], "send_uncertain")
        status, rec = self._req("POST", f"/payments/{pay['id']}/reconcile", {})
        self.assertEqual(status, 200, rec)
        self.assertEqual(rec["status"], "sent")
        self.assertTrue(rec["txid"])
        self.assertEqual(rec["reconciled"], "broadcast_found")
        # reconcile is idempotent: an already-sent row is a documented noop
        status, rec2 = self._req("POST", f"/payments/{pay['id']}/reconcile", {})
        self.assertEqual(status, 200)
        self.assertEqual(rec2["reconciled"], "already_sent_noop")

    def test_retry_after_proven_absence(self):
        chain, _mode = get_chain()
        chain.fail_next_send(1, broadcast_first=False)  # nothing went out
        _status, pay = self._req(
            "POST", "/payments", {"address": "MADDR-SMOKE", "usd_amount": 5.0}
        )
        self.assertEqual(pay["status"], "send_uncertain")
        _s, rec = self._req("POST", f"/payments/{pay['id']}/reconcile", {})
        self.assertEqual(rec["status"], "send_failed")
        self.assertEqual(rec["reconciled"], "no_broadcast_found")
        # retry refused for any state that is not send_failed-without-txid;
        # the re-broadcast itself succeeds and a second retry is refused
        _s3, retried = self._req("POST", f"/payments/{pay['id']}/retry", {})
        self.assertEqual(_s3, 200)
        self.assertEqual(retried["status"], "sent")
        self.assertTrue(retried["txid"])
        status, err = self._req("POST", f"/payments/{pay['id']}/retry", {})
        self.assertEqual(status, 409)  # second retry must be structurally refused

    def test_late_accept_at_locked_quote(self):
        late = self._make_late_paid(7.0)
        status, accepted = self._req("POST", f"/orders/{late['id']}/accept", {})
        self.assertEqual(status, 200, accepted)
        self.assertEqual(accepted["status"], "paid")
        # accept is for late_paid only: second accept is a 409
        status, err = self._req("POST", f"/orders/{late['id']}/accept", {})
        self.assertEqual(status, 409)

    def test_late_refund_at_original_locked_amount(self):
        late = self._make_late_paid(8.0)
        status, refunded = self._req(
            "POST", f"/orders/{late['id']}/refund", {"address": "MADDR-REFUND"}
        )
        self.assertEqual(status, 201, refunded)
        self.assertEqual(refunded["status"], "refunded")
        refund = refunded["refund"]
        self.assertEqual(refund["status"], "sent")
        self.assertEqual(refund["maza_amount"], late["maza_amount"])  # locked qty
        self.assertEqual(refund["parent_order_id"], late["id"])

    def test_export_mip_both_kinds(self):
        # seed one paid receipt and one sent disbursement for this test
        order = self._make_order(9.0)
        self._req("POST", f"/orders/{order['id']}/simulate_payment", {})
        self._req("POST", "/watch", {})
        self._req("POST", "/payments", {"address": "MADDR-SMOKE", "usd_amount": 1.5})
        status, err = self._req("GET", "/export/mip?kind=sideways")
        self.assertEqual(status, 400)
        status, receipts = self._req("GET", "/export/mip?kind=in")
        self.assertEqual(status, 200, receipts)
        self.assertGreaterEqual(receipts["rows"], 1)
        self.assertTrue(os.path.exists(receipts["path"]))
        self.assertEqual(receipts["skipped_already_exported"], True)
        status, disb = self._req("GET", "/export/mip?kind=out")
        self.assertEqual(status, 200, disb)
        self.assertGreaterEqual(disb["rows"], 1)
        # read-only reporting path re-exports everything already exported
        status, report = self._req("GET", "/export/mip?kind=in&include_exported=1")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(report["rows"], receipts["rows"])
        self.assertEqual(report["skipped_already_exported"], False)

    def test_simulate_refused_with_real_wallet(self):
        """A non-mock chain adapter must never serve the funding endpoint."""
        order = self._make_order(2.0)
        old_rpc = config.MAZAD_RPC_URL
        config.MAZAD_RPC_URL = "http://127.0.0.1:1"  # real-wallet mode
        try:
            status, err = self._req(
                "POST", f"/orders/{order['id']}/simulate_payment", {}
            )
            self.assertEqual(status, 403, err)
            self.assertIn("mock-chain mode", err["error"])
        finally:
            config.MAZAD_RPC_URL = old_rpc
        # back on the mock chain the same call succeeds
        status, sim = self._req("POST", f"/orders/{order['id']}/simulate_payment", {})
        self.assertEqual(status, 200, sim)

    def test_unknown_routes_and_ids(self):
        status, err = self._req("GET", "/orders/ord_does_not_exist")
        self.assertEqual(status, 404)
        self.assertIn("error", err)
        status, err = self._req("POST", "/definitely/not/a/route", {})
        self.assertEqual(status, 404)
        status, err = self._req("GET", "/export/mip", token=None)
        self.assertEqual(status, 401)  # export stays tokened even for odd queries


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
