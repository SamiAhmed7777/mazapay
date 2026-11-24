"""Monthly statements, MIP-shaped CSV and payslip data (contract §11).

Rows are plain dicts so the HTTP layer can serve them as JSON verbatim; the
CSV writer emits the exact header MIP's transaction import expects and
neutralises every cell with :func:`mazapay.mip_export._safe_cell` — the same
guard the bridge's own export uses, because these files are opened in
MIP/Excel and a purpose like ``=SUM(A1:A2)`` must never execute.

Money stays a decimal string end to end: no ``float()`` anywhere on an amount.
"""
import csv
import datetime
import io
import re
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from ..mip_export import _safe_cell
from . import store
from .service import TreasuryError

CSV_HEADER = [
    "REF",
    "DATE",
    "ACCOUNT",
    "KIND",
    "STATUS",
    "USD",
    "MAZA",
    "RATE",
    "RATE_SOURCE",
    "TXIDS",
    "SIGNERS",
    "DESCRIPTION",
]

POOL_ACCOUNT_CODE = "POOL"
POOL_ACCOUNT_NAME = "Humanitarian pool"


def _utc(ts: Any) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(float(ts), datetime.timezone.utc)


def _iso_date(ts: Any) -> str:
    if not ts:
        return ""
    return _utc(ts).strftime("%Y-%m-%d")


def current_month(now: Optional[float] = None) -> str:
    moment = _utc(now if now is not None else datetime.datetime.now(datetime.timezone.utc).timestamp())
    return moment.strftime("%Y-%m")


def month_bounds(month: Optional[str] = None) -> Tuple[float, float]:
    """UTC [start, end) timestamps for ``YYYY-MM`` (default: this month)."""
    if not month:
        month = current_month()
    if not isinstance(month, str) or not re.fullmatch(r"\d{4}-\d{2}", month):
        raise ValueError("month must be formatted YYYY-MM")
    try:
        start = datetime.datetime.strptime(month, "%Y-%m").replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        raise ValueError("month must be formatted YYYY-MM") from None
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start.timestamp(), end.timestamp()


def resolve_month(month: Optional[str] = None) -> str:
    if not month:
        return current_month()
    month_bounds(month)  # validates
    return month


def _request_row(req: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "ref": req["ref"],
        "date": _iso_date(req.get("prepared_ts") or req.get("created_ts")),
        "account_code": req.get("account_code") or "",
        "account_name": req.get("account_name") or "",
        "kind": req["kind"],
        "status": req["status"],
        "usd": req["usd_total"],
        "maza": req.get("maza_total") or "",
        "rate": req.get("rate") or "",
        "rate_source": req.get("rate_source") or "",
        "txids": store.request_txids(req["id"]),
        "signers": store.signing_user_names(req["id"]),
        "purpose": req["purpose"],
    }


def _donation_row(donation: Dict[str, Any]) -> Dict[str, Any]:
    maza = donation.get("maza_amount") or ""
    rate = ""
    try:
        if maza and Decimal(maza) > 0:
            rate = str(
                (Decimal(donation["usd_amount"]) / Decimal(maza)).quantize(Decimal("0.00000001"))
            )
    except Exception:  # pragma: no cover - defensive, never blocks a statement
        rate = ""
    purpose = donation["donor_label"]
    if donation.get("note"):
        purpose = f"{purpose} — {donation['note']}"
    return {
        "ref": donation["id"],
        "date": _iso_date(donation["received_ts"]),
        "account_code": POOL_ACCOUNT_CODE,
        "account_name": POOL_ACCOUNT_NAME,
        "kind": "donation",
        "status": "received",
        "usd": donation["usd_amount"],
        "maza": maza,
        "rate": rate,
        "rate_source": "donation-quote",
        "txids": [donation["txid"]] if donation.get("txid") else [],
        "signers": [],
        "purpose": purpose,
    }


def statement_rows(*, kind: str = "requests", month: Optional[str] = None) -> List[Dict[str, Any]]:
    """Statement rows for the UTC month ``YYYY-MM`` (contract §11).

    ``kind='requests'`` covers every request whose ``prepared_ts`` falls in
    the month; ``kind='pool'`` covers pool donations by ``received_ts``.
    """
    start, end = month_bounds(month)
    if kind in ("requests", "request", None):
        return [_request_row(req) for req in store.requests_prepared_between(start, end)]
    if kind in ("pool", "donations", "donation"):
        return [_donation_row(don) for don in store.donations_between(start, end)]
    raise TreasuryError("invalid_kind", f"unknown statement kind {kind!r}", 400)


def _csv_row(row: Dict[str, Any]) -> Dict[str, str]:
    txids = "; ".join(row.get("txids") or [])
    signers = "; ".join(row.get("signers") or [])
    return {
        "REF": _safe_cell(row.get("ref", "")),
        "DATE": _safe_cell(row.get("date", "")),
        "ACCOUNT": _safe_cell(row.get("account_code", "")),
        "KIND": _safe_cell(row.get("kind", "")),
        "STATUS": _safe_cell(row.get("status", "")),
        "USD": _safe_cell(row.get("usd", "")),
        "MAZA": _safe_cell(row.get("maza", "")),
        "RATE": _safe_cell(row.get("rate", "")),
        "RATE_SOURCE": _safe_cell(row.get("rate_source", "")),
        "TXIDS": _safe_cell(txids),
        "SIGNERS": _safe_cell(signers),
        "DESCRIPTION": _safe_cell(row.get("purpose", "")),
    }


def statement_csv(rows: List[Dict[str, Any]]) -> str:
    """The statement rows as ``text/csv`` body with the pinned MIP header."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_HEADER, lineterminator="\r\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(_csv_row(row))
    return buffer.getvalue()


def csv_for_month(*, kind: str = "requests", month: Optional[str] = None) -> str:
    return statement_csv(statement_rows(kind=kind, month=month))


def payslip(item_id: str) -> Dict[str, Any]:
    """Everything the printable payslip needs for one paid item (§11)."""
    item = store.get_item(item_id)
    if not item:
        raise TreasuryError("not_found", f"payslip item {item_id!r} not found", 404)
    req = store.get_request_row(item["request_id"])
    if not req:
        raise TreasuryError("not_found", "payslip request not found", 404)
    acct = store.get_account(req["account_id"]) or {}
    date_ts = item.get("sent_ts") or item.get("created_ts")
    return {
        "item_id": item["id"],
        "request_id": req["id"],
        "ref": req["ref"],
        "recipient": item["payee_name"],
        "payee_address": item["payee_address"],
        "category": item.get("category") or "",
        "usd": item["usd_amount"],
        "maza": item.get("maza_amount") or "",
        "rate": req.get("rate") or "",
        "rate_source": req.get("rate_source") or "",
        "txid": item.get("txid") or "",
        "date": _iso_date(date_ts),
        "account_name": acct.get("name") or "",
        "account_code": acct.get("code") or "",
        "purpose": req["purpose"],
        "status": item["status"],
        "error": item.get("error") or "",
        "value_note": "USD value at receipt",
    }
