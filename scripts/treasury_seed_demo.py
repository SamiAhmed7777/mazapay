#!/usr/bin/env python3
"""Idempotent demo seeder for the MazaPay treasury workspace.

    python3 scripts/treasury_seed_demo.py --db /tmp/treasury-demo.sqlite3

Builds the organisation shown on the treasury concept board: six roles, five
accounts, four payees, a donor-funded humanitarian pool, one completed
dual-signed drawdown, one drawdown waiting on its second signature, one
prepared elder-stipend run and four completed stipend payslips.

Every object is written with a *deterministic* id (sha256 of a stable key,
truncated to 12 hex — same shape as store._new_id) and inserted with
INSERT OR IGNORE, so re-running the script never duplicates anything and
never rewrites a row an operator has since edited.

Refuses to run against production:
  * a path inside the bridge's own /var/lib/mazapay needs
    --i-know-this-is-not-production (a sibling such as
    /var/lib/mazapay-treasury is a different service and is allowed);
  * a database holding real bridge orders (orders.kind = 'in') needs --force.

Degrades instead of crashing while sibling lanes are still in flight: the
treasury store/auth/service modules are imported optionally. Missing ones are
reported under "skipped" and the seeder falls back to contract-shaped inserts
(CONTRACT-treasury.md §4), which is the same schema store.init_db() creates.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from decimal import Decimal

# Demo quote rate. 48,200.00 USD / 0.00125 = 38,560,000.00 MAZA — the figure
# on the concept board. Pinned here (not read from the price feed) so the seed
# is reproducible offline; the server's own quotes are labelled separately.
RATE = Decimal("0.00125")
RATE_SOURCE = "fixed-demo"
CENT = Decimal("0.01")
PROD_MARKER = "/var/lib/mazapay"


def _under_production(path: str) -> bool:
    """True only for the bridge's own state dir, not sibling directories.

    Path-boundary aware: /var/lib/mazapay/... is production, while
    /var/lib/mazapay-treasury/... is a separate service's state dir.
    """
    resolved = os.path.realpath(path)
    return resolved == PROD_MARKER or resolved.startswith(PROD_MARKER + os.sep)
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Running this file by path puts scripts/ on sys.path[0], not the repo root —
# add the root so `import mazapay...` resolves exactly as it does for the
# server (`python3 -m mazapay.treasury.server`).
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

_AUDIT_GENESIS = "0" * 64
# Set by main() when mazapay.treasury.store is importable: its append_audit
# owns the chain, so seeded rows verify with store.verify_chain().
AUDIT_STORE = None


def _canonical(obj) -> str:
    # Byte-identical to mazapay.treasury.store._canonical (CONTRACT §4), so a
    # fallback-written row verifies with the real store's verify_chain().
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def append_audit(conn, *, ts, actor_user_id, action, object_type, object_id, detail,
                 store=None):
    """Append one row to tr_audit, chaining per CONTRACT §4."""
    body = detail if isinstance(detail, dict) else ({} if detail is None else {"value": detail})
    store = AUDIT_STORE if store is None else store
    if store is not None and hasattr(store, "append_audit"):
        return store.append_audit(
            conn, actor_user_id=actor_user_id, action=action, object_type=object_type,
            object_id=object_id, detail=body, ts=ts,
        )
    row = conn.execute("SELECT hash FROM tr_audit ORDER BY id DESC LIMIT 1").fetchone()
    prev = (row[0] if row else None) or _AUDIT_GENESIS
    canonical = _canonical(
        {
            "ts": ts,
            "actor_user_id": actor_user_id,
            "action": action,
            "object_type": object_type,
            "object_id": object_id,
            "detail": body,
        }
    )
    digest = hashlib.sha256((prev + "\n" + canonical).encode()).hexdigest()
    conn.execute(
        "INSERT INTO tr_audit(ts, actor_user_id, action, object_type, object_id,"
        " detail, prev_hash, hash) VALUES (?,?,?,?,?,?,?,?)",
        (ts, actor_user_id, action, object_type, object_id, _canonical(body), prev, digest),
    )
    return digest


def money(value) -> str:
    return str(Decimal(value).quantize(CENT))


def maza_for(usd) -> str:
    return str((Decimal(usd) / RATE).quantize(CENT))


def sid(prefix: str, key: str) -> str:
    """Deterministic id in the store's prefix_<12 hex> shape."""
    return f"{prefix}_{hashlib.sha256(key.encode()).hexdigest()[:12]}"


def txid(key: str) -> str:
    return hashlib.sha256(("tx:" + key).encode()).hexdigest()


