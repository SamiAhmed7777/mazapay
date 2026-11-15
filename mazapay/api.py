"""MazaPay bridge HTTP API — stdlib http.server, JSON in/out.

Safety model (v0.3 — reworked across judge rounds 1-4):

- AUTH: when MAZAPAY_API_TOKEN is set, every endpoint except GET /health,
  GET / (the public checkout page), and the member-facing collect routes
  (POST /orders, GET /orders/{id}) requires 'Authorization: Bearer <token>'
  (constant-time compare). The collect routes are public because they only
  create a watch address and lock a quote — no money moves — while every
  spending or administrative endpoint stays tokened. A real wallet RPC
  REFUSES to serve without a token — fail closed, because POST /payments
  spends from the wallet.
- REAL-MODE GUARDS: serving against a real wallet also refuses a synthetic
  fixed price unless the operator enabled MAZAPAY_ALLOW_FIXED_PRICE.
- DISBURSEMENT IS INTENT-FIRST: the payment row (status pending_send, with
  a unique nonce) is durably recorded BEFORE the broadcast. An RPC failure
  after broadcast leaves the row in send_uncertain (never silently lost);
  POST /payments/{id}/reconcile matches the broadcast via the nonce comment
  and either binds its txid (sent) or marks send_failed. Retry exists only
  for send_failed rows with no txid — a duplicate broadcast for an order
  that already has a txid is refused, so the double-spend window is closed
  structurally.
- LATE PAYMENTS: an expired order whose address later receives the quoted
  funds moves to late_paid; the operator accepts it (paid) or refunds it
  (a nonce-guarded outbound payment at the original locked amount) — funds
  are never forgotten, and refund payments are durably linked to their
  parent order so reconcile keeps the pair consistent.

Endpoints:
  GET  /                              — member-facing checkout page (public)
  GET  /health                        — liveness + mode info (no auth)
  POST /vendors                       — {name, code, maza_address?, notes?}
  GET  /vendors
  POST /orders                        — {usd_amount, ref?, vendor_id?}  (collect; public)
  GET  /orders/{id}                                                    (public)
  POST /orders/{id}/simulate_payment  — {confirms?}  (mock chain only)
  POST /orders/{id}/accept            — accept a late_paid order at its quote
  POST /orders/{id}/refund            — {address} refund a late_paid order
  POST /payments                      — {address, usd_amount, vendor_id?, ref?}
  POST /payments/{id}/reconcile       — resolve send_uncertain / send_failed
  POST /payments/{id}/retry           — re-broadcast a send_failed payment
  POST /watch                         — run one watcher pass now
  GET  /export/mip?kind=in|out        — write + return a MIP CSV session
"""
import hmac
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from . import config, mip_export, pricing, store, watcher
from .mazad import ChainError, get_chain, is_mock

_TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "checkout.html")

_ACCESS_LOG = logging.getLogger("mazapay.access")
_ERROR_LOG = logging.getLogger("mazapay.errors")
# Library-safe default: unconfigured processes (tests, library use) stay
# silent instead of leaking log lines to stderr via logging.lastResort;
# main() swaps in the real stream handler for journald capture.
_ACCESS_LOG.addHandler(logging.NullHandler())

# M-4: conservative CSP for the checkout page. The inline <script> and
# onclick= handlers in checkout.html require 'unsafe-inline' until the page's
# JS is externalized; frame-ancestors 'none' + X-Frame-Options DENY carry the
# clickjacking protection meanwhile. HSTS belongs at the TLS front (Caddy).
_CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self' 'unsafe-inline'; "
    "style-src 'unsafe-inline'; connect-src 'self'; img-src 'self'; "
    "base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
)

# H-1 intake: readable reference codes only — anything else (formula
# payloads, control characters, overlong strings) is refused at the door
# instead of neutralized later in the MIP export. fullmatch() so a trailing
# newline cannot sneak past the anchor.
_REF_RE = re.compile(r"[\w .:@/-]{0,120}")


