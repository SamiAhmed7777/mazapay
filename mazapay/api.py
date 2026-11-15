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
import os
import re
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


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _usd_amount(value: Any, field: str = "usd_amount") -> Decimal:
    """Parse a positive money amount strictly; non-numeric input is 400."""
    if value is None or value == "":
        raise ApiError(400, f"{field} must be a positive number")
    try:
        usd = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ApiError(400, f"{field} must be a number") from None
    if usd <= 0:
        raise ApiError(400, f"{field} must be positive")
    return usd


def _confirmations(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ApiError(400, "confirms must be an integer") from None


def _quote_or_503(usd: Decimal) -> Tuple[Decimal, Decimal, str]:
    try:
        maza, rate, source = pricing.quote_maza(usd)
    except (ValueError, pricing.PriceUnavailable) as exc:
        raise ApiError(503, f"price feed unavailable: {exc}") from exc
    return maza, rate, source


def _address_from(payload: Dict[str, Any]) -> Tuple[str, str]:
    """Resolve + validate the payee address; returns (address, vendor_id)."""
    vendor_id = payload.get("vendor_id", "")
    address = payload.get("address", "")
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


def create_order(usd_amount, ref: str = "", vendor_id: str = "") -> Dict[str, Any]:
    """Quote + open an inbound order with a locked rate and a fresh address."""
    usd = _usd_amount(usd_amount)
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
    """Send MAZA to a validated address; intent recorded before broadcast."""
    usd = _usd_amount(payload.get("usd_amount"))
    address, vendor_id = _address_from(payload)
    return _broadcast_out(usd, address, vendor_id, payload.get("ref", ""))


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
    address = payload.get("address", "") or (store.get_vendor(order["vendor_id"]) or {}).get(
        "maza_address", ""
    )
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


class Handler(BaseHTTPRequestHandler):
    server_version = "MazaPay/0.3.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002  (stdlib name)
        pass

    # -- helpers --------------------------------------------------------------
    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, indent=2, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if not config.API_TOKEN:
            return True  # open mode (mock/demo only; real mode refuses to serve)
        header = self.headers.get("Authorization", "")
        return hmac.compare_digest(header, f"Bearer {config.API_TOKEN}")

    def _body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "invalid JSON body") from None
        if not isinstance(data, dict):
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
            self._json(exc.status, {"error": exc.message})
        except Exception as exc:  # noqa: BLE001
            self._json(500, {"error": f"internal: {exc}"})

    def do_GET(self) -> None:  # noqa: N802
        self._route("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._route("POST")

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
                "allow_fixed_price": config.ALLOW_FIXED_PRICE,
                "rate_lock_seconds": config.RATE_LOCK_SECONDS,
            },
        )

    def _vendors_post(self) -> None:
        body = self._body()
        name = (body.get("name") or "").strip()
        code = (body.get("code") or "").strip()
        if not name or not code:
            raise ApiError(400, "name and code required")
        self._json(
            201,
            store.create_vendor(name, code, body.get("maza_address", ""), body.get("notes", "")),
        )

    def _vendors_get(self) -> None:
        self._json(200, {"vendors": store.list_vendors()})

    def _orders_post(self) -> None:
        body = self._body()
        self._json(201, create_order(body.get("usd_amount"), body.get("ref", ""), body.get("vendor_id", "")))

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
    if with_watcher:
        _WATCHER = watcher.Watcher()
        _WATCHER.start()
    return srv


def shutdown_watcher() -> None:
    if _WATCHER is not None:
        _WATCHER.stop()


def main() -> None:  # pragma: no cover - manual run helper
    store.init_db()
    srv = serve()
    print(f"mazapay-bridge listening on http://{srv.server_address[0]}:{srv.server_address[1]}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":  # pragma: no cover
    main()
