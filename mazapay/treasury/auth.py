"""Treasury identity layer — users, roles, magic-link login, sessions, CSRF.

Owns `tr_users`, `tr_login_tokens`, `tr_sessions` (docs/CONTRACT-treasury.md §4)
and the auth half of the audit trail (§8). Every mutation is appended to
`tr_audit` with the contract's sha256 hash chain.

Design notes (all pinned by the contract):

- Tokens are `secrets.token_urlsafe(32)`; only the sha256 hex is stored, and
  every comparison is `hmac.compare_digest`.
- No user enumeration: `request_magic_link` returns the same shape for an
  unknown address as for a known one (in demo mode the decoy carries a
  throwaway token, so even the presence of a link does not leak existence).
- Magic links are single-use: consumption flips `used_ts` with a
  compare-and-set UPDATE, so exactly one caller can win a link.
- Rate limiting is per email AND per IP, in process memory, thread-safe.
- `check_csrf` is derived from the session token hash because `tr_sessions`
  has no csrf column (the contract's DDL is exact) — deterministic, so it
  survives a restart, and never stored in plain text.
"""
import collections
import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
import uuid
from typing import Any, Dict, Iterable, List, Optional

from .. import store as _bridge_store
from . import settings

try:  # lane 1's schema/queries module; optional while lanes build in parallel
    from . import store as tr_store  # type: ignore
except Exception:  # pragma: no cover - sibling lane absent or mid-write
    tr_store = None

LOG = logging.getLogger(__name__)

# --- errors ------------------------------------------------------------------
#
# The canonical error class lives in `service.py` (lane 1). Import it when it
# is available so a TreasuryError raised here is the exact class the HTTP layer
# catches; fall back to a compatible local definition until lane 1 lands.
try:  # pragma: no cover - depends on sibling lane timing
    from .service import TreasuryError  # type: ignore
except Exception:  # pragma: no cover - sibling lane not written yet / broken import
    class TreasuryError(Exception):
        """Repo-local stand-in with the contract's `.code` / `.status` shape."""

        def __init__(self, code: str, message: str, status: int = 400):
            super().__init__(message)
            self.code = code
            self.message = message
            self.status = status


def _err(code: str, status: int, message: str) -> Exception:
    """Build a TreasuryError whatever constructor shape the class has."""
    try:
        exc = TreasuryError(code, message, status=status)
    except TypeError:  # a (code, message) implementation without a status arg
        exc = TreasuryError(code, message)
        exc.status = status
    if getattr(exc, "code", None) is None:
        exc.code = code
    if not isinstance(getattr(exc, "status", None), int):
        exc.status = status
    return exc


# --- constants ---------------------------------------------------------------

ROLES = (
    "admin",
    "executive",
    "council_treasurer",
    "treasurer",
    "finance_officer",
    "viewer",
)

# docs/CONTRACT-treasury.md §5. `sign` is policy-dependent, so it is handled by
# `may_sign(user, policy)`; `can()` answers "may this role sign anything?".
_PERMISSIONS: Dict[str, frozenset] = {
    "read": frozenset(ROLES),
    "create_request": frozenset({"admin", "treasurer", "finance_officer"}),
    "submit": frozenset({"admin", "treasurer", "finance_officer"}),
    "sign": frozenset({"executive", "council_treasurer", "treasurer"}),
    "cancel": frozenset({"admin", "treasurer", "finance_officer"}),
    "complete": frozenset({"admin", "treasurer"}),
    "record_donation": frozenset({"admin", "treasurer"}),
    "manage_users": frozenset({"admin"}),
}

