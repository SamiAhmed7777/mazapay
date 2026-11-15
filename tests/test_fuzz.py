"""Property/fuzz battery: garbage + boundary inputs, fixed seeds.

Stdlib only, deterministic (every RNG is random.Random(SEED) reseeded per
test method, so a failure reproduces identically on every run).

Targets and their contracts:

- api._usd_amount / api._confirmations — accept a parsed value or raise
  ApiError(4xx); NEVER any other exception (NaN/Infinity/huge exponents
  used to escape as InvalidOperation/OverflowError -> unhandled 500).
- pricing parsers (_price_paprika / _price_lore / quote_maza) — degrade to
  PriceUnavailable / ValueError on any input shape, including poisoned
  ticker payloads (NaN/Infinity prices, non-dict list elements); never an
  arithmetic escape.
- HTTP JSON handlers — garbage bodies must produce 4xx (or a legitimate
  2xx/404), never 5xx; the server must stay healthy after the barrage; the
  MIP export must remain parseable afterwards.
- mip_export rows/columns — every emitted cell neutralizes spreadsheet
  formula injection (leading '=', '+', '-' or '@', except plain numbers);
  unknown/configured columns are always present in every row.
- store — garbage writes fail cleanly (sqlite3.Error) or succeed; either
  way the append-only store and its audit trail stay intact (no partial
  writes, no corrupt rows, schema-level audit protection still armed).
"""
import csv
import json
import random
import sqlite3
import unittest
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

try:
    import _sandbox as _env
except ImportError:  # package-style invocation (python3 -m unittest tests.test_x)
    from tests import _sandbox as _env  # type: ignore

net_gate = _env.net_gate
requires_writable = _env.requires_writable

from mazapay import api, config, mip_export, pricing, store  # noqa: E402
from mazapay.pricing import PriceUnavailable  # noqa: E402

SEED = 20261114  # fixed: every failure is reproducible
TOKEN = "fuzz-token"

# garbage corpus shared by the validator/HTTP fuzzers (fixed, curated)
GARBAGE_SCALARS: List[Any] = [
    None, "", " ", 0, 0.0, -0.0, -1, 1, 0.5, "0", "-0", "abc", "12abc",
    "NaN", "nan", "-nan", "inf", "-inf", "Infinity", "-Infinity",
    "1E999999999", "-1E999999999", "1e309", "-1e309", "0.000000001",
    10**40, -(10**40), 1e308, 1e309, float("nan"), float("inf"),
    float("-inf"), True, False, [], {}, {"x": 1}, [1], "١٢٣", "１２３",
    "1_000", "1,000", " 12 ", "+12", ".5", "5.", "e5", "1e", "--5",
    "\x00", "12\x0012", "٤.٥",
]
FORMULA_PREFIXES = ("=", "+", "-", "@")
HOSTILE_TEXT = [
    "=cmd|'/c calc'!A1", "+SUM(A1:A9)", "@SUM(1+1)", "-2+3+cmd|'x'!A0",
    "-12.50", "12.50", "'already-quoted", "plain text", "MAZA 5.00 @ 0.002",
    "=HYPERLINK(\"http://evil.example\")", "@danger", "\t=tab-prefixed",
    "＝fullwidth-equals", "autoexec.bat", "",
]


def _rand_garbage(rnd: random.Random, n: int = 200) -> List[str]:
    alphabet = "<>[]{}()!@#$%^&*-_=+.,;:'\"\\|/? \t0123456789eEabcXYZ"
    return ["".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 20))) for _ in range(n)]


def _rand_jsonish(rnd: random.Random, depth: int = 0) -> Any:
    """A random JSON-shaped value: scalar, list, or dict, up to depth 3."""
    roll = rnd.random()
    if depth >= 3 or roll < 0.6:
        return rnd.choice(
            [None, True, False, 0, 1, -1, 0.5, 1e308, 1e309, float("nan"),
             float("inf"), float("-inf"), "", "abc", "NaN", "Infinity",
             "1E999999999", "0.002", -0.002, "١٢٣", 10**30]
        )
    if roll < 0.8:
        return [_rand_jsonish(rnd, depth + 1) for _ in range(rnd.randint(0, 3))]
    return {rnd.choice(["price", "quotes", "USD", "price_usd", "x", ""]):
            _rand_jsonish(rnd, depth + 1) for _ in range(rnd.randint(0, 3))}


