from __future__ import annotations

import json
import hashlib
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fed_forecast.historical_transitions_reporting import (
    canonical_json,
    verify_historical_transition_run,
    write_historical_transition_run,
)


class _Value:
    def __init__(self, payload):
        self.payload = payload

    def to_dict(self):
        return self.payload


def _run(root: Path) -> Path:
    return write_historical_transition_run(
        root,
        _Value({"schema_version": 1, "fetched_at": "2026-08-28T00:00:00Z"}),
        [_Value({"current_meeting_id": "a", "next_meeting_id": "b"})],
        [{"record_type": "surface_diagnostic"}],
        [{"reason": "gap"}],
        _Value({"schema_version": 1, "production_gates": {"status": "diagnostic_only"}}),
        now=datetime(2026, 8, 28, tzinfo=timezone.utc),
        suffix="test",
    )


class HistoricalReportingTests(unittest.TestCase):
    def test_writer_verifies_immutable_run_before_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = _run(root)
            verified = verify_historical_transition_run(run)
            self.assertEqual(set(verified.files), set(verified.manifest["files"]))
            self.assertEqual(json.loads((root / "latest.json").read_text())["run_path"], f"runs/{run.name}")

    def test_extra_file_and_symlink_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _run(Path(directory))
            (run / "extra.txt").write_text("x")
            with self.assertRaisesRegex(ValueError, "allowlist"):
                verify_historical_transition_run(run)
            (run / "extra.txt").unlink()
            (run / "report.md").unlink()
            os.symlink(run / "model.json", run / "report.md")
            with self.assertRaisesRegex(ValueError, "regular file"):
                verify_historical_transition_run(run)

    def test_run_directory_symlink_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = _run(root)
            link = root / "linked-run"
            os.symlink(run, link)
            with self.assertRaisesRegex(ValueError, "directory"):
                verify_historical_transition_run(link)

    def test_failed_verification_preserves_existing_latest_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = _run(root)
            before = (root / "latest.json").read_bytes()
            with patch("fed_forecast.historical_transitions_reporting.verify_historical_transition_run", side_effect=ValueError("forced verification failure")):
                with self.assertRaisesRegex(ValueError, "forced"):
                    write_historical_transition_run(
                        root, _Value({"schema_version": 1}), [], [], [], _Value({"schema_version": 1}),
                        now=datetime(2026, 8, 29, tzinfo=timezone.utc), suffix="failure",
                    )
            self.assertEqual((root / "latest.json").read_bytes(), before)
            self.assertTrue(first.is_dir())

    def test_tamper_duplicate_nonfinite_and_traversal_fail(self) -> None:
        for mutation, message in (
            (lambda run: (run / "model.json").write_text("{}"), "integrity"),
            (lambda run: (run / "manifest.json").write_text('{"schema_version":1,"schema_version":1,"files":{}}'), "duplicate"),
            (lambda run: (run / "manifest.json").write_text('{"schema_version":NaN,"files":{}}'), "non-finite"),
        ):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                run = _run(Path(directory))
                mutation(run)
                with self.assertRaisesRegex(ValueError, message):
                    verify_historical_transition_run(run)
        with tempfile.TemporaryDirectory() as directory:
            run = _run(Path(directory))
            manifest = json.loads((run / "manifest.json").read_text())
            manifest["files"]["../model.json"] = manifest["files"].pop("model.json")
            (run / "manifest.json").write_bytes(canonical_json(manifest))
            with self.assertRaisesRegex(ValueError, "allowlist"):
                verify_historical_transition_run(run)

    def test_matching_manifest_hash_does_not_allow_numeric_overflow(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _run(Path(directory))
            model = b'{"schema_version":1,"nested":[1e400]}'
            (run / "model.json").write_bytes(model)
            manifest = json.loads((run / "manifest.json").read_text())
            manifest["files"]["model.json"] = {"sha256": hashlib.sha256(model).hexdigest(), "size_bytes": len(model)}
            (run / "manifest.json").write_bytes(canonical_json(manifest))
            with self.assertRaisesRegex(ValueError, "non-finite"):
                verify_historical_transition_run(run)


if __name__ == "__main__":
    unittest.main()
