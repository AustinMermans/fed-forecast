"""Strict calendar and payload helpers for granular FOMC observations."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse
from zoneinfo import ZoneInfo


NY = ZoneInfo("America/New_York")
COLLECTOR_VERSION = "fomc-event-collector-v1"
_TOP_KEYS = {"schema_version", "timezone", "window", "maximum_runner_lateness_seconds", "meetings"}
_WINDOW_KEYS = {"start", "end", "interval_minutes"}
_MEETING_KEYS = {
    "event_id", "decision_date", "statement_time", "presser_time", "presser_end_time",
    "sep", "status", "official_calendar_url",
}
_EVENT_ID = re.compile(r"fomc-\d{4}-\d{2}-\d{2}")


class EventCollectionError(ValueError):
    """The event calendar, gate, or observation payload is invalid."""


@dataclass(frozen=True)
class EventMeeting:
    event_id: str
    decision_date: date
    statement_time: time
    presser_time: time
    presser_end_time: time
    sep: bool
    status: str
    official_calendar_url: str


@dataclass(frozen=True)
class EventCalendar:
    schema_version: int
    timezone_name: str
    window_start: time
    window_end: time
    interval_minutes: int
    maximum_runner_lateness_seconds: int
    meetings: tuple[EventMeeting, ...]


@dataclass(frozen=True)
class EventSlot:
    meeting: EventMeeting
    scheduled_at: datetime
    phase: str
    slot_index: int

    @property
    def slot_key(self) -> str:
        return self.scheduled_at.strftime("%H%M")


def _strict_json(path: Path) -> dict[str, Any]:
    def reject_constant(value: str) -> None:
        raise EventCollectionError(f"non-standard JSON constant: {value}")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise EventCollectionError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(path.read_text(encoding="utf-8"), parse_constant=reject_constant, object_pairs_hook=pairs)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EventCollectionError(f"could not read event calendar: {error}") from error
    if not isinstance(value, dict):
        raise EventCollectionError("event calendar must be an object")
    return value


def _keys(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    if set(value) != expected:
        unknown = sorted(set(value) - expected)
        missing = sorted(expected - set(value))
        raise EventCollectionError(f"{name} keys invalid; unknown={unknown}, missing={missing}")


def _clock(value: Any, name: str) -> time:
    if not isinstance(value, str) or re.fullmatch(r"\d{2}:\d{2}", value) is None:
        raise EventCollectionError(f"{name} must be HH:MM")
    try:
        parsed = time.fromisoformat(value)
    except ValueError as error:
        raise EventCollectionError(f"{name} is invalid") from error
    if parsed.second or parsed.microsecond:
        raise EventCollectionError(f"{name} must be minute-aligned")
    return parsed


def _iso_date(value: Any, name: str) -> date:
    if not isinstance(value, str):
        raise EventCollectionError(f"{name} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise EventCollectionError(f"{name} must be an ISO date") from error


def _market_dates(path: Path) -> set[date]:
    payload = _strict_json(path)
    meetings = payload.get("meetings")
    if not isinstance(meetings, list):
        raise EventCollectionError("market configuration meetings must be an array")
    result: set[date] = set()
    for item in meetings:
        if not isinstance(item, dict):
            raise EventCollectionError("market meeting must be an object")
        result.add(_iso_date(item.get("date"), "market meeting date"))
    return result


def load_event_calendar(path: Path, markets_path: Path) -> EventCalendar:
    payload = _strict_json(path)
    _keys(payload, _TOP_KEYS, "event calendar")
    if payload["schema_version"] != 1 or isinstance(payload["schema_version"], bool):
        raise EventCollectionError("schema_version must be 1")
    if payload["timezone"] != "America/New_York":
        raise EventCollectionError("timezone must be America/New_York")
    window = payload["window"]
    if not isinstance(window, dict):
        raise EventCollectionError("window must be an object")
    _keys(window, _WINDOW_KEYS, "window")
    start, end = _clock(window["start"], "window start"), _clock(window["end"], "window end")
    interval = window["interval_minutes"]
    lateness = payload["maximum_runner_lateness_seconds"]
    if isinstance(interval, bool) or not isinstance(interval, int) or interval != 5:
        raise EventCollectionError("interval_minutes must be 5")
    if isinstance(lateness, bool) or not isinstance(lateness, int) or not 0 <= lateness <= 1800:
        raise EventCollectionError("maximum_runner_lateness_seconds must be an integer in [0, 1800]")
    if not start < end or start.minute % interval or end.minute % interval:
        raise EventCollectionError("window must be increasing and five-minute aligned")
    raw_meetings = payload["meetings"]
    if not isinstance(raw_meetings, list) or not raw_meetings:
        raise EventCollectionError("meetings must be a non-empty array")
    market_dates = _market_dates(markets_path)
    meetings: list[EventMeeting] = []
    for raw in raw_meetings:
        if not isinstance(raw, dict):
            raise EventCollectionError("meeting must be an object")
        _keys(raw, _MEETING_KEYS, "meeting")
        event_id = raw["event_id"]
        decision_date = _iso_date(raw["decision_date"], "decision_date")
        if not isinstance(event_id, str) or _EVENT_ID.fullmatch(event_id) is None or event_id != f"fomc-{decision_date.isoformat()}":
            raise EventCollectionError("event_id must exactly match decision_date")
        statement = _clock(raw["statement_time"], "statement_time")
        presser = _clock(raw["presser_time"], "presser_time")
        presser_end = _clock(raw["presser_end_time"], "presser_end_time")
        if (statement, presser, presser_end) != (time(14, 0), time(14, 30), time(15, 30)):
            raise EventCollectionError("scheduled FOMC phase times must be 14:00, 14:30, and 15:30")
        if not start < statement < presser < presser_end < end:
            raise EventCollectionError("meeting phase times must be strictly inside the collection window")
        if any(value.minute % interval for value in (statement, presser, presser_end)):
            raise EventCollectionError("meeting phase times must be five-minute aligned")
        if not isinstance(raw["sep"], bool) or raw["status"] != "scheduled":
            raise EventCollectionError("meeting sep/status is invalid")
        url = raw["official_calendar_url"]
        parsed_url = urlparse(url) if isinstance(url, str) else None
        if parsed_url is None or parsed_url.scheme != "https" or parsed_url.hostname != "www.federalreserve.gov":
            raise EventCollectionError("official_calendar_url must be an HTTPS federalreserve.gov URL")
        if decision_date not in market_dates:
            raise EventCollectionError(f"{decision_date} is absent from the market configuration")
        meetings.append(EventMeeting(event_id, decision_date, statement, presser, presser_end, raw["sep"], raw["status"], url))
    if len({item.event_id for item in meetings}) != len(meetings) or len({item.decision_date for item in meetings}) != len(meetings):
        raise EventCollectionError("meeting event IDs and dates must be unique")
    if meetings != sorted(meetings, key=lambda item: item.decision_date):
        raise EventCollectionError("meetings must be chronological")
    return EventCalendar(1, payload["timezone"], start, end, interval, lateness, tuple(meetings))


def event_slots(calendar: EventCalendar, meeting: EventMeeting) -> tuple[EventSlot, ...]:
    cursor = datetime.combine(meeting.decision_date, calendar.window_start, NY)
    end = datetime.combine(meeting.decision_date, calendar.window_end, NY)
    result: list[EventSlot] = []
    while cursor <= end:
        if cursor.time() < meeting.statement_time:
            phase = "pre_action"
        elif cursor.time() < time(14, 15):
            phase = "action_window"
        elif cursor.time() < meeting.presser_time:
            phase = "pre_presser"
        elif cursor.time() <= meeting.presser_end_time:
            phase = "presser"
        else:
            phase = "post_presser"
        result.append(EventSlot(meeting, cursor, phase, len(result)))
        cursor += timedelta(minutes=calendar.interval_minutes)
    return tuple(result)


def infer_event_slot(calendar: EventCalendar, run_created_at: datetime) -> EventSlot | None:
    if run_created_at.tzinfo is None:
        raise EventCollectionError("run_created_at must include an offset")
    local = run_created_at.astimezone(NY)
    meeting = next((item for item in calendar.meetings if item.decision_date == local.date() and item.status == "scheduled"), None)
    if meeting is None:
        return None
    candidates = event_slots(calendar, meeting)
    nearest = min(candidates, key=lambda item: abs((local - item.scheduled_at).total_seconds()))
    if abs((local - nearest.scheduled_at).total_seconds()) > 150:
        return None
    return nearest


def validate_runner_lateness(calendar: EventCalendar, slot: EventSlot, actual_start_at: datetime) -> float:
    if actual_start_at.tzinfo is None:
        raise EventCollectionError("actual_start_at must include an offset")
    seconds = (actual_start_at.astimezone(timezone.utc) - slot.scheduled_at.astimezone(timezone.utc)).total_seconds()
    if seconds < -5 or seconds > calendar.maximum_runner_lateness_seconds:
        raise EventCollectionError("runner start is outside the permitted lateness window")
    return seconds


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def finite_number(value: object, name: str, *, minimum: float | None = None, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise EventCollectionError(f"{name} must be finite")
    result = float(value)
    if minimum is not None and result < minimum or maximum is not None and result > maximum:
        raise EventCollectionError(f"{name} is outside its valid range")
    return result
