"""Treasury service — request state machine, approval policy, pool accounting.

Everything here is a *domain* function: it takes the ``user`` dict that
``auth.current_user`` returns (``{id, email, name, role}``) and either returns
plain dicts or raises :class:`TreasuryError` carrying the machine ``code`` and
the HTTP ``status`` the router should emit. No HTTP, no templates.

Pinned rules implemented here (docs/CONTRACT-treasury.md §5-§7):

- role matrix: admin/treasurer/finance_officer prepare/process/submit/cancel;
  **admin may never sign**; executive + council_treasurer sign dual accounts
  only; treasurer signs single accounts only; complete = admin or treasurer.
- state machine: prepared -> processed -> pending_signatures -> authorized ->
  completed | completed_partial, with cancel from the first three states and
  decline from pending_signatures.
- separation of duties: the preparer and the processor of a request may not
  sign it even when their role qualifies.
- caps: ``cap_per_request_usd`` at create/process, ``cap_daily_usd`` re-checked
  at completion against today's completed requests.
- ``stipend_run`` draws on the donor pool, checked against the pool balance.
- ``account.kind='federal_oversight'`` is documentary: every transition up to
  ``authorized`` is allowed, ``complete`` refuses 409
  ``federal_award_documentary_only`` and nothing is broadcast.
- completion claims the request atomically, then calls
  ``mazapay.api.disburse`` once per pending item. One item's exception marks
  that item failed and the run continues; the request ends
  ``completed_partial`` and a later ``complete_request`` retries only items
  that never got a txid.

Transient state: the atomic completion claim moves the request to
``completing`` (not drawn in §6, which only names settled states). It is
visible for the duration of the broadcast loop only, and a stale claim older
than ``COMPLETION_LEASE_SECONDS`` can be re-claimed after a crash.
"""
import datetime
import hashlib
import json
import sqlite3
import time
from decimal import Decimal, DecimalException
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .. import api, config, pricing
from . import store

ROLES = ("admin", "executive", "council_treasurer", "treasurer", "finance_officer", "viewer")

READ_ROLES = ROLES
PREPARE_ROLES = ("admin", "treasurer", "finance_officer")
SIGN_ROLES = ("executive", "council_treasurer", "treasurer")
COMPLETE_ROLES = ("admin", "treasurer")
DONATION_ROLES = ("admin", "treasurer")

KINDS = ("drawdown", "payout", "stipend_run")

STATUSES = (
    "prepared", "processed", "pending_signatures", "authorized", "completing",
    "completed", "completed_partial", "cancelled", "declined",
)

COMPLETION_LEASE_SECONDS = 300
MAX_ITEMS = 200
MAX_PURPOSE = 2000
MAX_NOTE = 500
MAX_ERROR = 500

#: policy -> roles whose approve counts toward quorum
POLICY_ROLES = {
    "single": ("treasurer",),
    "dual": ("executive", "council_treasurer"),
}


