#!/usr/bin/env python3
"""Build the static GitHub Pages data bundle from verified forecast runs."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import shutil
from datetime import datetime, timezone
from itertools import product
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT_ROOT / "site"
ACTION_BUCKETS_BP = (-50.0, -25.0, 0.0, 25.0, 50.0)
ACTION_SCORES = (-1.0, -0.5, 0.0, 0.5, 1.0)
REPLAY_WINDOW_DAYS = 183


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected an object in {path}")
    return value


def _latest_run(root: Path) -> Path:
    pointer = _read_json(root / "latest.json")
    run_path = pointer.get("run_path")
    if not isinstance(run_path, str):
        raise ValueError(f"invalid latest pointer in {root}")
    candidate = root / run_path
    if not candidate.is_dir() or candidate.parent != root / "runs":
        raise ValueError(f"unsafe latest run in {root}")
    return candidate


def _optional_latest(root: Path, filename: str) -> tuple[Path | None, dict[str, Any] | None]:
    try:
        run = _latest_run(root)
        return run, _read_json(run / filename)
    except (FileNotFoundError, ValueError, json.JSONDecodeError):
        return None, None


def _event_activity(snapshot: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("events"), dict):
        return {}

    def finite_number(value: object) -> float | None:
        if isinstance(value, bool):
            return None
        try:
            result = float(value)
        except (TypeError, ValueError):
            return None
        return result if math.isfinite(result) and result >= 0 else None

    observed_at = snapshot.get("fetched_at")
    output: dict[str, dict[str, Any]] = {}
    for slug, event in snapshot["events"].items():
        if not isinstance(slug, str) or not isinstance(event, dict):
            continue
        values = {
            "volume_24h": finite_number(event.get("volume24hrClob", event.get("volume24hr"))),
            "volume_total": finite_number(event.get("volumeClob", event.get("volume"))),
            "liquidity": finite_number(event.get("liquidityClob", event.get("liquidity"))),
        }
        activity = {key: value for key, value in values.items() if value is not None}
        if activity:
            if isinstance(observed_at, str):
                activity["as_of"] = observed_at
            output[slug] = activity
    return output


def _compact_meetings(
    payload: dict[str, Any], activity_by_slug: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    activity_by_slug = activity_by_slug or {}
    meetings = []
    quality_rows = payload.get("price_quality", [])
    for item in payload.get("meetings", []):
        meeting = {
            "date": item["date"],
            "event_slug": item["event_slug"],
            "expected_change_bp": item["expected_change_bp"],
            "expected_target_upper_before": item["expected_target_upper_before"],
            "expected_target_upper_after": item["expected_target_upper_after"],
            "raw_total": item["raw_total"],
            "down": item["decrease_probability"],
            "unchanged": item["no_change_probability"],
            "up": item["increase_probability"],
            "prices": [
                {
                    "label": price["label"],
                    "probability": price["probability"],
                    "raw_probability": price["raw_probability"],
                    "representative_bp": price["representative_bp"],
                }
                for price in item["prices"]
            ],
            "actions": {
                category["category"]: category["conditional_change_bp"]
                for category in item["categories"]
            },
        }
        activity = activity_by_slug.get(str(item["event_slug"]))
        if activity:
            meeting["activity"] = activity
        matching_quality = [
            row for row in quality_rows
            if isinstance(row, dict) and row.get("source_id") == item["event_slug"]
        ] if isinstance(quality_rows, list) else []
        if matching_quality:
            sources = {str(row.get("source")) for row in matching_quality}
            qualities = {str(row.get("quality")) for row in matching_quality}
            stamps = [str(row.get("retrieved_at")) for row in matching_quality if row.get("retrieved_at")]
            meeting["quote_quality"] = {
                "source": next(iter(sources)) if len(sources) == 1 else "mixed",
                "quality": "good" if qualities == {"good"} else "degraded",
                "as_of": max(stamps) if stamps else payload.get("snapshot_fetched_at"),
                "max_spread": max(
                    (float(row["spread"]) for row in matching_quality if isinstance(row.get("spread"), (int, float))),
                    default=None,
                ),
            }
        meetings.append(meeting)
    return meetings


def _compact_tree(payload: dict[str, Any]) -> dict[str, Any]:
    tree = payload["conditional_tree"]
    return {
        "root": tree["root_node_id"],
        "node_count": tree["node_count"],
        "leaf_count": tree["leaf_count"],
        "quoted_marginals_preserved": tree["quoted_marginals_preserved"],
        "settings": tree["settings"],
        "nodes": [
            {
                "id": node["node_id"],
                "depth": node["depth"],
                "path": node["realized_path"],
                "probability": node["path_probability"],
                "rate": node["representative_target_upper"],
                "action_rate": node.get("action_implied_target_upper", node["representative_target_upper"]),
                "next_date": node["next_meeting_date"],
                "next": node["next_probabilities"],
                "branches": node["branches"],
            }
            for node in tree["nodes"]
        ],
    }


def _ensure_action_rates(policy: dict[str, Any]) -> dict[str, Any]:
    """Upgrade archived compact trees without hiding a terminal-date action."""
    tree = policy.get("tree")
    meetings = policy.get("meetings")
    terminal = policy.get("terminal_anchor")
    if not isinstance(tree, dict) or not isinstance(meetings, list) or not isinstance(terminal, dict):
        return policy
    nodes = tree.get("nodes")
    if not isinstance(nodes, list):
        return policy
    by_id = {str(node.get("id")): node for node in nodes if isinstance(node, dict)}
    terminal_date = str(terminal.get("date"))
    baseline = float(policy["target_upper_bound_baseline"])
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node.setdefault("action_rate", node.get("rate"))
        depth = int(node.get("depth", 0))
        if not depth or depth > len(meetings) or str(meetings[depth - 1].get("date")) != terminal_date:
            continue
        cursor = by_id.get(str(tree.get("root", "root")))
        action_bp = 0.0
        for category in node.get("path", []):
            if not isinstance(cursor, dict):
                break
            branch = next(
                (item for item in cursor.get("branches", []) if item.get("category") == category),
                None,
            )
            if not isinstance(branch, dict):
                break
            action_bp += float(branch["representative_action_bp"])
            cursor = by_id.get(str(branch.get("child_node_id")))
        else:
            node["action_rate"] = baseline + action_bp / 100.0
    return policy


def _compact_policy(
    payload: dict[str, Any], activity_by_slug: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return _ensure_action_rates({
        "target_upper_bound_baseline": payload["target_upper_bound_baseline"],
        "effective_rate_baseline": payload["effective_rate_baseline"],
        "meetings": _compact_meetings(payload, activity_by_slug),
        "terminal_anchor": payload["terminal_anchor"],
        "tree": _compact_tree(payload),
        "historical_diagnostic": _compact_historical_diagnostic(payload.get("historical_transition_diagnostic")),
        "source_urls": payload["source_urls"],
    })


def _compact_historical_diagnostic(value: object) -> dict[str, Any]:
    """Publish support and gate status without local paths or runtime fingerprints."""
    if not isinstance(value, dict):
        return {"status": "unavailable", "active_in_tree": False}
    allowed = (
        "status", "active_in_tree", "source", "training_count", "row_counts",
        "gate_failures", "timing_destination_identification", "interpretation",
    )
    return {key: value[key] for key in allowed if key in value}


def _attach_activity(
    policy: dict[str, Any], activity_by_slug: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    meetings = []
    for item in policy.get("meetings", []):
        meeting = dict(item)
        activity = activity_by_slug.get(str(meeting.get("event_slug", "")))
        if activity:
            meeting["activity"] = activity
        meetings.append(meeting)
    return {**policy, "meetings": meetings}


def _vintage_key(stamp: str, suffix: str = "") -> str:
    compact = re.sub(r"[^0-9A-Za-z]+", "", stamp)
    return f"{compact}{('-' + suffix) if suffix else ''}"


def _weighted_quantile(points: list[tuple[float, float]], quantile: float) -> float:
    ordered = sorted(points)
    total = sum(weight for _, weight in ordered)
    cumulative = 0.0
    for value, weight in ordered:
        cumulative += weight / total
        if cumulative >= quantile - 1e-12:
            return value
    return ordered[-1][0]


def _rate_summary(points: list[tuple[float, float]]) -> dict[str, float]:
    total = sum(weight for _, weight in points)
    normalized = [(value, weight / total) for value, weight in points]
    return {
        "q05": _weighted_quantile(normalized, 0.05),
        "q25": _weighted_quantile(normalized, 0.25),
        "q50": _weighted_quantile(normalized, 0.50),
        "q75": _weighted_quantile(normalized, 0.75),
        "q95": _weighted_quantile(normalized, 0.95),
        "mean": sum(value * weight for value, weight in normalized),
    }


def _five_bucket_path(
    *,
    vintage_at: str,
    baseline: float,
    meetings: list[dict[str, Any]],
    terminal: dict[str, Any],
    terminal_rates: dict[str, float],
    terminal_date: str,
    settings: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return a five-state marginal-preserving forward rate fan.

    The quoted five-bucket meeting and terminal marginals remain exact. A
    persistent stance kernel plus terminal consistency supplies cross-meeting
    dependence, and iterative proportional fitting restores every marginal.
    """
    meeting_dates = [str(item["date"]) for item in meetings]
    meeting_probabilities = [tuple(float(price["probability"]) for price in item["prices"]) for item in meetings]
    labels = list(terminal_rates)
    terminal_probabilities = tuple(float(terminal["probabilities"][label]) for label in labels)
    rates = tuple(terminal_rates[label] for label in labels)
    paths = list(product(range(5), repeat=len(meetings)))
    states = [(path, terminal_index) for path in paths for terminal_index in range(len(rates))]
    strength = float(settings["dependence_strength"])
    decay = float(settings["dependence_decay"])
    sigma = float(settings["terminal_consistency_sigma_bp"])
    log_kernels = []
    for path, terminal_index in states:
        persistence = sum(
            decay ** (right - left - 1) * ACTION_SCORES[path[left]] * ACTION_SCORES[path[right]]
            for left in range(len(path))
            for right in range(left + 1, len(path))
        )
        cumulative = sum(ACTION_BUCKETS_BP[outcome] for index, outcome in enumerate(path) if meeting_dates[index] <= terminal_date)
        gap_bp = 100.0 * (baseline + cumulative / 100.0 - rates[terminal_index])
        log_kernels.append(strength * persistence - 0.5 * (gap_bp / sigma) ** 2)
    maximum = max(log_kernels)
    weights = []
    for (path, terminal_index), log_kernel in zip(states, log_kernels, strict=True):
        marginal = terminal_probabilities[terminal_index]
        for index, outcome in enumerate(path):
            marginal *= meeting_probabilities[index][outcome]
        weights.append(marginal * math.exp(max(-700.0, log_kernel - maximum)))
    total = sum(weights)
    if total <= 0:
        raise ValueError("five-bucket path has zero support")
    weights = [value / total for value in weights]
    targets = meeting_probabilities + [terminal_probabilities]
    tolerance = float(settings["rake_tolerance"])
    for _ in range(int(settings["rake_max_iterations"])):
        for dimension, target in enumerate(targets):
            size = 5 if dimension < len(meetings) else len(rates)
            totals = [0.0] * size
            for state_index, (path, terminal_index) in enumerate(states):
                category = path[dimension] if dimension < len(meetings) else terminal_index
                totals[category] += weights[state_index]
            factors = [target[index] / totals[index] if totals[index] else 0.0 for index in range(size)]
            for state_index, (path, terminal_index) in enumerate(states):
                category = path[dimension] if dimension < len(meetings) else terminal_index
                weights[state_index] *= factors[category]
        maximum_error = 0.0
        for dimension, target in enumerate(targets):
            size = 5 if dimension < len(meetings) else len(rates)
            totals = [0.0] * size
            for state_index, (path, terminal_index) in enumerate(states):
                category = path[dimension] if dimension < len(meetings) else terminal_index
                totals[category] += weights[state_index]
            maximum_error = max(maximum_error, *(abs(totals[index] - target[index]) for index in range(size)))
        if maximum_error <= tolerance:
            break
    else:
        raise ValueError(f"five-bucket path raking did not converge: {maximum_error:.3g}")

    vintage_date = datetime.fromisoformat(vintage_at.replace("Z", "+00:00")).astimezone(ZoneInfo("America/New_York")).date().isoformat()
    output = [{"date": vintage_date, "kind": "vintage", **_rate_summary([(baseline, 1.0)])}]
    for horizon, meeting_date in enumerate(meeting_dates):
        distribution: dict[float, float] = {}
        for weight, (path, terminal_index) in zip(weights, states, strict=True):
            if meeting_date < terminal_date:
                value = baseline + sum(ACTION_BUCKETS_BP[path[index]] for index in range(horizon + 1)) / 100.0
            elif meeting_date == terminal_date:
                value = rates[terminal_index]
            else:
                after_terminal = sum(
                    ACTION_BUCKETS_BP[path[index]]
                    for index in range(horizon + 1)
                    if meeting_dates[index] > terminal_date
                )
                value = rates[terminal_index] + after_terminal / 100.0
            distribution[value] = distribution.get(value, 0.0) + weight
        output.append({"date": meeting_date, "kind": "meeting", **_rate_summary(list(distribution.items()))})
    if terminal_date not in meeting_dates:
        output.append({"date": terminal_date, "kind": "terminal", **_rate_summary(list(zip(rates, terminal_probabilities, strict=True)))})
    return sorted(output, key=lambda item: (item["date"], item["kind"]))