class TestFuzzUsdAmount(unittest.TestCase):
    """api._usd_amount: parsed Decimal or ApiError(400) — nothing else."""

    def _check(self, value: Any) -> None:
        try:
            usd = api._usd_amount(value)
        except api.ApiError as exc:
            self.assertEqual(exc.status, 400, f"non-4xx ApiError for {value!r}")
            return
        except Exception as exc:  # noqa: BLE001
            self.fail(f"_usd_amount({value!r}) escaped {type(exc).__name__}: {exc}")
        self.assertIsInstance(usd, Decimal)
        self.assertTrue(usd.is_finite(), f"non-finite parse for {value!r}")
        self.assertGreater(usd, 0, f"non-positive parse for {value!r}")
        self.assertLessEqual(usd, Decimal("1000000000000"), f"over-ceiling parse for {value!r}")

    def test_corpus(self):
        for value in GARBAGE_SCALARS:
            self._check(value)

    def test_random_strings_fixed_seed(self):
        rnd = random.Random(SEED)
        for garbage in _rand_garbage(rnd):
            self._check(garbage)

    def test_pinned_regressions(self):
        # each of these produced an unhandled 500 before the fix
        for bad in (float("nan"), float("inf"), "nan", "Infinity", "1E999999999"):
            with self.assertRaises(api.ApiError) as ctx:
                api._usd_amount(bad)
            self.assertEqual(ctx.exception.status, 400)


class TestFuzzConfirmations(unittest.TestCase):
    """api._confirmations: int or ApiError(400) — nothing else."""

    CORPUS = GARBAGE_SCALARS + ["6", 6, 6.0, 6.5, -1, "0x10", " 7 ", 10**30, "٣", "1e999"]

    def _check(self, value: Any) -> None:
        try:
            confirms = api._confirmations(value)
        except api.ApiError as exc:
            self.assertEqual(exc.status, 400, f"non-4xx ApiError for {value!r}")
            return
        except Exception as exc:  # noqa: BLE001
            self.fail(f"_confirmations({value!r}) escaped {type(exc).__name__}: {exc}")
        self.assertIsInstance(confirms, int)
        self.assertNotIsInstance(confirms, bool)

    def test_corpus(self):
        for value in self.CORPUS:
            self._check(value)

    def test_random_strings_fixed_seed(self):
        rnd = random.Random(SEED)
        for garbage in _rand_garbage(rnd):
            self._check(garbage)

    def test_pinned_regressions(self):
        # 1e999 parses to float('inf'); int(inf) used to escape OverflowError -> 500
        for bad in (1e999, float("inf"), float("-inf")):
            with self.assertRaises(api.ApiError) as ctx:
                api._confirmations(bad)
            self.assertEqual(ctx.exception.status, 400)