RATE_LIMIT_MAX = 5
RATE_LIMIT_WINDOW = 900.0  # 15 minutes

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tr_users (
    id TEXT PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
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
-- Defensive: auth.init_db() must be usable on its own (the contract gives the
-- exact DDL, so this composes with lane 1's store.init_db() via IF NOT EXISTS).
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
"""

_ZERO_HASH = "0" * 64

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_init_lock = threading.Lock()
_ready_paths: set = set()

# --- in-memory rate limiter --------------------------------------------------

_rl_lock = threading.Lock()
_rl_hits: Dict[Any, collections.deque] = {}


def _rate_keys(email: str, ip: Optional[str]) -> List[Any]:
    keys: List[Any] = [("email", email)]
    if ip:
        keys.append(("ip", ip))
    return keys


def _rate_take(keys: Iterable[Any], now: float) -> bool:
    """Record one attempt against every key, or refuse if any key is at cap."""
    cutoff = now - RATE_LIMIT_WINDOW
    with _rl_lock:
        for key in keys:
            dq = _rl_hits.get(key)
            if dq is None:
                continue
            while dq and dq[0] <= cutoff:
                dq.popleft()
            if len(dq) >= RATE_LIMIT_MAX:
                return False
        for key in keys:
            dq = _rl_hits.get(key)
            if dq is None:
                dq = _rl_hits[key] = collections.deque()
            dq.append(now)
        return True


def reset_rate_limits() -> None:
    """Drop every in-memory window (tests / operator relief valve)."""
    with _rl_lock:
        _rl_hits.clear()


# --- plumbing ----------------------------------------------------------------


def _now() -> float:
    return time.time()


def _db_path() -> str:
    """Effective treasury DB path — same precedence lane 1's store uses."""
    return (
        os.environ.get("MAZAPAY_TR_DB")
        or os.environ.get("MAZAPAY_DB")
        or settings.DB
    )


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _norm_email(email: Optional[str]) -> str:
    return (email or "").strip().lower()


@contextlib.contextmanager
def _db():
    """Read/write connection; lane 1's store owns the connection policy."""
    if tr_store is not None and hasattr(tr_store, "db"):
        with tr_store.db() as conn:
            yield conn
        return
    with _bridge_store.db(_db_path()) as conn:
        yield conn


@contextlib.contextmanager
def _write_db():
    """Write connection holding the sqlite write lock for the whole block.

    The audit append and the row change it describes must commit together and
    read the previous hash under the same lock, otherwise two writers can fork
    the chain (lane 1's store documents the same requirement).
    """
    if tr_store is not None and hasattr(tr_store, "write_db"):
        with tr_store.write_db() as conn:
            yield conn
        return
    conn = _bridge_store.connect(_db_path())
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except BaseException:
        try:
            conn.rollback()
        except Exception:  # pragma: no cover - closed handle
            pass
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Create the identity tables (idempotent; composes with store.init_db())."""
    if tr_store is not None and hasattr(tr_store, "init_db"):
        tr_store.init_db()
    else:
        with _bridge_store.db(_db_path()) as conn:
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except Exception:  # pragma: no cover - exotic sqlite builds
                pass
            conn.executescript(_SCHEMA)
    with _init_lock:
        _ready_paths.add(_db_path())


def _ensure() -> None:
    if _db_path() in _ready_paths:
        return
    with _init_lock:
        if _db_path() in _ready_paths:
            return
    init_db()


# --- audit -------------------------------------------------------------------


def _canonical(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def append_audit(
    conn,
    *,
    actor_user_id: Optional[str],
    action: str,
    object_type: str,
    object_id: str,
    detail: Optional[Dict[str, Any]] = None,
) -> str:
    """Append one `tr_audit` row and return its hash (contract §4 chain).

    Delegates to lane 1's `treasury.store.append_audit` when it is importable —
    one implementation, one canonical form, so auth rows and service rows link
    into a single chain. The local body is an identical stand-in for trees
    where store.py has not landed yet (same columns, same hash formula).
    """
    fn = getattr(tr_store, "append_audit", None) if tr_store is not None else None
    if fn is not None:
        try:
            return fn(
                conn,
                actor_user_id=actor_user_id,
                action=action,
                object_type=object_type,
                object_id=object_id,
                detail=detail or {},
            )
        except TypeError:  # pragma: no cover - different sibling signature
            pass
    detail = detail or {}
    row = conn.execute("SELECT hash FROM tr_audit ORDER BY id DESC LIMIT 1").fetchone()
    prev_hash = row["hash"] if row else _ZERO_HASH
    ts = _now()
    payload = {
        "ts": ts,
        "actor_user_id": actor_user_id,
        "action": action,
        "object_type": object_type,
        "object_id": object_id,
        "detail": detail,
    }
    row_hash = hashlib.sha256((prev_hash + "\n" + _canonical(payload)).encode("utf-8")).hexdigest()
    conn.execute(
        "INSERT INTO tr_audit (ts, actor_user_id, action, object_type, object_id,"
        " detail, prev_hash, hash) VALUES (?,?,?,?,?,?,?,?)",
        (
            ts,
            actor_user_id,
            action,
            object_type,
            object_id,
            _canonical(detail),
            prev_hash,
            row_hash,
        ),
    )
    return row_hash


def verify_audit_chain(db_path: Optional[str] = None) -> bool:
    """True when every `tr_audit` row links to its predecessor."""
    if tr_store is not None and hasattr(tr_store, "verify_chain") and db_path is None:
        try:
            ok = tr_store.verify_chain()
            return bool(ok[0]) if isinstance(ok, tuple) else bool(ok)
        except Exception:  # pragma: no cover - sibling signature drift
            pass
    with _bridge_store.db(db_path or _db_path()) as conn:
        prev = _ZERO_HASH
        for row in conn.execute("SELECT * FROM tr_audit ORDER BY id").fetchall():
            payload = {
                "ts": row["ts"],
                "actor_user_id": row["actor_user_id"],
                "action": row["action"],
                "object_type": row["object_type"],
                "object_id": row["object_id"],
                "detail": json.loads(row["detail"]),
            }
            expect = hashlib.sha256((prev + "\n" + _canonical(payload)).encode("utf-8")).hexdigest()
            if row["prev_hash"] != prev or row["hash"] != expect:
                return False
            prev = row["hash"]
    return True


# --- permissions -------------------------------------------------------------


def can(user: Optional[Dict[str, Any]], action: str) -> bool:
    """Role-matrix check from contract §5 (inactive users can do nothing)."""
    if not user or not user.get("active", True):
        return False
    allowed = _PERMISSIONS.get(action)
    return bool(allowed) and user.get("role") in allowed


def may_sign(user: Optional[Dict[str, Any]], policy: str) -> bool:
    """Signature eligibility per account policy: single -> treasurer,
    dual -> executive or council_treasurer. Admin never signs."""
    if not user or not user.get("active", True):
        return False
    role = user.get("role")
    if policy == "single":
        return role == "treasurer"
    if policy == "dual":
        return role in ("executive", "council_treasurer")
    return False


def _public_user(row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "email": row["email"],
        "name": row["name"],
        "role": row["role"],
        "active": bool(row["active"]),
        "created_ts": row["created_ts"],
    }


def _get_user_by_email(conn, email: str) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM tr_users WHERE email=?", (email,)).fetchone()
    return _public_user(row) if row else None


def _get_user(conn, user_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM tr_users WHERE id=?", (user_id,)).fetchone()
    return _public_user(row) if row else None


def _require_admin(admin_user: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not admin_user or not admin_user.get("id"):
        raise _err("not_authenticated", 401, "sign-in required")
    if not admin_user.get("active", True) or admin_user.get("role") != "admin":
        raise _err("insufficient_role", 403, "administrator role required")
    return admin_user


# --- users -------------------------------------------------------------------


def bootstrap_admin(email: str, name: str) -> Dict[str, Any]:
    """Idempotent first-admin creation for the seed script.

    Returns the existing user when the email (or, failing that, any admin)
    already exists — a second bootstrap never creates a second admin.
    """
    _ensure()
    addr = _norm_email(email)
    if not _EMAIL_RE.match(addr):
        raise _err("invalid_email", 400, "a valid email address is required")
    display = (name or "").strip() or addr
    with _write_db() as conn:
        existing = _get_user_by_email(conn, addr)
        if existing:
            return existing
        row = conn.execute(
            "SELECT * FROM tr_users WHERE role='admin' ORDER BY created_ts LIMIT 1"
        ).fetchone()
        if row:
            return _public_user(row)
        uid = _new_id("usr")
        try:
            conn.execute(
                "INSERT INTO tr_users (id, email, name, role, active, created_ts)"
                " VALUES (?,?,?,?,1,?)",
                (uid, addr, display, "admin", _now()),
            )
        except Exception as exc:  # lost a race on the UNIQUE email
            if "UNIQUE" not in str(exc):
                raise
            again = _get_user_by_email(conn, addr)
            if again:
                return again
            raise
        append_audit(
            conn,
            actor_user_id=None,
            action="bootstrap_admin",
            object_type="user",
            object_id=uid,
            detail={"email": addr, "name": display, "role": "admin"},
        )
        user = _get_user(conn, uid)
    return user or {"id": uid, "email": addr, "name": display, "role": "admin",
                    "active": True, "created_ts": _now()}


def create_user(
    admin_user: Optional[Dict[str, Any]], *, email: str, name: str, role: str
) -> Dict[str, Any]:
    """Create a user. Admin only; role must be in ROLES; email is unique."""
    _ensure()
    _require_admin(admin_user)
    if role not in ROLES:
        raise _err("invalid_role", 400, f"unknown role: {role!r}")
    addr = _norm_email(email)
    if not _EMAIL_RE.match(addr):
        raise _err("invalid_email", 400, "a valid email address is required")
    display = (name or "").strip()
    if not display:
        raise _err("invalid_name", 400, "a name is required")
    uid = _new_id("usr")
    with _write_db() as conn:
        if _get_user_by_email(conn, addr):
            raise _err("email_exists", 409, "a user with that email already exists")
        try:
            conn.execute(
                "INSERT INTO tr_users (id, email, name, role, active, created_ts)"
                " VALUES (?,?,?,?,1,?)",
                (uid, addr, display, role, _now()),
            )
        except Exception as exc:
            if "UNIQUE" in str(exc):
                raise _err("email_exists", 409, "a user with that email already exists")
            raise
        append_audit(
            conn,
            actor_user_id=admin_user["id"],
            action="user_created",
            object_type="user",
            object_id=uid,
            detail={"email": addr, "name": display, "role": role},
        )
        user = _get_user(conn, uid)
    assert user is not None
    return user


def list_users(user: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Every user (any signed-in role may read; contract §5)."""
    _ensure()
    if not user or not user.get("id"):
        raise _err("not_authenticated", 401, "sign-in required")
    with _db() as conn:
        rows = conn.execute("SELECT * FROM tr_users ORDER BY created_ts, email").fetchall()
    return [_public_user(r) for r in rows]


def deactivate_user(
    admin_user: Optional[Dict[str, Any]], user_id: str
) -> Dict[str, Any]:
    """Flip `active` to 0 and kill that user's live sessions. Idempotent."""
    _ensure()
    _require_admin(admin_user)
    with _write_db() as conn:
        target = _get_user(conn, user_id)
        if not target:
            raise _err("user_not_found", 404, "no such user")
        if target["active"]:
            conn.execute("UPDATE tr_users SET active=0 WHERE id=?", (user_id,))
            conn.execute(
                "UPDATE tr_sessions SET revoked_ts=? WHERE user_id=? AND revoked_ts IS NULL",
                (_now(), user_id),
            )
            append_audit(
                conn,
                actor_user_id=admin_user["id"],
                action="user_deactivated",
                object_type="user",
                object_id=user_id,
                detail={"email": target["email"], "role": target["role"]},
            )
        updated = _get_user(conn, user_id)
    assert updated is not None
    return updated


# --- magic links -------------------------------------------------------------


def _redact(token: str) -> str:
    return f"***{token[-4:]}" if len(token) > 4 else "***"


def _decoy_link(base_url: str) -> str:
    """Same shape as a real link, backed by a token that was never stored."""
    return f"{base_url.rstrip('/')}/treasury/auth/{secrets.token_urlsafe(32)}"


def request_magic_link(
    email: str, base_url: Optional[str] = None, *, ip: Optional[str] = None
) -> Dict[str, Any]:
    """Issue a single-use login link. Never reveals whether the email exists.

    Returns `{"sent": True}` in production and `{"sent": True, "link": url}` in
    demo mode (settings.DEMO_MODE) so staging can be driven without mail.

    In production the link is delivered by `mailer.send_magic_link` when an SMTP
    relay is configured (MAZAPAY_TR_SMTP_*, see docs/treasury.md §11); otherwise
    it is logged with the token redacted. Delivery state is audited
    (`login_emailed` / `login_email_failed` / `login_link_logged`) but never
    returned, so the response cannot be used to enumerate accounts.
    """
    _ensure()
    addr = _norm_email(email)
    origin = (base_url or settings.BASE_URL or "").rstrip("/")
    now = _now()
    keys = _rate_keys(addr, ip)

    # Audit a refused attempt: the auditor needs to see the flood even though
    # the caller learns nothing (identical response shape).
    with _write_db() as conn:
        if not _rate_take(keys, now):
            append_audit(
                conn,
                actor_user_id=None,
                action="login_rate_limited",
                object_type="login",
                object_id=addr,
                detail={"code": "rate_limited", "email": addr, "ip": ip,
                        "window_s": RATE_LIMIT_WINDOW, "max": RATE_LIMIT_MAX},
            )
            return {"sent": True}

        user = _get_user_by_email(conn, addr) if _EMAIL_RE.match(addr) else None
        append_audit(
            conn,
            actor_user_id=user["id"] if user else None,
            action="login_requested",
            object_type="user",
            object_id=user["id"] if user else "",
            detail={"email": addr, "ip": ip, "known": bool(user)},
        )

        if not user or not user["active"]:
            # Unknown or deactivated: no token row, no mail, same answer.
            if settings.DEMO_MODE:
                return {"sent": True, "link": _decoy_link(origin)}
            return {"sent": True}

        token = secrets.token_urlsafe(32)
        conn.execute(
            "INSERT INTO tr_login_tokens (id, user_id, token_hash, created_ts,"
            " expires_ts, used_ts) VALUES (?,?,?,?,?,NULL)",
            (
                _new_id("mlt"),
                user["id"],
                _hash_token(token),
                now,
                now + float(settings.MAGIC_TTL),
            ),
        )

    link = f"{origin}/treasury/auth/{token}"
    if settings.DEMO_MODE:
        return {"sent": True, "link": link}
    _deliver(addr, link, token, user)
    return {"sent": True}


def _deliver(addr: str, link: str, token: str, user: Dict[str, Any]) -> None:
    """Hand the link to the mailer, or log it; never fail the request.

    Delivery state is audited (`login_emailed`, `login_email_failed`,
    `login_link_logged`) but never returned: a caller must not be able to learn
    whether an address exists or whether mail reached it.
    """
    from . import mailer  # local: keeps this module importable without SMTP config

    if not mailer.configured():
        LOG.info(
            "treasury magic link for %s: %s (SMTP not configured; token redacted)",
            addr,
            link.replace(token, _redact(token)),
        )
        with _write_db() as conn:
            append_audit(
                conn,
                actor_user_id=user["id"],
                action="login_link_logged",
                object_type="user",
                object_id=user["id"],
                detail={"delivery": "log"},
            )
        return
    try:
        mailer.send_magic_link(addr, link)
    except Exception as exc:  # noqa: BLE001 - delivery must never fail the request
        LOG.warning("treasury magic link delivery failed: %s", type(exc).__name__)
        with _write_db() as conn:
            append_audit(
                conn,
                actor_user_id=user["id"],
                action="login_email_failed",
                object_type="user",
                object_id=user["id"],
                detail={"delivery": "smtp", "error": type(exc).__name__},
            )
        return
    with _write_db() as conn:
        append_audit(
            conn,
            actor_user_id=user["id"],
            action="login_emailed",
            object_type="user",
            object_id=user["id"],
            detail={"delivery": "smtp"},
        )


def consume_magic_link(token: Optional[str]) -> Optional[Dict[str, Any]]:
    """Redeem a magic link exactly once.

    Returns `{"user": {...}, "session_token": "<raw>"}` or None. Expired,
    already-used, unknown and unknown-user tokens all return None (and are
    audited as `login_failed` with a machine reason).
    """
    _ensure()
    if not token or not isinstance(token, str):
        return None
    token_hash = _hash_token(token)
    now = _now()
    with _write_db() as conn:
        row = conn.execute(
            "SELECT * FROM tr_login_tokens WHERE token_hash=?", (token_hash,)
        ).fetchone()
        if row is None:
            append_audit(
                conn, actor_user_id=None, action="login_failed",
                object_type="login", object_id="",
                detail={"code": "invalid_token"},
            )
            return None
        if row["used_ts"] is not None:
            append_audit(
                conn, actor_user_id=row["user_id"], action="login_failed",
                object_type="login", object_id=row["id"],
                detail={"code": "token_used"},
            )
            return None
        if row["expires_ts"] <= now:
            append_audit(
                conn, actor_user_id=row["user_id"], action="login_failed",
                object_type="login", object_id=row["id"],
                detail={"code": "expired"},
            )
            return None
        # Compare-and-set: exactly one concurrent consumer wins the link.
        cur = conn.execute(
            "UPDATE tr_login_tokens SET used_ts=? WHERE id=? AND used_ts IS NULL",
            (now, row["id"]),
        )
        if cur.rowcount != 1:
            append_audit(
                conn, actor_user_id=row["user_id"], action="login_failed",
                object_type="login", object_id=row["id"],
                detail={"code": "token_used"},
            )
            return None
        user = _get_user(conn, row["user_id"])
        if not user or not user["active"]:
            append_audit(
                conn, actor_user_id=row["user_id"], action="login_failed",
                object_type="login", object_id=row["id"],
                detail={"code": "user_inactive"},
            )
            return None

    session_token = create_session(user["id"])
    with _write_db() as conn:
        append_audit(
            conn, actor_user_id=user["id"], action="login_ok",
            object_type="session", object_id="",
            detail={"email": user["email"], "role": user["role"]},
        )
    return {"user": user, "session_token": session_token}


# --- sessions ----------------------------------------------------------------


def _csrf_for(token_hash: str) -> str:
    """Deterministic per-session CSRF token (no column for it in the contract)."""
    key = hashlib.sha256(b"mazapay-treasury-csrf-v1").digest()
    return hmac.new(key, token_hash.encode("utf-8"), hashlib.sha256).hexdigest()


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def create_session(
    user_id: str, ip: Optional[str] = None, user_agent: Optional[str] = None
) -> str:
    """Open a session; returns the RAW cookie token (only its hash is stored)."""
    _ensure()
    token = secrets.token_urlsafe(32)
    now = _now()
    with _write_db() as conn:
        conn.execute(
            "INSERT INTO tr_sessions (id, user_id, token_hash, created_ts,"
            " expires_ts, revoked_ts, ip, user_agent) VALUES (?,?,?,?,?,NULL,?,?)",
            (
                _new_id("ses"),
                user_id,
                _hash_token(token),
                now,
                now + float(settings.SESSION_TTL),
                ip,
                user_agent,
            ),
        )
    return token


def resolve_session(token: Optional[str]) -> Optional[Dict[str, Any]]:
    """`{"user": {...}, "session": {...}}` for a live session, else None."""
    _ensure()
    if not token or not isinstance(token, str):
        return None
    token_hash = _hash_token(token)
    now = _now()
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM tr_sessions WHERE token_hash=?", (token_hash,)
        ).fetchone()
        if row is None or row["revoked_ts"] is not None:
            return None
        if row["expires_ts"] <= now:
            return None
        user = _get_user(conn, row["user_id"])
    if not user or not user["active"]:
        return None
    session = {
        "id": row["id"],
        "user_id": row["user_id"],
        "created_ts": row["created_ts"],
        "expires_ts": row["expires_ts"],
        "ip": row["ip"],
        "user_agent": row["user_agent"],
        "csrf": _csrf_for(token_hash),
    }
    return {"user": user, "session": session}


def revoke_session(token: Optional[str]) -> None:
    """Revoke a session. Idempotent: unknown or already-revoked tokens no-op."""
    _ensure()
    if not token or not isinstance(token, str):
        return
    token_hash = _hash_token(token)
    with _write_db() as conn:
        row = conn.execute(
            "SELECT * FROM tr_sessions WHERE token_hash=?", (token_hash,)
        ).fetchone()
        if row is None:
            return
        cur = conn.execute(
            "UPDATE tr_sessions SET revoked_ts=? WHERE id=? AND revoked_ts IS NULL",
            (_now(), row["id"]),
        )
        if cur.rowcount == 1:
            append_audit(
                conn, actor_user_id=row["user_id"], action="session_revoked",
                object_type="session", object_id=row["id"],
                detail={"ip": row["ip"]},
            )


def check_csrf(
    session: Optional[Dict[str, Any]],
    cookie_token: Optional[str],
    header_token: Optional[str],
) -> bool:
    """Constant-time CSRF check: cookie AND header must both carry the
    session's token. Missing either one fails; a mismatch in either fails."""
    if not session or not cookie_token or not header_token:
        return False
    expected = session.get("csrf")
    if not expected:
        token_hash = session.get("token_hash")
        if not token_hash:
            return False
        expected = _csrf_for(token_hash)
    if not isinstance(cookie_token, str) or not isinstance(header_token, str):
        return False
    ok_cookie = hmac.compare_digest(expected, cookie_token)
    ok_header = hmac.compare_digest(expected, header_token)
    return ok_cookie and ok_header


__all__ = [
    "ROLES",
    "TreasuryError",
    "init_db",
    "append_audit",
    "verify_audit_chain",
    "can",
    "may_sign",
    "bootstrap_admin",
    "create_user",
    "list_users",
    "deactivate_user",
    "request_magic_link",
    "consume_magic_link",
    "create_session",
    "resolve_session",
    "revoke_session",
    "new_csrf_token",
    "check_csrf",
    "reset_rate_limits",
]