# --- schema fallback ------------------------------------------------------
# Mirrors CONTRACT-treasury.md §4 exactly. Only used when
# mazapay.treasury.store is not importable (lane 1 still writing); once the
# real store lands, init_db() takes over and this block is inert.
FALLBACK_SCHEMA = """
CREATE TABLE IF NOT EXISTS tr_users(id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL, role TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
    created_ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tr_login_tokens(id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE, created_ts REAL NOT NULL, expires_ts REAL NOT NULL,
    used_ts REAL);
CREATE TABLE IF NOT EXISTS tr_sessions(id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE, created_ts REAL NOT NULL, expires_ts REAL NOT NULL,
    revoked_ts REAL, ip TEXT, user_agent TEXT);
CREATE TABLE IF NOT EXISTS tr_accounts(id TEXT PRIMARY KEY, name TEXT NOT NULL,
    code TEXT UNIQUE NOT NULL, kind TEXT NOT NULL, maza_address TEXT,
    status TEXT NOT NULL DEFAULT 'open', signature_policy TEXT NOT NULL DEFAULT 'dual',
    cap_per_request_usd TEXT, cap_daily_usd TEXT, award_ref TEXT, notes TEXT,
    created_ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tr_payees(id TEXT PRIMARY KEY, name TEXT NOT NULL,
    code TEXT UNIQUE NOT NULL, maza_address TEXT NOT NULL, category TEXT, notes TEXT,
    created_ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tr_requests(id TEXT PRIMARY KEY, ref TEXT UNIQUE NOT NULL,
    account_id TEXT NOT NULL, kind TEXT NOT NULL, purpose TEXT NOT NULL, award_ref TEXT,
    note TEXT, status TEXT NOT NULL, usd_total TEXT NOT NULL, maza_total TEXT, rate TEXT,
    rate_source TEXT, rate_locked_ts REAL, rate_expires_ts REAL,
    prepared_by TEXT NOT NULL, prepared_ts REAL NOT NULL, processed_by TEXT,
    processed_ts REAL, submitted_ts REAL, authorized_ts REAL, completed_ts REAL,
    decline_reason TEXT, created_ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tr_request_items(id TEXT PRIMARY KEY, request_id TEXT NOT NULL,
    payee_id TEXT, payee_name TEXT NOT NULL, payee_address TEXT NOT NULL, category TEXT,
    usd_amount TEXT NOT NULL, maza_amount TEXT, status TEXT NOT NULL DEFAULT 'pending',
    payment_id TEXT, txid TEXT, error TEXT, sent_ts REAL, created_ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tr_signatures(id TEXT PRIMARY KEY, request_id TEXT NOT NULL,
    user_id TEXT NOT NULL, role_at_signing TEXT NOT NULL, decision TEXT NOT NULL,
    note TEXT, signed_ts REAL NOT NULL, record_hash TEXT NOT NULL,
    UNIQUE(request_id, user_id, decision));
CREATE TABLE IF NOT EXISTS tr_pool_donations(id TEXT PRIMARY KEY, donor_label TEXT NOT NULL,
    usd_amount TEXT NOT NULL, maza_amount TEXT, txid TEXT, note TEXT,
    received_ts REAL NOT NULL, recorded_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tr_audit(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
    actor_user_id TEXT, action TEXT NOT NULL, object_type TEXT NOT NULL,
    object_id TEXT NOT NULL, detail TEXT NOT NULL, prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS tr_audit_no_update BEFORE UPDATE ON tr_audit
    BEGIN SELECT RAISE(ABORT, 'tr_audit is append-only'); END;
CREATE TRIGGER IF NOT EXISTS tr_audit_no_delete BEFORE DELETE ON tr_audit
    BEGIN SELECT RAISE(ABORT, 'tr_audit is append-only'); END;
CREATE INDEX IF NOT EXISTS tr_requests_account_status ON tr_requests(account_id, status);
CREATE INDEX IF NOT EXISTS tr_requests_prepared_ts ON tr_requests(prepared_ts);
CREATE INDEX IF NOT EXISTS tr_request_items_request ON tr_request_items(request_id);
CREATE INDEX IF NOT EXISTS tr_signatures_request ON tr_signatures(request_id);
CREATE INDEX IF NOT EXISTS tr_audit_object ON tr_audit(object_type, object_id);
CREATE INDEX IF NOT EXISTS tr_sessions_token ON tr_sessions(token_hash);
"""


# --- data -----------------------------------------------------------------

ROLES = [
    ("executive", "executive@example.org", "Aiyana Redcloud"),
    ("council_treasurer", "council.treasurer@example.org", "Marcus Bell"),
    ("treasurer", "treasurer@example.org", "Dana Whitfield"),
    ("finance_officer", "finance.officer@example.org", "Priya Nayar"),
    ("viewer", "viewer@example.org", "Read-only Auditor"),
]

ACCOUNTS = [
    # (code, name, kind, policy, status, cap_per_request, cap_daily, award_ref, notes)
    ("OPS-CASH", "USD cash leg", "operating", "single", "banking_disrupted", None, None, None,
     "Operating cash swept to MAZA while the bank leg is disrupted."),
    ("OPS-GEN", "General Fund", "operating", "single", "open", "25000.00", "50000.00", None,
     "Day-to-day operating account; single-signature policy."),
    ("DRW-ROADS", "Roads & Facilities", "drawdown", "dual", "open", "75000.00", "150000.00", None,
     "Capital drawdown account; executive + council treasurer."),
    ("FED-HHS", "Health & Human Services", "federal_oversight", "dual", "open", None, None,
     "PL 93-638", "Federal award funds are documentary: requests authorise, nothing is broadcast."),
    ("POOL-HUM", "Humanitarian Pool", "pool", "single", "open", None, None, None,
     "Donor-funded pool: elder stipends and family support. Single-signature "
     "(treasurer) in the demo so stipend runs settle end to end."),
]

PAYEES = [
    ("Northline Construction", "PAY-NORTH", "vendor", "MZq7Rd2xVf4Lm8Kp1sTn6Yh3Bc9Wd5Eg"),
    ("Cedar Valley Health Clinic", "PAY-CEDAR", "health", "MZt4Hn8Jm2Qp6Rs1Vx5Za9Bc3Df7Gh1K"),
    ("Willow Bend Family Services", "PAY-WILLOW", "family", "MZk2Lp6Nq8Rt4Vx1Za5Bc9Df3Gh7Jm1P"),
    ("Elder Support Collective", "PAY-ELDER", "elder", "MZf9Gh3Jk7Lm1Np5Qr8St2Vx6Za4Bc9D"),
]

