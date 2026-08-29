from __future__ import annotations

import json
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

    def test_public_files_do_not_leak_local_paths(self) -> None:
        for path in (ROOT / "site").rglob("*"):
            if path.is_file() and path.suffix in {".html", ".js", ".json"}:
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("/Users/", text, path)
                self.assertNotIn("/home/runner/", text, path)
                self.assertNotIn("curve_forecaster", text, path)


if __name__ == "__main__":
    unittest.main()
