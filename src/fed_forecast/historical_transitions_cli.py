"""CLI for the leakage-safe resolved-FOMC transition diagnostic."""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from typing import TextIO

from .historical_transitions import HistoricalTransitionError, TransitionObservation, build_historical_transition_result, fit_potential
from .historical_transitions_client import (
    HistoricalConfig,
    HistoricalConfigError,
    HistoricalFetchError,
    HistoricalIntegrityError,
    HistoricalSnapshotError,
    HistoricalTransitionsClient,
    load_historical_config,
    load_historical_snapshot,
    reconstruct_observations,
)
from .historical_transitions_evidence import build_evidence_summary, write_evidence_summary_atomic
from .historical_transitions_reporting import write_historical_transition_run


DEFAULT_CONFIG = Path("config/historical_transitions.json")
DEFAULT_OUTPUT = Path("outputs/historical-transitions")
ClientFactory = Callable[[HistoricalConfig], HistoricalTransitionsClient]


def _robustness_diagnostics(
    observations: Sequence[TransitionObservation],
    topology_rows: Sequence[object],
    exclusion_rows: Sequence[object],
    config: HistoricalConfig,
) -> dict[str, object]:
    diagnostics = [row for row in topology_rows if isinstance(row, dict) and row.get("record_type") == "surface_diagnostic"]
    by_pair = {(item.current_meeting_id, item.next_meeting_id): item for item in observations}
    strict: list[TransitionObservation] = []
    fallback_free: list[TransitionObservation] = []
    decision_window: list[TransitionObservation] = []
    profile_support: Counter[str] = Counter()
    for row in diagnostics:
        key = (str(row.get("current_meeting_id")), str(row.get("next_meeting_id")))
        observation = by_pair.get(key)
        if observation is None:
            continue
        if row.get("strict_raw_total_bounds_passed") is True:
            strict.append(observation)
        if not row.get("child_action_fallback_categories"):
            fallback_free.append(observation)
        profile = row.get("event_time_profile")
        if isinstance(profile, dict):
            for cutoff, surface in profile.items():
                if isinstance(surface, dict) and surface.get("error") is None:
                    profile_support[str(cutoff)] += 1
            decision = profile.get("14:15")
            probabilities = decision.get("category_probabilities") if isinstance(decision, dict) else None
            if isinstance(probabilities, list) and len(probabilities) == 3:
                decision_window.append(replace(observation, next_post=(float(str(probabilities[0])), float(str(probabilities[1])), float(str(probabilities[2])))))

    def sensitivity(sample: Sequence[TransitionObservation]) -> dict[str, object]:
        if not sample:
            return {"status": "unavailable_no_observations", "transition_count": 0}
        model = fit_potential(
            sample,
            penalty=config.penalty,
            support_floor=config.support_floor,
            step_size=float(str(config.optimizer["initial_step"])),
            tolerance=float(str(config.optimizer["tolerance"])),
            max_iterations=int(str(config.optimizer["max_iterations"])),
            ipf_tolerance=float(str(config.ipf["tolerance"])),
            ipf_max_iterations=int(str(config.ipf["max_iterations"])),
        )
        return {"status": "estimated_diagnostic_only", "transition_count": len(sample), "model": model.to_dict()}

    topology = [row for row in topology_rows if isinstance(row, dict) and row.get("record_type") != "surface_diagnostic"]
    legacy_slugs = {str(row.get("event_slug")) for row in topology if row.get("topology_cohort") == "legacy"}
    primary_slugs = {str(row.get("event_slug")) for row in topology if row.get("topology_cohort") == "primary"}
    return {
        "event_time_profile_support": dict(sorted(profile_support.items())),
        "decision_window_1415": sensitivity(decision_window),
        "strict_raw_total_bounds": sensitivity(strict),
        "exclude_child_action_fallbacks": sensitivity(fallback_free),
        "official_realized_action_bp_counts": dict(sorted(Counter(str(item.realized_action_bp) for item in observations).items())),
        "cohorts": {
            "primary_event_count": len(primary_slugs),
            "legacy_event_count": len(legacy_slugs),
            "legacy_sensitivity": "not_estimated_topology_not_comparable",
            "all_mechanically_eligible_sensitivity": {
                "status": "equivalent_to_primary_fit_no_additional_eligible_cohort",
                "mechanically_eligible_event_count": len(primary_slugs),
                "usable_adjacent_transition_count": len(observations),
                "model": sensitivity(observations),
                "excluded_transition_count": len([row for row in exclusion_rows if isinstance(row, dict)]),
            },
        },
        "direct_path_audit": {"status": "not_counted_as_transition", "independent_observation_count": 0},
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fed-forecast historical-transitions",
        description="Reconstruct or verify the inactive resolved-FOMC transition diagnostic.",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--snapshot-input", type=Path, help="Replay a saved snapshot without network access.")
    parser.add_argument("--verify-run", type=Path, help="Verify the pinned immutable legacy run.")
    parser.add_argument("--evidence-output", type=Path, help="Write the allowlisted compact public summary.")
    return parser


def _client_factory(config: HistoricalConfig) -> HistoricalTransitionsClient:
    del config
    return HistoricalTransitionsClient()


def _validate_option_matrix(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if args.verify_run is not None:
        if args.snapshot_input is not None or args.evidence_output is None:
            parser.error("--verify-run requires --evidence-output and cannot be combined with --snapshot-input")
    elif args.evidence_output is not None:
        parser.error("--evidence-output is valid only with --verify-run")


def historical_transitions_main(
    argv: Sequence[str],
    *,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    client_factory: ClientFactory = _client_factory,
) -> int:
    parser = _parser()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            args = parser.parse_args(list(argv))
            _validate_option_matrix(args, parser)
    except SystemExit as error:
        return int(error.code) if isinstance(error.code, int) else 2
    try:
        if args.verify_run is not None:
            summary = build_evidence_summary(args.verify_run)
            write_evidence_summary_atomic(args.evidence_output, summary)
            print(f"Verified legacy run: {args.verify_run}", file=stdout)
            print(f"Evidence summary: {args.evidence_output}", file=stdout)
            return 0
        config = load_historical_config(args.config)
        snapshot = (
            load_historical_snapshot(args.snapshot_input, config)
            if args.snapshot_input is not None
            else client_factory(config).fetch_snapshot(config)
        )
        observations, topology_rows, exclusion_rows = reconstruct_observations(snapshot, config)
        optimizer = config.optimizer
        ipf = config.ipf
        result = build_historical_transition_result(
            observations,
            penalty=config.penalty,
            support_floor=config.support_floor,
            penalty_sensitivities=config.penalty_sensitivity,
            provenance_valid=True,
            replay_valid=False,
            walk_forward_min_training=int(str(config.walk_forward["minimum_training"])),
            walk_forward_min_per_row=int(str(config.walk_forward["minimum_per_row"])),
            production_min_transitions=int(str(config.production_gates["minimum_transitions"])),
            production_min_per_row=int(str(config.production_gates["minimum_per_row"])),
            production_max_condition_number=float(str(config.production_gates["maximum_condition_number"])),
            step_size=float(str(optimizer["initial_step"])),
            tolerance=float(str(optimizer["tolerance"])),
            max_iterations=int(str(optimizer["max_iterations"])),
            ipf_tolerance=float(str(ipf["tolerance"])),
            ipf_max_iterations=int(str(ipf["max_iterations"])),
        )
        model_payload = result.to_dict()
        model_payload["robustness_diagnostics"] = _robustness_diagnostics(
            observations,
            topology_rows,
            exclusion_rows,
            config,
        )
        run_dir = write_historical_transition_run(
            args.output_dir,
            snapshot,
            observations,
            topology_rows,
            exclusion_rows,
            model_payload,
        )
    except (
        HistoricalConfigError,
        HistoricalFetchError,
        HistoricalIntegrityError,
        HistoricalSnapshotError,
        HistoricalTransitionError,
        OSError,
        TypeError,
        ValueError,
    ) as error:
        print(f"error: {error}", file=stderr)
        return 2
    gates = result.production_gates
    print(f"Historical FOMC transitions: {len(observations)}", file=stdout)
    print(f"Model status: {gates.status}", file=stdout)
    print(f"Gate failures: {', '.join(gates.failures) if gates.failures else 'none'}", file=stdout)
    print(f"Run directory: {run_dir.resolve()}", file=stdout)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    return historical_transitions_main(sys.argv[1:] if argv is None else argv)
