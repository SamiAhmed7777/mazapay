"""What a MAZA amount is approximately worth, in any world currency.

DISPLAY ONLY. Nothing here prices an order, locks a quote or moves money:
checkout amounts stay USD-quoted and MAZA-settled through ``pricing`` and
``store``. This module answers one human question — "what is this MAZA worth
in money I understand?" — for a balance on a treasury screen and for a payment
on the public checkout page.

    worth in CUR = MAZA amount x MAZA price in USD x (1 USD in CUR)

The MAZA price is the bridge's own price (``pricing.get_maza_usd`` — the same
figure that quotes a checkout), read through a short cache so a page view
never turns into a feed request. The fiat table comes from one source chosen
by the operator (``MAZAPAY_FX_SOURCE``); see ``config`` for the options.

Reading rules a page can rely on
- never blocks long: a refresh runs in ONE background thread (single flight);
  a caller says how long it will wait for a first value (``wait_seconds``),
  and that is one budget for the table and the price together, not one each.
  A cached table past its TTL is served at once while it refreshes.
- never shows a number without its age: each value carries the price time and
  the rate's own date, and ``stale`` is True once a cache is past its TTL.
- fails closed: a value older than its max age, a malformed document, a
  non-positive or non-finite rate, a wrong base currency — none becomes a
  number. A failed fetch is not retried for ``MAZAPAY_FX_RETRY`` seconds.
- untrusted input: the fetched document and every currency name are attacker
  text as far as this module is concerned. Rates are validated Decimals, codes
  match ``^[A-Z]{3}$``, strings are cut to a fixed length and lose control and
  formatting characters. Escaping is the caller's job; ``embed_json`` gives the
  checkout page a literal that is safe inside a ``<script>``.
"""

import datetime
import http.client
import json
import logging
import re
import threading
import time
import urllib.request
from decimal import (
    ROUND_HALF_UP,
    Decimal,
    DecimalException,
    InvalidOperation,
    localcontext,
)
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import config, pricing


class RateUnavailable(Exception):
    """No trustworthy fiat table (or MAZA price) to show a value with."""


