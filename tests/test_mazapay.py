"""MazaPay bridge test suite — fully offline (fixed price + MockChain).

Covers the v0.2 safety rework: bearer auth, append-only audit enforcement,
intent-first disbursement with reconcile/retry, export idempotency, late
payment reconciliation, address validation, Decimal money math, strict
input parsing, and the fail-closed real-mode startup guards.
"""
import json
import os
import sqlite3
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest import mock

try:
    import _sandbox as _env
except ImportError:  # package-style invocation (python3 -m unittest tests.test_x)
    from tests import _sandbox as _env  # type: ignore

_age_claims = _env._age_claims
requires_writable = _env.requires_writable
net_gate = _env.net_gate  # for API-server test classes (bind local HTTP servers)


def run_concurrent(fn: Callable[[int], Any], n: int = 2) -> List[Any]:
    """Run fn(i) for i in range(n) on parallel threads (for race tests)."""
    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(fn, range(n)))

# _sandbox configured MAZAPAY_DB / MAZAPAY_MIP_EXPORT_DIR / price source.

from decimal import Decimal  # noqa: E402

from mazapay import api, config, mip_export, pricing, store, watcher  # noqa: E402
from mazapay.mazad import MockChain, get_chain  # noqa: E402


@requires_writable
class TestPricing(unittest.TestCase):
    def test_quote_math_fixed_price(self):
        maza, rate, source = pricing.quote_maza(50.0)
        self.assertEqual(rate, Decimal("0.002"))
        self.assertEqual(maza, Decimal("25000.00"))
        self.assertEqual(source, "fixed")

    def test_quote_decimal_rounds_down(self):
        maza, _rate, _src = pricing.quote_maza("0.0031")  # exactly 1.55
        self.assertEqual(maza, Decimal("1.55"))
        maza2, _, _ = pricing.quote_maza("0.00314")  # exactly 1.57
        self.assertEqual(maza2, Decimal("1.57"))
        maza3, _, _ = pricing.quote_maza("0.00314999")  # 1.574995 -> 1.57
        self.assertEqual(maza3, Decimal("1.57"))

    def test_rejects_nonpositive(self):
        with self.assertRaises(ValueError):
            pricing.quote_maza(0)
        with self.assertRaises(ValueError):
            pricing.quote_maza(-5)

    def test_paprika_parse_live_shape(self):
        fake = {
            "id": "maza-maza",
            "quotes": {"USD": {"price": 0.0017457656066163997, "volume_24h": 166.89}},
            "last_updated": "2026-11-14T23:45:14Z",
        }
        old_src, old_allow = config.PRICE_SOURCE, config.ALLOW_FIXED_PRICE
        config.PRICE_SOURCE = "paprika"
        try:
            with mock.patch.object(pricing, "_fetch_json", return_value=fake):
                price, source = pricing.get_maza_usd()
            self.assertEqual(source, "paprika")
            self.assertEqual(price, Decimal("0.0017457656066163997"))
        finally:
            config.PRICE_SOURCE, config.ALLOW_FIXED_PRICE = old_src, old_allow

    def test_failed_primary_raises_without_explicit_allowance(self):
        old_src, old_allow = config.PRICE_SOURCE, config.ALLOW_FIXED_PRICE
        config.PRICE_SOURCE, config.ALLOW_FIXED_PRICE = "paprika", False
        try:
            with mock.patch.object(pricing, "_fetch_json", side_effect=OSError("down")):
                with self.assertRaises(pricing.PriceUnavailable):
                    pricing.get_maza_usd()
        finally:
            config.PRICE_SOURCE, config.ALLOW_FIXED_PRICE = old_src, old_allow

    def test_synthetic_fallback_is_auditable_when_allowed(self):
        old_src, old_allow = config.PRICE_SOURCE, config.ALLOW_FIXED_PRICE
        config.PRICE_SOURCE, config.ALLOW_FIXED_PRICE = "paprika", True
        try:
            with mock.patch.object(pricing, "_fetch_json", side_effect=OSError("down")):
                price, source = pricing.get_maza_usd()
            self.assertEqual(source, "fixed-fallback")
            self.assertEqual(price, Decimal(str(config.FIXED_USD_PRICE)))
        finally:
            config.PRICE_SOURCE, config.ALLOW_FIXED_PRICE = old_src, old_allow


@requires_writable
class TestStore(unittest.TestCase):
    def setUp(self) -> None:
        # unittest runs classes ALPHABETICALLY, so TestApiEndToEnd's setUpClass
        # (which also calls init_db) may be SKIPPED in socket-restricted
        # sandboxes - every store-backed class must initialize its own schema.
        # init_db is idempotent (CREATE TABLE IF NOT EXISTS + tolerant _migrate).
        store.init_db()

    def test_vendor_roundtrip(self):
        v = store.create_vendor("Ponca Tribe Demo", "PONCA", "MADDR1", "demo vendor")
        got = store.get_vendor(v["id"])
        self.assertIsNotNone(got)
        self.assertEqual(got["name"], "Ponca Tribe Demo")
        self.assertEqual(got["maza_address"], "MADDR1")

    def test_order_lifecycle_and_expiry(self):
        o = store.create_order("in", 10.0, 5000.0, 0.002, "fixed", address="tMAZA-X")
        self.assertEqual(o["status"], "quoted")
        # before expiry: this order stays quoted (older leftovers may expire)
        store.mark_expired_stale(now=o["expires_ts"] - 1)
        mid = store.get_order(o["id"])
        self.assertIsNotNone(mid)
        self.assertEqual(mid["status"], "quoted")
        # at expiry: THIS order flips to expired
        store.mark_expired_stale(now=o["expires_ts"] + 1)
        expired = store.get_order(o["id"])
        self.assertIsNotNone(expired)
        self.assertEqual(expired["status"], "expired")

    def test_audit_is_append_only_at_schema_level(self):
        conn = sqlite3.connect(config.DB_PATH)
        try:
            conn.execute(
                "INSERT INTO audit (ts, entity, entity_id, event, detail) VALUES (?,?,?,?,?)",
                (1.0, "t", "t", "seed", "{}"),
            )
            row = conn.execute("SELECT id FROM audit WHERE event='seed'").fetchone()
            self.assertIsNotNone(row)
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE audit SET event='tampered' WHERE id=?", (row[0],))
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("DELETE FROM audit WHERE id=?", (row[0],))
            conn.rollback()  # the seed row insert itself is rolled back
        finally:
            conn.close()

    def test_export_state_tracking(self):
        o = store.create_order("in", 3.0, 1500.0, 0.002, "fixed", address="tMAZA-EX")
        store.update_order(o["id"], {"status": "paid", "paid_ts": 1799999999}, "receipt_confirmed")
        unexported = store.orders_unexported("in", ["paid"])
        self.assertTrue(any(r["id"] == o["id"] for r in unexported))
        store.mark_exported([o["id"]], "exporttest01")
        self.assertEqual(store.orders_unexported("in", ["paid"]), [])

    def test_unique_disbursement_nonce(self):
        store.create_order("out", 1.0, 500.0, 0.002, "fixed", address="A",
                           initial_status="pending_send", nonce="nonceA")
        with self.assertRaises(sqlite3.IntegrityError):
            store.create_order("out", 1.0, 500.0, 0.002, "fixed", address="A",
                               initial_status="pending_send", nonce="nonceA")