DONATIONS = [
    ("Mesa Community Trust", "2500.00", "General appeal, Q3"),
    ("Anonymous (matching)", "2000.00", "Match on health outreach"),
    ("Riverbend Credit Union", "1500.00", "Elder heating fund"),
    ("Two Rivers Casino Gives", "1250.00", "Winter stipend drive"),
    ("Kestrel Family Foundation", "1750.00", "Family emergency relief"),
    ("Individual donors (pool)", "1300.00", "Small gifts consolidated"),
]

POOL_TXIDS = {
    "Mesa Community Trust": "8f1c2a9d4b6e0f37a5c81d29e4b7f60c3a9d5e8b1f4c7a2d6e9b0c3f5a8d1e4b",
    "Anonymous (matching)": "2d7e4b1f8a5c9e6d3b0f7a4c1e8d5b2f9a6c3e0d7b4f1a8c5e2d9b6f3a0c7e4d",
    "Riverbend Credit Union": "6a3f0c7d4b1e8a5f2c9d6b3e0a7f4c1d8b5e2a9f6c3d0b7e4a1f8c5d2b9e6a3f",
    "Two Rivers Casino Gives": "9c6d3a0f7b4e1c8d5a2f9b6e3c0d7a4f1b8e5c2d9a6f3b0e7c4d1a8f5b2e9c6d",
    "Kestrel Family Foundation": "4e1b8c5d2a9f6b3e0c7d4a1f8b5e2c9d6a3f0b7e4c1d8a5f2b9e6c3d0a7f4b1e",
    "Individual donors (pool)": "7b4e1c8d5a2f9b6e3c0d7a4f1b8e5c2d9a6f3b0e7c4d1a8f5b2e9c6d3a0f7b4e",
}


# --- capability probing ---------------------------------------------------

def probe():
    """Import the treasury modules that exist *right now*; never raise."""
    caps = {"store": None, "auth": None, "service": None, "skipped": []}
    try:
        from mazapay.treasury import store as tr_store  # noqa: F401

        caps["store"] = tr_store
    except Exception as exc:  # ImportError today; any error is "not usable yet"
        caps["skipped"].append(f"mazapay.treasury.store ({exc.__class__.__name__}: {exc})")
    try:
        from mazapay.treasury import auth as tr_auth  # noqa: F401

        if hasattr(tr_auth, "init_db") and hasattr(tr_auth, "bootstrap_admin"):
            caps["auth"] = tr_auth
        else:
            caps["skipped"].append("mazapay.treasury.auth (missing init_db/bootstrap_admin)")
    except Exception as exc:
        caps["skipped"].append(f"mazapay.treasury.auth ({exc.__class__.__name__}: {exc})")
    try:
        from mazapay.treasury import service as tr_service  # noqa: F401

        needed = ("create_request", "process_request", "submit_request",
                  "sign_request", "complete_request", "record_donation")
        missing = [n for n in needed if not hasattr(tr_service, n)]
        if missing:
            caps["skipped"].append(f"mazapay.treasury.service (missing {', '.join(missing)})")
        else:
            caps["service"] = tr_service
    except Exception as exc:
        caps["skipped"].append(f"mazapay.treasury.service ({exc.__class__.__name__}: {exc})")
    try:
        from mazapay import store as bridge_store

        caps["bridge_store"] = bridge_store
    except Exception as exc:
        caps["bridge_store"] = None
        caps["skipped"].append(f"mazapay.store ({exc.__class__.__name__}: {exc})")
    return caps


# --- low-level writers ----------------------------------------------------

def insert_ignore(conn, sql: str, params: tuple) -> bool:
    cur = conn.execute(sql, params)
    return cur.rowcount > 0


# --- seeding steps --------------------------------------------------------

def seed_users(conn, caps, now):
    """Admin from env, then one user per role. Returns ({role: user}, created)."""
    admin_email = os.environ.get("MAZAPAY_TR_ADMIN_EMAIL", "admin@example.org").strip()
    admin_name = os.environ.get("MAZAPAY_TR_ADMIN_NAME", "Demo Administrator").strip()
    people, created = {}, 0

    if caps["auth"] is not None:
        admin = caps["auth"].bootstrap_admin(admin_email, admin_name)
    else:
        admin_id = sid("usr", admin_email)
        if insert_ignore(
            conn,
            "INSERT OR IGNORE INTO tr_users(id,email,name,role,active,created_ts)"
            " VALUES (?,?,?,?,1,?)",
            (admin_id, admin_email, admin_name, "admin", now),
        ):
            created += 1
            append_audit(conn, ts=now, actor_user_id=None, action="user_created",
                         object_type="user", object_id=admin_id,
                         detail={"email": admin_email, "role": "admin", "source": "seed"})
        admin = {"id": admin_id, "email": admin_email, "name": admin_name, "role": "admin"}
    people["admin"] = admin

    for role, email, name in ROLES:
        if caps["auth"] is not None:
            try:
                user = caps["auth"].create_user(admin, email=email, name=name, role=role)
            except Exception as exc:
                # Already exists on a re-run is the normal case for some
                # implementations; fall back to a store-level lookup/insert.
                row = conn.execute(
                    "SELECT id,email,name,role FROM tr_users WHERE email=?", (email,)
                ).fetchone()
                if row is None:
                    raise
                user = dict(row)
                people[role] = user
                continue
            people[role] = user
            created += 1
            continue
        uid = sid("usr", email)
        if insert_ignore(
            conn,
            "INSERT OR IGNORE INTO tr_users(id,email,name,role,active,created_ts)"
            " VALUES (?,?,?,?,1,?)",
            (uid, email, name, role, now),
        ):
            created += 1
            append_audit(conn, ts=now, actor_user_id=admin["id"], action="user_created",
                         object_type="user", object_id=uid,
                         detail={"email": email, "role": role, "source": "seed"})
        people[role] = {"id": uid, "email": email, "name": name, "role": role}
    return people, created


