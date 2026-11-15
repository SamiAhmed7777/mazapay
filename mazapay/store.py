"""SQLite store: vendors, orders/payments, audit trail.

Every state transition is appended to the audit table with a timestamp and
a JSON detail blob — the audit trail auditors (and the MIP reconciliation
step) will ask for. The audit table is enforced append-only at the database
level: BEFORE UPDATE / BEFORE DELETE triggers ABORT any mutation attempt,
so "append-only" is a property of the schema, not of caller discipline.

Orders carry the fields needed for the safety workflows:

- exported_ts     — MIP export idempotency (an order is exported to an
                    accounting session once; re-exports skip it)
- nonce           — per-disbursement unique token, embedded in the chain
                    comment so reconcile/retry can match a broadcast to its
                    intent row and duplicate sends are structurally refused
"""
import contextlib
import json
import sqlite3
import time
import uuid
from typing import Any, Dict, List, Optional

from . import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS vendors (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    code TEXT NOT NULL UNIQUE,
    maza_address TEXT,
    notes TEXT,
    created_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,             -- 'in' (receive) | 'out' (disburse)
    vendor_id TEXT,
    ref TEXT,
    usd_amount REAL NOT NULL,
    maza_amount REAL,
    rate REAL,
    rate_source TEXT,
    address TEXT,
    status TEXT NOT NULL,
    txid TEXT,
    confirmations INTEGER DEFAULT 0,
    created_ts REAL NOT NULL,
    expires_ts REAL,
    paid_ts REAL,
    deposit_alerted_ts REAL,         -- deposit alert DELIVERED (marked after dispatch)
    confirm_alerted_ts REAL,         -- confirm alert armed atomically with the paid flip
    exported_ts REAL,
    nonce TEXT,
    export_id TEXT,
    export_state TEXT,               -- NULL | 'claimed' | 'written'
    export_lease_ts REAL,            -- claim time; stale claims are adoptable
    parent_order_id TEXT             -- links a refund payment to its late order
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_nonce
    ON orders(nonce) WHERE nonce IS NOT NULL;
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    entity TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event TEXT NOT NULL,
    detail TEXT
);
-- Append-only enforcement at the schema level: any UPDATE or DELETE on the
-- audit table is ABORTed by the database itself.
CREATE TRIGGER IF NOT EXISTS audit_block_update BEFORE UPDATE ON audit
BEGIN
    SELECT RAISE(ABORT, 'audit is append-only: UPDATE refused');
END;
CREATE TRIGGER IF NOT EXISTS audit_block_delete BEFORE DELETE ON audit
BEGIN
    SELECT RAISE(ABORT, 'audit is append-only: DELETE refused');
