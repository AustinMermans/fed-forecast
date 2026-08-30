from __future__ import annotations

import re
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fed_forecast.fomc_event_collection import event_slots, load_event_calendar


ROOT = Path(__file__).resolve().parents[1]


class EventWorkflowTests(unittest.TestCase):
    def test_scheduled_workflow_is_isolated_disabled_and_exactly_scoped(self) -> None:
        workflow = (ROOT / ".github/workflows/collect-fomc-event.yml").read_text(encoding="utf-8")
        self.assertIn("vars.ENABLE_FOMC_EVENT_COLLECTION == 'true'", workflow)
        self.assertIn("group: fomc-event-collection", workflow)
        self.assertIn("contents: write", workflow)
        self.assertIn("actions: read", workflow)
        self.assertNotIn("pages: write", workflow)
        self.assertNotIn("id-token: write", workflow)
        self.assertNotIn("actions/deploy-pages", workflow)
        self.assertIn("git pull --ff-only origin event-data", workflow)
        self.assertIn('git add -- "fomc-events/$EVENT_ID/$RUN_PATH" "fomc-events/$EVENT_ID/$POINTER_PATH"', workflow)
        self.assertIn("git push origin HEAD:event-data", workflow)
        self.assertNotIn("site/", workflow)
        cron = re.findall(r'cron: "([^"]+)"', workflow)
        self.assertEqual(len(cron), 12)
        self.assertEqual(len(set(cron)), 12)
        calendar = load_event_calendar(ROOT / "config/fomc_event_collection.json", ROOT / "config/markets.json")

        def matches(expression: str, value) -> bool:  # type: ignore[no-untyped-def]
            minute, hour, day, month, _ = expression.split()
            def field(spec: str, number: int) -> bool:
                if spec == "*": return True
                if spec.startswith("*/"): return number % int(spec[2:]) == 0
                if "," in spec: return number in {int(item) for item in spec.split(",")}
                if "-" in spec:
                    start, end = (int(item) for item in spec.split("-"))
                    return start <= number <= end
                return number == int(spec)
            return field(minute, value.minute) and field(hour, value.hour) and field(day, value.day) and field(month, value.month)

        for meeting in calendar.meetings:
            expected = {slot.scheduled_at.astimezone(timezone.utc) for slot in event_slots(calendar, meeting)}
            for utc in expected:
                self.assertEqual(sum(matches(expression, utc) for expression in cron), 1)
            utc_date = next(iter(expected)).date()
            cursor = datetime.combine(utc_date, datetime.min.time(), timezone.utc)
            emitted = set()
            for _ in range(24 * 60):
                if any(matches(expression, cursor) for expression in cron):
                    emitted.add(cursor)
                cursor += timedelta(minutes=1)
            self.assertEqual(emitted, expected)

    def test_manual_inspection_is_read_only_and_cannot_collect(self) -> None:
        workflow = (ROOT / ".github/workflows/inspect-fomc-event-collector.yml").read_text(encoding="utf-8")
        self.assertIn("contents: read", workflow)
        self.assertNotIn("contents: write", workflow)
        self.assertNotIn(" collect \\", workflow)
        self.assertNotIn("git push", workflow)
        self.assertNotIn("deploy-pages", workflow)

    def test_existing_refresh_workflow_remains_six_hour_pages_publisher(self) -> None:
        workflow = (ROOT / ".github/workflows/refresh-pages.yml").read_text(encoding="utf-8")
        self.assertIn('cron: "17 */6 * * *"', workflow)
        self.assertIn("actions/deploy-pages@v4", workflow)
