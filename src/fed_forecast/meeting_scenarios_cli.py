"""Live orchestration for meeting decomposition and isolated action scenarios."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import secrets
import shutil
import sys
from dataclasses import asdict, replace
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .effr import fetch_latest_effr_payload, parse_latest_effr
from .fed_path import compute_meeting_distribution
from .fed_path_client import FedPathClient
from .fed_path_config import load_fed_path_config
from .fed_path_models import MeetingConfig, OutcomeConfig
from .meeting_scenarios import MeetingScenarioError, compute_meeting_scenarios
from .meeting_scenarios_svg import render_meeting_scenarios_svg
from .conditional_tree_svg import render_conditional_tree_svg
from .conditional_rate_fan_svg import render_conditional_rate_fan_svg
from .historical_transitions import TransitionObservation, apply_potential
from .historical_transitions_client import (
    HistoricalConfigError,
    HistoricalSnapshotError,
    current_historical_runtime_provenance,
    load_historical_config,
    load_historical_snapshot,
)


OUTCOMES = (
    OutcomeConfig("50+ bps decrease", -50.0),
    OutcomeConfig("25 bps decrease", -25.0),
    OutcomeConfig("No change", 0.0),
    OutcomeConfig("25 bps increase", 25.0),
    OutcomeConfig("50+ bps increase", 50.0),
)

_HISTORICAL_ARTIFACTS = {
    "event-time-profiles.csv",
    "exclusions.csv",
    "model.json",
    "observations.csv",
    "report.md",
    "snapshot.json",
    "topology-ledger.csv",
}


def _strict_json_file(path: Path, label: str) -> object:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-standard JSON constant: {value}")

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicates,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise MeetingScenarioError(f"historical transition {label} is not strict JSON: {error}") from error


def _historical_transition_diagnostic(payload: dict[str, object], project_root: Path) -> dict[str, object]:
    """Apply the learned adjacent-edge potential beside, never inside, the live tree."""
    root = project_root / "outputs" / "historical-transitions"
    pointer_path = root / "latest.json"
    if not pointer_path.exists():
        return {"status": "unavailable", "reason": "historical model not found", "active_in_tree": False}
    pointer = _strict_json_file(pointer_path, "latest pointer")
    if not isinstance(pointer, dict) or set(pointer) != {"schema_version", "run_path"} or pointer.get("schema_version") != 1:
        raise MeetingScenarioError("historical transition latest pointer has invalid schema")
    run_path = pointer.get("run_path") if isinstance(pointer, dict) else None
    candidate = Path(str(run_path)) if isinstance(run_path, str) else Path(".")
    if candidate.is_absolute() or len(candidate.parts) != 2 or candidate.parts[0] != "runs" or candidate.parts[1] in {"", ".", ".."}:
        raise MeetingScenarioError("historical transition latest pointer is unsafe")
    run_dir = root / candidate
    manifest = _strict_json_file(run_dir / "manifest.json", "manifest")
    if not isinstance(manifest, dict) or set(manifest) != {"schema_version", "files"} or manifest.get("schema_version") != 1:
        raise MeetingScenarioError("historical transition manifest has invalid schema")
    manifest_files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(manifest_files, dict) or set(manifest_files) != _HISTORICAL_ARTIFACTS:
        raise MeetingScenarioError("historical transition manifest is invalid")
    for name, evidence in manifest_files.items():
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or not isinstance(evidence, dict)
            or set(evidence) != {"sha256", "size_bytes"}
        ):
            raise MeetingScenarioError("historical transition manifest entry is invalid")
        digest = evidence.get("sha256")
        size = evidence.get("size_bytes")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
        ):
            raise MeetingScenarioError("historical transition manifest evidence is invalid")
        content = (run_dir / name).read_bytes()
        if digest != hashlib.sha256(content).hexdigest() or size != len(content):
            raise MeetingScenarioError(f"historical transition manifest mismatch: {name}")
    path = run_dir / "model.json"
    raw = path.read_bytes()
    model_payload = _strict_json_file(path, "model")
    if not isinstance(model_payload, dict) or model_payload.get("schema_version") != 1 or model_payload.get("category_order") != ["down", "unchanged", "up"]:
        raise MeetingScenarioError("historical transition model has invalid category order")
    fitted = model_payload.get("model")
    gates = model_payload.get("production_gates")
    if not isinstance(fitted, dict) or not isinstance(gates, dict):
        raise MeetingScenarioError("historical transition model has invalid schema")
    theta = fitted.get("theta")
    if not isinstance(theta, list) or len(theta) != 3 or any(not isinstance(row, list) or len(row) != 3 for row in theta):
        raise MeetingScenarioError("historical transition model has no potential")
    for row in theta:
        values = [float(value) for value in row]
        if any(not math.isfinite(value) for value in values) or abs(sum(values)) > 1e-8:
            raise MeetingScenarioError("historical transition potential is nonfinite or not row-centered")
    eligible = gates.get("eligible")
    status = gates.get("status")
    if not isinstance(eligible, bool) or status != ("production_eligible" if eligible else "diagnostic_only"):
        raise MeetingScenarioError("historical transition gate status is inconsistent")
    identification = model_payload.get("timing_destination_identification")
    if (
        identification not in {"identified", "not_identified"}
        or fitted.get("timing_destination_identification") != identification
        or gates.get("timing_destination_identification") != identification
    ):
        raise MeetingScenarioError("historical transition identification state is inconsistent")
    failures = gates.get("failures")
    if not isinstance(failures, list) or any(not isinstance(item, str) for item in failures):
        raise MeetingScenarioError("historical transition gate failures are invalid")
    identification_failure = "timing_destination_not_identified" in failures
    if identification_failure != (identification == "not_identified"):
        raise MeetingScenarioError("historical transition identification gate is inconsistent")
    if fitted.get("training_count") != gates.get("transition_count") or fitted.get("row_counts") != gates.get("row_counts"):
        raise MeetingScenarioError("historical transition training support is inconsistent")
    snapshot_raw = _strict_json_file(run_dir / "snapshot.json", "snapshot")
    try:
        historical_config = load_historical_config(project_root / "config" / "historical_transitions.json")
        snapshot = load_historical_snapshot(run_dir / "snapshot.json", historical_config).to_dict()
    except (HistoricalConfigError, HistoricalSnapshotError, OSError) as error:
        raise MeetingScenarioError(f"historical transition snapshot failed replay validation: {error}") from error
    if snapshot_raw != snapshot:
        raise MeetingScenarioError("historical transition snapshot representation is inconsistent")
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != 1:
        raise MeetingScenarioError("historical transition snapshot is invalid")
    provenance = snapshot.get("runtime_provenance")
    if not isinstance(provenance, dict):
        raise MeetingScenarioError("historical transition snapshot provenance is invalid")
    if provenance != current_historical_runtime_provenance():
        raise MeetingScenarioError("historical transition snapshot provenance does not match current code/runtime")
    meetings = payload.get("meetings")
    if not isinstance(meetings, list):
        raise MeetingScenarioError("meeting scenario payload has invalid meetings")
    tables = []
    for current, following in zip(meetings, meetings[1:]):
        current_categories = current.get("categories")
        if not isinstance(current_categories, list):
            raise MeetingScenarioError("meeting categories are malformed")
        actions_by_category = {str(item["category"]): item["conditional_change_bp"] for item in current_categories}
        actions = tuple(float(actions_by_category[name]) for name in ("down", "unchanged", "up"))
        current_pre = (
            float(current["decrease_probability"]),
            float(current["no_change_probability"]),
            float(current["increase_probability"]),
        )
        next_pre = (
            float(following["decrease_probability"]),
            float(following["no_change_probability"]),
            float(following["increase_probability"]),
        )
        rows = []
        for category, action in zip(("down", "unchanged", "up"), actions, strict=True):
            observation = TransitionObservation(
                current_meeting_id=str(current["event_slug"]),
                next_meeting_id=str(following["event_slug"]),
                current_meeting_date=date.fromisoformat(str(current["date"])),
                next_meeting_date=date.fromisoformat(str(following["date"])),
                current_pre=current_pre,
                current_candidate_actions_bp=actions,
                realized_category=category,
                realized_action_bp=action,
                next_pre=next_pre,
                next_post=next_pre,
            )
            prediction = apply_potential(
                observation,
                theta,
                support_floor=float(fitted.get("support_floor", 0.005)),
                ipf_tolerance=float(fitted.get("ipf", {}).get("tolerance", 1e-12)),
                ipf_max_iterations=int(fitted.get("ipf", {}).get("max_iterations", 2000)),
            )
            rows.append({"realized_category": category, "next_probabilities": dict(zip(("down", "unchanged", "up"), prediction, strict=True))})
        tables.append({"realized_meeting_date": current["date"], "next_meeting_date": following["date"], "rows": rows})
    return {
        "status": gates.get("status", "diagnostic_only"),
        "active_in_tree": False,
        "source": "historical_resolved_polymarket_transitions",
        "source_path": str(path),
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "training_count": fitted.get("training_count"),
        "row_counts": fitted.get("row_counts"),
        "gate_failures": gates.get("failures", []),
        "timing_destination_identification": model_payload.get("timing_destination_identification"),
        "config_sha256": snapshot.get("config_sha256"),
        "topology_blind_sha256": snapshot.get("topology_blind_sha256"),
        "official_decision_sha256": snapshot.get("official_decision_sha256"),
        "runtime_provenance": snapshot.get("runtime_provenance"),
        "adjacent_conditional_tables": tables,
        "interpretation": "Diagnostic total announcement-conditioned dependence; not used in the live tree while gates fail.",
    }


def _load_config(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    expected = {"schema_version", "max_spread", "terminal_date", "terminal_event_slug", "meetings", "conditional_tree"}
    if not isinstance(payload, dict) or set(payload) != expected or payload.get("schema_version") != 1:
        raise MeetingScenarioError("meeting scenario configuration has invalid schema")
    meetings = payload["meetings"]
    if not isinstance(meetings, list) or not meetings:
        raise MeetingScenarioError("meeting scenario configuration requires meetings")
    parsed_dates = []
    for row in meetings:
        if not isinstance(row, dict) or set(row) != {"date", "event_slug"}:
            raise MeetingScenarioError("meeting configuration row has invalid schema")
        parsed_dates.append(date.fromisoformat(str(row["date"])))
        if not isinstance(row["event_slug"], str) or not row["event_slug"]:
            raise MeetingScenarioError("meeting event slug must be non-empty")
    if parsed_dates != sorted(parsed_dates) or len(set(parsed_dates)) != len(parsed_dates):
        raise MeetingScenarioError("configured meetings must be unique and chronological")
    terminal_date = date.fromisoformat(str(payload["terminal_date"]))
    if terminal_date not in parsed_dates:
        raise MeetingScenarioError("terminal date must match a configured meeting")
    max_spread = float(payload["max_spread"])
    if not 0 < max_spread <= 1:
        raise MeetingScenarioError("max spread must lie within (0, 1]")
    tree = payload["conditional_tree"]
    tree_fields = {"dependence_strength", "dependence_decay", "rake_tolerance", "rake_max_iterations"}
    if not isinstance(tree, dict) or set(tree) != tree_fields:
        raise MeetingScenarioError("conditional tree configuration has invalid schema")
    return payload


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")


def _report(payload: dict[str, object]) -> bytes:
    meetings = payload["meetings"]
    scenarios = payload["scenarios"]
    terminal = payload["terminal_anchor"]
    lines = [
        "# Conditional Fed meeting tree", "",
        f"- Snapshot: {payload['snapshot_fetched_at']}",
        f"- Current target upper bound / EFFR: {float(payload['target_upper_bound_baseline']):.3f}% / {float(payload['effective_rate_baseline']):.3f}%",
        f"- End-2026 Polymarket terminal expected upper bound: {float(terminal['expected_target_upper']):.3f}%", "",
        "> Quoted meeting markets identify marginals. The conditional tree preserves those marginals exactly while modeling how later odds reprice after each realized outcome.", "",
        "## Meeting-change forecasts", "",
        "| Meeting | Down | Unchanged | Up | Expected action | Expected upper bound after action |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for meeting in meetings:
        lines.append(
            f"| {meeting['date']} | {float(meeting['decrease_probability']):.2%} | "
            f"{float(meeting['no_change_probability']):.2%} | {float(meeting['increase_probability']):.2%} | "
            f"{float(meeting['expected_change_bp']):+.2f} bp | {float(meeting['expected_target_upper_after']):.3f}% |"
        )
    tree = payload["conditional_tree"]
    lines.extend(("", "## Conditional transition tables", ""))
    for table in tree["adjacent_conditional_tables"]:
        lines.extend((
            f"### {table['realized_meeting_date']} -> {table['next_meeting_date']}", "",
            "| Realized outcome | Current probability | Next -50+ | Next -25 | Next 0 | Next +25 | Next +50+ |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ))
        for row in table["rows"]:
            next_probabilities = row["next_probabilities"]
            lines.append(
                f"| {row['realized_category']} | {float(row['realized_probability']):.2%} | "
                f"{float(next_probabilities['down_50plus']):.2%} | {float(next_probabilities['down_25']):.2%} | "
                f"{float(next_probabilities['unchanged']):.2%} | {float(next_probabilities['up_25']):.2%} | "
                f"{float(next_probabilities['up_50plus']):.2%} |"
            )
        lines.append("")
    historical = payload.get("historical_transition_diagnostic")
    if isinstance(historical, dict):
        lines.extend(("## Resolved-market transition diagnostic", ""))
        lines.append(
            f"Status: **{historical.get('status')}**; active in live tree: **{historical.get('active_in_tree')}**. "
            f"Training support: `{historical.get('row_counts')}`."
        )
        lines.append("")
        failures = historical.get("gate_failures", [])
        if failures:
            lines.extend((f"Gate failures: `{json.dumps(failures)}`", ""))
        for table in historical.get("adjacent_conditional_tables", []):
            lines.extend((
                f"### Historical diagnostic {table['realized_meeting_date']} -> {table['next_meeting_date']}", "",
                "| Realized outcome | Next down | Next unchanged | Next up |",
                "|---|---:|---:|---:|",
            ))
            for row in table["rows"]:
                probabilities = row["next_probabilities"]
                lines.append(
                    f"| {row['realized_category']} | {float(probabilities['down']):.2%} | "
                    f"{float(probabilities['unchanged']):.2%} | {float(probabilities['up']):.2%} |"
                )
            lines.append("")
    lines.extend((
        "## Highest-probability full paths", "",
        "| Path | Probability | Conditional end-2026 upper | Representative upper after last meeting |",
        "|---|---:|---:|---:|",
    ))
    for leaf in tree["leaf_paths"][:12]:
        lines.append(
            f"| {' -> '.join(leaf['path'])} | {float(leaf['path_probability']):.2%} | "
            f"{float(leaf['representative_target_upper_after_last_meeting']):.3f}% | "
            f"{float(leaf['representative_target_upper_after_last_meeting']):.3f}% |"
        )
    lines.extend((
        "", "## Mechanical one-meeting benchmark", "",
        "The selected category replaces only that meeting's expected action. The next meeting's own D/N/U probabilities and expected action remain unchanged.", "",
        "| Shock meeting | Scenario | Current probability | Conditional action | Surprise vs baseline | Next meeting | Baseline expected upper | Shock-only expected upper |",
        "|---|---|---:|---:|---:|---|---:|---:|",
    ))
    for scenario in scenarios:
        if scenario["next_meeting_date"] is None:
            continue
        lines.append(
            f"| {scenario['shock_meeting_date']} | {scenario['category']} | {float(scenario['scenario_probability']):.2%} | "
            f"{float(scenario['scenario_change_bp']):+.2f} bp | {float(scenario['surprise_vs_baseline_bp']):+.2f} bp | "
            f"{scenario['next_meeting_date']} | {float(scenario['next_meeting_baseline_target_upper']):.3f}% | "
            f"{float(scenario['next_meeting_mechanical_target_upper']):.3f}% |"
        )
    first_date = meetings[0]["date"]
    lines.extend(("", f"## {first_date} shock carried through the path", "", "| Scenario | Date | Point | Baseline upper | Mechanical upper | Anchor-respecting upper |", "|---|---|---|---:|---:|---:|"))
    for scenario in scenarios:
        if scenario["shock_meeting_date"] != first_date:
            continue
        for point in scenario["downstream"]:
            lines.append(
                f"| {scenario['category']} | {point['date']} | {point['kind']} | "
                f"{float(point['baseline_expected_target_upper']):.3f}% | {float(point['mechanical_expected_target_upper']):.3f}% | "
                f"{float(point['anchor_respecting_expected_target_upper']):.3f}% |"
            )
    lines.extend((
        "", "## Interpretation", "",
        "- The conditional tree is the primary scenario engine: every realized outcome updates all remaining meeting probabilities.",
        "- Iterative proportional fitting preserves each quoted meeting marginal and the quoted terminal-rate distribution exactly.",
        "- Conditional transitions are model-implied because separate markets do not directly trade P(next outcome | prior outcome).",
        "- A persistent policy-stance kernel transmits information forward while preserving every quoted meeting marginal; the end-2026 level surface remains an independent comparison.",
        "- The older one-shock calculation is retained only as a transparent mechanical benchmark.",
        "- The primary tree keeps -50+, -25, 0, +25 and +50+ as separate branches. Open-ended tails use +/-50 bp as their path-arithmetic representatives.",
        "- The older down/up benchmark still uses conditional means within grouped directions and is not the interactive tree.",
        "- A resolved-market historical transition estimate is shown separately when available; it is not activated while sample or identification gates fail.",
        "", "## Markets", "",
    ))
    for meeting in meetings:
        lines.append(f"- [{meeting['date']}](https://polymarket.com/event/{meeting['event_slug']})")
    lines.append(f"- [Independent end-2026 rate market](https://polymarket.com/event/{terminal['event_slug']})")
    return ("\n".join(lines) + "\n").encode("utf-8")


_HISTORY_FIELDS = (
    "run_id", "generated_at", "snapshot_fetched_at", "shock_meeting_date", "category",
    "scenario_probability", "scenario_change_bp", "baseline_expected_change_bp",
    "surprise_vs_baseline_bp", "next_meeting_date", "next_meeting_expected_change_bp",
    "next_meeting_baseline_target_upper", "next_meeting_mechanical_target_upper",
)


def _rebuild_history(root: Path) -> None:
    rows = []
    for result_path in sorted((root / "runs").glob("*/meeting_scenarios.json")):
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        for scenario in payload["scenarios"]:
            rows.append({
                "run_id": result_path.parent.name,
                "generated_at": payload["generated_at"],
                "snapshot_fetched_at": payload["snapshot_fetched_at"],
                **{field: scenario.get(field) for field in _HISTORY_FIELDS[3:]},
            })
    rows.sort(key=lambda item: (str(item["generated_at"]), str(item["run_id"]), str(item["shock_meeting_date"]), str(item["category"])))
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=_HISTORY_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    root.mkdir(parents=True, exist_ok=True)
    temporary = root / f".history.{secrets.token_hex(4)}.tmp"
    temporary.write_text(buffer.getvalue(), encoding="utf-8")
    os.replace(temporary, root / "history.csv")


_CONDITIONAL_HISTORY_FIELDS = (
    "run_id", "generated_at", "snapshot_fetched_at", "realized_meeting_date",
    "next_meeting_date", "realized_category", "realized_probability",
    "next_down_probability", "next_unchanged_probability", "next_up_probability",
    "dependence_strength", "dependence_decay",
)


def _rebuild_conditional_history(root: Path) -> None:
    rows = []
    for result_path in sorted((root / "runs").glob("*/meeting_scenarios.json")):
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        tree = payload.get("conditional_tree")
        if not isinstance(tree, dict):
            continue
        settings = tree["settings"]
        for table in tree["adjacent_conditional_tables"]:
            for row in table["rows"]:
                next_probabilities = row["next_probabilities"]
                if "down" in next_probabilities:
                    down_probability = next_probabilities["down"]
                    unchanged_probability = next_probabilities["unchanged"]
                    up_probability = next_probabilities["up"]
                else:
                    down_probability = next_probabilities["down_50plus"] + next_probabilities["down_25"]
                    unchanged_probability = next_probabilities["unchanged"]
                    up_probability = next_probabilities["up_25"] + next_probabilities["up_50plus"]
                rows.append({
                    "run_id": result_path.parent.name,
                    "generated_at": payload["generated_at"],
                    "snapshot_fetched_at": payload["snapshot_fetched_at"],
                    "realized_meeting_date": table["realized_meeting_date"],
                    "next_meeting_date": table["next_meeting_date"],
                    "realized_category": row["realized_category"],
                    "realized_probability": row["realized_probability"],
                    "next_down_probability": down_probability,
                    "next_unchanged_probability": unchanged_probability,
                    "next_up_probability": up_probability,
                    "dependence_strength": settings["dependence_strength"],
                    "dependence_decay": settings["dependence_decay"],
                })
    rows.sort(key=lambda item: (
        str(item["generated_at"]), str(item["run_id"]), str(item["realized_meeting_date"]),
        str(item["realized_category"]),
    ))
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=_CONDITIONAL_HISTORY_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    root.mkdir(parents=True, exist_ok=True)
    temporary = root / f".conditional-history.{secrets.token_hex(4)}.tmp"
    temporary.write_text(buffer.getvalue(), encoding="utf-8")
    os.replace(temporary, root / "conditional_history.csv")


def run_meeting_scenarios(config_path: Path, output_dir: Path, *, now: datetime | None = None) -> Path:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        raise MeetingScenarioError("run timestamp must include an offset")
    settings = _load_config(config_path)
    project_root = config_path.resolve().parent.parent
    base = load_fed_path_config(config_path.parent / "fed_path.json", project_root=project_root)
    if settings["terminal_event_slug"] != base.terminal_event_slug:
        raise MeetingScenarioError("terminal market does not match the reviewed fed-path topology")
    as_of = moment.astimezone(ZoneInfo("America/Los_Angeles")).date()
    configured_meetings = tuple(
        MeetingConfig(date.fromisoformat(str(row["date"])), str(row["event_slug"]), OUTCOMES)
        for row in settings["meetings"]
        if date.fromisoformat(str(row["date"])) >= as_of
    )
    if not configured_meetings:
        raise MeetingScenarioError("no configured meeting remains in the forecast horizon")
    effr_payload = fetch_latest_effr_payload()
    effr = parse_latest_effr(effr_payload)
    config = replace(
        base,
        target_upper_bound=effr.target_to,
        effective_rate_baseline=effr.rate,
        max_spread=float(settings["max_spread"]),
        meetings=configured_meetings,
    )
    snapshot = FedPathClient().fetch_snapshot(config)
    distributions = tuple(
        compute_meeting_distribution(
            meeting,
            tuple(price for price in snapshot.meeting_prices if price.meeting_date == meeting.date),
        )
        for meeting in config.meetings
    )
    generated_at = moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    result = compute_meeting_scenarios(
        config, distributions, snapshot.terminal_prices,
        terminal_date=date.fromisoformat(str(settings["terminal_date"])),
        generated_at=generated_at, snapshot_fetched_at=snapshot.fetched_at,
        tree_settings=settings["conditional_tree"],
    )
    result["price_quality"] = [asdict(item) for item in snapshot.selected_prices]
    result["source_urls"] = {
        meeting.event_slug: f"https://polymarket.com/event/{meeting.event_slug}"
        for meeting in config.meetings
    } | {config.terminal_event_slug: f"https://polymarket.com/event/{config.terminal_event_slug}"}
    result["historical_transition_diagnostic"] = _historical_transition_diagnostic(result, project_root)

    run_id = moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + "-" + secrets.token_hex(4)
    root = Path(output_dir)
    run_dir = root / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    files = {
        "meeting_scenarios.json": _json_bytes(result),
        "report.md": _report(result),
        "meeting_scenarios.svg": render_meeting_scenarios_svg(result),
        "conditional_tree.svg": render_conditional_tree_svg(result),
        "conditional_rate_fan.svg": render_conditional_rate_fan_svg(result),
        "config.json": _json_bytes(settings),
        "effr-latest.json": _json_bytes(effr_payload),
        "policy-snapshot.json": _json_bytes(snapshot.to_dict()),
    }
    try:
        for name, content in files.items():
            (run_dir / name).write_bytes(content)
        manifest = {
            "schema_version": 1,
            "files": {name: {"sha256": hashlib.sha256(content).hexdigest(), "size_bytes": len(content)} for name, content in files.items()},
        }
        (run_dir / "manifest.json").write_bytes(_json_bytes(manifest))
        root.mkdir(parents=True, exist_ok=True)
        temporary = root / f".latest.{secrets.token_hex(4)}.tmp"
        temporary.write_bytes(_json_bytes({"schema_version": 1, "run_path": f"runs/{run_id}"}))
        os.replace(temporary, root / "latest.json")
    except BaseException:
        if run_dir.exists():
            shutil.rmtree(run_dir)
        raise
    _rebuild_history(root)
    _rebuild_conditional_history(root)
    return run_dir


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a conditional five-outcome Fed meeting tree plus a grouped mechanical benchmark.")
    parser.add_argument("--config", type=Path, default=Path("config/markets.json"))
    parser.add_argument("--output-dir", type=Path, default=Path("outputs/meeting-scenarios"))
    return parser


def meeting_scenarios_main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        run_dir = run_meeting_scenarios(args.config, args.output_dir)
        payload = json.loads((run_dir / "meeting_scenarios.json").read_text(encoding="utf-8"))
    except Exception as error:
        print(f"meeting scenarios failed: {error}", file=sys.stderr)
        return 2
    first = payload["meetings"][0]
    print(
        f"{first['date']} D/N/U: {first['decrease_probability']:.1%} / "
        f"{first['no_change_probability']:.1%} / {first['increase_probability']:.1%}"
    )
    print(f"Run directory: {run_dir.resolve()}")
    print(f"History CSV: {(args.output_dir / 'history.csv').resolve()}")
    print(f"Conditional history CSV: {(args.output_dir / 'conditional_history.csv').resolve()}")
    return 0
