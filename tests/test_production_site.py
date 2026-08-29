from __future__ import annotations

import json
import hashlib
import math
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ProductionSiteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dashboard = json.loads((ROOT / "site/data/dashboard.json").read_text(encoding="utf-8"))
        self.replay = json.loads((ROOT / "site/data/forecast-replay.json").read_text(encoding="utf-8"))

    def test_public_page_is_rate_only(self) -> None:
        html = (ROOT / "site/index.html").read_text(encoding="utf-8")
        self.assertNotIn('id="curve"', html)
        self.assertNotIn('id="labor"', html)
        self.assertIn("CONDITIONAL PATH EXPLORER", html)

    def test_public_methodology_separates_quotes_model_and_evidence(self) -> None:
        html = (ROOT / "site/methodology.html").read_text(encoding="utf-8")
        for phrase in ("Five binary contracts", "Marginals do not identify a path", "exact enumeration", "not learned from historical", "risk-free rate", "SMALL-SAMPLE EVIDENCE", "Unit of observation"):
            self.assertIn(phrase, html)
        self.assertNotIn("PRODUCT_SPEC", html)
        self.assertNotIn("IMPLEMENTATION_PLAN", html)

    def test_stage1a_evidence_is_compact_inactive_and_pending(self) -> None:
        path = ROOT / "site" / "data" / "evidence-summary.json"
        evidence = json.loads(path.read_text(encoding="utf-8"))
        self.assertLessEqual(path.stat().st_size, 16 * 1024)
        legacy = evidence["legacy_canonical_15_30"]
        self.assertEqual(legacy["transition_count"], 13)
        self.assertEqual(legacy["row_counts"], {"down": 3, "unchanged": 10, "up": 0})
        self.assertEqual(legacy["walk_forward"]["scored_folds"], 0)
        self.assertEqual(len(legacy["stored_production_gate"]["failures"]), 5)
        self.assertFalse(legacy["active_in_tree"])
        self.assertEqual(evidence["stage1_primary_14_15"]["status"], "new_run_required")
        self.assertIsNone(evidence["exploratory_support_assessment"]["design_artifact"])

    def test_forecast_payload_is_unchanged_except_for_evidence_contract(self) -> None:
        dashboard = dict(self.dashboard)
        dashboard.pop("evidence_summary")
        canonical_dashboard = json.dumps(dashboard, sort_keys=True, separators=(",", ":")).encode()
        canonical_replay = json.dumps(self.replay, sort_keys=True, separators=(",", ":")).encode()
        self.assertEqual(hashlib.sha256(canonical_dashboard).hexdigest(), "c7b92bfd017a7f4bf42cd2787d702e241ae8ab90352d74e17344f49db218aea9")
        self.assertEqual(hashlib.sha256(canonical_replay).hexdigest(), "850c31ec60b37c371fd2f681493cf579e82f5150a2abba1297a88e6a880e5414")

    def test_branch_module_loads_before_dashboard_with_matching_cache_bust(self) -> None:
        html = (ROOT / "site" / "index.html").read_text(encoding="utf-8")
        branch = 'assets/branch-decomposition.js?v=stage1a-2'
        dashboard = 'assets/dashboard.js?v=stage1a-2'
        self.assertLess(html.index(branch), html.index(dashboard))
        javascript = (ROOT / "site" / "assets" / "dashboard.js").read_text(encoding="utf-8")
        for phrase in ("MARKET OBSERVED MARGINAL", "MODEL ASSUMED CONDITIONAL", "HISTORICAL SUPPORT COMPARISON UNAVAILABLE", "loadEvidenceSummary"):
            self.assertIn(phrase, javascript)

    def test_model_config_names_meeting_only_structure(self) -> None:
        model = json.loads((ROOT / "config/model.json").read_text(encoding="utf-8"))
        self.assertEqual(model["model"], "structural_meeting_persistence_ipf")
        self.assertFalse(model["active_historical_fit"])

    def test_every_current_meeting_has_five_normalized_prices_and_quality(self) -> None:
        for meeting in self.dashboard["policy"]["meetings"]:
            self.assertEqual(len(meeting["prices"]), 5)
            self.assertTrue(math.isclose(sum(item["probability"] for item in meeting["prices"]), 1.0, abs_tol=1e-9))
            self.assertIn(meeting["quote_quality"]["source"], {"clob_midpoint", "gamma", "mixed"})
            self.assertIn(meeting["quote_quality"]["quality"], {"good", "degraded"})

    def test_replay_is_chronological_and_keeps_january_at_the_end(self) -> None:
        stamps = [item["generated_at"] for item in self.replay["vintages"]]
        self.assertEqual(stamps, sorted(stamps))
        self.assertEqual(self.replay["window_days"], 183)
        for vintage in self.replay["vintages"][-7:]:
            self.assertIn("2027-01-27", {item["date"] for item in vintage["meetings"]})

    def test_javascript_literal_ids_exist(self) -> None:
        javascript = (ROOT / "site/assets/dashboard.js").read_text(encoding="utf-8")
        html = (ROOT / "site/index.html").read_text(encoding="utf-8")
        for element_id in set(re.findall(r'\$\("([^"]+)"\)', javascript)):
            self.assertIn(f'id="{element_id}"', html)
        methodology = (ROOT / "site/assets/methodology.js").read_text(encoding="utf-8")
        method_html = (ROOT / "site/methodology.html").read_text(encoding="utf-8")
        for element_id in set(re.findall(r'\$\("([^"]+)"\)', methodology)):
            self.assertIn(f'id="{element_id}"', method_html)

    def test_action_path_is_not_bridged_to_year_end_market(self) -> None:
        javascript = (ROOT / "site/assets/dashboard.js").read_text(encoding="utf-8")
        self.assertNotIn('anchorLabel.textContent = "YEAR-END MARKET ANCHOR";', javascript)
        self.assertNotIn("action reaches", javascript)
        self.assertIn("action-implied path level", javascript)

    def test_public_files_do_not_leak_local_paths(self) -> None:
        for path in (ROOT / "site").rglob("*"):
            if path.is_file() and path.suffix in {".html", ".js", ".json"}:
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("/Users/", text, path)
                self.assertNotIn("/home/runner/", text, path)
                self.assertNotIn("curve_forecaster", text, path)


if __name__ == "__main__":
    unittest.main()