class TreasuryError(Exception):
    """Domain refusal. ``code``/``status`` feed the JSON error envelope (§9)."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

    def as_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message}


# --- small helpers ----------------------------------------------------------


def _dec(value: Any) -> Decimal:
    try:
        dec = Decimal(str(value))
    except (DecimalException, ValueError, TypeError):
        raise TreasuryError("invalid_amount", f"not a number: {value!r}", 400) from None
    if not dec.is_finite():
        raise TreasuryError("invalid_amount", f"not a finite number: {value!r}", 400)
    return dec


def _money(value: Any) -> str:
    try:
        return store.to_money(value)
    except ValueError as exc:
        raise TreasuryError("invalid_amount", str(exc), 400) from None


def _maza_text(value: Decimal) -> str:
    """Decimal -> string without forcing 2dp (MAZA granularity is configurable)."""
    text = format(value, "f")
    return text if "." in text else text + ".00"


def _now() -> float:
    return time.time()


def _day_bounds(ts: Optional[float] = None) -> Tuple[float, float]:
    moment = datetime.datetime.fromtimestamp(
        ts if ts is not None else time.time(), datetime.timezone.utc
    )
    start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.timestamp(), (start + datetime.timedelta(days=1)).timestamp()


def _user_id(user: Any) -> str:
    if not isinstance(user, dict) or not user.get("id"):
        raise TreasuryError("unauthenticated", "sign-in required", 401)
    return str(user["id"])


def _role(user: Any) -> str:
    _user_id(user)
    role = user.get("role")
    if role not in ROLES:
        raise TreasuryError("insufficient_role", f"unknown role {role!r}", 403)
    return str(role)


def _require(user: Any, roles: Sequence[str], what: str) -> str:
    role = _role(user)
    if role not in roles:
        raise TreasuryError(
            "insufficient_role", f"role {role!r} may not {what}", 403
        )
    return role


def _read(user: Any) -> str:
    return _require(user, READ_ROLES, "read treasury data")


def _clean_text(value: Any, field: str, *, required: bool = False, limit: int = MAX_NOTE) -> Optional[str]:
    if value is None:
        if required:
            raise TreasuryError(f"invalid_{field}", f"{field} is required", 400)
        return None
    if not isinstance(value, str):
        value = str(value)
    text = value.strip()
    if required and not text:
        raise TreasuryError(f"invalid_{field}", f"{field} is required", 400)
    if len(text) > limit:
        raise TreasuryError(f"invalid_{field}", f"{field} is too long", 400)
    return text or None


def _ref_parts(kind: str, now: float) -> Tuple[str, str]:
    """(counter key, ref format parts) — DR-2027-0142 / HSR-0088."""
    year = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime("%Y")
    if kind == "drawdown":
        return f"DR-{year}", f"DR-{year}"
    if kind == "payout":
        return f"PAY-{year}", f"PAY-{year}"
    return "HSR", "HSR"


def _fmt_ref(stem: str, n: int) -> str:
    return f"{stem}-{n:04d}"


def _account(user: Any, account_id: str) -> Dict[str, Any]:
    acct = store.get_account(account_id) if account_id else None
    if not acct:
        raise TreasuryError("not_found", f"account {account_id!r} not found", 404)
    return acct


def _request_row(request_id: str) -> Dict[str, Any]:
    req = store.get_request_row(request_id) if request_id else None
    if not req:
        raise TreasuryError("not_found", f"request {request_id!r} not found", 404)
    return req


def _hydrate(req: Dict[str, Any]) -> Dict[str, Any]:
    """Request row + items + signatures + audit + account (contract §7)."""
    base = dict(req)
    payload = dict(base)
    payload["request"] = dict(base)
    payload["items"] = store.list_items(base["id"])
    payload["signatures"] = store.list_signatures(base["id"])
    audit = store.audit_rows(object_type="request", object_id=base["id"], limit=200)
    for item in payload["items"]:
        audit.extend(
            store.audit_rows(object_type="request_item", object_id=item["id"], limit=50)
        )
    audit.sort(key=lambda row: row["id"], reverse=True)
    payload["audit"] = audit[:200]
    payload["account"] = store.get_account(base["account_id"])
    return payload


def _cap(account: Dict[str, Any], key: str) -> Optional[Decimal]:
    raw = account.get(key)
    if raw is None or raw == "":
        return None
    return _dec(raw)


def _policy(account: Dict[str, Any]) -> str:
    policy = (account.get("signature_policy") or "dual").strip().lower()
    if policy not in POLICY_ROLES:
        raise TreasuryError("invalid_policy", f"unknown signature policy {policy!r}", 409)
    return policy


def _counts(policy: str, role: str) -> bool:
    return role in POLICY_ROLES[policy]


def _quorum(policy: str, approved_roles: Sequence[str]) -> bool:
    required = set(POLICY_ROLES[policy])
    return required.issubset(set(approved_roles))


def _approved_roles(conn: sqlite3.Connection, request_id: str, policy: str) -> List[str]:
    rows = conn.execute(
        "SELECT role_at_signing FROM tr_signatures WHERE request_id=? AND decision='approve'",
        (request_id,),
    ).fetchall()
    roles = []
    for row in rows:
        role = row["role_at_signing"]
        if _counts(policy, role) and role not in roles:
            roles.append(role)
    return roles


def _check_cap_per_request(account: Dict[str, Any], total: Decimal) -> None:
    cap = _cap(account, "cap_per_request_usd")
    if cap is not None and total > cap:
        raise TreasuryError(
            "cap_per_request",
            f"request total {total} exceeds the per-request cap {cap}",
            409,
        )


def _pool_balance_maza(
    conn: sqlite3.Connection, *, exclude_request_id: Optional[str] = None
) -> Decimal:
    donated = Decimal("0")
    for row in conn.execute("SELECT maza_amount FROM tr_pool_donations"):
        donated += _dec(row["maza_amount"] or "0")
    paid = Decimal("0")
    for row in conn.execute(
        "SELECT i.maza_amount FROM tr_request_items i"
        " JOIN tr_requests r ON r.id = i.request_id"
        " WHERE r.kind='stipend_run' AND i.status='sent'"
    ):
        paid += _dec(row["maza_amount"] or "0")
    # Holds taken by in-flight completions are not spendable either. A
    # reclaiming run must EXCLUDE its own hold: it is about to replace it with
    # the remaining unsent amount, and counting it would make the request
    # refuse itself after a crash.
    held = Decimal("0")
    sql = (
        "SELECT request_id, maza_amount FROM tr_reservations"
        " WHERE settled_ts IS NULL AND released_ts IS NULL"
    )
    args: tuple = ()
    if exclude_request_id:
        sql += " AND request_id != ?"
        args = (exclude_request_id,)
    for row in conn.execute(sql, args):
        held += _dec(row["maza_amount"] or "0")
    return donated - paid - held


# --- reads ------------------------------------------------------------------


def list_accounts(user) -> List[Dict[str, Any]]:
    _read(user)
    return store.list_accounts()


def get_account(user, account_id) -> Dict[str, Any]:
    _read(user)
    acct = store.get_account(account_id)
    if not acct:
        raise TreasuryError("not_found", f"account {account_id!r} not found", 404)
    return acct


def get_request(user, request_id) -> Dict[str, Any]:
    _read(user)
    return _hydrate(_request_row(request_id))


def list_requests(
    user, *, status=None, account_id=None, limit: int = 100
) -> List[Dict[str, Any]]:
    _read(user)
    if status is not None and status not in STATUSES:
        raise TreasuryError("invalid_state", f"unknown status {status!r}", 400)
    return store.list_request_rows(status=status, account_id=account_id, limit=limit)


def list_stipends(user, *, limit: int = 100) -> List[Dict[str, Any]]:
    _read(user)
    return store.stipend_items(limit=limit)


def audit_rows(user, *, object_type=None, object_id=None, limit: int = 200) -> List[Dict[str, Any]]:
    _read(user)
    return store.audit_rows(object_type=object_type, object_id=object_id, limit=limit)


# --- create / process / submit ---------------------------------------------


def create_request(
    user, *, account_id, kind, purpose, items, award_ref=None, note=None
) -> Dict[str, Any]:
    uid = _user_id(user)
    _require(user, PREPARE_ROLES, "create requests")

    if kind not in KINDS:
        raise TreasuryError("invalid_kind", f"unknown request kind {kind!r}", 400)
    purpose_text = _clean_text(purpose, "purpose", required=True, limit=MAX_PURPOSE)
    note_text = _clean_text(note, "note", limit=MAX_NOTE)
    if not isinstance(items, (list, tuple)) or not items:
        raise TreasuryError("no_items", "a request needs at least one item", 400)
    if len(items) > MAX_ITEMS:
        raise TreasuryError("too_many_items", f"at most {MAX_ITEMS} items per request", 400)

    acct = _account(user, account_id)
    if acct.get("status") != "open":
        raise TreasuryError("account_closed", f"account {acct['code']} is not open", 409)

    prepared_items: List[Dict[str, Any]] = []
    total = Decimal("0")
    for raw in items:
        if not isinstance(raw, dict):
            raise TreasuryError("invalid_item", "each item must be an object", 400)
        payee_id = raw.get("payee_id") or None
        payee = store.get_payee(payee_id) if payee_id else None
        if payee_id and not payee:
            raise TreasuryError("not_found", f"payee {payee_id!r} not found", 404)
        name = _clean_text(raw.get("payee_name") or (payee or {}).get("name"), "payee_name", required=True, limit=200)
        address = _clean_text(
            raw.get("payee_address") or (payee or {}).get("maza_address"),
            "payee_address",
            required=True,
            limit=200,
        )
        category = raw.get("category") or (payee or {}).get("category")
        amount = _dec(raw.get("usd_amount"))
        if amount <= 0:
            raise TreasuryError("invalid_amount", "item usd_amount must be positive", 400)
        amount_text = _money(amount)
        total += Decimal(amount_text)
        prepared_items.append(
            {
                "payee_id": payee["id"] if payee else None,
                "payee_name": name,
                "payee_address": address,
                "category": category,
                "usd_amount": amount_text,
            }
        )
    total_text = _money(total)
    _check_cap_per_request(acct, Decimal(total_text))

    if acct.get("kind") == "federal_oversight":
        award_ref = award_ref or acct.get("award_ref")

    now = _now()
    key, stem = _ref_parts(kind, now)
    request_id = store._new_id("req")
    with store.write_db() as conn:
        ref = ""
        for _attempt in range(8):
            ref = _fmt_ref(stem, store.next_counter(conn, key))
            try:
                store.insert_request(
                    conn,
                    {
                        "id": request_id,
                        "ref": ref,
                        "account_id": acct["id"],
                        "kind": kind,
                        "purpose": purpose_text,
                        "award_ref": award_ref,
                        "note": note_text,
                        "status": "prepared",
                        "usd_total": total_text,
                        "prepared_by": uid,
                        "prepared_ts": now,
                        "created_ts": now,
                    },
                )
                break
            except sqlite3.IntegrityError:
                if _attempt == 7:
                    raise TreasuryError(
                        "ref_conflict", "could not allocate a unique request ref", 409
                    ) from None
                continue
        for item in prepared_items:
            store.insert_item(
                conn,
                {
                    "id": store._new_id("item"),
                    "request_id": request_id,
                    "payee_id": item["payee_id"],
                    "payee_name": item["payee_name"],
                    "payee_address": item["payee_address"],
                    "category": item["category"],
                    "usd_amount": item["usd_amount"],
                    "status": "pending",
                    "created_ts": now,
                },
            )
        store.append_audit(
            conn,
            actor_user_id=uid,
            action="request_created",
            object_type="request",
            object_id=request_id,
            detail={
                "ref": ref,
                "account": acct["code"],
                "kind": kind,
                "usd_total": total_text,
                "items": len(prepared_items),
            },
        )
    return _hydrate(_request_row(request_id))


def process_request(user, request_id) -> Dict[str, Any]:
    uid = _user_id(user)
    _require(user, PREPARE_ROLES, "process requests")
    req = _request_row(request_id)
    acct = _account(user, req["account_id"])
    if req["status"] != "prepared":
        raise TreasuryError(
            "invalid_state", f"request is {req['status']}, not prepared", 409
        )
    total = _dec(req["usd_total"])
    _check_cap_per_request(acct, total)

    # Quote outside the write transaction: pricing may do network I/O and must
    # never hold the sqlite write lock. One rate for the whole request, each
    # item priced with that same locked rate via the checkout's own call.
    try:
        _maza_total, rate, source = pricing.quote_maza(total)
        item_quotes = [
            (item["id"], str(pricing.quote_maza(_dec(item["usd_amount"]), price=rate)[0]))
            for item in store.list_items(request_id)
        ]
    except ValueError as exc:
        raise TreasuryError("quote_unavailable", str(exc), 503) from None
    maza_total = sum((Decimal(m) for _iid, m in item_quotes), Decimal("0"))
    maza_total_text = f"{maza_total}"

    now = _now()
    expires = now + float(getattr(config, "RATE_LOCK_SECONDS", 900))
    with store.write_db() as conn:
        won = store.update_request(
            conn,
            request_id,
            {
                "status": "processed",
                "maza_total": maza_total_text,
                "rate": str(rate),
                "rate_source": source,
                "rate_locked_ts": now,
                "rate_expires_ts": expires,
                "processed_by": uid,
                "processed_ts": now,
            },
            from_status="prepared",
        )
        if not won:
            raise TreasuryError("invalid_state", "request left prepared concurrently", 409)
        for item_id, maza in item_quotes:
            store.update_item(conn, item_id, {"maza_amount": maza})
        store.append_audit(
            conn,
            actor_user_id=uid,
            action="request_processed",
            object_type="request",
            object_id=request_id,
            detail={
                "rate": str(rate),
                "rate_source": source,
                "maza_total": maza_total_text,
                "rate_expires_ts": expires,
            },
        )
    return _hydrate(_request_row(request_id))


def submit_request(user, request_id) -> Dict[str, Any]:
    uid = _user_id(user)
    _require(user, PREPARE_ROLES, "submit requests")
    req = _request_row(request_id)
    if req["status"] != "processed":
        raise TreasuryError(
            "invalid_state", f"request is {req['status']}, not processed", 409
        )
    items = store.list_items(request_id)
    if not items:
        raise TreasuryError("no_items", "a request needs at least one item", 400)
    now = _now()
    expires = req.get("rate_expires_ts")
    if not expires or float(expires) <= now:
        raise TreasuryError("quote_expired", "the locked quote has expired", 409)
    with store.write_db() as conn:
        won = store.update_request(
            conn,
            request_id,
            {"status": "pending_signatures", "submitted_ts": now},
            from_status="processed",
        )
        if not won:
            raise TreasuryError("invalid_state", "request left processed concurrently", 409)
        store.append_audit(
            conn,
            actor_user_id=uid,
            action="request_submitted",
            object_type="request",
            object_id=request_id,
            detail={"items": len(items)},
        )
    return _hydrate(_request_row(request_id))


# --- signing ----------------------------------------------------------------


def _signature_hash(
    request_id: str,
    user_id: str,
    role: str,
    decision: str,
    note: Optional[str],
    signed_ts: float,
) -> str:
    payload = {
        "request_id": request_id,
        "user_id": user_id,
        "role_at_signing": role,
        "decision": decision,
        "note": note,
        "signed_ts": signed_ts,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def sign_request(user, request_id, *, decision, note=None) -> Dict[str, Any]:
    uid = _user_id(user)
    role = _require(user, SIGN_ROLES, "sign requests")
    if decision not in ("approve", "decline"):
        raise TreasuryError("invalid_decision", "decision must be approve or decline", 400)
    note_text = _clean_text(note, "note", limit=MAX_NOTE)
    if decision == "decline" and not note_text:
        raise TreasuryError(
            "decline_reason_required", "a decline needs a reason", 400
        )

    now = _now()
    with store.write_db() as conn:
        req = store.get_request_row(request_id, conn)
        if not req:
            raise TreasuryError("not_found", f"request {request_id!r} not found", 404)
        if req["status"] != "pending_signatures":
            raise TreasuryError(
                "invalid_state", f"request is {req['status']}, not pending_signatures", 409
            )
        acct = store.get_account(req["account_id"])
        if not acct:
            raise TreasuryError("not_found", "request account not found", 404)
        policy = _policy(acct)

        if uid == req["prepared_by"] or uid == req.get("processed_by"):
            raise TreasuryError(
                "separation_of_duties",
                "the preparer or processor of a request may not sign it",
                409,
            )
        if store.get_signature(request_id, uid, conn):
            raise TreasuryError("already_signed", "this user already signed", 409)

        counted = _counts(policy, role)
        record_hash = _signature_hash(request_id, uid, role, decision, note_text, now)
        sig_id = store._new_id("sig")
        try:
            store.insert_signature(
                conn,
                {
                    "id": sig_id,
                    "request_id": request_id,
                    "user_id": uid,
                    "role_at_signing": role,
                    "decision": decision,
                    "note": note_text,
                    "signed_ts": now,
                    "record_hash": record_hash,
                },
            )
        except sqlite3.IntegrityError:
            raise TreasuryError("already_signed", "this user already signed", 409) from None

        store.append_audit(
            conn,
            actor_user_id=uid,
            action="signature_recorded",
            object_type="request",
            object_id=request_id,
            detail={
                "decision": decision,
                "role": role,
                "policy": policy,
                "counted": counted,
                "note": note_text,
            },
        )

        effective = "pending_signatures"
        if counted and decision == "decline":
            store.update_request(
                conn,
                request_id,
                {"status": "declined", "decline_reason": note_text},
                from_status="pending_signatures",
            )
            effective = "declined"
            store.append_audit(
                conn,
                actor_user_id=uid,
                action="request_declined",
                object_type="request",
                object_id=request_id,
                detail={"reason": note_text, "role": role},
            )
        elif counted:
            approved = _approved_roles(conn, request_id, policy)
            if _quorum(policy, approved):
                store.update_request(
                    conn,
                    request_id,
                    {"status": "authorized", "authorized_ts": now},
                    from_status="pending_signatures",
                )
                effective = "authorized"
                store.append_audit(
                    conn,
                    actor_user_id=uid,
                    action="request_authorized",
                    object_type="request",
                    object_id=request_id,
                    detail={"policy": policy, "approved_roles": approved},
                )

    hydrated = _hydrate(_request_row(request_id))
    hydrated["counted"] = counted
    hydrated["signature"] = next(
        (s for s in hydrated["signatures"] if s["id"] == sig_id), None
    )
    approved_roles: List[str] = []
    for sig in hydrated["signatures"]:
        if sig["decision"] == "approve" and _counts(policy, sig["role_at_signing"]):
            if sig["role_at_signing"] not in approved_roles:
                approved_roles.append(sig["role_at_signing"])
    hydrated["quorum"] = {
        "policy": policy,
        "required": list(POLICY_ROLES[policy]),
        "approved": approved_roles,
        "reached": hydrated["status"] == "authorized",
    }
    hydrated["effective_status"] = effective
    return hydrated


# --- cancel -----------------------------------------------------------------


def cancel_request(user, request_id, *, reason=None) -> Dict[str, Any]:
    uid = _user_id(user)
    _require(user, PREPARE_ROLES, "cancel requests")
    reason_text = _clean_text(reason, "reason", limit=MAX_NOTE)
    now = _now()
    with store.write_db() as conn:
        req = store.get_request_row(request_id, conn)
        if not req:
            raise TreasuryError("not_found", f"request {request_id!r} not found", 404)
        won = False
        for status in ("prepared", "processed", "pending_signatures"):
            if store.update_request(
                conn, request_id, {"status": "cancelled"}, from_status=status
            ):
                won = True
                break
        if not won:
            raise TreasuryError(
                "invalid_state", f"request is {req['status']} and cannot be cancelled", 409
            )
        store.append_audit(
            conn,
            actor_user_id=uid,
            action="request_cancelled",
            object_type="request",
            object_id=request_id,
            detail={"from": req["status"], "reason": reason_text, "ts": now},
        )
    return _hydrate(_request_row(request_id))


# --- completion -------------------------------------------------------------

# Crash safety rests on two things the bridge already gives us:
#   * an outbound order is durably recorded (with a nonce) BEFORE broadcast;
#   * its ``ref`` is ours to choose.
# So the treasury writes a STABLE ref onto the item before the first broadcast
# and, on any later attempt, adopts an existing order with that ref instead of
# paying again. Cap and pool claims are reserved atomically with the completion
# claim, so distinct requests cannot both pass the checks.


def _bridge_ref(req, item) -> str:
    ref = "treasury:%s:%s" % (req["ref"], str(item["id"])[-8:])
    return ref[:120]


def _adopt_order(req, item, order, user_id: str) -> bool:
    """Record an existing bridge order as this item's payment (never re-send)."""
    status = order.get("status")
    txid = order.get("txid")
    if txid and status == "sent":
        with store.write_db() as conn:
            _record_item_sent(conn, req, item, order, txid, user_id)
        return True
    if status in ("send_failed",) and not txid:
        try:
            retried = api.retry_payment(order["id"])
        except Exception as exc:  # noqa: BLE001 - one item must not stop the run
            with store.write_db() as conn:
                _record_item_failure(
                    conn, req, item, "send_uncertain: retry failed: %s" % (exc,), user_id
                )
            return True
        if isinstance(retried, dict) and retried.get("txid") and retried.get("status") == "sent":
            with store.write_db() as conn:
                _record_item_sent(conn, req, item, retried, retried["txid"], user_id)
            return True
        with store.write_db() as conn:
            _record_item_failure(
                conn, req, item, "send_uncertain: retry did not settle", user_id
            )
        return True
    if status in ("pending_send", "send_uncertain"):
        try:
            reconciled = api.reconcile_payment(order["id"])
        except Exception as exc:  # noqa: BLE001
            with store.write_db() as conn:
                _record_item_failure(
                    conn,
                    req,
                    item,
                    "send_uncertain: reconcile unavailable: %s" % (exc,),
                    user_id,
                )
            return True
        if isinstance(reconciled, dict) and reconciled.get("txid") and reconciled.get("status") == "sent":
            with store.write_db() as conn:
                _record_item_sent(
                    conn, req, item, reconciled, reconciled["txid"], user_id
                )
            return True
        with store.write_db() as conn:
            _record_item_failure(
                conn,
                req,
                item,
                "send_uncertain: bridge order %s is %s"
                % (order.get("id"), (reconciled or {}).get("status") or status),
                user_id,
            )
        return True
    with store.write_db() as conn:
        _record_item_failure(
            conn,
            req,
            item,
            "send_uncertain: bridge order %s is %s" % (order.get("id"), status),
            user_id,
        )
    return True