END;
"""


def connect(db_path: Optional[str] = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@contextlib.contextmanager
def db(db_path: Optional[str] = None):
    """Connection that COMMITS on success and always CLOSES.

    `with sqlite3.Connection` alone only manages the transaction — it never
    closes the handle, which leaks connections across a long-running server.
    """
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after v0.1 to databases created earlier."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(orders)")}
    for col, decl in (
        ("exported_ts", "REAL"),
        ("nonce", "TEXT"),
        ("export_id", "TEXT"),
        ("export_state", "TEXT"),
        ("export_lease_ts", "REAL"),
        ("parent_order_id", "TEXT"),
        ("deposit_alerted_ts", "REAL"),
        ("confirm_alerted_ts", "REAL"),
    ):
        if col not in cols:
            conn.execute(f"ALTER TABLE orders ADD COLUMN {col} {decl}")


def init_db(db_path: Optional[str] = None) -> None:
    with db(db_path) as conn:
        conn.executescript(_SCHEMA)
        _migrate(conn)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def append_audit(
    conn: sqlite3.Connection,
    entity: str,
    entity_id: str,
    event: str,
    detail: Optional[Dict[str, Any]] = None,
) -> None:
    conn.execute(
        "INSERT INTO audit (ts, entity, entity_id, event, detail) VALUES (?,?,?,?,?)",
        (time.time(), entity, entity_id, event, json.dumps(detail or {}, sort_keys=True)),
    )


# --- vendors ----------------------------------------------------------------


def create_vendor(
    name: str, code: str, maza_address: str = "", notes: str = ""
) -> Dict[str, Any]:
    vid = _new_id("ven")
    now = time.time()
    with db() as conn:
        conn.execute(
            "INSERT INTO vendors (id, name, code, maza_address, notes, created_ts)"
            " VALUES (?,?,?,?,?,?)",
            (vid, name, code, maza_address, notes, now),
        )
        append_audit(conn, "vendor", vid, "created", {"name": name, "code": code})
    return {"id": vid, "name": name, "code": code, "maza_address": maza_address}


def get_vendor(vendor_id: str) -> Optional[Dict[str, Any]]:
    with db() as conn:
        row = conn.execute("SELECT * FROM vendors WHERE id=?", (vendor_id,)).fetchone()
    return dict(row) if row else None


def list_vendors() -> List[Dict[str, Any]]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM vendors ORDER BY created_ts").fetchall()
    return [dict(r) for r in rows]


# --- orders / payments -------------------------------------------------------


def create_order(
    kind: str,
    usd_amount,
    maza_amount,
    rate,
    rate_source: str,
    vendor_id: str = "",
    ref: str = "",
    address: str = "",
    initial_status: Optional[str] = None,
    nonce: Optional[str] = None,
    parent_order_id: Optional[str] = None,
) -> Dict[str, Any]:
    oid = _new_id("ord" if kind == "in" else "pay")
    now = time.time()
    if initial_status:
        status = initial_status
    else:
        status = "quoted" if kind == "in" else "pending_send"
    expires = now + config.RATE_LOCK_SECONDS if kind == "in" else None
    with db() as conn:
        conn.execute(
            "INSERT INTO orders (id, kind, vendor_id, ref, usd_amount, maza_amount,"
            " rate, rate_source, address, status, created_ts, expires_ts, nonce,"
            " parent_order_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                oid,
                kind,
                vendor_id,
                ref,
                float(usd_amount),
                float(maza_amount) if maza_amount is not None else None,
                float(rate) if rate is not None else None,
                rate_source,
                address,
                status,
                now,
                expires,
                nonce,
                parent_order_id,
            ),
        )
        append_audit(
            conn,
            "order",
            oid,
            "created",
            {
                "kind": kind,
                "usd_amount": float(usd_amount),
                "maza_amount": float(maza_amount) if maza_amount is not None else None,
                "rate": float(rate) if rate is not None else None,
                "rate_source": rate_source,
                "status": status,
            },
        )
    created_ts = now
    result = {
        "id": oid,
        "kind": kind,
        "vendor_id": vendor_id,
        "ref": ref,
        "usd_amount": float(usd_amount),
        "maza_amount": float(maza_amount) if maza_amount is not None else None,
        "rate": float(rate) if rate is not None else None,
        "rate_source": rate_source,
        "address": address,
        "status": status,
        "txid": None,
        "confirmations": 0,
        "created_ts": created_ts,
        "expires_ts": expires,
        "paid_ts": None,
        "exported_ts": None,
        "nonce": nonce,
        "parent_order_id": parent_order_id,
    }
    return result


def get_order(order_id: str) -> Optional[Dict[str, Any]]:
    with db() as conn:
        row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    return dict(row) if row else None


def list_orders(kind: Optional[str] = None, status: Optional[str] = None) -> List[Dict[str, Any]]:
    q = "SELECT * FROM orders WHERE 1=1"
    args: List[Any] = []
    if kind:
        q += " AND kind=?"
        args.append(kind)
    if status:
        q += " AND status=?"
        args.append(status)
    q += " ORDER BY created_ts"
    with db() as conn:
        rows = conn.execute(q, args).fetchall()
    return [dict(r) for r in rows]


def update_order(order_id: str, fields: Dict[str, Any], event: str) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with db() as conn:
        conn.execute(
            f"UPDATE orders SET {cols} WHERE id=?", (*fields.values(), order_id)
        )
        append_audit(conn, "order", order_id, event, fields)


def transition(
    order_id: str,
    from_status: str,
    to_status: str,
    fields: Optional[Dict[str, Any]] = None,
    event: str = "transition",
) -> bool:
    """Atomic compare-and-set on order status.

    The UPDATE's WHERE clause carries the expected source status, so only
    one caller can win a race; losers see rowcount 0 and get False. This is
    the concurrency guard for retry/accept/refund — two concurrent callers
    can never both drive the same transition.
    """
    fields = dict(fields or {})
    cols = ", ".join(["status=?"] + [f"{k}=?" for k in fields])
    with db() as conn:
        cur = conn.execute(
            f"UPDATE orders SET {cols} WHERE id=? AND status=?",
            (to_status, *fields.values(), order_id, from_status),
        )
        won = cur.rowcount == 1
        if won:
            append_audit(
                conn, "order", order_id, event, {"from": from_status, "to": to_status, **fields}
            )
    return won


def claim_retry(order_id: str, new_nonce: str) -> bool:
    """Atomically claim a send_failed payment for re-broadcast.

    Only one concurrent retry can win; the winner takes the row to
    pending_send with a fresh nonce, losers get False.
    """
    return transition(
        order_id,
        "send_failed",
        "pending_send",
        fields={"nonce": new_nonce},
        event="retry_claimed",
    )


def mark_expired_stale(now: Optional[float] = None) -> int:
    """Expire 'quoted'/'awaiting' inbound orders past their quote expiry."""
    now = now if now is not None else time.time()
    with db() as conn:
        cur = conn.execute(
            "UPDATE orders SET status='expired'"
            " WHERE kind='in' AND status IN ('quoted','awaiting') AND expires_ts IS NOT NULL"
            " AND expires_ts < ?",
            (now,),
        )
        n = cur.rowcount
        if n:
            append_audit(conn, "order", "*", "expired_stale", {"count": n})
    return n


def pending_inbound() -> List[Dict[str, Any]]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM orders WHERE kind='in' AND status IN ('quoted','awaiting')"
        ).fetchall()
    return [dict(r) for r in rows]


def expired_inbound() -> List[Dict[str, Any]]:
    """Expired inbound orders that still own a chain address.

    The watcher keeps watching these for late payments instead of dropping
    them: funds that arrive at an expired order's address are real money in
    a wallet the bridge controls, so they move to the 'late_paid'
    reconciliation state rather than being forgotten.
    """
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM orders WHERE kind='in' AND status='expired'"
            " AND address IS NOT NULL AND address != ''"
        ).fetchall()
    return [dict(r) for r in rows]


def orders_unexported(kind: str, statuses: List[str]) -> List[Dict[str, Any]]:
    placeholders = ", ".join("?" for _ in statuses)
    with db() as conn:
        rows = conn.execute(
            f"SELECT * FROM orders WHERE kind=? AND status IN ({placeholders})"
            " AND export_state IS NULL ORDER BY created_ts",
            (kind, *statuses),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_exported(order_ids: List[str], export_id: str) -> None:
    if not order_ids:
        return
    now = time.time()
    placeholders = ", ".join("?" for _ in order_ids)
    with db() as conn:
        conn.execute(
            f"UPDATE orders SET exported_ts=?, export_state='written' WHERE id IN ({placeholders})",
            (now, *order_ids),
        )
        append_audit(
            conn,
            "order",
            "*",
            "exported_to_mip",
            {"export_id": export_id, "count": len(order_ids)},
        )


# --- export claim machinery (crash-safe, concurrency-safe) -------------------


def pending_claim(kind: str, adopt_after_seconds: int = 0) -> Optional[str]:
    """Export id of a STALE claimed-but-never-written session (crash recovery).

    Matches claimed rows of the kind regardless of current status (a row
    whose status left the export set while claimed must still surface its
    session so the next run adopts and releases it). Adoption requires the
    claim's lease to be older than adopt_after_seconds: a LIVE exporter's
    fresh lease is never stolen by a concurrent run — that is what makes
    the recovery path race-free. Rows claimed by a crashed run are
    recoverable after the grace period.
    """
    cutoff = time.time() - max(0, int(adopt_after_seconds))
    with db() as conn:
        row = conn.execute(
            "SELECT export_id FROM orders WHERE export_state='claimed'"
            " AND kind=? AND export_id IS NOT NULL"
            " AND (export_lease_ts IS NULL OR export_lease_ts < ?)"
            " LIMIT 1",
            (kind, cutoff),
        ).fetchone()
    return row["export_id"] if row else None


def adopt_stale_claim(kind: str, adopt_after_seconds: int = 0) -> Optional[str]:
    """Atomically adopt a stale export SESSION — all its rows at once.

    A session may span several claimed rows. Selecting one row and renewing
    only it would let two recovery workers each renew DIFFERENT rows of the
    same session and both claim the recovery. So: select a candidate
    session by its stale-row count, then renew the lease on the ENTIRE
    session with one UPDATE whose WHERE re-checks staleness, and require
    the rowcount to equal the stale-row count seen at selection. If another
    worker renewed any row of the session in between, the rowcount will not
    match and this worker loses (returns None) — exactly one worker can
    recover a session, even under concurrency.
    """
    now = time.time()
    cutoff = now - max(0, int(adopt_after_seconds))
    stale_expr = "(export_lease_ts IS NULL OR export_lease_ts < ?)"
    with db() as conn:
        row = conn.execute(
            f"SELECT export_id, COUNT(*) AS n FROM orders"
            f" WHERE export_state='claimed' AND kind=? AND export_id IS NOT NULL AND {stale_expr}"
            f" GROUP BY export_id ORDER BY MIN(created_ts) LIMIT 1",
            (kind, cutoff),
        ).fetchone()
        if not row:
            return None
        export_id, stale_count = row["export_id"], row["n"]
        cur = conn.execute(
            f"UPDATE orders SET export_lease_ts=?"
            f" WHERE export_id=? AND export_state='claimed' AND {stale_expr}",
            (now, export_id, cutoff),
        )
        if cur.rowcount != stale_count:
            # another recovery worker renewed (part of) this session between
            # our SELECT and UPDATE — they own the recovery, we lose
            return None
        append_audit(
            conn,
            "order",
            "*",
            "export_claim_adopted",
            {"export_id": export_id, "rows": stale_count},
        )
    return export_id


def claim_unexported(kind: str, statuses: List[str], export_id: str) -> List[Dict[str, Any]]:
    """Atomically claim the currently unclaimed rows for an export session.

    A single UPDATE statement flips export_state NULL -> 'claimed' with this
    session's export_id and a fresh lease, so concurrent exporters partition
    the rows between them and no row is ever claimed twice. An ADOPTED
    stale session (same export_id, aged lease) is completed; claimed rows
    whose status left the export set (e.g. a claimed late_paid order
    refunded meanwhile) are released back to NULL so they are never
    exported against their current state.
    """
    placeholders = ", ".join("?" for _ in statuses)
    with db() as conn:
        conn.execute(
            f"UPDATE orders SET export_state='claimed', export_id=?, export_lease_ts=?"
            f" WHERE kind=? AND status IN ({placeholders}) AND export_state IS NULL",
            (export_id, time.time(), kind, *statuses),
        )
        conn.execute(
            f"UPDATE orders SET export_state=NULL, export_id=NULL, export_lease_ts=NULL"
            f" WHERE export_id=? AND export_state='claimed' AND status NOT IN ({placeholders})",
            (export_id, *statuses),
        )
        rows = conn.execute(
            "SELECT * FROM orders WHERE export_id=? AND export_state='claimed'"
            " ORDER BY created_ts",
            (export_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def finalize_export(export_id: str) -> int:
    """Mark a claimed session as written (single transaction)."""
    with db() as conn:
        cur = conn.execute(
            "UPDATE orders SET export_state='written', exported_ts=?"
            " WHERE export_id=? AND export_state='claimed'",
            (time.time(), export_id),
        )
        n = cur.rowcount
        if n:
            append_audit(
                conn, "order", "*", "exported_to_mip", {"export_id": export_id, "count": n}
            )
    return n