def _compact_replay_meetings(meetings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for item in meetings:
        meeting = {
            "date": item["date"],
            "event_slug": item["event_slug"],
            "probabilities": {price["label"]: price["probability"] for price in item["prices"]},
        }
        if isinstance(item.get("activity"), dict):
            meeting["activity"] = item["activity"]
        if isinstance(item.get("quote_quality"), dict):
            meeting["quote_quality"] = item["quote_quality"]
        if isinstance(item.get("raw_total"), (int, float)):
            meeting["raw_total"] = item["raw_total"]
        for key in ("carried_forward_from", "quote_status", "source_timestamp"):
            if item.get(key) is not None:
                meeting[key] = item[key]
        output.append(meeting)
    return output


def _replay_vintage(
    *,
    payload: dict[str, Any],
    kind: str,
    label: str,
    event: str | None,
    terminal_rates: dict[str, float],
    terminal_date: str,
    settings: dict[str, Any],
) -> dict[str, Any]:
    meetings = payload["meeting_distributions"] if "meeting_distributions" in payload else payload["meetings"]
    terminal = payload["terminal"] if "terminal" in payload else payload["terminal_anchor"]
    baseline = float(payload["target_upper_bound_baseline"])
    return {
        "generated_at": payload["generated_at"],
        "kind": kind,
        "model_version": "structural-terminal-ipf-v1",
        "label": label,
        "event": event,
        "baseline_target_upper": baseline,
        "horizon": _five_bucket_path(
            vintage_at=payload["generated_at"],
            baseline=baseline,
            meetings=meetings,
            terminal=terminal,
            terminal_rates=terminal_rates,
            terminal_date=terminal_date,
            settings=settings,
        ),
        "meetings": _compact_replay_meetings(meetings),
    }


def _replay_preference(item: dict[str, Any]) -> tuple[int, int, int, str]:
    """Rank same-day replay candidates by completeness, then timestamp."""
    kind = str(item.get("kind", ""))
    full_tree = 1 if kind == "full_tree" else 0
    kind_rank = {"historical_daily": 1, "daily": 2, "full_tree": 3}.get(kind, 0)
    return full_tree, len(item.get("meetings", [])), kind_rank, str(item.get("generated_at", ""))


def _annotate_historical_vintage(candidate: dict[str, Any]) -> dict[str, Any]:
    """Attach the information-set evidence used by the public replay."""
    generated = _timestamp_number(str(candidate["generated_at"]))
    meetings = []
    for source in candidate.get("meetings", []):
        meeting = dict(source)
        source_timestamp = meeting.get("source_timestamp")
        if not isinstance(source_timestamp, (int, float)):
            raise ValueError("historical meeting lacks a source timestamp")
        age = generated - float(source_timestamp)
        if age < -1:
            raise ValueError("historical quote postdates its replay frame")
        meeting["quote_status"] = "reconstructed_daily"
        meeting["source_kind"] = "polymarket_clob_price_history"
        meeting["quote_age_seconds"] = max(0.0, age)
        meetings.append(meeting)
    has_terminal = any(
        isinstance(point, dict) and point.get("kind") == "terminal"
        for point in candidate.get("horizon", [])
    )
    return {
        **candidate,
        "meetings": meetings,
        "model_version": (
            "historical-native-terminal-v1" if has_terminal
            else "historical-native-meeting-v1"
        ),
    }


def _forecast_replay(
    output: Path, meeting: dict[str, Any], meeting_activity: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    fed_config = _read_json(PROJECT_ROOT / "config" / "fed_path.json")
    scenario_config = _read_json(PROJECT_ROOT / "config" / "markets.json")
    terminal_rates = {item["label"]: float(item["representative_rate"]) for item in fed_config["terminal"]["buckets"]}
    terminal_date = str(scenario_config["terminal_date"])
    settings = scenario_config["conditional_tree"]
    replay_path = output / "data" / "forecast-replay.json"
    prior: dict[str, Any] = {}
    if replay_path.exists():
        try:
            previous = _read_json(replay_path)
            prior = {item["generated_at"]: item for item in previous.get("vintages", [])}
        except (ValueError, json.JSONDecodeError):
            prior = {}
    candidates = {
        stamp: item for stamp, item in prior.items()
        if item.get("kind") != "event_checkpoint"
    }
    historical_path = PROJECT_ROOT / "data" / "historical-policy-replay.json"
    historical_disclosure = None
    if historical_path.exists():
        try:
            historical = _read_json(historical_path)
            historical_disclosure = historical.get("disclosure")
            candidates = {stamp: item for stamp, item in candidates.items() if item.get("kind") != "historical_daily"}
            for candidate in historical.get("vintages", []):
                if isinstance(candidate, dict) and isinstance(candidate.get("generated_at"), str):
                    candidates[candidate["generated_at"]] = _annotate_historical_vintage(candidate)
        except (ValueError, TypeError, json.JSONDecodeError):
            pass
    fed_payloads: list[dict[str, Any]] = []
    for path in sorted((PROJECT_ROOT / "outputs" / "fed-path-history" / "runs").glob("*/fed_path.json")):
        try:
            payload = _read_json(path)
            if not payload.get("meeting_distributions") or not payload.get("terminal"):
                continue
            fed_payloads.append(payload)
            candidate = _replay_vintage(
                payload=payload,
                kind="daily",
                label="Daily market observation",
                event=None,
                terminal_rates=terminal_rates,
                terminal_date=terminal_date,
                settings=settings,
            )
            candidates[candidate["generated_at"]] = candidate
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    for path in sorted((output / "data" / "vintages").glob("*.json")):
        try:
            archived = _read_json(path)
            if archived.get("kind") == "event_checkpoint":
                continue
            policy = archived["policy"]
            normalized = {
                "generated_at": archived["generated_at"],
                "target_upper_bound_baseline": policy["target_upper_bound_baseline"],
                "meetings": policy["meetings"],
                "terminal_anchor": policy.get("terminal_anchor"),
            }
            if normalized["terminal_anchor"] is None:
                stamp = str(archived["generated_at"])
                eligible = [item for item in fed_payloads if str(item["generated_at"]) <= stamp]
                if not eligible:
                    continue
                normalized["terminal_anchor"] = eligible[-1]["terminal"]
            candidate = _replay_vintage(
                payload=normalized,
                kind=str(archived["kind"]),
                label=str(archived["label"]),
                event=archived.get("event"),
                terminal_rates=terminal_rates,
                terminal_date=terminal_date,
                settings=settings,
            )
            candidates[candidate["generated_at"]] = candidate
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    current_meeting = {**meeting, "meetings": _compact_meetings(meeting, meeting_activity)}
    candidates[meeting["generated_at"]] = _replay_vintage(
        payload=current_meeting,
        kind="full_tree",
        label="Current full-market forecast",
        event=None,
        terminal_rates=terminal_rates,
        terminal_date=terminal_date,
        settings=settings,
    )
    ordered = sorted(candidates.values(), key=lambda item: item["generated_at"])
    by_day: dict[str, dict[str, Any]] = {}
    retained = []
    for item in ordered:
        day = datetime.fromisoformat(item["generated_at"].replace("Z", "+00:00")).astimezone(ZoneInfo("America/New_York")).date().isoformat()
        if item["kind"] == "event_checkpoint":
            retained.append(item)
        else:
            incumbent = by_day.get(day)
            # Prefer a richer historical meeting surface to a thinner two-date
            # daily adapter, but always retain a same-day full conditional tree.
            if incumbent is None or _replay_preference(item) > _replay_preference(incumbent):
                by_day[day] = item
    retained.extend(by_day.values())
    retained = sorted(retained, key=lambda item: item["generated_at"])[-760:]
    if retained and meeting_activity:
        latest = retained[-1]
        latest["meetings"] = [
            {
                **item,
                **({"activity": meeting_activity[str(item.get("event_slug"))]}
                   if str(item.get("event_slug")) in meeting_activity else {}),
            }
            for item in latest.get("meetings", [])
        ]
    meeting_calendar = sorted({
        str(meeting["date"])
        for item in retained
        for meeting in item.get("meetings", [])
        if isinstance(meeting, dict) and isinstance(meeting.get("date"), str)
    })
    decisions = _read_json(PROJECT_ROOT / "config" / "historical_transitions.json")["official_decisions"]
    actual = [
        {
            "date": item["date"],
            "before_upper": float(item["before"][1]),
            "after_upper": float(item["after"][1]),
            "change_bp": 100.0 * (float(item["after"][1]) - float(item["before"][1])),
        }
        for item in decisions
    ]
    return {
        "schema_version": 3,
        "method": "versioned marginal-preserving forward fan",
        "model_versions": {
            "historical-native-meeting-v1": "Native historical meeting buckets with a meeting-only persistence kernel; no terminal-consistency joint.",
            "historical-native-terminal-v1": "Native historical meeting buckets with a separately quoted terminal endpoint; meeting dependence remains meeting-only and no terminal-consistency joint is fitted.",
            "structural-terminal-ipf-v1": "Five meeting buckets plus a separately traded terminal-rate marginal, joined by persistence and terminal-consistency kernels and raked to quoted marginals.",
        },
        "window_days": REPLAY_WINDOW_DAYS,
        "archive_start": retained[0]["generated_at"],
        "latest": retained[-1]["generated_at"],
        "vintages": retained,
        "meeting_calendar": meeting_calendar,
        "actual_target_upper": actual,
        "events": _read_json(PROJECT_ROOT / "config" / "market_events.json").get("events", []),
        "disclosure": "Meeting and terminal marginals are quoted markets. Cross-meeting dependence is modeled with a persistent-stance and terminal-consistency kernel, then raked back to every quoted five-outcome marginal. When an intraday collector omits an otherwise observable horizon meeting, its last prior quote is carried forward and explicitly marked instead of dropping the meeting from the replay.",
        "historical_disclosure": historical_disclosure,
    }


def _timestamp_number(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _carried_historical_meeting(item: dict[str, Any], *, carried_from: str) -> dict[str, Any]:
    outcomes = item.get("native_outcomes")
    if not isinstance(outcomes, list) or len(outcomes) != 5:
        raise ValueError("historical fallback meeting requires five native outcomes")
    ordered = sorted(outcomes, key=lambda outcome: float(outcome["representative_bp"]))
    representatives = tuple(float(outcome["representative_bp"]) for outcome in ordered)
    if representatives != ACTION_BUCKETS_BP:
        raise ValueError("historical fallback meeting has invalid action representatives")
    raw_probabilities = [float(outcome["probability"]) for outcome in ordered]
    total = sum(raw_probabilities)
    if total <= 0 or any(value < 0 for value in raw_probabilities):
        raise ValueError("historical fallback meeting probabilities are invalid")
    probabilities = [value / total for value in raw_probabilities]
    expected = sum(value * move for value, move in zip(probabilities, ACTION_BUCKETS_BP, strict=True))
    down = probabilities[0] + probabilities[1]
    up = probabilities[3] + probabilities[4]
    return {
        "date": str(item["date"]),
        "event_slug": str(item["event_slug"]),
        "event_url": item.get("event_url"),
        "expected_change_bp": expected,
        "down": down,
        "unchanged": probabilities[2],
        "up": up,
        "prices": [
            {
                "label": str(outcome["label"]),
                "probability": probability,
                "raw_probability": float(outcome.get("raw_probability", outcome["probability"])),
                "representative_bp": representative,
            }
            for outcome, probability, representative in zip(ordered, probabilities, ACTION_BUCKETS_BP, strict=True)
        ],
        "actions": {
            "down": sum(probabilities[index] * ACTION_BUCKETS_BP[index] for index in (0, 1)) / down if down else -25.0,
            "unchanged": 0.0,
            "up": sum(probabilities[index] * ACTION_BUCKETS_BP[index] for index in (3, 4)) / up if up else 25.0,
        },
        "quote_status": "carried_forward",
        "carried_forward_from": carried_from,
        "source_timestamp": item.get("source_timestamp"),
    }


def _complete_event_meetings(
    event_meetings: list[dict[str, Any]], historical_vintage: dict[str, Any] | None, *, baseline: float,
) -> list[dict[str, Any]]:
    meetings = [dict(item) for item in event_meetings]
    present = {str(item["date"]) for item in meetings}
    if isinstance(historical_vintage, dict):
        carried_from = str(historical_vintage["generated_at"])
        for item in historical_vintage.get("meetings", []):
            if isinstance(item, dict) and str(item.get("date")) not in present:
                meetings.append(_carried_historical_meeting(item, carried_from=carried_from))
    meetings.sort(key=lambda item: str(item["date"]))
    target_before = baseline
    for meeting in meetings:
        meeting["expected_target_upper_before"] = target_before
        target_before += float(meeting["expected_change_bp"]) / 100.0
        meeting["expected_target_upper_after"] = target_before
    return meetings


def _event_policy_vintages(current: dict[str, Any]) -> list[dict[str, Any]]:
    path = PROJECT_ROOT / "outputs" / "event-studies" / "warsh-jackson-hole-2026-08-28" / "policy_meetings.json"
    if not path.exists():
        return []
    event = _read_json(path)
    window = event.get("window")
    event_meetings = event.get("meetings")
    if not isinstance(window, dict) or not isinstance(event_meetings, list):
        return []
    representatives = (-50.0, -25.0, 0.0, 25.0, 50.0)
    labels = ("50+ bps decrease", "25 bps decrease", "No change", "25 bps increase", "50+ bps increase")
    historical_vintages: list[dict[str, Any]] = []
    historical_path = PROJECT_ROOT / "data" / "historical-policy-replay.json"
    if historical_path.exists():
        historical_vintages = [
            item for item in _read_json(historical_path).get("vintages", [])
            if isinstance(item, dict) and isinstance(item.get("generated_at"), str)
        ]
    terminal_payloads = []
    for terminal_path in (PROJECT_ROOT / "outputs" / "fed-path-history" / "runs").glob("*/fed_path.json"):
        try:
            payload = _read_json(terminal_path)
            if isinstance(payload.get("generated_at"), str) and isinstance(payload.get("terminal"), dict):
                terminal_payloads.append(payload)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    output = []
    for checkpoint in ("before", "during", "after"):
        stamp = window.get(checkpoint)
        if not isinstance(stamp, str):
            continue
        meetings = []
        target_before = float(current["target_upper_bound_baseline"])
        urls: dict[str, str] = {}
        for item in event_meetings:
            if not isinstance(item, dict) or not isinstance(item.get("markets"), list):
                continue
            raw = [float(market[checkpoint]) for market in item["markets"]]
            total = sum(raw)
            probabilities = [value / total for value in raw]
            expected = sum(value * move for value, move in zip(probabilities, representatives, strict=True))
            down = probabilities[0] + probabilities[1]
            up = probabilities[3] + probabilities[4]
            down_action = sum(probabilities[index] * representatives[index] for index in (0, 1)) / down if down else -25.0
            up_action = sum(probabilities[index] * representatives[index] for index in (3, 4)) / up if up else 25.0
            date_value = str(item["date"])
            slug = str(item["event_slug"])
            meetings.append({
                "date": date_value,
                "event_slug": slug,
                "expected_change_bp": expected,
                "expected_target_upper_before": target_before,
                "expected_target_upper_after": target_before + expected / 100.0,
                "down": down,
                "unchanged": probabilities[2],
                "up": up,
                "prices": [
                    {"label": label, "probability": probability, "raw_probability": raw_probability, "representative_bp": representative}
                    for label, probability, raw_probability, representative in zip(labels, probabilities, raw, representatives, strict=True)
                ],
                "actions": {"down": down_action, "unchanged": 0.0, "up": up_action},
            })
            target_before += expected / 100.0
            urls[slug] = str(item.get("event_url", ""))
        checkpoint_time = _timestamp_number(stamp)
        historical_eligible = [item for item in historical_vintages if _timestamp_number(str(item["generated_at"])) <= checkpoint_time]
        fallback_vintage = max(historical_eligible, key=lambda item: _timestamp_number(str(item["generated_at"]))) if historical_eligible else None
        meetings = _complete_event_meetings(
            meetings, fallback_vintage, baseline=float(current["target_upper_bound_baseline"]),
        )
        terminal_eligible = [item for item in terminal_payloads if _timestamp_number(str(item["generated_at"])) <= checkpoint_time]
        terminal_source = max(terminal_eligible, key=lambda item: _timestamp_number(str(item["generated_at"]))) if terminal_eligible else None
        terminal_anchor = None
        if terminal_source is not None:
            terminal_anchor = {**terminal_source["terminal"], "carried_forward_from": terminal_source["generated_at"]}
        for meeting in meetings:
            if meeting.get("event_url"):
                urls[str(meeting["event_slug"])] = str(meeting["event_url"])
        output.append({
            "schema_version": 1,
            "kind": "event_checkpoint",
            "checkpoint": checkpoint,
            "label": {"before": "Pre-speech", "during": "Speech underway", "after": "Post-speech"}[checkpoint],
            "generated_at": stamp,
            "snapshot_fetched_at": stamp,
            "event": "Warsh Jackson Hole speech + 10:00 ET data window",
            "policy": {
                "target_upper_bound_baseline": current["target_upper_bound_baseline"],
                "effective_rate_baseline": current["effective_rate_baseline"],
                "meetings": meetings,
                "terminal_anchor": terminal_anchor,
                "tree": None,
                "historical_diagnostic": None,
                "source_urls": urls,
            },
        })
    return output


def _write_vintages(output: Path, meeting_root: Path, current: dict[str, Any]) -> list[dict[str, Any]]:
    vintage_dir = output / "data" / "vintages"
    vintage_dir.mkdir(parents=True, exist_ok=True)
    for path in sorted((meeting_root / "runs").glob("*/meeting_scenarios.json")):
        try:
            payload = _read_json(path)
            snapshot_path = path.with_name("policy-snapshot.json")
            snapshot = _read_json(snapshot_path) if snapshot_path.exists() else None
            vintage = {
                "schema_version": 1,
                "kind": "full_tree",
                "label": "Full market snapshot",
                "generated_at": payload["generated_at"],
                "snapshot_fetched_at": payload["snapshot_fetched_at"],
                "event": None,
                "policy": _compact_policy(payload, _event_activity(snapshot)),
            }
            key = _vintage_key(str(payload["generated_at"]))
            (vintage_dir / f"{key}.json").write_text(json.dumps(vintage, sort_keys=True, separators=(",", ":"), allow_nan=False), encoding="utf-8")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    # Older committed vintages remain part of the public archive. Rewrite only
    # the diagnostic envelope so extraction never publishes workstation paths
    # or obsolete runtime fingerprints.
    for path in vintage_dir.glob("*.json"):
        try:
            vintage = _read_json(path)
            if vintage.get("kind") == "event_checkpoint":
                path.unlink()
                continue
            policy = vintage.get("policy")
            if isinstance(policy, dict):
                _ensure_action_rates(policy)
            if isinstance(policy, dict) and "historical_diagnostic" in policy:
                policy["historical_diagnostic"] = _compact_historical_diagnostic(policy.get("historical_diagnostic"))
                path.write_text(json.dumps(vintage, sort_keys=True, separators=(",", ":"), allow_nan=False), encoding="utf-8")
        except (OSError, ValueError, json.JSONDecodeError):
            continue

    index = []
    for path in vintage_dir.glob("*.json"):
        try:
            vintage = _read_json(path)
            policy = vintage["policy"]
            meetings = policy["meetings"]
            first = meetings[0] if meetings else None
            index.append({
                "key": path.stem,
                "url": f"data/vintages/{path.name}",
                "kind": vintage["kind"],
                "label": vintage["label"],
                "generated_at": vintage["generated_at"],
                "event": vintage.get("event"),
                "meeting_count": len(meetings),
                "first_meeting_date": first["date"] if first else None,
                "first_meeting_up": first["up"] if first else None,
                "first_meeting_unchanged": first["unchanged"] if first else None,
                "has_tree": policy.get("tree") is not None,
            })
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return sorted(index, key=lambda row: (row["generated_at"], row["key"]))[-730:]


def _cadence_rows(run: Path) -> list[dict[str, Any]]:
    with (run / "event-time-profiles.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    surfaces: dict[tuple[str, str], dict[str, str]] = {}
    for row in rows:
        if not row.get("error") and row.get("change_vs_pre_bp"):
            surfaces.setdefault((row["current_meeting_date"], row["cutoff_ny"]), row)
    output = []
    for cutoff in sorted({key[1] for key in surfaces}):
        selected = [row for (_, value), row in surfaces.items() if value == cutoff]
        expected = [float(row["change_vs_pre_bp"]) for row in selected]
        down_tail = [float(row["down_50plus_change"]) for row in selected if row.get("down_50plus_change")]
        up_tail = [float(row["up_50plus_change"]) for row in selected if row.get("up_50plus_change")]
        output.append(
            {
                "cutoff": cutoff,
                "meeting_count": len(expected),
                "expected_action_change_bp": sum(expected) / len(expected),
                "down_50plus_change_pp": 100 * sum(down_tail) / len(down_tail) if down_tail else None,
                "down_tail_count": len(down_tail),
                "up_50plus_change_pp": 100 * sum(up_tail) / len(up_tail) if up_tail else None,
                "up_tail_count": len(up_tail),
            }
        )
    return output


def _intraday_history(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            key = (row["run_id"], row["shock_meeting_date"])
            grouped.setdefault(
                key,
                {
                    "run_id": row["run_id"],
                    "generated_at": row["generated_at"],
                    "meeting_date": row["shock_meeting_date"],
                    "expected_change_bp": float(row["baseline_expected_change_bp"]),
                },
            )
    return sorted(grouped.values(), key=lambda row: (row["generated_at"], row["meeting_date"]))[-320:]


def _load_daily_history(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, list) else []


def _daily_record(meeting: dict[str, Any], grid: dict[str, Any] | None, nfp: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "generated_at": meeting["generated_at"],
        "meetings": [
            {
                "date": item["date"],
                "expected_change_bp": item["expected_change_bp"],
                "prices": {price["label"]: price["probability"] for price in item["prices"]},
            }
            for item in meeting["meetings"]
        ],
        "curve": None
        if grid is None
        else {
            "as_of": grid["as_of"],
            "expected_delta_2y_bp": grid["expected_delta_2y_bp"],
            "expected_delta_10y_bp": grid["expected_delta_10y_bp"],
            "expected_slope_change_bp": grid["expected_slope_change_bp"],
        },
        "nfp": None
        if nfp is None or nfp.get("kalshi") is None
        else {
            "reference_month": nfp["reference_month"],
            "mean": nfp["kalshi"]["capped_mean"],
            "median_bracket": nfp["kalshi"]["quantile_50"],
        },
    }


def _copy_if_present(source: Path | None, target: Path) -> None:
    if source is not None and source.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def build(output: Path) -> Path:
    output.mkdir(parents=True, exist_ok=True)
    data_dir = output / "data"
    assets_dir = output / "assets"
    data_dir.mkdir(exist_ok=True)
    assets_dir.mkdir(exist_ok=True)

    prior_dashboard = None
    prior_path = data_dir / "dashboard.json"
    if prior_path.exists():
        try:
            prior_dashboard = _read_json(prior_path)
        except (ValueError, json.JSONDecodeError):
            prior_dashboard = None

    meeting_root = PROJECT_ROOT / "outputs" / "meeting-scenarios"
    if not (meeting_root / "latest.json").exists():
        # A source-only clone includes the last verified static bundle. Building
        # without a newly collected run is intentionally a validation/no-op,
        # never an excuse to replace good public data with a partial snapshot.
        if (data_dir / "dashboard.json").is_file() and (data_dir / "forecast-replay.json").is_file():
            (output / ".nojekyll").touch()
            return output
        raise FileNotFoundError("no verified meeting snapshot and no last-good site bundle")
    meeting_run = _latest_run(meeting_root)
    meeting = _read_json(meeting_run / "meeting_scenarios.json")
    snapshot_path = meeting_run / "policy-snapshot.json"
    meeting_snapshot = _read_json(snapshot_path) if snapshot_path.exists() else None
    meeting_activity = _event_activity(meeting_snapshot)
    grid_run = nfp_run = fed_run = None
    grid = nfp = fed = None
    prior_is_newer = bool(
        prior_dashboard is not None
        and str(prior_dashboard.get("forecast_generated_at", "")) > str(meeting.get("generated_at", ""))
    )
    current_policy = prior_dashboard["policy"] if prior_is_newer else _compact_policy(meeting, meeting_activity)
    _ensure_action_rates(current_policy)
    current_policy = _attach_activity(current_policy, meeting_activity)
    current_generated_at = prior_dashboard["forecast_generated_at"] if prior_is_newer else meeting["generated_at"]
    current_snapshot_at = prior_dashboard["snapshot_fetched_at"] if prior_is_newer else meeting["snapshot_fetched_at"]
    historical_run, historical = _optional_latest(
        PROJECT_ROOT / "outputs" / "historical-transitions", "model.json"
    )

    daily_path = data_dir / "daily-history.json"
    daily = _load_daily_history(daily_path)
    record = _daily_record(meeting, grid, nfp)
    daily = [row for row in daily if row.get("generated_at") != record["generated_at"]]
    daily.append(record)
    daily = sorted(daily, key=lambda row: str(row.get("generated_at", "")))[-730:]

    vintage_index = _write_vintages(output, meeting_root, meeting)
    full_tree_vintages = [item for item in vintage_index if item["kind"] == "full_tree"]
    if full_tree_vintages and str(full_tree_vintages[-1]["generated_at"]) > str(current_generated_at):
        archived_current = _read_json(output / str(full_tree_vintages[-1]["url"]))
        current_policy = archived_current["policy"]
        _ensure_action_rates(current_policy)
        current_policy = _attach_activity(current_policy, meeting_activity)
        current_generated_at = archived_current["generated_at"]
        current_snapshot_at = archived_current["snapshot_fetched_at"]
    events_path = PROJECT_ROOT / "config" / "market_events.json"
    events = _read_json(events_path).get("events", []) if events_path.exists() else []
    forecast_replay = _forecast_replay(output, meeting, meeting_activity)
    (data_dir / "forecast-replay.json").write_text(
        json.dumps(forecast_replay, sort_keys=True, separators=(",", ":"), allow_nan=False),
        encoding="utf-8",
    )

    payload = {
        "schema_version": 2,
        "site_built_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "forecast_generated_at": current_generated_at,
        "snapshot_fetched_at": current_snapshot_at,
        "policy": current_policy,
        "historical": (
            {
                "status": historical["production_gates"]["status"],
                "transition_count": historical["production_gates"]["transition_count"],
                "row_counts": historical["production_gates"]["row_counts"],
                "failures": historical["production_gates"]["failures"],
                "cadence": _cadence_rows(historical_run),
            }
            if historical is not None and historical_run is not None
            else _compact_historical_diagnostic(meeting.get("historical_transition_diagnostic"))
        ),
        "intraday_history": _intraday_history(meeting_root / "history.csv"),
        "daily_history": daily,
        "events": events,
        "vintage_index": vintage_index,
        "forecast_replay_url": "data/forecast-replay.json",
    }
    (data_dir / "dashboard.json").write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False),
        encoding="utf-8",
    )
    daily_path.write_text(json.dumps(daily, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    (output / ".nojekyll").touch()

    _copy_if_present(meeting_run / "conditional_rate_fan.svg", assets_dir / "conditional_rate_fan.svg")
    _copy_if_present(meeting_run / "conditional_tree.svg", assets_dir / "conditional_tree.svg")
    _copy_if_present(meeting_run / "meeting_scenarios.svg", assets_dir / "meeting_scenarios.svg")
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    print(build(args.output.resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
