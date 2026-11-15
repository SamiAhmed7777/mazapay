"""Security-hardening regression tests — one test per open audit finding.

Findings closed by this module (audit /root/mazapay-security-audit.md):

- H-1 intake   — POST /orders `ref` validated (formula payloads, control
                 characters, >120 chars are 400; hostile input never reaches
                 the store or the MIP export)
- H-2 caps     — per-order USD ceiling (400), per-client-IP and global
                 awaiting-order caps (429 + Retry-After, X-Forwarded-For
                 honored), disbursement ceiling
- H-3 hygiene  — request read timeout (408), bounded concurrent connections
                 (503 + Retry-After), malformed Content-Length (400)
- M-1 residual — non-ASCII Authorization header answers 401 (the old
                 compare_digest on str raised TypeError and DROPPED the
                 connection with no HTTP response); garbage Content-Length
                 no longer 500s
- M-2 races    — poll_once is single-flight (concurrent passes cannot
                 double-alert) and the paid/late_paid flip is a CAS (a pass
                 whose snapshot went stale skips the confirm dispatch)
- M-4 headers  — CSP/X-Frame-Options/X-Content-Type-Options/Referrer-Policy
                 on HTML and JSON responses; Server header carries no version
- M-5 logging  — one access-log line per request (method, path, status,
                 duration, client ip) with the bearer token NEVER in it
- M-7 sqlite   — WAL journal mode at init_db, busy_timeout per connection
"""
import http.client
import json
import logging
import sqlite3
import threading
import time
import unittest
import unittest.mock
from concurrent.futures import ThreadPoolExecutor
try:
    import _sandbox as _env
except ImportError:  # package-style invocation (python3 -m unittest tests.test_x)
    from tests import _sandbox as _env  # type: ignore

net_gate = _env.net_gate
requires_writable = _env.requires_writable

from mazapay import api, config, store, watcher  # noqa: E402
from mazapay import notifier  # noqa: E402
from mazapay.mazad import get_chain, is_mock  # noqa: E402

TOKEN = "hardening-suite-token"


class _CountingChannel:
    """Fake notifier channel that records every dispatched alert event."""

    name = "counting"

    def __init__(self):
        self.sent = []
        self._lock = threading.Lock()

    def send(self, alert):
        with self._lock:
            self.sent.append(alert.get("event"))
        return True


