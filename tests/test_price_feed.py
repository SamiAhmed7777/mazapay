"""Price-feed resilience tests: live-source failover, honest source labels,
feed-health state, and the fixed-price gating contract.

2026-11-15 live finding: the paprika default source 403s from datacenter
hosts (IP-level, UA-independent); CoinLore (MAZA id 184745) carried the first
real E2E quote. Failover order is primary -> other configured live source ->
gated 'fixed-fallback' (never silent), and every quote's source label names
what actually served.
"""
import unittest
from decimal import Decimal, ROUND_DOWN
from unittest import mock

try:
    import _sandbox as _env
except ImportError:  # package-style invocation (python3 -m unittest tests.test_x)
    from tests import _sandbox as _env  # type: ignore

requires_writable = _env.requires_writable

from mazapay import config, pricing  # noqa: E402


def _lore_body(price="0.001229"):
    # Verified live shape: [{"id": "184745", "price_usd": "0.001229", ...}]
    return [{"id": "184745", "price_usd": price, "name": "MazaCoin", "symbol": "MAZA"}]


def _paprika_body(price="0.001229"):
    # Verified live shape: {"id": "maza-maza", "quotes": {"USD": {"price": ...}}}
    return {"id": "maza-maza", "quotes": {"USD": {"price": float(price)}}}


def _expected_maza(usd: str, rate: str) -> Decimal:
    granularity = Decimal(10) ** -int(config.MAZA_DECIMALS)
    return (Decimal(usd) / Decimal(rate)).quantize(granularity, rounding=ROUND_DOWN)