@requires_writable
class TestMockChain(unittest.TestCase):
    def setUp(self):
        self.chain = MockChain()

    def test_received_filters_by_confirmations(self):
        addr = self.chain.getnewaddress("t")
        self.chain.fund(addr, 10, confirmations=1)
        self.chain.fund(addr, 20, confirmations=6)
        self.assertAlmostEqual(self.chain.getreceivedbyaddress(addr, 1), 30.0)
        self.assertAlmostEqual(self.chain.getreceivedbyaddress(addr, 6), 20.0)

    def test_sendtoaddress_records_outflow(self):
        addr = self.chain.getnewaddress("t")
        txid = self.chain.sendtoaddress(addr, 7.5, "demo")
        self.assertTrue(txid.startswith("mock"))
        tx = [t for t in self.chain.listtransactions() if t["txid"] == txid][0]
        self.assertEqual(tx["amount"], -7.5)
        self.assertEqual(tx["category"], "send")

    def test_timeout_after_broadcast_records_the_send(self):
        addr = self.chain.getnewaddress("t")
        self.chain.fail_next_send(1, broadcast_first=True)
        with self.assertRaises(Exception):
            self.chain.sendtoaddress(addr, 5.0, "mazapay:n1")
        match = watcher.find_send_txid(self.chain, addr, 5.0, "mazapay:n1")
        self.assertTrue(match)  # the broadcast DID happen; reconcile can find it

    def test_pre_broadcast_failure_sends_nothing(self):
        addr = self.chain.getnewaddress("t")
        self.chain.fail_next_send(1, broadcast_first=False)
        with self.assertRaises(Exception):
            self.chain.sendtoaddress(addr, 5.0, "mazapay:n2")
        self.assertEqual(watcher.find_send_txid(self.chain, addr, 5.0, "mazapay:n2"), "")

    def test_scan_failure_raises_never_returns_absent(self):
        addr = self.chain.getnewaddress("t")
        self.chain.fail_next_scan(1)
        with self.assertRaises(Exception):
            watcher.find_send_txid(self.chain, addr, 5.0, "mazapay:n3")

    def test_scan_depth_uses_configured_limit(self):
        addr = self.chain.getnewaddress("t")
        self.chain.sendtoaddress(addr, 1.0, "mazapay:depth")
        watcher.find_send_txid(self.chain, addr, 1.0, "mazapay:depth")
        self.assertEqual(self.chain.last_scan_count, config.TX_SCAN_LIMIT)


@requires_writable
class TestWatcher(unittest.TestCase):
    def setUp(self) -> None:
        store.init_db()  # order-independent schema (see TestStore.setUp)

    def test_pending_not_paid_below_confirmations(self):
        chain, mode = get_chain()
        o = store.create_order(
            "in", 4.0, 2000.0, 0.002, "fixed", address=chain.getnewaddress("w1")
        )
        store.update_order(o["id"], {"status": "awaiting"}, "awaiting_funds")
        chain.fund(o["address"], 2000.0, confirmations=config.CONFIRMATIONS_REQUIRED - 1)
        counts = watcher.poll_once()
        still = store.get_order(o["id"])
        self.assertIsNotNone(still)
        self.assertEqual(still["status"], "awaiting")
        self.assertGreaterEqual(counts["still_pending"], 1)

    def test_paid_at_required_confirmations_with_txid(self):
        chain, _mode = get_chain()
        o = store.create_order(
            "in", 4.0, 2000.0, 0.002, "fixed", address=chain.getnewaddress("w2")
        )
        store.update_order(o["id"], {"status": "awaiting"}, "awaiting_funds")
        chain.fund(o["address"], 2000.0, confirmations=config.CONFIRMATIONS_REQUIRED)
        counts = watcher.poll_once()
        self.assertGreaterEqual(counts["paid"], 1)
        paid = store.get_order(o["id"])
        self.assertIsNotNone(paid)
        self.assertEqual(paid["status"], "paid")
        self.assertIsNotNone(paid["paid_ts"])
        self.assertIsNotNone(paid["txid"])

    def test_late_payment_reaches_late_paid_not_dropped(self):
        chain, _mode = get_chain()
        o = store.create_order(
            "in", 6.0, 3000.0, 0.002, "fixed", address=chain.getnewaddress("w3")
        )
        store.update_order(o["id"], {"status": "awaiting"}, "awaiting_funds")
        store.mark_expired_stale(now=(o["expires_ts"] or 0) + 1)  # quote expires...
        self.assertEqual(store.get_order(o["id"])["status"], "expired")
        # ...then the member pays anyway
        chain.fund(o["address"], 3000.0, confirmations=config.CONFIRMATIONS_REQUIRED)
        counts = watcher.poll_once()
        self.assertGreaterEqual(counts["late_paid"], 1)
        late = store.get_order(o["id"])
        self.assertIsNotNone(late)
        self.assertEqual(late["status"], "late_paid")
        self.assertIsNotNone(late["txid"])


