from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fed_forecast.fomc_event_collection import EventCollectionError, event_slots, load_event_calendar
from fed_forecast.fomc_event_collection_reporting import (
    archive_observation, audit_archive, deterministic_gzip, verify_run_directory,
)
from fed_forecast.fomc_event_observer import load_observation_topology


ROOT = Path(__file__).resolve().parents[1]
TOPOLOGY = load_observation_topology(ROOT / "config/markets.json")


def observation(completed: datetime) -> dict[str, object]:
    identities = [
        (meeting.event_slug, "meeting", meeting.date.isoformat(), outcome.label)
        for meeting in TOPOLOGY.meetings for outcome in meeting.outcomes
    ] + [(TOPOLOGY.terminal_event_slug, "terminal", None, bucket.label) for bucket in TOPOLOGY.terminal_buckets]
    timestamps = [(completed - timedelta(seconds=index)).isoformat().replace("+00:00", "Z") for index in range(len(identities))]
    events = {
        slug: {"market_status": "active", "activity": {"liquidity": 100.0, "volume_24h": 10.0, "volume_total": 1000.0}}
        for slug, _, _, _ in identities
    }
    return {
        "schema_version": 1,
        "collector_version": "fomc-event-observer-v1",
        "started_at": timestamps[-1],
        "completed_at": completed.isoformat().replace("+00:00", "Z"),
        "markets_config_sha256": "c" * 64,
        "api_bases": {"gamma": "https://gamma-api.polymarket.com", "clob": "https://clob.polymarket.com"},
        "events": events,
        "coordinates": [{
            "event_slug": identity[0], "coordinate_kind": identity[1], "meeting_date": identity[2], "label": identity[3],
            "question": f"Question {index}", "yes_token": f"token-{index}",
            "raw_probability": 0.2 if identity[1] == "meeting" else 1 / 15, "source": "clob_midpoint", "quality": "good",
            "market_status": "active", "observed_at": timestamps[index], "exchange_quote_timestamp": None,
            "exchange_quote_age_seconds": None, "exchange_timestamp_status": "unavailable", "liquidity": 100.0,
            "best_bid": 0.19, "best_ask": 0.21, "spread": 0.02, "diagnostic_codes": [],
        } for index, identity in enumerate(identities)],
        "raw_responses": [{
            "method": "GET", "url": "https://gamma-api.polymarket.com/events/slug/example",
            "status": 200, "observed_at": timestamps[0], "body": {"evidence": True},
        }],
        "surface": {"coordinate_count": len(identities), "expected_coordinate_count": len(identities), "all_coordinates_complete": True},
    }


class EventArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.calendar = load_event_calendar(ROOT / "config/fomc_event_collection.json", ROOT / "config/markets.json")
        self.slot = event_slots(self.calendar, self.calendar.meetings[0])[2]
        self.topology = TOPOLOGY
        self.completed = self.slot.scheduled_at.astimezone(timezone.utc) + timedelta(seconds=50)
        self.github = {
            "repository": "owner/repo", "workflow": "workflow.yml", "run_id": "123",
            "run_attempt": 1, "run_number": 2, "head_sha": "b" * 40,
            "ref": "refs/heads/main", "event_name": "schedule", "cron": "*/5",
        }
        self.provenance = {
            "calendar_sha256": "a" * 64, "markets_config_sha256": "c" * 64,
            "fed_path_config_sha256": "d" * 64, "code_commit_sha": "b" * 40,
        }

    def test_gzip_and_archive_are_deterministic_verified_and_idempotent(self) -> None:
        self.assertEqual(deterministic_gzip(b"same"), deterministic_gzip(b"same"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = archive_observation(
                root, observation(self.completed), self.slot, self.topology, run_id="run-1",
                run_created_at=self.slot.scheduled_at, actual_start_at=self.slot.scheduled_at,
                github=self.github, provenance=self.provenance,
            )
            self.assertEqual(first.status, "captured")
            metadata = verify_run_directory(first.run_directory, self.topology)
            self.assertEqual(metadata["surface"]["coordinate_count"], 35)
            self.assertEqual(metadata["surface"]["exchange_timestamp_status"], "unavailable")
            duplicate = archive_observation(
                root, observation(self.completed + timedelta(seconds=1)), self.slot, self.topology, run_id="run-2",
                run_created_at=self.slot.scheduled_at, actual_start_at=self.slot.scheduled_at,
                github=self.github, provenance=self.provenance,
            )
            self.assertEqual(duplicate.status, "duplicate_same_slot")
            self.assertFalse((root / self.slot.meeting.event_id / "runs/run-2").exists())
            audit = audit_archive(root, self.slot.meeting.event_id, self.topology, self.calendar)
            self.assertEqual(audit["captured_count"], 1)
            self.assertEqual(len(audit["missing_slots"]), 26)

    def test_tampering_and_pointer_traversal_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = archive_observation(
                root, observation(self.completed), self.slot, self.topology, run_id="run-1",
                run_created_at=self.slot.scheduled_at, actual_start_at=self.slot.scheduled_at,
                github=self.github, provenance=self.provenance,
            )
            (result.run_directory / "collection.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(EventCollectionError, "manifest mismatch"):
                verify_run_directory(result.run_directory, self.topology)
            result.pointer_path.write_text(json.dumps({"run_path": "../../outside"}), encoding="utf-8")
            with self.assertRaises(EventCollectionError):
                audit_archive(root, self.slot.meeting.event_id, self.topology, self.calendar)

    def test_incomplete_surface_is_rejected_and_competing_writers_cannot_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            incomplete = observation(self.completed)
            incomplete["coordinates"] = incomplete["coordinates"][:-1]
            with self.assertRaisesRegex(EventCollectionError, "completeness|incomplete|reconcile"):
                archive_observation(
                    root, incomplete, self.slot, self.topology, run_id="bad",
                    run_created_at=self.slot.scheduled_at, actual_start_at=self.slot.scheduled_at,
                    github=self.github, provenance=self.provenance,
                )
            omitted = observation(self.completed)
            omitted_slug = self.topology.meetings[-1].event_slug
            omitted["coordinates"] = [row for row in omitted["coordinates"] if row["event_slug"] != omitted_slug]
            omitted["events"].pop(omitted_slug)
            omitted["surface"]["coordinate_count"] -= 5
            omitted["surface"]["expected_coordinate_count"] -= 5
            with self.assertRaisesRegex(EventCollectionError, "configured coordinate topology"):
                archive_observation(
                    root, omitted, self.slot, self.topology, run_id="omitted",
                    run_created_at=self.slot.scheduled_at, actual_start_at=self.slot.scheduled_at,
                    github=self.github, provenance=self.provenance,
                )
            barrier = threading.Barrier(2)
            statuses: list[str] = []
            errors: list[BaseException] = []

            def write(run_id: str) -> None:
                try:
                    barrier.wait()
                    result = archive_observation(
                        root, observation(self.completed), self.slot, self.topology, run_id=run_id,
                        run_created_at=self.slot.scheduled_at, actual_start_at=self.slot.scheduled_at,
                        github=self.github, provenance=self.provenance,
                    )
                    statuses.append(result.status)
                except BaseException as error:
                    errors.append(error)

            threads = [threading.Thread(target=write, args=(f"race-{index}",)) for index in range(2)]
            for thread in threads: thread.start()
            for thread in threads: thread.join()
            self.assertEqual(errors, [])
            self.assertEqual(sorted(statuses), ["captured", "duplicate_same_slot"])
            event_root = root / self.slot.meeting.event_id
            self.assertEqual(len(list((event_root / "runs").iterdir())), 1)
            self.assertEqual(audit_archive(root, self.slot.meeting.event_id, self.topology, self.calendar)["captured_count"], 1)
            (event_root / "slots" / "unexpected.tmp").write_text("x", encoding="utf-8")
            with self.assertRaisesRegex(EventCollectionError, "unexpected"):
                audit_archive(root, self.slot.meeting.event_id, self.topology, self.calendar)
            (event_root / "slots" / "unexpected.tmp").unlink()
            canonical = event_root / "slots" / f"{self.slot.slot_key}.json"
            canonical.rename(event_root / "slots" / "1200.json")
            with self.assertRaisesRegex(EventCollectionError, "canonical"):
                audit_archive(root, self.slot.meeting.event_id, self.topology, self.calendar)
