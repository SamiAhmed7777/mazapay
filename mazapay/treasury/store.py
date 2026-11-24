"""Treasury store — ``tr_*`` schema, append-only hash-chained audit, queries.

Conventions are copied from :mod:`mazapay.store`, because the treasury shares
the bridge's sqlite file:

- connections come from ``mazapay.store.connect(settings.DB)`` (WAL, busy
  timeout and ``sqlite3.Row`` factory already configured);
- :func:`db` commits on success and always closes;
- :func:`write_db` opens the write transaction with ``BEGIN IMMEDIATE`` so the
  write lock is taken up front (a read-then-write upgrade under concurrency
  otherwise surfaces as ``SQLITE_BUSY``). Every state change in the service
  layer runs inside one :func:`write_db` block, so its row update and its
  audit append commit atomically;
- money is ``Decimal`` and lives in ``TEXT`` columns — never ``REAL``, never
  ``float``.

The audit table is append-only *at the schema level*: BEFORE UPDATE / BEFORE
DELETE triggers ABORT any mutation, exactly like the bridge's ``audit`` table.
``hash = sha256(prev_hash + "\\n" + canonical_json({ts, actor_user_id, action,
object_type, object_id, detail}))`` with the first row's ``prev_hash`` being 64
zeros, so a rewrite of history is detectable with :func:`verify_chain`.
"""
import contextlib
import hashlib
import json
import sqlite3
import time
import uuid
from decimal import Decimal, DecimalException, InvalidOperation
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .. import store as _bridge
from . import settings

