"""Immutable reporting and verification for granular FOMC observations."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import math
import os
import re
import secrets
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping

from .fed_path_models import FedPathConfig
from .fomc_event_collection import COLLECTOR_VERSION, EventCalendar, EventCollectionError, EventSlot, event_slots


MAX_METADATA_BYTES = 32 * 1024
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
MAX_COMPRESSED_BYTES = 2 * 1024 * 1024
MAX_EVENT_BYTES = 128 * 1024 * 1024
EXPECTED_SLOT_COUNT = 27
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_MEETING_LABELS = {
    "50+ bps decrease", "25 bps decrease", "No change", "25 bps increase", "50+ bps increase",
}
_TERMINAL_LABELS = {
    "≤1.0%", "1.25%", "1.5%", "1.75%", "2.0%", "2.25%", "2.5%", "2.75%",
    "3.0%", "3.25%", "3.5%", "3.75%", "4.0%", "4.25%", "≥4.5%",
}
_OBSERVATION_KEYS = {
    "schema_version", "collector_version", "started_at", "completed_at", "markets_config_sha256",
    "api_bases", "events", "coordinates", "raw_responses", "surface",
}
_COORDINATE_KEYS = {
    "event_slug", "coordinate_kind", "meeting_date", "label", "question", "yes_token",
    "raw_probability", "source", "quality", "market_status", "observed_at",
    "exchange_quote_timestamp", "exchange_quote_age_seconds", "exchange_timestamp_status",
    "liquidity", "best_bid", "best_ask", "spread", "diagnostic_codes",
}
_QUOTE_KEYS = {
    "event_slug", "coordinate_kind", "meeting_date", "label", "raw_probability", "source",
    "quality", "market_status", "observed_at", "exchange_quote_timestamp",
    "exchange_quote_age_seconds", "exchange_timestamp_status", "liquidity", "best_bid",
    "best_ask", "spread", "diagnostic_codes", "client_retrieval_age_at_completion_seconds",
}
_INFERENCE_METHOD = "nearest configured five-minute slot to GitHub run created_at"


@dataclass(frozen=True)
class ArchiveResult:
    status: str
    run_directory: Path
    pointer_path: Path


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def strict_json_loads(data: bytes) -> object:
    def reject_constant(value: str) -> None:
        raise EventCollectionError(f"non-standard JSON constant: {value}")

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise EventCollectionError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        result = json.loads(data, parse_constant=reject_constant, object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EventCollectionError("JSON is invalid") from error

    def finite(value: object) -> None:
        if isinstance(value, float) and not math.isfinite(value):
            raise EventCollectionError("JSON contains a non-finite number")
        if isinstance(value, dict):
            for item in value.values():
                finite(item)
        elif isinstance(value, list):
            for item in value:
                finite(item)

    finite(result)
    return result


def deterministic_gzip(data: bytes) -> bytes:
    output = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0, compresslevel=9) as stream:
        stream.write(data)
    return output.getvalue()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _parse_timestamp(value: object, name: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise EventCollectionError(f"{name} must be a UTC Z timestamp")
    try:
        result = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise EventCollectionError(f"{name} is invalid") from error
    return result


def _finite(value: object, name: str, minimum: float | None = None, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise EventCollectionError(f"{name} must be finite")
    result = float(value)
    if minimum is not None and result < minimum or maximum is not None and result > maximum:
        raise EventCollectionError(f"{name} is outside its valid range")
    return result


def _expected_identities(config: FedPathConfig) -> set[tuple[str, str]]:
    return {
        *((meeting.event_slug, outcome.label) for meeting in config.meetings for outcome in meeting.outcomes),
        *((config.terminal_event_slug, bucket.label) for bucket in config.terminal_buckets),
    }


def validate_observation(observation: Mapping[str, object], topology: FedPathConfig) -> list[dict[str, object]]:
    if set(observation) != _OBSERVATION_KEYS or observation.get("schema_version") != 1 or observation.get("collector_version") != "fomc-event-observer-v1":
        raise EventCollectionError("observation schema is invalid")
    started = _parse_timestamp(observation.get("started_at"), "observation started_at")
    completed = _parse_timestamp(observation.get("completed_at"), "observation completed_at")
    if completed < started:
        raise EventCollectionError("observation timestamps are reversed")
    digest = observation.get("markets_config_sha256")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise EventCollectionError("observation market configuration digest is invalid")
    if observation.get("api_bases") != {"gamma": "https://gamma-api.polymarket.com", "clob": "https://clob.polymarket.com"}:
        raise EventCollectionError("observation API provenance is invalid")
    coordinates = observation.get("coordinates")
    events = observation.get("events")
    raw_responses = observation.get("raw_responses")
    surface = observation.get("surface")
    if not isinstance(coordinates, list) or not coordinates or not isinstance(events, dict) or not isinstance(raw_responses, list) or not raw_responses or not isinstance(surface, dict):
        raise EventCollectionError("observation surface evidence is incomplete")
    if set(surface) != {"coordinate_count", "expected_coordinate_count", "all_coordinates_complete"}:
        raise EventCollectionError("observation surface schema is invalid")
    rows: list[dict[str, object]] = []
    identities: set[tuple[str, str]] = set()
    tokens: set[str] = set()
    by_event: dict[str, list[dict[str, object]]] = {}
    for raw in coordinates:
        if not isinstance(raw, dict) or set(raw) != _COORDINATE_KEYS:
            raise EventCollectionError("coordinate schema is invalid")
        slug, label, token = raw["event_slug"], raw["label"], raw["yes_token"]
        if not all(isinstance(value, str) and value for value in (slug, label, token, raw["question"])):
            raise EventCollectionError("coordinate identity is invalid")
        identity = (slug, label)
        if identity in identities or token in tokens:
            raise EventCollectionError("coordinate identities and tokens must be unique")
        identities.add(identity); tokens.add(token)
        kind, status = raw["coordinate_kind"], raw["market_status"]
        if kind not in {"meeting", "terminal"} or status not in {"active", "closed_pending_resolution", "resolved"}:
            raise EventCollectionError("coordinate kind or lifecycle is invalid")
        if kind == "meeting":
            if label not in _MEETING_LABELS or not isinstance(raw["meeting_date"], str):
                raise EventCollectionError("meeting coordinate topology is invalid")
        elif label not in _TERMINAL_LABELS or raw["meeting_date"] is not None:
            raise EventCollectionError("terminal coordinate topology is invalid")
        _finite(raw["raw_probability"], "raw_probability", 0.0, 1.0)
        _parse_timestamp(raw["observed_at"], "coordinate observed_at")
        if raw["exchange_quote_timestamp"] is not None or raw["exchange_quote_age_seconds"] is not None or raw["exchange_timestamp_status"] != "unavailable":
            raise EventCollectionError("unsupported exchange timestamp claim")
        if raw["liquidity"] is not None:
            _finite(raw["liquidity"], "liquidity", 0.0)
        for name in ("best_bid", "best_ask", "spread"):
            if raw[name] is not None:
                _finite(raw[name], name, 0.0, 1.0)
        if not isinstance(raw["diagnostic_codes"], list) or any(not isinstance(item, str) for item in raw["diagnostic_codes"]):
            raise EventCollectionError("coordinate diagnostics are invalid")
        if status == "active" and raw["source"] not in {"clob_midpoint", "gamma"}:
            raise EventCollectionError("active coordinate source is invalid")
        if status == "active" and raw["source"] == "clob_midpoint" and (
            raw["quality"] != "good" or any(raw[name] is None for name in ("best_bid", "best_ask", "spread"))
        ):
            raise EventCollectionError("live CLOB coordinate quality is invalid")
        if status == "active" and raw["source"] == "gamma" and (
            raw["quality"] != "degraded" or "gamma_fallback_price" not in raw["diagnostic_codes"]
        ):
            raise EventCollectionError("active Gamma fallback quality is invalid")
        if status == "closed_pending_resolution" and (raw["source"] != "gamma_pending_resolution" or any(raw[name] is not None for name in ("best_bid", "best_ask", "spread"))):
            raise EventCollectionError("pending coordinate source is invalid")
        if status in {"closed_pending_resolution", "resolved"} and raw["quality"] != "closed_not_live":
            raise EventCollectionError("closed coordinate quality is invalid")
        if status == "resolved" and (raw["source"] != "gamma_resolution" or raw["raw_probability"] not in {0.0, 1.0} or any(raw[name] is not None for name in ("best_bid", "best_ask", "spread"))):
            raise EventCollectionError("resolved coordinate is invalid")
        rows.append(raw)
        by_event.setdefault(str(slug), []).append(raw)
    terminal_events = 0
    for slug, items in by_event.items():
        kinds = {item["coordinate_kind"] for item in items}
        statuses = {item["market_status"] for item in items}
        if len(kinds) != 1 or len(statuses) != 1:
            raise EventCollectionError("event coordinate lifecycle/topology is mixed")
        kind = next(iter(kinds))
        expected = _TERMINAL_LABELS if kind == "terminal" else _MEETING_LABELS
        if {item["label"] for item in items} != expected:
            raise EventCollectionError(f"{slug} coordinate labels are incomplete")
        if kind == "terminal":
            terminal_events += 1
        if next(iter(statuses)) == "resolved" and sum(float(item["raw_probability"]) for item in items) != 1.0:
            raise EventCollectionError("resolved event must have exactly one winner")
    if terminal_events != 1 or set(events) != set(by_event):
        raise EventCollectionError("event surface does not reconcile")
    if identities != _expected_identities(topology):
        raise EventCollectionError("observation does not match the configured coordinate topology")
    configured_dates = {meeting.event_slug: meeting.date.isoformat() for meeting in topology.meetings}
    for slug, items in by_event.items():
        if slug == topology.terminal_event_slug:
            continue
        if slug not in configured_dates or {item["meeting_date"] for item in items} != {configured_dates[slug]}:
            raise EventCollectionError("meeting coordinate dates do not match the configured topology")
    for slug, event in events.items():
        if not isinstance(event, dict) or set(event) != {"market_status", "activity"} or event["market_status"] != by_event[slug][0]["market_status"]:
            raise EventCollectionError("event lifecycle summary does not reconcile")
        activity = event["activity"]
        if not isinstance(activity, dict) or set(activity) != {"liquidity", "volume_24h", "volume_total"}:
            raise EventCollectionError("event activity schema is invalid")
        for name, value in activity.items():
            if value is not None:
                _finite(value, f"event activity {name}", 0.0)
    for response in raw_responses:
        if not isinstance(response, dict) or not {"method", "url", "status", "body"} <= set(response):
            raise EventCollectionError("raw response schema is invalid")
        allowed = (
            {"method", "url", "status", "body", "observed_at"},
            {"method", "url", "status", "body", "retrieved_at"},
            {"method", "url", "status", "body", "observed_at", "error"},
        )
        if set(response) not in allowed:
            raise EventCollectionError("raw response wrapper contains unexpected fields")
        if response["method"] not in {"GET", "POST"} or not isinstance(response["url"], str) or not str(response["url"]).startswith(("https://gamma-api.polymarket.com/", "https://clob.polymarket.com/")):
            raise EventCollectionError("raw response provenance is invalid")
        timestamp_keys = [key for key in ("observed_at", "retrieved_at") if key in response]
        if len(timestamp_keys) != 1:
            raise EventCollectionError("raw response timestamp schema is invalid")
        _parse_timestamp(response[timestamp_keys[0]], "raw response timestamp")
        if response["status"] is not None and (isinstance(response["status"], bool) or not isinstance(response["status"], int) or not 100 <= response["status"] <= 599):
            raise EventCollectionError("raw response status is invalid")
    if surface != {"coordinate_count": len(rows), "expected_coordinate_count": len(rows), "all_coordinates_complete": True}:
        raise EventCollectionError("surface completeness summary does not reconcile")
    return rows


def build_collection_metadata(
    observation: Mapping[str, object], slot: EventSlot, topology: FedPathConfig, *, run_created_at: datetime,
    actual_start_at: datetime, github: Mapping[str, object], provenance: Mapping[str, object],
    snapshot_stored_sha256: str, snapshot_uncompressed_sha256: str,
    snapshot_compressed_size: int, snapshot_uncompressed_size: int,
) -> dict[str, object]:
    completed = _parse_timestamp(observation.get("completed_at"), "observation completed_at")
    coordinates = validate_observation(observation, topology)
    quotes: list[dict[str, object]] = []
    observed_times: list[datetime] = []
    lifecycle = {"active": 0, "closed_pending_resolution": 0, "resolved": 0}
    for raw in coordinates:
        if not isinstance(raw, dict):
            raise EventCollectionError("observation coordinate must be an object")
        observed = _parse_timestamp(raw.get("observed_at"), "coordinate observed_at")
        observed_times.append(observed)
        status = raw.get("market_status")
        if status not in lifecycle:
            raise EventCollectionError("coordinate market_status is invalid")
        lifecycle[str(status)] += 1
        quotes.append({
            key: raw.get(key) for key in (
                "event_slug", "coordinate_kind", "meeting_date", "label", "raw_probability",
                "source", "quality", "market_status", "observed_at", "exchange_quote_timestamp",
                "exchange_quote_age_seconds", "exchange_timestamp_status", "liquidity", "best_bid",
                "best_ask", "spread", "diagnostic_codes",
            )
        } | {"client_retrieval_age_at_completion_seconds": max(0.0, (completed - observed).total_seconds())})
    ages = sorted(max(0.0, (completed - item).total_seconds()) for item in observed_times)
    middle = len(ages) // 2
    median = ages[middle] if len(ages) % 2 else (ages[middle - 1] + ages[middle]) / 2
    inference_error = abs((run_created_at.astimezone(timezone.utc) - slot.scheduled_at.astimezone(timezone.utc)).total_seconds())
    return {
        "schema_version": 1,
        "event_id": slot.meeting.event_id,
        "decision_date": slot.meeting.decision_date.isoformat(),
        "phase": slot.phase,
        "slot_index": slot.slot_index,
        "inferred_scheduled_slot_ny": slot.scheduled_at.isoformat(),
        "slot_inference_method": _INFERENCE_METHOD,
        "slot_inference_error_seconds": inference_error,
        "github_run_created_at": run_created_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "actual_start_at": actual_start_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "collection_completed_at": completed.isoformat().replace("+00:00", "Z"),
        "github": dict(github),
        "calendar": {
            "statement_time": slot.meeting.statement_time.isoformat(timespec="minutes"),
            "presser_time": slot.meeting.presser_time.isoformat(timespec="minutes"),
            "presser_end_time": slot.meeting.presser_end_time.isoformat(timespec="minutes"),
            "sep": slot.meeting.sep,
            "official_calendar_url": slot.meeting.official_calendar_url,
        },
        "provenance": {"collector_version": COLLECTOR_VERSION} | dict(provenance),
        "snapshot": {
            "fetched_at": observation.get("started_at"),
            "stored_sha256": snapshot_stored_sha256,
            "uncompressed_sha256": snapshot_uncompressed_sha256,
            "compressed_size_bytes": snapshot_compressed_size,
            "uncompressed_size_bytes": snapshot_uncompressed_size,
        },
        "quotes": quotes,
        "surface": {
            "coordinate_count": len(quotes),
            "all_coordinates_complete": observation.get("surface", {}).get("all_coordinates_complete") if isinstance(observation.get("surface"), dict) else False,
            "lifecycle_counts": lifecycle,
            "active_coordinate_count": lifecycle["active"],
            "closed_pending_resolution_coordinate_count": lifecycle["closed_pending_resolution"],
            "resolved_coordinate_count": lifecycle["resolved"],
            "maximum_client_retrieval_age_seconds": max(ages),
            "median_client_retrieval_age_seconds": median,
            "observation_timestamp_dispersion_seconds": (max(observed_times) - min(observed_times)).total_seconds(),
            "exchange_timestamp_status": "unavailable",
        },
    }


def _read_limited_gzip(data: bytes) -> bytes:
    with gzip.GzipFile(fileobj=io.BytesIO(data), mode="rb") as stream:
        result = stream.read(MAX_SNAPSHOT_BYTES + 1)
    if len(result) > MAX_SNAPSHOT_BYTES:
        raise EventCollectionError("snapshot exceeds the uncompressed size limit")
    return result


def verify_run_directory(
    path: Path, topology: FedPathConfig, *, expected_slot: EventSlot | None = None,
    expected_provenance: Mapping[str, object] | None = None,
) -> dict[str, object]:
    if path.is_symlink() or not path.is_dir():
        raise EventCollectionError("run path must be a regular directory")
    expected = {"collection.json", "market-observation.json.gz", "manifest.json"}
    actual = {item.name for item in path.iterdir()}
    if actual != expected or any(item.is_symlink() or not item.is_file() for item in path.iterdir()):
        raise EventCollectionError("run directory has an invalid file set")
    try:
        manifest = strict_json_loads((path / "manifest.json").read_bytes())
        metadata = strict_json_loads((path / "collection.json").read_bytes())
    except EventCollectionError as error:
        raise EventCollectionError("run JSON is invalid") from error
    if not isinstance(manifest, dict) or set(manifest) != {"schema_version", "files", "snapshot_uncompressed", "provenance"} or manifest["schema_version"] != 1:
        raise EventCollectionError("manifest schema is invalid")
    files = manifest["files"]
    if not isinstance(files, dict) or set(files) != {"collection.json", "market-observation.json.gz"}:
        raise EventCollectionError("manifest file set is invalid")
    for name in files:
        data = (path / name).read_bytes()
        row = files[name]
        if not isinstance(row, dict) or row != {"sha256": _sha(data), "size_bytes": len(data)}:
            raise EventCollectionError(f"manifest mismatch for {name}")
    compressed = (path / "market-observation.json.gz").read_bytes()
    if len(compressed) > MAX_COMPRESSED_BYTES:
        raise EventCollectionError("compressed snapshot exceeds size limit")
    uncompressed = _read_limited_gzip(compressed)
    expected_raw = manifest["snapshot_uncompressed"]
    if not isinstance(expected_raw, dict) or expected_raw != {"sha256": _sha(uncompressed), "size_bytes": len(uncompressed)}:
        raise EventCollectionError("uncompressed snapshot manifest mismatch")
    try:
        snapshot = strict_json_loads(uncompressed)
    except EventCollectionError as error:
        raise EventCollectionError("snapshot JSON is invalid") from error
    if not isinstance(snapshot, dict):
        raise EventCollectionError("snapshot must be an object")
    coordinates = validate_observation(snapshot, topology)
    if not isinstance(metadata, dict) or set(metadata) != {
        "schema_version", "event_id", "decision_date", "phase", "slot_index",
        "inferred_scheduled_slot_ny", "slot_inference_method", "slot_inference_error_seconds",
        "github_run_created_at", "actual_start_at", "collection_completed_at", "github",
        "calendar", "provenance", "snapshot", "quotes", "surface",
    } or metadata.get("schema_version") != 1:
        raise EventCollectionError("collection metadata schema is invalid")
    if metadata.get("phase") not in {"pre_action", "action_window", "pre_presser", "presser", "post_presser"}:
        raise EventCollectionError("collection phase is invalid")
    if not isinstance(metadata.get("slot_index"), int) or not 0 <= int(metadata["slot_index"]) < EXPECTED_SLOT_COUNT:
        raise EventCollectionError("collection slot index is invalid")
    for key in ("github_run_created_at", "actual_start_at", "collection_completed_at"):
        _parse_timestamp(metadata.get(key), key)
    github = metadata.get("github")
    if not isinstance(github, dict) or set(github) != {"repository", "workflow", "run_id", "run_attempt", "run_number", "head_sha", "ref", "event_name", "cron"}:
        raise EventCollectionError("GitHub provenance schema is invalid")
    if any(not isinstance(github.get(key), str) or not github[key] for key in ("repository", "workflow", "run_id", "head_sha", "ref", "event_name", "cron")) or any(
        isinstance(github.get(key), bool) or not isinstance(github.get(key), int) or int(github[key]) < 1 for key in ("run_attempt", "run_number")
    ) or re.fullmatch(r"[0-9a-f]{40}", str(github["head_sha"])) is None:
        raise EventCollectionError("GitHub provenance values are invalid")
    calendar = metadata.get("calendar")
    if not isinstance(calendar, dict) or set(calendar) != {"statement_time", "presser_time", "presser_end_time", "sep", "official_calendar_url"} or (
        calendar.get("statement_time"), calendar.get("presser_time"), calendar.get("presser_end_time")
    ) != ("14:00", "14:30", "15:30") or not isinstance(calendar.get("sep"), bool) or not str(calendar.get("official_calendar_url", "")).startswith("https://www.federalreserve.gov/"):
        raise EventCollectionError("collection calendar schema is invalid")
    provenance = metadata.get("provenance")
    if not isinstance(provenance, dict) or set(provenance) != {
        "collector_version", "calendar_sha256", "markets_config_sha256", "fed_path_config_sha256", "code_commit_sha",
    }:
        raise EventCollectionError("collection provenance schema is invalid")
    if provenance.get("collector_version") != COLLECTOR_VERSION or any(
        not isinstance(provenance.get(key), str) or re.fullmatch(r"[0-9a-f]{64}", str(provenance[key])) is None
        for key in ("calendar_sha256", "markets_config_sha256", "fed_path_config_sha256")
    ) or not isinstance(provenance.get("code_commit_sha"), str) or re.fullmatch(r"[0-9a-f]{40}", str(provenance["code_commit_sha"])) is None:
        raise EventCollectionError("collection provenance hashes are invalid")
    if snapshot.get("markets_config_sha256") != provenance.get("markets_config_sha256"):
        raise EventCollectionError("snapshot market configuration digest does not reconcile")
    if expected_provenance is not None and any(provenance.get(key) != value for key, value in expected_provenance.items()):
        raise EventCollectionError("artifact provenance does not match the supplied configuration")
    if metadata.get("provenance") != manifest.get("provenance"):
        raise EventCollectionError("manifest provenance does not reconcile")
    snapshot_meta = metadata.get("snapshot")
    if not isinstance(snapshot_meta, dict) or set(snapshot_meta) != {
        "fetched_at", "stored_sha256", "uncompressed_sha256", "compressed_size_bytes", "uncompressed_size_bytes",
    } or snapshot_meta.get("stored_sha256") != _sha(compressed) or snapshot_meta.get("uncompressed_sha256") != _sha(uncompressed) or snapshot_meta.get("compressed_size_bytes") != len(compressed) or snapshot_meta.get("uncompressed_size_bytes") != len(uncompressed):
        raise EventCollectionError("collection snapshot hashes do not reconcile")
    _parse_timestamp(snapshot_meta.get("fetched_at"), "snapshot fetched_at")
    if github["head_sha"] != provenance["code_commit_sha"]:
        raise EventCollectionError("code commit provenance does not reconcile")
    if expected_slot is not None:
        expected_fields = {
            "event_id": expected_slot.meeting.event_id,
            "decision_date": expected_slot.meeting.decision_date.isoformat(),
            "phase": expected_slot.phase,
            "slot_index": expected_slot.slot_index,
            "inferred_scheduled_slot_ny": expected_slot.scheduled_at.isoformat(),
        }
        if any(metadata.get(key) != value for key, value in expected_fields.items()):
            raise EventCollectionError("collection slot metadata does not match the calendar")
        expected_calendar = {
            "statement_time": expected_slot.meeting.statement_time.isoformat(timespec="minutes"),
            "presser_time": expected_slot.meeting.presser_time.isoformat(timespec="minutes"),
            "presser_end_time": expected_slot.meeting.presser_end_time.isoformat(timespec="minutes"),
            "sep": expected_slot.meeting.sep,
            "official_calendar_url": expected_slot.meeting.official_calendar_url,
        }
        if metadata.get("calendar") != expected_calendar or metadata.get("slot_inference_method") != _INFERENCE_METHOD:
            raise EventCollectionError("collection calendar provenance does not match the reviewed event")
        run_created = _parse_timestamp(metadata.get("github_run_created_at"), "github_run_created_at")
        actual_start = _parse_timestamp(metadata.get("actual_start_at"), "actual_start_at")
        inference_error = abs((run_created - expected_slot.scheduled_at.astimezone(timezone.utc)).total_seconds())
        runner_lateness = (actual_start - expected_slot.scheduled_at.astimezone(timezone.utc)).total_seconds()
        if metadata.get("slot_inference_error_seconds") != inference_error or inference_error > 150 or not -5 <= runner_lateness <= 720:
            raise EventCollectionError("collection scheduler timing does not reconcile")
    quotes = metadata.get("quotes")
    if not isinstance(quotes, list) or len(quotes) != len(coordinates):
        raise EventCollectionError("collection quotes do not reconcile")
    for quote, coordinate in zip(quotes, coordinates, strict=True):
        if not isinstance(quote, dict) or set(quote) != _QUOTE_KEYS or any(quote.get(key) != coordinate.get(key) for key in (
            "event_slug", "coordinate_kind", "meeting_date", "label", "raw_probability", "source",
            "quality", "market_status", "observed_at", "exchange_quote_timestamp",
            "exchange_quote_age_seconds", "exchange_timestamp_status", "liquidity", "best_bid",
            "best_ask", "spread", "diagnostic_codes",
        )):
            raise EventCollectionError("collection quote projection does not reconcile")
    surface = metadata.get("surface")
    if not isinstance(surface, dict) or set(surface) != {
        "coordinate_count", "all_coordinates_complete", "lifecycle_counts", "active_coordinate_count",
        "closed_pending_resolution_coordinate_count", "resolved_coordinate_count", "maximum_client_retrieval_age_seconds",
        "median_client_retrieval_age_seconds", "observation_timestamp_dispersion_seconds", "exchange_timestamp_status",
    } or surface.get("coordinate_count") != len(coordinates) or surface.get("all_coordinates_complete") is not True or surface.get("exchange_timestamp_status") != "unavailable" or sum(int(surface.get(key, -1)) for key in (
        "active_coordinate_count", "closed_pending_resolution_coordinate_count", "resolved_coordinate_count",
    )) != len(coordinates):
        raise EventCollectionError("collection lifecycle counts do not reconcile")
    expected_lifecycle = {
        "active": int(surface["active_coordinate_count"]),
        "closed_pending_resolution": int(surface["closed_pending_resolution_coordinate_count"]),
        "resolved": int(surface["resolved_coordinate_count"]),
    }
    if surface.get("lifecycle_counts") != expected_lifecycle:
        raise EventCollectionError("collection lifecycle map does not reconcile")
    completed = _parse_timestamp(snapshot.get("completed_at"), "snapshot completed_at")
    started = _parse_timestamp(snapshot.get("started_at"), "snapshot started_at")
    if metadata.get("collection_completed_at") != snapshot.get("completed_at") or snapshot_meta.get("fetched_at") != snapshot.get("started_at"):
        raise EventCollectionError("collection observation timestamps do not reconcile")
    observed = [_parse_timestamp(item["observed_at"], "coordinate observed_at") for item in coordinates]
    if any(item < started or item > completed for item in observed):
        raise EventCollectionError("coordinate timestamps are outside the observation window")
    ages = sorted(max(0.0, (completed - item).total_seconds()) for item in observed)
    middle = len(ages) // 2
    median = ages[middle] if len(ages) % 2 else (ages[middle - 1] + ages[middle]) / 2
    expected_timing = {
        "maximum_client_retrieval_age_seconds": max(ages),
        "median_client_retrieval_age_seconds": median,
        "observation_timestamp_dispersion_seconds": (max(observed) - min(observed)).total_seconds(),
    }
    if any(surface.get(key) != value for key, value in expected_timing.items()):
        raise EventCollectionError("collection timing summary does not reconcile")
    for quote, age in zip(quotes, (max(0.0, (completed - item).total_seconds()) for item in observed), strict=True):
        if quote.get("client_retrieval_age_at_completion_seconds") != age:
            raise EventCollectionError("quote retrieval age does not reconcile")
    return metadata


def _tree_size(path: Path) -> int:
    total = 0
    if path.exists():
        for item in path.rglob("*"):
            if item.is_symlink():
                raise EventCollectionError("archive must not contain symlinks")
            if item.is_file():
                total += item.stat().st_size
    return total


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def archive_observation(
    root: Path, observation: Mapping[str, object], slot: EventSlot, topology: FedPathConfig, *, run_id: str,
    run_created_at: datetime, actual_start_at: datetime, github: Mapping[str, object],
    provenance: Mapping[str, object],
) -> ArchiveResult:
    if _RUN_ID.fullmatch(run_id) is None:
        raise EventCollectionError("run_id is invalid")
    event_root = root / slot.meeting.event_id
    pointer = event_root / "slots" / f"{slot.slot_key}.json"
    if pointer.exists():
        try:
            existing = strict_json_loads(pointer.read_bytes())
            if not isinstance(existing, dict):
                raise EventCollectionError("existing pointer must be an object")
            relative = existing["run_path"]
            if not isinstance(relative, str) or relative.startswith("/") or ".." in Path(relative).parts:
                raise EventCollectionError("existing pointer path is invalid")
            run = event_root / relative
            metadata = verify_run_directory(run, topology, expected_slot=slot, expected_provenance=provenance)
            if metadata.get("event_id") != slot.meeting.event_id or metadata.get("inferred_scheduled_slot_ny") != slot.scheduled_at.isoformat():
                raise EventCollectionError("existing pointer targets a different slot")
        except (OSError, KeyError, EventCollectionError) as error:
            raise EventCollectionError("existing slot pointer is invalid") from error
        return ArchiveResult("duplicate_same_slot", run, pointer)
    snapshot_raw = json_bytes(dict(observation))
    if len(snapshot_raw) > MAX_SNAPSHOT_BYTES:
        raise EventCollectionError("snapshot exceeds size limit")
    snapshot_gzip = deterministic_gzip(snapshot_raw)
    if len(snapshot_gzip) > MAX_COMPRESSED_BYTES:
        raise EventCollectionError("compressed snapshot exceeds size limit")
    metadata = build_collection_metadata(
        observation, slot, topology, run_created_at=run_created_at, actual_start_at=actual_start_at,
        github=github, provenance=provenance, snapshot_stored_sha256=_sha(snapshot_gzip),
        snapshot_uncompressed_sha256=_sha(snapshot_raw), snapshot_compressed_size=len(snapshot_gzip),
        snapshot_uncompressed_size=len(snapshot_raw),
    )
    metadata_raw = json_bytes(metadata)
    if len(metadata_raw) > MAX_METADATA_BYTES:
        raise EventCollectionError("collection metadata exceeds size limit")
    manifest = {
        "schema_version": 1,
        "files": {
            "collection.json": {"sha256": _sha(metadata_raw), "size_bytes": len(metadata_raw)},
            "market-observation.json.gz": {"sha256": _sha(snapshot_gzip), "size_bytes": len(snapshot_gzip)},
        },
        "snapshot_uncompressed": {"sha256": _sha(snapshot_raw), "size_bytes": len(snapshot_raw)},
        "provenance": metadata["provenance"],
    }
    manifest_raw = json_bytes(manifest)
    added = len(metadata_raw) + len(snapshot_gzip) + len(manifest_raw)
    if _tree_size(event_root) + added > MAX_EVENT_BYTES:
        raise EventCollectionError("event archive exceeds size limit")
    runs = event_root / "runs"
    slots = event_root / "slots"
    runs.mkdir(parents=True, exist_ok=True)
    slots.mkdir(parents=True, exist_ok=True)
    run_dir = runs / run_id
    if run_dir.exists():
        raise EventCollectionError("run_id already exists")
    staging = event_root / f".staging-{run_id}-{secrets.token_hex(4)}"
    staging.mkdir(mode=0o700)
    try:
        for name, data in (
            ("collection.json", metadata_raw),
            ("market-observation.json.gz", snapshot_gzip),
            ("manifest.json", manifest_raw),
        ):
            target = staging / name
            with target.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        verify_run_directory(staging, topology, expected_slot=slot, expected_provenance=provenance)
        os.replace(staging, run_dir)
        _fsync_directory(runs)
        pointer_payload = json_bytes({
            "schema_version": 1,
            "event_id": slot.meeting.event_id,
            "slot": slot.scheduled_at.isoformat(),
            "run_path": f"runs/{run_id}",
            "manifest_sha256": _sha(manifest_raw),
        })
        temporary = slots / f".{slot.slot_key}.{secrets.token_hex(4)}.tmp"
        with temporary.open("xb") as stream:
            stream.write(pointer_payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # Atomic first-write-wins publication: hard-link creation fails if
            # another writer has already claimed the canonical slot.
            os.link(temporary, pointer)
        except FileExistsError:
            temporary.unlink()
            shutil.rmtree(run_dir)
            return archive_observation(root, observation, slot, topology, run_id=run_id, run_created_at=run_created_at, actual_start_at=actual_start_at, github=github, provenance=provenance)
        temporary.unlink()
        _fsync_directory(slots)
        return ArchiveResult("captured", run_dir, pointer)
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        if run_dir.exists() and not pointer.exists():
            shutil.rmtree(run_dir)
        raise


def audit_archive(
    root: Path, event_id: str, topology: FedPathConfig, calendar: EventCalendar, *,
    expected_provenance: Mapping[str, object] | None = None,
) -> dict[str, object]:
    event_root = root / event_id
    if event_root.is_symlink() or not event_root.is_dir():
        raise EventCollectionError("event archive must be a regular directory")
    if {item.name for item in event_root.iterdir()} != {"runs", "slots"}:
        raise EventCollectionError("event archive contains unexpected paths")
    if any(item.is_symlink() or not item.is_dir() for item in (event_root / "runs", event_root / "slots")):
        raise EventCollectionError("event archive containers are invalid")
    slot_entries = list((event_root / "slots").iterdir())
    if any(item.is_symlink() or not item.is_file() or item.suffix != ".json" or re.fullmatch(r"\d{4}\.json", item.name) is None for item in slot_entries):
        raise EventCollectionError("slot directory contains an unexpected entry")
    pointers = sorted(slot_entries)
    captured: list[str] = []
    referenced_runs: set[str] = set()
    meeting = next((item for item in calendar.meetings if item.event_id == event_id), None)
    if meeting is None:
        raise EventCollectionError("event archive is absent from the reviewed calendar")
    slots_by_key = {item.slot_key: item for item in event_slots(calendar, meeting)}
    for pointer in pointers:
        if pointer.is_symlink():
            raise EventCollectionError("slot pointer must not be a symlink")
        if pointer.stem not in slots_by_key:
            raise EventCollectionError("slot pointer is outside the canonical event window")
        payload = strict_json_loads(pointer.read_bytes())
        if not isinstance(payload, dict):
            raise EventCollectionError("slot pointer must be an object")
        if set(payload) != {"schema_version", "event_id", "slot", "run_path", "manifest_sha256"} or payload.get("schema_version") != 1 or payload.get("event_id") != event_id:
            raise EventCollectionError("slot pointer schema is invalid")
        run_path = payload.get("run_path")
        if not isinstance(run_path, str) or run_path.startswith("/") or ".." in Path(run_path).parts:
            raise EventCollectionError("slot pointer path is invalid")
        if run_path in referenced_runs:
            raise EventCollectionError("multiple slots target the same run")
        referenced_runs.add(run_path)
        run_directory = event_root / run_path
        metadata = verify_run_directory(
            run_directory, topology, expected_slot=slots_by_key[pointer.stem],
            expected_provenance=expected_provenance,
        )
        manifest_sha = _sha((run_directory / "manifest.json").read_bytes())
        if payload.get("manifest_sha256") != manifest_sha or metadata.get("event_id") != event_id:
            raise EventCollectionError("slot pointer provenance does not reconcile")
        try:
            pointer_slot = datetime.fromisoformat(str(payload.get("slot")))
        except ValueError as error:
            raise EventCollectionError("slot pointer timestamp is invalid") from error
        if pointer_slot.strftime("%H%M") != pointer.stem or metadata.get("inferred_scheduled_slot_ny") != payload.get("slot"):
            raise EventCollectionError("slot pointer identity does not reconcile")
        captured.append(pointer.stem)
    actual_runs = {f"runs/{item.name}" for item in (event_root / "runs").iterdir() if item.is_dir() and not item.is_symlink()}
    if actual_runs != referenced_runs or any(not item.is_dir() or item.is_symlink() for item in (event_root / "runs").iterdir()):
        raise EventCollectionError("event archive contains orphaned or invalid runs")
    expected = [
        (datetime(2000, 1, 1, 13, 55) + index * timedelta(minutes=5)).strftime("%H%M")
        for index in range(EXPECTED_SLOT_COUNT)
    ]
    return {
        "schema_version": 1,
        "event_id": event_id,
        "captured_slots": captured,
        "missing_slots": [item for item in expected if item not in captured],
        "captured_count": len(captured),
        "expected_count": EXPECTED_SLOT_COUNT,
        "size_bytes": _tree_size(event_root),
        "manifest_status": "verified",
    }