class TestFuzzPricingParsers(unittest.TestCase):
    """Ticker parsing degrades to PriceUnavailable; quote_maza to ValueError."""

    def setUp(self):
        self._old = (config.PRICE_SOURCE, config.ALLOW_FIXED_PRICE, config.LORE_TICKER_URL)
        config.ALLOW_FIXED_PRICE = False  # a poisoned feed must not fall back silently

    def tearDown(self):
        config.PRICE_SOURCE, config.ALLOW_FIXED_PRICE, config.LORE_TICKER_URL = self._old

    def _paprika(self, payload: Any):
        config.PRICE_SOURCE = "paprika"
        with mock.patch.object(pricing, "_fetch_json", return_value=payload):
            try:
                return pricing.get_maza_usd()
            except PriceUnavailable:
                return None

    def _lore(self, payload: Any):
        config.PRICE_SOURCE = "lore"
        config.LORE_TICKER_URL = "http://lore.invalid"
        with mock.patch.object(pricing, "_fetch_json", return_value=payload):
            try:
                return pricing.get_maza_usd()
            except PriceUnavailable:
                return None

    def _assert_price_or_none(self, got: Any, label: str) -> None:
        if got is None:
            return
        price, source = got
        self.assertIsInstance(price, Decimal, label)
        self.assertTrue(price.is_finite(), f"non-finite price accepted ({label})")
        self.assertGreater(price, 0, f"non-positive price accepted ({label})")
        self.assertIsInstance(source, str, label)

    def test_paprika_poisoned_shapes(self):
        poisoned = [
            {}, {"quotes": None}, {"quotes": "x"}, {"quotes": {}},
            {"quotes": {"USD": None}}, {"quotes": {"USD": {}}},
            {"quotes": {"USD": {"price": None}}}, {"quotes": {"USD": {"price": "abc"}}},
            {"quotes": {"USD": {"price": float("nan")}}},
            {"quotes": {"USD": {"price": float("inf")}}},
            {"quotes": {"USD": {"price": float("-inf")}}},
            {"quotes": {"USD": {"price": -1}}}, {"quotes": {"USD": {"price": 0}}},
            {"quotes": {"USD": {"price": [1]}}}, {"quotes": {"USD": {"price": {"a": 1}}}},
            "not-a-dict", None, [], [1, 2], 42,
        ]
        for payload in poisoned:
            self._assert_price_or_none(self._paprika(payload), repr(payload))
        # the one healthy shape still parses exactly
        got = self._paprika({"quotes": {"USD": {"price": "0.001745"}}})
        self.assertIsNotNone(got)
        self.assertEqual(got[0], Decimal("0.001745"))

    def test_paprika_random_payloads_fixed_seed(self):
        rnd = random.Random(SEED)
        for _ in range(300):
            self._assert_price_or_none(self._paprika(_rand_jsonish(rnd)), "random payload")

    def test_lore_poisoned_shapes(self):
        poisoned = [
            ["garbage"], [{"price_usd": "abc"}], [{"price_usd": float("nan")}],
            [{"price_usd": float("inf")}], [{"price_usd": 0}], [{}], [], "x",
            None, [{"price_usd": -2}], [None], [{"price_usd": [1]}],
            [{"price_usd": {"a": 1}}], [42], [{"other": 1}], [[{"price_usd": 1}]],
            [{"price_usd": ""}],
        ]
        for payload in poisoned:
            self._assert_price_or_none(self._lore(payload), repr(payload))
        got = self._lore([{"price_usd": "0.001"}])
        self.assertIsNotNone(got)
        self.assertEqual(got[0], Decimal("0.001"))

    def test_quote_maza_garbage_amounts(self):
        config.PRICE_SOURCE = "fixed"
        for value in GARBAGE_SCALARS + ["0.0000001", "1E999999999", {"a": 1}]:
            try:
                maza, rate, source = pricing.quote_maza(value)
            except ValueError:
                continue
            except Exception as exc:  # noqa: BLE001
                self.fail(f"quote_maza({value!r}) escaped {type(exc).__name__}: {exc}")
            self.assertTrue(maza.is_finite() and maza > 0, f"bad quote for {value!r}")
            self.assertTrue(rate.is_finite() and rate > 0)
            self.assertIsInstance(source, str)

    def test_quote_maza_garbage_prices(self):
        config.PRICE_SOURCE = "fixed"
        for price in (0, -1, float("nan"), float("inf"), "nan", "Infinity", "abc", 1e-999, [1], {}):
            try:
                maza, rate, _source = pricing.quote_maza(10, price=price)
            except ValueError:
                continue
            except Exception as exc:  # noqa: BLE001
                self.fail(f"quote_maza(price={price!r}) escaped {type(exc).__name__}: {exc}")
            self.assertTrue(maza.is_finite() and maza > 0, f"bad quote for price={price!r}")

    def test_pinned_regressions(self):
        config.PRICE_SOURCE = "fixed"
        # every one of these escaped a decimal arithmetic exception pre-fix
        for bad in ("nan", float("inf"), "1E999999999", float("nan"), "Infinity"):
            with self.assertRaises(ValueError):
                pricing.quote_maza(bad)
        with self.assertRaises(ValueError):
            pricing.quote_maza(10, price=float("nan"))


