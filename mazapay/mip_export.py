"""MIP Fund Accounting export: CSV transaction-import sessions.

MIP ingests transactions via its Transaction Import module: the org's
consultant maps a CSV onto the deployment's import definition (journals,
cash receipts, AP invoices, cash disbursements...). This module emits two
session shapes:

- cash_receipts      — MAZA collections booked in USD equivalent
- cash_disbursements — vendor payments sent in MAZA, booked in USD

Idempotency and crash safety (three-phase claim/write/finalize):

1. CLAIM   — a single atomic UPDATE flips export_state NULL -> 'claimed'
   with the session's export_id, so concurrent exporters partition rows
   between them and no row is ever in two sessions. A crashed run leaves
   rows 'claimed' with an export_id; the next run ADOPTS that session id
   and rewrites the same session (rows whose status left the export set —
   e.g. a claimed late_paid order that got refunded meanwhile — are
   released back to unclaimed, never exported against their current
   state).
2. WRITE   — the CSV is written atomically (temp file + fsync + os.replace)
   under a collision-resistant name carrying the session id.
3. FINALIZE — one transaction flips 'claimed' -> 'written' with exported_ts.

Between claim and finalize a crash is recoverable (resume same session);
after finalize a re-run produces zero new rows, so repeated imports cannot
duplicate accounting transactions.

The include_exported=True reporting path reads without state changes.

Column layouts are configurable (MAZAPAY_MIP_RECEIPT_COLUMNS /
MAZAPAY_MIP_DISBURSEMENT_COLUMNS) because import definitions differ per
deployment. Every row carries the full audit trail in the description:
MAZA quantity, locked rate, rate source, and chain txid.
"""
import csv
import datetime
import os
import re
import uuid
from typing import Any, Dict, List

from . import config, store

# Spreadsheet formula injection: a cell whose text begins with '=', '+', '-'
# or '@' is evaluated as a formula by Excel/LibreOffice on open. MIP import
# rows carry operator/member-supplied text (vendor names, refs, txids), so
# every emitted cell is neutralized with the standard leading apostrophe —
# except plain numbers (AMOUNT/FX_RATE), which are always safe.
_FORMULA_PREFIXES = ("=", "+", "-", "@")
_PLAIN_NUMBER = re.compile(r"-?(?:\d+(?:\.\d+)?|\.\d+)")


def _safe_cell(value: Any) -> str:
    text = value if isinstance(value, str) else str(value)
    if text.startswith(_FORMULA_PREFIXES) and not _PLAIN_NUMBER.fullmatch(text):
        return "'" + text
    return text


def _trx_date(ts: Any) -> str:
    if not ts:
        return ""
    return datetime.datetime.fromtimestamp(float(ts), datetime.timezone.utc).strftime(
        "%Y-%m-%d"
    )


def _desc(order: Dict[str, Any]) -> str:
    bits = [
        f"MAZA {order.get('maza_amount')} @ {order.get('rate')} USD/MAZA",
        f"src={order.get('rate_source')}",
    ]
    if order.get("txid"):
        bits.append(f"txid={order['txid']}")
    if order.get("ref"):
        bits.append(f"ref={order['ref']}")
    return " | ".join(bits)


def _rows(orders: List[Dict[str, Any]], columns: List[str]) -> List[Dict[str, str]]:
    vendor_names = {}
    try:
        vendor_names = {v["id"]: v for v in store.list_vendors()}
    except Exception:
        vendor_names = {}
    out: List[Dict[str, str]] = []
    for o in orders:
        vendor = vendor_names.get(o.get("vendor_id") or "", {})
        payor = (
            vendor.get("name")
            or (o.get("vendor_id") and f"vendor {o['vendor_id']}")
            or o.get("ref")
            or "member"
        )
        row = {
            "SESSION_ID": o["id"],
            "TRX_DATE": _trx_date(o.get("paid_ts") or o.get("created_ts")),
            "BANK_CODE": config.MIP_DEFAULT_BANK_CODE,
            "PAYOR": payor,
            "VENDOR": vendor.get("code") or payor,
            "REFERENCE": o.get("txid") or o["id"],
            "GL_ACCOUNT": config.MIP_DEFAULT_GL_ACCOUNT,
            "AMOUNT": f"{float(o['usd_amount']):.2f}",
            "CURRENCY": config.MIP_CURRENCY_CODE,
            "FX_RATE": "1.0000",
            "DESCRIPTION": _desc(o),
        }
        out.append({col: _safe_cell(row.get(col, "")) for col in columns})
    return out


def _write_csv_atomic(path: str, columns: List[str], rows: List[Dict[str, str]]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{uuid.uuid4().hex[:8]}"
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    # fsync the directory so the rename itself survives power loss
    try:
        dirfd = os.open(os.path.dirname(path), os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    except OSError:
        pass  # not all platforms support directory fsync; best effort


def _export(
    kind: str,
    statuses: List[str],
    columns: List[str],
    stem: str,
    include_exported: bool,
) -> Dict[str, Any]:
    resumed = False
    if include_exported:
        # Reporting/read-only path: no claim, no state changes, fresh id.
        export_id = uuid.uuid4().hex[:12]
        orders = [o for o in store.list_orders(kind=kind) if o["status"] in statuses]
        rows = _rows(orders, columns)
    else:
        # Adopt only a STALE claim (crashed run past its lease), atomically:
        # adopt_stale_claim renews the lease via compare-and-set, so exactly
        # one recovery worker can adopt a given session even under
        # concurrency. The session id is the FILENAME, so a resumed session
        # rewrites the SAME file — a crash after write but before finalize
        # can never leave two importable CSVs for the same rows.
        export_id = store.adopt_stale_claim(kind, config.MIP_CLAIM_ADOPT_SECONDS)
        if export_id:
            resumed = True
        else:
            export_id = uuid.uuid4().hex[:12]
        orders = store.claim_unexported(kind, statuses, export_id)
        rows = _rows(orders, columns)
    # Deterministic per-session filename (id = filename), unique by session id.
    path = os.path.join(config.MIP_EXPORT_DIR, f"{stem}_{export_id}.csv")
    _write_csv_atomic(path, columns, rows)
    if not include_exported:
        store.finalize_export(export_id)
    return {
        "export_id": export_id,
        "path": path,
        "rows": len(rows),
        "columns": columns,
        "order_ids": [o["id"] for o in orders],
        "resumed_claim": resumed,
        "skipped_already_exported": not include_exported,
    }


def export_cash_receipts(include_exported: bool = False) -> Dict[str, Any]:
    return _export(
        "in",
        ["paid", "late_paid"],
        config.CASH_RECEIPT_COLUMNS,
        "mip_cash_receipts",
        include_exported,
    )


def export_cash_disbursements(include_exported: bool = False) -> Dict[str, Any]:
    return _export(
        "out",
        ["sent"],
        config.CASH_DISBURSEMENT_COLUMNS,
        "mip_cash_disbursements",
        include_exported,
    )
