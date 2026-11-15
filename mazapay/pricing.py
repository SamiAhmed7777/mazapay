"""MAZA/USD price feed with explicit, auditable rate sourcing.

Primary source: CoinPaprika public ticker (no API key). Optional CoinLore
fallback, and an explicit fixed-price operating mode for offline runs/tests.

Money math is Decimal end to end: quotes are computed in Decimal and only
converted to float at the storage boundary (SQLite REAL columns), so the
rounding semantics are exact decimal ROUND_DOWN, not binary-float artifacts.

Fallback policy (auditable, not silent): if the primary source fails, the
synthetic fixed price is used ONLY when PRICE_SOURCE is already 'fixed'
(explicit mode) or MAZAPAY_ALLOW_FIXED_PRICE is enabled — and the resulting
quotes are labeled rate_source='fixed-fallback' so the store, audit trail,
and MIP export rows reveal exactly which quotes were priced synthetically.
Every order carries its own rate + rate source for the audit trail and the
MIP FX columns (IRS Notice 2014-21 / ASU 2023-08 treatment).
"""
import json
import urllib.request
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Optional, Tuple

from . import config


class PriceUnavailable(Exception):
    pass


def _fetch_json(url: str, timeout: Optional[int] = None) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": "mazapay-bridge/0.1"})
    with urllib.request.urlopen(req, timeout=timeout or config.PRICE_TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _price_paprika() -> Tuple[Decimal, str]:
    # Live shape (verified 2026-11-14):
    # {"id":"maza-maza", ..., "quotes":{"USD":{"price":0.001745..., "volume_24h":166.89, ...}}}
    data = _fetch_json(config.PAPRIKA_TICKER_URL)
    price: Optional[Decimal] = None
    if isinstance(data, dict):
        quotes = data.get("quotes")
        usd = quotes.get("USD", {}) if isinstance(quotes, dict) else {}
        raw = usd.get("price")
        try:
            price = Decimal(str(raw)) if raw is not None else None
        except InvalidOperation:
            price = None
    if not price or price <= 0:
        raise PriceUnavailable(f"paprika returned price={price!r}")
    return price, "paprika"


def _price_lore() -> Tuple[Decimal, str]:
    if not config.LORE_TICKER_URL:
        raise PriceUnavailable("lore source not configured")
    data = _fetch_json(config.LORE_TICKER_URL)
    if isinstance(data, list) and data and data[0].get("price_usd"):
        return Decimal(str(data[0]["price_usd"])), "lore"
    raise PriceUnavailable(f"lore returned {data!r}")


def _price_fixed() -> Tuple[Decimal, str]:
    if config.FIXED_USD_PRICE <= 0:
        raise PriceUnavailable("fixed price not configured")
    return Decimal(str(config.FIXED_USD_PRICE)), "fixed"


_SOURCES = {
    "paprika": _price_paprika,
    "lore": _price_lore,
    "fixed": _price_fixed,
}


def get_maza_usd() -> Tuple[Decimal, str]:
    """Return (price_usd, source), trying sources in configured order.

    A failed primary source falls back to the synthetic fixed price only
    when the mode is auditable (explicit 'fixed' source, or the operator
    enabled MAZAPAY_ALLOW_FIXED_PRICE). Otherwise this raises so no
    real-wallet quote is ever priced from a stale synthetic rate silently.
    """
    primary = config.PRICE_SOURCE
    if primary not in _SOURCES:
        raise PriceUnavailable(f"unknown PRICE_SOURCE {primary!r}")
    try:
        return _SOURCES[primary]()
    except (PriceUnavailable, OSError, ValueError, json.JSONDecodeError) as exc:
        if primary == "fixed":
            return _price_fixed()  # explicit fixed mode; re-raises if unconfigured
        if config.ALLOW_FIXED_PRICE:
            price, _src = _price_fixed()
            return price, "fixed-fallback"  # auditable synthetic-rate label
        raise PriceUnavailable(f"price source {primary!r} failed: {exc}") from exc


def quote_maza(usd_amount, price: Optional[Decimal] = None) -> Tuple[Decimal, Decimal, str]:
    """Lock a quote: (maza_amount, rate, source).

    maza_amount is rounded DOWN to the configured granularity (exact Decimal
    ROUND_DOWN). Honest semantics note: rounding the token amount down
    slightly under-collects MAZA relative to the exact USD/price ratio — the
    bridge prices conservatively for the payer, and the locked rate column
    records the precise ratio used.
    """
    usd = Decimal(str(usd_amount))
    if usd <= 0:
        raise ValueError("usd_amount must be positive")
    if price is None:
        rate, source = get_maza_usd()
    else:
        rate, source = Decimal(str(price)), "given"
    granularity = Decimal(10) ** -int(config.MAZA_DECIMALS)
    maza = (usd / rate).quantize(granularity, rounding=ROUND_DOWN)
    if maza <= 0:
        raise ValueError("usd_amount too small for MAZA granularity")
    return maza, rate, source