@requires_writable
class TestFuzzMipExport(unittest.TestCase):
    """CSV rows: formula injection neutralized; columns always exact."""

    def setUp(self):
        store.init_db()
        self._export_dir = config.MIP_EXPORT_DIR  # _sandbox tmp dir

    def _hostile_receipt(self) -> Dict[str, Any]:
        vendor = store.create_vendor(
            name="=cmd|'/c calc'!A1",
            code=f"+HYPERLINK{uuid.uuid4().hex[:8]}",  # codes are UNIQUE
            maza_address="", notes="@x",
        )
        order = store.create_order(
            "in", 5.0, 2500.0, 0.002, "fixed", vendor_id=vendor["id"],
            ref="@SUM(1+1)@ref", address="tMAZA-FUZZ",
        )
        store.update_order(
            order["id"],
            {"status": "paid", "paid_ts": 1_700_000_000.0, "txid": "-2+3+cmd|'calc'!A0"},
            "fuzz_paid",
        )
        return order

    def _read_csv(self, path: str) -> List[List[str]]:
        with open(path, newline="", encoding="utf-8") as fh:
            return list(csv.reader(fh))

    def _assert_no_formula_cells(self, rows: List[List[str]]) -> None:
        for row in rows[1:]:  # skip header
            for cell in row:
                self.assertFalse(
                    cell.startswith(("=", "+", "@")),
                    f"formula cell survived neutralization: {cell!r}",
                )
                if cell.startswith("-"):
                    self.assertRegex(
                        cell, r"^-?\d+(\.\d+)?$",
                        f"negative-looking text cell not neutralized: {cell!r}",
                    )

    def test_hostile_receipt_export_is_neutralized(self):
        self._hostile_receipt()
        result = mip_export.export_cash_receipts(include_exported=True)
        self.assertGreaterEqual(result["rows"], 1)
        rows = self._read_csv(result["path"])
        self._assert_no_formula_cells(rows)
        blob = "\n".join(",".join(r) for r in rows)
        self.assertIn("'=cmd|'/c calc'!A1", blob.replace('"', ""))  # neutralized, intact

    def test_rows_never_lose_or_invent_columns(self):
        for columns in (
            config.CASH_RECEIPT_COLUMNS,
            config.CASH_DISBURSEMENT_COLUMNS,
            ["SESSION_ID", "BOGUS_COLUMN"],
            [],
        ):
            rows = mip_export._rows([], columns)
            self.assertEqual(rows, [])
        order = self._hostile_receipt()
        for columns in (["SESSION_ID", "BOGUS_COLUMN"], ["AMOUNT"], config.CASH_RECEIPT_COLUMNS):
            rows = mip_export._rows([store.get_order(order["id"])], columns)
            self.assertEqual(len(rows), 1)
            self.assertEqual(list(rows[0].keys()), columns, f"column drift for {columns}")
            self.assertEqual(rows[0].get("BOGUS_COLUMN", ""), "")

    def test_rows_random_hostile_text_fixed_seed(self):
        rnd = random.Random(SEED)
        order = self._hostile_receipt()
        base = store.get_order(order["id"])
        for _ in range(120):
            hostile = rnd.choice(HOSTILE_TEXT)
            victim = rnd.choice(["ref", "txid", "rate_source", "vendor_id", "id"])
            variant = dict(base)
            variant[victim] = hostile
            for columns in (config.CASH_RECEIPT_COLUMNS,):
                for row in mip_export._rows([variant], columns):
                    self._assert_no_formula_cells([[]] + [list(row.values())])

    def test_amount_column_stays_numeric(self):
        order = self._hostile_receipt()
        rows = mip_export._rows([store.get_order(order["id"])], ["AMOUNT"])
        self.assertEqual(rows[0]["AMOUNT"], "5.00")  # legit numbers are untouched
        # fractional-without-leading-digit numbers are real numbers, not text
        self.assertEqual(mip_export._safe_cell("-.5"), "-.5")
        self.assertEqual(mip_export._safe_cell("+.25"), "'+.25")  # "+" is never numeric