@requires_writable
class TestMipExport(unittest.TestCase):
    def setUp(self):
        store.init_db()  # order-independent schema (see TestStore.setUp)
        # Flush any un-exported orders left by earlier test classes so each
        # export test below asserts on exactly the rows it creates.
        store.mark_exported(
            [r["id"] for r in store.orders_unexported("in", ["paid", "late_paid"])],
            "setup-flush-in",
        )
        store.mark_exported(
            [r["id"] for r in store.orders_unexported("out", ["sent"])],
            "setup-flush-out",
        )

    def test_cash_receipts_csv(self):
        chain, _mode = get_chain()
        o = store.create_order(
            "in", 100.0, 50000.0, 0.002, "fixed", ref="dues-oct",
            address=chain.getnewaddress("mip-r"),
        )
        store.update_order(o["id"], {"status": "awaiting"}, "awaiting_funds")
        chain.fund(o["address"], 50000.0, confirmations=config.CONFIRMATIONS_REQUIRED)
        watcher.poll_once()
        result = mip_export.export_cash_receipts()
        self.assertEqual(result["rows"], 1)
        self.assertEqual(result["order_ids"], [o["id"]])
        with open(result["path"], newline="", encoding="utf-8") as fh:
            content = fh.read()
        self.assertIn("SESSION_ID", content)
        self.assertIn(o["id"], content)
        self.assertIn("100.00", content)
        self.assertIn("MAZA 50000.0 @ 0.002", content)

    def test_reexport_contains_no_new_rows(self):
        second = mip_export.export_cash_receipts()
        self.assertEqual(second["rows"], 0)  # every paid order was already exported once

    def test_crash_between_claim_and_write_resumes_same_session(self):
        # a fresh paid receipt this test owns
        chain, _mode = get_chain()
        o = store.create_order(
            "in", 14.0, 7000.0, 0.002, "fixed", address=chain.getnewaddress("crash"),
        )
        store.update_order(o["id"], {"status": "paid", "paid_ts": 1799999999}, "x")
        # Simulate a crash after CLAIM: the atomic write raises once.
        boom = RuntimeError("crash before write completed")
        with mock.patch.object(
            mip_export, "_write_csv_atomic", side_effect=boom
        ):
            with self.assertRaises(RuntimeError):
                mip_export.export_cash_receipts()
        # rows are claimed with a session id but no file was finalized
        claimed_id = store.pending_claim("in", 0)
        self.assertIsNotNone(claimed_id)
        # age the lease: simulate the recovery run happening later
        _age_claims(config.DB_PATH)
        # the next run ADOPTS the crashed session and completes it
        result = mip_export.export_cash_receipts()
        self.assertTrue(result["resumed_claim"])
        self.assertEqual(result["export_id"], claimed_id)
        self.assertIn(o["id"], result["order_ids"])
        self.assertIsNone(store.pending_claim("in", 0))
        # and a further run emits nothing new
        again = mip_export.export_cash_receipts()
        self.assertEqual(again["rows"], 0)
        self.assertFalse(again["resumed_claim"])

    def test_claimed_row_whose_status_left_export_set_is_released(self):
        o = store.create_order("in", 2.0, 1000.0, 0.002, "fixed", address="tMAZA-REL")
        store.update_order(o["id"], {"status": "late_paid", "paid_ts": 1799999999}, "x")
        with mock.patch.object(mip_export, "_write_csv_atomic", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                mip_export.export_cash_receipts()  # claims the late_paid row, crashes
        # the operator refunds it while the session is still only claimed
        store.transition(o["id"], "late_paid", "refunding", event="t")
        store.transition(o["id"], "refunding", "refunded", event="t2")
        _age_claims(config.DB_PATH)  # recovery run happens later, lease aged
        result = mip_export.export_cash_receipts()  # resume releases the refunded row
        self.assertNotIn(o["id"], result["order_ids"])  # never exported against its state
        got = store.get_order(o["id"])
        self.assertIsNotNone(got)
        self.assertIsNone(got["export_state"])  # released back to unclaimed

    def test_concurrent_exporters_partition_rows(self):
        chain, _mode = get_chain()
        made = []
        for i in range(2):
            o = store.create_order(
                "in", 1.0 + i, 500.0 + i, 0.002, "fixed",
                address=chain.getnewaddress(f"part{i}"),
            )
            store.update_order(o["id"], {"status": "paid", "paid_ts": 1799999999}, "x")
            made.append(o["id"])
        results = run_concurrent(lambda _i: mip_export.export_cash_receipts())
        all_ids = [oid for r in results for oid in r["order_ids"]]
        self.assertEqual(sorted(all_ids), sorted(made))  # each order in exactly one session
        self.assertEqual(len(set(r["export_id"] for r in results)), len(results))
        # nothing left unclaimed
        self.assertEqual(store.orders_unexported("in", ["paid"]), [])

    def test_crash_after_write_before_finalize_rewrites_same_file(self):
        # fresh paid receipt this test owns
        chain, _mode = get_chain()
        o = store.create_order(
            "in", 21.0, 10500.0, 0.002, "fixed", address=chain.getnewaddress("pw"),
        )
        store.update_order(o["id"], {"status": "paid", "paid_ts": 1799999999}, "x")
        # crash AFTER write (replace) but BEFORE finalize: finalize is a no-op
        with mock.patch.object(mip_export.store, "finalize_export", return_value=0):
            first = mip_export.export_cash_receipts()
        self.assertTrue(os.path.exists(first["path"]))
        self.assertEqual(store.pending_claim("in", 0), first["export_id"])
        _age_claims(config.DB_PATH)  # recovery run happens later, lease aged
        second = mip_export.export_cash_receipts()
        self.assertTrue(second["resumed_claim"])
        self.assertEqual(second["export_id"], first["export_id"])
        # the resumed session REWRITES THE SAME FILE — no second importable CSV
        self.assertEqual(second["path"], first["path"])
        sessions = [
            f for f in os.listdir(config.MIP_EXPORT_DIR)
            if f.startswith("mip_cash_receipts_") and first["export_id"] in f
        ]
        self.assertEqual(len(sessions), 1)
        self.assertEqual(second["rows"], 1)
        self.assertEqual(sorted(second["order_ids"]), [o["id"]])

    def test_concurrent_adopt_stale_claim_single_winner(self):
        chain, _mode = get_chain()
        o = store.create_order(
            "in", 22.0, 11000.0, 0.002, "fixed", address=chain.getnewaddress("adopt"),
        )
        store.update_order(o["id"], {"status": "paid", "paid_ts": 1799999999}, "x")
        session_id = "adoptme123"
        store.claim_unexported("in", ["paid"], session_id)
        _age_claims(config.DB_PATH)  # lease stale -> adoptable
        winners = run_concurrent(lambda _i: store.adopt_stale_claim("in", 3600))
        got = [w for w in winners if w]
        self.assertEqual(len(got), 1)  # exactly one recovery worker adopted
        self.assertEqual(got[0], session_id)
        # the winner renewed the lease; complete the session so no stale
        # claim leaks into later tests
        store.finalize_export(session_id)
        self.assertEqual(store.pending_claim("in", 3600), None)

    def test_adopt_race_with_multirow_session(self):
        # a session spanning MULTIPLE rows: a per-row lease renewal would let
        # two workers each renew a different row and both "recover" it
        chain, _mode = get_chain()
        for i in range(2):
            o = store.create_order(
                "in", 30.0 + i, 1500.0 + i, 0.002, "fixed",
                address=chain.getnewaddress(f"multi{i}"),
            )
            store.update_order(o["id"], {"status": "paid", "paid_ts": 1799999999}, "x")
        store.claim_unexported("in", ["paid"], "multi123")  # claims both rows
        _age_claims(config.DB_PATH)  # lease stale -> adoptable
        winners = run_concurrent(lambda _i: store.adopt_stale_claim("in", 3600))
        got = [w for w in winners if w]
        self.assertEqual(len(got), 1)  # exactly one worker adopted the session
        self.assertEqual(got[0], "multi123")
        # the winner's renewal covers BOTH rows: nothing stale remains
        self.assertIsNone(store.adopt_stale_claim("in", 3600))
        store.finalize_export("multi123")

    def test_cash_disbursements_csv(self):
        o = store.create_order(
            "out", 25.0, 12500.0, 0.002, "fixed", address="MADDR9",
            initial_status="pending_send", nonce="exp-nonce-1",
        )
        store.update_order(
            o["id"], {"status": "sent", "txid": "mocktx99", "paid_ts": 1799999999}, "disbursed"
        )
        result = mip_export.export_cash_disbursements()
        self.assertGreaterEqual(result["rows"], 1)
        with open(result["path"], newline="", encoding="utf-8") as fh:
            content = fh.read()
        self.assertIn("mocktx99", content)
        self.assertIn("25.00", content)
        self.assertNotIn(".tmp.", result["path"])  # atomic temp files never leak names
        reexport = mip_export.export_cash_disbursements()
        self.assertEqual(reexport["rows"], 0)


@net_gate
class TestApiEndToEnd(unittest.TestCase):
    """Real HTTP against a real server thread: quote -> fund -> watch -> export."""

    @classmethod
    def setUpClass(cls):
        store.init_db()
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

    def _req(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Tuple[int, Any]:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if config.API_TOKEN:
            headers["Authorization"] = f"Bearer {config.API_TOKEN}"
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method, headers=headers
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode()
                try:
                    return resp.status, json.loads(raw)
                except ValueError:
                    return resp.status, raw  # non-JSON body (e.g. the checkout page)
        except urllib.error.HTTPError as exc:  # 4xx/5xx carry JSON bodies too
            payload = exc.read().decode() or "{}"
            exc.close()
            try:
                return exc.code, json.loads(payload)
            except ValueError:
                return exc.code, payload

    def test_full_collect_flow(self):
        status, health = self._req("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(health["chain_mode"], "mock")
        self.assertEqual(health["auth_mode"], "open")

        status, vendor = self._req(
            "POST", "/vendors", {"name": "Test Supplier", "code": "TSUP", "maza_address": "MADDR88"}
        )
        self.assertEqual(status, 201)

        status, order = self._req(
            "POST", "/orders", {"usd_amount": 20.0, "vendor_id": vendor["id"], "ref": "invoice-42"}
        )
        self.assertEqual(status, 201)
        self.assertEqual(order["status"], "awaiting")
        self.assertEqual(order["maza_amount"], 10000.0)
        self.assertEqual(order["rate"], 0.002)
        self.assertTrue(order["address"])

        # below confirmations -> still awaiting
        self._req("POST", f"/orders/{order['id']}/simulate_payment", {"confirms": 1})
        self._req("POST", "/watch", {})
        _s, o1 = self._req("GET", f"/orders/{order['id']}")
        self.assertEqual(o1["status"], "awaiting")

        # full confirmations -> watcher marks paid
        self._req(
            "POST", f"/orders/{order['id']}/simulate_payment", {"confirms": config.CONFIRMATIONS_REQUIRED}
        )
        self._req("POST", "/watch", {})
        _s, o2 = self._req("GET", f"/orders/{order['id']}")
        self.assertEqual(o2["status"], "paid")
        self.assertIsNotNone(o2["txid"])

        status, export = self._req("GET", "/export/mip?kind=in")
        self.assertEqual(status, 200)
        self.assertTrue(os.path.exists(export["path"]))

    def test_disburse_intent_first_then_sent(self):
        status, pay = self._req(
            "POST", "/payments", {"address": "MADDR77", "usd_amount": 12.0, "ref": "po-7"}
        )
        self.assertEqual(status, 201)
        self.assertEqual(pay["status"], "sent")
        self.assertEqual(pay["maza_amount"], 6000.0)
        self.assertTrue(pay["txid"])
        status, export = self._req("GET", "/export/mip?kind=out")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(export["rows"], 1)

    def test_rpc_timeout_after_broadcast_reconciles_without_duplicate(self):
        chain, _mode = get_chain()
        chain.fail_next_send(1, broadcast_first=True)
        status, pay = self._req(
            "POST", "/payments", {"address": "MADDR71", "usd_amount": 8.0, "ref": "po-uncertain"}
        )
        self.assertEqual(status, 201)
        self.assertEqual(pay["status"], "send_uncertain")
        self.assertIsNone(pay["txid"])

        status, rec = self._req("POST", f"/payments/{pay['id']}/reconcile", {})
        self.assertEqual(status, 200)
        self.assertEqual(rec["status"], "sent")
        self.assertTrue(rec["txid"])
        self.assertEqual(rec["reconciled"], "broadcast_found")

        # idempotent: reconcile again is a no-op, no second send
        tx_count_before = len(chain.listtransactions())
        status, rec2 = self._req("POST", f"/payments/{pay['id']}/reconcile", {})
        self.assertEqual(rec2["reconciled"], "already_sent_noop")
        self.assertEqual(len(chain.listtransactions()), tx_count_before)

        # retry is refused for a payment that already has a txid
        status, _body = self._req("POST", f"/payments/{pay['id']}/retry", {})
        self.assertEqual(status, 409)

    def test_pre_broadcast_failure_retries_once_and_refuses_more(self):
        chain, _mode = get_chain()
        chain.fail_next_send(1, broadcast_first=False)
        status, pay = self._req(
            "POST", "/payments", {"address": "MADDR72", "usd_amount": 3.0, "ref": "po-failed"}
        )
        self.assertEqual(status, 201)
        self.assertEqual(pay["status"], "send_uncertain")

        status, rec = self._req("POST", f"/payments/{pay['id']}/reconcile", {})
        self.assertEqual(rec["status"], "send_failed")
        self.assertEqual(rec["reconciled"], "no_broadcast_found")

        status, retry = self._req("POST", f"/payments/{pay['id']}/retry", {})
        self.assertEqual(status, 200)
        self.assertEqual(retry["status"], "sent")
        self.assertTrue(retry["txid"])

        status, _body = self._req("POST", f"/payments/{pay['id']}/retry", {})
        self.assertEqual(status, 409)  # the row is now 'sent'; retry refused

    def test_invalid_address_rejected(self):
        status, body = self._req(
            "POST", "/payments", {"address": "garbage", "usd_amount": 1.0}
        )
        self.assertEqual(status, 400)
        self.assertIn("invalid MAZA address", body["error"])

    def test_malformed_inputs_return_400(self):
        status, _ = self._req("POST", "/orders", {"usd_amount": "abc"})
        self.assertEqual(status, 400)
        status, _ = self._req("POST", "/orders", {"usd_amount": -1})
        self.assertEqual(status, 400)
        status, _ = self._req("POST", "/orders", {})
        self.assertEqual(status, 400)
        status, _ = self._req(
            "POST", "/payments", {"address": "MADDR73", "usd_amount": "1.0.0.0"}
        )
        self.assertEqual(status, 400)

    def test_late_payment_accept_and_refund(self):
        chain, _mode = get_chain()
        status, order = self._req("POST", "/orders", {"usd_amount": 9.0, "ref": "late-1"})
        self.assertEqual(status, 201)
        # expire the quote, then the funds arrive anyway
        store.mark_expired_stale(now=(order["expires_ts"] or 0) + 1)
        chain.fund(order["address"], 9000.0, confirmations=config.CONFIRMATIONS_REQUIRED)
        self._req("POST", "/watch", {})
        _s, late = self._req("GET", f"/orders/{order['id']}")
        self.assertEqual(late["status"], "late_paid")

        status, accepted = self._req("POST", f"/orders/{order['id']}/accept", {})
        self.assertEqual(status, 200)
        self.assertEqual(accepted["status"], "paid")

        # second late order -> refund path
        status, order2 = self._req("POST", "/orders", {"usd_amount": 7.0, "ref": "late-2"})
        store.mark_expired_stale(now=(order2["expires_ts"] or 0) + 1)
        chain.fund(order2["address"], 3500.0, confirmations=config.CONFIRMATIONS_REQUIRED)
        self._req("POST", "/watch", {})
        status, refund = self._req(
            "POST", f"/orders/{order2['id']}/refund", {"address": "MADDRREFUND"}
        )
        self.assertEqual(status, 201)
        self.assertEqual(refund["status"], "refunded")  # the late order itself
        self.assertEqual(refund["refund"]["status"], "sent")  # the outbound refund payment
        self.assertEqual(refund["refund"]["maza_amount"], 3500.0)
        _s, refunded = self._req("GET", f"/orders/{order2['id']}")
        self.assertEqual(refunded["status"], "refunded")

    def test_accept_only_for_late_paid(self):
        status, order = self._req("POST", "/orders", {"usd_amount": 5.0, "ref": "acc-1"})
        status, _body = self._req("POST", f"/orders/{order['id']}/accept", {})
        self.assertEqual(status, 409)

    def test_error_paths(self):
        _s, body = self._req("GET", "/orders/nope")
        self.assertIn("error", body)

    # -- round-2 adversarial paths (judge findings) ---------------------------

    def test_rpc_scan_failure_keeps_send_uncertain(self):
        chain, _mode = get_chain()
        chain.fail_next_send(1, broadcast_first=True)
        status, pay = self._req(
            "POST", "/payments", {"address": "MADDR81", "usd_amount": 6.0, "ref": "scan-fail"}
        )
        self.assertEqual(pay["status"], "send_uncertain")
        # now the scan itself is unavailable — this must NOT become send_failed
        chain.fail_next_scan(1)
        status, rec = self._req("POST", f"/payments/{pay['id']}/reconcile", {})
        self.assertEqual(status, 200)
        self.assertEqual(rec["status"], "send_uncertain")
        self.assertTrue(str(rec["reconciled"]).startswith("scan_unavailable"))
        # retry stays refused while uncertain
        status, _body = self._req("POST", f"/payments/{pay['id']}/retry", {})
        self.assertEqual(status, 409)
        # scan recovers -> reconcile binds the broadcast
        status, rec2 = self._req("POST", f"/payments/{pay['id']}/reconcile", {})
        self.assertEqual(rec2["status"], "sent")
        self.assertTrue(rec2["txid"])

    def _concurrent(self, fn, n=2):
        return run_concurrent(fn, n)

    def test_concurrent_retry_broadcasts_exactly_once(self):
        chain, _mode = get_chain()
        chain.fail_next_send(1, broadcast_first=False)
        status, pay = self._req(
            "POST", "/payments", {"address": "MADDR82", "usd_amount": 4.0, "ref": "race-retry"}
        )
        self.assertEqual(pay["status"], "send_uncertain")
        status, rec = self._req("POST", f"/payments/{pay['id']}/reconcile", {})
        self.assertEqual(rec["status"], "send_failed")

        sends_before = len([t for t in chain.listtransactions() if t["address"] == "MADDR82"])
        results = self._concurrent(
            lambda _i: self._req("POST", f"/payments/{pay['id']}/retry", {})
        )
        statuses = [s for s, _ in results]
        self.assertEqual(statuses.count(200), 1)  # exactly one retry won the CAS
        self.assertEqual(statuses.count(409), len(results) - 1)
        sends_after = len([t for t in chain.listtransactions() if t["address"] == "MADDR82"])
        self.assertEqual(sends_after - sends_before, 1)  # one broadcast, no duplicates

    def test_concurrent_refund_pays_exactly_once(self):
        chain, _mode = get_chain()
        status, order = self._req("POST", "/orders", {"usd_amount": 11.0, "ref": "race-refund"})
        store.mark_expired_stale(now=(order["expires_ts"] or 0) + 1)
        chain.fund(order["address"], 5500.0, confirmations=config.CONFIRMATIONS_REQUIRED)
        self._req("POST", "/watch", {})

        results = self._concurrent(
            lambda _i: self._req(
                "POST", f"/orders/{order['id']}/refund", {"address": "MADDR83"}
            )
        )
        statuses = [s for s, _ in results]
        self.assertEqual(statuses.count(201), 1)  # one refund won the CAS
        self.assertEqual(statuses.count(409), len(results) - 1)
        refund_sends = [
            t for t in chain.listtransactions()
            if t["address"] == "MADDR83" and t.get("category") == "send"
        ]
        self.assertEqual(len(refund_sends), 1)
        _s, final = self._req("GET", f"/orders/{order['id']}")
        self.assertEqual(final["status"], "refunded")

    def test_concurrent_accept_and_refund_single_winner(self):
        chain, _mode = get_chain()
        status, order = self._req("POST", "/orders", {"usd_amount": 13.0, "ref": "race-both"})
        store.mark_expired_stale(now=(order["expires_ts"] or 0) + 1)
        chain.fund(order["address"], 6500.0, confirmations=config.CONFIRMATIONS_REQUIRED)
        self._req("POST", "/watch", {})

        results = self._concurrent(
            lambda i: (
                self._req("POST", f"/orders/{order['id']}/accept", {})
                if i == 0
                else self._req("POST", f"/orders/{order['id']}/refund", {"address": "MADDR84"})
            )
        )
        statuses = sorted(s for s, _ in results)
        self.assertEqual(statuses.count(409), 1)  # the loser of the CAS
        self.assertIn(statuses[0], (200, 201))
        _s, final = self._req("GET", f"/orders/{order['id']}")
        self.assertIn(final["status"], ("paid", "refunded"))

    def test_refund_returns_original_locked_amount_when_price_moves(self):
        chain, _mode = get_chain()
        status, order = self._req("POST", "/orders", {"usd_amount": 7.0, "ref": "lock-refund"})
        store.mark_expired_stale(now=(order["expires_ts"] or 0) + 1)
        chain.fund(order["address"], 3500.0, confirmations=config.CONFIRMATIONS_REQUIRED)
        self._req("POST", "/watch", {})
        _s, late = self._req("GET", f"/orders/{order['id']}")
        self.assertEqual(late["status"], "late_paid")

        # the market halves the price AFTER receipt — refund must not change
        old_price = config.FIXED_USD_PRICE
        config.FIXED_USD_PRICE = old_price * 2
        try:
            status, resp = self._req(
                "POST", f"/orders/{order['id']}/refund", {"address": "MADDR85"}
            )
        finally:
            config.FIXED_USD_PRICE = old_price
        self.assertEqual(status, 201)
        self.assertEqual(resp["refund"]["maza_amount"], 3500.0)  # original locked quantity
        self.assertEqual(resp["refund"]["rate"], 0.002)  # original locked rate

    def test_uncertain_refund_reconciled_sent_settles_parent(self):
        chain, _mode = get_chain()
        status, order = self._req("POST", "/orders", {"usd_amount": 15.0, "ref": "link-1"})
        store.mark_expired_stale(now=(order["expires_ts"] or 0) + 1)
        chain.fund(order["address"], 7500.0, confirmations=config.CONFIRMATIONS_REQUIRED)
        self._req("POST", "/watch", {})
        chain.fail_next_send(1, broadcast_first=True)
        status, resp = self._req(
            "POST", f"/orders/{order['id']}/refund", {"address": "MADDR90"}
        )
        self.assertEqual(status, 201)
        self.assertEqual(resp["status"], "refunding")  # parent waits, visible
        refund_payment_id = resp["refund"]["id"]
        status, rec = self._req("POST", f"/payments/{refund_payment_id}/reconcile", {})
        self.assertEqual(rec["status"], "sent")
        _s, parent = self._req("GET", f"/orders/{order['id']}")
        self.assertEqual(parent["status"], "refunded")  # settled by reconcile

    def test_refund_proven_absent_releases_parent_to_late_paid(self):
        chain, _mode = get_chain()
        status, order = self._req("POST", "/orders", {"usd_amount": 16.0, "ref": "link-2"})
        store.mark_expired_stale(now=(order["expires_ts"] or 0) + 1)
        chain.fund(order["address"], 8000.0, confirmations=config.CONFIRMATIONS_REQUIRED)
        self._req("POST", "/watch", {})
        chain.fail_next_send(1, broadcast_first=False)
        status, resp = self._req(
            "POST", f"/orders/{order['id']}/refund", {"address": "MADDR91"}
        )
        refund_payment_id = resp["refund"]["id"]
        status, rec = self._req("POST", f"/payments/{refund_payment_id}/reconcile", {})
        self.assertEqual(rec["status"], "send_failed")
        _s, parent = self._req("GET", f"/orders/{order['id']}")
        self.assertEqual(parent["status"], "late_paid")  # released for another refund try
        # the old refund payment is SUPERSEDED: terminal, not retryable
        old_payment = store.get_order(refund_payment_id)
        self.assertIsNotNone(old_payment)
        self.assertEqual(old_payment["status"], "superseded")
        status, _body = self._req("POST", f"/payments/{refund_payment_id}/retry", {})
        self.assertEqual(status, 409)  # the duplicate-refund path is closed
        # a replacement refund works
        status, retry_refund = self._req(
            "POST", f"/orders/{order['id']}/refund", {"address": "MADDR91"}
        )
        self.assertEqual(status, 201)
        self.assertEqual(retry_refund["status"], "refunded")
        # and exactly ONE refund broadcast ever reached the payee address
        refund_sends = [
            t for t in chain.listtransactions()
            if t["address"] == "MADDR91" and t.get("category") == "send"
        ]
        self.assertEqual(len(refund_sends), 1)

    def test_concurrent_reconcile_keeps_superseded_terminal(self):
        chain, _mode = get_chain()
        status, order = self._req("POST", "/orders", {"usd_amount": 17.0, "ref": "race-rec"})
        store.mark_expired_stale(now=(order["expires_ts"] or 0) + 1)
        chain.fund(order["address"], 8500.0, confirmations=config.CONFIRMATIONS_REQUIRED)
        self._req("POST", "/watch", {})
        chain.fail_next_send(1, broadcast_first=False)
        status, resp = self._req(
            "POST", f"/orders/{order['id']}/refund", {"address": "MADDR93"}
        )
        refund_payment_id = resp["refund"]["id"]
        self.assertEqual(resp["refund"]["status"], "send_uncertain")

        # two concurrent reconciles of the same uncertain refund
        results = self._concurrent(
            lambda _i: self._req("POST", f"/payments/{refund_payment_id}/reconcile", {})
        )
        self.assertEqual(len(results), 2)
        # no matter the interleaving, the payment must end terminal-superseded
        # (a loser of the reconcile race can never clobber it back to send_failed)
        final = store.get_order(refund_payment_id)
        self.assertIsNotNone(final)
        self.assertEqual(final["status"], "superseded")
        status, _body = self._req("POST", f"/payments/{refund_payment_id}/retry", {})
        self.assertEqual(status, 409)  # still non-retryable under concurrency
        _s, parent = self._req("GET", f"/orders/{order['id']}")
        self.assertEqual(parent["status"], "late_paid")

    def test_reconcile_versus_retry_race_stays_consistent(self):
        """Reconcile (proves absent + supersedes) racing a retry (claims + broadcasts).

        Invariant under ANY interleaving: a payment that is broadcastable
        (pending_send) or already broadcast (sent, has a txid) is never
        marked superseded, and the parent order state always matches the
        payment outcome.
        """
        chain, _mode = get_chain()
        status, order = self._req("POST", "/orders", {"usd_amount": 18.0, "ref": "race-cvR"})
        store.mark_expired_stale(now=(order["expires_ts"] or 0) + 1)
        chain.fund(order["address"], 9000.0, confirmations=config.CONFIRMATIONS_REQUIRED)
        self._req("POST", "/watch", {})
        chain.fail_next_send(1, broadcast_first=False)
        status, resp = self._req(
            "POST", f"/orders/{order['id']}/refund", {"address": "MADDR94"}
        )
        refund_payment_id = resp["refund"]["id"]
        self.assertEqual(resp["refund"]["status"], "send_uncertain")
        # first reconcile proves absence (uncertain -> send_failed + supersede attempt)

        # now race: one reconcile-style settle pass (simulated by the store-level
        # operations reconcile would perform) against a concurrent retry
        def reconcile_settle(_i):
            won = store.transition(
                refund_payment_id, "send_uncertain", "send_failed",
                event="reconciled_no_broadcast",
            )
            if won:
                fresh = store.get_order(refund_payment_id)
                assert fresh is not None  # won the transition, the row exists
                api._settle_refund_parent(fresh, "send_failed")
            return won

        def do_retry(_i):
            return self._req("POST", f"/payments/{refund_payment_id}/retry", {})

        results = run_concurrent(
            lambda i: reconcile_settle(i) if i == 0 else do_retry(i)[0], n=2
        )
        final = store.get_order(refund_payment_id)
        self.assertIsNotNone(final)
        # INVARIANT: superseded only when never broadcastable; sent only with txid
        if final["status"] == "superseded":
            self.assertEqual(final["txid"], None)
            _s, parent = self._req("GET", f"/orders/{order['id']}")
            self.assertEqual(parent["status"], "late_paid")
        elif final["status"] == "sent":
            self.assertTrue(final["txid"])
            _s, parent = self._req("GET", f"/orders/{order['id']}")
            self.assertEqual(parent["status"], "refunded")
        else:
            self.fail(f"unexpected terminal state: {final['status']}")
        # and a broadcastable/broadcast payment was never superseded
        if final["status"] == "sent":
            superseded_tx = [
                t for t in chain.listtransactions()
                if t["address"] == "MADDR94" and t.get("category") == "send"
            ]
            self.assertEqual(len(superseded_tx), 1)  # exactly the retry's broadcast


@net_gate
class TestAuth(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        store.init_db()
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

    def _req(self, method: str, path: str, body: Optional[Dict[str, Any]] = None, token: Optional[str] = "x"):
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
                    return resp.status, raw  # non-JSON body (e.g. the checkout page)
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode() or "{}"
            exc.close()
            try:
                return exc.code, json.loads(payload)
            except ValueError:
                return exc.code, payload

    def test_token_required_on_mutating_and_read_routes(self):
        old = config.API_TOKEN
        config.API_TOKEN = "sekrit-token"
        try:
            status, _ = self._req("POST", "/vendors", {"name": "N", "code": "C"}, token=None)
            self.assertEqual(status, 401)
            status, _ = self._req("POST", "/vendors", {"name": "N", "code": "C"}, token="wrong")
            self.assertEqual(status, 401)
            status, _ = self._req("GET", "/vendors", token=None)
            self.assertEqual(status, 401)
            status, _ = self._req(
                "POST", "/payments", {"address": "MADDR80", "usd_amount": 1.0}, token=None
            )
            self.assertEqual(status, 401)
            status, _ = self._req("GET", "/health", token=None)
            self.assertEqual(status, 200)  # health stays open
            status, body = self._req(
                "POST", "/vendors", {"name": "Authed", "code": "AUTH1"}, token="sekrit-token"
            )
            self.assertEqual(status, 201)
            # member checkout is public in every mode: no money moves through it
            status, page = self._req("GET", "/", token=None)
            self.assertEqual(status, 200)
            self.assertIn("Pay with MAZA", page)
            status, order = self._req("POST", "/orders", {"usd_amount": 1.0}, token=None)
            self.assertEqual(status, 201)
            status, fetched = self._req("GET", f"/orders/{order['id']}", token=None)
            self.assertEqual(status, 200)
            # but refund (a spending action on the order) is tokened
            status, _ = self._req(
                "POST", f"/orders/{order['id']}/refund", {"address": "MADDRX"}, token=None
            )
            self.assertEqual(status, 401)
            # outbound payment IDs are NOT publicly readable: 404 without a
            # token (never leaking existence), 200 with one
            status, pay = self._req(
                "POST", "/payments", {"address": "MADDR80", "usd_amount": 1.0},
                token="sekrit-token",
            )
            self.assertEqual(status, 201)
            status, _ = self._req("GET", f"/orders/{pay['id']}", token=None)
            self.assertEqual(status, 404)
            status, fetched_pay = self._req("GET", f"/orders/{pay['id']}", token="sekrit-token")
            self.assertEqual(status, 200)
            self.assertEqual(fetched_pay["kind"], "out")
        finally:
            config.API_TOKEN = old

    def test_open_mode_when_no_token(self):
        old = config.API_TOKEN
        config.API_TOKEN = ""
        try:
            status, _body = self._req("POST", "/vendors", {"name": "Open", "code": "OPEN1"}, token=None)
            self.assertEqual(status, 201)
        finally:
            config.API_TOKEN = old


@requires_writable
class TestRealModeGuards(unittest.TestCase):
    def test_real_wallet_without_token_refused(self):
        old_rpc, old_tok = config.MAZAD_RPC_URL, config.API_TOKEN
        config.MAZAD_RPC_URL, config.API_TOKEN = "http://127.0.0.1:1", ""
        try:
            with self.assertRaises(RuntimeError):
                api.require_safe_mode()
        finally:
            config.MAZAD_RPC_URL, config.API_TOKEN = old_rpc, old_tok

    def test_real_wallet_with_fixed_price_refused_unless_allowed(self):
        old = (
            config.MAZAD_RPC_URL,
            config.API_TOKEN,
            config.PRICE_SOURCE,
            config.ALLOW_FIXED_PRICE,
        )
        config.MAZAD_RPC_URL, config.API_TOKEN = "http://127.0.0.1:1", "tok"
        config.PRICE_SOURCE, config.ALLOW_FIXED_PRICE = "fixed", False
        try:
            with self.assertRaises(RuntimeError):
                api.require_safe_mode()
            config.ALLOW_FIXED_PRICE = True
            api.require_safe_mode()  # explicit allowance passes
        finally:
            (
                config.MAZAD_RPC_URL,
                config.API_TOKEN,
                config.PRICE_SOURCE,
                config.ALLOW_FIXED_PRICE,
            ) = old

    def test_mock_mode_needs_no_token(self):
        old_rpc = config.MAZAD_RPC_URL
        config.MAZAD_RPC_URL = ""
        try:
            api.require_safe_mode()  # no raise in mock mode
        finally:
            config.MAZAD_RPC_URL = old_rpc


if __name__ == "__main__":
    unittest.main()