@net_gate
class TestHardeningHttp(unittest.TestCase):
    """HTTP-surface hardening: one server, token-auth mode."""

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
        cls.pool.shutdown(wait=True)
        config.API_TOKEN = cls._old_token

    # -- helpers --------------------------------------------------------------
    def _http(self, method, path, body=None, token=TOKEN, extra_headers=None):
        """Raw http.client request returning (status, headers, parsed body)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        headers.update(extra_headers or {})
        data = json.dumps(body).encode() if body is not None else None
        try:
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            raw = resp.read().decode()
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = raw
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, parsed
        finally:
            conn.close()

    def _expire_awaiting(self):
        """Clear the awaiting set so cap baselines stay deterministic."""
        for o in store.list_orders(kind="in", status="awaiting"):
            store.update_order(o["id"], {"status": "expired"}, "hardening_cleanup")
        for o in store.list_orders(kind="in", status="quoted"):
            store.update_order(o["id"], {"status": "expired"}, "hardening_cleanup")

    def setUp(self):
        self._expire_awaiting()

    # -- M-1 residual: non-ASCII Authorization header --------------------------
    def test_non_ascii_authorization_header_is_401_not_dropped(self):
        # The live probe: a non-ASCII bearer used to raise TypeError inside
        # hmac.compare_digest and the connection was dropped with NO response.
        status, headers, body = self._http(
            "GET", "/vendors", token="Bearer caf\xe9-token"
        )
        self.assertEqual(status, 401, body)
        self.assertIn("error", body)
        # A WRONG-ascii token still 401s the same way.
        status, _h, _b = self._http("GET", "/vendors", token="wrong-token")
        self.assertEqual(status, 401)
        # And the correct token still works (fix did not break auth).
        status, _h, _b = self._http("GET", "/vendors", token=TOKEN)
        self.assertEqual(status, 200)

    # -- H-1 intake: ref validation -------------------------------------------
    def test_ref_formula_payload_is_400(self):
        hostile = '=HYPERLINK("http://evil.example","pwned")'
        status, _h, body = self._http(
            "POST", "/orders", {"usd_amount": 10, "ref": hostile}, token=None
        )
        self.assertEqual(status, 400, body)
        # nothing was created behind the refusal
        self.assertEqual(
            [o for o in store.list_orders(kind="in") if o.get("ref") == hostile], []
        )

    def test_ref_overlong_and_control_chars_are_400(self):
        status, _h, _b = self._http(
            "POST", "/orders", {"usd_amount": 10, "ref": "a" * 121}, token=None
        )
        self.assertEqual(status, 400)
        status, _h, _b = self._http(
            "POST", "/orders", {"usd_amount": 10, "ref": "ok\n=cmd"}, token=None
        )
        self.assertEqual(status, 400)
        status, _h, _b = self._http(
            "POST", "/orders", {"usd_amount": 10, "ref": "tab\tref"}, token=None
        )
        self.assertEqual(status, 400)

    def test_ref_valid_formats_still_201(self):
        for ref in ("INV-2026_11", "order 42:ok", "a/b@c.d", "", "x" * 120):
            status, _h, body = self._http(
                "POST", "/orders", {"usd_amount": 5, "ref": ref}, token=None
            )
            self.assertEqual(status, 201, (ref, body))
            self.assertEqual(body["ref"], ref)

    # -- H-2: caps --------------------------------------------------------------
    def test_per_order_usd_ceiling_400(self):
        base = store.count_awaiting()
        with unittest.mock.patch.object(config, "MAX_ORDER_USD", 10.0):
            status, _h, body = self._http(
                "POST", "/orders", {"usd_amount": 25}, token=None
            )
            self.assertEqual(status, 400, body)
            self.assertIn("maximum", body["error"])
            status, _h, _b = self._http(
                "POST", "/orders", {"usd_amount": 9.5}, token=None
            )
            self.assertEqual(status, 201)
        self.assertEqual(store.count_awaiting(), base + 1)

    def test_per_ip_awaiting_cap_429_with_retry_after(self):
        base_ip = store.count_awaiting(client_ip="127.0.0.1")
        with unittest.mock.patch.object(
            config, "MAX_AWAITING_ORDERS_PER_IP", base_ip + 2
        ):
            for _ in range(2):
                status, _h, _b = self._http(
                    "POST", "/orders", {"usd_amount": 5}, token=None
                )
                self.assertEqual(status, 201)
            status, headers, body = self._http(
                "POST", "/orders", {"usd_amount": 5}, token=None
            )
            self.assertEqual(status, 429, body)
            self.assertEqual(headers.get("retry-after"), str(config.ORDER_RETRY_AFTER_SECONDS))
            self.assertIn("from your address", body["error"])

    def test_per_ip_cap_is_per_forwarded_for_not_per_socket(self):
        # All connections come from 127.0.0.1; the proxy's X-Forwarded-For is
        # the cap key, so distinct clients stay independent.
        with unittest.mock.patch.object(config, "MAX_AWAITING_ORDERS_PER_IP", 1):
            status, _h, body = self._http(
                "POST", "/orders", {"usd_amount": 5}, token=None,
                extra_headers={"X-Forwarded-For": "9.9.9.9"},
            )
            self.assertEqual(status, 201, body)
            status, _h, body = self._http(
                "POST", "/orders", {"usd_amount": 5}, token=None,
                extra_headers={"X-Forwarded-For": "9.9.9.9"},
            )
            self.assertEqual(status, 429)
            status, _h, body = self._http(
                "POST", "/orders", {"usd_amount": 5}, token=None,
                extra_headers={"X-Forwarded-For": "8.8.8.8"},
            )
            self.assertEqual(status, 201, body)

    def test_global_awaiting_cap_429_with_retry_after(self):
        base = store.count_awaiting()
        with unittest.mock.patch.object(config, "MAX_AWAITING_ORDERS_GLOBAL", base + 1):
            status, _h, _b = self._http(
                "POST", "/orders", {"usd_amount": 5}, token=None
            )
            self.assertEqual(status, 201)
            status, headers, body = self._http(
                "POST", "/orders", {"usd_amount": 5}, token=None
            )
            self.assertEqual(status, 429, body)
            self.assertIn("retry-after", headers)
            self.assertIn("too many open orders", body["error"])

    def test_caps_do_not_apply_to_tokened_disburse(self):
        chain, _mode = get_chain()
        self.assertTrue(is_mock(chain))
        address = chain.getnewaddress("hardening")
        base = store.count_awaiting()
        with unittest.mock.patch.object(config, "MAX_AWAITING_ORDERS_GLOBAL", 0):
            # global cap 0 would refuse every public order...
            status, _h, _b = self._http("POST", "/orders", {"usd_amount": 5}, token=None)
            self.assertEqual(status, 429)
            # ...but the tokened spending route is a policy surface of its own
            status, _h, body = self._http(
                "POST", "/payments", {"address": address, "usd_amount": 1}
            )
            self.assertEqual(status, 201, body)
        self.assertEqual(store.count_awaiting(), base)

    # -- M-6: disbursement ceiling ---------------------------------------------
    def test_disburse_ceiling_400_before_intent_row(self):
        chain, _mode = get_chain()
        address = chain.getnewaddress("hardening")
        before = len(store.list_orders(kind="out"))
        with unittest.mock.patch.object(config, "MAX_DISBURSE_USD", 5.0):
            status, _h, body = self._http(
                "POST", "/payments", {"address": address, "usd_amount": 6}
            )
            self.assertEqual(status, 400, body)
            self.assertIn("ceiling", body["error"])
            # refused BEFORE the intent-first row is written
            self.assertEqual(len(store.list_orders(kind="out")), before)
            status, _h, body = self._http(
                "POST", "/payments", {"address": address, "usd_amount": 4}
            )
            self.assertEqual(status, 201, body)
            self.assertEqual(body["status"], "sent")
        self.assertEqual(len(store.list_orders(kind="out")), before + 1)

    # -- H-3 / M-1: body-read hygiene -------------------------------------------
    def test_malformed_content_length_is_400_not_500(self):
        status, _h, body = self._http(
            "POST", "/orders", token=None, extra_headers={"Content-Length": "garbage"}
        )
        self.assertEqual(status, 400, body)
        self.assertIn("Content-Length", body["error"])

    def test_request_body_read_timeout_is_408(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        old_timeout = api.Handler.timeout
        api.Handler.timeout = 1  # 1s so the test is fast; restored below
        try:
            # declare 200 bytes, send 1, then dribble nothing: the server's
            # read trips its socket timeout and answers 408
            conn.request(
                "POST",
                "/orders",
                body=b"{",
                headers={"Content-Type": "application/json", "Content-Length": "200"},
            )
            resp = conn.getresponse()
            body = json.loads(resp.read().decode())
            self.assertEqual(resp.status, 408, body)
            self.assertIn("timed out", body["error"])
        finally:
            api.Handler.timeout = old_timeout
            conn.close()

    def test_bounded_concurrent_connections_503(self):
        slots = self.srv.connection_slots
        acquired = 0
        while slots.acquire(blocking=False):
            acquired += 1
        self.assertGreater(acquired, 0)
        try:
            status, headers, body = self._http("GET", "/health", token=None)
            self.assertEqual(status, 503, body)
            self.assertIn("retry-after", headers)
            self.assertIn("busy", body["error"])
        finally:
            for _ in range(acquired):
                slots.release()
        status, _h, _b = self._http("GET", "/health", token=None)
        self.assertEqual(status, 200)

    # -- M-4: security headers + no version banner ------------------------------
    def test_security_headers_on_html_and_json(self):
        expected = {
            "x-content-type-options": "nosniff",
            "x-frame-options": "DENY",
            "referrer-policy": "no-referrer",
        }
        for path in ("/", "/health", "/no-such-route"):
            status, headers, body = self._http("GET", path, token=None)
            self.assertEqual(status, (200 if path != "/no-such-route" else 404))
            for key, value in expected.items():
                self.assertEqual(headers.get(key), value, (path, key))
            csp = headers.get("content-security-policy", "")
            for directive in (
                "default-src 'none'",
                "frame-ancestors 'none'",
                "base-uri 'none'",
                "form-action 'none'",
            ):
                self.assertIn(directive, csp, (path, directive))

    def test_server_header_has_no_version_banner(self):
        status, headers, _b = self._http("GET", "/", token=None)
        self.assertEqual(status, 200)
        server = headers.get("server", "")
        self.assertEqual(server, "MazaPay", server)
        self.assertNotIn("Python", server)
        self.assertNotIn("/", server)

    # -- M-5: access logging -----------------------------------------------------
    def _wait_for_log(self, cm, substring, timeout=2.0):
        """Wait for the server thread to emit a record (the line is written
        just AFTER the response bytes reach the client, so a request that
        has already returned does not guarantee the record has landed)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if any(substring in line for line in cm.output):
                return True
            time.sleep(0.02)
        return False

    def test_access_log_line_has_request_shape_and_never_the_token(self):
        with self.assertLogs("mazapay.access", level="INFO") as cm:
            self._http("GET", "/health", token=TOKEN)
            self._http("GET", "/vendors", token="bad-token")
            self.assertTrue(self._wait_for_log(cm, '"GET /vendors" 401'))
        text = "\n".join(cm.output)
        self.assertIn('"GET /health" 200', text)
        self.assertIn('"GET /vendors" 401', text)
        self.assertTrue(any(r.levelno == logging.WARNING for r in cm.records))
        self.assertNotIn(TOKEN, text)  # the token never appears
        self.assertIn("127.0.0.1", text)

    def test_access_log_xff_first_hop_recorded(self):
        with self.assertLogs("mazapay.access", level="INFO") as cm:
            self._http(
                "POST", "/orders", {"usd_amount": 5}, token=None,
                extra_headers={"X-Forwarded-For": "203.0.113.7, 10.0.0.1"},
            )
            self.assertTrue(self._wait_for_log(cm, "203.0.113.7"))
        self.assertTrue(any("203.0.113.7" in line for line in cm.output))


