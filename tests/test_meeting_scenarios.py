from __future__ import annotations

import math
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path

from fed_forecast.fed_path import compute_meeting_distribution
from fed_forecast.fed_path_config import load_fed_path_config
from fed_forecast.fed_path_models import MeetingConfig, MeetingPrice, OutcomeConfig
from fed_forecast.meeting_scenarios import compute_meeting_scenarios
from fed_forecast.meeting_scenarios_svg import render_meeting_scenarios_svg
from fed_forecast.conditional_tree_svg import render_conditional_tree_svg
from fed_forecast.conditional_rate_fan_svg import render_conditional_rate_fan_svg


ROOT = Path(__file__).resolve().parents[1]
OUTCOMES = (
    OutcomeConfig("50+ bps decrease", -50.0),
    OutcomeConfig("25 bps decrease", -25.0),
    OutcomeConfig("No change", 0.0),
    OutcomeConfig("25 bps increase", 25.0),
    OutcomeConfig("50+ bps increase", 50.0),
)


class MeetingScenarioTests(unittest.TestCase):
    def setUp(self) -> None:
        base = load_fed_path_config(ROOT / "config/fed_path.json")
        meetings = (
            MeetingConfig(date(2026, 9, 16), "september", OUTCOMES),
            MeetingConfig(date(2026, 10, 28), "october", OUTCOMES),
            MeetingConfig(date(2026, 12, 9), "december", OUTCOMES),
            MeetingConfig(date(2027, 1, 27), "january", OUTCOMES),
        )
        self.config = replace(base, meetings=meetings)
        values = (
            (.05, .15, .50, .25, .05),
            (.02, .08, .62, .24, .04),
            (.01, .04, .43, .49, .03),
            (.05, .15, .60, .17, .03),
        )
        self.distributions = tuple(
            compute_meeting_distribution(
                meeting,
                tuple(MeetingPrice(meeting.date, outcome.label, probability) for outcome, probability in zip(OUTCOMES, probabilities, strict=True)),
            )
            for meeting, probabilities in zip(meetings, values, strict=True)
        )
        self.terminal = {bucket.label: 0.0 for bucket in base.terminal_buckets}
        self.terminal["3.75%"] = .6
        self.terminal["4.0%"] = .4

    def result(self):
        return compute_meeting_scenarios(
            self.config, self.distributions, self.terminal,
            terminal_date=date(2026, 12, 9),
            generated_at="2026-08-28T12:00:00Z",
            snapshot_fetched_at="2026-08-28T11:59:00Z",
        )

    def test_decomposition_and_next_meeting_shock_are_exact(self) -> None:
        result = self.result()
        self.assertEqual(len(result["meetings"]), 4)
        self.assertEqual(len(result["scenarios"]), 12)
        september = result["meetings"][0]
        self.assertAlmostEqual(
            september["decrease_probability"] + september["no_change_probability"] + september["increase_probability"],
            1.0,
        )
        up = next(item for item in result["scenarios"] if item["shock_meeting_date"] == "2026-09-16" and item["category"] == "up")
        expected_conditional_up = (.25 * 25 + .05 * 50) / .30
        self.assertAlmostEqual(up["scenario_change_bp"], expected_conditional_up)
        self.assertAlmostEqual(
            up["next_meeting_mechanical_target_upper"] - up["next_meeting_baseline_target_upper"],
            up["surprise_vs_baseline_bp"] / 100,
        )
        self.assertAlmostEqual(up["next_meeting_probabilities_unchanged"]["down"], .10)
        self.assertAlmostEqual(up["next_meeting_probabilities_unchanged"]["unchanged"], .62)
        self.assertAlmostEqual(up["next_meeting_probabilities_unchanged"]["up"], .28)

    def test_terminal_anchor_resets_earlier_shocks_but_mechanical_path_does_not(self) -> None:
        result = self.result()
        september_down = next(item for item in result["scenarios"] if item["shock_meeting_date"] == "2026-09-16" and item["category"] == "down")
        december_action = next(item for item in september_down["downstream"] if item["date"] == "2026-12-09" and item["kind"] == "meeting_action")
        terminal_anchor = next(item for item in september_down["downstream"] if item["kind"] == "terminal_anchor")
        january = next(item for item in september_down["downstream"] if item["date"] == "2027-01-27")
        self.assertNotEqual(december_action["anchor_respecting_change_bp"], 0.0)
        self.assertEqual(terminal_anchor["anchor_respecting_change_bp"], 0.0)
        self.assertEqual(january["anchor_respecting_change_bp"], 0.0)
        self.assertAlmostEqual(january["mechanical_change_bp"], september_down["surprise_vs_baseline_bp"])

    def test_conditional_tree_preserves_marginals_and_reprices_future_nodes(self) -> None:
        result = self.result()
        tree = result["conditional_tree"]
        self.assertTrue(tree["quoted_marginals_preserved"])
        self.assertEqual(tree["node_count"], 781)
        self.assertEqual(tree["leaf_count"], 625)
        self.assertLessEqual(tree["raking"]["max_marginal_error"], 1e-10)
        self.assertAlmostEqual(sum(item["path_probability"] for item in tree["leaf_paths"]), 1.0)

        root = next(item for item in tree["nodes"] if item["node_id"] == "root")
        september = result["meetings"][0]
        expected_categories = ("down_50plus", "down_25", "unchanged", "up_25", "up_50plus")
        for category, price in zip(expected_categories, september["prices"], strict=True):
            self.assertAlmostEqual(root["next_probabilities"][category], price["probability"])
        self.assertEqual(
            [branch["representative_action_bp"] for branch in root["branches"]],
            [-50.0, -25.0, 0.0, 25.0, 50.0],
        )

        september_to_october = tree["adjacent_conditional_tables"][0]
        conditional_up = [row["next_probabilities"]["up_25"] for row in september_to_october["rows"]]
        self.assertGreater(max(conditional_up) - min(conditional_up), 0.01)
        recovered_october_up = sum(
            row["realized_probability"] * row["next_probabilities"]["up_25"]
            for row in september_to_october["rows"]
        )
        self.assertAlmostEqual(recovered_october_up, result["meetings"][1]["prices"][3]["probability"])

        september_up_node = next(item for item in tree["nodes"] if item["node_id"] == "up_25")
        september_hold_node = next(item for item in tree["nodes"] if item["node_id"] == "unchanged")
        self.assertNotEqual(september_up_node["next_probabilities"], september_hold_node["next_probabilities"])
        self.assertNotAlmostEqual(
            september_up_node["conditional_terminal_expected_upper"],
            september_hold_node["conditional_terminal_expected_upper"],
        )

        december_hold = next(item for item in tree["nodes"] if item["node_id"] == "up_25_unchanged_unchanged")
        january_hold = next(item for item in tree["nodes"] if item["node_id"] == "up_25_unchanged_unchanged_unchanged")
        self.assertAlmostEqual(december_hold["representative_target_upper"], december_hold["conditional_terminal_expected_upper"])
        self.assertAlmostEqual(january_hold["representative_target_upper"], december_hold["representative_target_upper"])

        december_triple_hike = next(item for item in tree["nodes"] if item["node_id"] == "up_50plus_up_50plus_up_50plus")
        january_fourth_hike = next(item for item in tree["nodes"] if item["node_id"] == "up_50plus_up_50plus_up_50plus_up_50plus")
        self.assertAlmostEqual(december_triple_hike["action_implied_target_upper"], 5.25)
        self.assertLess(
            december_triple_hike["representative_target_upper"],
            december_triple_hike["action_implied_target_upper"],
        )
        self.assertAlmostEqual(
            january_fourth_hike["action_implied_target_upper"],
            january_fourth_hike["representative_target_upper"],
        )

    def test_payload_is_finite_and_svg_has_all_bars_and_next_meeting_scenarios(self) -> None:
        result = self.result()

        def assert_finite(value):
            if isinstance(value, float):
                self.assertTrue(math.isfinite(value))
            elif isinstance(value, dict):
                for item in value.values():
                    assert_finite(item)
            elif isinstance(value, list):
                for item in value:
                    assert_finite(item)

        assert_finite(result)
        svg = render_meeting_scenarios_svg(result).decode("utf-8")
        self.assertEqual(svg.count('data-bar="'), 12)
        self.assertEqual(svg.count('data-scenario="'), 9)
        self.assertIn("not conditional repricing forecasts", svg)
        tree_svg = render_conditional_tree_svg(result).decode("utf-8")
        self.assertEqual(tree_svg.count('data-conditional-bar="'), 75)
        fan_svg = render_conditional_rate_fan_svg(result).decode("utf-8")
        self.assertEqual(fan_svg.count('data-fan-band="'), 3)
        self.assertEqual(fan_svg.count('data-rate-state="'), 781)
        self.assertIn("Conditional policy-rate fan", fan_svg)
        self.assertIn("quoted marginals preserved", tree_svg)
        self.assertIn("Transitions are modeled, not traded", tree_svg)


if __name__ == "__main__":
    unittest.main()
