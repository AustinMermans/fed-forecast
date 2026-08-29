import json
import unittest
from pathlib import Path

from fed_forecast.parsing import (
    MarketParseError,
    RateBucket,
    parse_market,
    parse_rate_bucket,
)


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "live_events.json"


def load_live_fixture() -> dict[str, object]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def eligible_market(**changes: object) -> dict[str, object]:
    market: dict[str, object] = {
        "question": "Will the test outcome happen?",
        "groupItemTitle": "3.75%",
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": '["yes-token", "no-token"]',
        "outcomePrices": '["0.3", "0.7"]',
        "active": True,
        "closed": False,
        "acceptingOrders": True,
        "liquidityNum": 123.45,
    }
    market.update(changes)
    return market


class LiveFixtureTests(unittest.TestCase):
    def test_pinned_fixture_contains_full_live_event_shapes(self) -> None:
        fixture = load_live_fixture()
        self.assertEqual(len(fixture["policy_2026"]["markets"]), 15)
        self.assertEqual(len(fixture["ten_year_high_2026"]["markets"]), 10)
        self.assertEqual(len(fixture["ten_year_low_2026"]["markets"]), 9)
        policy = fixture["policy_2026"]["markets"]
        self.assertIn("3.5%", {market["groupItemTitle"] for market in policy})
        self.assertIn("3.75%", {market["groupItemTitle"] for market in policy})
        self.assertIn("4.0%", {market["groupItemTitle"] for market in policy})
        self.assertIn("≤1.0%", {market["groupItemTitle"] for market in policy})
        self.assertIn("≥ 4.5%", {market["groupItemTitle"] for market in policy})
        anomaly = next(market for market in policy if market["groupItemTitle"] == "1.25")
        self.assertTrue(anomaly["question"].endswith("1.25% at the end of 2026?"))
        for event_id in ("policy_2026", "ten_year_high_2026", "ten_year_low_2026"):
            for market in fixture[event_id]["markets"]:
                for field in (
                    "outcomes",
                    "outcomePrices",
                    "clobTokenIds",
                    "active",
                    "closed",
                    "acceptingOrders",
                    "liquidityNum",
                ):
                    self.assertIn(field, market)
                lengths = {
                    len(json.loads(market[field]))
                    for field in ("outcomes", "outcomePrices", "clobTokenIds")
                }
                self.assertEqual(lengths, {2})
        self.assertTrue(any(market["closed"] for market in fixture["ten_year_high_2026"]["markets"]))
        self.assertTrue(any(market["closed"] for market in fixture["ten_year_low_2026"]["markets"]))


class RateBucketTests(unittest.TestCase):
    def test_rate_bucket_parses_exact_bounds_and_question_fallback(self) -> None:
        self.assertEqual(parse_rate_bucket("3.75%", "..."), RateBucket("exact", 3.75))
        self.assertEqual(parse_rate_bucket("≤1.0%", "..."), RateBucket("lte", 1.0))
        self.assertEqual(parse_rate_bucket("≥ 4.5%", "..."), RateBucket("gte", 4.5))
        self.assertEqual(
            parse_rate_bucket(
                "1.25",
                "Will the upper bound of the target federal funds rate be "
                "1.25% at the end of 2026?",
            ),
            RateBucket("exact", 1.25),
        )

    def test_rate_bucket_contains_uses_declared_comparator(self) -> None:
        self.assertTrue(RateBucket("exact", 3.75).contains(3.75 + 5e-10))
        self.assertFalse(RateBucket("exact", 3.75).contains(3.75 + 2e-9))
        self.assertTrue(RateBucket("lte", 1.0).contains(0.5))
        self.assertFalse(RateBucket("lte", 1.0).contains(1.01))
        self.assertTrue(RateBucket("gte", 4.5).contains(4.75))
        self.assertFalse(RateBucket("gte", 4.5).contains(4.49))


class MarketParsingTests(unittest.TestCase):
    def test_market_alignment_finds_yes_by_label(self) -> None:
        raw = eligible_market(
            outcomes='["No", "Yes"]',
            clobTokenIds='["no-token", "yes-token"]',
            outcomePrices='["0.7", "0.3"]',
        )
        parsed = parse_market(raw)
        self.assertEqual(parsed.yes_token, "yes-token")
        self.assertEqual(parsed.gamma_yes_price, 0.3)
        self.assertEqual(parsed.liquidity_num, 123.45)
        self.assertIs(parsed.raw, raw)

    def test_ineligible_and_misaligned_markets_fail(self) -> None:
        for change in (
            {"active": False},
            {"closed": True},
            {"acceptingOrders": False},
            {"outcomePrices": '["0.3"]'},
            {"outcomes": '["Yes", "YES"]'},
            {"outcomePrices": '["NaN", "0.7"]'},
        ):
            with self.subTest(change=change), self.assertRaises(MarketParseError):
                parse_market(eligible_market(**change))

    def test_every_outcome_price_must_be_finite(self) -> None:
        with self.assertRaises(MarketParseError):
            parse_market(eligible_market(outcomePrices='["0.3", "NaN"]'))


if __name__ == "__main__":
    unittest.main()