def _claim_completion(
    user_id: str, request_id: str, conn: sqlite3.Connection
) -> Optional[Dict[str, Any]]:
    """Atomically claim completion AND reserve the spend.

    Runs inside BEGIN IMMEDIATE: the cap/pool checks below see every other
    in-flight reservation, so two different authorized requests cannot both be
    allowed to spend the same headroom.
    """
    req = store.get_request_row(request_id, conn)
    if not req:
        return None
    status = req["status"]
    if status in ("authorized", "completed_partial"):
        pass
    elif status == "completing":
        row = conn.execute(
            "SELECT ts FROM tr_audit WHERE object_type='request' AND object_id=?"
            " AND action='completion_started' ORDER BY id DESC LIMIT 1",
            (request_id,),
        ).fetchone()
        stale = row is None or (time.time() - float(row["ts"])) > COMPLETION_LEASE_SECONDS
        if not stale:
            return None
    else:
        return None

    acct_row = conn.execute(
        "SELECT * FROM tr_accounts WHERE id=?", (req["account_id"],)
    ).fetchone()
    acct = dict(acct_row) if acct_row else {}
    items = store.list_items(request_id, conn=conn)
    total = _dec(req["usd_total"])

    # -- daily cap, reservations included ----------------------------------
    start, end = _day_bounds()
    spent = sum(
        (
            _dec(r["usd_total"])
            for r in store.completed_requests_for_day(
                acct["id"], start, end, exclude_request_id=request_id
            )
        ),
        Decimal("0"),
    )
    held = sum(
        (
            _dec(r["usd_amount"])
            for r in store.active_reservations(
                acct["id"], start, end, exclude_request_id=request_id, conn=conn
            )
        ),
        Decimal("0"),
    )
    cap_daily = _cap(acct, "cap_daily_usd")
    if cap_daily is not None and spent + held + total > cap_daily:
        raise TreasuryError(
            "cap_daily",
            "today's completed %s plus in-flight %s plus this request %s exceeds"
            " the daily cap %s" % (spent, held, total, cap_daily),
            409,
        )

    # -- donor pool, reservations included ---------------------------------
    outstanding = Decimal("0")
    if req["kind"] == "stipend_run":
        outstanding = sum(
            (
                _dec(i.get("maza_amount") or "0")
                for i in items
                if not i.get("txid") and i.get("status") != "sent"
            ),
            Decimal("0"),
        )
        balance = _pool_balance_maza(conn, exclude_request_id=request_id)
        if outstanding > balance:
            raise TreasuryError(
                "pool_balance",
                "stipend run needs %s MAZA but the pool holds %s" % (outstanding, balance),
                409,
            )

    if not store.update_request(conn, request_id, {"status": "completing"}, from_status=status):
        return None
    store.create_reservation(
        conn,
        request_id=request_id,
        account_id=acct["id"],
        usd_amount=total,
        maza_amount=outstanding if outstanding else None,
    )
    store.append_audit(
        conn,
        actor_user_id=user_id,
        action="completion_started",
        object_type="request",
        object_id=request_id,
        detail={"from": status, "reserved_usd": str(total), "reserved_maza": str(outstanding)},
    )
    return req