@requires_writable
class TestFuzzStore(unittest.TestCase):
    """Garbage writes fail cleanly or succeed — never corrupt the store."""

    def setUp(self):
        store.init_db()
        self.order = store.create_order("in", 10.0, 5000.0, 0.002, "fixed", address="tMAZA-Z")
        self._audit_before = self._audit_count()

    def _audit_count(self) -> int:
        with store.db() as conn:
            return conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0]

    def _orders_intact(self) -> None:
        with store.db() as conn:
            rows = conn.execute(
                "SELECT id, kind, status, created_ts FROM orders"
            ).fetchall()
        self.assertTrue(rows)
        for row in rows:
            self.assertTrue(row["id"])
            self.assertIn(row["kind"], ("in", "out"))
            self.assertTrue(row["status"])
            self.assertIsNotNone(row["created_ts"])
        target = store.get_order(self.order["id"])
        self.assertIsNotNone(target)

    def test_get_order_injection_strings(self):
        rnd = random.Random(SEED)
        attempts = [
            "'; DROP TABLE orders; --", "' OR '1'='1", "%", "", "ord_",
            "x" * 10000, "١٢٣", "\x00", *["".join(rnd.choice("';--%_\"\\ ") for _ in range(12)) for _ in range(50)],
        ]
        for probe in attempts:
            got = store.get_order(probe)
            self.assertIn(got, (None,), f"get_order({probe[:20]!r}) matched something")
        self.assertIsNotNone(store.get_order(self.order["id"]))  # real rows unaffected
        self._orders_intact()

    def test_create_order_garbage_amounts_fail_clean(self):
        for amount in (float("nan"), float("inf"), -(10**40), {"a": 1}, [1]):
            try:
                made = store.create_order("in", amount, 1.0, 0.002, "fixed")
            except (TypeError, ValueError, sqlite3.Error):
                continue  # rejected at the coercion/bind/constraint layer: clean
            except Exception as exc:  # noqa: BLE001
                self.fail(f"create_order({amount!r}) raised {type(exc).__name__}: {exc}")
            # if it somehow stored, the row must be sane
            self.assertTrue(made["id"])
            self._orders_intact()
        self._orders_intact()

    def test_update_order_garbage_keys_are_atomic(self):
        garbage_fields = [
            {"status' --": "pwned"},
            {"status='pwned', vendor_id": "x"},  # valid injected SQL pre-whitelist!
            {"DROP TABLE orders": 1},
            {"status": {"a": 1}},
            {"status": [1]},
            {"\x00": 1},
        ]
        for fields in garbage_fields:
            try:
                store.update_order(self.order["id"], fields, "fuzz_update")
            except (ValueError, sqlite3.Error):
                pass  # expected: rejected by the column whitelist or the bind
            except Exception as exc:  # noqa: BLE001
                self.fail(f"update_order({fields!r}) raised {type(exc).__name__}: {exc}")
            fresh = store.get_order(self.order["id"])
            self.assertEqual(fresh["status"], "quoted", f"corrupted by {fields!r}")
        self.assertEqual(self._audit_count(), self._audit_before)
        self._orders_intact()

    def test_transition_garbage_statuses_fail_clean(self):
        for from_status, to_status in (("quoted", [1]), ({"a": 1}, "paid")):
            try:
                won = store.transition(self.order["id"], from_status, to_status)
            except sqlite3.Error:
                won = False
            self.assertFalse(bool(won))
        # field keys go through the same column whitelist as update_order
        with self.assertRaises(ValueError):
            store.transition(self.order["id"], "quoted", "paid", fields={"pwned": 1})
        self.assertEqual(store.get_order(self.order["id"])["status"], "quoted")
        self._orders_intact()

    def test_audit_still_append_only_after_barrage(self):
        with store.db() as conn:
            with self.assertRaises(sqlite3.Error):
                conn.execute("UPDATE audit SET event='tampered'")
            with self.assertRaises(sqlite3.Error):
                conn.execute("DELETE FROM audit")
        self._orders_intact()


