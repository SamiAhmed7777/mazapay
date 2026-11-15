"""Deposit/confirmation alerting tests: notifier channels, watcher
integration on MockChain, and restart idempotency via the persisted
alert markers (deposit_alerted_ts / confirm_alerted_ts).

Delivery contract under test:
- deposit_detected: delivered first, marked only after delivery;
  undelivered alerts retry on every poll (at-least-once).
- deposit_confirmed: marked atomically with the paid/late_paid flip and
  dispatched once — exactly-once across watcher restarts.
"""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List
from unittest import mock
from contextlib import contextmanager

try:
    import _sandbox as _env
except ImportError:  # package-style invocation (python3 -m unittest tests.test_x)
    from tests import _sandbox as _env  # type: ignore

requires_writable = _env.requires_writable
net_gate = _env.net_gate

from mazapay import config, notifier, store, watcher  # noqa: E402
from mazapay.mazad import get_chain  # noqa: E402

_ALERT_KEYS = (
    "ALERTS_ENABLED",
    "ALERT_LOG_ENABLED",
    "ALERT_WEBHOOK_URL",
    "ALERT_WEBHOOK_TOKEN",
    "ALERT_WEBHOOK_TIMEOUT",
    "ALERT_DEPOSIT_MINCONF",
)


@contextmanager
def alert_config(**overrides):
    """Temporarily set config alert knobs (tests restore the originals)."""
    saved = {k: getattr(config, k) for k in _ALERT_KEYS}
    try:
        for k, v in overrides.items():
            setattr(config, k, v)
        yield
    finally:
        for k, v in saved.items():
            setattr(config, k, v)


def audit_count(entity_id: str, event: str) -> int:
    with store.db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM audit WHERE entity='order' AND entity_id=? AND event=?",
            (entity_id, event),
        ).fetchone()
    return int(row[0])


def awaiting_order(chain, usd: float = 4.0, maza: float = 2000.0, tag: str = "al") -> Dict[str, Any]:
    o = store.create_order("in", usd, maza, 0.002, "fixed", address=chain.getnewaddress(tag))
    store.update_order(o["id"], {"status": "awaiting"}, "awaiting_funds")
    return o


class _HookHandler(BaseHTTPRequestHandler):
    """Minimal webhook target: records POSTs, replies with a settable status."""

    status = 200
    captured: List[Dict[str, Any]] = []

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length)
        try:
            parsed = json.loads(body.decode("utf-8"))
        except Exception:
            parsed = None
        type(self).captured.append(
            {
                "path": self.path,
                "body": parsed,
                "authorization": self.headers.get("Authorization"),
                "content_type": self.headers.get("Content-Type"),
            }
        )
        self.send_response(type(self).status)
        self.end_headers()

    def log_message(self, fmt: str, *args) -> None:  # silence the test output
        pass


@requires_writable
class TestNotifierChannels(unittest.TestCase):
    def test_log_notifier_writes_structured_line(self):
        alert = {
            "event": "deposit_detected",
            "order_id": "ord_test1",
            "status": "awaiting",
            "address": "tMAZA-A",
            "maza_amount": 5.0,
            "received": 5.0,
            "txid": "mock000001",
        }
        with self.assertLogs("mazapay.alerts", level="INFO") as cm:
            ok = notifier.LogNotifier().send(alert)
        self.assertTrue(ok)
        self.assertTrue(any("deposit_detected" in m and "ord_test1" in m for m in cm.output))

    def test_log_notifier_never_fails(self):
        self.assertTrue(notifier.LogNotifier().send({}))

    def test_build_notifiers_matrix(self):
        cases = [
            # (master, log, webhook_url) -> active channel names
            (True, True, "", ["log"]),
            (True, True, "http://127.0.0.1:9/hook", ["log", "webhook"]),
            (True, False, "http://127.0.0.1:9/hook", ["webhook"]),
            (True, False, "", []),
            (False, True, "http://127.0.0.1:9/hook", []),
        ]
        for master, log, url, expected in cases:
            with alert_config(ALERTS_ENABLED=master, ALERT_LOG_ENABLED=log, ALERT_WEBHOOK_URL=url):
                names = [c.name for c in notifier.build_notifiers()]
            self.assertEqual(names, expected, f"master={master} log={log} url={url!r}")

    def test_webhook_carries_token_and_timeout(self):
        with alert_config(
            ALERT_WEBHOOK_URL="http://127.0.0.1:9/hook",
            ALERT_WEBHOOK_TIMEOUT=3,
            ALERT_WEBHOOK_TOKEN="tok",
        ):
            channels = notifier.build_notifiers()
        hook = [c for c in channels if c.name == "webhook"][0]
        self.assertEqual(hook.timeout, 3)
        self.assertEqual(hook.token, "tok")

    def test_webhook_refused_connection_returns_false(self):
        # Port 9 on localhost: connection refused in permissive environments;
        # in sandboxes that deny sockets entirely the send still returns
        # False (the exception is swallowed) — False either way, no raise.
        ok = notifier.WebhookNotifier("http://127.0.0.1:9/hook", timeout=1).send(
            {"event": "deposit_detected"}
        )
        self.assertFalse(ok)


