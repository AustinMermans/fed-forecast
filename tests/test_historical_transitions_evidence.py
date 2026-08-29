from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from fed_forecast.historical_transitions_evidence import _sample_selection, _surprise_support, build_evidence_summary, summarize_surface_synchronization, validate_evidence_summary

ROOT = Path(__file__).resolve().parents[1]
CANONICAL_RUN = Path("/Users/austin/Babel/30_Projects/polymarket-curve-forecaster/outputs/historical-transitions/runs/20260829T001836.831584Z-3a7eda0c")


class HistoricalEvidenceTests(unittest.TestCase):
    def test_exclusion_reasons_are_allowlisted_without_token_identifiers(self) -> None:
        result = _sample_selection([], [{"reason": "no synchronized quote for token 123456"}])
        self.assertEqual(result["exclusions_by_primary_reason"], {"missing_synchronized_quote": 1})
    def test_synchronization_aggregates_are_compact_and_tight_counted(self) -> None:
        action = '{"cutoff_timestamp":100}'
        profile = '{"15:30":{"cutoff_timestamp":200}}'
        topology = [{
            "record_type": "surface_diagnostic", "current_meeting_date": "a", "next_meeting_date": "b",
            "current_pre_action_buckets": action, "next_pre_action_buckets": action,
            "event_time_profile": profile, "current_pre_timestamps": "[99,98]",
            "next_pre_timestamps": "[99,98]", "next_post_timestamps": "[199,198]",
        }]
        result = summarize_surface_synchronization(topology, [{"current_meeting_date": "a", "next_meeting_date": "b"}])
        aggregate = result["edge_synchronization_aggregates"]
        self.assertEqual(aggregate["tight_60s_eligible_edge_count"], 1)
        self.assertEqual(aggregate["coordinate_timestamp_dispersion_seconds"]["max"], 1)
        self.assertEqual(aggregate["maximum_coordinate_quote_age_seconds"]["max"], 2)

    def test_smoothed_dhu_support_uses_the_registered_floor(self) -> None:
        rows = [
            {"current_pre": "[0.0,0.994,0.006]", "current_candidate_actions_bp": "[-25,0,25]", "realized_action_bp": "0"},
            {"current_pre": "[0.006,0.994,0.0]", "current_candidate_actions_bp": "[-25,0,25]", "realized_action_bp": "0"},
        ]
        result = _surprise_support(rows)
        self.assertAlmostEqual(result["observed_min_25bp_units"], -0.000995024876, places=12)
        self.assertAlmostEqual(result["observed_max_25bp_units"], 0.000995024876, places=12)

    def test_stage1a_contract_rejects_activation_extras_and_xss(self) -> None:
        base = json.loads((ROOT / "site/data/evidence-summary.json").read_text())
        validate_evidence_summary(base)
        base = copy.deepcopy(base)
        validate_evidence_summary(base)
        base["exploratory_support_assessment"]["design_artifact"] = "config/fake.json"
        with self.assertRaisesRegex(ValueError, "(type|value)"):
            validate_evidence_summary(base)
        injected = json.loads((ROOT / "site/data/evidence-summary.json").read_text())
        injected["legacy_canonical_15_30"]["xss"] = '<img src=x onerror=alert(1)>'
        with self.assertRaisesRegex(ValueError, "keys"):
            validate_evidence_summary(injected)

    @unittest.skipUnless(CANONICAL_RUN.is_dir(), "canonical local smoke input is not present")
    def test_canonical_local_smoke_matches_pinned_facts(self) -> None:
        result = build_evidence_summary(CANONICAL_RUN, generated_at="2026-08-29T06:12:50Z")
        support = result["legacy_canonical_15_30"]["dhu_smoothed_surprise_support"]
        self.assertEqual(support["observed_min_25bp_units"], -0.030363364858)
        self.assertEqual(support["observed_max_25bp_units"], 0.028250857496)


if __name__ == "__main__":
    unittest.main()
