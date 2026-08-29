from __future__ import annotations

import importlib.util
import math
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_github_pages.py"
SPEC = importlib.util.spec_from_file_location("build_github_pages", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ForecastReplayTests(unittest.TestCase):
    def test_compact_tree_keeps_action_and_terminal_anchor_rates_separate(self) -> None:
        compact = MODULE._compact_tree({
            "conditional_tree": {
                "root_node_id": "root", "node_count": 1, "leaf_count": 1,
                "quoted_marginals_preserved": True, "settings": {},
                "nodes": [{
                    "node_id": "root", "depth": 0, "realized_path": [], "path_probability": 1.0,
                    "representative_target_upper": 4.0, "action_implied_target_upper": 5.25,
                    "rate_distribution": [{"rate": 3.75, "probability": 0.4}, {"rate": 4.25, "probability": 0.6}],
                    "next_meeting_date": None, "next_probabilities": None, "branches": [],
                }],
            },
        })
        self.assertEqual(compact["nodes"][0]["rate"], 4.0)
        self.assertEqual(compact["nodes"][0]["action_rate"], 5.25)
        self.assertEqual(compact["nodes"][0]["rate_distribution"][1]["rate"], 4.25)

    def test_legacy_tree_reconstructs_terminal_action_before_anchor_reset(self) -> None:
        policy = {
            "target_upper_bound_baseline": 3.75,
            "terminal_anchor": {"date": "2026-12-09"},
            "meetings": [{"date": "2026-09-16"}, {"date": "2026-10-28"}, {"date": "2026-12-09"}],
            "tree": {
                "root": "root",
                "nodes": [
                    {"id": "root", "depth": 0, "path": [], "rate": 3.75, "branches": [{"category": "up_50plus", "representative_action_bp": 50.0, "child_node_id": "a"}]},
                    {"id": "a", "depth": 1, "path": ["up_50plus"], "rate": 4.25, "branches": [{"category": "up_50plus", "representative_action_bp": 50.0, "child_node_id": "b"}]},
                    {"id": "b", "depth": 2, "path": ["up_50plus", "up_50plus"], "rate": 4.75, "branches": [{"category": "up_50plus", "representative_action_bp": 50.0, "child_node_id": "c"}]},
                    {"id": "c", "depth": 3, "path": ["up_50plus", "up_50plus", "up_50plus"], "rate": 4.02, "branches": []},
                ],
            },
        }
        MODULE._ensure_action_rates(policy)
        self.assertEqual(policy["tree"]["nodes"][-1]["action_rate"], 5.25)
        self.assertEqual(policy["tree"]["nodes"][-1]["rate"], 4.02)

    def test_forward_chart_keeps_event_rules_off_the_plot(self) -> None:
        dashboard = (SCRIPT.parents[1] / "site" / "assets" / "dashboard.js").read_text(encoding="utf-8")
        self.assertNotIn('class: "event-line"', dashboard)
        self.assertNotIn('class: "event-label"', dashboard)
        self.assertNotIn('class: "forecast-mean"', dashboard)

    def test_forward_chart_marks_fomc_meetings_without_event_rules(self) -> None:
        dashboard = (SCRIPT.parents[1] / "site" / "assets" / "dashboard.js").read_text(encoding="utf-8")
        self.assertIn('class: "meeting-rule"', dashboard)
        self.assertIn('label.textContent = "FOMC";', dashboard)
        self.assertIn("state.replay?.meeting_calendar", dashboard)

    def test_forward_chart_uses_quarter_point_scrolling_grid(self) -> None:
        dashboard = (SCRIPT.parents[1] / "site" / "assets" / "dashboard.js").read_text(encoding="utf-8")
        self.assertIn("const AXIS_TICK_PP = .25;", dashboard)
        self.assertIn("const AXIS_VIEW_SPAN_PP = 2.25;", dashboard)
        self.assertIn("value += AXIS_TICK_PP", dashboard)
        self.assertIn("prepareAxisCenters(state.index);", dashboard)

    def test_replay_axis_uses_a_speed_aware_continuous_camera(self) -> None:
        dashboard = (SCRIPT.parents[1] / "site" / "assets" / "dashboard.js").read_text(encoding="utf-8")
        self.assertIn("const AXIS_CAMERA_MAX_PP_PER_SECOND = .26;", dashboard)
        self.assertIn("Math.exp(-deltaMs / axisCameraTimeConstant())", dashboard)
        self.assertIn("Math.max(50, 1000 / state.playbackSpeed)", dashboard)
        self.assertIn("selectPosition(target, playbackTransition);", dashboard)
        self.assertNotIn("Math.max(12, 180 / state.playbackSpeed)", dashboard)

    def test_replay_uses_fixed_six_month_window(self) -> None:
        self.assertEqual(MODULE.REPLAY_WINDOW_DAYS, 183)

    def test_event_activity_uses_clob_fields_and_rejects_invalid_values(self) -> None:
        activity = MODULE._event_activity({
            "events": {
                "meeting": {
                    "volume24hr": 80.0,
                    "volume24hrClob": 100.0,
                    "volume": 900.0,
                    "volumeClob": 1000.0,
                    "liquidity": 450.0,
                    "liquidityClob": 500.0,
                },
                "invalid": {"volume24hr": "nan", "liquidity": -1},
            },
        })
        self.assertEqual(activity["meeting"], {"volume_24h": 100.0, "volume_total": 1000.0, "liquidity": 500.0})
        self.assertNotIn("invalid", activity)

    def test_replay_preference_favors_richer_surface_and_full_tree(self) -> None:
        historical = {"kind": "historical_daily", "generated_at": "2026-08-07T00:00:00Z", "meetings": [{}, {}, {}, {}]}
        thin_live = {"kind": "daily", "generated_at": "2026-08-07T13:30:00Z", "meetings": [{}, {}]}
        full_tree = {"kind": "full_tree", "generated_at": "2026-08-07T12:00:00Z", "meetings": [{}, {}, {}, {}]}
        self.assertGreater(MODULE._replay_preference(historical), MODULE._replay_preference(thin_live))
        self.assertGreater(MODULE._replay_preference(full_tree), MODULE._replay_preference(historical))

    def test_event_checkpoint_carries_missing_horizon_meeting_without_overwriting_live_rows(self) -> None:
        outcomes = [
            {"label": label, "probability": probability, "raw_probability": probability, "representative_bp": move}
            for label, probability, move in zip(
                ("50+ bps decrease", "25 bps decrease", "No change", "25 bps increase", "50+ bps increase"),
                (0.05, 0.15, 0.50, 0.25, 0.05),
                MODULE.ACTION_BUCKETS_BP,
                strict=True,
            )
        ]
        event_meeting = {
            "date": "2026-09-16", "event_slug": "september", "expected_change_bp": 25.0,
            "prices": outcomes,
        }
        historical = {
            "generated_at": "2026-08-28T00:00:00Z",
            "meetings": [
                {"date": "2026-09-16"},
                {"date": "2027-01-27", "event_slug": "january", "event_url": "https://example.test/january", "native_outcomes": outcomes, "source_timestamp": 123},
            ],
        }
        completed = MODULE._complete_event_meetings([event_meeting], historical, baseline=4.25)
        self.assertEqual([item["date"] for item in completed], ["2026-09-16", "2027-01-27"])
        self.assertEqual(completed[0]["event_slug"], event_meeting["event_slug"])
        self.assertNotIn("quote_status", completed[0])
        self.assertEqual(completed[1]["quote_status"], "carried_forward")
        self.assertEqual(completed[1]["carried_forward_from"], historical["generated_at"])
        self.assertTrue(math.isclose(sum(item["probability"] for item in completed[1]["prices"]), 1.0))
        self.assertTrue(math.isclose(completed[0]["expected_target_upper_after"], 4.50))
        self.assertTrue(math.isclose(completed[1]["expected_target_upper_before"], 4.50))

        compact = MODULE._compact_replay_meetings(completed)
        self.assertEqual(compact[1]["quote_status"], "carried_forward")
        self.assertEqual(compact[1]["source_timestamp"], 123)

    def test_five_bucket_path_preserves_meeting_mean_without_terminal_reset(self) -> None:
        probabilities = (0.01, 0.04, 0.80, 0.14, 0.01)
        labels = ("50+ bps decrease", "25 bps decrease", "No change", "25 bps increase", "50+ bps increase")
        meeting = {
            "date": "2026-09-16",
            "prices": [
                {"label": label, "probability": probability}
                for label, probability in zip(labels, probabilities, strict=True)
            ],
        }
        points = MODULE._five_bucket_path(
            vintage_at="2026-08-28T13:30:00Z",
            baseline=3.75,
            meetings=[meeting],
            settings={
                "dependence_strength": 0.35,
                "dependence_decay": 0.70,
                "rake_tolerance": 1e-12,
                "rake_max_iterations": 2000,
            },
        )
        self.assertEqual([point["kind"] for point in points], ["vintage", "meeting"])
        self.assertTrue(math.isclose(points[1]["mean"], 3.775, abs_tol=1e-10))
        for point in points:
            self.assertLessEqual(point["q05"], point["q25"])
            self.assertLessEqual(point["q25"], point["q50"])
            self.assertLessEqual(point["q50"], point["q75"])
            self.assertLessEqual(point["q75"], point["q95"])


if __name__ == "__main__":
    unittest.main()
