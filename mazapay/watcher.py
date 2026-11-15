"""Confirmation watcher: turns pending inbound orders into paid receipts,
catches late payments on expired orders, and locates outbound broadcasts.

Polls the chain adapter for every pending order's address; when the
received amount (at CONFIRMATIONS_REQUIRED) covers the quoted MAZA amount,
the order flips to `paid` with paid_ts + txid, and an audit entry records
the receipt evidence (amount, confirmations, txid).

Each poll also drives the alert pipeline (see notifier.py): a
`deposit_detected` alert fires once the funds first cover the quote at
ALERT_DEPOSIT_MINCONF (delivered before it is marked, retried across
polls until a channel accepts it), and a `deposit_confirmed` alert fires
at the paid/late_paid flip (marked atomically with the flip, dispatched
once — restarts can never repeat either alert).

Late payments are a reconciliation state, not a dead end: an order whose
quote expired but whose address then receives the funds moves to
`late_paid` (the money is in the wallet the bridge controls) and waits for
an operator to accept it at the original quote or refund it.

The txid is best-effort at this layer: getreceivedbyaddress proves the
amount, and listtransactions is scanned for the matching receive.
"""
import threading
import time
from typing import Any, Dict, Optional

from . import config, notifier, store
from .mazad import ChainError, get_chain


def find_txid(chain: Any, address: str, min_amount: float) -> Optional[str]:
    """Scan recent transactions for a receive to `address` >= min_amount.

    Receive-side helper: the amount is already proven by
    getreceivedbyaddress before this is called, so a failed scan just means
    a missing txid annotation, not an unknown state.
    """
    try:
        txs = chain.listtransactions(config.TX_SCAN_LIMIT)
    except Exception:
        return None
    for tx in reversed(txs):
        if tx.get("address") == address and tx.get("category") == "receive":
            if abs(float(tx.get("amount", 0))) >= min_amount:
                return tx.get("txid")
    return None


def find_send_txid(chain: Any, address: str, amount: float, comment: str) -> str:
    """Scan recent transactions for a send matching address + amount + comment.

    Returns the txid, or the empty string when the scan COMPLETED and found
    no match. A failed scan raises ChainError — an RPC lookup failure is
    never evidence that no broadcast happened, so callers keep the payment
    in send_uncertain instead of downgrading to send_failed.

    The comment is the order's nonce (mazapay:<nonce>), which is how a
    broadcast whose RPC response was lost gets reconciled to its intent row.
    Scan depth is config.TX_SCAN_LIMIT; the residual window is disclosed in
    the README.
    """
    try:
        txs = chain.listtransactions(config.TX_SCAN_LIMIT)
    except Exception as exc:
        raise ChainError(f"listtransactions scan failed: {exc}") from exc
    for tx in reversed(txs):
        if (
            tx.get("category") == "send"
            and tx.get("address") == address
            and tx.get("comment") == comment
            and abs(float(tx.get("amount", 0))) + 1e-9 >= abs(float(amount))
        ):
            return str(tx.get("txid"))
    return ""


def _alert_payload(
    order: Dict[str, Any], event: str, received: float, txid: Optional[str], status: str
) -> Dict[str, Any]:
    """JSON-safe alert body shared by every channel (no secrets in it)."""
    return {
        "service": "mazapay",
        "event": event,
        "kind": order.get("kind"),
        "order_id": order.get("id"),
        "status": status,
        "address": order.get("address"),
        "usd_amount": order.get("usd_amount"),
        "maza_amount": order.get("maza_amount"),
        "received": received,
        "confirmations_required": config.CONFIRMATIONS_REQUIRED,
        "txid": txid,
        "ts": time.time(),
    }


def _dispatch(alert: Dict[str, Any]) -> bool:
    """Deliver to every configured channel; True only if all accepted.

    An empty channel list (alerts disabled or every channel off) counts
    as NOT delivered, so nothing is ever marked as alerted that no
    channel actually saw.
    """
    channels = notifier.build_notifiers()
    if not channels:
        return False
    return all(channel.send(alert) for channel in channels)


