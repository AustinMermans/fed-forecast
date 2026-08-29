"""Strict configuration for the public fed-funds meeting model."""

import json
import math
from datetime import date
from pathlib import Path
from typing import Any

from .fed_path_models import FedPathConfig, MeetingConfig, OutcomeConfig, TerminalBucketConfig


class FedPathConfigError(ValueError):
    """Raised when the fed-path market topology is invalid."""


_TOP_LEVEL_KEYS = {
    "schema_version", "target_upper_bound", "effective_rate_baseline",
    "standard_move_bp", "max_spread", "meetings", "terminal",
}
_MEETING_KEYS = {"date", "event_slug", "outcomes"}
_OUTCOME_KEYS = {"label", "representative_bp"}
_TERMINAL_KEYS = {"event_slug", "buckets"}
_TERMINAL_BUCKET_KEYS = {"label", "kind", "representative_rate"}
_OUTCOMES = (
    OutcomeConfig("50+ bps decrease", -50.0), OutcomeConfig("25 bps decrease", -25.0),
    OutcomeConfig("No change", 0.0), OutcomeConfig("25 bps increase", 25.0),
    OutcomeConfig("50+ bps increase", 50.0),
)
_TERMINAL_BUCKETS = (
    TerminalBucketConfig("≤1.0%", "lte", 1.0),
    *(TerminalBucketConfig(label, "exact", rate) for label, rate in (
        ("1.25%", 1.25), ("1.5%", 1.5), ("1.75%", 1.75), ("2.0%", 2.0),
        ("2.25%", 2.25), ("2.5%", 2.5), ("2.75%", 2.75), ("3.0%", 3.0),
        ("3.25%", 3.25), ("3.5%", 3.5), ("3.75%", 3.75), ("4.0%", 4.0),
        ("4.25%", 4.25),
    )),
    TerminalBucketConfig("≥4.5%", "gte", 4.5),
)


def _mapping(value: Any, description: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FedPathConfigError(f"{description} must be an object")
    return value


def _strict_keys(payload: dict[str, Any], expected: set[str], description: str) -> None:
    unknown, missing = payload.keys() - expected, expected - payload.keys()
    if unknown:
        raise FedPathConfigError(f"{description} contains unknown key(s): {', '.join(sorted(unknown))}")
    if missing:
        raise FedPathConfigError(f"{description} is missing required key(s): {', '.join(sorted(missing))}")


def _string(value: Any, description: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FedPathConfigError(f"{description} must be a non-empty string")
    return value


def _number(value: Any, description: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise FedPathConfigError(f"{description} must be a finite number")
    return float(value)


def _date(value: Any, description: str) -> date:
    if not isinstance(value, str):
        raise FedPathConfigError(f"{description} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise FedPathConfigError(f"{description} must be an ISO date") from error


def _meeting(value: Any) -> MeetingConfig:
    payload = _mapping(value, "meeting")
    _strict_keys(payload, _MEETING_KEYS, "meeting")
    outcomes_value = payload["outcomes"]
    if not isinstance(outcomes_value, list):
        raise FedPathConfigError("meeting outcomes must be an array")
    outcomes = []
    for item in outcomes_value:
        row = _mapping(item, "outcome")
        _strict_keys(row, _OUTCOME_KEYS, "outcome")
        outcomes.append(OutcomeConfig(
            _string(row["label"], "outcome label"),
            _number(row["representative_bp"], "outcome representative_bp"),
        ))
    if tuple(outcomes) != _OUTCOMES:
        raise FedPathConfigError("meeting must use the exact five-outcome topology")
    return MeetingConfig(
        _date(payload["date"], "meeting date"),
        _string(payload["event_slug"], "meeting event_slug"),
        tuple(outcomes),
    )


def _terminal(value: Any) -> tuple[str, tuple[TerminalBucketConfig, ...]]:
    payload = _mapping(value, "terminal")
    _strict_keys(payload, _TERMINAL_KEYS, "terminal")
    raw_buckets = payload["buckets"]
    if not isinstance(raw_buckets, list):
        raise FedPathConfigError("terminal buckets must be an array")
    buckets = []
    for item in raw_buckets:
        row = _mapping(item, "terminal bucket")
        _strict_keys(row, _TERMINAL_BUCKET_KEYS, "terminal bucket")
        buckets.append(TerminalBucketConfig(
            _string(row["label"], "terminal bucket label"),
            _string(row["kind"], "terminal bucket kind"),
            _number(row["representative_rate"], "terminal bucket representative_rate"),
        ))
    if tuple(buckets) != _TERMINAL_BUCKETS:
        raise FedPathConfigError("terminal must use the exact 15-bucket topology")
    slug = _string(payload["event_slug"], "terminal event_slug")
    if slug != "what-will-the-fed-rate-be-at-the-end-of-2026":
        raise FedPathConfigError("terminal event_slug must be the configured end-2026 event")
    return slug, tuple(buckets)


def load_fed_path_config(path: Path, project_root: Path | None = None) -> FedPathConfig:
    """Load the market topology; ``project_root`` remains for API compatibility."""
    del project_root
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FedPathConfigError(f"could not read configuration: {error}") from error
    payload = _mapping(raw, "configuration")
    _strict_keys(payload, _TOP_LEVEL_KEYS, "configuration")
    if isinstance(payload["schema_version"], bool) or payload["schema_version"] != 2:
        raise FedPathConfigError("schema_version must be 2")
    target_upper_bound = _number(payload["target_upper_bound"], "target_upper_bound")
    effective_rate_baseline = _number(payload["effective_rate_baseline"], "effective_rate_baseline")
    standard_move_bp = _number(payload["standard_move_bp"], "standard_move_bp")
    max_spread = _number(payload["max_spread"], "max_spread")
    if not 0 <= effective_rate_baseline <= target_upper_bound <= 20:
        raise FedPathConfigError("policy baselines are outside their valid range")
    if standard_move_bp <= 0 or not 0 < max_spread <= 1:
        raise FedPathConfigError("price-quality settings are outside their valid range")
    raw_meetings = payload["meetings"]
    if not isinstance(raw_meetings, list) or not raw_meetings:
        raise FedPathConfigError("meetings must be a non-empty array")
    meetings = tuple(_meeting(item) for item in raw_meetings)
    if len({meeting.date for meeting in meetings}) != len(meetings) or len({meeting.event_slug.casefold() for meeting in meetings}) != len(meetings):
        raise FedPathConfigError("meeting dates and event slugs must be unique")
    if tuple(sorted(meeting.date for meeting in meetings)) != tuple(meeting.date for meeting in meetings):
        raise FedPathConfigError("meetings must be chronological")
    terminal_slug, terminal_buckets = _terminal(payload["terminal"])
    return FedPathConfig(
        2, target_upper_bound, effective_rate_baseline, standard_move_bp, max_spread,
        meetings, terminal_slug, terminal_buckets,
    )
