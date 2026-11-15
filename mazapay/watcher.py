"""Confirmation watcher: turns pending inbound orders into paid receipts,
catches late payments on expired orders, and locates outbound broadcasts.

Polls the chain adapter for every pending order's address; when the
received amount (at CONFIRMATIONS_REQUIRED) covers the quoted MAZA amount,
the order flips to `paid` with paid_ts + txid, and an audit entry records
the receipt evidence (amount, confirmations, txid).

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

from . import config, store
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


def poll_once() -> Dict[str, int]:
    """One watcher pass. Returns counts: {paid, late_paid, expired, still_pending}."""
    chain, _mode = get_chain()
    store.mark_expired_stale()
    counts = {"paid": 0, "late_paid": 0, "expired": 0, "still_pending": 0}
    for order in store.pending_inbound():
        try:
            received = chain.getreceivedbyaddress(
                order["address"], config.CONFIRMATIONS_REQUIRED
            )
        except Exception:
            counts["still_pending"] += 1
            continue
        if order["maza_amount"] is not None and received >= order["maza_amount"]:
            txid = find_txid(chain, order["address"], order["maza_amount"])
            store.update_order(
                order["id"],
                {
                    "status": "paid",
                    "paid_ts": time.time(),
                    "confirmations": config.CONFIRMATIONS_REQUIRED,
                    "txid": txid,
                },
                "receipt_confirmed",
            )
            counts["paid"] += 1
        else:
            counts["still_pending"] += 1
    # Late payments on expired orders: reconcile, never drop.
    for order in store.expired_inbound():
        try:
            received = chain.getreceivedbyaddress(
                order["address"], config.CONFIRMATIONS_REQUIRED
            )
        except Exception:
            continue
        if order["maza_amount"] is not None and received >= order["maza_amount"]:
            txid = find_txid(chain, order["address"], order["maza_amount"])
            store.update_order(
                order["id"],
                {
                    "status": "late_paid",
                    "paid_ts": time.time(),
                    "confirmations": config.CONFIRMATIONS_REQUIRED,
                    "txid": txid,
                },
                "late_receipt_detected",
            )
            counts["late_paid"] += 1
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
