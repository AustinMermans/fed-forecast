"""Pure estimator for historical adjacent-FOMC probability transitions.

This module intentionally contains no network, filesystem, CLI, or live-tree
integration code.  It estimates a diagnostic log potential from already
reconstructed meeting observations and keeps the timing-versus-destination
identification failure explicit in every serialized result.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, replace
from datetime import date
from typing import Iterable, Mapping, Sequence


CATEGORIES = ("down", "unchanged", "up")
_CATEGORY_INDEX = {name: index for index, name in enumerate(CATEGORIES)}
_SCORES = (-1.0, 0.0, 1.0)
_DEFAULT_IDENTIFICATION = "not_identified"

Vector = tuple[float, float, float]
Matrix = tuple[Vector, Vector, Vector]


class HistoricalTransitionError(ValueError):
    """Raised when a transition input or numerical contract is invalid."""


def _finite_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HistoricalTransitionError(f"{name} must be a finite number")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise HistoricalTransitionError(f"{name} must be a finite number")
    return parsed


def _probability_vector(values: Sequence[float], name: str) -> Vector:
    if len(values) != 3:
        raise HistoricalTransitionError(f"{name} must contain D/H/U coordinates")
    parsed = tuple(_finite_number(value, f"{name}[{index}]") for index, value in enumerate(values))
    if any(value < 0.0 for value in parsed):
        raise HistoricalTransitionError(f"{name} cannot contain negative values")
    total = sum(parsed)
    if total <= 0.0:
        raise HistoricalTransitionError(f"{name} must have positive total support")
    return tuple(value / total for value in parsed)  # type: ignore[return-value]


def _positive(value: object, name: str) -> float:
    parsed = _finite_number(value, name)
    if parsed <= 0.0:
        raise HistoricalTransitionError(f"{name} must be positive")
    return parsed


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise HistoricalTransitionError(f"{name} must be a positive integer")
    return value


def _row_counts(observations: Sequence[TransitionObservation]) -> tuple[int, int, int]:
    counts = [0, 0, 0]
    for observation in observations:
        counts[_CATEGORY_INDEX[observation.realized_category]] += 1
    return tuple(counts)  # type: ignore[return-value]


def _validated_sample(observations: Sequence[TransitionObservation]) -> tuple[TransitionObservation, ...]:
    sample = tuple(observations)
    if not sample:
        raise HistoricalTransitionError("observations must be non-empty")
    if any(not isinstance(item, TransitionObservation) for item in sample):
        raise HistoricalTransitionError("observations must contain TransitionObservation values")
    current_ids = [item.current_meeting_id for item in sample]
    pairs = [(item.current_meeting_id, item.next_meeting_id) for item in sample]
    if len(set(current_ids)) != len(current_ids) or len(set(pairs)) != len(pairs):
        raise HistoricalTransitionError("observations must contain one row per unique transition")
    return sample


@dataclass(frozen=True)
class TransitionObservation:
    """One current-meeting to next-meeting transition.

    Probability fields are normalized defensively by estimator functions rather
    than mutated here, so ``to_dict`` preserves the reconstruction-layer input.
    ``realized_action_bp`` is diagnostic only: the ex-ante IPF joint always uses
    candidate surprises for all three rows.
    """

    current_meeting_id: str
    next_meeting_id: str
    current_meeting_date: date
    next_meeting_date: date
    current_pre: Vector
    current_candidate_actions_bp: Vector
    realized_category: str
    realized_action_bp: float
    next_pre: Vector
    next_post: Vector
    topology_cohort: str = "primary_negrisk"
    next_realized_category: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.current_meeting_id, str) or not self.current_meeting_id:
            raise HistoricalTransitionError("current_meeting_id must be a non-empty string")
        if not isinstance(self.next_meeting_id, str) or not self.next_meeting_id:
            raise HistoricalTransitionError("next_meeting_id must be a non-empty string")
        if self.current_meeting_id == self.next_meeting_id:
            raise HistoricalTransitionError("current and next meeting ids must differ")
        if not isinstance(self.current_meeting_date, date) or not isinstance(self.next_meeting_date, date):
            raise HistoricalTransitionError("meeting dates must be date values")
        if self.next_meeting_date <= self.current_meeting_date:
            raise HistoricalTransitionError("next meeting date must follow current meeting date")
        _probability_vector(self.current_pre, "current_pre")
        _probability_vector(self.next_pre, "next_pre")
        _probability_vector(self.next_post, "next_post")
        actions = tuple(
            _finite_number(value, f"current_candidate_actions_bp[{index}]")
            for index, value in enumerate(self.current_candidate_actions_bp)
        )
        if len(actions) != 3:
            raise HistoricalTransitionError("current_candidate_actions_bp must contain D/H/U values")
        if not actions[0] < actions[1] < actions[2]:
            raise HistoricalTransitionError("candidate actions must be strictly ordered down/unchanged/up")
        _finite_number(self.realized_action_bp, "realized_action_bp")
        if self.realized_category not in _CATEGORY_INDEX:
            raise HistoricalTransitionError("realized_category must be down, unchanged, or up")
        if self.next_realized_category is not None and self.next_realized_category not in _CATEGORY_INDEX:
            raise HistoricalTransitionError("next_realized_category must be down, unchanged, up, or None")
        if not isinstance(self.topology_cohort, str) or not self.topology_cohort:
            raise HistoricalTransitionError("topology_cohort must be a non-empty string")
        object.__setattr__(self, "current_pre", tuple(float(value) for value in self.current_pre))
        object.__setattr__(
            self,
            "current_candidate_actions_bp",
            tuple(float(value) for value in self.current_candidate_actions_bp),
        )
        object.__setattr__(self, "realized_action_bp", float(self.realized_action_bp))
        object.__setattr__(self, "next_pre", tuple(float(value) for value in self.next_pre))
        object.__setattr__(self, "next_post", tuple(float(value) for value in self.next_post))

    def to_dict(self) -> dict[str, object]:
        return {
            "current_meeting_id": self.current_meeting_id,
            "next_meeting_id": self.next_meeting_id,
            "current_meeting_date": self.current_meeting_date.isoformat(),
            "next_meeting_date": self.next_meeting_date.isoformat(),
            "current_pre": list(self.current_pre),
            "current_pre_raw": list(self.current_pre),
            "current_candidate_actions_bp": list(self.current_candidate_actions_bp),
            "realized_category": self.realized_category,
            "realized_action_bp": self.realized_action_bp,
            "next_pre": list(self.next_pre),
            "next_pre_raw": list(self.next_pre),
            "next_post": list(self.next_post),
            "next_post_raw": list(self.next_post),
            "topology_cohort": self.topology_cohort,
            "next_realized_category": self.next_realized_category,
        }


def smooth_support(probabilities: Sequence[float], *, floor: float = 0.005) -> Vector:
    """Normalize, floor each D/H/U coordinate, and renormalize."""

    floor = _positive(floor, "floor")
    if floor >= 1.0 / 3.0:
        raise HistoricalTransitionError("floor must be below one third")
    normalized = _probability_vector(probabilities, "probabilities")
    floored = tuple(max(floor, value) for value in normalized)
    total = sum(floored)
    return tuple(value / total for value in floored)  # type: ignore[return-value]


def candidate_surprises(
    observation: TransitionObservation,
    *,
    standard_move_bp: float = 25.0,
    support_floor: float = 0.005,
) -> Vector:
    """Return ex-ante D/H/U candidate surprises in standard-move units."""

    standard_move_bp = _positive(standard_move_bp, "standard_move_bp")
    current = smooth_support(observation.current_pre, floor=support_floor)
    expected_action = sum(
        probability * action
        for probability, action in zip(current, observation.current_candidate_actions_bp, strict=True)
    )
    return tuple(
        (float(action) - expected_action) / standard_move_bp
        for action in observation.current_candidate_actions_bp
    )  # type: ignore[return-value]


def realized_surprise(
    observation: TransitionObservation,
    *,
    standard_move_bp: float = 25.0,
    support_floor: float = 0.005,
) -> float:
    """Return the exact realized surprise for diagnostics only."""

    standard_move_bp = _positive(standard_move_bp, "standard_move_bp")
    current = smooth_support(observation.current_pre, floor=support_floor)
    expected_action = sum(
        probability * action
        for probability, action in zip(current, observation.current_candidate_actions_bp, strict=True)
    )
    return (float(observation.realized_action_bp) - expected_action) / standard_move_bp


def _validate_theta(theta: Sequence[Sequence[float]], *, require_centered: bool = True) -> Matrix:
    if len(theta) != 3 or any(len(row) != 3 for row in theta):
        raise HistoricalTransitionError("theta must be a 3x3 matrix")
    parsed = tuple(
        tuple(_finite_number(value, f"theta[{row_index}][{column_index}]") for column_index, value in enumerate(row))
        for row_index, row in enumerate(theta)
    )
    if require_centered and any(abs(sum(row)) > 1e-9 for row in parsed):
        raise HistoricalTransitionError("theta rows must be centered")
    return parsed  # type: ignore[return-value]


def _theta_from_parameters(parameters: Sequence[float]) -> Matrix:
    if len(parameters) != 6:
        raise HistoricalTransitionError("potential parameter vector must have six entries")
    values = tuple(_finite_number(value, f"parameter[{index}]") for index, value in enumerate(parameters))
    rows = []
    for index in range(0, 6, 2):
        first, second = values[index], values[index + 1]
        rows.append((first, second, -first - second))
    return tuple(rows)  # type: ignore[return-value]


def ipf_joint(
    current_pre: Sequence[float],
    next_pre: Sequence[float],
    surprises: Sequence[float],
    theta: Sequence[Sequence[float]],
    *,
    support_floor: float = 0.005,
    tolerance: float = 1e-12,
    max_iterations: int = 10_000,
) -> Matrix:
    """Create the coherent 3x3 joint with exact smoothed row/column marginals."""

    row_target = smooth_support(current_pre, floor=support_floor)
    column_target = smooth_support(next_pre, floor=support_floor)
    if len(surprises) != 3:
        raise HistoricalTransitionError("surprises must contain D/H/U values")
    parsed_surprises = tuple(
        _finite_number(value, f"surprises[{index}]") for index, value in enumerate(surprises)
    )
    parsed_theta = _validate_theta(theta)
    tolerance = _positive(tolerance, "tolerance")
    max_iterations = _positive_int(max_iterations, "max_iterations")

    weights = [
        [
            row_target[row] * column_target[column]
            * math.exp(max(-50.0, min(50.0, parsed_surprises[row] * parsed_theta[row][column])))
            for column in range(3)
        ]
        for row in range(3)
    ]
    for _ in range(max_iterations):
        for row in range(3):
            total = sum(weights[row])
            if total <= 0.0 or not math.isfinite(total):
                raise HistoricalTransitionError("IPF produced invalid row support")
            factor = row_target[row] / total
            weights[row] = [value * factor for value in weights[row]]
        for column in range(3):
            total = sum(weights[row][column] for row in range(3))
            if total <= 0.0 or not math.isfinite(total):
                raise HistoricalTransitionError("IPF produced invalid column support")
            factor = column_target[column] / total
            for row in range(3):
                weights[row][column] *= factor
        row_error = max(abs(sum(weights[row]) - row_target[row]) for row in range(3))
        column_error = max(
            abs(sum(weights[row][column] for row in range(3)) - column_target[column])
            for column in range(3)
        )
        if max(row_error, column_error) <= tolerance:
            return tuple(tuple(row) for row in weights)  # type: ignore[return-value]
    raise HistoricalTransitionError("IPF did not converge")


def apply_potential(
    observation: TransitionObservation,
    theta: Sequence[Sequence[float]],
    *,
    support_floor: float = 0.005,
    standard_move_bp: float = 25.0,
    ipf_tolerance: float = 1e-12,
    ipf_max_iterations: int = 10_000,
) -> Vector:
    """Predict the next post surface conditional on the realized D/H/U row."""

    surprises = candidate_surprises(
        observation,
        standard_move_bp=standard_move_bp,
        support_floor=support_floor,
    )
    joint = ipf_joint(
        observation.current_pre,
        observation.next_pre,
        surprises,
        theta,
        support_floor=support_floor,
        tolerance=ipf_tolerance,
        max_iterations=ipf_max_iterations,
    )
    row = joint[_CATEGORY_INDEX[observation.realized_category]]
    total = sum(row)
    if total <= 0.0:
        raise HistoricalTransitionError("realized IPF row has no support")
    return tuple(value / total for value in row)  # type: ignore[return-value]


def no_update_prediction(
    observation: TransitionObservation, *, support_floor: float = 0.005
) -> Vector:
    """Return the smoothed next-meeting pre surface."""

    return smooth_support(observation.next_pre, floor=support_floor)


def scalar_persistence_prediction(
    observation: TransitionObservation,
    *,
    strength: float = 0.35,
    support_floor: float = 0.005,
    ipf_tolerance: float = 1e-12,
    ipf_max_iterations: int = 10_000,
) -> Vector:
    """Return the existing mirrored scalar-persistence rule as a benchmark."""

    strength = _finite_number(strength, "strength")
    current = smooth_support(observation.current_pre, floor=support_floor)
    future = smooth_support(observation.next_pre, floor=support_floor)
    weights = [
        [
            current[row] * future[column] * math.exp(strength * _SCORES[row] * _SCORES[column])
            for column in range(3)
        ]
        for row in range(3)
    ]
    tolerance = _positive(ipf_tolerance, "ipf_tolerance")
    max_iterations = _positive_int(ipf_max_iterations, "ipf_max_iterations")
    for _ in range(max_iterations):
        for row in range(3):
            factor = current[row] / sum(weights[row])
            weights[row] = [value * factor for value in weights[row]]
        for column in range(3):
            factor = future[column] / sum(weights[row][column] for row in range(3))
            for row in range(3):
                weights[row][column] *= factor
        if max(
            max(abs(sum(weights[row]) - current[row]) for row in range(3)),
            max(
                abs(sum(weights[row][column] for row in range(3)) - future[column])
                for column in range(3)
            ),
        ) <= tolerance:
            realized = weights[_CATEGORY_INDEX[observation.realized_category]]
            total = sum(realized)
            return tuple(value / total for value in realized)  # type: ignore[return-value]
    raise HistoricalTransitionError("scalar benchmark IPF did not converge")


@dataclass(frozen=True)
class SurfaceMetrics:
    cross_entropy: float
    kl_divergence: float
    total_variation: float

    def to_dict(self) -> dict[str, float]:
        return {
            "cross_entropy": self.cross_entropy,
            "kl_divergence": self.kl_divergence,
            "total_variation": self.total_variation,
        }


def surface_metrics(
    predicted: Sequence[float],
    observed: Sequence[float],
    *,
    support_floor: float = 0.005,
) -> SurfaceMetrics:
    """Score a prediction against the normalized raw post surface."""

    prediction = smooth_support(predicted, floor=support_floor)
    target = _probability_vector(observed, "observed")
    cross_entropy = -sum(
        probability * math.log(prediction[index])
        for index, probability in enumerate(target)
        if probability > 0.0
    )
    target_entropy = -sum(probability * math.log(probability) for probability in target if probability > 0.0)
    return SurfaceMetrics(
        cross_entropy=cross_entropy,
        kl_divergence=max(0.0, cross_entropy - target_entropy),
        total_variation=0.5 * sum(abs(prediction[index] - target[index]) for index in range(3)),
    )


def _secondary_scores(predicted: Vector, outcome: str | None) -> tuple[float | None, float | None]:
    if outcome is None:
        return None, None
    outcome_index = _CATEGORY_INDEX[outcome]
    brier = sum(
        (predicted[index] - (1.0 if index == outcome_index else 0.0)) ** 2
        for index in range(3)
    )
    return brier, -math.log(predicted[outcome_index])


def _objective(
    observations: Sequence[TransitionObservation],
    parameters: Sequence[float],
    *,
    penalty: float,
    support_floor: float,
    standard_move_bp: float,
    ipf_tolerance: float,
    ipf_max_iterations: int,
) -> float:
    theta = _theta_from_parameters(parameters)
    cross_entropy = sum(
        surface_metrics(
            apply_potential(
                observation,
                theta,
                support_floor=support_floor,
                standard_move_bp=standard_move_bp,
                ipf_tolerance=ipf_tolerance,
                ipf_max_iterations=ipf_max_iterations,
            ),
            observation.next_post,
            support_floor=support_floor,
        ).cross_entropy
        for observation in observations
    ) / len(observations)
    return cross_entropy + penalty * sum(value * value for row in theta for value in row)


@dataclass(frozen=True)
class HistoricalTransitionModel:
    theta: Matrix
    penalty: float
    support_floor: float
    standard_move_bp: float
    training_count: int
    row_counts: tuple[int, int, int]
    objective: float
    iterations: int
    converged: bool
    initial_step_size: float
    final_step_size: float
    tolerance: float
    max_iterations: int
    ipf_tolerance: float
    ipf_max_iterations: int
    timing_destination_identification: str = _DEFAULT_IDENTIFICATION

    def to_dict(self) -> dict[str, object]:
        return {
            "category_order": list(CATEGORIES),
            "theta": [list(row) for row in self.theta],
            "penalty": self.penalty,
            "support_floor": self.support_floor,
            "standard_move_bp": self.standard_move_bp,
            "training_count": self.training_count,
            "row_counts": dict(zip(CATEGORIES, self.row_counts, strict=True)),
            "objective": self.objective,
            "optimizer": {
                "kind": "deterministic_coordinate_descent",
                "iterations": self.iterations,
                "converged": self.converged,
                "initial_step_size": self.initial_step_size,
                "final_step_size": self.final_step_size,
                "tolerance": self.tolerance,
                "max_iterations": self.max_iterations,
            },
            "ipf": {
                "tolerance": self.ipf_tolerance,
                "max_iterations": self.ipf_max_iterations,
            },
            "timing_destination_identification": self.timing_destination_identification,
            "adjacent_edge_only": True,
            "directional_mirroring": False,
        }


def fit_potential(
    observations: Sequence[TransitionObservation],
    *,
    penalty: float = 5.0,
    support_floor: float = 0.005,
    standard_move_bp: float = 25.0,
    step_size: float = 0.25,
    tolerance: float = 1e-9,
    max_iterations: int = 500,
    ipf_tolerance: float = 1e-12,
    ipf_max_iterations: int = 10_000,
) -> HistoricalTransitionModel:
    """Fit a row-centered 3x3 potential with deterministic coordinate descent."""

    sample = _validated_sample(observations)
    penalty = _finite_number(penalty, "penalty")
    if penalty < 0.0:
        raise HistoricalTransitionError("penalty cannot be negative")
    support_floor = _positive(support_floor, "support_floor")
    standard_move_bp = _positive(standard_move_bp, "standard_move_bp")
    initial_step = _positive(step_size, "step_size")
    tolerance = _positive(tolerance, "tolerance")
    max_iterations = _positive_int(max_iterations, "max_iterations")
    ipf_tolerance = _positive(ipf_tolerance, "ipf_tolerance")
    ipf_max_iterations = _positive_int(ipf_max_iterations, "ipf_max_iterations")

    ordered = tuple(
        sorted(
            sample,
            key=lambda item: (
                item.current_meeting_date,
                item.next_meeting_date,
                item.current_meeting_id,
                item.next_meeting_id,
            ),
        )
    )
    parameters = [0.0] * 6
    step = initial_step
    best = _objective(
        ordered,
        parameters,
        penalty=penalty,
        support_floor=support_floor,
        standard_move_bp=standard_move_bp,
        ipf_tolerance=ipf_tolerance,
        ipf_max_iterations=ipf_max_iterations,
    )
    iterations = 0
    while iterations < max_iterations and step >= tolerance:
        improved = False
        for coordinate in range(6):
            chosen_parameters = parameters
            chosen_objective = best
            for direction in (1.0, -1.0):
                proposal = list(parameters)
                proposal[coordinate] += direction * step
                value = _objective(
                    ordered,
                    proposal,
                    penalty=penalty,
                    support_floor=support_floor,
                    standard_move_bp=standard_move_bp,
                    ipf_tolerance=ipf_tolerance,
                    ipf_max_iterations=ipf_max_iterations,
                )
                if value < chosen_objective - 1e-15:
                    chosen_parameters = proposal
                    chosen_objective = value
            if chosen_objective < best - 1e-15:
                parameters = list(chosen_parameters)
                best = chosen_objective
                improved = True
        if not improved:
            step *= 0.5
        iterations += 1
    theta = _theta_from_parameters(parameters)
    return HistoricalTransitionModel(
        theta=theta,
        penalty=penalty,
        support_floor=support_floor,
        standard_move_bp=standard_move_bp,
        training_count=len(ordered),
        row_counts=_row_counts(ordered),
        objective=best,
        iterations=iterations,
        converged=step < tolerance,
        initial_step_size=initial_step,
        final_step_size=step,
        tolerance=tolerance,
        max_iterations=max_iterations,
        ipf_tolerance=ipf_tolerance,
        ipf_max_iterations=ipf_max_iterations,
    )


@dataclass(frozen=True)
class BenchmarkMetrics:
    label: str
    prediction: Vector
    surface: SurfaceMetrics
    brier: float | None
    log_loss: float | None

    def to_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "prediction": list(self.prediction),
            "surface": self.surface.to_dict(),
            "secondary_eventual_outcome": {
                "brier": self.brier,
                "log_loss": self.log_loss,
            },
        }


@dataclass(frozen=True)
class ValidationFold:
    current_meeting_id: str
    next_meeting_id: str
    training_count: int
    row_counts: tuple[int, int, int]
    status: str
    skip_reason: str | None
    benchmarks: tuple[BenchmarkMetrics, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "current_meeting_id": self.current_meeting_id,
            "next_meeting_id": self.next_meeting_id,
            "training_count": self.training_count,
            "row_counts": dict(zip(CATEGORIES, self.row_counts, strict=True)),
            "status": self.status,
            "skip_reason": self.skip_reason,
            "benchmarks": [item.to_dict() for item in self.benchmarks],
        }


@dataclass(frozen=True)
class AggregateMetrics:
    label: str
    count: int
    cross_entropy: float
    kl_divergence: float
    total_variation: float
    brier: float | None
    log_loss: float | None

    def to_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "count": self.count,
            "cross_entropy": self.cross_entropy,
            "kl_divergence": self.kl_divergence,
            "total_variation": self.total_variation,
            "secondary_eventual_outcome": {
                "brier": self.brier,
                "log_loss": self.log_loss,
            },
        }


@dataclass(frozen=True)
class BootstrapInterval:
    benchmark: str
    metric: str
    lower: float
    upper: float

    def to_dict(self) -> dict[str, object]:
        return {
            "benchmark": self.benchmark,
            "metric": self.metric,
            "lower": self.lower,
            "upper": self.upper,
        }


@dataclass(frozen=True)
class BootstrapResult:
    state: str
    scored_transitions: int
    block_length: int | None
    replicates: int
    seed: int
    intervals: tuple[BootstrapInterval, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "scored_transitions": self.scored_transitions,
            "block_length": self.block_length,
            "replicates": self.replicates,
            "seed": self.seed,
            "intervals": [item.to_dict() for item in self.intervals],
        }


@dataclass(frozen=True)
class WalkForwardResult:
    eligible_transitions: int
    unique_meetings: int
    calendar_start: str
    calendar_end: str
    row_counts: tuple[int, int, int]
    scored_transitions: int
    skipped_transitions: int
    folds: tuple[ValidationFold, ...]
    aggregate: tuple[AggregateMetrics, ...]
    bootstrap: BootstrapResult

    def to_dict(self) -> dict[str, object]:
        return {
            "eligible_transitions": self.eligible_transitions,
            "unique_meetings": self.unique_meetings,
            "calendar_span": {"start": self.calendar_start, "end": self.calendar_end},
            "row_counts": dict(zip(CATEGORIES, self.row_counts, strict=True)),
            "scored_transitions": self.scored_transitions,
            "skipped_transitions": self.skipped_transitions,
            "folds": [item.to_dict() for item in self.folds],
            "aggregate": [item.to_dict() for item in self.aggregate],
            "bootstrap": self.bootstrap.to_dict(),
        }


def _percentile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise HistoricalTransitionError("percentile values must be non-empty")
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _moving_block_bootstrap(
    folds: Sequence[ValidationFold],
    *,
    minimum_count: int = 30,
    replicates: int = 2_000,
    seed: int = 20_260_828,
) -> BootstrapResult:
    count = len(folds)
    if count < minimum_count:
        return BootstrapResult(
            state="unavailable_insufficient_scored_transitions",
            scored_transitions=count,
            block_length=None,
            replicates=replicates,
            seed=seed,
            intervals=(),
        )
    block_length = max(2, round(count ** (1.0 / 3.0)))
    by_fold = [{item.label: item for item in fold.benchmarks} for fold in folds]
    rng = random.Random(seed)
    intervals = []
    for benchmark in ("no_update", "scalar_persistence"):
        for metric in ("cross_entropy", "kl_divergence", "total_variation"):
            differences = [
                getattr(items["historical_potential"].surface, metric)
                - getattr(items[benchmark].surface, metric)
                for items in by_fold
            ]
            estimates = []
            for _ in range(replicates):
                selected: list[float] = []
                while len(selected) < count:
                    start = rng.randint(0, count - block_length)
                    selected.extend(differences[start:start + block_length])
                estimates.append(sum(selected[:count]) / count)
            intervals.append(
                BootstrapInterval(
                    benchmark=benchmark,
                    metric=metric,
                    lower=_percentile(estimates, 0.025),
                    upper=_percentile(estimates, 0.975),
                )
            )
    return BootstrapResult(
        state="available",
        scored_transitions=count,
        block_length=block_length,
        replicates=replicates,
        seed=seed,
        intervals=tuple(intervals),
    )


def walk_forward_validate(
    observations: Sequence[TransitionObservation],
    *,
    min_training: int = 12,
    min_row_training: int = 2,
    penalty: float = 5.0,
    support_floor: float = 0.005,
    standard_move_bp: float = 25.0,
    scalar_strength: float = 0.35,
    step_size: float = 0.25,
    tolerance: float = 1e-9,
    max_iterations: int = 500,
    ipf_tolerance: float = 1e-12,
    ipf_max_iterations: int = 10_000,
    bootstrap_minimum: int = 30,
    bootstrap_replicates: int = 2_000,
    bootstrap_seed: int = 20_260_828,
) -> WalkForwardResult:
    """Run expanding chronological validation with explicit fold support gates."""

    sample = _validated_sample(observations)
    min_training = _positive_int(min_training, "min_training")
    min_row_training = _positive_int(min_row_training, "min_row_training")
    ordered = tuple(
        sorted(
            sample,
            key=lambda item: (
                item.current_meeting_date,
                item.next_meeting_date,
                item.current_meeting_id,
                item.next_meeting_id,
            ),
        )
    )
    folds: list[ValidationFold] = []
    for index, held_out in enumerate(ordered):
        training = ordered[:index]
        counts = _row_counts(training)
        reasons = []
        if len(training) < min_training:
            reasons.append("minimum_training_not_met")
        if any(count < min_row_training for count in counts):
            reasons.append("minimum_row_training_not_met")
        if reasons:
            folds.append(
                ValidationFold(
                    current_meeting_id=held_out.current_meeting_id,
                    next_meeting_id=held_out.next_meeting_id,
                    training_count=len(training),
                    row_counts=counts,
                    status="skipped",
                    skip_reason=";".join(reasons),
                    benchmarks=(),
                )
            )
            continue
        model = fit_potential(
            training,
            penalty=penalty,
            support_floor=support_floor,
            standard_move_bp=standard_move_bp,
            step_size=step_size,
            tolerance=tolerance,
            max_iterations=max_iterations,
            ipf_tolerance=ipf_tolerance,
            ipf_max_iterations=ipf_max_iterations,
        )
        predictions = (
            ("no_update", no_update_prediction(held_out, support_floor=support_floor)),
            (
                "scalar_persistence",
                scalar_persistence_prediction(
                    held_out,
                    strength=scalar_strength,
                    support_floor=support_floor,
                    ipf_tolerance=ipf_tolerance,
                    ipf_max_iterations=ipf_max_iterations,
                ),
            ),
            (
                "historical_potential",
                apply_potential(
                    held_out,
                    model.theta,
                    support_floor=support_floor,
                    standard_move_bp=standard_move_bp,
                    ipf_tolerance=ipf_tolerance,
                    ipf_max_iterations=ipf_max_iterations,
                ),
            ),
        )
        benchmarks = []
        for label, prediction in predictions:
            brier, log_loss = _secondary_scores(prediction, held_out.next_realized_category)
            benchmarks.append(
                BenchmarkMetrics(
                    label=label,
                    prediction=prediction,
                    surface=surface_metrics(prediction, held_out.next_post, support_floor=support_floor),
                    brier=brier,
                    log_loss=log_loss,
                )
            )
        folds.append(
            ValidationFold(
                current_meeting_id=held_out.current_meeting_id,
                next_meeting_id=held_out.next_meeting_id,
                training_count=len(training),
                row_counts=counts,
                status="scored",
                skip_reason=None,
                benchmarks=tuple(benchmarks),
            )
        )
    scored = tuple(fold for fold in folds if fold.status == "scored")
    aggregate = []
    for label in ("no_update", "scalar_persistence", "historical_potential"):
        metrics = [next(item for item in fold.benchmarks if item.label == label) for fold in scored]
        if not metrics:
            continue
        secondary = [item for item in metrics if item.brier is not None and item.log_loss is not None]
        aggregate.append(
            AggregateMetrics(
                label=label,
                count=len(metrics),
                cross_entropy=sum(item.surface.cross_entropy for item in metrics) / len(metrics),
                kl_divergence=sum(item.surface.kl_divergence for item in metrics) / len(metrics),
                total_variation=sum(item.surface.total_variation for item in metrics) / len(metrics),
                brier=(sum(item.brier if item.brier is not None else 0.0 for item in secondary) / len(secondary) if secondary else None),
                log_loss=(sum(item.log_loss if item.log_loss is not None else 0.0 for item in secondary) / len(secondary) if secondary else None),
            )
        )
    meetings = {item.current_meeting_id for item in ordered} | {item.next_meeting_id for item in ordered}
    return WalkForwardResult(
        eligible_transitions=len(ordered),
        unique_meetings=len(meetings),
        calendar_start=min(item.current_meeting_date for item in ordered).isoformat(),
        calendar_end=max(item.next_meeting_date for item in ordered).isoformat(),
        row_counts=_row_counts(ordered),
        scored_transitions=len(scored),
        skipped_transitions=len(ordered) - len(scored),
        folds=tuple(folds),
        aggregate=tuple(aggregate),
        bootstrap=_moving_block_bootstrap(
            scored,
            minimum_count=bootstrap_minimum,
            replicates=bootstrap_replicates,
            seed=bootstrap_seed,
        ),
    )


def _log_odds_vector(
    observation: TransitionObservation,
    theta: Matrix,
    model: HistoricalTransitionModel,
) -> tuple[float, float]:
    prediction = apply_potential(
        observation,
        theta,
        support_floor=model.support_floor,
        standard_move_bp=model.standard_move_bp,
        ipf_tolerance=model.ipf_tolerance,
        ipf_max_iterations=model.ipf_max_iterations,
    )
    return (
        math.log(prediction[0] / prediction[2]),
        math.log(prediction[1] / prediction[2]),
    )


def _jacobi_eigenvalues(matrix: Sequence[Sequence[float]]) -> tuple[float, ...]:
    size = len(matrix)
    values = [list(row) for row in matrix]
    if size == 0 or any(len(row) != size for row in values):
        raise HistoricalTransitionError("eigenvalue input must be square")
    for _ in range(10_000):
        upper = [
            (abs(values[row][column]), row, column)
            for row in range(size)
            for column in range(row + 1, size)
        ]
        maximum, left, right = max(upper, default=(0.0, 0, 0))
        if maximum <= 1e-14:
            break
        angle = 0.5 * math.atan2(
            2.0 * values[left][right],
            values[right][right] - values[left][left],
        )
        cosine, sine = math.cos(angle), math.sin(angle)
        left_diag = values[left][left]
        right_diag = values[right][right]
        cross = values[left][right]
        values[left][left] = cosine * cosine * left_diag - 2.0 * sine * cosine * cross + sine * sine * right_diag
        values[right][right] = sine * sine * left_diag + 2.0 * sine * cosine * cross + cosine * cosine * right_diag
        values[left][right] = values[right][left] = 0.0
        for index in range(size):
            if index in {left, right}:
                continue
            old_left = values[index][left]
            old_right = values[index][right]
            values[index][left] = values[left][index] = cosine * old_left - sine * old_right
            values[index][right] = values[right][index] = sine * old_left + cosine * old_right
    return tuple(values[index][index] for index in range(size))


def empirical_identification(
    observations: Sequence[TransitionObservation],
    model: HistoricalTransitionModel,
    *,
    jacobian_step: float = 1e-6,
    relative_singular_floor: float = 1e-8,
    max_condition_number: float = 1e8,
) -> tuple[bool, tuple[float, ...], float | None]:
    """Evaluate the six-parameter log-odds Jacobian identification gate."""

    sample = tuple(observations)
    if not sample:
        return False, (0.0,) * 6, None
    sample = _validated_sample(sample)
    step = _positive(jacobian_step, "jacobian_step")
    relative_floor = _finite_number(relative_singular_floor, "relative_singular_floor")
    if relative_floor < 0.0:
        raise HistoricalTransitionError("relative_singular_floor cannot be negative")
    maximum_condition = _positive(max_condition_number, "max_condition_number")
    parameters = tuple(value for row in model.theta for value in row[:2])
    jacobian_rows: list[list[float]] = []
    for observation in sample:
        derivatives = [[0.0] * 6 for _ in range(2)]
        for coordinate in range(6):
            plus = list(parameters)
            minus = list(parameters)
            plus[coordinate] += step
            minus[coordinate] -= step
            plus_odds = _log_odds_vector(observation, _theta_from_parameters(plus), model)
            minus_odds = _log_odds_vector(observation, _theta_from_parameters(minus), model)
            for row in range(2):
                derivatives[row][coordinate] = (plus_odds[row] - minus_odds[row]) / (2.0 * step)
        jacobian_rows.extend(derivatives)
    gram = [
        [
            sum(row[left] * row[right] for row in jacobian_rows)
            for right in range(6)
        ]
        for left in range(6)
    ]
    eigenvalues = _jacobi_eigenvalues(gram)
    singular = tuple(sorted((math.sqrt(max(0.0, value)) for value in eigenvalues), reverse=True))
    largest, smallest = singular[0], singular[-1]
    condition = largest / smallest if smallest > 0.0 else math.inf
    identified = (
        largest > 0.0
        and smallest > relative_floor * largest
        and condition <= maximum_condition
    )
    return identified, singular, (condition if math.isfinite(condition) else None)


@dataclass(frozen=True)
class ProductionGateResult:
    status: str
    eligible: bool
    failures: tuple[str, ...]
    transition_count: int
    row_counts: tuple[int, int, int]
    topology_cohorts: tuple[str, ...]
    timing_destination_identification: str
    empirical_identification_passed: bool
    singular_values: tuple[float, ...]
    condition_number: float | None

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "eligible": self.eligible,
            "failures": list(self.failures),
            "transition_count": self.transition_count,
            "row_counts": dict(zip(CATEGORIES, self.row_counts, strict=True)),
            "topology_cohorts": list(self.topology_cohorts),
            "timing_destination_identification": self.timing_destination_identification,
            "empirical_identification": {
                "passed": self.empirical_identification_passed,
                "singular_values": list(self.singular_values),
                "condition_number": self.condition_number,
            },
        }


def evaluate_production_gates(
    observations: Sequence[TransitionObservation],
    model: HistoricalTransitionModel,
    *,
    timing_destination_identification: str = _DEFAULT_IDENTIFICATION,
    schema_valid: bool = True,
    numerical_valid: bool = True,
    provenance_valid: bool = False,
    replay_valid: bool = False,
    min_transitions: int = 30,
    min_per_row: int = 5,
    jacobian_step: float = 1e-6,
    relative_singular_floor: float = 1e-8,
    max_condition_number: float = 1e8,
) -> ProductionGateResult:
    """Evaluate all production gates without providing a promotion override."""

    sample = _validated_sample(observations)
    min_transitions = _positive_int(min_transitions, "min_transitions")
    min_per_row = _positive_int(min_per_row, "min_per_row")
    counts = _row_counts(sample)
    cohorts = tuple(sorted({item.topology_cohort for item in sample}))
    identified, singular_values, condition_number = empirical_identification(
        sample,
        model,
        jacobian_step=jacobian_step,
        relative_singular_floor=relative_singular_floor,
        max_condition_number=max_condition_number,
    )
    failures = []
    if len(sample) < min_transitions:
        failures.append("minimum_transition_count_not_met")
    for category, count in zip(CATEGORIES, counts, strict=True):
        if count < min_per_row:
            failures.append(f"minimum_{category}_row_count_not_met")
    if len(cohorts) != 1:
        failures.append("single_topology_cohort_not_met")
    if timing_destination_identification != "identified":
        failures.append("timing_destination_not_identified")
    if not identified:
        failures.append("potential_not_empirically_identified")
    if not schema_valid:
        failures.append("schema_validation_failed")
    if not numerical_valid or not model.converged or not all(
        math.isfinite(value) for row in model.theta for value in row
    ):
        failures.append("numerical_validation_failed")
    if model.training_count != len(sample) or model.row_counts != counts:
        failures.append("model_training_sample_mismatch")
    if not provenance_valid:
        failures.append("provenance_validation_failed")
    if not replay_valid:
        failures.append("replay_validation_failed")
    eligible = not failures
    return ProductionGateResult(
        status="production_eligible" if eligible else "diagnostic_only",
        eligible=eligible,
        failures=tuple(failures),
        transition_count=len(sample),
        row_counts=counts,
        topology_cohorts=cohorts,
        timing_destination_identification=timing_destination_identification,
        empirical_identification_passed=identified,
        singular_values=singular_values,
        condition_number=condition_number,
    )


@dataclass(frozen=True)
class SensitivityFit:
    kind: str
    value: float
    model: HistoricalTransitionModel

    def to_dict(self) -> dict[str, object]:
        return {"kind": self.kind, "value": self.value, "model": self.model.to_dict()}


@dataclass(frozen=True)
class HistoricalTransitionResult:
    model: HistoricalTransitionModel
    validation: WalkForwardResult
    production_gates: ProductionGateResult
    sensitivities: tuple[SensitivityFit, ...]
    applied_controls: Mapping[str, object]
    timing_destination_identification: str = _DEFAULT_IDENTIFICATION

    def to_dict(self) -> dict[str, object]:
        limitations = [
            "total_announcement_conditioned_dependence",
            "adjacent_edge_only",
            "serially_dependent_transitions",
        ]
        if self.timing_destination_identification != "identified":
            limitations.insert(1, "timing_destination_not_identified")
        return {
            "schema_version": 1,
            "category_order": list(CATEGORIES),
            "timing_destination_identification": self.timing_destination_identification,
            "model": self.model.to_dict(),
            "validation": self.validation.to_dict(),
            "production_gates": self.production_gates.to_dict(),
            "sensitivities": [item.to_dict() for item in self.sensitivities],
            "applied_controls": dict(self.applied_controls),
            "limitations": limitations,
        }


def build_historical_transition_result(
    observations: Sequence[TransitionObservation],
    *,
    penalty: float = 5.0,
    support_floor: float = 0.005,
    penalty_sensitivities: Iterable[float] = (2.0, 10.0),
    floor_sensitivities: Iterable[float] = (0.0025, 0.010),
    timing_destination_identification: str = _DEFAULT_IDENTIFICATION,
    provenance_valid: bool = False,
    replay_valid: bool = False,
    walk_forward_min_training: int = 12,
    walk_forward_min_per_row: int = 2,
    production_min_transitions: int = 30,
    production_min_per_row: int = 5,
    production_max_condition_number: float = 1e8,
    **fit_options: object,
) -> HistoricalTransitionResult:
    """Fit, validate, gate, and serialize the pure diagnostic result."""

    sample = _validated_sample(observations)
    allowed = {
        "standard_move_bp",
        "step_size",
        "tolerance",
        "max_iterations",
        "ipf_tolerance",
        "ipf_max_iterations",
    }
    unknown = set(fit_options) - allowed
    if unknown:
        raise HistoricalTransitionError(f"unknown fit options: {sorted(unknown)}")
    if timing_destination_identification not in {"identified", _DEFAULT_IDENTIFICATION}:
        raise HistoricalTransitionError("timing_destination_identification must be identified or not_identified")
    model = replace(
        fit_potential(
            sample,
            penalty=penalty,
            support_floor=support_floor,
            **fit_options,  # type: ignore[arg-type]
        ),
        timing_destination_identification=timing_destination_identification,
    )
    validation = walk_forward_validate(
        sample,
        min_training=walk_forward_min_training,
        min_row_training=walk_forward_min_per_row,
        penalty=penalty,
        support_floor=support_floor,
        **fit_options,  # type: ignore[arg-type]
    )
    gates = evaluate_production_gates(
        sample,
        model,
        timing_destination_identification=timing_destination_identification,
        provenance_valid=provenance_valid,
        replay_valid=replay_valid,
        min_transitions=production_min_transitions,
        min_per_row=production_min_per_row,
        max_condition_number=production_max_condition_number,
    )
    sensitivities = []
    for value in penalty_sensitivities:
        sensitivities.append(
            SensitivityFit(
                kind="penalty",
                value=float(value),
                model=replace(
                    fit_potential(
                        sample,
                        penalty=float(value),
                        support_floor=support_floor,
                        **fit_options,  # type: ignore[arg-type]
                    ),
                    timing_destination_identification=timing_destination_identification,
                ),
            )
        )
    for value in floor_sensitivities:
        sensitivities.append(
            SensitivityFit(
                kind="support_floor",
                value=float(value),
                model=replace(
                    fit_potential(
                        sample,
                        penalty=penalty,
                        support_floor=float(value),
                        **fit_options,  # type: ignore[arg-type]
                    ),
                    timing_destination_identification=timing_destination_identification,
                ),
            )
        )
    return HistoricalTransitionResult(
        model=model,
        validation=validation,
        production_gates=gates,
        sensitivities=tuple(sensitivities),
        applied_controls={
            "walk_forward_min_training": walk_forward_min_training,
            "walk_forward_min_per_row": walk_forward_min_per_row,
            "production_min_transitions": production_min_transitions,
            "production_min_per_row": production_min_per_row,
            "production_max_condition_number": production_max_condition_number,
        },
        timing_destination_identification=timing_destination_identification,
    )
