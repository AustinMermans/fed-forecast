"""Immutable artifacts for the resolved-FOMC transition study."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import secrets
import shutil
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, timezone
from pathlib import Path

from .historical_transitions import (
    TransitionObservation,
    apply_potential,
    no_update_prediction,
    scalar_persistence_prediction,
    surface_metrics,
)


def _portable(value: object) -> object:
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite output")
        return round(value, 12)
    if isinstance(value, Mapping):
        return {str(key): _portable(item) for key, item in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_portable(item) for item in value]
    if hasattr(value, "to_dict"):
        return _portable(value.to_dict())
    if hasattr(value, "isoformat"):
        return value.isoformat()  # type: ignore[union-attr]
    raise TypeError(f"unsupported output type: {type(value).__name__}")


def canonical_json(payload: object) -> bytes:
    return (json.dumps(_portable(payload), allow_nan=False, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def _evidence_json(payload: object) -> bytes:
    """Serialize raw evidence without the model's 12-decimal quantization."""
    return (json.dumps(payload, allow_nan=False, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def _rows_csv(rows: Iterable[Mapping[str, object]]) -> bytes:
    normalized: list[dict[str, object]] = []
    for row in rows:
        converted = _portable(dict(row))
        if not isinstance(converted, Mapping):
            raise TypeError("CSV row must remain a mapping")
        normalized.append(dict(converted))
    if not normalized:
        return b""
    fields = sorted({key for row in normalized for key in row})
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="raise", lineterminator="\n")
    writer.writeheader()
    def safe_cell(value: object) -> object:
        encoded = json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else value
        if isinstance(encoded, str) and encoded.startswith(("=", "+", "-", "@")):
            return "'" + encoded
        return encoded

    for row in sorted(normalized, key=lambda item: tuple(str(item.get(field, "")) for field in fields)):
        writer.writerow({key: safe_cell(value) for key, value in row.items()})
    return stream.getvalue().encode()


def _event_profile_rows(topology: Iterable[Mapping[str, object]]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for diagnostic in topology:
        if diagnostic.get("record_type") != "surface_diagnostic":
            continue
        profile = diagnostic.get("event_time_profile")
        if not isinstance(profile, Mapping):
            continue
        pre_surface = diagnostic.get("next_pre_action_buckets")
        pre_expected = pre_surface.get("expected_action_bp") if isinstance(pre_surface, Mapping) else None
        pre_down_tail = pre_surface.get("down_50plus_probability") if isinstance(pre_surface, Mapping) else None
        pre_up_tail = pre_surface.get("up_50plus_probability") if isinstance(pre_surface, Mapping) else None
        for cutoff, surface in profile.items():
            if not isinstance(surface, Mapping):
                continue
            base = {
                "current_meeting_date": diagnostic.get("current_meeting_date"),
                "next_meeting_date": diagnostic.get("next_meeting_date"),
                "cutoff_ny": cutoff,
                "expected_action_bp": surface.get("expected_action_bp"),
                "pre_expected_action_bp": pre_expected,
                "change_vs_pre_bp": (
                    float(str(surface["expected_action_bp"])) - float(str(pre_expected))
                    if isinstance(surface.get("expected_action_bp"), (int, float)) and isinstance(pre_expected, (int, float))
                    else None
                ),
                "pre_down_50plus_probability": pre_down_tail,
                "pre_up_50plus_probability": pre_up_tail,
                "down_50plus_change": (
                    float(str(surface["down_50plus_probability"])) - float(str(pre_down_tail))
                    if isinstance(surface.get("down_50plus_probability"), (int, float)) and isinstance(pre_down_tail, (int, float))
                    else None
                ),
                "up_50plus_change": (
                    float(str(surface["up_50plus_probability"])) - float(str(pre_up_tail))
                    if isinstance(surface.get("up_50plus_probability"), (int, float)) and isinstance(pre_up_tail, (int, float))
                    else None
                ),
                "down_50plus_probability": surface.get("down_50plus_probability"),
                "up_50plus_probability": surface.get("up_50plus_probability"),
                "down_50plus_identified": surface.get("down_50plus_identified"),
                "up_50plus_identified": surface.get("up_50plus_identified"),
                "category_probabilities": surface.get("category_probabilities"),
                "error": surface.get("error"),
            }
            buckets = surface.get("buckets")
            if not isinstance(buckets, list) or not buckets:
                rows.append(base)
                continue
            for bucket in buckets:
                if isinstance(bucket, Mapping):
                    rows.append({**base, **{f"bucket_{key}": value for key, value in bucket.items()}})
    return rows


def _descriptive(model: Mapping[str, object], observations: list[Mapping[str, object]]) -> dict[str, dict[str, float]]:
    fitted = model.get("model")
    if not observations or not isinstance(fitted, Mapping):
        return {}
    required = {"current_meeting_id", "next_meeting_id", "current_meeting_date", "next_meeting_date", "current_pre", "current_candidate_actions_bp", "realized_category", "realized_action_bp", "next_pre", "next_post"}
    if any(not required.issubset(row) for row in observations):
        return {}
    theta = fitted.get("theta")
    if not isinstance(theta, list):
        return {}
    totals = {name: {metric: 0.0 for metric in ("cross_entropy", "kl_divergence", "total_variation")} for name in ("no_update", "scalar_persistence", "historical")}
    def triple(value: object) -> tuple[float, float, float]:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 3:
            raise TypeError("serialized probability/action vector must have length three")
        return (float(str(value[0])), float(str(value[1])), float(str(value[2])))

    for row in observations:
        next_realized = row.get("next_realized_category")
        if next_realized is not None and not isinstance(next_realized, str):
            raise TypeError("next_realized_category must be a string or null")
        observation = TransitionObservation(
            current_meeting_id=str(row["current_meeting_id"]),
            next_meeting_id=str(row["next_meeting_id"]),
            current_meeting_date=date.fromisoformat(str(row["current_meeting_date"])),
            next_meeting_date=date.fromisoformat(str(row["next_meeting_date"])),
            current_pre=triple(row["current_pre"]),
            current_candidate_actions_bp=triple(row["current_candidate_actions_bp"]),
            realized_category=str(row["realized_category"]),
            realized_action_bp=float(str(row["realized_action_bp"])),
            next_pre=triple(row["next_pre"]),
            next_post=triple(row["next_post"]),
            topology_cohort=str(row.get("topology_cohort", "primary_negrisk")),
            next_realized_category=next_realized,
        )
        predictions = {
            "no_update": no_update_prediction(observation),
            "scalar_persistence": scalar_persistence_prediction(observation),
            "historical": apply_potential(observation, theta, support_floor=float(fitted.get("support_floor", 0.005))),
        }
        for name, prediction in predictions.items():
            metrics = surface_metrics(prediction, observation.next_post)
            for metric in totals[name]:
                totals[name][metric] += float(getattr(metrics, metric))
    return {name: {metric: value / len(observations) for metric, value in values.items()} for name, values in totals.items()}


def _report(model: Mapping[str, object], observations: list[Mapping[str, object]], exclusions: list[Mapping[str, object]], profile_rows: Sequence[Mapping[str, object]]) -> bytes:
    gates = model.get("production_gates", model.get("gates", {}))
    status = gates.get("status", "diagnostic_only") if isinstance(gates, Mapping) else "diagnostic_only"
    fitted = model.get("model", {})
    counts = fitted.get("row_counts", {}) if isinstance(fitted, Mapping) else {}
    validation = model.get("walk_forward", model.get("validation", {}))
    failures = gates.get("failures", gates.get("failure_reasons", [])) if isinstance(gates, Mapping) else []
    descriptive = _descriptive(model, observations)
    fitted_floor = float(fitted.get("support_floor", 0.005)) if isinstance(fitted, Mapping) else 0.005
    floor_applications = 0
    for row in observations:
        for field in ("current_pre", "next_pre"):
            vector = row.get(field)
            if isinstance(vector, Sequence) and not isinstance(vector, (str, bytes)):
                floor_applications += sum(1 for value in vector if isinstance(value, (int, float)) and float(value) < fitted_floor)
    unique_meetings = validation.get("unique_meetings") if isinstance(validation, Mapping) else None
    calendar_span = validation.get("calendar_span") if isinstance(validation, Mapping) else None
    robustness = model.get("robustness_diagnostics", {})
    lines = [
        "# Historical Polymarket FOMC transitions", "",
        f"**Status: {status}.** This study reconstructs how the next FOMC market repriced after each resolved meeting; it is not a directly traded conditional tree.", "",
        "## What was measured", "",
        "For each eligible adjacent meeting pair, the pipeline takes synchronized pre-announcement probabilities for both meetings, observes the current decision, and compares the next meeting's 15:30 New York distribution with its 13:00 distribution. The fitted association preserves the explicitly support-smoothed estimation marginals through IPF, while the raw surfaces remain separately retained, and uses only ex-ante action-bucket representatives.", "",
        "## Sample and deployment gate", "",
        f"- Eligible transitions: {len(observations)}",
        f"- Unique meetings: {unique_meetings}",
        f"- Calendar span: `{json.dumps(calendar_span, sort_keys=True)}`",
        f"- Excluded pairs: {len(exclusions)}",
        f"- Realized row counts: `{json.dumps(counts, sort_keys=True)}`",
        f"- Timing/destination identification: `{model.get('timing_destination_identification', 'not_identified')}`",
        f"- Gate failures: `{json.dumps(failures, sort_keys=True)}`", "",
        f"- Estimation support floor: {fitted_floor:.4f}; applied to {floor_applications} pre-marginal coordinates inside the estimator",
        "- Quote rule: one-minute source history; common event-time cutoffs; each coordinate at most 10 minutes old and within 10 minutes of its surface peers", "",
        "The public history remains diagnostic unless every global, row-support, topology, identification, numerical, provenance, and replay gate passes. In particular, the model does not infer a hike response by mirroring historical cuts.", "",
        "## Descriptive fit (same-sample; not out-of-sample evidence)", "",
        "| Method | Cross-entropy | KL divergence | Total variation |",
        "|---|---:|---:|---:|",
    ]
    for name in ("no_update", "scalar_persistence", "historical"):
        metric_values = descriptive.get(name)
        if metric_values:
            lines.append(f"| {name.replace('_', ' ').title()} | {metric_values['cross_entropy']:.4f} | {metric_values['kl_divergence']:.4f} | {metric_values['total_variation']:.4f} |")
    if isinstance(validation, Mapping):
        bootstrap = validation.get("bootstrap", {})
        lines.extend((
            "", "## Walk-forward comparison", "",
            f"- Scored transitions: {validation.get('scored_transitions', 0)}",
            f"- Skipped transitions: {validation.get('skipped_transitions', 0)}",
            f"- Bootstrap state: `{bootstrap.get('state') if isinstance(bootstrap, Mapping) else 'unavailable'}`",
            "- Folds are scored only after the predeclared support burn-in; no supported fold means no out-of-sample performance claim.", "",
        ))
    cadence: dict[str, Mapping[str, object]] = {}
    for row in profile_rows:
        value = row.get("change_vs_pre_bp")
        cutoff = row.get("cutoff_ny")
        # One expected-action value is repeated by bucket; de-duplicate by
        # meeting/cutoff before averaging below.
        if isinstance(cutoff, str) and isinstance(value, (int, float)):
            key = f"{row.get('current_meeting_date')}|{cutoff}"
            cadence.setdefault(key, row)
    by_cutoff: dict[str, list[float]] = {}
    down_tail_by_cutoff: dict[str, list[float]] = {}
    up_tail_by_cutoff: dict[str, list[float]] = {}
    for key, cadence_row in cadence.items():
        cutoff = key.split("|", 1)[1]
        by_cutoff.setdefault(cutoff, []).append(float(str(cadence_row["change_vs_pre_bp"])))
        down_change = cadence_row.get("down_50plus_change")
        up_change = cadence_row.get("up_50plus_change")
        if isinstance(down_change, (int, float)):
            down_tail_by_cutoff.setdefault(cutoff, []).append(float(down_change))
        if isinstance(up_change, (int, float)):
            up_tail_by_cutoff.setdefault(cutoff, []).append(float(up_change))
    lines.extend(("## Event-time cadence diagnostic", "", "| New York cutoff | Mean expected-action repricing vs 13:00 | Mean change in −50+ tail | Mean change in +50+ tail | Meetings |", "|---|---:|---:|---:|---:|"))
    for cutoff in sorted(by_cutoff):
        cutoff_values = by_cutoff[cutoff]
        down_values = down_tail_by_cutoff.get(cutoff, [])
        up_values = up_tail_by_cutoff.get(cutoff, [])
        down_text = "n/a" if not down_values else f"{100 * sum(down_values) / len(down_values):+.2f} pp (n={len(down_values)})"
        up_text = "n/a" if not up_values else f"{100 * sum(up_values) / len(up_values):+.2f} pp (n={len(up_values)})"
        lines.append(f"| {cutoff} | {sum(cutoff_values) / len(cutoff_values):+.2f} bp | {down_text} | {up_text} | {len(cutoff_values)} |")
    lines.extend(("", "The full action-bucket probabilities—including 50+ bp tails—are in `event-time-profiles.csv`; D/N/U is used only at the tree interface.", ""))
    if isinstance(robustness, Mapping):
        decision = robustness.get("decision_window_1415", {})
        strict = robustness.get("strict_raw_total_bounds", {})
        fallback = robustness.get("exclude_child_action_fallbacks", {})
        cohorts = robustness.get("cohorts", {})
        cohort_mapping = cohorts if isinstance(cohorts, Mapping) else {}
        eligible = cohort_mapping.get("all_mechanically_eligible_sensitivity", {})
        eligible_mapping = eligible if isinstance(eligible, Mapping) else {}
        lines.extend((
            "## Robustness cohorts", "",
            f"- 14:15 decision-window fit: {decision.get('transition_count') if isinstance(decision, Mapping) else 'unavailable'} transitions",
            f"- Strict-total fit: {strict.get('transition_count') if isinstance(strict, Mapping) else 'unavailable'} transitions",
            f"- Child-fallback-exclusion fit: {fallback.get('transition_count') if isinstance(fallback, Mapping) else 'unavailable'} transitions",
            f"- Official realized move strata: `{json.dumps(robustness.get('official_realized_action_bp_counts', {}), sort_keys=True)}`",
            f"- Primary/legacy event counts: {cohort_mapping.get('primary_event_count', 'unavailable')} / {cohort_mapping.get('legacy_event_count', 'unavailable')}",
            f"- All mechanically eligible sensitivity: `{eligible_mapping.get('status', eligible)}`; {eligible_mapping.get('mechanically_eligible_event_count', 'unavailable')} events, {eligible_mapping.get('usable_adjacent_transition_count', 'unavailable')} usable adjacent transitions, {eligible_mapping.get('excluded_transition_count', 'unavailable')} excluded transition candidates",
            "- Full sensitivity parameters and diagnostics are retained in `model.json`.", "",
        ))
    lines.extend((
        "## Interpretation", "",
        "The learned object is total announcement-conditioned dependence for the adjacent meeting only. It does not identify how much of the response is timing substitution versus a change in the terminal policy destination, so it is displayed beside the current live tree rather than silently replacing it.", "",
        "## Data caveats", "",
        "Polymarket price history contains observed marks/trades rather than historical bid-ask books. Surfaces require tightly synchronized coordinates, topology is cohort-controlled, transitions are serially dependent, and intraday points are never counted as separate events.", "",
    ))
    return "\n".join(lines).encode()


def write_historical_transition_run(
    output_dir: Path,
    snapshot: object,
    observations: Iterable[object],
    topology_rows: Iterable[Mapping[str, object]],
    exclusion_rows: Iterable[Mapping[str, object]],
    result: object,
    *,
    now: datetime | None = None,
    suffix: str | None = None,
) -> Path:
    """Publish one immutable run and atomically update stable pointers."""
    root = Path(output_dir)
    runs = root / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    run_id = f"{moment.strftime('%Y%m%dT%H%M%S.%fZ')}-{suffix or secrets.token_hex(4)}"
    staging, final = runs / f".{run_id}.tmp", runs / run_id
    if staging.exists() or final.exists():
        raise FileExistsError(f"run already exists: {run_id}")
    def portable_rows(values: Iterable[object]) -> list[Mapping[str, object]]:
        output: list[Mapping[str, object]] = []
        for value in values:
            converted = _portable(value.to_dict() if hasattr(value, "to_dict") else value)
            if not isinstance(converted, Mapping):
                raise TypeError("artifact row must serialize to a mapping")
            output.append(converted)
        return output

    obs = portable_rows(observations)
    topology = portable_rows(topology_rows)
    exclusions = portable_rows(exclusion_rows)
    model = _portable(result.to_dict() if hasattr(result, "to_dict") else result)
    if not isinstance(model, dict):
        raise TypeError("model result must serialize to an object")
    profile_rows = _event_profile_rows(topology)
    files = {
        "snapshot.json": _evidence_json(snapshot.to_dict() if hasattr(snapshot, "to_dict") else snapshot),
        "topology-ledger.csv": _rows_csv(topology),
        "observations.csv": _rows_csv(obs),
        "exclusions.csv": _rows_csv(exclusions),
        "event-time-profiles.csv": _rows_csv(profile_rows),
        "model.json": canonical_json(model),
        "report.md": _report(model, obs, exclusions, profile_rows),
    }
    manifest = {"schema_version": 1, "files": {name: {"sha256": hashlib.sha256(content).hexdigest(), "size_bytes": len(content)} for name, content in sorted(files.items())}}
    files["manifest.json"] = canonical_json(manifest)
    staging.mkdir()
    try:
        for name, content in files.items():
            path = staging / name
            with path.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        os.replace(staging, final)
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    root.mkdir(parents=True, exist_ok=True)
    pointer = canonical_json({"schema_version": 1, "run_path": f"runs/{run_id}"})
    temporary = root / f".latest.{secrets.token_hex(4)}.tmp"
    temporary.write_bytes(pointer)
    os.replace(temporary, root / "latest.json")
    descriptor = os.open(root, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return final