def _record_item_sent(conn, req, item, order, txid: str, user_id: str) -> None:
    won = store.update_item(
        conn,
        item["id"],
        {
            "status": "sent",
            "payment_id": order.get("id"),
            "txid": txid,
            "error": None,
            "sent_ts": time.time(),
        },
        from_status=item["status"],
    )
    store.append_audit(
        conn,
        actor_user_id=user_id,
        action="item_sent" if won else "item_sent_duplicate_guard",
        object_type="request_item",
        object_id=item["id"],
        detail={
            "request_id": req["id"],
            "ref": req["ref"],
            "payment_id": order.get("id"),
            "txid": txid,
            "usd": item["usd_amount"],
            "maza": item.get("maza_amount"),
        },
    )


def _record_item_failure(conn, req, item, message: str, user_id: str) -> None:
    store.update_item(
        conn,
        item["id"],
        {"status": "failed", "error": message[:MAX_ERROR]},
        from_status=item["status"],
    )
    store.append_audit(
        conn,
        actor_user_id=user_id,
        action="item_failed",
        object_type="request_item",
        object_id=item["id"],
        detail={"request_id": req["id"], "ref": req["ref"], "error": message[:MAX_ERROR]},
    )


def complete_request(user, request_id) -> Dict[str, Any]:
    uid = _user_id(user)
    _require(user, COMPLETE_ROLES, "complete requests")
    req = _request_row(request_id)
    acct = _account(user, req["account_id"])

    # Federal award accounts are documentary: nothing is ever broadcast.
    if acct.get("kind") == "federal_oversight":
        raise TreasuryError(
            "federal_award_documentary_only",
            "federal award funds are documentary and are never disbursed",
            409,
        )
    if acct.get("status") != "open":
        raise TreasuryError("account_closed", "account %s is not open" % acct["code"], 409)
    if req["status"] not in ("authorized", "completed_partial", "completing"):
        raise TreasuryError(
            "invalid_state", "request is %s, not authorized" % req["status"], 409
        )

    with store.write_db() as conn:
        claimed = _claim_completion(uid, request_id, conn)
    if not claimed:
        existing = store.get_request_row(request_id) or req
        if existing["status"] == "completing":
            raise TreasuryError(
                "completion_in_progress", "another completion run holds this request", 409
            )
        raise TreasuryError(
            "invalid_state", "request is %s, not authorized" % existing["status"], 409
        )

    items = store.list_items(request_id)
    sent_before = sum(1 for i in items if i.get("txid"))
    for item in items:
        # Never broadcast twice for an item that already has a txid, and do not
        # re-attempt a send the bridge reported as *uncertain*.
        if item.get("txid") or item.get("status") == "sent":
            continue
        if item.get("status") == "failed" and (item.get("error") or "").startswith("send_uncertain"):
            continue

        bridge_ref = item.get("broadcast_ref") or _bridge_ref(req, item)
        if not item.get("broadcast_ref"):
            with store.write_db() as conn:
                store.update_item(
                    conn, item["id"], {"broadcast_ref": bridge_ref},
                    from_status=item["status"],
                )

        # Recovery: an order for this stable ref means a previous attempt
        # already reached (or may have reached) the wallet.
        existing = store.find_outbound_by_ref(bridge_ref)
        if existing:
            _adopt_order(req, item, existing, uid)
            continue

        try:
            order = api.disburse(
                {
                    "address": item["payee_address"],
                    "usd_amount": item["usd_amount"],
                    "ref": bridge_ref,
                }
            )
        except Exception as exc:  # one item failing must not stop the run
            # The bridge writes its intent BEFORE broadcasting, so an exception
            # may still leave an order behind: adopt it rather than re-send.
            existing = store.find_outbound_by_ref(bridge_ref)
            if existing:
                _adopt_order(req, item, existing, uid)
                continue
            with store.write_db() as conn:
                _record_item_failure(conn, req, item, str(exc) or exc.__class__.__name__, uid)
            continue

        txid = order.get("txid") if isinstance(order, dict) else None
        status = order.get("status") if isinstance(order, dict) else None
        if txid and status == "sent":
            with store.write_db() as conn:
                _record_item_sent(conn, req, item, order, txid, uid)
        else:
            detail = ""
            if isinstance(order, dict):
                detail = str(order.get("send_error") or status or "no txid")
            with store.write_db() as conn:
                _record_item_failure(
                    conn, req, item, "send_uncertain: %s" % detail.strip(), uid
                )

    final_items = store.list_items(request_id)
    failed = [i for i in final_items if i["status"] == "failed" or not i.get("txid")]
    final_status = "completed" if not failed else "completed_partial"
    now = _now()
    with store.write_db() as conn:
        won = store.update_request(
            conn,
            request_id,
            {"status": final_status, "completed_ts": now},
            from_status="completing",
        )
        sent_now = sum(1 for i in final_items if i.get("txid"))
        store.settle_reservation(conn, request_id, released=sent_now == 0)
        store.append_audit(
            conn,
            actor_user_id=uid,
            action="request_completed" if final_status == "completed" else "request_completed_partial",
            object_type="request",
            object_id=request_id,
            detail={
                "status": final_status,
                "sent": sent_now,
                "failed": sum(1 for i in final_items if i["status"] == "failed"),
                "items": len(final_items),
                "claimed": won,
                "sent_before_this_run": sent_before,
            },
        )
    return _hydrate(_request_row(request_id))


