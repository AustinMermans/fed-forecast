from __future__ import annotations

import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from fed_forecast.historical_transitions_cli import historical_transitions_main
from fed_forecast.historical_transitions_client import HistoricalTransitionsClient, load_historical_config
from tests.test_historical_transitions_client import HistoryTransport

ROOT = Path(__file__).resolve().parents[1]


class HistoricalTransitionsCliTests(unittest.TestCase):
    def test_help(self) -> None:
        output = io.StringIO()
        self.assertEqual(historical_transitions_main(["--help"], stdout=output), 0)
        self.assertIn("inactive resolved-FOMC", output.getvalue())

    def test_evidence_option_matrix_fails_closed(self) -> None:
        combinations = (
            ["--evidence-output", "x.json"],
            ["--verify-run", "run"],
            ["--verify-run", "run", "--evidence-output", "x.json", "--snapshot-input", "snapshot.json"],
            ["--snapshot-input", "snapshot.json", "--evidence-output", "x.json"],
        )
        for arguments in combinations:
            with self.subTest(arguments=arguments):
                self.assertEqual(historical_transitions_main(arguments, stderr=io.StringIO()), 2)

    def test_bad_config_is_clean_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text("{}")
            error = io.StringIO()
            self.assertEqual(historical_transitions_main(["--config", str(path)], stderr=error), 2)
            self.assertIn("configuration", error.getvalue())

    def test_offline_replay_publishes_robustness_and_updates_pointer_only_on_success(self) -> None:
        config_path = ROOT / "config/historical_transitions.json"
        config = load_historical_config(config_path)
        snapshot = HistoricalTransitionsClient(
            HistoryTransport(),
            now=lambda: datetime(2026, 8, 28, 20, 0, tzinfo=timezone.utc),
        ).fetch_snapshot(config)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot_path = root / "snapshot.json"
            snapshot_path.write_text(json.dumps(snapshot.to_dict(), allow_nan=False))
            output = root / "outputs"
            stdout = io.StringIO()
            status = historical_transitions_main([
                "--config", str(config_path), "--snapshot-input", str(snapshot_path), "--output-dir", str(output),
            ], stdout=stdout, stderr=io.StringIO())
            self.assertEqual(status, 0, stdout.getvalue())
            pointer_path = output / "latest.json"
            pointer_before = pointer_path.read_bytes()
            run = output / json.loads(pointer_before)["run_path"]
            model = json.loads((run / "model.json").read_text())
            robustness = model["robustness_diagnostics"]
            for key in ("decision_window_1415", "strict_raw_total_bounds", "exclude_child_action_fallbacks", "cohorts", "official_realized_action_bp_counts"):
                self.assertIn(key, robustness)
            report = (run / "report.md").read_text()
            for phrase in ("14:15 decision-window fit", "Strict-total fit", "Child-fallback-exclusion fit", "Official realized move strata"):
                self.assertIn(phrase, report)
            snapshot_path.write_text('{"schema_version":1,"broken":true}')
            self.assertEqual(historical_transitions_main([
                "--config", str(config_path), "--snapshot-input", str(snapshot_path), "--output-dir", str(output),
            ], stderr=io.StringIO()), 2)
            self.assertEqual(pointer_path.read_bytes(), pointer_before)


if __name__ == "__main__":
    unittest.main()
