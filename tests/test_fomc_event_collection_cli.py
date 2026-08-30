from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from fed_forecast.fomc_event_collection_cli import fomc_event_collection_main


ROOT = Path(__file__).resolve().parents[1]


class EventCollectionCliTests(unittest.TestCase):
    def test_gate_reports_reviewed_slot_and_writes_github_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "github-output"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                status = fomc_event_collection_main([
                    "gate", "--calendar", str(ROOT / "config/fomc_event_collection.json"),
                    "--markets", str(ROOT / "config/markets.json"),
                    "--run-created-at", "2026-09-16T18:05:00Z",
                    "--actual-start-at", "2026-09-16T18:05:30Z",
                    "--github-output", str(output),
                ])
            self.assertEqual(status, 0)
            self.assertIn('"eligible":true', stdout.getvalue())
            self.assertIn("event_id=fomc-2026-09-16", output.read_text())

    def test_invalid_code_sha_fails_before_network_collection(self) -> None:
        stderr = io.StringIO()
        with patch("fed_forecast.fomc_event_collection_cli.FomcEventObserver") as observer, redirect_stderr(stderr):
            status = fomc_event_collection_main([
                "collect", "--calendar", str(ROOT / "config/fomc_event_collection.json"),
                "--markets", str(ROOT / "config/markets.json"), "--archive", "/tmp/unused",
                "--run-created-at", "2026-09-16T18:05:00Z", "--actual-start-at", "2026-09-16T18:05:30Z",
                "--repository", "owner/repo", "--workflow", "workflow.yml", "--github-run-id", "1",
                "--github-run-attempt", "1", "--github-run-number", "1", "--head-sha", "invalid",
                "--ref", "refs/heads/main", "--event-name", "schedule", "--cron", "*/5",
            ])
        self.assertEqual(status, 2)
        self.assertIn("code commit SHA is invalid", stderr.getvalue())
        observer.assert_not_called()