@net_gate
class TestWebhookDelivery(unittest.TestCase):
    def setUp(self):
        store.init_db()
        _HookHandler.captured = []
        _HookHandler.status = 200
        self.srv = HTTPServer(("127.0.0.1", 0), _HookHandler)
        self.port = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        self._ctx = alert_config(
            ALERTS_ENABLED=True,
            ALERT_LOG_ENABLED=False,  # webhook-only: counts isolate this channel
            ALERT_WEBHOOK_URL=f"http://127.0.0.1:{self.port}/hook",
            ALERT_WEBHOOK_TOKEN="test-webhook-token",
            ALERT_WEBHOOK_TIMEOUT=3,
        )
        self._ctx.__enter__()

    def tearDown(self):
        self._ctx.__exit__(None, None, None)
        self.srv.shutdown()
        self.srv.server_close()
        self.thread.join(timeout=5)

    def test_delivered_alerts_post_json_with_bearer_token(self):
        chain, _mode = get_chain()
        o = awaiting_order(chain, tag="hook")
        chain.fund(o["address"], 2000.0, confirmations=config.CONFIRMATIONS_REQUIRED)
        counts = watcher.poll_once()
        self.assertEqual(counts["deposit_alerts"], 1)
        self.assertEqual(counts["confirm_alerts"], 1)
        self.assertEqual(len(_HookHandler.captured), 2)
        events = [c["body"]["event"] for c in _HookHandler.captured]
        self.assertEqual(events, ["deposit_detected", "deposit_confirmed"])
        for cap in _HookHandler.captured:
            self.assertEqual(cap["path"], "/hook")
            self.assertEqual(cap["authorization"], "Bearer test-webhook-token")
            self.assertEqual(cap["content_type"], "application/json")
            self.assertEqual(cap["body"]["order_id"], o["id"])
        first = _HookHandler.captured[0]["body"]
        self.assertEqual(first["status"], "awaiting")
        self.assertEqual(first["maza_amount"], 2000.0)
        self.assertEqual(first["confirmations_required"], config.CONFIRMATIONS_REQUIRED)
        second = _HookHandler.captured[1]["body"]
        self.assertEqual(second["status"], "paid")
        self.assertIsNotNone(second["txid"])
        row = store.get_order(o["id"])
        self.assertIsNotNone(row["deposit_alerted_ts"])
        self.assertIsNotNone(row["confirm_alerted_ts"])

    def test_500_is_undelivered_then_recovers_without_duplicates(self):
        chain, _mode = get_chain()
        o = awaiting_order(chain, tag="hook5")
        chain.fund(o["address"], 2000.0, confirmations=config.ALERT_DEPOSIT_MINCONF)
        _HookHandler.status = 500
        counts = watcher.poll_once()
        self.assertEqual(counts["deposit_alerts"], 0)  # delivery refused by 500
        self.assertIsNone(store.get_order(o["id"])["deposit_alerted_ts"])
        first_attempts = len(_HookHandler.captured)
        self.assertGreaterEqual(first_attempts, 1)
        _HookHandler.status = 200
        counts = watcher.poll_once()  # retry succeeds exactly once
        self.assertEqual(counts["deposit_alerts"], 1)
        self.assertIsNotNone(store.get_order(o["id"])["deposit_alerted_ts"])
        after_recovery = len(_HookHandler.captured)
        self.assertEqual(after_recovery, first_attempts + 1)
        counts = watcher.poll_once()  # settled: no further posts, no duplicates
        self.assertEqual(counts["deposit_alerts"], 0)
        self.assertEqual(len(_HookHandler.captured), after_recovery)