def seed_accounts(conn, actor_id, now):
    """No account-creation call exists in the service API (§7) — store-level."""
    created = 0
    ids = {}
    for code, name, kind, policy, status, cap_req, cap_day, award, notes in ACCOUNTS:
        aid = sid("acc", code)
        ids[code] = aid
        if insert_ignore(
            conn,
            "INSERT OR IGNORE INTO tr_accounts(id,name,code,kind,maza_address,status,"
            "signature_policy,cap_per_request_usd,cap_daily_usd,award_ref,notes,created_ts)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, name, code, kind, f"MZ{sid('', code)[1:]}addr{code.lower().replace('-', '')}",
             status, policy, cap_req, cap_day, award, notes, now),
        ):
            created += 1
            append_audit(conn, ts=now, actor_user_id=actor_id, action="account_created",
                         object_type="account", object_id=aid,
                         detail={"code": code, "kind": kind, "policy": policy, "source": "seed"})
    return ids, created


def seed_payees(conn, actor_id, now):
    """No payee call exists in the service API (§7) — store-level."""
    created = 0
    ids = {}
    for name, code, category, address in PAYEES:
        pid = sid("pay", code)
        ids[code] = pid
        if insert_ignore(
            conn,
            "INSERT OR IGNORE INTO tr_payees(id,name,code,maza_address,category,notes,created_ts)"
            " VALUES (?,?,?,?,?,?,?)",
            (pid, name, code, address, category, "Demo payee", now),
        ):
            created += 1
            append_audit(conn, ts=now, actor_user_id=actor_id, action="payee_created",
                         object_type="payee", object_id=pid,
                         detail={"code": code, "category": category, "source": "seed"})
    return ids, created


def seed_donations(conn, caps, people, now):
    """service.record_donation when shipped, else store-level + chained audit."""
    created = 0
    treasurer = people["treasurer"]
    # The service opens its own BEGIN IMMEDIATE connection; releasing our write
    # transaction first is what keeps this from deadlocking against it.
    conn.commit()
    for idx, (donor, usd, note) in enumerate(DONATIONS):
        received = now - (len(DONATIONS) - idx) * 86400
        # Idempotency first: record_donation has no natural key, so the check
        # must happen BEFORE the call or a re-run would bank the gift twice.
        existing = conn.execute(
            "SELECT 1 FROM tr_pool_donations WHERE donor_label=? AND usd_amount=?",
            (donor, usd),
        ).fetchone()
        if existing:
            continue
        if caps["service"] is not None:
            before = conn.execute("SELECT COUNT(*) FROM tr_pool_donations").fetchone()[0]
            try:
                caps["service"].record_donation(
                    treasurer, donor_label=donor, usd_amount=usd,
                    txid=POOL_TXIDS[donor], note=note,
                )
            except Exception:
                try:
                    caps["service"].record_donation(
                        treasurer, donor_label=donor, usd_amount=Decimal(usd),
                        txid=POOL_TXIDS[donor], note=note,
                    )
                except Exception:
                    pass  # fall through to the store-level insert below
            after = conn.execute("SELECT COUNT(*) FROM tr_pool_donations").fetchone()[0]
            if after > before:
                created += 1
                continue
        did = sid("don", donor)
        if insert_ignore(
            conn,
            "INSERT OR IGNORE INTO tr_pool_donations(id,donor_label,usd_amount,maza_amount,"
            "txid,note,received_ts,recorded_by) VALUES (?,?,?,?,?,?,?,?)",
            (did, donor, usd, maza_for(usd), POOL_TXIDS[donor], note, received,
             treasurer["id"]),
        ):
            created += 1
            append_audit(conn, ts=received, actor_user_id=treasurer["id"],
                         action="donation_recorded", object_type="donation", object_id=did,
                         detail={"donor": donor, "usd": usd, "maza": maza_for(usd),
                                 "rate": str(RATE), "source": "seed"})
    return created


# --- requests -------------------------------------------------------------

def _items_for(specs, payees):
    out = []
    for payee_code, usd, label in specs:
        name, _, category, address = next(p for p in PAYEES if p[1] == payee_code)
        out.append({
            "payee_id": payees[payee_code],
            "payee_name": label or name,
            "payee_address": address,
            "category": category,
            "usd_amount": usd,
        })
    return out