AUDIT_GENESIS = "0" * 64
MONEY_QUANT = Decimal("0.01")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tr_users (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    role TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tr_login_tokens (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_ts REAL NOT NULL,
    expires_ts REAL NOT NULL,
    used_ts REAL
);
CREATE TABLE IF NOT EXISTS tr_sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_ts REAL NOT NULL,
    expires_ts REAL NOT NULL,
    revoked_ts REAL,
    ip TEXT,
    user_agent TEXT
);
CREATE TABLE IF NOT EXISTS tr_accounts (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    code TEXT UNIQUE NOT NULL,
    kind TEXT NOT NULL,
    maza_address TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    signature_policy TEXT NOT NULL DEFAULT 'dual',
    cap_per_request_usd TEXT,
    cap_daily_usd TEXT,
    award_ref TEXT,
    notes TEXT,
    created_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tr_payees (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    code TEXT UNIQUE NOT NULL,
    maza_address TEXT NOT NULL,
    category TEXT,
    notes TEXT,
    created_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tr_requests (
    id TEXT PRIMARY KEY,
    ref TEXT UNIQUE NOT NULL,
    account_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    purpose TEXT NOT NULL,
    award_ref TEXT,
    note TEXT,
    status TEXT NOT NULL,
    usd_total TEXT NOT NULL,
    maza_total TEXT,
    rate TEXT,
    rate_source TEXT,
    rate_locked_ts REAL,
    rate_expires_ts REAL,
    prepared_by TEXT NOT NULL,
    prepared_ts REAL NOT NULL,
    processed_by TEXT,
    processed_ts REAL,
    submitted_ts REAL,
    authorized_ts REAL,
    completed_ts REAL,
    decline_reason TEXT,
    created_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tr_request_items (
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    payee_id TEXT,
    payee_name TEXT NOT NULL,
    payee_address TEXT NOT NULL,
    category TEXT,
    usd_amount TEXT NOT NULL,
    maza_amount TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    payment_id TEXT,
    txid TEXT,
    error TEXT,
    sent_ts REAL,
    broadcast_ref TEXT,
    created_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tr_signatures (
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    role_at_signing TEXT NOT NULL,
    decision TEXT NOT NULL,
    note TEXT,
    signed_ts REAL NOT NULL,
    record_hash TEXT NOT NULL,
    UNIQUE(request_id, user_id, decision)
);
CREATE TABLE IF NOT EXISTS tr_pool_donations (
    id TEXT PRIMARY KEY,
    donor_label TEXT NOT NULL,
    usd_amount TEXT NOT NULL,
    maza_amount TEXT,
    txid TEXT,
    note TEXT,
    received_ts REAL NOT NULL,
    recorded_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tr_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    actor_user_id TEXT,
    action TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    detail TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);
-- Request-ref sequence (contract explicitly allows a counter table). Allocation
-- is a single UPDATE inside the caller's write transaction, so concurrent
-- creators get distinct numbers instead of colliding on the UNIQUE ref.
CREATE TABLE IF NOT EXISTS tr_counters (
    key TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tr_requests_account_status
    ON tr_requests(account_id, status);
CREATE INDEX IF NOT EXISTS idx_tr_requests_prepared_ts
    ON tr_requests(prepared_ts);
CREATE INDEX IF NOT EXISTS idx_tr_request_items_request
    ON tr_request_items(request_id);
CREATE INDEX IF NOT EXISTS idx_tr_signatures_request
    ON tr_signatures(request_id);
CREATE INDEX IF NOT EXISTS idx_tr_audit_object
    ON tr_audit(object_type, object_id);

-- Spend reservations: a request's claim on a cap/pool is HELD from the moment
-- the completion claim is taken until the run settles or releases it. Without
-- this, two authorized requests could each pass the daily-cap / pool-balance
-- checks and broadcast, collectively overspending.
CREATE TABLE IF NOT EXISTS tr_reservations (
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    usd_amount TEXT NOT NULL,
    maza_amount TEXT,
    created_ts REAL NOT NULL,
    settled_ts REAL,
    released_ts REAL,
    UNIQUE(request_id)
);
CREATE INDEX IF NOT EXISTS idx_tr_reservations_account
    ON tr_reservations(account_id, created_ts);
CREATE INDEX IF NOT EXISTS idx_tr_sessions_token
    ON tr_sessions(token_hash);
CREATE TRIGGER IF NOT EXISTS tr_audit_block_update BEFORE UPDATE ON tr_audit
BEGIN
    SELECT RAISE(ABORT, 'tr_audit is append-only: UPDATE refused');
END;
CREATE TRIGGER IF NOT EXISTS tr_audit_block_delete BEFORE DELETE ON tr_audit
BEGIN
    SELECT RAISE(ABORT, 'tr_audit is append-only: DELETE refused');
END;
-- Link guard: an INSERT whose prev_hash is not the current chain head is
-- refused. A caller that appended from a stale read (no write lock held)
-- gets an error instead of silently forking the chain; append_audit()
-- re-reads the head and retries.
CREATE TRIGGER IF NOT EXISTS tr_audit_link_guard BEFORE INSERT ON tr_audit
WHEN NEW.prev_hash != COALESCE(
    (SELECT hash FROM tr_audit ORDER BY id DESC LIMIT 1),
    '0000000000000000000000000000000000000000000000000000000000000000'
)
BEGIN
    SELECT RAISE(ABORT, 'tr_audit chain fork refused: prev_hash is not the chain head');
END;
"""

# Columns that may appear in a tr_requests UPDATE: values are bound, keys are
# interpolated, so the key set is whitelisted (same rule as store._ORDER_COLUMNS).
_REQUEST_COLUMNS = {
    "status", "usd_total", "maza_total", "rate", "rate_source",
    "rate_locked_ts", "rate_expires_ts", "processed_by", "processed_ts",
    "submitted_ts", "authorized_ts", "completed_ts", "decline_reason",
    "award_ref", "note", "purpose",
}

_ITEM_COLUMNS = {
    "status", "maza_amount", "payment_id", "txid", "error", "sent_ts",
    "payee_name", "payee_address", "category", "usd_amount", "broadcast_ref",
}


def _db_path() -> str:
    """Database path: ``MAZAPAY_TR_DB`` wins, then the bridge's ``MAZAPAY_DB``.

    Contract §3 pins the default to ``config.DB`` (the bridge's sqlite file)
    and §12 requires the tests to run against the temp DB ``tests/_sandbox.py``
    exports via ``MAZAPAY_DB``. The parent-owned ``settings.DB`` currently
    falls back to a path inside the package because ``mazapay.config`` exposes
    ``DB_PATH``, not ``DB``; resolving here keeps both promises without
    editing a file this lane does not own.
    """
    import os

    return os.environ.get("MAZAPAY_TR_DB") or os.environ.get("MAZAPAY_DB") or settings.DB


def connect() -> sqlite3.Connection:
    """A treasury connection against the configured database file."""
    return _bridge.connect(_db_path())


@contextlib.contextmanager
def db():
    """Read connection: commits on success (harmless) and always closes."""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except BaseException:
        try:
            conn.rollback()
        except sqlite3.Error:  # pragma: no cover - closed handle
            pass
        raise
    finally:
        conn.close()


@contextlib.contextmanager
def write_db():
    """Write connection holding the sqlite write lock for the whole block.

    ``BEGIN IMMEDIATE`` matters: without it two writers both start as readers
    and one fails to upgrade, which under the threaded server is a spurious
    500. Inside this block a row change and its ``tr_audit`` append commit
    together, so the audit trail can never miss a state transition.
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except BaseException:
        try:
            conn.rollback()
        except sqlite3.Error:  # pragma: no cover
            pass
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Create every table, index and trigger. Idempotent and process-safe."""
    with db() as conn:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:  # pragma: no cover - unsupported filesystem
            pass
        conn.executescript(_SCHEMA)
        # Columns added after the first release: an existing database keeps its
        # rows, so additive migrations run here (idempotent, process-safe).
        cols = {row[1] for row in conn.execute("PRAGMA table_info(tr_request_items)")}
        if "broadcast_ref" not in cols:
            conn.execute("ALTER TABLE tr_request_items ADD COLUMN broadcast_ref TEXT")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def to_money(value: Any) -> str:
    """Decimal -> 2dp TEXT. Raises ValueError for anything not a finite number."""
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"not a decimal amount: {value!r}") from None
    if not dec.is_finite():
        raise ValueError(f"not a finite amount: {value!r}")
    try:
        return str(dec.quantize(MONEY_QUANT))
    except DecimalException:
        raise ValueError(f"amount out of range: {value!r}") from None


def _fetchall(sql: str, args: Sequence[Any] = (), conn=None) -> List[sqlite3.Row]:
    if conn is not None:
        return conn.execute(sql, args).fetchall()
    with db() as own:
        return own.execute(sql, args).fetchall()


def _fetchone(sql: str, args: Sequence[Any] = (), conn=None) -> Optional[sqlite3.Row]:
    if conn is not None:
        return conn.execute(sql, args).fetchone()
    with db() as own:
        return own.execute(sql, args).fetchone()


# --- audit ------------------------------------------------------------------


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _audit_hash(
    prev_hash: str,
    ts: float,
    actor_user_id: Optional[str],
    action: str,
    object_type: str,
    object_id: str,
    detail_obj: Any,
) -> str:
    payload = {
        "ts": ts,
        "actor_user_id": actor_user_id,
        "action": action,
        "object_type": object_type,
        "object_id": object_id,
        "detail": detail_obj,
    }
    return hashlib.sha256(
        (prev_hash + "\n" + _canonical(payload)).encode("utf-8")
    ).hexdigest()


def append_audit(
    conn: sqlite3.Connection,
    *,
    actor_user_id: Optional[str],
    action: str,
    object_type: str,
    object_id: str,
    detail: Optional[Dict[str, Any]] = None,
    ts: Optional[float] = None,
) -> str:
    """Append one chained audit row on ``conn``; returns its hash.

    Must run inside :func:`write_db` (or any transaction that holds the write
    lock): the previous hash is read and the new row written under the same
    lock, so two writers can never fork the chain.
    """
    ts = time.time() if ts is None else float(ts)
    detail_obj: Any = detail if isinstance(detail, dict) else ({} if detail is None else {"value": detail})
    for attempt in range(5):
        row = conn.execute("SELECT hash FROM tr_audit ORDER BY id DESC LIMIT 1").fetchone()
        prev = row["hash"] if row else AUDIT_GENESIS
        digest = _audit_hash(prev, ts, actor_user_id, action, object_type, object_id, detail_obj)
        try:
            conn.execute(
                "INSERT INTO tr_audit (ts, actor_user_id, action, object_type, object_id,"
                " detail, prev_hash, hash) VALUES (?,?,?,?,?,?,?,?)",
                (ts, actor_user_id, action, object_type, object_id, _canonical(detail_obj), prev, digest),
            )
            return digest
        except sqlite3.IntegrityError as exc:
            # Another writer appended between our head read and our insert:
            # tr_audit_link_guard refused the fork. Re-read and retry; inside
            # write_db this never happens (the write lock is already held).
            if "chain fork" in str(exc) and attempt < 4:
                continue
            raise
    raise sqlite3.IntegrityError("tr_audit append failed: chain head kept moving")


def verify_chain(
    object_type: Optional[str] = None, object_id: Optional[str] = None
) -> Tuple[bool, Optional[str]]:
    """Verify the audit hash chain; returns ``(ok, first_broken_id)``.

    The whole chain is always walked — a hash chain is only meaningful
    end-to-end, and a break early on invalidates every later row. The filters
    scope which row id is *reported*: the earliest broken row matching the
    filter, else the earliest broken row overall.
    """
    rows = _fetchall(
        "SELECT id, ts, actor_user_id, action, object_type, object_id, detail,"
        " prev_hash, hash FROM tr_audit ORDER BY id"
    )
    prev = AUDIT_GENESIS
    broken_global: Optional[str] = None
    broken_scoped: Optional[str] = None
    for row in rows:
        broken = False
        if row["prev_hash"] != prev:
            broken = True
        else:
            try:
                detail_obj = json.loads(row["detail"])
            except (ValueError, TypeError):
                detail_obj = row["detail"]
            expected = _audit_hash(
                prev,
                row["ts"],
                row["actor_user_id"],
                row["action"],
                row["object_type"],
                row["object_id"],
                detail_obj,
            )
            if expected != row["hash"]:
                broken = True
        if broken:
            rid = str(row["id"])
            if broken_global is None:
                broken_global = rid
            if (
                broken_scoped is None
                and (object_type is None or row["object_type"] == object_type)
                and (object_id is None or row["object_id"] == object_id)
            ):
                broken_scoped = rid
        prev = row["hash"]
    if broken_global is None:
        return True, None
    if object_type is not None or object_id is not None:
        return False, broken_scoped or broken_global
    return False, broken_global


def audit_rows(
    *,
    object_type: Optional[str] = None,
    object_id: Optional[str] = None,
    actor_user_id: Optional[str] = None,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM tr_audit WHERE 1=1"
    args: List[Any] = []
    if object_type is not None:
        sql += " AND object_type=?"
        args.append(object_type)
    if object_id is not None:
        sql += " AND object_id=?"
        args.append(object_id)
    if actor_user_id is not None:
        sql += " AND actor_user_id=?"
        args.append(actor_user_id)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit or 200), 1000)))
    return [dict(r) for r in _fetchall(sql, args)]


# --- counters ---------------------------------------------------------------


def next_counter(conn: sqlite3.Connection, key: str) -> int:
    """Allocate the next integer for ``key`` inside the caller's write txn."""
    conn.execute("INSERT OR IGNORE INTO tr_counters (key, value) VALUES (?, 0)", (key,))
    conn.execute("UPDATE tr_counters SET value = value + 1 WHERE key = ?", (key,))
    row = conn.execute("SELECT value FROM tr_counters WHERE key = ?", (key,)).fetchone()
    return int(row["value"])


# --- accounts / payees ------------------------------------------------------


def create_account(
    name: str,
    code: str,
    kind: str,
    *,
    maza_address: Optional[str] = None,
    status: str = "open",
    signature_policy: str = "dual",
    cap_per_request_usd: Optional[str] = None,
    cap_daily_usd: Optional[str] = None,
    award_ref: Optional[str] = None,
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    aid = _new_id("acct")
    now = time.time()
    with write_db() as conn:
        conn.execute(
            "INSERT INTO tr_accounts (id, name, code, kind, maza_address, status,"
            " signature_policy, cap_per_request_usd, cap_daily_usd, award_ref,"
            " notes, created_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                aid,
                name,
                code,
                kind,
                maza_address,
                status,
                signature_policy,
                to_money(cap_per_request_usd) if cap_per_request_usd not in (None, "") else None,
                to_money(cap_daily_usd) if cap_daily_usd not in (None, "") else None,
                award_ref,
                notes,
                now,
            ),
        )
        append_audit(
            conn,
            actor_user_id=None,
            action="account_created",
            object_type="account",
            object_id=aid,
            detail={"code": code, "kind": kind, "policy": signature_policy},
        )
    return get_account(aid) or {}


def get_account(account_id: str) -> Optional[Dict[str, Any]]:
    row = _fetchone("SELECT * FROM tr_accounts WHERE id=?", (account_id,))
    return dict(row) if row else None


def get_account_by_code(code: str) -> Optional[Dict[str, Any]]:
    row = _fetchone("SELECT * FROM tr_accounts WHERE code=?", (code,))
    return dict(row) if row else None


def list_accounts() -> List[Dict[str, Any]]:
    return [dict(r) for r in _fetchall("SELECT * FROM tr_accounts ORDER BY code")]


def create_payee(
    name: str,
    code: str,
    maza_address: str,
    *,
    category: Optional[str] = None,
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    pid = _new_id("payee")
    now = time.time()
    with write_db() as conn:
        conn.execute(
            "INSERT INTO tr_payees (id, name, code, maza_address, category, notes,"
            " created_ts) VALUES (?,?,?,?,?,?,?)",
            (pid, name, code, maza_address, category, notes, now),
        )
        append_audit(
            conn,
            actor_user_id=None,
            action="payee_created",
            object_type="payee",
            object_id=pid,
            detail={"code": code, "category": category},
        )
    return get_payee(pid) or {}


def get_payee(payee_id: str) -> Optional[Dict[str, Any]]:
    row = _fetchone("SELECT * FROM tr_payees WHERE id=?", (payee_id,))
    return dict(row) if row else None


def get_payee_by_code(code: str) -> Optional[Dict[str, Any]]:
    row = _fetchone("SELECT * FROM tr_payees WHERE code=?", (code,))
    return dict(row) if row else None


def list_payees() -> List[Dict[str, Any]]:
    return [dict(r) for r in _fetchall("SELECT * FROM tr_payees ORDER BY name")]


# --- requests ---------------------------------------------------------------


def get_request_row(
    request_id: str, conn: Optional[sqlite3.Connection] = None
) -> Optional[Dict[str, Any]]:
    row = _fetchone("SELECT * FROM tr_requests WHERE id=?", (request_id,), conn)
    return dict(row) if row else None


def get_request_by_ref(ref: str) -> Optional[Dict[str, Any]]:
    row = _fetchone("SELECT * FROM tr_requests WHERE ref=?", (ref,))
    return dict(row) if row else None


def list_request_rows(
    *,
    status: Optional[str] = None,
    account_id: Optional[str] = None,
    limit: int = 100,
    conn: Optional[sqlite3.Connection] = None,
) -> List[Dict[str, Any]]:
    sql = (
        "SELECT r.*, a.code AS account_code, a.name AS account_name,"
        " a.kind AS account_kind FROM tr_requests r"
        " LEFT JOIN tr_accounts a ON a.id = r.account_id WHERE 1=1"
    )
    args: List[Any] = []
    if status:
        sql += " AND r.status=?"
        args.append(status)
    if account_id:
        sql += " AND r.account_id=?"
        args.append(account_id)
    sql += " ORDER BY r.prepared_ts DESC, r.id DESC LIMIT ?"
    args.append(max(1, min(int(limit or 100), 1000)))
    return [dict(r) for r in _fetchall(sql, args, conn)]


def insert_request(conn: sqlite3.Connection, row: Dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO tr_requests (id, ref, account_id, kind, purpose, award_ref,"
        " note, status, usd_total, maza_total, rate, rate_source, rate_locked_ts,"
        " rate_expires_ts, prepared_by, prepared_ts, processed_by, processed_ts,"
        " submitted_ts, authorized_ts, completed_ts, decline_reason, created_ts)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            row["id"], row["ref"], row["account_id"], row["kind"], row["purpose"],
            row.get("award_ref"), row.get("note"), row["status"], row["usd_total"],
            row.get("maza_total"), row.get("rate"), row.get("rate_source"),
            row.get("rate_locked_ts"), row.get("rate_expires_ts"),
            row["prepared_by"], row["prepared_ts"], row.get("processed_by"),
            row.get("processed_ts"), row.get("submitted_ts"),
            row.get("authorized_ts"), row.get("completed_ts"),
            row.get("decline_reason"), row["created_ts"],
        ),
    )


def update_request(
    conn: sqlite3.Connection,
    request_id: str,
    fields: Dict[str, Any],
    *,
    from_status: Optional[str] = None,
) -> bool:
    """Update request columns; with ``from_status`` it is an atomic CAS.

    Returns True when the row was updated. A CAS that loses the race returns
    False, which the service layer turns into a 409 rather than a silent
    double transition.
    """
    if not fields:
        return False
    unknown = [k for k in fields if k not in _REQUEST_COLUMNS]
    if unknown:
        raise ValueError(f"unknown request columns: {unknown}")
    cols = ", ".join(f"{k}=?" for k in fields)
    sql = f"UPDATE tr_requests SET {cols} WHERE id=?"
    args: List[Any] = list(fields.values()) + [request_id]
    if from_status is not None:
        sql += " AND status=?"
        args.append(from_status)
    return conn.execute(sql, args).rowcount == 1


def list_items(
    request_id: str, conn: Optional[sqlite3.Connection] = None
) -> List[Dict[str, Any]]:
    return [
        dict(r)
        for r in _fetchall(
            "SELECT * FROM tr_request_items WHERE request_id=? ORDER BY created_ts, rowid",
            (request_id,),
            conn,
        )
    ]


def get_item(item_id: str, conn: Optional[sqlite3.Connection] = None) -> Optional[Dict[str, Any]]:
    row = _fetchone("SELECT * FROM tr_request_items WHERE id=?", (item_id,), conn)
    return dict(row) if row else None


def insert_item(conn: sqlite3.Connection, row: Dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO tr_request_items (id, request_id, payee_id, payee_name,"
        " payee_address, category, usd_amount, maza_amount, status, payment_id,"
        " txid, error, sent_ts, created_ts)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            row["id"], row["request_id"], row.get("payee_id"), row["payee_name"],
            row["payee_address"], row.get("category"), row["usd_amount"],
            row.get("maza_amount"), row.get("status", "pending"), row.get("payment_id"),
            row.get("txid"), row.get("error"), row.get("sent_ts"), row["created_ts"],
        ),
    )


def update_item(
    conn: sqlite3.Connection,
    item_id: str,
    fields: Dict[str, Any],
    *,
    from_status: Optional[str] = None,
) -> bool:
    if not fields:
        return False
    unknown = [k for k in fields if k not in _ITEM_COLUMNS]
    if unknown:
        raise ValueError(f"unknown item columns: {unknown}")
    cols = ", ".join(f"{k}=?" for k in fields)
    sql = f"UPDATE tr_request_items SET {cols} WHERE id=?"
    args: List[Any] = list(fields.values()) + [item_id]
    if from_status is not None:
        sql += " AND status=?"
        args.append(from_status)
    return conn.execute(sql, args).rowcount == 1


def list_signatures(
    request_id: str, conn: Optional[sqlite3.Connection] = None
) -> List[Dict[str, Any]]:
    return [
        dict(r)
        for r in _fetchall(
            "SELECT * FROM tr_signatures WHERE request_id=? ORDER BY signed_ts, rowid",
            (request_id,),
            conn,
        )
    ]


def get_signature(
    request_id: str, user_id: str, conn: Optional[sqlite3.Connection] = None
) -> Optional[Dict[str, Any]]:
    row = _fetchone(
        "SELECT * FROM tr_signatures WHERE request_id=? AND user_id=? LIMIT 1",
        (request_id, user_id),
        conn,
    )
    return dict(row) if row else None


def insert_signature(conn: sqlite3.Connection, row: Dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO tr_signatures (id, request_id, user_id, role_at_signing,"
        " decision, note, signed_ts, record_hash) VALUES (?,?,?,?,?,?,?,?)",
        (
            row["id"], row["request_id"], row["user_id"], row["role_at_signing"],
            row["decision"], row.get("note"), row["signed_ts"], row["record_hash"],
        ),
    )


def request_txids(request_id: str) -> List[str]:
    rows = _fetchall(
        "SELECT txid FROM tr_request_items WHERE request_id=? AND txid IS NOT NULL"
        " ORDER BY sent_ts, rowid",
        (request_id,),
    )
    return [r["txid"] for r in rows]


# --- pool -------------------------------------------------------------------


def insert_donation(conn: sqlite3.Connection, row: Dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO tr_pool_donations (id, donor_label, usd_amount, maza_amount,"
        " txid, note, received_ts, recorded_by) VALUES (?,?,?,?,?,?,?,?)",
        (
            row["id"], row["donor_label"], row["usd_amount"], row.get("maza_amount"),
            row.get("txid"), row.get("note"), row["received_ts"], row["recorded_by"],
        ),
    )


def list_donations(
    *, since_ts: Optional[float] = None, limit: int = 500
) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM tr_pool_donations WHERE 1=1"
    args: List[Any] = []
    if since_ts is not None:
        sql += " AND received_ts >= ?"
        args.append(since_ts)
    sql += " ORDER BY received_ts DESC LIMIT ?"
    args.append(max(1, min(int(limit or 500), 5000)))
    return [dict(r) for r in _fetchall(sql, args)]


def get_donation(donation_id: str) -> Optional[Dict[str, Any]]:
    row = _fetchone("SELECT * FROM tr_pool_donations WHERE id=?", (donation_id,))
    return dict(row) if row else None


def stipend_items(*, limit: int = 500) -> List[Dict[str, Any]]:
    """Stipend-run items joined with their request/account (pool ledger)."""
    return [
        dict(r)
        for r in _fetchall(
            "SELECT i.*, r.ref AS request_ref, r.status AS request_status,"
            " r.purpose AS request_purpose, r.account_id AS account_id,"
            " r.rate AS rate, r.rate_source AS rate_source,"
            " a.name AS account_name, a.code AS account_code"
            " FROM tr_request_items i"
            " JOIN tr_requests r ON r.id = i.request_id"
            " LEFT JOIN tr_accounts a ON a.id = r.account_id"
            " WHERE r.kind='stipend_run' AND i.status='sent'"
            " ORDER BY i.created_ts DESC, i.rowid DESC LIMIT ?",
            (max(1, min(int(limit or 500), 5000)),),
        )
    ]


def requests_prepared_between(start_ts: float, end_ts: float) -> List[Dict[str, Any]]:
    return [
        dict(r)
        for r in _fetchall(
            "SELECT r.*, a.code AS account_code, a.name AS account_name,"
            " a.kind AS account_kind, a.award_ref AS account_award_ref"
            " FROM tr_requests r LEFT JOIN tr_accounts a ON a.id = r.account_id"
            " WHERE r.prepared_ts >= ? AND r.prepared_ts < ?"
            " ORDER BY r.prepared_ts, r.id",
            (start_ts, end_ts),
        )
    ]


def donations_between(start_ts: float, end_ts: float) -> List[Dict[str, Any]]:
    return [
        dict(r)
        for r in _fetchall(
            "SELECT * FROM tr_pool_donations WHERE received_ts >= ? AND received_ts < ?"
            " ORDER BY received_ts, id",
            (start_ts, end_ts),
        )
    ]


def completed_requests_for_day(
    account_id: str, start_ts: float, end_ts: float, *, exclude_request_id: Optional[str] = None
) -> List[Dict[str, Any]]:
    sql = (
        "SELECT * FROM tr_requests WHERE account_id=? AND status IN ('completed','completed_partial')"
        " AND completed_ts IS NOT NULL AND completed_ts >= ? AND completed_ts < ?"
    )
    args: List[Any] = [account_id, start_ts, end_ts]
    if exclude_request_id:
        sql += " AND id != ?"
        args.append(exclude_request_id)
    return [dict(r) for r in _fetchall(sql, args)]


def signing_user_names(request_id: str) -> List[str]:
    """Names of the users who approved a request (statement SIGNERS cell)."""
    rows = _fetchall(
        "SELECT COALESCE(u.name, s.user_id) AS name FROM tr_signatures s"
        " LEFT JOIN tr_users u ON u.id = s.user_id"
        " WHERE s.request_id=? AND s.decision='approve' ORDER BY s.signed_ts",
        (request_id,),
    )
    return [r["name"] for r in rows]

# --- spend reservations + bridge adoption -----------------------------------
#
# A reservation is the request's HOLD on a cap and on the pool balance, taken
# inside the same BEGIN IMMEDIATE transaction that claims completion. Two
# distinct authorized requests therefore cannot both pass a cap check and
# broadcast: the second sees the first's hold.


def create_reservation(
    conn: sqlite3.Connection,
    *,
    request_id: str,
    account_id: str,
    usd_amount: Any,
    maza_amount: Any = None,
) -> str:
    rid = _new_id("res")
    conn.execute(
        "INSERT INTO tr_reservations"
        " (id, request_id, account_id, usd_amount, maza_amount, created_ts)"
        " VALUES (?,?,?,?,?,?)"
        " ON CONFLICT(request_id) DO UPDATE SET"
        " usd_amount=excluded.usd_amount, maza_amount=excluded.maza_amount,"
        " created_ts=excluded.created_ts, settled_ts=NULL, released_ts=NULL",
        (
            rid,
            request_id,
            account_id,
            to_money(usd_amount),
            None if maza_amount is None else str(maza_amount),
            time.time(),
        ),
    )
    return rid


def active_reservations(
    account_id: str,
    start_ts: float,
    end_ts: float,
    *,
    exclude_request_id: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> List[Dict[str, Any]]:
    sql = (
        "SELECT * FROM tr_reservations WHERE account_id=? AND created_ts >= ?"
        " AND created_ts < ? AND settled_ts IS NULL AND released_ts IS NULL"
    )
    args: List[Any] = [account_id, start_ts, end_ts]
    if exclude_request_id:
        sql += " AND request_id != ?"
        args.append(exclude_request_id)
    return [dict(r) for r in _fetchall(sql, args, conn)]


def live_reservations(
    *,
    exclude_request_id: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM tr_reservations WHERE settled_ts IS NULL AND released_ts IS NULL"
    args: List[Any] = []
    if exclude_request_id:
        sql += " AND request_id != ?"
        args.append(exclude_request_id)
    return [dict(r) for r in _fetchall(sql, args, conn)]


def settle_reservation(
    conn: sqlite3.Connection, request_id: str, *, released: bool = False
) -> None:
    now = time.time()
    if released:
        conn.execute(
            "UPDATE tr_reservations SET released_ts=?"
            " WHERE request_id=? AND settled_ts IS NULL AND released_ts IS NULL",
            (now, request_id),
        )
    else:
        conn.execute(
            "UPDATE tr_reservations SET settled_ts=?"
            " WHERE request_id=? AND settled_ts IS NULL",
            (now, request_id),
        )


def find_outbound_by_ref(
    ref: str, conn: Optional[sqlite3.Connection] = None
) -> Optional[Dict[str, Any]]:
    """The bridge order already created for a treasury item, if any.

    The treasury records a stable ``broadcast_ref`` on the item BEFORE calling
    the bridge, so a crash inside the broadcast is recoverable by lookup
    instead of by a second payment.
    """
    try:
        rows = _fetchall(
            "SELECT * FROM orders WHERE kind='out' AND ref=?"
            " ORDER BY created_ts DESC LIMIT 1",
            (ref,),
            conn,
        )
    except sqlite3.OperationalError:
        # The bridge schema is absent (a treasury-only test database): there
        # cannot be a prior broadcast to adopt.
        return None
    return dict(rows[0]) if rows else None