@requires_writable
class TestWatcherAlerts(unittest.TestCase):
    def setUp(self):
        store.init_db()  # order-independent schema (see TestStore.setUp)
        # Quarantine orders left by earlier test methods/classes so every
        # poll evaluates exactly the rows this test creates: expired status
        # leaves pending_inbound, blank address leaves expired_inbound.
        with store.db() as conn:
            rows = conn.execute("SELECT id FROM orders WHERE kind='in'").fetchall()
        for r in rows:
            store.update_order(r["id"], {"status": "expired", "address": ""}, "test_quarantine")
        self._ctx = alert_config(
            ALERTS_ENABLED=True,
            ALERT_LOG_ENABLED=True,
            ALERT_WEBHOOK_URL="",
            ALERT_WEBHOOK_TOKEN="",
            ALERT_WEBHOOK_TIMEOUT=2,
            ALERT_DEPOSIT_MINCONF=1,
        )
        self._ctx.__enter__()

    def tearDown(self):
        self._ctx.__exit__(None, None, None)

    def test_both_alerts_fire_on_a_fully_confirmed_deposit(self):
        chain, _mode = get_chain()
        o = awaiting_order(chain, tag="both")
        txid = chain.fund(o["address"], 2000.0, confirmations=config.CONFIRMATIONS_REQUIRED)
        with self.assertLogs("mazapay.alerts", level="INFO") as cm:
            counts = watcher.poll_once()
        self.assertEqual(counts["paid"], 1)
        self.assertEqual(counts["deposit_alerts"], 1)
        self.assertEqual(counts["confirm_alerts"], 1)
        output = "\n".join(cm.output)
        self.assertIn("deposit_detected", output)
        self.assertIn("deposit_confirmed", output)
        row = store.get_order(o["id"])
        self.assertEqual(row["status"], "paid")
        self.assertEqual(row["txid"], txid)
        self.assertIsNotNone(row["deposit_alerted_ts"])
        self.assertIsNotNone(row["confirm_alerted_ts"])
        self.assertEqual(audit_count(o["id"], "deposit_alerted"), 1)
        self.assertEqual(audit_count(o["id"], "receipt_confirmed"), 1)

    def test_confirm_alert_exactly_once_across_restarts(self):
        chain, _mode = get_chain()
        o = awaiting_order(chain, tag="restart")
        chain.fund(o["address"], 2000.0, confirmations=config.CONFIRMATIONS_REQUIRED)
        counts = watcher.poll_once()
        self.assertEqual(counts["confirm_alerts"], 1)
        store.init_db()  # a restart re-opens the store; markers are persisted
        counts = watcher.poll_once()
        self.assertEqual(counts["paid"], 0)
        self.assertEqual(counts["deposit_alerts"], 0)
        self.assertEqual(counts["confirm_alerts"], 0)
        self.assertEqual(audit_count(o["id"], "deposit_alerted"), 1)
        self.assertEqual(audit_count(o["id"], "receipt_confirmed"), 1)

    def test_deposit_alert_fires_pre_confirmation_and_is_not_repeated(self):
        chain, _mode = get_chain()
        o = awaiting_order(chain, tag="preconf")
        chain.fund(o["address"], 2000.0, confirmations=config.ALERT_DEPOSIT_MINCONF)
        counts = watcher.poll_once()
        self.assertEqual(counts["deposit_alerts"], 1)
        self.assertEqual(counts["confirm_alerts"], 0)
        self.assertEqual(counts["paid"], 0)
        self.assertEqual(counts["still_pending"], 1)
        row = store.get_order(o["id"])
        self.assertEqual(row["status"], "awaiting")
        marked_ts = row["deposit_alerted_ts"]
        self.assertIsNotNone(marked_ts)
        store.init_db()  # restart: the marker must suppress the repeat
        counts = watcher.poll_once()
        self.assertEqual(counts["deposit_alerts"], 0)
        self.assertEqual(store.get_order(o["id"])["deposit_alerted_ts"], marked_ts)
        self.assertEqual(audit_count(o["id"], "deposit_alerted"), 1)

    def test_undelivered_deposit_alert_retries_until_a_channel_accepts(self):
        chain, _mode = get_chain()
        o = awaiting_order(chain, tag="retry")
        chain.fund(o["address"], 2000.0, confirmations=config.ALERT_DEPOSIT_MINCONF)
        # Every configured channel is down: webhook unreachable, log off.
        with alert_config(ALERT_LOG_ENABLED=False, ALERT_WEBHOOK_URL="http://127.0.0.1:9/hook"):
            counts = watcher.poll_once()
            self.assertEqual(counts["deposit_alerts"], 0)
            self.assertIsNone(store.get_order(o["id"])["deposit_alerted_ts"])
            counts = watcher.poll_once()  # still retrying, still unmarked
            self.assertEqual(counts["deposit_alerts"], 0)
        self.assertEqual(audit_count(o["id"], "deposit_alerted"), 0)
        # A channel comes back online: the pending deposit alert delivers.
        counts = watcher.poll_once()
        self.assertEqual(counts["deposit_alerts"], 1)
        self.assertIsNotNone(store.get_order(o["id"])["deposit_alerted_ts"])
        self.assertEqual(audit_count(o["id"], "deposit_alerted"), 1)

    def test_disabled_alerts_leave_watcher_behaviour_unchanged(self):
        chain, _mode = get_chain()
        o = awaiting_order(chain, tag="off")
        chain.fund(o["address"], 2000.0, confirmations=config.CONFIRMATIONS_REQUIRED)
        with alert_config(ALERTS_ENABLED=False):
            counts = watcher.poll_once()
        self.assertEqual(counts["paid"], 1)
        self.assertEqual(counts["deposit_alerts"], 0)
        self.assertEqual(counts["confirm_alerts"], 0)
        row = store.get_order(o["id"])
        self.assertEqual(row["status"], "paid")
        self.assertIsNone(row["deposit_alerted_ts"])
        self.assertIsNone(row["confirm_alerted_ts"])
        self.assertEqual(audit_count(o["id"], "receipt_confirmed"), 1)
        self.assertEqual(audit_count(o["id"], "deposit_alerted"), 0)

    def test_late_paid_order_gets_both_alerts(self):
        chain, _mode = get_chain()
        o = awaiting_order(chain, tag="late")
        store.mark_expired_stale(now=(o["expires_ts"] or 0) + 1)  # quote expires...
        self.assertEqual(store.get_order(o["id"])["status"], "expired")
        # ...then the member pays anyway, already fully confirmed
        chain.fund(o["address"], 2000.0, confirmations=config.CONFIRMATIONS_REQUIRED)
        counts = watcher.poll_once()
        self.assertEqual(counts["late_paid"], 1)
        self.assertEqual(counts["deposit_alerts"], 1)
        self.assertEqual(counts["confirm_alerts"], 1)
        row = store.get_order(o["id"])
        self.assertEqual(row["status"], "late_paid")
        self.assertIsNotNone(row["txid"])
        self.assertIsNotNone(row["deposit_alerted_ts"])
        self.assertIsNotNone(row["confirm_alerted_ts"])
        self.assertEqual(audit_count(o["id"], "deposit_alerted"), 1)
        self.assertEqual(audit_count(o["id"], "late_receipt_detected"), 1)

    def test_partial_deposit_below_quote_does_not_alert(self):
        chain, _mode = get_chain()
        o = awaiting_order(chain, tag="partial")
        chain.fund(o["address"], 1500.0, confirmations=config.ALERT_DEPOSIT_MINCONF)
        counts = watcher.poll_once()
        self.assertEqual(counts["deposit_alerts"], 0)
        self.assertEqual(counts["still_pending"], 1)
        self.assertIsNone(store.get_order(o["id"])["deposit_alerted_ts"])

    def test_alert_payload_shape_is_json_safe(self):
        chain, _mode = get_chain()
        o = awaiting_order(chain, tag="shape")
        txid = chain.fund(o["address"], 2000.0, confirmations=config.CONFIRMATIONS_REQUIRED)
        captured: List[Dict[str, Any]] = []

        class Capture:
            name = "capture"

            def send(self, alert: Dict[str, Any]) -> bool:
                captured.append(dict(alert))
                return True

        with mock.patch.object(notifier, "build_notifiers", return_value=[Capture()]):
            counts = watcher.poll_once()
        self.assertEqual(counts["deposit_alerts"], 1)
        self.assertEqual(counts["confirm_alerts"], 1)
        self.assertEqual([a["event"] for a in captured], ["deposit_detected", "deposit_confirmed"])
        for alert in captured:
            json.dumps(alert)  # must be JSON-serializable for webhook bodies
            self.assertEqual(alert["service"], "mazapay")
            self.assertEqual(alert["kind"], "in")
            self.assertEqual(alert["order_id"], o["id"])
            self.assertEqual(alert["address"], o["address"])
            self.assertEqual(alert["maza_amount"], 2000.0)
            self.assertEqual(alert["usd_amount"], 4.0)
            self.assertEqual(alert["confirmations_required"], config.CONFIRMATIONS_REQUIRED)
            self.assertIsInstance(alert["ts"], float)
            self.assertEqual(alert["txid"], txid)
        self.assertEqual(captured[0]["status"], "awaiting")
        self.assertGreaterEqual(captured[0]["received"], 2000.0)
        self.assertEqual(captured[1]["status"], "paid")
        self.assertGreaterEqual(captured[1]["received"], 2000.0)

    def test_counts_keys_stay_backward_compatible(self):
        chain, _mode = get_chain()
        o = awaiting_order(chain, tag="counts")
        chain.fund(o["address"], 2000.0, confirmations=config.CONFIRMATIONS_REQUIRED)
        counts = watcher.poll_once()
        for key in ("paid", "late_paid", "expired", "still_pending",
                    "deposit_alerts", "confirm_alerts"):
            self.assertIn(key, counts)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