REQUEST_SPECS = {
    # ref: header fields + item list
    "DR-2027-0141": {
        "account": "DRW-ROADS", "kind": "drawdown", "status": "completed",
        "purpose": "Road resurfacing — Phase 2 (2027)", "award_ref": None,
        "note": "Completed dual-signed drawdown (demo).",
        "preparer": "finance_officer", "signers": ["executive", "council_treasurer"],
        "items": [("PAY-NORTH", "18000.00", None),
                  ("PAY-WILLOW", "12500.00", "Access road & easement works")],
    },
    "DR-2027-0142": {
        "account": "DRW-ROADS", "kind": "drawdown", "status": "pending_signatures",
        "purpose": "Bridge deck replacement — Crow Creek crossing", "award_ref": None,
        "note": "Executive signed; council treasurer signature pending.",
        "preparer": "treasurer", "signers": ["executive"],
        "items": [("PAY-NORTH", "32000.00", None),
                  ("PAY-WILLOW", "16200.00", "Right-of-way support")],
    },
    "HSR-0088": {
        "account": "POOL-HUM", "kind": "stipend_run", "status": "prepared",
        "purpose": "Elder winter stipends — December 2027", "award_ref": None,
        "note": "Prepared, not yet processed (no rate locked).",
        "preparer": "treasurer", "signers": [],
        "items": [("PAY-ELDER", "250.00", f"Elder stipend #{i:02d}") for i in range(1, 13)],
    },
}

PAYSLIP_SPECS = [
    # ref, recipient label, usd, payee code
    ("PS-0138", "Elder stipend — M. Whitehorse", "250.00", "PAY-ELDER"),
    ("PS-0139", "Elder stipend — R. Stonecipher", "300.00", "PAY-ELDER"),
    ("PS-0140", "Elder stipend — L. Bighorn", "250.00", "PAY-ELDER"),
    ("PS-0141", "Elder stipend — A. Killsnight", "200.00", "PAY-ELDER"),
]


def _signature_hash(request_id, user_id, decision, ts):
    return hashlib.sha256(f"{request_id}|{user_id}|{decision}|{ts}".encode()).hexdigest()


def seed_requests_raw(conn, people, accounts, payees, now):
    """Contract-shaped writes: same rows the state machine would leave behind."""
    created = 0
    day = 86400.0
    for offset, (ref, spec) in enumerate(REQUEST_SPECS.items()):
        rid = sid("req", ref)
        items = _items_for(spec["items"], payees)
        usd_total = sum((Decimal(i["usd_amount"]) for i in items), Decimal("0"))
        done = spec["status"] == "completed"
        prepared_ts = now - (8 - offset) * day
        prepared_by = people[spec["preparer"]]["id"]
        state = spec["status"]
        locked = state in ("pending_signatures", "authorized", "completed")
        row = (
            rid, ref, accounts[spec["account"]], spec["kind"], spec["purpose"],
            spec["award_ref"], spec["note"], state, money(usd_total),
            maza_for(usd_total) if locked else None,
            str(RATE) if locked else None, RATE_SOURCE if locked else None,
            prepared_ts + 60 if locked else None, prepared_ts + 960 if locked else None,
            prepared_by, prepared_ts,
            prepared_by if state != "prepared" else None,
            prepared_ts + 120 if state != "prepared" else None,
            prepared_ts + 180 if state != "prepared" else None,
            prepared_ts + 240 if state in ("authorized", "completed") else None,
            prepared_ts + 420 if done else None,
            None, prepared_ts,
        )
        if not insert_ignore(
            conn,
            "INSERT OR IGNORE INTO tr_requests(id,ref,account_id,kind,purpose,award_ref,note,"
            "status,usd_total,maza_total,rate,rate_source,rate_locked_ts,rate_expires_ts,"
            "prepared_by,prepared_ts,processed_by,processed_ts,submitted_ts,authorized_ts,"
            "completed_ts,decline_reason,created_ts) VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            row,
        ):
            continue
        created += 1
        append_audit(conn, ts=prepared_ts, actor_user_id=prepared_by, action="request_created",
                     object_type="request", object_id=rid,
                     detail={"ref": ref, "usd": money(usd_total), "kind": spec["kind"],
                             "account": spec["account"], "source": "seed"})
        if state != "prepared":
            append_audit(conn, ts=prepared_ts + 120, actor_user_id=prepared_by,
                         action="request_processed", object_type="request", object_id=rid,
                         detail={"ref": ref, "maza": maza_for(usd_total), "rate": str(RATE),
                                 "rate_source": RATE_SOURCE})
            append_audit(conn, ts=prepared_ts + 180, actor_user_id=prepared_by,
                         action="request_submitted", object_type="request", object_id=rid,
                         detail={"ref": ref})
        for n, item in enumerate(items, start=1):
            iid = sid("itm", f"{ref}:{n}:{item['payee_name']}")
            sent = done
            conn.execute(
                "INSERT OR IGNORE INTO tr_request_items(id,request_id,payee_id,payee_name,"
                "payee_address,category,usd_amount,maza_amount,status,payment_id,txid,error,"
                "sent_ts,created_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,?,?)",
                (iid, rid, item["payee_id"], item["payee_name"], item["payee_address"],
                 item["category"], money(item["usd_amount"]),
                 maza_for(item["usd_amount"]) if locked else None,
                 "sent" if sent else "pending",
                 sid("pay", f"{ref}:{n}") if sent else None,
                 txid(f"{ref}:{n}") if sent else None,
                 prepared_ts + 400 if sent else None, prepared_ts),
            )
        for signer in spec["signers"]:
            user = people[signer]
            signed = prepared_ts + 240
            conn.execute(
                "INSERT OR IGNORE INTO tr_signatures(id,request_id,user_id,role_at_signing,"
                "decision,note,signed_ts,record_hash) VALUES (?,?,?,?,?,?,?,?)",
                (sid("sig", f"{ref}:{signer}"), rid, user["id"], user["role"], "approve",
                 "Approved (demo)", signed,
                 _signature_hash(rid, user["id"], "approve", signed)),
            )
            append_audit(conn, ts=signed, actor_user_id=user["id"], action="signature_approved",
                         object_type="request", object_id=rid,
                         detail={"ref": ref, "role": user["role"], "counted": True})
        if spec["signers"]:
            append_audit(conn, ts=prepared_ts + 240, actor_user_id=None,
                         action="request_authorized" if done else "signature_pending",
                         object_type="request", object_id=rid,
                         detail={"ref": ref, "signatures": len(spec["signers"]), "quorum": 2})
        if done:
            append_audit(conn, ts=prepared_ts + 420, actor_user_id=people["treasurer"]["id"],
                         action="request_completed", object_type="request", object_id=rid,
                         detail={"ref": ref, "usd": money(usd_total),
                                 "maza": maza_for(usd_total), "items_sent": len(items)})

    # Four completed stipend payslips in the pool (single-item stipend runs).
    for n, (ref, label, usd, payee_code) in enumerate(PAYSLIP_SPECS):
        rid = sid("req", ref)
        ts0 = now - (5 - n) * day
        if not insert_ignore(
            conn,
            "INSERT OR IGNORE INTO tr_requests(id,ref,account_id,kind,purpose,award_ref,note,"
            "status,usd_total,maza_total,rate,rate_source,rate_locked_ts,rate_expires_ts,"
            "prepared_by,prepared_ts,processed_by,processed_ts,submitted_ts,authorized_ts,"
            "completed_ts,decline_reason,created_ts) VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, ref, accounts["POOL-HUM"], "stipend_run", label, None,
             "Completed pool payslip (demo).", "completed", money(usd), maza_for(usd),
             str(RATE), RATE_SOURCE, ts0 + 60, ts0 + 960,
             people["finance_officer"]["id"], ts0,
             people["finance_officer"]["id"], ts0 + 90, ts0 + 120, ts0 + 180,
             ts0 + 300, None, ts0),
        ):
            continue
        created += 1
        name, _, category, address = next(p for p in PAYEES if p[1] == payee_code)
        iid = sid("itm", f"{ref}:1")
        conn.execute(
            "INSERT OR IGNORE INTO tr_request_items(id,request_id,payee_id,payee_name,"
            "payee_address,category,usd_amount,maza_amount,status,payment_id,txid,error,"
            "sent_ts,created_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,?,?)",
            (iid, rid, payees[payee_code], label, address, category, money(usd),
             maza_for(usd), "sent", sid("pay", f"{ref}:1"), txid(f"{ref}:1"),
             ts0 + 290, ts0),
        )
        sig_ts = ts0 + 180
        conn.execute(
            "INSERT OR IGNORE INTO tr_signatures(id,request_id,user_id,role_at_signing,"
            "decision,note,signed_ts,record_hash) VALUES (?,?,?,?,?,?,?,?)",
            (sid("sig", f"{ref}:treasurer"), rid, people["treasurer"]["id"], "treasurer",
             "approve", "Approved (demo)", sig_ts,
             _signature_hash(rid, people["treasurer"]["id"], "approve", sig_ts)),
        )
        append_audit(conn, ts=ts0, actor_user_id=people["finance_officer"]["id"],
                     action="request_created", object_type="request", object_id=rid,
                     detail={"ref": ref, "usd": money(usd), "kind": "stipend_run",
                             "account": "POOL-HUM", "source": "seed"})
        append_audit(conn, ts=sig_ts, actor_user_id=None, action="request_authorized",
                     object_type="request", object_id=rid,
                     detail={"ref": ref, "signatures": 1, "quorum": 1})
        append_audit(conn, ts=ts0 + 300, actor_user_id=people["treasurer"]["id"],
                     action="request_completed", object_type="request", object_id=rid,
                     detail={"ref": ref, "usd": money(usd), "maza": maza_for(usd),
                             "payslip_item_id": iid})
    return created


