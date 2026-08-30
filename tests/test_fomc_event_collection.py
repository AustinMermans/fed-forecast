from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from fed_forecast.fomc_event_collection import (
    EventCollectionError, event_slots, infer_event_slot, load_event_calendar,
    validate_runner_lateness,
)


ROOT = Path(__file__).resolve().parents[1]


class EventCalendarTests(unittest.TestCase):
    def setUp(self) -> None:
        self.calendar = load_event_calendar(
            ROOT / "config/fomc_event_collection.json", ROOT / "config/markets.json",
        )

    def test_calendar_has_exact_slots_and_descriptive_phases(self) -> None:
        for meeting in self.calendar.meetings:
            slots = event_slots(self.calendar, meeting)
            self.assertEqual(len(slots), 27)
            self.assertEqual(slots[0].slot_key, "1355")
            self.assertEqual(slots[-1].slot_key, "1605")
            counts = {phase: sum(item.phase == phase for item in slots) for phase in {item.phase for item in slots}}
            self.assertEqual(counts, {
                "pre_action": 1, "action_window": 3, "pre_presser": 3,
                "presser": 13, "post_presser": 7,
            })

    def test_dst_maps_new_york_slots_to_the_correct_utc_hour(self) -> None:
        summer = event_slots(self.calendar, self.calendar.meetings[0])[0]
        winter = event_slots(self.calendar, self.calendar.meetings[2])[0]
        self.assertEqual(summer.scheduled_at.astimezone(timezone.utc).strftime("%H:%M"), "17:55")
        self.assertEqual(winter.scheduled_at.astimezone(timezone.utc).strftime("%H:%M"), "18:55")

    def test_gate_is_year_date_slot_and_lateness_strict(self) -> None:
        accepted = infer_event_slot(self.calendar, datetime(2026, 9, 16, 18, 5, 41, tzinfo=timezone.utc))
        self.assertIsNotNone(accepted)
        assert accepted is not None
        self.assertEqual(accepted.slot_key, "1405")
        self.assertEqual(validate_runner_lateness(
            self.calendar, accepted, datetime(2026, 9, 16, 18, 16, 59, tzinfo=timezone.utc),
        ), 719.0)
        self.assertIsNone(infer_event_slot(self.calendar, datetime(2025, 9, 16, 18, 5, tzinfo=timezone.utc)))
        self.assertIsNone(infer_event_slot(self.calendar, datetime(2026, 9, 16, 17, 50, tzinfo=timezone.utc)))
        self.assertIsNone(infer_event_slot(self.calendar, datetime(2026, 9, 16, 17, 52, 29, tzinfo=timezone.utc)))
        with self.assertRaisesRegex(EventCollectionError, "lateness"):
            validate_runner_lateness(
                self.calendar, accepted, datetime(2026, 9, 16, 18, 17, 1, tzinfo=timezone.utc),
            )

    def test_strict_calendar_rejects_unknown_duplicate_and_market_mismatch(self) -> None:
        original = json.loads((ROOT / "config/fomc_event_collection.json").read_text())
        markets = ROOT / "config/markets.json"
        mutations = []
        unknown = json.loads(json.dumps(original)); unknown["unknown"] = True; mutations.append(unknown)
        bad_zone = json.loads(json.dumps(original)); bad_zone["timezone"] = "UTC"; mutations.append(bad_zone)
        duplicate_date = json.loads(json.dumps(original)); duplicate_date["meetings"][1]["decision_date"] = duplicate_date["meetings"][0]["decision_date"]; mutations.append(duplicate_date)
        absent = json.loads(json.dumps(original)); absent["meetings"][0]["decision_date"] = "2026-09-17"; absent["meetings"][0]["event_id"] = "fomc-2026-09-17"; mutations.append(absent)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calendar.json"
            for payload in mutations:
                with self.subTest(payload=payload):
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaises(EventCollectionError):
                        load_event_calendar(path, markets)
            path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
            with self.assertRaisesRegex(EventCollectionError, "duplicate"):
                load_event_calendar(path, markets)