def _watch_inbound(
    chain: Any, order: Dict[str, Any], counts: Dict[str, int], late: bool
) -> None:
    """Evaluate one inbound order's address: deposit alert + confirm flip.

    `late` selects the expired-order reconciliation path (status
    late_paid). Below-threshold and RPC-failure outcomes count as
    still_pending only on the live path, matching the pre-alerting
    behaviour of the expired loop.
    """
    maza = order["maza_amount"]
    if maza is None:
        if not late:
            counts["still_pending"] += 1
        return
    address = order["address"]

    # Stage 1 — deposit detected: funds cover the quote at a low
    # confirmation threshold. Deliver first, mark only on success: the
    # deposit_alerted_ts marker on the order row survives restarts, so a
    # delivered alert is never repeated, while an undelivered one (channel
    # down) is retried on every subsequent poll.
    if order.get("deposit_alerted_ts") is None:
        try:
            seen = chain.getreceivedbyaddress(address, config.ALERT_DEPOSIT_MINCONF)
        except Exception:
            seen = None
        if seen is not None and seen >= maza:
            payload = _alert_payload(
                order, "deposit_detected", seen, find_txid(chain, address, maza),
                order["status"],
            )
            if _dispatch(payload):
                store.update_order(
                    order["id"], {"deposit_alerted_ts": time.time()}, "deposit_alerted"
                )
                counts["deposit_alerts"] += 1

    # Stage 2 — confirmation threshold: flips paid/late_paid exactly once
    # (the order then leaves the watch set). confirm_alerted_ts rides in
    # the SAME update as the status flip, so the confirm alert is armed
    # exactly once per order regardless of restarts.
    try:
        received = chain.getreceivedbyaddress(address, config.CONFIRMATIONS_REQUIRED)
    except Exception:
        if not late:
            counts["still_pending"] += 1
        return
    if received >= maza:
        txid = find_txid(chain, address, maza)
        status = "late_paid" if late else "paid"
        fields = {
            "status": status,
            "paid_ts": time.time(),
            "confirmations": config.CONFIRMATIONS_REQUIRED,
            "txid": txid,
        }
        if config.ALERTS_ENABLED:
            # Armed in the SAME update as the flip: exactly once per order,
            # restarts can never re-alert the transition. When alerting is
            # disabled no marker is written, so the audit entry stays
            # identical to the pre-alerting behaviour.
            fields["confirm_alerted_ts"] = time.time()
        store.update_order(
            order["id"],
            fields,
            "late_receipt_detected" if late else "receipt_confirmed",
        )
        counts["late_paid" if late else "paid"] += 1
        if _dispatch(_alert_payload(order, "deposit_confirmed", received, txid, status)):
            counts["confirm_alerts"] += 1
    elif not late:
        counts["still_pending"] += 1


def poll_once() -> Dict[str, int]:
    """One watcher pass.

    Returns counts: {paid, late_paid, expired, still_pending,
    deposit_alerts, confirm_alerts} — the last two count alerts actually
    delivered by this pass (a retried deposit alert counts again: it had
    not been delivered before).
    """
    chain, _mode = get_chain()
    store.mark_expired_stale()
    counts = {
        "paid": 0,
        "late_paid": 0,
        "expired": 0,
        "still_pending": 0,
        "deposit_alerts": 0,
        "confirm_alerts": 0,
    }
    for order in store.pending_inbound():
        _watch_inbound(chain, order, counts, late=False)
    # Late payments on expired orders: reconcile, never drop.
    for order in store.expired_inbound():
        _watch_inbound(chain, order, counts, late=True)
    return counts


class Watcher(threading.Thread):
    """Background poll loop; daemon so it never blocks shutdown."""

    def __init__(self, interval: Optional[int] = None):
        super().__init__(name="mazapay-watcher", daemon=True)
        self.interval = interval or config.WATCH_INTERVAL_SECONDS
        self._halt = threading.Event()

    def run(self) -> None:
        while not self._halt.is_set():
            try:
                poll_once()
            except Exception:
                pass  # state changes are audited; the loop must survive errors
            self._halt.wait(self.interval)

    def stop(self) -> None:
        self._halt.set()
