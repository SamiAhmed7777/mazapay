"""Tests for ``mazapay.fx`` — the display-only "what is this MAZA worth" figures.

No network: every reply is supplied through ``fx._urlopen``, a cache's load
function or a patched price feed. ``fixtures/fx_live_sample_2026-11-25.json``
holds verbatim excerpts of the three replies the parsers were written against
(captured 2026-11-25 UTC; its ``_note`` explains why most rows are dated a day
later). Expected figures are literals worked out independently of the module.
"""
import datetime
import http.client
import json
import logging
import threading
import time
import unittest
import urllib.error
import warnings
from decimal import Decimal
from pathlib import Path
from unittest import mock

from mazapay import config, fx, pricing  # noqa: E402

_LIVE = json.loads(
    (Path(__file__).with_name("fixtures") / "fx_live_sample_2026-11-25.json").read_text(
        encoding="utf-8"
    )
)
_FF_RATES = _LIVE["frankfurter_rates"]
_FF_CURRENCIES = _LIVE["frankfurter_currencies"]
_ERAPI = _LIVE["erapi"]
FIAT = 35  # world currencies in the sample (it also carries BTC and XAU)
DAY = "2026-11-26"
# Twenty-five real ISO codes for synthetic tables (counted).
_CODES = (
    "AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND BOB "
    "BRL BSD BTN BWP BYN BZD"
).split()


def _rows(count=len(_CODES), extra=()):
    """``count`` clean rate entries plus whatever ``extra`` holds."""
    clean = [
        {"date": DAY, "base": "USD", "quote": code, "rate": 1 + i / 4}
        for i, code in enumerate(_CODES[:count])
    ]
    return clean + list(extra)


def _erapi_body(rates=None, **fields):
    body = dict(_ERAPI)
    body["rates"] = dict(_ERAPI["rates"]) if rates is None else rates
    body.update(fields)
    return body