@requires_writable
class TestWatcherSingleFlightAndCas(unittest.TestCase):
    """M-2: concurrent polls never double-alert; the flip is a CAS.

    Guard split, asserted separately: stage 1 (deposit alert) is guarded by
    the single-flight poll lock — every real pass enters through poll_once —
    and stage 2 (the paid/late_paid flip + confirm alert) is additionally a
    CAS, so even a pass holding a stale pre-flip snapshot cannot re-flip or
    re-dispatch the confirm.
    """

    def setUp(self):
        store.init_db()
        self.channel = _CountingChannel()
        self._notifiers_patcher = unittest.mock.patch.object(
            notifier, "build_notifiers", return_value=[self.channel]
        )
        self._notifiers_patcher.start()
        self.addCleanup(self._notifiers_patcher.stop)

    def tearDown(self):
        for o in store.list_orders(kind="in"):
            if o["status"] in ("quoted", "awaiting"):
                store.update_order(o["id"], {"status": "expired"}, "hardening_cleanup")

    def _funded_awaiting_order(self):
        order = api.create_order(10, "race-test")
        chain, _mode = get_chain()
        chain.fund(order["address"], order["maza_amount"], config.CONFIRMATIONS_REQUIRED)
        return order

    def test_poll_once_is_single_flight(self):
        """A pass starting while another holds the poll lock skips cleanly."""
        order = self._funded_awaiting_order()
        lock = watcher._POLL_LOCK
        with lock:  # simulate an in-flight pass holding the lock
            counts = watcher.poll_once()
        self.assertEqual(counts["already_running"], 1)
        self.assertEqual(counts["paid"], 0)
        self.assertEqual(self.channel.sent, [])  # the skipper touched nothing
        # once the in-flight pass releases, polling works normally
        counts = watcher.poll_once()
        self.assertEqual(counts["already_running"], 0)
        self.assertEqual(counts["paid"], 1)
        self.assertEqual(self.channel.sent.count("deposit_confirmed"), 1)
        self.assertEqual(store.get_order(order["id"])["status"], "paid")

    def test_concurrent_polls_cannot_double_alert_or_double_flip(self):
        self._funded_awaiting_order()
        with ThreadPoolExecutor(max_workers=4) as pool:
            counts = list(pool.map(lambda _: watcher.poll_once(), range(4)))
        # the flip happened exactly once across every pass...
        self.assertEqual(sum(c["paid"] for c in counts), 1, counts)
        # ...and each alert dispatched exactly once
        self.assertEqual(self.channel.sent.count("deposit_detected"), 1)
        self.assertEqual(self.channel.sent.count("deposit_confirmed"), 1)

    def test_cas_flip_loses_race_skips_confirm_dispatch(self):
        """A pass holding the SAME stale pre-flip snapshot cannot re-flip."""
        order = self._funded_awaiting_order()
        chain, _mode = get_chain()
        counts_a = watcher._counts_template()
        watcher._watch_inbound(chain, order, counts_a, late=False)
        self.assertEqual(counts_a["paid"], 1)
        # Second pass with the PRE-FLIP snapshot (status 'awaiting',
        # deposit_alerted_ts None) — exactly what a racy concurrent pass
        # would have read. Stage 1 re-dispatches (in production the
        # single-flight lock makes this impossible); stage 2 must lose the
        # CAS and dispatch nothing.
        counts_b = watcher._counts_template()
        watcher._watch_inbound(chain, order, counts_b, late=False)
        self.assertEqual(counts_b["paid"], 0)
        self.assertEqual(counts_b["confirm_alerts"], 0)
        self.assertEqual(counts_b["already_running"], 0)
        self.assertEqual(self.channel.sent.count("deposit_confirmed"), 1)
        self.assertEqual(self.channel.sent.count("deposit_detected"), 2)
        self.assertEqual(store.get_order(order["id"])["status"], "paid")

    def test_late_paid_flip_is_cas_too(self):
        order = api.create_order(10, "race-late")
        store.update_order(order["id"], {"expires_ts": time.time() - 1}, "test_expire")
        chain, _mode = get_chain()
        chain.fund(order["address"], order["maza_amount"], config.CONFIRMATIONS_REQUIRED)
        counts = watcher._counts_template()
        watcher._watch_inbound(chain, store.get_order(order["id"]), counts, late=True)
        self.assertEqual(counts["late_paid"], 1)
        self.assertEqual(store.get_order(order["id"])["status"], "late_paid")
        # replay with a snapshot claiming pre-flip state ('expired'): the
        # expired -> late_paid CAS now refuses; no second dispatch
        stale = dict(store.get_order(order["id"]))
        stale["status"] = "expired"
        counts2 = watcher._counts_template()
        watcher._watch_inbound(chain, stale, counts2, late=True)
        self.assertEqual(counts2["late_paid"], 0)
        self.assertEqual(counts2["confirm_alerts"], 0)
        self.assertEqual(store.get_order(order["id"])["status"], "late_paid")