# --- pool -------------------------------------------------------------------


def record_donation(user, *, donor_label, usd_amount, txid=None, note=None) -> Dict[str, Any]:
    uid = _user_id(user)
    _require(user, DONATION_ROLES, "record donations")
    label = _clean_text(donor_label, "donor_label", required=True, limit=200)
    usd = _dec(usd_amount)
    if usd <= 0:
        raise TreasuryError("invalid_amount", "usd_amount must be positive", 400)
    usd_text = _money(usd)
    txid_text = _clean_text(txid, "txid", limit=200)
    note_text = _clean_text(note, "note", limit=MAX_NOTE)
    try:
        maza, _rate, _source = pricing.quote_maza(Decimal(usd_text))
    except ValueError as exc:
        raise TreasuryError("quote_unavailable", str(exc), 503) from None

    donation_id = store._new_id("don")
    now = _now()
    with store.write_db() as conn:
        store.insert_donation(
            conn,
            {
                "id": donation_id,
                "donor_label": label,
                "usd_amount": usd_text,
                "maza_amount": str(maza),
                "txid": txid_text,
                "note": note_text,
                "received_ts": now,
                "recorded_by": uid,
            },
        )
        store.append_audit(
            conn,
            actor_user_id=uid,
            action="donation_recorded",
            object_type="donation",
            object_id=donation_id,
            detail={"donor": label, "usd": usd_text, "maza": str(maza), "txid": txid_text},
        )
    return store.get_donation(donation_id) or {}


