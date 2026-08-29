"""Allowlisted public evidence for the inactive historical-transition diagnostic."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

from .historical_transitions import smooth_support
from .historical_transitions_reporting import VerifiedHistoricalRun, canonical_json, verify_historical_transition_run


LEGACY_SNAPSHOT_SHA256 = "ceaa5e1bbdf3263ad709c44a78f576f64c13c5f0a5fe508323fdd9b676851994"
LEGACY_MODEL_SHA256 = "815b2379dffdb16744030ddbdf5fd30176d15a8aaa78f994d4edfa3873ae54c7"
LEGACY_MANIFEST_SHA256 = "2073e23c94b3618713c7f71ba4cdf02a80eb44c9a05d200bd7df3c81913f0446"
LEGACY_FAILURES = (
    "minimum_transition_count_not_met",
    "minimum_down_row_count_not_met",
    "minimum_up_row_count_not_met",
    "timing_destination_not_identified",
    "replay_validation_failed",
)
MAX_PUBLIC_BYTES = 16 * 1024
LEGACY_CONFIG_CANONICAL_SHA256 = "c78c20c2ff6eea32ad7d5e4b2862447e1bd8e74218a0e093e5b260a9bc954f83"
LEGACY_CONFIG_FILE_SHA256 = "f1707f20601fba5dbbc7b5f256f34c3402db7bab7678f00a42d8b7650fedebe8"
LEGACY_SOURCE_SHA256 = {
    "historical_transitions.py": "a6f45ba6ab13d084e32c441694edd9212175c3086cef440ff3717186aea08e23",
    "historical_transitions_cli.py": "a0c6a5a64d888b56d3b391d8ec9db4715a29c585c358af683ddbcf29ab68e358",
    "historical_transitions_client.py": "e8fb3dd6b3a231535c322fa31f6c349d1fe9b447449e2cb461dd84f212dc9242",
    "historical_transitions_reporting.py": "c42a6066708a8ef01113d2f55a78edbf99dc2948593730ab65d92eae9f6536f3",
}
RFC3339_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")


def _json(content: bytes, name: str) -> dict[str, object]:
    value = json.loads(content)
    if not isinstance(value, dict):
        raise ValueError(f"{name} must contain an object")
    return value


def _csv(content: bytes) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(content.decode("utf-8"))))


def _nearest_rank(values: Sequence[float], probability: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("cannot summarize an empty synchronization sample")
    rank = max(1, math.ceil(probability * len(ordered)))
    return ordered[rank - 1]


def _distribution(values: Sequence[float]) -> dict[str, float]:
    ordered = sorted(values)
    middle = len(ordered) // 2
    median = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
    return {"median": median, "p90": _nearest_rank(ordered, 0.9), "max": ordered[-1]}


def summarize_surface_synchronization(
    topology_rows: Sequence[Mapping[str, str]],
    observation_rows: Sequence[Mapping[str, str]],
) -> dict[str, object]:
    usable = {(row["current_meeting_date"], row["next_meeting_date"]) for row in observation_rows}
    dispersions: list[float] = []
    ages: list[float] = []
    for row in topology_rows:
        if row.get("record_type") != "surface_diagnostic" or (row.get("current_meeting_date"), row.get("next_meeting_date")) not in usable:
            continue
        current = json.loads(row["current_pre_action_buckets"])
        future = json.loads(row["next_pre_action_buckets"])
        profile = json.loads(row["event_time_profile"])["15:30"]
        surfaces = (
            (json.loads(row["current_pre_timestamps"]), float(current["cutoff_timestamp"])),
            (json.loads(row["next_pre_timestamps"]), float(future["cutoff_timestamp"])),
            (json.loads(row["next_post_timestamps"]), float(profile["cutoff_timestamp"])),
        )
        edge_dispersion = max(max(stamps) - min(stamps) for stamps, _ in surfaces)
        edge_age = max(cutoff - min(stamps) for stamps, cutoff in surfaces)
        if edge_dispersion < 0 or edge_age < 0:
            raise ValueError("invalid historical synchronization metric")
        dispersions.append(float(edge_dispersion))
        ages.append(float(edge_age))
    if len(dispersions) != len(observation_rows):
        raise ValueError("historical synchronization rows do not match observations")
    tight = sum(d <= 60 and a <= 60 for d, a in zip(dispersions, ages, strict=True))
    loose_only = sum((d <= 600 and a <= 600) and not (d <= 60 and a <= 60) for d, a in zip(dispersions, ages, strict=True))
    return {
        "status": "legacy_600s_diagnostic",
        "configured_max_coordinate_dispersion_seconds": 600,
        "warning": "Ten-minute surfaces may be synthetic and non-simultaneous.",
        "edge_synchronization_aggregates": {
            "edge_count": len(dispersions),
            "coordinate_timestamp_dispersion_seconds": _distribution(dispersions),
            "maximum_coordinate_quote_age_seconds": _distribution(ages),
            "tight_60s_eligible_edge_count": tight,
            "loose_600s_only_edge_count": loose_only,
        },
    }


def _sample_selection(observations: Sequence[Mapping[str, str]], exclusions: Sequence[Mapping[str, str]]) -> dict[str, object]:
    def normalized_reason(row: Mapping[str, str]) -> str:
        reason = row.get("reason") or ""
        if reason == "missing_consecutive_primary_topology":
            return reason
        if reason.startswith("no synchronized quote for token"):
            return "missing_synchronized_quote"
        return "other_quality_or_topology_exclusion"

    reasons = Counter(normalized_reason(row) for row in exclusions)
    usable = len(observations)
    excluded = len(exclusions)
    return {
        "candidate_adjacent_edges": usable + excluded,
        "usable_adjacent_edges": usable,
        "excluded_adjacent_edges": excluded,
        "exclusions_by_primary_reason": dict(sorted(reasons.items())),
    }


def _surprise_support(observations: Sequence[Mapping[str, str]]) -> dict[str, object]:
    values: list[float] = []
    for row in observations:
        probabilities = smooth_support(json.loads(row["current_pre"]), floor=0.005)
        actions = json.loads(row["current_candidate_actions_bp"])
        expected = sum(float(p) * float(a) for p, a in zip(probabilities, actions, strict=True))
        values.append((float(row["realized_action_bp"]) - expected) / 25.0)
    return {
        "scale": "smoothed_dhu_estimator_s25",
        "standard_move_bp": 25.0,
        "observed_min_25bp_units": round(min(values), 12),
        "observed_max_25bp_units": round(max(values), 12),
        "estimator_support_floor": 0.005,
        "comparable_to_live_five_outcome": False,
    }


def build_evidence_summary(run_dir: Path, *, generated_at: str | None = None) -> dict[str, object]:
    verified: VerifiedHistoricalRun = verify_historical_transition_run(run_dir)
    file_hashes = {name: hashlib.sha256(content).hexdigest() for name, content in verified.files.items()}
    if (
        file_hashes["snapshot.json"] != LEGACY_SNAPSHOT_SHA256
        or file_hashes["model.json"] != LEGACY_MODEL_SHA256
        or verified.manifest_sha256 != LEGACY_MANIFEST_SHA256
    ):
        raise ValueError("legacy run does not match the pinned canonical hashes")
    snapshot = _json(verified.files["snapshot.json"], "snapshot.json")
    model = _json(verified.files["model.json"], "model.json")
    observations = _csv(verified.files["observations.csv"])
    exclusions = _csv(verified.files["exclusions.csv"])
    topology = _csv(verified.files["topology-ledger.csv"])
    gates = model.get("production_gates")
    validation = model.get("validation")
    runtime = snapshot.get("runtime_provenance")
    if not isinstance(gates, dict) or tuple(gates.get("failures", ())) != LEGACY_FAILURES or not isinstance(validation, dict) or not isinstance(runtime, dict):
        raise ValueError("canonical model facts do not match the publication contract")
    source_hashes = runtime.get("source_sha256")
    if not isinstance(source_hashes, dict):
        raise ValueError("canonical source provenance is missing")
    moment = generated_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    payload: dict[str, object] = {
        "schema_version": 2,
        "summary_generated_at": moment,
        "summary_builder_version": "evidence-summary-v2",
        "live": {
            "meeting_marginals": {"status": "market_observed", "source_policy": "clob_midpoint_gamma_fallback", "normalization": "proportional_yes_sum"},
            "conditional_response": {"status": "structural_assumption", "model": "five_outcome_persistence_ipf", "model_version": "structural_meeting_persistence_ipf-v1", "exact_enumeration": True, "learned_transition_active": False, "marginals_preserved": True},
        },
        "legacy_canonical_15_30": {
            "status": "diagnostic_only_legacy_window",
            "active_in_tree": False,
            "canonical_cutoff_date": "2026-07-29",
            "estimator_version": "fomc-dhu-v1-15:30",
            "event_target": {"window_role": "full_communications_robustness", "post_cutoff_ny": "15:30"},
            "transition_count": gates["transition_count"],
            "row_counts": gates["row_counts"],
            "unique_meeting_count": validation["unique_meetings"],
            "calendar_start": validation["calendar_span"]["start"],
            "calendar_end": validation["calendar_span"]["end"],
            "sample_selection": _sample_selection(observations, exclusions),
            "surface_synchronization": summarize_surface_synchronization(topology, observations),
            "walk_forward": {"scored_folds": validation["scored_transitions"], "skipped_folds": validation["skipped_transitions"], "status": "unavailable_insufficient_scored_transitions"},
            "dhu_smoothed_surprise_support": _surprise_support(observations),
            "stored_production_gate": {"eligible": False, "failures": list(LEGACY_FAILURES)},
            "limitations": ["total_announcement_conditioned_dependence", "timing_destination_not_identified", "adjacent_edge_only", "serially_dependent_transitions", "no_realized_hike_rows", "dhu_support_not_comparable_to_live_five_outcome"],
            "provenance": {
                "snapshot_sha256": LEGACY_SNAPSHOT_SHA256,
                "model_sha256": LEGACY_MODEL_SHA256,
                "config_canonical_sha256": LEGACY_CONFIG_CANONICAL_SHA256,
                "config_file_sha256": LEGACY_CONFIG_FILE_SHA256,
                "manifest_sha256": LEGACY_MANIFEST_SHA256,
                "manifest_schema_version": 1,
                "snapshot_schema_version": snapshot["schema_version"],
                "model_schema_version": model["schema_version"],
                "code_commit_sha": None,
                "code_commit_status": "unavailable_legacy_artifact_source_hashes_retained",
                "source_sha256": source_hashes,
                "snapshot_fetched_at": snapshot["fetched_at"],
                "run_generated_at": "2026-08-29T00:18:36.831584Z",
                "data_cutoff_at": "2026-07-29T15:30:00-04:00",
            },
        },
        "stage1_primary_14_15": {
            "status": "new_run_required",
            "active_in_tree": False,
            "estimator_version": "fomc-native5-primary-14:15-v1",
            "event_target": {"window_role": "primary_action_window", "post_cutoff_ny": "14:15", "lower_bound": "strictly_after_recorded_official_decision_timestamp"},
            "sample_selection": None,
            "surface_synchronization": {"status": "pending_new_run", "primary_max_coordinate_dispersion_seconds": 60, "primary_max_quote_age_seconds": 60, "edge_synchronization_aggregates": None},
            "native_five_outcome_surprise_support": {"status": "pending_new_run", "scale": "unsmoothed_native_five_outcome_s25", "standard_move_bp": 25.0, "observed_min_25bp_units": None, "observed_max_25bp_units": None},
            "walk_forward": None,
            "stored_production_gate": None,
            "provenance": None,
        },
        "stage1_reproducibility_gates": {"status": "pending_new_run", "names": ["manifest_valid", "provenance_complete", "primary_14_15_timing_valid", "native_five_outcome_support_status_resolved", "synchronization_aggregates_complete", "synthetic_replay_tests_pass"]},
        "exploratory_support_assessment": {"status": "not_created_stage1a", "design_artifact": None, "eligible_for_activation": False},
        "prospective_activation_gate": {"status": "not_registered", "active": False, "required_design": "future_versioned_shadow_or_holdout_registered_before_new_meetings_resolve"},
        "frictions": {"probabilities_fee_adjusted": False, "spread_adjusted": False, "slippage_adjusted": False, "funding_adjusted": False, "rewards_adjusted": False, "interpretation": "market context only"},
        "futures_benchmark": {"status": "not_connected", "comparison_claim_allowed": False, "required_source": "licensed_cme_fedwatch_eod_api", "redistribution_clearance": False},
    }
    validate_evidence_summary(payload)
    return payload


def _expected_public_summary(generated_at: str) -> dict[str, object]:
    """Return the only public Stage 1A evidence shape; values are deliberately pinned."""
    return {
        "schema_version": 2,
        "summary_generated_at": generated_at,
        "summary_builder_version": "evidence-summary-v2",
        "live": {
            "meeting_marginals": {"status": "market_observed", "source_policy": "clob_midpoint_gamma_fallback", "normalization": "proportional_yes_sum"},
            "conditional_response": {"status": "structural_assumption", "model": "five_outcome_persistence_ipf", "model_version": "structural_meeting_persistence_ipf-v1", "exact_enumeration": True, "learned_transition_active": False, "marginals_preserved": True},
        },
        "legacy_canonical_15_30": {
            "status": "diagnostic_only_legacy_window", "active_in_tree": False, "canonical_cutoff_date": "2026-07-29", "estimator_version": "fomc-dhu-v1-15:30",
            "event_target": {"window_role": "full_communications_robustness", "post_cutoff_ny": "15:30"},
            "transition_count": 13, "row_counts": {"down": 3, "unchanged": 10, "up": 0}, "unique_meeting_count": 15,
            "calendar_start": "2024-07-31", "calendar_end": "2026-07-29",
            "sample_selection": {"candidate_adjacent_edges": 35, "usable_adjacent_edges": 13, "excluded_adjacent_edges": 22, "exclusions_by_primary_reason": {"missing_consecutive_primary_topology": 21, "missing_synchronized_quote": 1}},
            "surface_synchronization": {"status": "legacy_600s_diagnostic", "configured_max_coordinate_dispersion_seconds": 600, "warning": "Ten-minute surfaces may be synthetic and non-simultaneous.", "edge_synchronization_aggregates": {"edge_count": 13, "coordinate_timestamp_dispersion_seconds": {"median": 1.0, "p90": 4.0, "max": 8.0}, "maximum_coordinate_quote_age_seconds": {"median": 55.0, "p90": 58.0, "max": 59.0}, "tight_60s_eligible_edge_count": 13, "loose_600s_only_edge_count": 0}},
            "walk_forward": {"scored_folds": 0, "skipped_folds": 13, "status": "unavailable_insufficient_scored_transitions"},
            "dhu_smoothed_surprise_support": {"scale": "smoothed_dhu_estimator_s25", "standard_move_bp": 25.0, "observed_min_25bp_units": -0.030363364858, "observed_max_25bp_units": 0.028250857496, "estimator_support_floor": 0.005, "comparable_to_live_five_outcome": False},
            "stored_production_gate": {"eligible": False, "failures": list(LEGACY_FAILURES)},
            "limitations": ["total_announcement_conditioned_dependence", "timing_destination_not_identified", "adjacent_edge_only", "serially_dependent_transitions", "no_realized_hike_rows", "dhu_support_not_comparable_to_live_five_outcome"],
            "provenance": {"snapshot_sha256": LEGACY_SNAPSHOT_SHA256, "model_sha256": LEGACY_MODEL_SHA256, "config_canonical_sha256": LEGACY_CONFIG_CANONICAL_SHA256, "config_file_sha256": LEGACY_CONFIG_FILE_SHA256, "manifest_sha256": LEGACY_MANIFEST_SHA256, "manifest_schema_version": 1, "snapshot_schema_version": 1, "model_schema_version": 1, "code_commit_sha": None, "code_commit_status": "unavailable_legacy_artifact_source_hashes_retained", "source_sha256": LEGACY_SOURCE_SHA256, "snapshot_fetched_at": "2026-08-29T00:18:35.902772Z", "run_generated_at": "2026-08-29T00:18:36.831584Z", "data_cutoff_at": "2026-07-29T15:30:00-04:00"},
        },
        "stage1_primary_14_15": {"status": "new_run_required", "active_in_tree": False, "estimator_version": "fomc-native5-primary-14:15-v1", "event_target": {"window_role": "primary_action_window", "post_cutoff_ny": "14:15", "lower_bound": "strictly_after_recorded_official_decision_timestamp"}, "sample_selection": None, "surface_synchronization": {"status": "pending_new_run", "primary_max_coordinate_dispersion_seconds": 60, "primary_max_quote_age_seconds": 60, "edge_synchronization_aggregates": None}, "native_five_outcome_surprise_support": {"status": "pending_new_run", "scale": "unsmoothed_native_five_outcome_s25", "standard_move_bp": 25.0, "observed_min_25bp_units": None, "observed_max_25bp_units": None}, "walk_forward": None, "stored_production_gate": None, "provenance": None},
        "stage1_reproducibility_gates": {"status": "pending_new_run", "names": ["manifest_valid", "provenance_complete", "primary_14_15_timing_valid", "native_five_outcome_support_status_resolved", "synchronization_aggregates_complete", "synthetic_replay_tests_pass"]},
        "exploratory_support_assessment": {"status": "not_created_stage1a", "design_artifact": None, "eligible_for_activation": False},
        "prospective_activation_gate": {"status": "not_registered", "active": False, "required_design": "future_versioned_shadow_or_holdout_registered_before_new_meetings_resolve"},
        "frictions": {"probabilities_fee_adjusted": False, "spread_adjusted": False, "slippage_adjusted": False, "funding_adjusted": False, "rewards_adjusted": False, "interpretation": "market context only"},
        "futures_benchmark": {"status": "not_connected", "comparison_claim_allowed": False, "required_source": "licensed_cme_fedwatch_eod_api", "redistribution_clearance": False},
    }


def _assert_exact(actual: object, expected: object, path: str = "$") -> None:
    if type(actual) is not type(expected):
        raise ValueError(f"public evidence type is invalid at {path}")
    if isinstance(expected, dict):
        if set(actual) != set(expected):
            raise ValueError(f"public evidence keys are invalid at {path}")
        for key in expected:
            _assert_exact(actual[key], expected[key], f"{path}.{key}")
    elif isinstance(expected, list):
        if len(actual) != len(expected):
            raise ValueError(f"public evidence list is invalid at {path}")
        for index, item in enumerate(expected):
            _assert_exact(actual[index], item, f"{path}[{index}]")
    elif isinstance(expected, float):
        if not math.isfinite(actual) or actual != expected:
            raise ValueError(f"public evidence number is invalid at {path}")
    elif actual != expected:
        raise ValueError(f"public evidence value is invalid at {path}")


def validate_evidence_summary(payload: Mapping[str, object]) -> None:
    if not isinstance(payload, dict):
        raise ValueError("public evidence must be a plain object")
    moment = payload.get("summary_generated_at")
    if not isinstance(moment, str) or not RFC3339_UTC.fullmatch(moment):
        raise ValueError("public evidence generation time is invalid")
    _assert_exact(payload, _expected_public_summary(moment))
    encoded = canonical_json(dict(payload))
    if len(encoded) > MAX_PUBLIC_BYTES:
        raise ValueError("public evidence summary exceeds 16 KiB")
    if any(marker in encoded for marker in (b"/Users/", b"/home/runner/", b"token_ids", b"theta")):
        raise ValueError("public evidence summary contains private fields")


def write_evidence_summary_atomic(path: Path, payload: Mapping[str, object]) -> None:
    validate_evidence_summary(payload)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_bytes(canonical_json(dict(payload)))
    os.replace(temporary, target)