@requires_writable
class TestStoreHardening(unittest.TestCase):
    """M-7: WAL + busy_timeout; count_awaiting drives the caps."""

    def setUp(self):
        store.init_db()

    def test_wal_journal_mode_enabled(self):
        conn = sqlite3.connect(config.DB_PATH)
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(mode.lower(), "wal")

    def test_busy_timeout_per_connection(self):
        conn = store.connect()
        try:
            value = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(value, 15000)

    def test_count_awaiting_global_and_per_ip(self):
        base_global = store.count_awaiting()
        base_ip = store.count_awaiting(client_ip="10.10.10.10")
        order = store.create_order(
            kind="in", usd_amount=5, maza_amount=2500, rate=0.002,
            rate_source="fixed", client_ip="10.10.10.10",
        )
        self.assertEqual(store.count_awaiting(), base_global + 1)
        self.assertEqual(store.count_awaiting(client_ip="10.10.10.10"), base_ip + 1)
        self.assertEqual(store.count_awaiting(client_ip="11.11.11.11"), 0)
        # paid orders leave the awaiting set
        store.transition(order["id"], "quoted", "paid", event="test_paid")
        self.assertEqual(store.count_awaiting(client_ip="10.10.10.10"), base_ip)
        self.assertEqual(store.count_awaiting(), base_global)

    def test_client_ip_survives_store_roundtrip(self):
        order = api.create_order(7, "ip-check", client_ip="192.0.2.9")
        stored = store.get_order(order["id"])
        self.assertEqual(stored["client_ip"], "192.0.2.9")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
