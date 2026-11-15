"""Pluggable alert notifiers for the payment watcher.

The watcher emits two alert kinds over an inbound order's watch address:

- deposit_detected  — funds covering the quote are visible at
  ALERT_DEPOSIT_MINCONF confirmations (pre-threshold early signal)
- deposit_confirmed — the order flipped to paid/late_paid at
  CONFIRMATIONS_REQUIRED

Channels are stdlib-only and env-configured (see config.py): a log
notifier (on by default, logger ``mazapay.alerts``) and a JSON webhook
(active when MAZAPAY_ALERT_WEBHOOK_URL is set; optional bearer token via
MAZAPAY_ALERT_WEBHOOK_TOKEN).

Delivery semantics differ per alert kind, on purpose — the store's state
machine dictates each:

- A deposit alert is DELIVERED FIRST and only then marked on the order
  row (``deposit_alerted_ts``). While undelivered it is retried on every
  poll, so a webhook outage degrades to delayed delivery; a written
  marker means the alert went out, so restarts never duplicate it.
- The confirm alert is MARKED atomically with the paid/late_paid flip
  (``confirm_alerted_ts`` rides in the same UPDATE + audit entry) and
  then dispatched. The transition happens exactly once per order no
  matter how often the watcher restarts, so this alert cannot repeat;
  if the process dies between the flip and the dispatch, the receipt
  evidence is still in the append-only audit trail (advisory alert,
  durable record).

Notifiers never raise into the watcher loop: every send returns a bool,
and all configured channels must accept an alert for it to count as
delivered.
"""
import json
import logging
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from . import config

LOGGER = logging.getLogger("mazapay.alerts")


class BaseNotifier:
    """One delivery channel: ``send(alert) -> bool``, never raises."""

    name = "base"

    def send(self, alert: Dict[str, Any]) -> bool:  # pragma: no cover - interface
        raise NotImplementedError


class LogNotifier(BaseNotifier):
    """One structured line per alert on the mazapay.alerts logger."""

    name = "log"

    def send(self, alert: Dict[str, Any]) -> bool:
        LOGGER.info(
            "%s order=%s status=%s address=%s maza=%s received=%s txid=%s",
            alert.get("event"),
            alert.get("order_id"),
            alert.get("status"),
            alert.get("address"),
            alert.get("maza_amount"),
            alert.get("received"),
            alert.get("txid"),
        )
        return True


class WebhookNotifier(BaseNotifier):
    """POST the alert as JSON to a configured URL; bool result, never raises.

    Any transport failure or non-2xx status returns False — for deposit
    alerts that means "undelivered, retry on the next poll".
    """

    name = "webhook"

    def __init__(self, url: str, timeout: Optional[int] = None, token: str = ""):
        self.url = url
        self.timeout = timeout or config.ALERT_WEBHOOK_TIMEOUT
        self.token = token

    def send(self, alert: Dict[str, Any]) -> bool:
        headers = {"Content-Type": "application/json", "User-Agent": "mazapay-watcher"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(
            self.url, data=json.dumps(alert).encode("utf-8"), headers=headers
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return 200 <= resp.status < 300
        except urllib.error.HTTPError as exc:
            exc.close()  # drain the error body so no ResourceWarning leaks
            return False
        except Exception:
            return False


def build_notifiers() -> List[BaseNotifier]:
    """Channels active under the current config, in dispatch order.

    Read on every dispatch (no cache) so env-driven config changes and
    test monkeypatching take effect immediately. Master switch off or
    zero channels enabled => empty list => nothing counts as delivered.
    """
    if not config.ALERTS_ENABLED:
        return []
    channels: List[BaseNotifier] = []
    if config.ALERT_LOG_ENABLED:
        channels.append(LogNotifier())
    if config.ALERT_WEBHOOK_URL:
        channels.append(
            WebhookNotifier(
                config.ALERT_WEBHOOK_URL,
                config.ALERT_WEBHOOK_TIMEOUT,
                config.ALERT_WEBHOOK_TOKEN,
            )
        )
    return channels