@net_gate
class TestFuzzApiHttp(unittest.TestCase):
    """Garbage over real HTTP: 4xx-or-2xx only, server stays healthy."""

    @classmethod
    def setUpClass(cls):
        store.init_db()
        cls._old_token = config.API_TOKEN
        config.API_TOKEN = TOKEN
        cls.srv = api.serve("127.0.0.1", 0, with_watcher=False)
        cls.port = cls.srv.server_address[1]
        cls.pool = ThreadPoolExecutor(max_workers=1)
        cls.pool.submit(cls.srv.serve_forever)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        api.shutdown_watcher()
        cls.pool.shutdown(wait=True)
        config.API_TOKEN = cls._old_token

    def _req(
        self,
        method: str,
        path: str,
        body: Any = None,
        raw: Optional[bytes] = None,
        token: Optional[str] = TOKEN,
    ) -> Tuple[int, Any]:
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method, headers=headers
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw_body = resp.read().decode()
                try:
                    return resp.status, json.loads(raw_body)
                except ValueError:
                    return resp.status, raw_body
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode() or "{}"
            exc.close()
            try:
                return exc.code, json.loads(payload)
            except ValueError:
                return exc.code, payload

    # -- pinned regressions: each of these was a 500 before the fix ------------
    def test_pinned_regressions_are_4xx(self):
        status, _ = self._req("POST", "/orders", {"usd_amount": float("nan")})
        self.assertEqual(status, 400)
        status, _ = self._req("POST", "/orders", {"usd_amount": float("inf")})
        self.assertEqual(status, 400)
        status, _ = self._req("POST", "/orders", {"usd_amount": "1E999999999"})
        self.assertEqual(status, 400)
        status, _ = self._req("POST", "/orders", {"usd_amount": "Infinity"})
        self.assertEqual(status, 400)
        status, _ = self._req("POST", "/orders", {"usd_amount": 5, "ref": {"x": 1}})
        self.assertEqual(status, 400)
        status, _ = self._req("POST", "/orders", {"usd_amount": 5, "vendor_id": [1]})
        self.assertEqual(status, 400)
        status, _ = self._req("POST", "/vendors", {"name": {"x": 1}, "code": "C"})
        self.assertEqual(status, 400)
        status, _ = self._req("POST", "/vendors", {"name": 5, "code": "C"})
        self.assertEqual(status, 400)
        status, _ = self._req(
            "POST", "/payments", {"address": {"x": 1}, "usd_amount": 1.0}
        )
        self.assertEqual(status, 400)
        order = self._req("POST", "/orders", {"usd_amount": 1.0})[1]
        status, _ = self._req("POST", f"/orders/{order['id']}/simulate_payment", {"confirms": 1e999})
        self.assertEqual(status, 400)
        status, _ = self._req("POST", f"/orders/{order['id']}/simulate_payment", {"confirms": float("inf")})
        self.assertEqual(status, 400)

    # -- the barrage ------------------------------------------------------------
    GARBAGE_BODIES = [
        {}, [], "str", 42, None, {"usd_amount": None}, {"usd_amount": "nan"},
        {"usd_amount": float("nan")}, {"usd_amount": float("inf")},
        {"usd_amount": float("-inf")}, {"usd_amount": -0.0}, {"usd_amount": "0"},
        {"usd_amount": -5}, {"usd_amount": "abc"}, {"usd_amount": "1.0.0"},
        {"usd_amount": "1E999999999"}, {"usd_amount": 10**40}, {"usd_amount": [1]},
        {"usd_amount": {"a": 1}}, {"usd_amount": True}, {"usd_amount": "١٢٣"},
        {"usd_amount": 1, "ref": {"x": 1}}, {"usd_amount": 1, "ref": [1]},
        {"usd_amount": 1, "vendor_id": {}}, {"usd_amount": 1, "vendor_id": 3},
        {"usd_amount": 1, "vendor_id": "\x00"},
        {"name": {"x": 1}, "code": "C"}, {"name": 5, "code": "C"},
        {"name": "N", "code": []}, {"name": "N", "code": "C", "notes": {}},
        {"name": "N", "code": "C", "maza_address": [1]},
        {"address": {}, "usd_amount": 1}, {"address": 123, "usd_amount": 1},
        {"address": "", "usd_amount": 1}, {"address": "MOK", "usd_amount": "nan"},
        {"address": "MOK", "usd_amount": 1e999},
        {"address": "MOK", "usd_amount": 1, "ref": {"deep": [1, 2]}},
        {"confirms": 1e999}, {"confirms": "nan"}, {"confirms": [6]},
        {"confirms": {"x": 1}}, {"confirms": "0x10"}, {"confirms": 6.5},
        {"address": "=cmd|'/c calc'!A1", "usd_amount": 1},
    ]

    def _routes(self) -> List[Tuple[str, str, bool]]:
        order = self._req("POST", "/orders", {"usd_amount": 2.0})[1]
        pay = self._req("POST", "/payments", {"address": "MADDR-F", "usd_amount": 1.0})[1]
        return [
            ("POST", "/orders", True),
            ("POST", "/vendors", True),
            ("POST", "/payments", True),
            ("POST", f"/orders/{order['id']}/simulate_payment", True),
            ("POST", f"/orders/{order['id']}/accept", True),
            ("POST", f"/orders/{order['id']}/refund", True),
            ("POST", f"/payments/{pay['id']}/reconcile", True),
            ("POST", f"/payments/{pay['id']}/retry", True),
            ("POST", "/watch", True),
            ("GET", "/export/mip?kind=in", True),
            ("GET", "/export/mip?kind=out", True),
            ("GET", "/export/mip?kind=<script>", True),
            ("GET", "/export/mip?include_exported=garbage", True),
            ("POST", "/orders", False),   # public collect routes get the barrage too
            ("GET", f"/orders/{order['id']}", False),
        ]

    def test_garbage_bodies_never_500(self):
        rnd = random.Random(SEED)
        routes = self._routes()
        seen_statuses: Dict[str, int] = {}
        for method, path, needs_token in routes:
            token = TOKEN if needs_token else None
            for body in self.GARBAGE_BODIES:
                status, _payload = self._req(method, path, body, token=token)
                seen_statuses[f"{method} {path}"] = status
                self.assertLess(
                    status, 500, f"{method} {path} body={body!r} -> {status} (5xx)"
                )
            for garbage in _rand_garbage(rnd, 25):
                status, _payload = self._req(method, path, {"fuzz": garbage}, token=token)
                self.assertLess(status, 500, f"{method} {path} fuzz={garbage!r} -> {status}")
        # basic sanity: sane requests on the same routes still work afterwards
        status, health = self._req("GET", "/health", token=None)
        self.assertEqual(status, 200)
        status, order = self._req("POST", "/orders", {"usd_amount": 3.0}, token=None)
        self.assertEqual(status, 201)

    def test_raw_and_malformed_framing(self):
        cases = [
            (b"", None),                                   # no body at all
            (b"not json", None),                           # invalid JSON
            (b"\xff\xfe\x00garbage", None),                # invalid UTF-8
            (b"null", None),                               # valid JSON, not an object
            (b"[]", None),
            (b"\"str\"", None),
            (b"42", None),
            (b"[" * 50_000 + b"]" * 50_000, None),         # deep nesting
            (b"{\"usd_amount\": 1, \"ref\": \"" + b"A" * 200_000 + b"\"}", None),  # huge field
        ]
        for raw, _ in cases:
            for path in ("/orders", "/payments", "/vendors"):
                status, _payload = self._req("POST", path, raw=raw)
                self.assertIn(status, (200, 201, 400, 404), f"POST {path} raw[{len(raw)}B] -> {status}")

    def test_weird_paths_stay_4xx(self):
        for path in (
            "/orders/%2e%2e%2fetc%2fpasswd", "/orders/..", "/orders/%00",
            "/orders/ord_%E9%A9%AC", "/export/mip?kind=in%00", "/%2f%2e%2e",
            "/orders/" + "A" * 2000, "/orders/ord_x?x=" + "y" * 2000,
        ):
            status, _payload = self._req("GET", path, token=TOKEN)
            # authed on purpose: export-query garbage is a 400, everything
            # else (unknown paths/ids) a 404
            self.assertIn(status, (400, 404), f"GET {path[:40]} -> {status}")

    def test_oversized_declared_body_is_413(self):
        """A declared Content-Length over the cap is refused before buffering."""
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(
                "POST", "/orders", body=b"x",
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": str(20 * 1024 * 1024),  # declared, not sent
                    "Authorization": f"Bearer {TOKEN}",
                },
            )
            resp = conn.getresponse()
            self.assertEqual(resp.status, 413)
        finally:
            conn.close()
        # the server is still healthy afterwards
        status, _ = self._req("GET", "/health", token=None)
        self.assertEqual(status, 200)

    def test_export_stays_parseable_after_barrage(self):
        # seed one valid paid receipt so the export always has a row to emit
        order = self._req("POST", "/orders", {"usd_amount": 2.5}, token=None)[1]
        self._req("POST", f"/orders/{order['id']}/simulate_payment", {})
        self._req("POST", "/watch", {})
        # the barrage above created junk refs/unicode orders; export them all
        status, result = self._req("GET", "/export/mip?kind=in&include_exported=1")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(result["rows"], 1)
        with open(result["path"], newline="", encoding="utf-8") as fh:
            rows = list(csv.reader(fh))
        self.assertTrue(rows)
        width = len(rows[0])
        for row in rows:
            self.assertEqual(len(row), width, "ragged CSV row — corrupted export")
            for cell in row:
                self.assertFalse(cell.startswith(("=", "+", "@")), f"formula cell: {cell!r}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