_CODE = re.compile(r"^[A-Z]{3}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# A real table lists well over a hundred currencies. Fewer usable rates than
# this, or more than half of the document unusable, is not a published table.
_MIN_RATES = 20
_MAX_CURRENCIES = 500
_MAX_NAME = 48
_MAX_SYMBOL = 8
_MAX_WANTED = 40
_RATE_MIN = Decimal("0.000000001")
_RATE_MAX = Decimal("1000000000000")
_AMOUNT_MAX = Decimal("1000000000000000")
_MAZA_STEP = Decimal("0.00000001")
_PRICE_STEP = Decimal("0.00000001")
_CENTS = Decimal("0.01")
_TEN_THOUSANDTHS = Decimal("0.0001")
_ZERO = Decimal(0)
_ONE = Decimal(1)

PRICE_LABELS = {
    "paprika": "CoinPaprika",
    "lore": "CoinLore",
    "fixed": "Fixed price",
    "fixed-fallback": "Fixed price (feeds down)",
    "given": "Given price",
}
SOURCE_LABELS = {"frankfurter": "Frankfurter", "erapi": "ExchangeRate-API"}


def _label(table: Dict[str, str], key: str) -> str:
    return table.get(str(key or ""), str(key or ""))


def price_label(source: str) -> str:
    """Human name of the feed that served the MAZA price."""
    return _label(PRICE_LABELS, source)


def source_label(source: str) -> str:
    """Human name of the fiat-rate source."""
    return _label(SOURCE_LABELS, source)


class Table:
    """One fetched fiat table: how much of each currency 1 USD buys."""

    __slots__ = ("source", "fetched_at", "rates", "dates", "names", "symbols")

    def __init__(
        self,
        source: str,
        fetched_at: float,
        rates: Dict[str, Decimal],
        dates: Optional[Dict[str, str]] = None,
        names: Optional[Dict[str, str]] = None,
        symbols: Optional[Dict[str, str]] = None,
    ) -> None:
        rates = dict(rates)
        rates["USD"] = _ONE
        self.source = str(source)
        self.fetched_at = float(fetched_at)
        self.rates = rates
        self.dates = dict(dates or {})
        self.names = dict(names or {})
        self.symbols = dict(symbols or {})

    def codes(self) -> List[str]:
        """Offered currencies: USD first, then by name."""
        rest = [code for code in sorted(self.rates) if code != "USD"]
        rest.sort(key=lambda code: (self.names.get(code, code).casefold(), code))
        return ["USD"] + rest


# --- untrusted-document helpers ----------------------------------------------
def _text(raw: Any, limit: int) -> str:
    """A short printable string, or empty. Drops control, format and bidi
    characters (a currency name is rendered next to money amounts)."""
    if not isinstance(raw, str):
        return ""
    return "".join(ch for ch in raw if ch.isprintable()).strip()[:limit]


def _decimal(raw: Any) -> Optional[Decimal]:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = Decimal(str(raw).strip())
    except (DecimalException, ValueError, TypeError):
        return None
    return value if value.is_finite() else None


def _rate(raw: Any) -> Optional[Decimal]:
    value = _decimal(raw)
    if value is None or not (_RATE_MIN <= value <= _RATE_MAX):
        return None
    return value


def _date(raw: Any) -> str:
    if not isinstance(raw, str) or not _DATE.match(raw):
        return ""
    try:
        datetime.date.fromisoformat(raw)
    except ValueError:
        return ""
    return raw


def _accept(usable: int, total: int, what: str) -> None:
    if usable < _MIN_RATES or (total and usable * 2 < total):
        raise RateUnavailable(f"{what}: only {usable} of {total} entries usable")

# --- fetching ----------------------------------------------------------------
# Indirected so tests supply replies and a clock without a network.
_urlopen = urllib.request.urlopen
_monotonic = time.monotonic


def _fetch_json(url: str) -> object:
    """GET one small JSON document, or raise RateUnavailable. Bounded in time
    and size: a dribbling or oversized upstream costs one deadline, never a
    request thread."""
    timeout = max(1, int(config.FX_TIMEOUT_SECONDS))
    limit = max(1024, int(config.FX_MAX_BYTES))
    request = urllib.request.Request(
        url, headers={"User-Agent": "mazapay-bridge/0.1", "Accept": "application/json"}
    )
    deadline = _monotonic() + 2 * timeout
    size = 0
    chunks: List[bytes] = []
    try:
        with _urlopen(request, timeout=timeout) as resp:
            while size <= limit:
                if _monotonic() > deadline:
                    raise RateUnavailable("rate document took too long")
                chunk = resp.read(min(65536, limit + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
    except RateUnavailable:
        raise
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise RateUnavailable(f"rate source unreachable ({exc.__class__.__name__})") from None
    if size > limit:
        raise RateUnavailable("rate document too large")
    try:
        return json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise RateUnavailable("rate document is not JSON") from None


# --- parsing (every field is untrusted) --------------------------------------
def _parse_frankfurter_rates(data: object) -> Tuple[Dict[str, Decimal], Dict[str, str]]:
    """``[{"date": "2026-11-26", "base": "USD", "quote": "EUR", "rate": 0.91332}]``
    as served by Frankfurter v2 (shape read from the live API 2026-11-25)."""
    if not isinstance(data, list) or len(data) > _MAX_CURRENCIES:
        raise RateUnavailable("frankfurter rates: not a usable list")
    rates: Dict[str, Decimal] = {}
    dates: Dict[str, str] = {}
    for entry in data:
        if not isinstance(entry, dict) or entry.get("base") != "USD":
            continue
        code = entry.get("quote")
        value = _rate(entry.get("rate"))
        if not isinstance(code, str) or not _CODE.match(code) or value is None:
            continue
        if code == "USD" and value != _ONE:
            continue  # a dollar is a dollar; anything else is a wrong table
        rates[code] = value
        dates[code] = _date(entry.get("date"))
    _accept(len(rates), len(data), "frankfurter rates")
    return rates, dates


def _parse_frankfurter_currencies(data: object) -> Tuple[Dict[str, str], Dict[str, str]]:
    """``[{"iso_code": "EUR", "name": "Euro", "symbol": "€", "type": "currency"}]``
    Only ``currency`` entries are offered: a table that also quotes crypto and
    metals must not turn the picker into a coin-to-coin converter."""
    if not isinstance(data, list) or len(data) > _MAX_CURRENCIES:
        raise RateUnavailable("frankfurter currencies: not a usable list")
    names: Dict[str, str] = {}
    symbols: Dict[str, str] = {}
    for entry in data:
        if not isinstance(entry, dict) or entry.get("type") != "currency":
            continue
        code = entry.get("iso_code")
        if not isinstance(code, str) or not _CODE.match(code):
            continue
        names[code] = _text(entry.get("name"), _MAX_NAME)
        symbols[code] = _text(entry.get("symbol"), _MAX_SYMBOL)
    if len(names) < _MIN_RATES:
        raise RateUnavailable(f"frankfurter currencies: only {len(names)} currencies")
    return names, symbols


def _load_frankfurter(now: float) -> Table:
    base = config.FX_FRANKFURTER_URL.rstrip("/")
    rates, dates = _parse_frankfurter_rates(_fetch_json(base + "/v2/rates?base=USD"))
    names, symbols = _parse_frankfurter_currencies(_fetch_json(base + "/v2/currencies"))
    offered = sorted(code for code in names if code in rates)
    if "USD" not in offered:
        raise RateUnavailable("frankfurter currencies: no US Dollar")
    return Table(
        "frankfurter",
        now,
        {code: rates[code] for code in offered},
        {code: dates.get(code, "") for code in offered},
        {code: names[code] for code in offered},
        {code: symbols.get(code, "") for code in offered},
    )


def _load_erapi(now: float) -> Table:
    """``{"result": "success", "base_code": "USD", "time_last_update_unix": ...,
    "rates": {"AED": 3.6725, ...}}`` as served by open.er-api.com v6 (shape
    read from the live API 2026-11-25). Codes only: no names, one date for all."""
    data = _fetch_json(config.FX_ERAPI_URL)
    if (
        not isinstance(data, dict)
        or data.get("result") != "success"
        or data.get("base_code") != "USD"
        or not isinstance(data.get("rates"), dict)
        or len(data["rates"]) > _MAX_CURRENCIES
    ):
        raise RateUnavailable("er-api: not a success document in USD")
    day = ""
    stamp = _decimal(data.get("time_last_update_unix"))
    if stamp is not None and 0 < stamp < 32503680000:  # a plausible unix time
        try:
            day = (
                datetime.datetime.fromtimestamp(float(stamp), datetime.timezone.utc)
                .date()
                .isoformat()
            )
        except (OverflowError, OSError, ValueError):
            day = ""
    rates: Dict[str, Decimal] = {}
    dates: Dict[str, str] = {}
    for code, raw in data["rates"].items():
        value = _rate(raw)
        if not isinstance(code, str) or not _CODE.match(code) or value is None:
            continue
        if code == "USD" and value != _ONE:
            continue
        rates[code] = value
        dates[code] = day
    _accept(len(rates), len(data["rates"]), "er-api rates")
    return Table("erapi", now, rates, dates)


_LOADERS: Dict[str, Callable[[float], Table]] = {
    "frankfurter": _load_frankfurter,
    "erapi": _load_erapi,
}


def _loader() -> Callable[[float], Table]:
    """The configured source's loader, or RateUnavailable before any thread
    is started (feature off / misconfigured must not spawn refreshes)."""
    source = config.FX_SOURCE
    if source == "none":
        raise RateUnavailable("currency display is off")
    loader = _LOADERS.get(source)
    if loader is None:
        raise RateUnavailable(f"unknown MAZAPAY_FX_SOURCE {source!r}")
    return loader


# --- cache -------------------------------------------------------------------
# One clock for every age in this module (tests move it).
_clock = time.time


def _why(exc: BaseException) -> str:
    return f"{exc.__class__.__name__}: {exc}"[:160]


def _wait(wait_seconds: Any) -> float:
    """How long a caller may be made to wait: never negative, never NaN,
    never blocking on something that is not a number."""
    try:
        wait = max(0.0, float(wait_seconds or 0.0))
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if wait != wait else wait


class _Cached:
    """Single-flight background refresh with serve-stale and a failure memo.

    ``get`` returns ``(value, stale)``. ``stale`` is True whenever the value is
    past its TTL, so a caller shows its age and never a bare number. A value
    past ``max_age`` is never returned. With no usable value a caller waits at
    most ``wait_seconds`` for the refresh that is already running.
    """

    def __init__(
        self, name: str, ttl: int, max_age: int, retry: int, load: Callable[[float], Any]
    ) -> None:
        self.name = name
        self.ttl = max(0, int(ttl))
        self.max_age = max(self.ttl, int(max_age))
        self.retry = max(0, int(retry))
        self.load = load
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._value: Any = None
        self._at = 0.0
        self._next_try = 0.0
        self._busy = False
        self._error: Optional[str] = None

    def get(self, now: Optional[float] = None, wait_seconds: float = 0.0) -> Tuple[Any, bool]:
        now = _clock() if now is None else now
        wait = _wait(wait_seconds)
        with self._lock:
            value, at = self._value, self._at
            age = now - at
            if value is not None and 0 <= age < self.ttl:
                return value, False
            usable = value is not None and 0 <= age <= self.max_age
            if self._busy:
                done, owner = self._done, False
            elif now < self._next_try:
                if usable:
                    return value, True
                raise RateUnavailable(self._error or f"{self.name} is unavailable")
            else:
                self._busy = True
                self._done = threading.Event()
                done, owner = self._done, True
        if owner:
            threading.Thread(
                target=self._refresh, args=(done,), name=f"mazapay-{self.name}", daemon=True
            ).start()
        if usable:
            return value, True
        done.wait(wait)
        with self._lock:
            value, at, error = self._value, self._at, self._error
            # the clock moved on while waiting: a refresh that finished meanwhile is
            # stamped after the caller arrived, and that value must be accepted
            age = max(now, _clock()) - at
        if value is not None and 0 <= age <= self.max_age:
            return value, age >= self.ttl
        raise RateUnavailable(error or f"{self.name} is not ready yet")

    def _refresh(self, done: threading.Event) -> None:
        try:
            value = self.load(_clock())
        except Exception as exc:  # a display refresh never escapes its thread
            with self._lock:
                self._busy = False
                self._next_try = _clock() + self.retry
                self._error = _why(exc)
        else:
            with self._lock:
                self._busy = False
                self._value, self._at, self._next_try, self._error = value, _clock(), 0.0, None
        done.set()

    def clear(self) -> None:
        """Forget everything (tests). Only safe while no refresh is running."""
        with self._lock:
            self._value, self._at, self._next_try, self._error = None, 0.0, 0.0, None


def _load_price(now: float) -> Tuple[Decimal, str, float]:
    """The bridge's own MAZA price, cut to 8 decimals so every figure shown
    on a page is multiplied by the price the page prints."""
    try:
        rate, source = pricing.get_maza_usd()
    except pricing.PriceUnavailable as exc:
        raise RateUnavailable(f"MAZA price unavailable ({exc})") from None
    try:
        with localcontext() as ctx:
            ctx.prec = 60
            unit = rate.quantize(_PRICE_STEP)
    except DecimalException:
        raise RateUnavailable("MAZA price is out of range") from None
    if not unit.is_finite() or unit <= 0:
        raise RateUnavailable("MAZA price is not positive")
    return unit, str(source), now


_RATES = _Cached(
    "fx-rates",
    config.FX_CACHE_SECONDS,
    config.FX_MAX_AGE_SECONDS,
    config.FX_RETRY_SECONDS,
    lambda now: _loader()(now),
)
_PRICE = _Cached(
    "maza-price",
    config.WORTH_PRICE_CACHE_SECONDS,
    config.WORTH_PRICE_MAX_AGE_SECONDS,
    config.FX_RETRY_SECONDS,
    _load_price,
)


def reset() -> None:
    """Forget the cached table and price (tests, and a source switch)."""
    _RATES.clear()
    _PRICE.clear()


# --- figures -----------------------------------------------------------------
def _plain(value: Decimal) -> str:
    text = format(value, "f")  # never exponent notation
    return text.rstrip("0").rstrip(".") if "." in text else text


def format_amount(value: Any) -> str:
    """An approximate money figure: two decimals from 1 upward, four below
    that, four significant digits below 0.0001; grouped with commas.
    Locale-neutral on purpose — the caller puts the symbol or code beside it.
    Raises ValueError for anything that is not a finite number."""
    amount = value if isinstance(value, Decimal) else _decimal(value)
    if amount is None:
        raise ValueError("value must be a finite number")
    sign = "-" if amount < 0 else ""
    amount = abs(amount)
    if amount == 0:
        return "0.00"
    with localcontext() as ctx:
        ctx.prec = 60
        try:
            if amount >= _ONE:
                text = format(amount.quantize(_CENTS, rounding=ROUND_HALF_UP), ",.2f")
            elif amount >= _TEN_THOUSANDTHS:
                text = format(amount.quantize(_TEN_THOUSANDTHS, rounding=ROUND_HALF_UP), ",.4f")
            else:
                step = _ONE.scaleb(amount.adjusted() - 3)
                text = format(amount.quantize(step, rounding=ROUND_HALF_UP).normalize(), "f")
        except DecimalException:
            raise ValueError("value is out of range") from None
    return sign + text


def _amount(raw: Any) -> Decimal:
    if isinstance(raw, Decimal):
        amount = raw
    elif isinstance(raw, (str, int)) and not isinstance(raw, bool):
        try:
            amount = Decimal(str(raw).strip())
        except (DecimalException, ValueError):
            raise ValueError("maza_amount must be a number") from None
    else:
        raise ValueError("maza_amount must be a number")
    if not amount.is_finite():
        raise ValueError("maza_amount must be finite")
    if amount < 0:
        raise ValueError("maza_amount cannot be negative")
    try:
        with localcontext() as ctx:
            ctx.prec = 60
            amount = amount.quantize(_MAZA_STEP)  # MAZA has 8 decimals
    except DecimalException:
        raise ValueError("maza_amount is out of range") from None
    if amount > _AMOUNT_MAX:
        raise ValueError("maza_amount is too large")
    return _ZERO if amount == 0 else amount


def _wanted(currencies: Any) -> List[str]:
    if currencies is None:
        return ["USD"]
    if isinstance(currencies, str):
        items = [currencies]
    else:
        try:
            items = list(currencies)
        except TypeError:  # promised as ValueError, like every other bad argument
            raise ValueError("currency codes are three letters") from None
    wanted: List[str] = []
    for raw in items[: _MAX_WANTED + 1]:
        code = raw.strip().upper() if isinstance(raw, str) else ""
        if not _CODE.match(code):
            raise ValueError("currency codes are three letters")
        if code not in wanted:
            wanted.append(code)
    if len(wanted) > _MAX_WANTED:
        raise ValueError("too many currencies")
    return wanted


# --- public reading API ------------------------------------------------------
_LOG = logging.getLogger("mazapay.fx")
_LOG.addHandler(logging.NullHandler())  # library use stays silent unless configured
_WARN_LOCK = threading.Lock()
_WARNED: set = set()


def _warn_once(text: str) -> None:
    """One journal line per distinct unexpected failure, not one per page view."""
    with _WARN_LOCK:
        if text in _WARNED:
            return
        if len(_WARNED) > 50:
            _WARNED.clear()
        _WARNED.add(text)
        _LOG.warning("%s", text)


def table(now: Optional[float] = None, wait_seconds: float = 0.0) -> Table:
    """The fiat table: what 1 USD buys in each offered currency."""
    _loader()  # off or misconfigured fails here, before any thread starts
    return _RATES.get(now, wait_seconds)[0]


def price(
    now: Optional[float] = None, wait_seconds: float = 0.0
) -> Tuple[Decimal, str, float, bool]:
    """``(MAZA price in USD, feed name, fetched at, stale)``."""
    (unit, source, at), stale = _PRICE.get(now, wait_seconds)
    return unit, str(source), at, bool(stale)


def options(
    now: Optional[float] = None, wait_seconds: float = 0.0
) -> List[Tuple[str, str, str]]:
    """``(code, name, symbol)`` for a picker, USD first."""
    rates = table(now, wait_seconds)
    return [(code, rates.names.get(code, ""), rates.symbols.get(code, "")) for code in rates.codes()]


def pick(raw: Any, now: Optional[float] = None, wait_seconds: float = 0.0) -> str:
    """The offered currency that untrusted input names, else ``USD``. Never
    echoes the input: the result is always a code from the table."""
    code = raw.strip().upper()[:3] if isinstance(raw, str) else ""
    if not _CODE.match(code):
        return "USD"
    try:
        return code if code in table(now, wait_seconds).rates else "USD"
    except RateUnavailable:
        return "USD"


def _both(
    now: Optional[float] = None, wait_seconds: float = 0.0
) -> Tuple[Table, bool, Tuple[Decimal, str, float], bool]:
    """``(table, table stale, price, price stale)`` under ONE time budget.

    Both refreshes are started before either is waited for, and together the
    waits take at most ``wait_seconds``: the second gets only what the first
    left. Raises RateUnavailable when either cannot be shown.
    """
    budget = _wait(wait_seconds)
    started = _monotonic()
    fetched: List[Any] = []
    for cached in (_RATES, _PRICE):
        try:
            fetched.append(cached.get(now, 0.0))  # both refreshes start at once
        except RateUnavailable:
            fetched.append(None)
    for index, cached in enumerate((_RATES, _PRICE)):
        if fetched[index] is None:
            left = budget - (_monotonic() - started)
            fetched[index] = cached.get(now, max(0.0, left))
    rates, fx_stale = fetched[0]
    price, price_stale = fetched[1]
    return rates, fx_stale, price, price_stale


def worth(
    maza_amount: Any,
    currencies: Any = None,
    *,
    now: Optional[float] = None,
    wait_seconds: float = 0.0,
) -> Dict[str, Any]:
    """What a MAZA amount is approximately worth, in each requested currency.

    ``values[code]`` holds a formatted ``amount`` plus that currency's
    ``name``, ``symbol`` and the ``date`` of its rate. The result is JSON-safe
    (str / int / float / bool / dict only). Raises ValueError for a bad amount
    or code and RateUnavailable when no trustworthy figure can be shown.
    """
    _loader()
    amount = _amount(maza_amount)
    wanted = _wanted(currencies)
    rates, fx_stale, (unit, source, at), price_stale = _both(now, wait_seconds)
    usd = amount * unit
    values: Dict[str, Dict[str, str]] = {}
    for code in wanted:
        rate = rates.rates.get(code)
        if rate is None:
            raise RateUnavailable(f"{code} is not offered by the rate source")
        values[code] = {
            "code": code,
            "amount": format_amount(usd * rate),
            "name": rates.names.get(code, ""),
            "symbol": rates.symbols.get(code, ""),
            "date": rates.dates.get(code, ""),
        }
    return {
        "maza": _plain(amount),
        "usd": format_amount(usd),
        "price": _plain(unit),
        "price_source": source,
        "price_label": price_label(source),
        "price_at": at,
        "fx_source": rates.source,
        "fx_label": source_label(rates.source),
        "fx_fetched_at": rates.fetched_at,
        "stale": bool(fx_stale or price_stale),
        "values": values,
    }


def embed(
    now: Optional[float] = None, wait_seconds: float = 0.0
) -> Optional[Dict[str, Any]]:
    """Compact figures for the checkout page (``c`` currencies, ``r`` what 1
    USD buys in each, ``d`` each rate's date, ``p``/``s``/``t`` the MAZA price,
    its feed and time, ``st`` 1 when past TTL), or None when nothing can be
    shown. Never raises: the public page renders either way."""
    try:
        _loader()
        rates, fx_stale, (unit, source, at), price_stale = _both(now, wait_seconds)
    except RateUnavailable as exc:
        _LOG.debug("worth figures unavailable: %s", exc)
        return None
    except Exception as exc:  # a display bug must not take the page down
        _warn_once(f"worth figures failed unexpectedly: {_why(exc)}")
        return None
    codes = rates.codes()
    return {
        "p": _plain(unit),
        "s": source,
        "t": at,
        "f": rates.source,
        "st": int(bool(fx_stale or price_stale)),
        "c": [[code, rates.names.get(code, ""), rates.symbols.get(code, "")] for code in codes],
        "r": {code: format(rates.rates[code], "f") for code in codes},
        "d": {code: rates.dates.get(code, "") for code in codes},
    }


def embed_json(data: Any) -> str:
    """A JSON literal that cannot end a ``<script>`` element: ASCII-only with
    ``< > &`` escaped. ``None`` becomes ``null``."""
    if data is None:
        return "null"
    text = json.dumps(data, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


__all__ = [
    "RateUnavailable",
    "Table",
    "embed",
    "embed_json",
    "format_amount",
    "options",
    "pick",
    "price",
    "price_label",
    "reset",
    "source_label",
    "table",
    "worth",
]