def seed_requests_service(caps, conn, people, accounts, payees, now):
    """Drive the shipped service layer; raises on any mismatch so the caller
    can clean up and fall back to contract-shaped inserts."""
    svc = caps["service"]
    created = 0
    made_ids = []

    for ref, spec in REQUEST_SPECS.items():
        if conn.execute("SELECT 1 FROM tr_requests WHERE ref=?", (ref,)).fetchone():
            continue  # re-run: the pinned ref is already there
        items = _items_for(spec["items"], payees)
        req = svc.create_request(
            people[spec["preparer"]], account_id=accounts[spec["account"]],
            kind=spec["kind"], purpose=spec["purpose"], items=items,
            award_ref=spec["award_ref"], note=spec["note"],
        )
        rid = req["id"]
        made_ids.append((rid, ref))
        created += 1
        if spec["status"] != "prepared":
            svc.process_request(people[spec["preparer"]], rid)
            svc.submit_request(people[spec["preparer"]], rid)
            for signer in spec["signers"]:
                svc.sign_request(people[signer], rid, decision="approve", note="Approved (demo)")
        if spec["status"] == "completed":
            svc.complete_request(people["treasurer"], rid)

    for ref, label, usd, payee_code in PAYSLIP_SPECS:
        if conn.execute("SELECT 1 FROM tr_requests WHERE ref=?", (ref,)).fetchone():
            continue  # re-run
        items = _items_for([(payee_code, usd, label)], payees)
        # Prepared by the finance officer, signed by the treasurer: the service
        # enforces separation of duties, so one person cannot do both.
        req = svc.create_request(
            people["finance_officer"], account_id=accounts["POOL-HUM"],
            kind="stipend_run", purpose=label, items=items,
            note="Completed pool payslip (demo).",
        )
        svc.process_request(people["finance_officer"], req["id"])
        svc.submit_request(people["finance_officer"], req["id"])
        svc.sign_request(people["treasurer"], req["id"], decision="approve", note="Approved (demo)")
        svc.complete_request(people["treasurer"], req["id"])
        made_ids.append((req["id"], ref))
        created += 1

    # The service mints its own refs; pin the concept-board refs afterwards
    # (caller does it with UPDATE — never DELETE — so the audit chain stays
    # append-only).
    return created, made_ids


