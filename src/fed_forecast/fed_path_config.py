"""Strict, pinned configuration for the fed-funds implied path."""

import hashlib
import json
import math
from datetime import date
from pathlib import Path
from typing import Any

from .fed_path_models import (
    FedPathConfig,
    MeetingConfig,
    OutcomeConfig,
    TerminalBucketConfig,
    WirpReferenceRow,
)


class FedPathConfigError(ValueError):
    """Raised when a fed-path configuration does not match its provenance."""


_TOP_LEVEL_KEYS = {
    "schema_version", "pricing_date", "source_image", "source_sha256",
    "target_upper_bound", "effective_rate_baseline", "standard_move_bp",
    "max_spread", "meetings", "terminal", "wirp_rows",
}
_MEETING_KEYS = {"date", "event_slug", "outcomes"}
_OUTCOME_KEYS = {"label", "representative_bp"}
_TERMINAL_KEYS = {"event_slug", "buckets"}
_TERMINAL_BUCKET_KEYS = {"label", "kind", "representative_rate"}
_WIRP_KEYS = {"date", "incremental_moves", "cumulative_moves", "implied_rate_delta", "implied_rate"}
_SOURCE_SHA256 = "4c298af9537ae26c22917d6f3f48eb9fd5ed2654be9ce17499b9b218d2397d34"
_OUTCOMES = (
    OutcomeConfig("50+ bps decrease", -50.0), OutcomeConfig("25 bps decrease", -25.0),
    OutcomeConfig("No change", 0.0), OutcomeConfig("25 bps increase", 25.0),
    OutcomeConfig("50+ bps increase", 50.0),
)
_MEETING_IDENTITIES = (
    (date(2026, 7, 29), "fed-decision-in-july-181"),
    (date(2026, 9, 16), "fed-decision-in-september-762"),
    (date(2026, 10, 28), "fed-decision-in-october-20260617190323537"),
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
_WIRP_ROWS = (
    WirpReferenceRow(date(2026, 7, 29), .337, .337, .084, 3.713), WirpReferenceRow(date(2026, 9, 16), .710, 1.047, .262, 3.890),
    WirpReferenceRow(date(2026, 10, 28), .270, 1.317, .329, 3.958), WirpReferenceRow(date(2026, 12, 9), .385, 1.701, .425, 4.054),
    WirpReferenceRow(date(2027, 1, 27), .185, 1.887, .472, 4.100), WirpReferenceRow(date(2027, 3, 17), .243, 2.130, .532, 4.161),
    WirpReferenceRow(date(2027, 4, 28), .107, 2.237, .559, 4.188), WirpReferenceRow(date(2027, 6, 9), -.002, 2.234, .559, 4.187),
    WirpReferenceRow(date(2027, 7, 28), -.058, 2.177, .544, 4.173), WirpReferenceRow(date(2027, 9, 15), -.125, 2.052, .513, 4.141),
    WirpReferenceRow(date(2027, 10, 27), -.085, 1.967, .492, 4.120),
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


def _source_path(config_path: Path, source_image: str, project_root: Path | None) -> Path:
    relative = Path(source_image)
    if relative.is_absolute() or ".." in relative.parts:
        raise FedPathConfigError("source_image must be a relative path without traversal")
    root = (Path(project_root) if project_root is not None else config_path.parent.parent).resolve()
    source = (root / relative).resolve()
    if not source.is_relative_to(root):
        raise FedPathConfigError("source_image must be a relative path without traversal")
    return source


def _meeting(value: Any) -> MeetingConfig:
    payload = _mapping(value, "meeting")
    _strict_keys(payload, _MEETING_KEYS, "meeting")
    outcomes_value = payload["outcomes"]
    if not isinstance(outcomes_value, list):
        raise FedPathConfigError("meeting outcomes must be an array")
    outcomes = tuple(
        OutcomeConfig(
            _string(_mapping(item, "outcome").get("label"), "outcome label"),
            _number(_mapping(item, "outcome").get("representative_bp"), "outcome representative_bp"),
        )
        for item in outcomes_value
    )
    for item in outcomes_value:
        _strict_keys(_mapping(item, "outcome"), _OUTCOME_KEYS, "outcome")
    if len({outcome.label for outcome in outcomes}) != len(outcomes):
        raise FedPathConfigError("meeting outcome labels must be unique")
    if outcomes != _OUTCOMES:
        raise FedPathConfigError("meeting must use the exact approved five-outcome topology")
    return MeetingConfig(_date(payload["date"], "meeting date"), _string(payload["event_slug"], "meeting event_slug"), outcomes)


def _terminal(value: Any) -> tuple[str, tuple[TerminalBucketConfig, ...]]:
    payload = _mapping(value, "terminal")
    _strict_keys(payload, _TERMINAL_KEYS, "terminal")
    raw_buckets = payload["buckets"]
    if not isinstance(raw_buckets, list):
        raise FedPathConfigError("terminal buckets must be an array")
    buckets: list[TerminalBucketConfig] = []
    for item in raw_buckets:
        row = _mapping(item, "terminal bucket")
        _strict_keys(row, _TERMINAL_BUCKET_KEYS, "terminal bucket")
        buckets.append(TerminalBucketConfig(_string(row["label"], "terminal bucket label"), _string(row["kind"], "terminal bucket kind"), _number(row["representative_rate"], "terminal bucket representative_rate")))
    result = tuple(buckets)
    if result != _TERMINAL_BUCKETS:
        raise FedPathConfigError("terminal must use the exact approved 15-bucket topology")
    slug = _string(payload["event_slug"], "terminal event_slug")
    if slug != "what-will-the-fed-rate-be-at-the-end-of-2026":
        raise FedPathConfigError("terminal event_slug must be the approved end-2026 event")
    return slug, result


def _wirp_row(value: Any) -> WirpReferenceRow:
    payload = _mapping(value, "WIRP row")
    _strict_keys(payload, _WIRP_KEYS, "WIRP row")
    return WirpReferenceRow(_date(payload["date"], "WIRP date"), *(_number(payload[key], f"WIRP {key}") for key in ("incremental_moves", "cumulative_moves", "implied_rate_delta", "implied_rate")))


def load_fed_path_config(path: Path, project_root: Path | None = None) -> FedPathConfig:
    """Load schema version 1 and validate the pinned WIRP reference identity."""
    config_path = Path(path)
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FedPathConfigError(f"could not read configuration: {error}") from error
    payload = _mapping(raw, "configuration")
    _strict_keys(payload, _TOP_LEVEL_KEYS, "configuration")
    if isinstance(payload["schema_version"], bool) or payload["schema_version"] != 1:
        raise FedPathConfigError("schema_version must be 1")
    if _date(payload["pricing_date"], "pricing_date") != date(2026, 7, 24):
        raise FedPathConfigError("pricing_date must match the approved WIRP reference")
    source_image = _string(payload["source_image"], "source_image")
    source_sha256 = _string(payload["source_sha256"], "source_sha256")
    source_path = _source_path(config_path, source_image, project_root)
    if source_image != "docs/source/wirp-fed-funds-2026-07-24.jpg":
        raise FedPathConfigError("source_image must match the approved WIRP reference")
    if source_sha256 != _SOURCE_SHA256:
        raise FedPathConfigError("source_sha256 must match the approved SHA-256")
    try:
        actual_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
    except OSError as error:
        raise FedPathConfigError("source image is missing or unreadable") from error
    if actual_hash != _SOURCE_SHA256:
        raise FedPathConfigError("source image SHA-256 does not match the approved SHA-256")
    target_upper_bound = _number(payload["target_upper_bound"], "target_upper_bound")
    effective_rate_baseline = _number(payload["effective_rate_baseline"], "effective_rate_baseline")
    standard_move_bp = _number(payload["standard_move_bp"], "standard_move_bp")
    max_spread = _number(payload["max_spread"], "max_spread")
    if (target_upper_bound, effective_rate_baseline, standard_move_bp, max_spread) != (3.75, 3.628, 25.0, 0.1):
        raise FedPathConfigError("baselines and price-quality values must match the approved reference")
    raw_meetings = payload["meetings"]
    if not isinstance(raw_meetings, list) or len(raw_meetings) != 3:
        raise FedPathConfigError("meetings must contain the three approved events")
    meetings = tuple(_meeting(item) for item in raw_meetings)
    if len({meeting.date for meeting in meetings}) != len(meetings) or len({meeting.event_slug.strip().casefold() for meeting in meetings}) != len(meetings):
        raise FedPathConfigError("meeting dates and event slugs must be unique")
    if tuple(sorted(meeting.date for meeting in meetings)) != tuple(meeting.date for meeting in meetings):
        raise FedPathConfigError("meetings must be chronological")
    if tuple((meeting.date, meeting.event_slug) for meeting in meetings) != _MEETING_IDENTITIES:
        raise FedPathConfigError("meetings must match the exact approved meeting identities")
    terminal_slug, terminal_buckets = _terminal(payload["terminal"])
    wirp_value = payload["wirp_rows"]
    if not isinstance(wirp_value, list):
        raise FedPathConfigError("wirp_rows must be an array")
    wirp_rows = tuple(_wirp_row(item) for item in wirp_value)
    if wirp_rows != _WIRP_ROWS:
        raise FedPathConfigError("WIRP rows must match the approved transcription")
    return FedPathConfig(1, date(2026, 7, 24), source_image, source_sha256, target_upper_bound, effective_rate_baseline, standard_move_bp, max_spread, meetings, terminal_slug, terminal_buckets, wirp_rows)
