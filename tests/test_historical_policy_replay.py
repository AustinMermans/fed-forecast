from __future__ import annotations

import math
import unittest
from datetime import date

from fed_forecast.historical_policy_replay import (
    action_from_label,
    baseline_for_day,
    display_probabilities,
    forward_fan,
)


class HistoricalPolicyReplayTests(unittest.TestCase):
    def test_action_from_label_retains_native_tail(self) -> None:
        cases = [
            ("75+ bps decrease", -75.0),
            ("50 bps decrease", -50.0),
            ("25 bps decrease", -25.0),
            ("No Change", 0.0),
            ("25+ bps increase", 25.0),
            ("50+ bps increase", 50.0),
            ("Other", None),
        ]
        for label, expected in cases:
            with self.subTest(label=label):
                self.assertEqual(action_from_label(label), expected)

    def test_display_probabilities_aggregates_native_downside_tail(self) -> None:
        values = display_probabilities(
            [
                {"representative_bp": -75, "probability": 0.1},
                {"representative_bp": -50, "probability": 0.2},
                {"representative_bp": -25, "probability": 0.3},
                {"representative_bp": 0, "probability": 0.4},
            ]
        )
        self.assertAlmostEqual(values["50+ bps decrease"], 0.3)
        self.assertAlmostEqual(values["25 bps decrease"], 0.3)
        self.assertAlmostEqual(values["No change"], 0.4)

    def test_forward_fan_preserves_meeting_means_and_quantile_order(self) -> None:
        meetings = [
            {
                "date": "2025-09-17",
                "native_outcomes": [
                    {"representative_bp": -50, "probability": 0.10},
                    {"representative_bp": -25, "probability": 0.40},
                    {"representative_bp": 0, "probability": 0.45},
                    {"representative_bp": 25, "probability": 0.05},
                ],
            },
            {
                "date": "2025-10-29",
                "native_outcomes": [
                    {"representative_bp": -50, "probability": 0.05},
                    {"representative_bp": -25, "probability": 0.25},
                    {"representative_bp": 0, "probability": 0.60},
                    {"representative_bp": 25, "probability": 0.10},
                ],
            },
        ]
        fan = forward_fan(4.5, meetings, vintage_date="2025-09-01")
        self.assertAlmostEqual(fan[1]["mean"], 4.5 + (-50 * .1 - 25 * .4 + 25 * .05) / 100)
        self.assertAlmostEqual(fan[2]["mean"], fan[1]["mean"] + (-50 * .05 - 25 * .25 + 25 * .1) / 100)
        for point in fan:
            values = [point[key] for key in ("q05", "q25", "q50", "q75", "q95")]
            self.assertEqual(values, sorted(values))
            self.assertTrue(all(math.isfinite(value) for value in values + [point["mean"]]))

    def test_baseline_for_day_steps_only_after_official_decision(self) -> None:
        decisions = [
            {"date": "2024-09-18", "before": [5.25, 5.50], "after": [4.75, 5.00]},
            {"date": "2024-11-07", "before": [4.75, 5.00], "after": [4.50, 4.75]},
        ]
        self.assertEqual(baseline_for_day(date(2024, 9, 17), decisions), 5.50)
        self.assertEqual(baseline_for_day(date(2024, 9, 18), decisions), 5.00)
        self.assertEqual(baseline_for_day(date(2024, 10, 1), decisions), 5.00)

    def test_forward_fan_keeps_quoted_terminal_outside_meeting_path(self) -> None:
        meetings = [
            {"date": "2026-12-09", "native_outcomes": [
                {"representative_bp": -25, "probability": 0.5},
                {"representative_bp": 0, "probability": 0.5},
            ]},
            {"date": "2027-01-27", "native_outcomes": [
                {"representative_bp": 0, "probability": 0.75},
                {"representative_bp": 25, "probability": 0.25},
            ]},
        ]
        terminal = {"date": "2026-12-09", "native_outcomes": [
            {"representative_rate": 3.75, "probability": 0.4},
            {"representative_rate": 4.00, "probability": 0.6},
        ]}
        fan = forward_fan(3.75, meetings, vintage_date="2026-08-01", terminal=terminal)
        december_point = next(item for item in fan if item["date"] == "2026-12-09")
        january_point = next(item for item in fan if item["date"] == "2027-01-27")
        self.assertEqual(december_point["kind"], "meeting")
        self.assertAlmostEqual(december_point["mean"], 3.625)
        self.assertAlmostEqual(january_point["mean"], 3.6875)


if __name__ == "__main__":
    unittest.main()