class ApiError(Exception):
    def __init__(self, status: int, message: str, headers: Optional[Dict[str, str]] = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.headers = dict(headers or {})


_MAX_USD_AMOUNT = Decimal("1000000000000")  # $1T sanity ceiling
_MAX_BODY_BYTES = 10 * 1024 * 1024  # 10 MiB request-body cap (see _body)


def _usd_amount(value: Any, field: str = "usd_amount") -> Decimal:
    """Parse a positive money amount strictly; non-numeric input is 400.

    NaN/Infinity are refused: they are not money, a NaN comparison raises
    InvalidOperation, and an Infinity/large-exponent amount raises
    decimal.Overflow inside the quote quantize — each of which surfaced as
    an unhandled 500 instead of a 4xx.
    """
    if value is None or value == "":
        raise ApiError(400, f"{field} must be a positive number")
    try:
        usd = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ApiError(400, f"{field} must be a number") from None
    if not usd.is_finite():
        raise ApiError(400, f"{field} must be a finite number")
    if usd <= 0:
        raise ApiError(400, f"{field} must be positive")
    if usd > _MAX_USD_AMOUNT:
        raise ApiError(400, f"{field} exceeds the maximum supported amount")
    return usd


def _confirmations(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        # OverflowError: float('inf') / 1e999 are JSON-representable and
        # cannot convert to int — a 400, never a 500.
        raise ApiError(400, "confirms must be an integer") from None


def _str_field(value: Any, field: str) -> str:
    """Coerce an optional free-form string field; structured types are 400.

    Dict/list values would otherwise crash the SQLite bind (ProgrammingError
    -> 500) or ride into the store and the MIP CSV export untouched.
    """
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        return value
    raise ApiError(400, f"{field} must be a string")


def _quote_or_503(usd: Decimal) -> Tuple[Decimal, Decimal, str]:
    try:
        maza, rate, source = pricing.quote_maza(usd)
    except (ValueError, pricing.PriceUnavailable) as exc:
        raise ApiError(503, f"price feed unavailable: {exc}") from exc
    return maza, rate, source


def _address_from(payload: Dict[str, Any]) -> Tuple[str, str]:
    """Resolve + validate the payee address; returns (address, vendor_id)."""
    vendor_id = _str_field(payload.get("vendor_id", ""), "vendor_id")
    address = _str_field(payload.get("address", ""), "address")
    if vendor_id and not store.get_vendor(vendor_id):
        raise ApiError(404, f"vendor {vendor_id} not found")
    if vendor_id and not address:
        vendor = store.get_vendor(vendor_id)
        address = (vendor or {}).get("maza_address", "")
    if not address:
        raise ApiError(400, "address required (or set one on the vendor)")
    chain, _mode = get_chain()
    try:
        va = chain.validateaddress(address)
    except ChainError as exc:
        raise ApiError(503, f"chain unavailable: {exc}") from exc
    if not va.get("isvalid"):
        raise ApiError(400, f"invalid MAZA address: {address}")
    return address, vendor_id


def create_order(
    usd_amount,
    ref: str = "",
    vendor_id: str = "",
    client_ip: str = "",
) -> Dict[str, Any]:
    """Quote + open an inbound order with a locked rate and a fresh address.

    Carries the H-2 per-order ceiling; the awaiting-order caps (per-IP and
    global) are checked by the HTTP intake in _orders_post.
    """
    usd = _usd_amount(usd_amount)
    if usd > config.MAX_ORDER_USD:
        raise ApiError(
            400,
            f"usd_amount exceeds the per-order maximum of {config.MAX_ORDER_USD:g} USD",
        )
    if vendor_id and not store.get_vendor(vendor_id):
        raise ApiError(404, f"vendor {vendor_id} not found")
    maza_amount, rate, source = _quote_or_503(usd)
    chain, mode = get_chain()
    try:
        address = chain.getnewaddress(f"mazapay:{ref or 'order'}")
    except ChainError as exc:
        raise ApiError(503, f"chain unavailable: {exc}") from exc
    order = store.create_order(
        kind="in",
        usd_amount=usd,
        maza_amount=maza_amount,
        rate=rate,
        rate_source=source,
        vendor_id=vendor_id,
        ref=ref,
        address=address,
        client_ip=client_ip,
    )
    order["status"] = "awaiting"  # quoted -> awaiting funds
    store.update_order(order["id"], {"status": "awaiting"}, "awaiting_funds")
    order["chain_mode"] = mode
    return order


def _broadcast_out(
    usd: Decimal,
    address: str,
    vendor_id: str = "",
    ref: str = "",
    locked: Optional[Tuple[Decimal, Decimal, str]] = None,
    parent_order_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Intent-first disbursement shared by /payments and late refunds.

    Order of operations: durably record the payment intent (pending_send +
    unique nonce) BEFORE calling sendtoaddress; on RPC failure classify
    uncertain (broadcast may have happened) vs unclassified (row stays
    pending_send only for the instant before the call).

    `locked` carries a pre-agreed quote (maza_amount, rate, rate_source) —
    the late-payment refund path uses it so the refund returns exactly the
    MAZA quantity that was received at the order's original locked rate,
    regardless of what the market price does in between.

    `parent_order_id` durably links a refund payment to its late order so
    the parent transitions to `refunded` when the refund payment is later
    reconciled as sent (and back to `late_paid` if it is proven not to have
    broadcast) — the pair can never be left inconsistent by reconcile.
    """
    if locked is not None:
        maza_amount, rate, source = locked
    else:
        maza_amount, rate, source = _quote_or_503(usd)
    nonce = uuid.uuid4().hex
    order = store.create_order(
        kind="out",
        usd_amount=usd,
        maza_amount=maza_amount,
        rate=rate,
        rate_source=source,
        vendor_id=vendor_id,
        ref=ref,
        address=address,
        initial_status="pending_send",
        nonce=nonce,
        parent_order_id=parent_order_id,
    )
    chain, mode = get_chain()
    try:
        txid = chain.sendtoaddress(address, float(maza_amount), f"mazapay:{nonce}")
    except ChainError as exc:
        store.update_order(order["id"], {"status": "send_uncertain"}, "send_uncertain")
        order["status"] = "send_uncertain"
        order["chain_mode"] = mode
        order["send_error"] = str(exc)
        return order
    store.update_order(
        order["id"], {"status": "sent", "txid": txid, "paid_ts": time.time()}, "disbursed"
    )
    order["status"] = "sent"
    order["txid"] = txid
    order["chain_mode"] = mode
    return order


def disburse(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Send MAZA to a validated address; intent recorded before broadcast.

    Carries the M-6 disbursement ceiling: a leaked token must not be able to
    drain the wallet in one oversized call (checked BEFORE the intent row is
    written, so a refused call leaves nothing behind).
    """
    usd = _usd_amount(payload.get("usd_amount"))
    if usd > config.MAX_DISBURSE_USD:
        raise ApiError(
            400,
            f"usd_amount exceeds the disbursement ceiling of {config.MAX_DISBURSE_USD:g} USD",
        )
    address, vendor_id = _address_from(payload)
    return _broadcast_out(usd, address, vendor_id, _str_field(payload.get("ref", ""), "ref"))


def reconcile_payment(order_id: str) -> Dict[str, Any]:
    """Resolve send_uncertain / send_failed against the chain, idempotently.

    Scan semantics: only a COMPLETED scan that finds no matching send may
    downgrade the payment to send_failed. A failed RPC scan raises
    ChainError; the payment keeps its current state (an unavailable lookup
    is never treated as proof that no broadcast occurred, and no write
    happens at all on that path).

    State moves are atomic compare-and-sets (store.transition), so
    concurrent reconciles can never clobber a state a parallel flow has
    already applied — in particular a 'superseded' refund can never be
    flipped back to send_failed, which would reopen the duplicate-refund
    path. A loser re-reads and reports the winner's state.
    """
    order = store.get_order(order_id)
    if not order or order["kind"] != "out":
        raise ApiError(404, f"outbound payment {order_id} not found")
    if order["txid"] and order["status"] == "sent":
        order["reconciled"] = "already_sent_noop"
        return order
    if order["status"] not in ("send_uncertain", "send_failed"):
        raise ApiError(409, f"payment {order_id} is '{order['status']}'; nothing to reconcile")
    chain, _mode = get_chain()
    comment = f"mazapay:{order['nonce']}"
    try:
        txid = watcher.find_send_txid(chain, order["address"], order["maza_amount"], comment)
    except ChainError as exc:
        # strict read: no write at all — superseded/sent stay untouched
        fresh = store.get_order(order_id) or order
        fresh["reconciled"] = f"scan_unavailable: {exc}"
        return fresh
    if txid:
        won = store.transition(
            order_id,
            order["status"],  # uncertain OR failed: a found broadcast is always sent
            "sent",
            fields={"txid": txid, "paid_ts": time.time()},
            event="reconciled_broadcast_found",
        )
        if not won:
            fresh = store.get_order(order_id) or order
            fresh["reconciled"] = "lost_race_state_changed_elsewhere"
            return fresh
        order["status"], order["txid"] = "sent", txid
        order["reconciled"] = "broadcast_found"
        _settle_refund_parent(order, "sent")
        return order
    # completed scan, no match: only uncertain -> failed is a legal move;
    # a payment already superseded (or otherwise moved) is left alone
    won = store.transition(
        order_id, "send_uncertain", "send_failed", event="reconciled_no_broadcast"
    )
    fresh = store.get_order(order_id) or order
    if won:
        _settle_refund_parent(fresh, "send_failed")
        fresh["reconciled"] = "no_broadcast_found"
    else:
        fresh["reconciled"] = f"state_moved_elsewhere: {fresh['status']}"
    return fresh


def _settle_refund_parent(payment: Dict[str, Any], outcome: str) -> None:
    """Keep a refund payment and its late order consistent across reconcile.

    The outbound refund row durably carries parent_order_id. When the refund
    is proven sent, the parent late order completes as refunded. When it is
    proven NOT to have broadcast, the old payment is FIRST superseded via a
    compare-and-set (send_failed -> superseded) and the parent is released
    to late_paid ONLY if that supersede won. Ordering matters: if a
    concurrent retry claimed the payment between the reconcile's scan and
    the supersede, the CAS loses, the parent stays 'refunding', and the
    retry completes it — a payment that is broadcastable or already
    broadcast can never be marked superseded, so the duplicate-refund path
    stays closed even under the reconcile-versus-retry race.
    """
    parent_id = payment.get("parent_order_id")
    if not parent_id:
        return
    if outcome == "sent":
        store.transition(parent_id, "refunding", "refunded", event="refund_reconciled_sent")
    elif outcome == "send_failed":
        superseded = store.transition(
            payment["id"], "send_failed", "superseded", event="refund_superseded"
        )
        if superseded:
            store.transition(
                parent_id, "refunding", "late_paid", event="refund_reconciled_absent"
            )


def retry_payment(order_id: str) -> Dict[str, Any]:
    """Re-broadcast a payment proven NOT to have broadcast — same row.

    The retry atomically claims the row via compare-and-set (only one
    concurrent caller can move send_failed -> pending_send), rebinds a
    fresh nonce before the broadcast so a reconcile scan matches the latest
    intent, and reuses the row so no duplicate payment record exists. Only
    a send_failed row with no txid qualifies; anything else is refused,
    closing the double-spend path.
    """
    order = store.get_order(order_id)
    if not order or order["kind"] != "out":
        raise ApiError(404, f"outbound payment {order_id} not found")
    if order["txid"] or order["status"] != "send_failed":
        raise ApiError(
            409,
            f"payment {order_id} is '{order['status']}' (txid={order['txid']!r});"
            " retry is only for send_failed payments with no txid",
        )
    if not store.claim_retry(order_id, uuid.uuid4().hex):
        raise ApiError(409, f"payment {order_id} is being retried or changed elsewhere")
    order = store.get_order(order_id) or order
    new_nonce = order["nonce"]
    chain, mode = get_chain()
    try:
        txid = chain.sendtoaddress(
            order["address"], order["maza_amount"], f"mazapay:{new_nonce}"
        )
    except ChainError as exc:
        store.update_order(order["id"], {"status": "send_uncertain"}, "retry_send_uncertain")
        order["status"] = "send_uncertain"
        order["send_error"] = str(exc)
        order["chain_mode"] = mode
        return order
    store.update_order(
        order["id"], {"status": "sent", "txid": txid, "paid_ts": time.time()}, "retry_disbursed"
    )
    order["status"], order["txid"] = "sent", txid
    order["chain_mode"] = mode
    _settle_refund_parent(order, "sent")
    return order


def accept_late(order_id: str) -> Dict[str, Any]:
    """Operator accepts a late_paid order at its original locked quote.

    The late_paid -> paid move is an atomic compare-and-set, so concurrent
    accept/refund calls cannot both act on the same order.
    """
    order = store.get_order(order_id)
    if not order or order["kind"] != "in":
        raise ApiError(404, f"inbound order {order_id} not found")
    if order["status"] != "late_paid":
        raise ApiError(409, f"order {order_id} is '{order['status']}'; accept is for late_paid")
    if not store.transition(order_id, "late_paid", "paid", event="late_accepted"):
        raise ApiError(409, f"order {order_id} changed state concurrently; re-check and retry")
    order["status"] = "paid"
    return order


def refund_late(order_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Operator refunds a late_paid order via a nonce-guarded outbound payment.

    The refund returns the ORIGINAL locked MAZA amount (the quantity that
    was actually received at the order's locked rate) — not a fresh market
    quote — so a price move between receipt and refund can never change the
    refunded quantity. The order moves late_paid -> refunding atomically;
    only the winner pays. An uncertain refund keeps the order 'refunding'
    (visible state; the refund payment has its own reconcile path).
    """
    order = store.get_order(order_id)
    if not order or order["kind"] != "in":
        raise ApiError(404, f"inbound order {order_id} not found")
    if order["status"] != "late_paid":
        raise ApiError(409, f"order {order_id} is '{order['status']}'; refund is for late_paid")
    if not store.transition(order_id, "late_paid", "refunding", event="refund_claimed"):
        raise ApiError(409, f"order {order_id} changed state concurrently; re-check and retry")
    address = _str_field(payload.get("address", ""), "address") or (
        store.get_vendor(order["vendor_id"]) or {}
    ).get("maza_address", "")
    if not address:
        store.update_order(order["id"], {"status": "late_paid"}, "refund_needs_address")
        raise ApiError(400, "address required for refund")
    chain, _mode = get_chain()
    try:
        va = chain.validateaddress(address)
    except ChainError as exc:
        store.update_order(order["id"], {"status": "late_paid"}, "refund_chain_unavailable")
        raise ApiError(503, f"chain unavailable: {exc}") from exc
    if not va.get("isvalid"):
        store.update_order(order["id"], {"status": "late_paid"}, "refund_invalid_address")
        raise ApiError(400, f"invalid MAZA address: {address}")
    locked = (
        Decimal(str(order["maza_amount"])),
        Decimal(str(order["rate"])),
        order["rate_source"] or "locked",
    )
    refund = _broadcast_out(
        Decimal(str(order["usd_amount"])),
        address,
        order["vendor_id"],
        f"refund:{order_id}",
        locked=locked,
        parent_order_id=order_id,
    )
    if refund["status"] == "sent":
        store.transition(order_id, "refunding", "refunded", event="late_refunded")
        order["status"] = "refunded"
    else:
        store.update_order(order["id"], {"status": "refunding"}, "refund_send_uncertain")
        order["status"] = "refunding"
    order["refund"] = refund
    return order


def simulate_payment(order_id: str, confirms: int) -> Dict[str, Any]:
    """Demo helper: fund an inbound order's address on the MockChain."""
    chain, _mode = get_chain()
    if not is_mock(chain):
        raise ApiError(403, "simulate_payment is only available in mock-chain mode")
    order = store.get_order(order_id)
    if not order or order["kind"] != "in":
        raise ApiError(404, f"inbound order {order_id} not found")
    txid = chain.fund(order["address"], order["maza_amount"], confirms)
    return {"simulated_txid": txid, "confirms": confirms, "order": order_id}


# (method, regex, handler-name, requires_auth)
# POST /orders + GET /orders/{id} are public (member checkout): they create
# a watch address and lock a quote — no money can move through them.
_ROUTES = [
    ("GET", re.compile(r"^/$"), "index", False),
    ("GET", re.compile(r"^/health$"), "health", False),
    ("POST", re.compile(r"^/vendors$"), "vendors_post", True),
    ("GET", re.compile(r"^/vendors$"), "vendors_get", True),
    ("POST", re.compile(r"^/orders$"), "orders_post", False),
    ("GET", re.compile(r"^/orders/([\w-]+)$"), "order_get", False),
    ("POST", re.compile(r"^/orders/([\w-]+)/simulate_payment$"), "order_simulate", True),
    ("POST", re.compile(r"^/orders/([\w-]+)/accept$"), "order_accept", True),
    ("POST", re.compile(r"^/orders/([\w-]+)/refund$"), "order_refund", True),
    ("POST", re.compile(r"^/payments$"), "payments_post", True),
    ("POST", re.compile(r"^/payments/([\w-]+)/reconcile$"), "payment_reconcile", True),
    ("POST", re.compile(r"^/payments/([\w-]+)/retry$"), "payment_retry", True),
    ("POST", re.compile(r"^/watch$"), "watch_post", True),
    ("GET", re.compile(r"^/export/mip$"), "export_get", True),
]


def _check_intake_caps(client_ip: str) -> None:
    """H-2 application caps on PUBLIC order creation.

    The awaiting-order slots (per client IP and globally) are finite: an
    anonymous flood can no longer grow the wallet keypool, the watcher scan
    set, and the store without bound. 429 + Retry-After, matching the
    documented failure shape.
    """
    if store.count_awaiting() >= config.MAX_AWAITING_ORDERS_GLOBAL:
        raise ApiError(
            429,
            "too many open orders; retry later",
            headers={"Retry-After": str(config.ORDER_RETRY_AFTER_SECONDS)},
        )
    if store.count_awaiting(client_ip=client_ip) >= config.MAX_AWAITING_ORDERS_PER_IP:
        raise ApiError(
            429,
            "too many open orders from your address; retry later",
            headers={"Retry-After": str(config.ORDER_RETRY_AFTER_SECONDS)},
        )


class Handler(BaseHTTPRequestHandler):
    # No version, no interpreter banner (M-4): "MazaPay" only — the old
    # "MazaPay/0.3.1 Python/3.x" Server header aided targeted scanning.
    server_version = "MazaPay"

    def version_string(self) -> str:
        # BaseHTTPRequestHandler appends sys_version ("Python/x.y.z") by
        # default — drop the interpreter banner too.
        return self.server_version
    # H-3: socket timeout on every handler read/write (slowloris body
    # dribbles can no longer hold a thread forever). A timed-out body read
    # answers 408 (see _body); the stdlib closes hung request-line reads.
    timeout = config.HANDLER_TIMEOUT_SECONDS

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002  (stdlib name)
        pass

    # -- connection lifecycle (bounded concurrency, H-3) ----------------------
    def setup(self) -> None:
        self._connection_slot: Optional[threading.BoundedSemaphore] = None
        self._shed = False
        slots = getattr(self.server, "connection_slots", None)
        if slots is not None:
            if slots.acquire(blocking=False):
                self._connection_slot = slots
            else:
                # Over the concurrent-connection bound: shed load instead of
                # growing the thread count unbounded.
                self._shed = True
        super().setup()
        if self._shed:
            # request parsing never happened (setup precedes
            # handle_one_request), so the stdlib logging machinery's
            # attributes do not exist yet and request_version is still
            # HTTP/0.9 (which would suppress the whole status line). Set
            # what send_response touches before shedding.
            self.request_version = "HTTP/1.0"
            self.requestline = ""
            self.close_connection = True
            try:
                body = b'{"error": "server busy; retry later"}'
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Retry-After", str(config.ORDER_RETRY_AFTER_SECONDS))
                self._security_headers()  # uniform with every other response
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                pass
            # _client_ip() cannot see X-Forwarded-For here (headers are
            # parsed after setup), so the shed line records the peer
            # address — the proxy hop, not the end client.
            _ACCESS_LOG.warning(
                '%s "CONNECT shed" 503 (connection limit reached)',
                self._client_ip(),
            )

    def handle(self) -> None:
        # A connection shed in setup() never got a slot: its 503 is already
        # written, there is nothing to serve.
        if self._shed:
            return
        super().handle()

    def finish(self) -> None:
        try:
            super().finish()
        finally:
            if self._connection_slot is not None:
                self._connection_slot.release()
                self._connection_slot = None

    # -- helpers --------------------------------------------------------------
    def _client_ip(self) -> str:
        """Client IP for caps + access logs: X-Forwarded-For's first hop when
        present (trusted only because the listener binds loopback behind the
        proxy), else the peer address. headers may not exist yet during
        setup() — getattr guards that."""
        headers = getattr(self, "headers", None)
        xff = headers.get("X-Forwarded-For", "") if headers else ""
        if xff:
            return xff.split(",")[0].strip()
        try:
            return self.client_address[0]
        except Exception:  # pragma: no cover - peer vanished before logging
            return "-"

    def _security_headers(self) -> None:
        """M-4: emitted on EVERY response (HTML + JSON)."""
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", _CONTENT_SECURITY_POLICY)

    def send_response(self, code: int, message: Optional[str] = None) -> None:
        self._response_status = code  # captured for the access log line
        super().send_response(code, message)

    def _json(self, status: int, payload: Any, extra_headers: Optional[Dict[str, str]] = None) -> None:
        body = json.dumps(payload, indent=2, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if not config.API_TOKEN:
            return True  # open mode (mock/demo only; real mode refuses to serve)
        header = self.headers.get("Authorization", "")
        try:
            # Compare ENCODED BYTES: compare_digest rejects str inputs with
            # non-ASCII characters (TypeError -> dropped connection with no
            # HTTP response); bytes never raise, so hostile headers are a
            # plain 401.
            return hmac.compare_digest(
                header.encode("utf-8"), f"Bearer {config.API_TOKEN}".encode("utf-8")
            )
        except Exception:
            return False

    def _body(self) -> Dict[str, Any]:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None or raw_length == "":
            return {}
        try:
            length = int(raw_length)
        except (TypeError, ValueError):
            self.close_connection = True
            raise ApiError(400, "invalid Content-Length") from None
        if length <= 0:
            # 0 = empty body; a negative value would make rfile.read() read
            # until EOF — refuse before touching the socket.
            return {}
        if length > _MAX_BODY_BYTES:
            # refuse before buffering: a multi-GB declared body would
            # otherwise be read fully before the JSON parse rejects it
            self.close_connection = True
            raise ApiError(413, "request body too large")
        try:
            data = self.rfile.read(length)
        except TimeoutError:
            # H-3: the socket timeout tripped mid-body — a controlled 408
            # instead of a thread wedged forever on a dribbling client.
            self.close_connection = True
            raise ApiError(408, "request body read timed out") from None
        except OSError:
            self.close_connection = True
            raise ApiError(400, "request body read failed") from None
        try:
            data = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, RecursionError):
            self.close_connection = True
            raise ApiError(400, "invalid JSON body") from None
        if not isinstance(data, dict):
            self.close_connection = True
            raise ApiError(400, "JSON object body required")
        return data

    # -- dispatch -------------------------------------------------------------
    def _route(self, method: str) -> None:
        path = urlparse(self.path)
        route = None
        needs_auth = True
        args: Tuple[Any, ...] = ()
        for m, rx, name, route_needs_auth in _ROUTES:
            match = rx.match(path.path)
            if match and m == method:
                route, needs_auth, args = name, route_needs_auth, match.groups()
                break
        if route is None:
            return self._json(404, {"error": f"no route {method} {path.path}"})
        if needs_auth and not self._authorized():
            return self._json(401, {"error": "missing or invalid Authorization bearer token"})
        try:
            handler = getattr(self, f"_{route}")
            handler(*args)
        except ApiError as exc:
            self._json_quiet(exc.status, {"error": exc.message}, exc.headers)
        except (BrokenPipeError, ConnectionResetError):
            # client vanished mid-response — routine (aborted page loads),
            # never an error condition and never a 500
            self.close_connection = True
        except Exception:  # noqa: BLE001
            # M-1 hygiene: the exception repr used to be returned to the
            # client (`internal: <repr>`) — log it server-side, return a
            # fixed message.
            _ERROR_LOG.exception("unhandled error serving %s %s", method, self.path)
            self._json_quiet(500, {"error": "internal server error"})

    def _json_quiet(self, status: int, payload: Any, headers: Optional[Dict[str, str]] = None) -> None:
        """Respond, but treat a client-side disconnect as routine."""
        try:
            self._json(status, payload, headers)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        """Route with timing, then write the M-5 access-log line.

        One line per request: client ip, method, path, status, duration.
        Never the Authorization header, never bodies — the token must not
        appear in any log. Auth/limit failures log at WARNING.
        """
        start = time.perf_counter()
        try:
            self._route(method)
        finally:
            status = getattr(self, "_response_status", 0)
            level = logging.WARNING if status in (400, 401, 403, 408, 413, 429, 503) else logging.INFO
            _ACCESS_LOG.log(
                level,
                '%s "%s %s" %s %.3fs',
                self._client_ip(),
                method,
                self.path,
                status,
                time.perf_counter() - start,
            )

    # -- handlers -------------------------------------------------------------
    def _index(self) -> None:
        try:
            with open(_TEMPLATE_PATH, "rb") as fh:
                body = fh.read()
        except OSError:
            return self._json(404, {"error": "checkout template missing"})
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _health(self) -> None:
        _chain, mode = get_chain()
        self._json(
            200,
            {
                "ok": True,
                "service": "mazapay-bridge",
                "chain_mode": mode,
                "auth_mode": "token" if config.API_TOKEN else "open",
                "confirmations_required": config.CONFIRMATIONS_REQUIRED,
                "price_source": config.PRICE_SOURCE,
                "price_feed": pricing.feed_health(),
                "allow_fixed_price": config.ALLOW_FIXED_PRICE,
                "rate_lock_seconds": config.RATE_LOCK_SECONDS,
            },
        )

    def _vendors_post(self) -> None:
        body = self._body()
        name = _str_field(body.get("name"), "name").strip()
        code = _str_field(body.get("code"), "code").strip()
        if not name or not code:
            raise ApiError(400, "name and code required")
        try:
            vendor = store.create_vendor(
                name,
                code,
                _str_field(body.get("maza_address", ""), "maza_address"),
                _str_field(body.get("notes", ""), "notes"),
            )
        except sqlite3.IntegrityError:
            # codes are UNIQUE: a re-POST of an existing code is a conflict,
            # not an unhandled 500
            raise ApiError(409, f"vendor code {code!r} already exists") from None
        self._json(201, vendor)

    def _vendors_get(self) -> None:
        self._json(200, {"vendors": store.list_vendors()})

    def _orders_post(self) -> None:
        body = self._body()
        ref = _str_field(body.get("ref", ""), "ref")
        # H-1 intake: the ref is unauthenticated input that lands in MIP
        # export cells (PAYOR/VENDOR/DESCRIPTION). Readable reference codes
        # only — formula payloads never reach the store at all (the export
        # side additionally neutralizes cells, defense in depth).
        if not _REF_RE.fullmatch(ref):
            raise ApiError(
                400,
                "ref must be at most 120 characters of letters, digits,"
                " space, or . : @ / - _",
            )
        client_ip = self._client_ip()
        _check_intake_caps(client_ip)
        self._json(
            201,
            create_order(
                body.get("usd_amount"),
                ref,
                _str_field(body.get("vendor_id", ""), "vendor_id"),
                client_ip=client_ip,
            ),
        )

    def _order_get(self, order_id: str) -> None:
        order = store.get_order(order_id)
        if not order:
            raise ApiError(404, f"order {order_id} not found")
        # Public collect visibility: only inbound (member) orders are
        # readable unauthenticated. Outbound payments carry spending
        # details (payee address, amounts) — tokened or 404 (never leak
        # existence).
        if order["kind"] != "in" and not self._authorized():
            raise ApiError(404, f"order {order_id} not found")
        self._json(200, order)

    def _order_simulate(self, order_id: str) -> None:
        body = self._body()
        confirms = (
            _confirmations(body["confirms"])
            if "confirms" in body
            else config.CONFIRMATIONS_REQUIRED
        )
        self._json(200, simulate_payment(order_id, confirms))

    def _order_accept(self, order_id: str) -> None:
        self._json(200, accept_late(order_id))

    def _order_refund(self, order_id: str) -> None:
        self._json(201, refund_late(order_id, self._body()))

    def _payments_post(self) -> None:
        self._json(201, disburse(self._body()))

    def _payment_reconcile(self, order_id: str) -> None:
        self._json(200, reconcile_payment(order_id))

    def _payment_retry(self, order_id: str) -> None:
        self._json(200, retry_payment(order_id))

    def _watch_post(self) -> None:
        self._json(200, watcher.poll_once())

    def _export_get(self) -> None:
        q = parse_qs(urlparse(self.path).query)
        kind = (q.get("kind") or ["in"])[0]
        include_exported = (q.get("include_exported") or ["0"])[0] in ("1", "true", "yes")
        if kind == "in":
            self._json(200, mip_export.export_cash_receipts(include_exported))
        elif kind == "out":
            self._json(200, mip_export.export_cash_disbursements(include_exported))
        else:
            raise ApiError(400, "kind must be 'in' or 'out'")


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        # H-3: bound simultaneously ACTIVE handler connections (one thread
        # spawns per connection otherwise). Handler.setup sheds the overflow
        # with 503 + Retry-After; slots release in finish().
        self.connection_slots = threading.BoundedSemaphore(config.MAX_CONCURRENT_CONNECTIONS)


def require_safe_mode() -> None:
    """Fail closed on dangerous real-mode configurations.

    - a real wallet RPC without MAZAPAY_API_TOKEN would expose sendtoaddress
      to anyone who can reach the port — refuse to serve;
    - a synthetic fixed price in real-wallet mode silently prices real money
      from a stale rate — refuse unless the operator enabled
      MAZAPAY_ALLOW_FIXED_PRICE explicitly.
    """
    if config.MAZAD_RPC_URL:
        if not config.API_TOKEN:
            raise RuntimeError(
                "MAZAPAY_API_TOKEN is required when MAZAPAY_MAZAD_RPC is configured"
                " (POST /payments spends from the wallet)"
            )
        if config.PRICE_SOURCE == "fixed" and not config.ALLOW_FIXED_PRICE:
            raise RuntimeError(
                "PRICE_SOURCE=fixed is refused in real-wallet mode;"
                " configure a live price source or set MAZAPAY_ALLOW_FIXED_PRICE=1"
                " to accept the synthetic rate explicitly"
            )


_WATCHER: Optional[watcher.Watcher] = None  # retained handle (join/stop)


def serve(host: Optional[str] = None, port: Optional[int] = None, with_watcher: bool = True) -> Server:
    global _WATCHER
    store.init_db()
    require_safe_mode()
    srv = Server((host or config.LISTEN_HOST, port or config.LISTEN_PORT), Handler)
    bind = host or config.LISTEN_HOST
    if bind not in ("127.0.0.1", "::1", "localhost"):
        # The per-IP order cap and access logs trust X-Forwarded-For
        # because the listener is loopback-bound behind the TLS proxy;
        # on any other bind that cap key becomes client-controlled.
        _ERROR_LOG.warning(
            "listening on %s (not loopback): the X-Forwarded-For-based"
            " per-IP cap key is client-controlled on a non-loopback bind;"
            " bind 127.0.0.1 behind the proxy", bind
        )
    if with_watcher:
        _WATCHER = watcher.Watcher()
        _WATCHER.start()
    return srv


def shutdown_watcher() -> None:
    if _WATCHER is not None:
        _WATCHER.stop()


def main() -> None:  # pragma: no cover - manual run helper
    # M-5: the access log is only useful if something consumes it — journald
    # captures stderr for the systemd unit. Tests use assertLogs instead.
    # (An embedder that configured a real handler keeps theirs.)
    if all(isinstance(h, logging.NullHandler) for h in _ACCESS_LOG.handlers):
        _handler = logging.StreamHandler()
        _handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
        _ACCESS_LOG.addHandler(_handler)
        _ACCESS_LOG.setLevel(logging.INFO)
    store.init_db()
    srv = serve()
    print(f"mazapay-bridge listening on http://{srv.server_address[0]}:{srv.server_address[1]}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":  # pragma: no cover
    main()