class _Clock:
    """A movable stand-in for ``fx._clock``."""

    def __init__(self, start=1795600000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class _Reply:
    """A file-like HTTP reply that hands out its body in fixed-size pieces."""

    def __init__(self, body, piece=1 << 16):
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.piece = piece
        self.pos = 0
        self.reads = 0

    def read(self, size=-1):
        self.reads += 1
        if self.pos >= len(self.body):
            return b""
        end = self.pos + (self.piece if size < 0 else min(size, self.piece))
        out, self.pos = self.body[self.pos:end], end
        return out

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _ConfigCase(unittest.TestCase):
    """Restores the config values a test is allowed to change."""

    _saved = (
        "FX_SOURCE",
        "FX_FRANKFURTER_URL",
        "FX_ERAPI_URL",
        "FX_TIMEOUT_SECONDS",
        "FX_MAX_BYTES",
    )

    def setUp(self):
        self._before = {name: getattr(config, name) for name in self._saved}
        self.addCleanup(self._restore)

    def _restore(self):
        for name, value in self._before.items():
            setattr(config, name, value)


# --- parsing -----------------------------------------------------------------
class FrankfurterParserTests(_ConfigCase):
    def test_reads_the_live_document_and_keeps_each_rate_s_own_date(self):
        rates, dates = fx._parse_frankfurter_rates(_FF_RATES)
        self.assertEqual(rates["EUR"], Decimal("0.91332"))
        self.assertEqual(dates["EUR"], DAY)
        self.assertEqual(rates["JPY"], Decimal("159.34"))
        self.assertEqual(rates["USD"], Decimal("1.0"))
        self.assertEqual(rates["INR"], Decimal("92.5"))
        self.assertEqual(rates["BTC"], Decimal("0.000016666"))
        self.assertEqual(set(dates.values()) - {"2026-11-25", DAY}, set())

    def test_offers_fiat_only_with_names_and_symbols(self):
        with mock.patch.object(fx, "_fetch_json", side_effect=[_FF_RATES, _FF_CURRENCIES]) as fetch:
            table = fx._load_frankfurter(500.0)
        self.assertEqual(
            [call.args[0] for call in fetch.call_args_list],
            [
                config.FX_FRANKFURTER_URL + "/v2/rates?base=USD",
                config.FX_FRANKFURTER_URL + "/v2/currencies",
            ],
        )
        self.assertEqual(table.source, "frankfurter")
        self.assertEqual(table.fetched_at, 500.0)
        codes = table.codes()
        self.assertEqual(len(codes), FIAT)
        self.assertEqual(codes[0], "USD")
        self.assertEqual(codes[1:], sorted(codes[1:], key=lambda code: table.names[code].casefold()))
        for not_money in ("BTC", "XAU"):
            self.assertNotIn(not_money, table.rates)  # quoted in the document, never offered
        self.assertEqual(table.names["USD"], "United States Dollar")
        self.assertEqual(table.symbols["USD"], "$")
        self.assertEqual(table.names["JPY"], "Japanese Yen")
        self.assertEqual(table.symbols["INR"], "₹")
        self.assertTrue(all(table.dates[code] for code in codes))

    def test_a_trailing_slash_on_the_configured_address_is_harmless(self):
        config.FX_FRANKFURTER_URL = "https://fx.internal.example/"
        with mock.patch.object(fx, "_fetch_json", side_effect=[_FF_RATES, _FF_CURRENCIES]) as fetch:
            fx._load_frankfurter(0.0)
        self.assertEqual(
            [call.args[0] for call in fetch.call_args_list],
            [
                "https://fx.internal.example/v2/rates?base=USD",
                "https://fx.internal.example/v2/currencies",
            ],
        )

    def test_a_table_without_the_us_dollar_is_refused(self):
        currencies = [e for e in _FF_CURRENCIES if e["iso_code"] != "USD"]
        with mock.patch.object(fx, "_fetch_json", side_effect=[_FF_RATES, currencies]):
            with self.assertRaises(fx.RateUnavailable):
                fx._load_frankfurter(0.0)

    def test_a_rate_that_is_not_a_positive_finite_number_is_dropped(self):
        bad = [0, -1, "0", "-0.5", "abc", "", None, True, False, "NaN", "Infinity", "-Infinity", 1e13, "1E+13", 1e-10, [1], {"rate": 1}]
        entries = [{"date": DAY, "base": "USD", "quote": "ZZZ", "rate": value} for value in bad]
        rates, _ = fx._parse_frankfurter_rates(_rows(extra=entries))
        self.assertNotIn("ZZZ", rates)
        self.assertEqual(len(rates), 25)

    def test_numeric_strings_and_small_exponents_are_rates(self):
        entries = [
            {"date": DAY, "base": "USD", "quote": "AAA", "rate": "0.91332"},
            {"date": DAY, "base": "USD", "quote": "BBB", "rate": 1.6666e-05},
            {"date": DAY, "base": "USD", "quote": "CCC", "rate": 1e-9},
            {"date": DAY, "base": "USD", "quote": "DDD", "rate": 1e12},
        ]
        rates, _ = fx._parse_frankfurter_rates(_rows(extra=entries))
        self.assertEqual(rates["AAA"], Decimal("0.91332"))
        self.assertEqual(rates["BBB"], Decimal("0.000016666"))
        self.assertEqual(rates["CCC"], Decimal("0.000000001"))
        self.assertEqual(rates["DDD"], Decimal("1000000000000"))

    def test_rows_for_another_base_or_an_odd_code_are_dropped(self):
        entries = [
            {"date": DAY, "base": "EUR", "quote": "USD", "rate": 1.1},
            {"date": DAY, "base": "USD", "quote": "USDT", "rate": 1.0014},
            {"date": DAY, "base": "USD", "quote": "usd", "rate": 1},
            {"date": DAY, "base": "USD", "quote": "B T", "rate": 2},
            {"date": DAY, "base": "USD", "quote": 123, "rate": 2},
            {"date": DAY, "base": "USD", "quote": "USD", "rate": 1.1},
            "just a string",
            None,
            7,
        ]
        rates, _ = fx._parse_frankfurter_rates(_rows(extra=entries))
        self.assertEqual(set(rates), set(_CODES))

    def test_a_table_that_is_mostly_unusable_is_refused(self):
        def bad(count):
            return [
                {"date": DAY, "base": "USD", "quote": "Z%s%s" % (chr(65 + i // 26), chr(65 + i % 26)), "rate": "nope"}
                for i in range(count)
            ]

        with self.assertRaises(fx.RateUnavailable):
            fx._parse_frankfurter_rates(_rows(21, bad(22)))  # 21 of 43 usable
        rates, _ = fx._parse_frankfurter_rates(_rows(21, bad(20)))  # 21 of 41 is still a table
        self.assertEqual(len(rates), 21)

    def test_a_document_that_is_not_a_published_table_is_refused(self):
        short = [{"date": DAY, "base": "USD", "quote": c, "rate": 1} for c in _CODES[:19]]
        long = [{"date": DAY, "base": "USD", "quote": "AA%d" % (i % 10), "rate": 1} for i in range(501)]
        for document in ({}, None, "[]", [], short, long):
            with self.subTest(document=str(document)[:40]):
                with self.assertRaises(fx.RateUnavailable):
                    fx._parse_frankfurter_rates(document)

    def test_dates_must_be_real_calendar_dates(self):
        days = ["2026-13-45", "2026-02-30", "20261126", "26-11-2026", "", None, 20261126, "<script>", "2026-11-26T00:00:00Z", DAY]
        entries = [{"date": day, "base": "USD", "quote": "ZZ" + chr(65 + i), "rate": 2} for i, day in enumerate(days)]
        _, dates = fx._parse_frankfurter_rates(_rows(extra=entries))
        self.assertEqual([dates["ZZ" + chr(65 + i)] for i in range(len(days))], [""] * (len(days) - 1) + [DAY])

    def test_names_and_symbols_are_cut_and_lose_control_characters(self):
        rows = [
            {"iso_code": "AAA", "type": "currency", "name": "A\u202egreat\u200b name", "symbol": "\u202e$"},
            {"iso_code": "BBB", "type": "currency", "name": "N" * 200, "symbol": "S" * 200},
            {"iso_code": "CCC", "type": "currency", "name": 5, "symbol": None},
            {"iso_code": "DDD", "type": "currency", "name": "   ", "symbol": "   "},
            {"iso_code": "EEE", "type": "currency", "name": "Zz " * 30, "symbol": "€" * 30},
        ]
        rows += [{"iso_code": c, "type": "currency", "name": c, "symbol": ""} for c in _CODES[:20]]
        names, symbols = fx._parse_frankfurter_currencies(rows)
        self.assertEqual(names["AAA"], "Agreat name")
        self.assertEqual(symbols["AAA"], "$")
        self.assertEqual(names["BBB"], "N" * 48)
        self.assertEqual(symbols["BBB"], "S" * 8)
        self.assertEqual((names["CCC"], symbols["CCC"], names["DDD"], symbols["DDD"]), ("", "", "", ""))
        self.assertEqual(names["EEE"], "Zz " * 16)  # cut, not trimmed mid-way
        self.assertEqual(symbols["EEE"], "€" * 8)

    def test_only_currency_entries_are_offered(self):
        rows = [
            {"iso_code": "BTC", "type": "crypto", "name": "Bitcoin", "symbol": "₿"},
            {"iso_code": "XAU", "type": "metal", "name": "Gold", "symbol": "oz t"},
            {"iso_code": "AAA", "name": "No type", "symbol": ""},
            {"type": "currency", "name": "No code", "symbol": ""},
            {"iso_code": "A", "type": "currency", "name": "Short", "symbol": ""},
            {"iso_code": "bbbb", "type": "currency", "name": "Long", "symbol": ""},
        ]
        rows += [{"iso_code": c, "type": "currency", "name": c, "symbol": ""} for c in _CODES[:20]]
        names, symbols = fx._parse_frankfurter_currencies(rows)
        self.assertEqual(set(names), set(_CODES[:20]))
        self.assertEqual(set(symbols), set(_CODES[:20]))

    def test_a_currency_list_that_is_too_short_or_oversized_is_refused(self):
        for document in ({}, None, "x", [{"iso_code": c, "type": "currency"} for c in _CODES[:19]], [{"iso_code": "AA%d" % (i % 10), "type": "currency"} for i in range(501)]):
            with self.subTest(document=str(document)[:40]):
                with self.assertRaises(fx.RateUnavailable):
                    fx._parse_frankfurter_currencies(document)


class ErApiParserTests(unittest.TestCase):
    def test_reads_the_live_document(self):
        with mock.patch.object(fx, "_fetch_json", return_value=_ERAPI) as fetch:
            table = fx._load_erapi(500.0)
        fetch.assert_called_once_with(config.FX_ERAPI_URL)
        self.assertEqual(table.source, "erapi")
        self.assertEqual(table.fetched_at, 500.0)
        self.assertEqual(len(table.rates), FIAT)
        self.assertEqual(table.codes()[0], "USD")
        self.assertEqual(table.rates["USD"], Decimal("1"))
        self.assertEqual(set(table.dates.values()), {"2026-11-25"})
        self.assertEqual((table.names, table.symbols), ({}, {}))

    def test_anything_but_a_success_document_in_usd_is_refused(self):
        huge = {str(i): 1 for i in range(501)}
        no_rates = dict(_ERAPI, rates=None)
        for body in (_erapi_body(result="error"), _erapi_body(base_code="EUR"), _erapi_body(rates=[]), no_rates, _erapi_body(rates=huge), [], None, "ok"):
            with self.subTest(keys=sorted(body)[:6] if isinstance(body, dict) else body, result=body.get("result") if isinstance(body, dict) else None):
                with mock.patch.object(fx, "_fetch_json", return_value=body):
                    with self.assertRaises(fx.RateUnavailable):
                        fx._load_erapi(0.0)

    def test_bad_rates_are_dropped_and_a_bad_stamp_leaves_no_date(self):
        rates = dict(list(_ERAPI["rates"].items())[:25])
        rates.update({"ZZZ": "abc", "YYY": 0, "XXX": -5, "WWW": True, "VVV": "Infinity", "UUU": 1e20})
        for stamp in ("abc", None, -5, 1e30, True, "1e99", 0):
            with self.subTest(stamp=repr(stamp)):
                with mock.patch.object(fx, "_fetch_json", return_value=_erapi_body(rates, time_last_update_unix=stamp)):
                    table = fx._load_erapi(0.0)
                self.assertEqual(len(table.rates), 26)  # 25 kept, plus USD which is 1 by definition
                self.assertNotIn("ZZZ", table.rates)
                self.assertEqual(set(table.dates.values()), {""})
        for stamp in ("1795564951", 1795564951.9, Decimal("1795564951")):
            with self.subTest(stamp=repr(stamp)):
                with mock.patch.object(fx, "_fetch_json", return_value=_erapi_body(time_last_update_unix=stamp)):
                    table = fx._load_erapi(0.0)
                self.assertEqual(set(table.dates.values()), {"2026-11-25"})


# --- fetching ----------------------------------------------------------------
class FetchTests(_ConfigCase):
    def setUp(self):
        super().setUp()
        config.FX_MAX_BYTES = 4096
        config.FX_TIMEOUT_SECONDS = 6

    def test_reads_a_document_handed_out_in_small_pieces(self):
        reply = _Reply({"ok": "€" * 100}, piece=3)
        with mock.patch.object(fx, "_urlopen", return_value=reply) as open_url:
            self.assertEqual(fx._fetch_json("https://fx.example/rates"), {"ok": "€" * 100})
        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, "https://fx.example/rates")
        self.assertEqual(request.get_header("User-agent"), "mazapay-bridge/0.1")
        self.assertEqual(request.get_header("Accept"), "application/json")
        self.assertEqual(open_url.call_args.kwargs, {"timeout": 6})
        self.assertGreater(reply.reads, 10)

    def test_an_oversized_document_is_refused(self):
        with mock.patch.object(fx, "_urlopen", return_value=_Reply(b"x" * 5000)):
            with self.assertRaises(fx.RateUnavailable) as caught:
                fx._fetch_json("https://fx.example/rates")
        self.assertIn("too large", str(caught.exception))

    def test_a_document_that_is_not_json_is_refused(self):
        for body in (b"<html>maintenance</html>", b"\xff\xfe\x00", b'{"a": 1', b"", b'{"a": 1}\x00'):
            with self.subTest(body=body):
                with mock.patch.object(fx, "_urlopen", return_value=_Reply(body)):
                    with self.assertRaises(fx.RateUnavailable) as caught:
                        fx._fetch_json("https://fx.example/rates")
                self.assertIn("not JSON", str(caught.exception))

    def test_network_failures_become_rate_unavailable(self):
        failures = [
            OSError("connection refused"),
            urllib.error.URLError("dns"),
            urllib.error.HTTPError("https://fx.example", 503, "down", {}, None),
            http.client.RemoteDisconnected(),
            http.client.IncompleteRead(b"partial"),
            http.client.BadStatusLine("garbage"),
            ValueError("unknown url type"),
            TimeoutError("timed out"),
        ]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)  # the unraised HTTPError has no body to close
            for failure in failures:
                with self.subTest(failure=repr(failure)):
                    with mock.patch.object(fx, "_urlopen", side_effect=failure):
                        with self.assertRaises(fx.RateUnavailable) as caught:
                            fx._fetch_json("https://fx.example/rates")
                    self.assertIn(type(failure).__name__, str(caught.exception))

    def test_a_programming_error_is_not_disguised_as_an_outage(self):
        with mock.patch.object(fx, "_urlopen", side_effect=RuntimeError("bug")):
            with self.assertRaises(RuntimeError):
                fx._fetch_json("https://fx.example/rates")

    def test_a_source_that_trickles_forever_costs_one_deadline(self):
        clock = _Clock(1000.0)

        class Trickle(_Reply):
            def read(self, size=-1):
                clock.advance(10)
                return b" "

        reply = Trickle(b" ")
        with mock.patch.object(fx, "_urlopen", return_value=reply), mock.patch.object(fx, "_monotonic", clock):
            with self.assertRaises(fx.RateUnavailable) as caught:
                fx._fetch_json("https://fx.example/rates")
        self.assertIn("took too long", str(caught.exception))
        self.assertLessEqual(reply.reads, 3)


# --- cache -------------------------------------------------------------------
class CachedTests(unittest.TestCase):
    def setUp(self):
        self.clock = _Clock()
        patcher = mock.patch.object(fx, "_clock", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _cached(self, load):
        return fx._Cached("test", 60, 600, 30, load)

    def test_fresh_value_is_served_without_a_new_refresh(self):
        calls = []

        def load(now):
            calls.append(now)
            return "v"

        cached = self._cached(load)
        self.assertEqual(cached.get(wait_seconds=1), ("v", False))
        self.assertEqual(calls, [self.clock.now])
        self.clock.advance(59)
        self.assertEqual(cached.get(wait_seconds=1), ("v", False))
        self.assertEqual(len(calls), 1)

    def test_value_past_ttl_is_served_as_stale_while_it_refreshes(self):
        calls = []

        def load(now):
            calls.append(now)
            return "v%d" % len(calls)

        cached = self._cached(load)
        cached.get(wait_seconds=1)
        self.clock.advance(61)
        self.assertEqual(cached.get(), ("v1", True))  # at once, flagged stale
        self.assertTrue(cached._done.wait(2))
        self.assertEqual(cached.get(), ("v2", False))
        self.assertEqual(len(calls), 2)

    def test_value_past_max_age_is_never_returned(self):
        calls = []

        release = threading.Event()

        def load(now):
            release.wait(5)
            return "v2"

        cached = self._cached(load)
        cached._value, cached._at = "v1", self.clock.now  # as if fetched long ago
        self.clock.advance(601)
        with self.assertRaises(fx.RateUnavailable):
            cached.get()  # too old to show, and it does not wait
        release.set()
        self.assertEqual(cached.get(wait_seconds=2), ("v2", False))

    def test_failed_refresh_is_remembered_and_not_retried_at_once(self):
        calls = []

        def load(now):
            calls.append(now)
            raise OSError("feed down")

        cached = self._cached(load)
        with self.assertRaises(fx.RateUnavailable) as caught:
            cached.get(wait_seconds=2)
        self.assertIn("OSError: feed down", str(caught.exception))
        for seconds in (0, 29):
            self.clock.advance(seconds)
            with self.assertRaises(fx.RateUnavailable):
                cached.get()
        self.assertEqual(len(calls), 1)
        self.clock.advance(2)
        with self.assertRaises(fx.RateUnavailable):
            cached.get(wait_seconds=2)
        self.assertEqual(len(calls), 2)

    def test_last_good_value_survives_a_failed_refresh_until_max_age(self):
        state = {"fail": False}

        def load(now):
            if state["fail"]:
                raise OSError("down")
            return "good"

        cached = self._cached(load)
        cached.get(wait_seconds=1)
        state["fail"] = True
        self.clock.advance(61)
        self.assertEqual(cached.get(), ("good", True))
        self.assertTrue(cached._done.wait(2))
        self.assertEqual(cached.get(), ("good", True))
        self.clock.advance(601)
        with self.assertRaises(fx.RateUnavailable) as caught:
            cached.get(wait_seconds=2)
        self.assertIn("OSError: down", str(caught.exception))

    def test_clear_forgets_everything(self):
        cached = self._cached(lambda now: "v")
        cached.get(wait_seconds=1)
        cached.clear()
        self.assertEqual(cached._value, None)
        with mock.patch.object(cached, "load", side_effect=OSError("down")):
            with self.assertRaises(fx.RateUnavailable):
                cached.get(wait_seconds=2)

    def test_a_bad_wait_never_blocks(self):
        def load(now):
            raise OSError("down")

        cached = self._cached(load)
        for wait in (None, "soon", float("nan"), float("inf"), -5, object()):
            with self.subTest(wait=repr(wait)):
                cached.clear()
                started = self.clock.now
                with self.assertRaises(fx.RateUnavailable):
                    cached.get(wait_seconds=wait)
                self.assertEqual(self.clock.now, started)

    def test_a_value_loaded_after_the_caller_arrived_is_returned_to_that_caller(self):
        calls = []

        def load(now):
            calls.append(now)
            self.clock.advance(3)  # the clock moved on while the reply came in
            return "v"

        cached = self._cached(load)
        self.assertEqual(cached.get(wait_seconds=1), ("v", False))  # the first waiting call
        self.assertEqual(calls, [self.clock.now - 3])

    def test_the_same_on_the_real_clock_with_a_slow_reply(self):
        def slow(now):
            time.sleep(0.05)
            return "v"

        with mock.patch.object(fx, "_clock", time.time):
            cached = fx._Cached("test", 60, 600, 30, slow)
            self.assertEqual(cached.get(wait_seconds=2), ("v", False))  # the first call
            self.assertEqual(cached.get(), ("v", False))

    def test_a_value_stamped_ahead_of_the_clock_is_still_refused(self):
        cached = self._cached(lambda now: "v")
        cached._value, cached._at = "v", self.clock.now + 500  # the clock moved backwards
        with mock.patch.object(cached, "load", side_effect=OSError("down")):
            with self.assertRaises(fx.RateUnavailable) as caught:
                cached.get(wait_seconds=2)
        self.assertIn("OSError: down", str(caught.exception))


# --- figures -----------------------------------------------------------------
UNIT = Decimal("0.000014")  # the figure quoted in the thread: $14 for a million MAZA
BUDGET = 0.4  # seconds a page says it can wait, in the one-budget tests
SLACK = 1.0  # thread wake-ups on a loaded machine; the recorded waits are the exact check


def _table(now, rates, names=None, symbols=None, dates=None, source="frankfurter"):
    return fx.Table(
        source,
        now,
        {code: Decimal(text) for code, text in rates.items()},
        {code: DAY for code in rates} if dates is None else dates,
        names,
        symbols,
    )


class FigureCase(_ConfigCase):
    """The module's own caches on a frozen clock, fed hand-made tables."""

    RATES = {"EUR": "0.9213", "JPY": "150.12", "BGN": "1.79"}  # what 1 USD buys
    NAMES = {"EUR": "Euro", "JPY": "Japanese Yen", "BGN": "Bulgarian Lev"}
    SYMBOLS = {"EUR": "€", "JPY": "¥", "BGN": "лв"}

    def setUp(self):
        super().setUp()
        fx.reset()
        self.clock = _Clock()
        self._patch(fx, "_clock", self.clock)
        self.loads = []  # the clock reading of every table fetch

        def load(now):
            self.loads.append(now)
            return self.table(now)

        self._patch(fx, "_LOADERS", {"frankfurter": load, "erapi": load})
        config.FX_SOURCE = "frankfurter"
        self._patch(fx.pricing, "get_maza_usd", lambda: (UNIT, "lore"))
        self.addCleanup(self._drain)  # runs first, while the patches are still on

    def _patch(self, target, name, value):
        patcher = mock.patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def table(self, now=None, rates=None, names=None, symbols=None, dates=None):
        return _table(
            self.clock.now if now is None else now,
            self.RATES if rates is None else rates,
            self.NAMES if names is None else names,
            self.SYMBOLS if symbols is None else symbols,
            dates,
        )

    def _drain(self):
        """A refresh that is still running finishes before the caches clear."""
        deadline = time.monotonic() + 5
        caches = [c for c in (fx._RATES, fx._PRICE) if isinstance(c, fx._Cached)]
        while any(c._busy for c in caches) and time.monotonic() < deadline:
            time.sleep(0.01)
        if len(caches) == 2:
            fx.reset()

    def _slow(self, table_delay=None, price_delay=None):
        """Loaders that answer after their delay; ``None`` means not until
        the test is over."""
        gate = threading.Event()
        self.addCleanup(gate.set)  # first cleanup, so _drain sees them finish

        def take(delay):
            if delay is None:
                gate.wait(10)
            else:
                time.sleep(delay)

        def table_load(now):
            take(table_delay)
            return self.table(now)

        def price_load():
            take(price_delay)
            return UNIT, "lore"

        self._patch(fx, "_LOADERS", {"frankfurter": table_load})
        self._patch(fx.pricing, "get_maza_usd", price_load)

    def _waits_asked_for(self):
        """Every wait either cache is asked for, in order."""
        asked = []
        get = fx._Cached.get

        def spy(cached, now=None, wait_seconds=0.0):
            asked.append(wait_seconds)
            return get(cached, now, wait_seconds)

        self._patch(fx._Cached, "get", spy)
        return asked


class LabelTests(unittest.TestCase):
    def test_each_feed_and_source_has_a_name_and_a_stranger_keeps_its_own(self):
        self.assertEqual(fx.price_label("lore"), "CoinLore")
        self.assertEqual(fx.price_label("paprika"), "CoinPaprika")
        self.assertEqual(fx.source_label("frankfurter"), "Frankfurter")
        self.assertEqual(fx.source_label("erapi"), "ExchangeRate-API")
        self.assertEqual((fx.price_label("new-feed"), fx.source_label("")), ("new-feed", ""))


class FormatAmountTests(unittest.TestCase):
    def test_two_decimals_from_one_upward_grouped_with_commas(self):
        self.assertEqual(fx.format_amount(Decimal("70")), "70.00")
        self.assertEqual(fx.format_amount("14"), "14.00")
        self.assertEqual(fx.format_amount(Decimal("1234.5678")), "1,234.57")
        self.assertEqual(fx.format_amount(Decimal("999.9999")), "1,000.00")
        self.assertEqual(fx.format_amount(Decimal("18811.38")), "18,811.38")
        self.assertEqual(fx.format_amount(Decimal("1E+30")),
                         "1,000,000,000,000,000,000,000,000,000,000.00")

    def test_four_decimals_below_one(self):
        self.assertEqual(fx.format_amount(Decimal("0.5")), "0.5000")
        self.assertEqual(fx.format_amount(Decimal("0.00223076")), "0.0022")
        self.assertEqual(fx.format_amount(Decimal("0.00225076")), "0.0023")
        self.assertEqual(fx.format_amount(Decimal("0.99999")), "1.0000")
        self.assertEqual(fx.format_amount(Decimal("0.0001")), "0.0001")

    def test_four_significant_digits_below_a_ten_thousandth(self):
        self.assertEqual(fx.format_amount(Decimal("0.00001278648")), "0.00001279")
        self.assertEqual(fx.format_amount(Decimal("1.4E-10")), "0.00000000014")
        self.assertEqual(fx.format_amount(Decimal("1.4E-16")), "0.00000000000000014")
        self.assertEqual(fx.format_amount(Decimal("0.000099999")), "0.0001")

    def test_zero_and_negative_figures(self):
        self.assertEqual(fx.format_amount(0), "0.00")
        self.assertEqual(fx.format_amount("-0.5"), "-0.5000")
        self.assertEqual(fx.format_amount(Decimal("-0.00001278648")), "-0.00001279")

    def test_anything_that_is_not_a_finite_number_is_refused(self):
        for bad in (None, "", "abc", "NaN", "Infinity", True, object()):
            with self.subTest(bad=repr(bad)), self.assertRaises(ValueError):
                fx.format_amount(bad)


class SourceSwitchTests(FigureCase):
    def test_off_means_no_figures_and_no_fetch(self):
        config.FX_SOURCE = "none"
        for read in (fx.table, fx.options, lambda: fx.worth(1)):
            with self.assertRaises(fx.RateUnavailable):
                read()
        self.assertIsNone(fx.embed())
        self.assertEqual(fx.pick("EUR"), "USD")
        self.assertEqual(self.loads, [])
        self.assertFalse(fx._RATES._busy)

    def test_an_unknown_source_name_is_refused_the_same_way(self):
        config.FX_SOURCE = "bogus"
        with self.assertRaises(fx.RateUnavailable):
            fx.worth(1)
        self.assertIsNone(fx.embed())
        self.assertEqual(self.loads, [])

    def test_reset_forgets_the_table_and_the_price(self):
        fx.worth(1000000, wait_seconds=2)
        fx.worth(2000000, wait_seconds=2)
        self.assertEqual(len(self.loads), 1)
        fx.reset()
        fx.worth(3000000, wait_seconds=2)
        self.assertEqual(len(self.loads), 2)


class PriceTests(FigureCase):
    def test_the_price_is_cut_to_eight_decimals_with_its_feed_and_time(self):
        self._patch(fx.pricing, "get_maza_usd", lambda: (Decimal("0.00122901234"), "lore"))
        self.assertEqual(fx.price(wait_seconds=2), (Decimal("0.00122901"), "lore", self.clock.now, False))

    def test_a_missing_price_is_a_rate_unavailable(self):
        self._patch(fx.pricing, "get_maza_usd", mock.Mock(side_effect=pricing.PriceUnavailable("down")))
        with self.assertRaisesRegex(fx.RateUnavailable, "MAZA price unavailable"):
            fx.price(wait_seconds=2)

    def test_a_price_that_is_not_positive_or_out_of_range_is_refused(self):
        cases = (
            (Decimal("0"), "not positive"),
            (Decimal("-0.0012"), "not positive"),
            (Decimal("1E-12"), "not positive"),
            (Decimal("1E+400"), "out of range"),
            (Decimal("NaN"), "not positive"),  # a quiet NaN never signals; it is simply not a price
        )
        for bad, reason in cases:
            with self.subTest(bad=bad):
                fx.reset()
                self._patch(fx.pricing, "get_maza_usd", lambda bad=bad: (bad, "lore"))
                with self.assertRaisesRegex(fx.RateUnavailable, reason):
                    fx.price(wait_seconds=2)


class WorthTests(FigureCase):
    def test_a_million_maza_in_each_requested_currency(self):
        result = fx.worth(1000000, ["EUR", "JPY"], wait_seconds=2)
        self.assertEqual(result["maza"], "1000000")
        self.assertEqual(result["usd"], "14.00")
        self.assertEqual(list(result["values"]), ["EUR", "JPY"])
        self.assertEqual(result["values"]["EUR"],
                         {"code": "EUR", "amount": "12.90", "name": "Euro", "symbol": "€", "date": DAY})
        self.assertEqual(result["values"]["JPY"],
                         {"code": "JPY", "amount": "2,101.68", "name": "Japanese Yen", "symbol": "¥", "date": DAY})

    def test_us_dollars_are_the_default(self):
        result = fx.worth("250000", wait_seconds=2)
        self.assertEqual(result["usd"], "3.50")
        self.assertEqual(list(result["values"]), ["USD"])
        self.assertEqual(result["values"]["USD"],
                         {"code": "USD", "amount": "3.50", "name": "", "symbol": "", "date": ""})

    def test_the_line_names_both_feeds_and_when_they_were_read(self):
        result = fx.worth(1, wait_seconds=2)
        self.assertEqual(result["price"], "0.000014")
        self.assertEqual(result["price_source"], "lore")
        self.assertEqual(result["price_label"], "CoinLore")
        self.assertEqual(result["fx_source"], "frankfurter")
        self.assertEqual(result["fx_label"], "Frankfurter")
        self.assertEqual(result["price_at"], self.clock.now)
        self.assertEqual(result["fx_fetched_at"], self.clock.now)
        self.assertEqual(result["stale"], False)
        self.assertEqual(result["values"]["USD"]["amount"], "0.000014")

    def test_small_amounts_keep_their_significant_digits(self):
        self.assertEqual(fx.worth(1, ["EUR"], wait_seconds=2)["values"]["EUR"]["amount"], "0.0000129")
        self.assertEqual(fx.worth(1, ["JPY"], wait_seconds=2)["values"]["JPY"]["amount"], "0.0021")
        tiny = fx.worth("0.000000014", wait_seconds=2)
        self.assertEqual((tiny["maza"], tiny["usd"]), ("0.00000001", "0.00000000000014"))

    def test_an_amount_is_cut_to_maza_s_eight_decimals(self):
        self.assertEqual(fx.worth("1000.500000004", wait_seconds=2)["maza"], "1000.5")
        self.assertEqual(fx.worth("0.000000019", wait_seconds=2)["maza"], "0.00000002")
        self.assertEqual(fx.worth("0.000000004", wait_seconds=2)["values"]["USD"]["amount"], "0.00")
        self.assertEqual(fx.worth(0, wait_seconds=2)["values"]["USD"]["amount"], "0.00")
        self.assertEqual(fx.worth(" 1000 ", wait_seconds=2)["maza"], "1000")

    def test_a_bad_amount_is_refused_before_any_fetch(self):
        for bad in ("-5", "abc", "", None, 1.5, True, Decimal("Infinity"), Decimal("NaN"), Decimal("1E+16")):
            with self.subTest(bad=repr(bad)), self.assertRaises(ValueError):
                fx.worth(bad, wait_seconds=2)
        self.assertEqual(self.loads, [])

    def test_currency_codes_are_three_letters_and_never_repeated(self):
        self.assertEqual(list(fx.worth(1, "eur", wait_seconds=2)["values"]), ["EUR"])
        self.assertEqual(list(fx.worth(1, ["USD", "eur", "EUR"], wait_seconds=2)["values"]), ["USD", "EUR"])

    def test_a_bad_currency_list_is_refused(self):
        many = [a + b + c for a in "ABC" for b in "ABCD" for c in "ABCDE"]  # 60 distinct codes
        for bad in (["US"], ["EUR", 5], [5], 5, "EURO", many[:41]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                fx.worth(1, bad, wait_seconds=2)
        self.assertEqual(self.loads, [])

    def test_a_well_formed_currency_the_source_does_not_offer_has_no_figure(self):
        many = [a + b + c for a in "ABC" for b in "ABCD" for c in "ABCDE"]
        with self.assertRaises(fx.RateUnavailable):
            fx.worth(1, many[:40], wait_seconds=2)

    def test_a_currency_the_hand_made_table_leaves_out_is_refused_too(self):
        with self.assertRaisesRegex(fx.RateUnavailable, "CHF is not offered"):
            fx.worth(1, ["CHF"], wait_seconds=2)

    def test_nothing_is_shown_without_a_price_or_without_a_table(self):
        self._patch(fx.pricing, "get_maza_usd", mock.Mock(side_effect=pricing.PriceUnavailable("down")))
        with self.assertRaisesRegex(fx.RateUnavailable, "MAZA price unavailable"):
            fx.worth(1000000, wait_seconds=2)
        self._patch(fx, "_LOADERS", {"frankfurter": mock.Mock(side_effect=OSError("down"))})
        fx.reset()
        with self.assertRaises(fx.RateUnavailable):
            fx.worth(1000000, wait_seconds=2)

    def test_a_stale_table_is_flagged_not_hidden(self):
        fx.worth(1000000, ["EUR"], wait_seconds=2)
        self.clock.advance(config.FX_CACHE_SECONDS + 1)
        result = fx.worth(1000000, ["EUR"])
        self.assertEqual(result["stale"], True)
        self.assertEqual(result["values"]["EUR"]["amount"], "12.90")

    def test_a_stale_price_alone_flags_the_line(self):
        self.assertEqual(fx.worth(1000000, wait_seconds=2)["stale"], False)
        self.clock.advance(config.WORTH_PRICE_CACHE_SECONDS + 1)
        self.assertEqual(fx.worth(1000000)["stale"], True)

    def test_a_price_older_than_its_limit_is_never_shown(self):
        feeds = [(Decimal("0.000014"), "lore"), (Decimal("0.00002"), "lore")]
        self._patch(fx.pricing, "get_maza_usd", mock.Mock(side_effect=feeds))
        self.assertEqual(fx.worth(1000000, wait_seconds=2)["usd"], "14.00")
        self.clock.advance(config.WORTH_PRICE_MAX_AGE_SECONDS + 1)
        self.assertEqual(fx.worth(1000000, wait_seconds=2)["usd"], "20.00")

    def test_the_result_can_go_straight_into_a_page(self):
        result = fx.worth(1000000, ["BGN"], wait_seconds=2)
        self.assertEqual(result["values"]["BGN"],
                         {"code": "BGN", "amount": "25.06", "name": "Bulgarian Lev", "symbol": "лв", "date": DAY})

        def plain(value):
            if isinstance(value, dict):
                return all(isinstance(key, str) and plain(item) for key, item in value.items())
            if isinstance(value, list):
                return all(plain(item) for item in value)
            return value is None or type(value) in (str, int, float, bool)

        self.assertTrue(plain(result))
        self.assertEqual(json.loads(json.dumps(result)), result)

    def test_both_refreshes_run_at_once(self):
        started = []
        gates = {"table": threading.Event(), "price": threading.Event()}

        def table_load(now):
            started.append("table")
            gates["table"].wait(5)
            return self.table(now)

        def price_load():
            started.append("price")
            gates["price"].wait(5)
            return UNIT, "lore"

        self._patch(fx, "_LOADERS", {"frankfurter": table_load})
        self._patch(fx.pricing, "get_maza_usd", price_load)
        out = {}

        def read():
            out.update(fx.worth(1000000, wait_seconds=5))

        thread = threading.Thread(target=read, daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        while len(started) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        both_running = sorted(started) == ["price", "table"]  # neither can end until a gate opens
        for gate in gates.values():
            gate.set()
        thread.join(10)
        self.assertTrue(both_running, started)
        self.assertEqual(out.get("usd"), "14.00")

    def test_one_wait_covers_the_table_and_the_price_together(self):
        self._slow()  # neither refresh can finish while the test runs
        asked = self._waits_asked_for()
        started = time.monotonic()
        with self.assertRaises(fx.RateUnavailable):
            fx.worth(1000000, wait_seconds=BUDGET)
        elapsed = time.monotonic() - started
        self.assertEqual(asked[:2], [0.0, 0.0])  # both start before anyone waits
        self.assertLessEqual(sum(asked), BUDGET)  # and the whole call waits no longer
        self.assertLess(elapsed, BUDGET + SLACK)

    def test_the_price_gets_only_the_time_the_table_left(self):
        self._slow(table_delay=BUDGET / 2)
        asked = self._waits_asked_for()
        started = time.monotonic()
        with self.assertRaises(fx.RateUnavailable):
            fx.worth(1000000, wait_seconds=BUDGET)
        elapsed = time.monotonic() - started
        self.assertEqual(asked[:2], [0.0, 0.0])
        self.assertLessEqual(asked[3], BUDGET / 2)  # the table used at least half the budget
        self.assertLess(elapsed, BUDGET + SLACK)

    def test_two_slow_feeds_that_finish_in_time_are_both_shown(self):
        self._slow(table_delay=0.05, price_delay=0.05)
        result = fx.worth(1000000, ["EUR"], wait_seconds=2)
        self.assertEqual((result["usd"], result["values"]["EUR"]["amount"]), ("14.00", "12.90"))

    def test_a_first_value_that_arrives_after_the_call_started_is_shown(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def table_load(now):
            release.wait(5)
            self.clock.advance(2)  # the reply took two seconds of clock time
            return self.table(self.clock.now)

        self._patch(fx, "_LOADERS", {"frankfurter": table_load})
        self._patch(fx.pricing, "get_maza_usd", lambda: (UNIT, "lore"))
        threading.Timer(0.5, release.set).start()
        result = fx.worth(1000000, ["EUR"], wait_seconds=2)
        self.assertEqual((result["usd"], result["values"]["EUR"]["amount"]), ("14.00", "12.90"))
        self.assertEqual(result["fx_fetched_at"], self.clock.now)

    def test_a_cold_start_on_the_real_clock_shows_figures_on_the_first_call(self):
        def slow_table(now):
            time.sleep(0.05)
            return self.table(now)

        def slow_price():
            time.sleep(0.05)
            return UNIT, "lore"

        self._patch(fx, "_clock", time.time)  # the clock the page runs on
        self._patch(fx, "_LOADERS", {"frankfurter": slow_table})
        self._patch(fx.pricing, "get_maza_usd", slow_price)
        result = fx.worth(1000000, ["EUR"], wait_seconds=2.5)
        self.assertEqual((result["usd"], result["values"]["EUR"]["amount"]), ("14.00", "12.90"))
        fx.reset()  # and the same cold start through the page embed
        embedded = fx.embed(wait_seconds=2.5)
        self.assertIsInstance(embedded, dict)
        self.assertEqual((embedded["p"], embedded["r"]["EUR"], embedded["st"]), ("0.000014", "0.9213", 0))


class OptionsAndPickTests(FigureCase):
    def test_the_picker_lists_the_dollar_first_then_names_in_order(self):
        self.assertEqual(fx.options(wait_seconds=2),
                         [("USD", "", ""), ("BGN", "Bulgarian Lev", "лв"), ("EUR", "Euro", "€"),
                          ("JPY", "Japanese Yen", "¥")])

    def test_names_sort_without_regard_to_case(self):
        names = {"EUR": "euro", "JPY": "Japanese Yen", "BGN": "bulgarian Lev"}
        self._patch(fx, "_LOADERS", {"frankfurter": lambda now: self.table(now, names=names)})
        self.assertEqual([code for code, _, _ in fx.options(wait_seconds=2)], ["USD", "BGN", "EUR", "JPY"])

    def test_a_pick_is_always_a_code_from_the_table(self):
        for raw, expected in (("EUR", "EUR"), (" eur ", "EUR"), ("eur", "EUR"), ("EURO", "EUR"), ("CHF", "USD"),
                              ("EU", "USD"), ("<script>", "USD"), (5, "USD"), (None, "USD")):
            with self.subTest(raw=raw):
                self.assertEqual(fx.pick(raw, wait_seconds=2), expected)


class EmbedTests(FigureCase):
    def test_the_compact_figure_holds_what_a_page_needs(self):
        self.assertEqual(fx.embed(wait_seconds=2), {
            "p": "0.000014", "s": "lore", "t": self.clock.now, "f": "frankfurter", "st": 0,
            "c": [["USD", "", ""], ["BGN", "Bulgarian Lev", "лв"], ["EUR", "Euro", "€"],
                  ["JPY", "Japanese Yen", "¥"]],
            "r": {"USD": "1", "BGN": "1.79", "EUR": "0.9213", "JPY": "150.12"},
            "d": {"USD": "", "BGN": DAY, "EUR": DAY, "JPY": DAY},
        })

    def test_the_compact_figure_marks_a_stale_value_with_one(self):
        fresh = fx.embed(wait_seconds=2)
        self.assertIsNotNone(fresh)
        self.assertEqual(fresh["st"], 0)
        self.clock.advance(config.FX_CACHE_SECONDS + 1)
        aged = fx.embed()
        self.assertIsNotNone(aged)
        self.assertEqual(aged["st"], 1)

    def test_nothing_is_embedded_when_a_figure_cannot_be_trusted(self):
        self._patch(fx.pricing, "get_maza_usd", mock.Mock(side_effect=pricing.PriceUnavailable("down")))
        self.assertIsNone(fx.embed(wait_seconds=2))
        fx.reset()
        self._patch(fx.pricing, "get_maza_usd", lambda: (UNIT, "lore"))
        self._patch(fx, "_LOADERS", {"frankfurter": mock.Mock(side_effect=OSError("down"))})
        self.assertIsNone(fx.embed(wait_seconds=2))

    def test_one_wait_covers_the_table_and_the_price_together(self):
        self._slow()  # neither refresh can finish while the test runs
        asked = self._waits_asked_for()
        started = time.monotonic()
        self.assertIsNone(fx.embed(wait_seconds=BUDGET))
        elapsed = time.monotonic() - started
        self.assertEqual(asked[:2], [0.0, 0.0])
        self.assertLessEqual(sum(asked), BUDGET)
        self.assertLess(elapsed, BUDGET + SLACK)

    def test_the_price_gets_only_the_time_the_table_left(self):
        self._slow(table_delay=BUDGET / 2)
        asked = self._waits_asked_for()
        started = time.monotonic()
        self.assertIsNone(fx.embed(wait_seconds=BUDGET))
        elapsed = time.monotonic() - started
        self.assertEqual(asked[:2], [0.0, 0.0])
        self.assertLessEqual(asked[3], BUDGET / 2)  # the table used at least half the budget
        self.assertLess(elapsed, BUDGET + SLACK)

    def test_a_display_bug_never_takes_the_page_down_and_is_logged_once(self):
        seen = []

        class _See(logging.Handler):
            def emit(self, record):
                seen.append(record.getMessage())

        handler = _See(level="WARNING")
        fx._LOG.addHandler(handler)
        self.addCleanup(fx._LOG.removeHandler, handler)
        warned, fx._WARNED = set(fx._WARNED), set()
        self.addCleanup(setattr, fx, "_WARNED", warned)

        class _Broken:
            @staticmethod
            def get(now=None, wait_seconds=0.0):
                raise RuntimeError("boom")

        self._patch(fx, "_RATES", _Broken())  # a cache that raises instead of answering
        self.assertIsNone(fx.embed())
        self.assertIsNone(fx.embed())
        self.assertEqual(seen, ["worth figures failed unexpectedly: RuntimeError: boom"])

    def test_the_embedded_json_cannot_close_a_script_element(self):
        self.assertEqual(fx.embed_json(None), "null")
        data = {"name": 'a<b>&"\'é日', "ok": True}
        text = fx.embed_json(data)
        self.assertEqual(text, '{"name":"a\\u003cb\\u003e\\u0026\\"\'\\u00e9\\u65e5","ok":true}')
        self.assertEqual(json.loads(text), data)
        with self.assertRaises(ValueError):
            fx.embed_json({"figure": float("nan")})


class LiveSampleWorthTests(_ConfigCase):
    """Figures worked out from the published table the parsers were written against."""

    CODES = ["USD", "EUR", "GBP", "INR", "JPY", "KRW", "MXN"]

    def setUp(self):
        super().setUp()
        fx.reset()
        self.clock = _Clock()
        replies = [_Reply(_FF_RATES), _Reply(_FF_CURRENCIES)]
        for name, value in (
            ("_clock", self.clock),
            ("_urlopen", lambda *args, **kwargs: replies.pop(0)),
        ):
            self._on(fx, name, value)
        self._on(fx.pricing, "get_maza_usd", lambda: (Decimal("0.000014"), "lore"))

    def _on(self, target, name, value):
        patcher = mock.patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _figures(self, maza):
        values = fx.worth(maza, self.CODES, wait_seconds=2)["values"]
        return {code: figure["amount"] for code, figure in values.items()}

    def test_a_million_maza_across_seven_published_rates(self):
        self.assertEqual(self._figures(1000000), {
            "USD": "14.00", "EUR": "12.79", "GBP": "10.69", "INR": "1,295.00",
            "JPY": "2,230.76", "KRW": "18,811.38", "MXN": "273.40",
        })

    def test_five_million_maza_is_seventy_dollars_and_rounds_half_up(self):
        self.assertEqual(self._figures(5000000), {
            "USD": "70.00", "EUR": "63.93", "GBP": "53.47", "INR": "6,475.00",
            "JPY": "11,153.80", "KRW": "94,056.90", "MXN": "1,367.00",
        })

    def test_one_maza_keeps_its_significant_digits(self):
        self.assertEqual(self._figures(1), {
            "USD": "0.000014", "EUR": "0.00001279", "GBP": "0.00001069", "INR": "0.0013",
            "JPY": "0.0022", "KRW": "0.0188", "MXN": "0.0003",
        })

    def test_every_offered_currency_is_named_dated_and_gets_a_figure(self):
        codes = [code for code, _, _ in fx.options(wait_seconds=2)]
        self.assertEqual((len(codes), codes[0]), (FIAT, "USD"))
        values = fx.worth(1000000, codes, wait_seconds=2)["values"]
        self.assertEqual(list(values), codes)
        self.assertEqual(values["EUR"], {"code": "EUR", "amount": "12.79", "name": "Euro", "symbol": "€", "date": DAY})
        self.assertTrue(all(figure["date"] in (DAY, "2026-11-25") for figure in values.values()), values)
        compact = fx.embed(wait_seconds=2)
        self.assertIsNotNone(compact)
        self.assertEqual(compact["c"], [[code, values[code]["name"], values[code]["symbol"]] for code in codes])