class PriceFeedFailoverTests(unittest.TestCase):
    def setUp(self):
        self._saved = (
            config.PRICE_SOURCE,
            config.ALLOW_FIXED_PRICE,
            config.LORE_TICKER_URL,
            config.FIXED_USD_PRICE,
        )
        pricing._FEED_STATE.clear()

    def tearDown(self):
        (
            config.PRICE_SOURCE,
            config.ALLOW_FIXED_PRICE,
            config.LORE_TICKER_URL,
            config.FIXED_USD_PRICE,
        ) = self._saved
        pricing._FEED_STATE.clear()

    @requires_writable
    def test_paprika_fail_lore_serves_with_honest_label(self):
        config.PRICE_SOURCE = "paprika"
        config.LORE_TICKER_URL = "https://lore.example/ticker?id=184745"
        responses = [OSError("403 forbidden from datacenter IP"), _lore_body("0.00125")]
        with mock.patch.object(pricing, "_fetch_json", side_effect=responses) as fj:
            price, source = pricing.get_maza_usd()
        self.assertEqual(source, "lore")  # label names the source that served
        self.assertEqual(price, Decimal("0.00125"))
        self.assertEqual(fj.call_count, 2)
        state = pricing.feed_health()["sources"]
        self.assertFalse(state["paprika"]["ok"])
        self.assertTrue(state["lore"]["ok"])
        self.assertIsNone(state["paprika"]["last_ok_ts"])
        self.assertIsNotNone(state["lore"]["last_ok_ts"])

    @requires_writable
    def test_all_live_fail_raises_without_fixed(self):
        config.PRICE_SOURCE = "paprika"
        config.LORE_TICKER_URL = "https://lore.example/ticker?id=184745"
        config.ALLOW_FIXED_PRICE = False
        with mock.patch.object(pricing, "_fetch_json", side_effect=OSError("down")):
            with self.assertRaises(pricing.PriceUnavailable) as ctx:
                pricing.get_maza_usd()
        msg = str(ctx.exception)
        self.assertIn("paprika", msg)  # both sources named in the failure
        self.assertIn("lore", msg)
        self.assertNotIn("fixed-fallback", msg)

    @requires_writable
    def test_all_live_fail_allow_fixed_keeps_fallback_label(self):
        config.PRICE_SOURCE = "paprika"
        config.LORE_TICKER_URL = "https://lore.example/ticker?id=184745"
        config.ALLOW_FIXED_PRICE = True
        with mock.patch.object(pricing, "_fetch_json", side_effect=OSError("down")):
            price, source = pricing.get_maza_usd()
        self.assertEqual(source, "fixed-fallback")  # auditable synthetic label
        self.assertEqual(price, Decimal(str(config.FIXED_USD_PRICE)))

    @requires_writable
    def test_fixed_primary_probes_nothing(self):
        config.PRICE_SOURCE = "fixed"
        boom = AssertionError("network probe attempted in explicit fixed mode")
        with mock.patch.object(pricing, "_fetch_json", side_effect=boom):
            price, source = pricing.get_maza_usd()
        self.assertEqual(source, "fixed")
        self.assertEqual(price, Decimal(str(config.FIXED_USD_PRICE)))

    @requires_writable
    def test_lore_primary_fails_paprika_serves(self):
        config.PRICE_SOURCE = "lore"
        config.LORE_TICKER_URL = "https://lore.example/ticker?id=184745"
        responses = [OSError("lore down"), _paprika_body("0.0011")]
        with mock.patch.object(pricing, "_fetch_json", side_effect=responses):
            price, source = pricing.get_maza_usd()
        self.assertEqual(source, "paprika")
        self.assertEqual(price, Decimal("0.0011"))

    @requires_writable
    def test_unconfigured_lore_is_skipped_not_attempted(self):
        config.PRICE_SOURCE = "paprika"
        config.LORE_TICKER_URL = ""  # failover source explicitly disabled
        with mock.patch.object(pricing, "_fetch_json", side_effect=OSError("down")) as fj:
            with self.assertRaises(pricing.PriceUnavailable):
                pricing.get_maza_usd()
        self.assertEqual(fj.call_count, 1)  # paprika only; lore never probed
        self.assertNotIn("lore", pricing.feed_health()["failover_order"])

    @requires_writable
    def test_quote_maza_carries_failover_source(self):
        config.PRICE_SOURCE = "paprika"
        config.LORE_TICKER_URL = "https://lore.example/ticker?id=184745"
        responses = [OSError("403"), _lore_body("0.001")]
        with mock.patch.object(pricing, "_fetch_json", side_effect=responses):
            maza, rate, source = pricing.quote_maza("1.00")
        self.assertEqual(source, "lore")
        self.assertEqual(rate, Decimal("0.001"))
        self.assertEqual(maza, _expected_maza("1.00", "0.001"))

    @requires_writable
    def test_feed_health_shape_and_failover_order(self):
        config.PRICE_SOURCE = "paprika"
        config.LORE_TICKER_URL = "https://lore.example/ticker?id=184745"
        health = pricing.feed_health()
        self.assertEqual(health["primary"], "paprika")
        self.assertEqual(health["failover_order"], ["paprika", "lore"])
        for name in ("paprika", "lore"):
            entry = health["sources"][name]
            self.assertIsNone(entry["ok"])  # never attempted -> null, not False
            self.assertIsNone(entry["error"])
            self.assertIsNone(entry["last_attempt_ts"])
        # explicit fixed mode: failover order is empty — probes nothing
        config.PRICE_SOURCE = "fixed"
        self.assertEqual(pricing.feed_health()["failover_order"], [])

    @requires_writable
    def test_feed_error_is_capped(self):
        config.PRICE_SOURCE = "paprika"
        config.LORE_TICKER_URL = ""
        huge = OSError("x" * 5000)
        with mock.patch.object(pricing, "_fetch_json", side_effect=huge):
            with self.assertRaises(pricing.PriceUnavailable):
                pricing.get_maza_usd()
        recorded = pricing.feed_health()["sources"]["paprika"]["error"]
        self.assertLessEqual(len(recorded), 200)


if __name__ == "__main__":
    unittest.main()