def pool_summary(user) -> Dict[str, Any]:
    _read(user)
    now = _now()
    month = datetime.datetime.fromtimestamp(now, datetime.timezone.utc)
    month_start = month.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    next_month = (month_start + datetime.timedelta(days=32)).replace(day=1)
    week_start = now - 7 * 86400

    with store.db() as conn:
        donated_maza = Decimal("0")
        donated_usd = Decimal("0")
        donors = set()
        for row in conn.execute("SELECT donor_label, usd_amount, maza_amount FROM tr_pool_donations"):
            donated_usd += _dec(row["usd_amount"])
            donated_maza += _dec(row["maza_amount"] or "0")
            donors.add(row["donor_label"])

        paid_maza = Decimal("0")
        paid_usd = Decimal("0")
        paid_week = Decimal("0")
        categories: Dict[str, Decimal] = {}
        for row in conn.execute(
            "SELECT i.usd_amount, i.maza_amount, i.category, i.sent_ts"
            " FROM tr_request_items i JOIN tr_requests r ON r.id = i.request_id"
            " WHERE r.kind='stipend_run' AND i.status='sent'"
        ):
            item_usd = _dec(row["usd_amount"])
            paid_usd += item_usd
            paid_maza += _dec(row["maza_amount"] or "0")
            if row["sent_ts"] and float(row["sent_ts"]) >= week_start:
                paid_week += item_usd
            key = row["category"] or "uncategorized"
            categories[key] = categories.get(key, Decimal("0")) + item_usd

        donated_month = sum(
            (_dec(r["usd_amount"]) for r in store.donations_between(month_start.timestamp(), next_month.timestamp())),
            Decimal("0"),
        )

    return {
        "balance_maza": _maza_text(donated_maza - paid_maza),
        "balance_usd": _money(donated_usd - paid_usd),
        "donated_month_usd": _money(donated_month),
        "paid_week_usd": _money(paid_week),
        "donors": len(donors),
        "categories": [
            {"category": name, "usd": _money(value)}
            for name, value in sorted(categories.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        "donated_total_usd": _money(donated_usd),
        "paid_total_usd": _money(paid_usd),
    }