def cleanup_partial(conn, made_ids):
    """Remove half-built requests from a failed service run (audit untouched)."""
    for rid, _ref in made_ids:
        conn.execute("DELETE FROM tr_request_items WHERE request_id=?", (rid,))
        conn.execute("DELETE FROM tr_signatures WHERE request_id=?", (rid,))
        conn.execute("DELETE FROM tr_requests WHERE id=?", (rid,))


def pin_refs(conn, made_ids):
    for rid, ref in made_ids:
        conn.execute("UPDATE tr_requests SET ref=? WHERE id=?", (ref, rid))


# --- summary --------------------------------------------------------------

def summary(conn):
    tables = ["tr_users", "tr_accounts", "tr_payees", "tr_pool_donations",
              "tr_requests", "tr_request_items", "tr_signatures", "tr_audit"]
    out = []
    for t in tables:
        try:
            out.append((t, conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]))
        except sqlite3.Error:
            out.append((t, "missing"))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Seed an idempotent MazaPay treasury demo organisation.",
        epilog="Run only against a throwaway database; production paths are refused.",
    )
    ap.add_argument("--db", help="sqlite path (default: $MAZAPAY_TR_DB, else ./mazapay-demo.sqlite3)")
    ap.add_argument("--force", action="store_true",
                    help="seed even though the database already holds real bridge orders")
    ap.add_argument("--i-know-this-is-not-production", action="store_true",
                    help="required for any path under /var/lib/mazapay")
    args = ap.parse_args(argv)

    db = args.db or os.environ.get("MAZAPAY_TR_DB") or os.path.join(REPO_ROOT, "mazapay-demo.sqlite3")
    db = os.path.abspath(os.path.expanduser(db))

    if _under_production(db) and not args.i_know_this_is_not_production:
        print(f"refusing: {db} resolves under {PROD_MARKER} (production).\n"
              "Pass --i-know-this-is-not-production only for a deliberately disposable copy.",
              file=sys.stderr)
        return 2
    if os.path.realpath(db) == os.path.realpath(os.path.join(
            os.path.dirname(REPO_ROOT), "mazapay.sqlite3")):
        print(f"refusing: {db} is the repository's working database.", file=sys.stderr)
        return 2

    # Pin the demo quote rate before any treasury module reads settings, so a
    # service-layer quote matches the numbers printed below.
    os.environ["MAZAPAY_TR_DB"] = db
    # Same sqlite file as the bridge (CONTRACT §3): completion records its
    # disbursement in orders, so the bridge must point at the demo DB too —
    # otherwise a seed run would write mock orders into /tmp/mazapay-dev.sqlite3.
    os.environ["MAZAPAY_DB"] = db
    os.environ.setdefault("MAZAPAY_PRICE_SOURCE", "fixed")
    os.environ.setdefault("MAZAPAY_FIXED_USD_PRICE", str(RATE))
    os.environ.setdefault("MAZAPAY_TR_DEMO_MODE", "1")
    # The concept board's drawdowns are larger than the $10k per-payment cap the
    # bridge applies to POST /payments; raise it for the seed process only, so
    # the demo's completed drawdown really completes instead of going partial.
    os.environ.setdefault("MAZAPAY_MAX_DISBURSE_USD", "100000")

    parent = os.path.dirname(db)
    if parent:
        os.makedirs(parent, exist_ok=True)

    caps = probe()
    global AUDIT_STORE

    AUDIT_STORE = caps["store"]
    try:
        from mazapay import store as bridge_store  # noqa: F811

        caps["bridge_store"] = bridge_store
    except Exception:
        caps["bridge_store"] = caps.get("bridge_store")

    conn = (caps["bridge_store"].connect(db) if caps["bridge_store"] is not None
            else sqlite3.connect(db))
    conn.row_factory = sqlite3.Row

    notes, created = [], {}

    # --- real-data guard ---------------------------------------------------
    has_orders = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='orders'"
    ).fetchone()
    real_orders = 0
    if has_orders:
        real_orders = conn.execute(
            "SELECT COUNT(*) FROM orders WHERE kind='in'"
        ).fetchone()[0]
    if real_orders and not args.force:
        print(f"refusing: {db} holds {real_orders} bridge order(s) with kind='in' "
              "(looks like a live database).\nUse --force only on a copy you can throw away.",
              file=sys.stderr)
        conn.close()
        return 3

    # --- schema ------------------------------------------------------------
    schema_source = "mazapay.treasury.store.init_db()"
    if caps["store"] is not None:
        try:
            caps["store"].init_db()
        except Exception as exc:
            notes.append(f"store.init_db() failed ({exc.__class__.__name__}: {exc}); "
                         "using the contract-shaped fallback schema")
            conn.executescript(FALLBACK_SCHEMA)
            schema_source = "fallback DDL (CONTRACT §4)"
    else:
        conn.executescript(FALLBACK_SCHEMA)
        schema_source = "fallback DDL (CONTRACT §4) — mazapay.treasury.store not importable"
    if caps["auth"] is not None:
        try:
            caps["auth"].init_db()
        except Exception as exc:
            notes.append(f"auth.init_db() failed ({exc.__class__.__name__}: {exc})")
    if caps["bridge_store"] is not None:
        try:
            caps["bridge_store"].init_db(db)
        except Exception as exc:
            notes.append(f"mazapay.store.init_db() failed ({exc.__class__.__name__}: {exc})")

    now = time.time()
    before = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("tr_users", "tr_accounts", "tr_payees",
                      "tr_pool_donations", "tr_requests")
    }
    people, created["users"] = seed_users(conn, caps, now)
    accounts, created["accounts"] = seed_accounts(conn, people["admin"]["id"], now)
    payees, created["payees"] = seed_payees(conn, people["admin"]["id"], now)
    conn.commit()  # release the write lock before any service-layer call
    created["donations"] = seed_donations(conn, caps, people, now)
    conn.commit()

    if caps["service"] is not None:
        made = []
        try:
            created["requests"], made = seed_requests_service(
                caps, conn, people, accounts, payees, now)
            pin_refs(conn, made)
        except Exception as exc:
            notes.append(f"service-layer seeding failed ({exc.__class__.__name__}: {exc}); "
                         "cleaned up and used store-level inserts")
            cleanup_partial(conn, made)
            created["requests"] = seed_requests_raw(conn, people, accounts, payees, now)
    else:
        created["requests"] = seed_requests_raw(conn, people, accounts, payees, now)
        notes.append("requests seeded at store level (service.py not available yet); "
                     "rows match the state machine's shapes")

    conn.commit()

    # Report real deltas (bootstrap_admin, for one, is created outside the
    # per-role loop), so "created this run" is measured, not asserted.
    created = {
        "users": conn.execute("SELECT COUNT(*) FROM tr_users").fetchone()[0] - before["tr_users"],
        "accounts": conn.execute("SELECT COUNT(*) FROM tr_accounts").fetchone()[0] - before["tr_accounts"],
        "payees": conn.execute("SELECT COUNT(*) FROM tr_payees").fetchone()[0] - before["tr_payees"],
        "donations": conn.execute("SELECT COUNT(*) FROM tr_pool_donations").fetchone()[0] - before["tr_pool_donations"],
        "requests": conn.execute("SELECT COUNT(*) FROM tr_requests").fetchone()[0] - before["tr_requests"],
    }

    # --- report ------------------------------------------------------------
    print(f"treasury demo seed — {db}")
    print(f"schema: {schema_source}")
    if real_orders:
        print(f"note: --force used on a database holding {real_orders} bridge order(s)")
    print()
    print(f"{'created this run':<22}{'value'}")
    for key in ("users", "accounts", "payees", "donations", "requests"):
        print(f"{key:<22}{created[key]}")
    print()
    print(f"{'table':<22}{'rows'}")
    for table, count in summary(conn):
        print(f"{table:<22}{count}")
    print()
    balance = conn.execute(
        "SELECT COALESCE(SUM(CAST(usd_amount AS REAL)),0) FROM tr_pool_donations"
    ).fetchone()[0]
    print(f"pool donations total  {money(balance)} USD @ {RATE} USD/MAZA")
    if AUDIT_STORE is not None and hasattr(AUDIT_STORE, "verify_chain"):
        ok, broken = AUDIT_STORE.verify_chain()
        print(f"audit hash chain      {'intact' if ok else 'BROKEN at row ' + str(broken)}")
    pending = conn.execute(
        "SELECT ref, usd_total FROM tr_requests WHERE status='pending_signatures' ORDER BY ref"
    ).fetchall()
    for row in pending:
        print(f"awaiting signature    {row['ref']} — {row['usd_total']} USD")
    for ref in list(REQUEST_SPECS) + [spec[0] for spec in PAYSLIP_SPECS]:
        row = conn.execute(
            "SELECT r.id, r.status, (SELECT id FROM tr_request_items i"
            " WHERE i.request_id=r.id ORDER BY i.id LIMIT 1) AS item_id"
            " FROM tr_requests r WHERE r.ref=?", (ref,),
        ).fetchone()
        if not row:
            continue
        label = "payslip" if ref.startswith("PS-") else "request"
        line = f"{label:<21} {ref}  {row['status']:<18} /treasury/requests/{row['id']}"
        if row["item_id"]:
            line += f"  payslip /treasury/payslips/{row['item_id']}"
        print(line)

    if notes:
        print()
        print("notes:")
        for note in notes:
            print(f"  - {note}")
    if caps["skipped"]:
        print()
        print("skipped capabilities (sibling lanes not on disk at seed time):")
        for item in caps["skipped"]:
            print(f"  - {item}")

    print()
    print("start the server:")
    print(f"  MAZAPAY_TR_DB={db} MAZAPAY_TR_DEMO_MODE=1 "
          "MAZAPAY_TR_COOKIE_SECURE=0 python3 -m mazapay.treasury.server")
    admin_email = os.environ.get("MAZAPAY_TR_ADMIN_EMAIL", "admin@example.org")
    print("open http://127.0.0.1:18501/treasury/login in a browser, then mint a")
    print("sign-in link for the demo user (there is no mail delivery yet):")
    print(f"  MAZAPAY_TR_DB={db} MAZAPAY_TR_DEMO_MODE=1 python3 -c "
          "\"from mazapay.treasury import auth; "
          f"print(auth.request_magic_link('{admin_email}', "
          "'http://127.0.0.1:18501')['link'])\"")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
