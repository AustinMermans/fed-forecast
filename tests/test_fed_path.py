import math
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path

from fed_forecast.models import Diagnostic
from fed_forecast.fed_path import FedPathError, compute_fed_path, compute_meeting_distribution
from fed_forecast.fed_path_config import load_fed_path_config
from fed_forecast.fed_path_models import MeetingPrice


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FedPathMathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_fed_path_config(PROJECT_ROOT / "config/fed_path.json")

    def prices(self, meeting_index: int, values: tuple[float, ...]) -> tuple[MeetingPrice, ...]:
        meeting = self.config.meetings[meeting_index]
        return tuple(MeetingPrice(meeting.date, outcome.label, value) for outcome, value in zip(reversed(meeting.outcomes), reversed(values), strict=True))

    def distribution(self, meeting_index: int, values: tuple[float, ...]):
        return compute_meeting_distribution(self.config.meetings[meeting_index], self.prices(meeting_index, values))

    def terminal_prices(self, *, low: float = .02, high: float = .03) -> dict[str, float]:
        buckets = self.config.terminal_buckets
        result = {bucket.label: .01 for bucket in buckets}
        result[buckets[0].label] = low
        result[buckets[-1].label] = high
        result["3.75%"] = .3
        result["4.0%"] = .2
        return result

    def test_normalizes_out_of_order_prices_before_expected_value(self) -> None:
        distribution = self.distribution(0, (.05, .15, .4, .3, .2))
        self.assertEqual(distribution.raw_total, 1.1)
        self.assertEqual([item.label for item in distribution.prices], [outcome.label for outcome in self.config.meetings[0].outcomes])
        self.assertTrue(math.isclose(sum(item.probability for item in distribution.prices), 1.0, abs_tol=1e-12))
        self.assertTrue(math.isclose(distribution.expected_change_bp, 10.227272727272727, abs_tol=1e-12))
        self.assertTrue(distribution.tail_capped)
        self.assertEqual((distribution.decrease_probability, distribution.no_change_probability, distribution.increase_probability), (.18181818181818182, .36363636363636365, .45454545454545453))
        self.assertEqual((distribution.negative_tail_probability, distribution.positive_tail_probability), (.045454545454545456, .18181818181818182))

    def test_rejects_nonexact_topology_and_invalid_totals(self) -> None:
        meeting = self.config.meetings[0]
        prices = list(self.prices(0, (.2, .2, .2, .2, .2)))
        prices[-1] = MeetingPrice(meeting.date, "unknown", .2)
        with self.assertRaisesRegex(FedPathError, "topology"):
            compute_meeting_distribution(meeting, tuple(prices))
        with self.assertRaisesRegex(FedPathError, "raw total"):
            self.distribution(0, (0, 0, 0, 0, 0))

    def test_path_accumulates_meetings_and_substitutes_terminal_anchor(self) -> None:
        meetings = (
            self.distribution(0, (.05, .15, .4, .3, .2)),
            self.distribution(1, (.1, .2, .4, .2, .1)),
            self.distribution(2, (.2, .2, .4, .15, .05)),
        )
        result = compute_fed_path(self.config, meetings, self.terminal_prices(), generated_at="2026-07-24T12:00:00Z", snapshot_fetched_at="2026-07-24T11:59:00Z")
        self.assertEqual([point.kind for point in result.points], ["meeting_distribution", "meeting_distribution", "meeting_distribution", "terminal_anchor"])
        self.assertEqual(result.points[0].date, date(2026, 7, 29))
        self.assertTrue(math.isclose(result.points[0].implied_target_upper, 3.852272727272727, abs_tol=1e-12))
        december = result.points[-1]
        self.assertEqual(december.date, date(2026, 12, 9))
        self.assertTrue(math.isclose(december.implied_target_upper, result.terminal.expected_target_upper, abs_tol=1e-12))
        self.assertTrue(math.isclose(december.cumulative_moves, (december.implied_target_upper - 3.75) / .25, abs_tol=1e-12))
        self.assertTrue(math.isclose(december.implied_change_bp, (december.implied_target_upper - result.points[2].implied_target_upper) * 100, abs_tol=1e-12))
        codes = {diagnostic.code for diagnostic in result.diagnostics}
        self.assertTrue({"meeting_prices_renormalized", "tail_bucket_capped", "terminal_anchor_substitution", "no_polymarket_2027_coverage"} <= codes)

    def test_terminal_anchor_remains_available_after_all_configured_meetings(self) -> None:
        config = replace(
            self.config,
            target_upper_bound=3.5,
            effective_rate_baseline=3.37,
            meetings=(),
        )
        result = compute_fed_path(
            config,
            (),
            self.terminal_prices(),
            generated_at="2026-10-29T14:00:00Z",
            snapshot_fetched_at="2026-10-29T13:59:00Z",
        )

        self.assertEqual(len(result.points), 1)
        terminal = result.points[0]
        self.assertEqual(terminal.date, date(2026, 12, 9))
        self.assertEqual(terminal.kind, "terminal_anchor")
        self.assertTrue(math.isclose(terminal.implied_target_upper, result.terminal.expected_target_upper, abs_tol=1e-12))
        self.assertTrue(math.isclose(terminal.implied_change_bp, 100 * (result.terminal.expected_target_upper - 3.5), abs_tol=1e-12))
        self.assertTrue(math.isclose(terminal.implied_effective_rate, result.terminal.effective_rate_proxy, abs_tol=1e-12))

    def test_boundary_diagnostic_and_terminal_tail_representatives(self) -> None:
        meetings = tuple(self.distribution(index, (.2, .2, .2, .2, .2)) for index in range(3))
        terminal = {bucket.label: 0.0 for bucket in self.config.terminal_buckets}
        terminal["≤1.0%"] = .5
        terminal["≥4.5%"] = .5
        result = compute_fed_path(self.config, meetings, terminal, generated_at="g", snapshot_fetched_at="s")
        self.assertEqual(result.terminal.expected_target_upper, 2.75)
        codes = [diagnostic.code for diagnostic in result.diagnostics]
        self.assertNotIn("meeting_prices_renormalized", codes)
        self.assertIn("cross_market_path_inconsistency", codes)
        self.assertEqual(result.terminal.lower_tail_probability, .5)
        self.assertEqual(result.terminal.upper_tail_probability, .5)

    def test_meeting_renormalization_uses_the_strict_one_nanoboundary(self) -> None:
        baseline = tuple(self.distribution(index, (.2, .2, .2, .2, .2)) for index in range(3))
        at_boundary = self.distribution(0, (.2, .2, .2, .2, .2 + 1e-9))
        above_boundary = self.distribution(0, (.2, .2, .2, .2, .2 + 2e-9))
        self.assertEqual(str(at_boundary.raw_total), "1.000000001")
        boundary_codes = {
            item.code for item in compute_fed_path(
                self.config, (at_boundary, baseline[1], baseline[2]), self.terminal_prices(),
                generated_at="g", snapshot_fetched_at="s",
            ).diagnostics
        }
        above_codes = {
            item.code for item in compute_fed_path(
                self.config, (above_boundary, baseline[1], baseline[2]), self.terminal_prices(),
                generated_at="g", snapshot_fetched_at="s",
            ).diagnostics
        }
        self.assertNotIn("meeting_prices_renormalized", boundary_codes)
        self.assertIn("meeting_prices_renormalized", above_codes)

    def test_rejects_constructed_malformed_meeting_distributions(self) -> None:
        valid = tuple(self.distribution(index, (.2, .2, .2, .2, .2)) for index in range(3))
        first = valid[0]
        malformed = (
            replace(first, config=replace(first.config, event_slug="not-the-approved-event")),
            replace(first, prices=first.prices[:-1]),
            replace(first, prices=(replace(first.prices[0], raw_probability=float("nan")), *first.prices[1:])),
            replace(first, prices=(replace(first.prices[0], probability=1.1), *first.prices[1:])),
            replace(first, raw_total=.8),
            replace(first, expected_change_bp=1.0),
            replace(first, decrease_probability=.2, no_change_probability=.2, increase_probability=.2),
            replace(first, negative_tail_probability=.1),
        )
        for item in malformed:
            with self.subTest(item=item), self.assertRaisesRegex(FedPathError, "meeting distribution"):
                compute_fed_path(self.config, (item, valid[1], valid[2]), self.terminal_prices(), generated_at="g", snapshot_fetched_at="s")

    def test_wirp_comparison_serialization_is_detached_and_finite(self) -> None:
        meetings = tuple(self.distribution(index, (.2, .2, .2, .2, .2)) for index in range(3))
        result = compute_fed_path(self.config, meetings, self.terminal_prices(), generated_at="g", snapshot_fetched_at="s", diagnostics=(Diagnostic("warning", None, "upstream", "message"),))
        payload = result.to_dict()
        self.assertEqual(payload["points"][0]["wirp_incremental_moves"], .337)
        self.assertEqual(payload["points"][-1]["wirp_incremental_moves"], .385)
        payload["points"][0]["date"] = "changed"
        self.assertEqual(result.points[0].date, date(2026, 7, 29))
        for point in result.points:
            for value in (point.implied_change_bp, point.cumulative_change_bp, point.implied_target_upper, point.implied_effective_rate):
                self.assertTrue(math.isfinite(value))
        self.assertTrue(math.isclose(sum(result.terminal.probabilities.values()), 1.0, abs_tol=1e-12))

    def test_result_exposes_baselines_and_explicit_wirp_differences(self) -> None:
        meetings = tuple(self.distribution(index, (.2, .2, .2, .2, .2)) for index in range(3))
        result = compute_fed_path(self.config, meetings, self.terminal_prices(), generated_at="g", snapshot_fetched_at="s")
        payload = result.to_dict()
        self.assertEqual(payload["target_upper_bound_baseline"], 3.75)
        self.assertEqual(payload["effective_rate_baseline"], 3.628)
        self.assertEqual(payload["baseline_spread"], .122)
        self.assertEqual(payload["standard_move_bp"], 25.0)
        self.assertEqual(len(payload["wirp_rows"]), 11)
        self.assertEqual(payload["wirp_rows"][0]["date"], "2026-07-29")
        self.assertEqual(payload["wirp_rows"][-1]["date"], "2027-10-27")
        july = result.points[0]
        self.assertTrue(math.isclose(july.polymarket_minus_wirp_incremental_moves, july.incremental_moves - .337, abs_tol=1e-12))
        self.assertTrue(math.isclose(july.polymarket_minus_wirp_implied_rate, july.implied_effective_rate - 3.713, abs_tol=1e-12))
        self.assertTrue(math.isclose(july.polymarket_minus_wirp_implied_rate_bp, 100 * (july.implied_effective_rate - 3.713), abs_tol=1e-12))
        self.assertEqual(payload["points"][0]["polymarket_minus_wirp_implied_rate_bp"], july.polymarket_minus_wirp_implied_rate_bp)

    def test_terminal_probabilities_are_deeply_immutable_and_serialization_is_detached(self) -> None:
        meetings = tuple(self.distribution(index, (.2, .2, .2, .2, .2)) for index in range(3))
        result = compute_fed_path(self.config, meetings, self.terminal_prices(), generated_at="g", snapshot_fetched_at="s")
        with self.assertRaises(TypeError):
            result.terminal.probabilities["3.75%"] = .0
        payload = result.to_dict()
        payload["terminal"]["probabilities"]["3.75%"] = .0
        self.assertNotEqual(result.terminal.probabilities["3.75%"], .0)


if __name__ == "__main__":
    unittest.main()
