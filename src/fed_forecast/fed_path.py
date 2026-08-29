"""Pure arithmetic for a Polymarket-implied fed-funds path."""

import math
from collections.abc import Mapping
from decimal import Decimal
from datetime import date

from .fed_path_models import (
    FedPathConfig,
    FedPathPoint,
    FedPathResult,
    MeetingConfig,
    MeetingDistribution,
    MeetingPrice,
    NormalizedMeetingPrice,
    TerminalAnchor,
)
from .models import Diagnostic, stable_unique_diagnostics


class FedPathError(ValueError):
    """Raised when prices cannot form a valid fed-path calculation."""


def _finite(value: float, description: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise FedPathError(f"{description} must be finite")
    return float(value)


def _diagnostic(code: str, message: str, source_id: str | None = None) -> Diagnostic:
    return Diagnostic("warning", source_id, code, message)


def _close(left: float, right: float) -> bool:
    return math.isclose(left, right, rel_tol=0.0, abs_tol=1e-12)


def _invalid_distribution(reason: str) -> None:
    raise FedPathError(f"meeting distribution {reason}")


def _validate_meeting_distribution(
    expected_config: MeetingConfig, distribution: MeetingDistribution,
) -> None:
    """Defend the public path calculator from hand-constructed records."""
    if distribution.config != expected_config:
        _invalid_distribution("configuration does not match the approved meeting")
    if distribution.tail_capped is not True:
        _invalid_distribution("must mark the capped tail convention")
    if len(distribution.prices) != len(expected_config.outcomes):
        _invalid_distribution("does not have the exact five-price topology")
    raw_total = _finite(distribution.raw_total, "meeting distribution raw total")
    if raw_total <= 0.0:
        _invalid_distribution("raw total must be positive")
    raw_values: list[float] = []
    normalized_values: list[float] = []
    for expected, price in zip(expected_config.outcomes, distribution.prices, strict=True):
        if (price.label, price.representative_bp) != (expected.label, expected.representative_bp):
            _invalid_distribution("price topology does not match configuration")
        raw = _finite(price.raw_probability, "meeting distribution raw probability")
        normalized = _finite(price.probability, "meeting distribution normalized probability")
        if not 0.0 <= raw <= 1.0 or not 0.0 <= normalized <= 1.0:
            _invalid_distribution("probabilities must be within [0, 1]")
        raw_values.append(raw)
        normalized_values.append(normalized)
    if not _close(raw_total, sum(raw_values)):
        _invalid_distribution("raw total is inconsistent with the supplied prices")
    if not _close(sum(normalized_values), 1.0):
        _invalid_distribution("normalized probabilities must sum to one")
    for raw, normalized in zip(raw_values, normalized_values, strict=True):
        if not _close(normalized, raw / raw_total):
            _invalid_distribution("normalized probabilities are inconsistent with raw prices")
    expected_change_bp = sum(
        price.probability * outcome.representative_bp
        for price, outcome in zip(distribution.prices, expected_config.outcomes, strict=True)
    )
    if not _close(_finite(distribution.expected_change_bp, "meeting distribution expected change"), expected_change_bp):
        _invalid_distribution("expected change is inconsistent with prices")
    values = (
        _finite(distribution.decrease_probability, "meeting distribution decrease probability"),
        _finite(distribution.no_change_probability, "meeting distribution no-change probability"),
        _finite(distribution.increase_probability, "meeting distribution increase probability"),
        _finite(distribution.negative_tail_probability, "meeting distribution negative-tail probability"),
        _finite(distribution.positive_tail_probability, "meeting distribution positive-tail probability"),
    )
    if any(not 0.0 <= value <= 1.0 for value in values):
        _invalid_distribution("category and tail probabilities must be within [0, 1]")
    decrease, no_change, increase, negative_tail, positive_tail = values
    if not _close(decrease + no_change + increase, 1.0):
        _invalid_distribution("category probabilities must sum to one")
    if not _close(decrease, normalized_values[0] + normalized_values[1]):
        _invalid_distribution("decrease probability is inconsistent with prices")
    if not _close(no_change, normalized_values[2]):
        _invalid_distribution("no-change probability is inconsistent with prices")
    if not _close(increase, normalized_values[3] + normalized_values[4]):
        _invalid_distribution("increase probability is inconsistent with prices")
    if not _close(negative_tail, normalized_values[0]) or not _close(positive_tail, normalized_values[4]):
        _invalid_distribution("tail probabilities are inconsistent with prices")


def _meeting_prices_renormalized(raw_total: float) -> bool:
    """Apply the stated strict decimal 1e-9 boundary without binary drift."""
    return abs(Decimal(str(raw_total)) - Decimal("1.0")) > Decimal("1e-9")


def compute_meeting_distribution(
    config: MeetingConfig, prices: tuple[MeetingPrice, ...]
) -> MeetingDistribution:
    """Normalize the complete five-outcome market and calculate its EV in bp."""
    expected_labels = tuple(item.label for item in config.outcomes)
    if len(prices) != len(expected_labels):
        raise FedPathError("meeting price topology must contain exactly five outcomes")
    by_label: dict[str, MeetingPrice] = {}
    for price in prices:
        if price.meeting_date != config.date:
            raise FedPathError("meeting price date does not match configuration")
        if price.label in by_label:
            raise FedPathError("meeting price topology contains duplicate labels")
        if price.label not in expected_labels:
            raise FedPathError("meeting price topology contains an unknown label")
        probability = _finite(price.raw_probability, "meeting raw probability")
        if not 0.0 <= probability <= 1.0:
            raise FedPathError("meeting raw probability must be within [0, 1]")
        by_label[price.label] = price
    if tuple(by_label) != expected_labels and set(by_label) != set(expected_labels):
        raise FedPathError("meeting price topology does not match configured outcomes")
    ordered = tuple(by_label[label] for label in expected_labels)
    raw_total = sum(item.raw_probability for item in ordered)
    if not math.isfinite(raw_total) or raw_total <= 0.0:
        raise FedPathError("meeting raw total must be positive and finite")
    normalized = tuple(
        NormalizedMeetingPrice(
            outcome.label,
            outcome.representative_bp,
            price.raw_probability,
            price.raw_probability / raw_total,
        )
        for outcome, price in zip(config.outcomes, ordered, strict=True)
    )
    expected_bp = sum(item.probability * item.representative_bp for item in normalized)
    if not math.isfinite(expected_bp) or not math.isclose(sum(item.probability for item in normalized), 1.0, rel_tol=0.0, abs_tol=1e-12):
        raise FedPathError("meeting distribution invariants failed")
    probability = {item.label: item.probability for item in normalized}
    return MeetingDistribution(
        config=config,
        prices=normalized,
        raw_total=raw_total,
        expected_change_bp=expected_bp,
        decrease_probability=probability["50+ bps decrease"] + probability["25 bps decrease"],
        no_change_probability=probability["No change"],
        increase_probability=probability["25 bps increase"] + probability["50+ bps increase"],
        negative_tail_probability=probability["50+ bps decrease"],
        positive_tail_probability=probability["50+ bps increase"],
    )


def _terminal_anchor(config: FedPathConfig, terminal_prices: Mapping[str, float]) -> TerminalAnchor:
    expected_labels = tuple(bucket.label for bucket in config.terminal_buckets)
    if set(terminal_prices) != set(expected_labels) or len(terminal_prices) != len(expected_labels):
        raise FedPathError("terminal price topology must contain exactly the configured 15 buckets")
    raw: dict[str, float] = {}
    for label in expected_labels:
        value = _finite(terminal_prices[label], "terminal raw probability")
        if not 0.0 <= value <= 1.0:
            raise FedPathError("terminal raw probability must be within [0, 1]")
        raw[label] = value
    raw_total = sum(raw.values())
    if not math.isfinite(raw_total) or raw_total <= 0.0:
        raise FedPathError("terminal raw total must be positive and finite")
    probabilities = {label: raw[label] / raw_total for label in expected_labels}
    expected_target_upper = sum(
        probabilities[bucket.label] * bucket.representative_rate
        for bucket in config.terminal_buckets
    )
    if not math.isfinite(expected_target_upper) or not math.isclose(sum(probabilities.values()), 1.0, rel_tol=0.0, abs_tol=1e-12):
        raise FedPathError("terminal distribution invariants failed")
    return TerminalAnchor(
        event_slug=config.terminal_event_slug,
        raw_total=raw_total,
        probabilities=probabilities,
        expected_target_upper=expected_target_upper,
        effective_rate_proxy=expected_target_upper - (config.target_upper_bound - config.effective_rate_baseline),
        lower_tail_probability=probabilities[config.terminal_buckets[0].label],
        upper_tail_probability=probabilities[config.terminal_buckets[-1].label],
    )


def _wirp_by_date(config: FedPathConfig) -> dict[date, object]:
    return {row.date: row for row in config.wirp_rows}


def _point(
    *, date_value: date, kind: str, change_bp: float, cumulative_bp: float,
    config: FedPathConfig, wirp_by_date: Mapping[date, object], decrease: float | None,
    no_change: float | None, increase: float | None, negative_tail: float,
    positive_tail: float,
) -> FedPathPoint:
    row = wirp_by_date.get(date_value)
    return FedPathPoint(
        date=date_value, kind=kind, implied_change_bp=change_bp,
        cumulative_change_bp=cumulative_bp, incremental_moves=change_bp / config.standard_move_bp,
        cumulative_moves=cumulative_bp / config.standard_move_bp,
        implied_target_upper=config.target_upper_bound + cumulative_bp / 100.0,
        implied_effective_rate=config.effective_rate_baseline + cumulative_bp / 100.0,
        decrease_probability=decrease, no_change_probability=no_change,
        increase_probability=increase, negative_tail_probability=negative_tail,
        positive_tail_probability=positive_tail, tail_capped=True,
        wirp_incremental_moves=None if row is None else row.incremental_moves,
        wirp_cumulative_moves=None if row is None else row.cumulative_moves,
        wirp_implied_rate_delta=None if row is None else row.implied_rate_delta,
        wirp_implied_rate=None if row is None else row.implied_rate,
        polymarket_minus_wirp_incremental_moves=None if row is None else change_bp / config.standard_move_bp - row.incremental_moves,
        polymarket_minus_wirp_cumulative_moves=None if row is None else cumulative_bp / config.standard_move_bp - row.cumulative_moves,
        polymarket_minus_wirp_implied_rate=None if row is None else config.effective_rate_baseline + cumulative_bp / 100.0 - row.implied_rate,
        polymarket_minus_wirp_implied_rate_bp=None if row is None else 100.0 * (config.effective_rate_baseline + cumulative_bp / 100.0 - row.implied_rate),
    )


def compute_fed_path(
    config: FedPathConfig,
    meetings: tuple[MeetingDistribution, ...],
    terminal_prices: Mapping[str, float],
    *,
    generated_at: str,
    snapshot_fetched_at: str,
    diagnostics: tuple[Diagnostic, ...] = (),
) -> FedPathResult:
    """Accumulate expected meeting moves and replace December with the terminal anchor."""
    if len(meetings) != len(config.meetings):
        raise FedPathError("meeting distributions must match all configured meetings")
    for expected_config, meeting in zip(config.meetings, meetings, strict=True):
        _validate_meeting_distribution(expected_config, meeting)
    terminal = _terminal_anchor(config, terminal_prices)
    wirp_by_date = _wirp_by_date(config)
    result_diagnostics: list[Diagnostic] = list(diagnostics)
    points: list[FedPathPoint] = []
    cumulative_bp = 0.0
    for meeting in meetings:
        if not math.isfinite(meeting.expected_change_bp):
            raise FedPathError("meeting expected change must be finite")
        cumulative_bp += meeting.expected_change_bp
        points.append(_point(
            date_value=meeting.config.date, kind="meeting_distribution",
            change_bp=meeting.expected_change_bp, cumulative_bp=cumulative_bp, config=config,
            wirp_by_date=wirp_by_date, decrease=meeting.decrease_probability,
            no_change=meeting.no_change_probability, increase=meeting.increase_probability,
            negative_tail=meeting.negative_tail_probability, positive_tail=meeting.positive_tail_probability,
        ))
        if _meeting_prices_renormalized(meeting.raw_total):
            result_diagnostics.append(_diagnostic("meeting_prices_renormalized", "The five mutually exclusive meeting prices were normalized.", meeting.config.event_slug))
    result_diagnostics.extend((
        _diagnostic("tail_bucket_capped", "Open-ended meeting and terminal tails use their boundary representatives."),
        _diagnostic("terminal_anchor_substitution", "December uses the separately traded end-2026 terminal distribution.", config.terminal_event_slug),
        _diagnostic("no_polymarket_2027_coverage", "No comparable Polymarket meeting or terminal coverage is configured for 2027."),
    ))
    terminal_candidates = tuple(row.date for row in config.wirp_rows if row.date.year == config.pricing_date.year)
    terminal_date = max(terminal_candidates, default=None)
    if terminal_date is None:
        raise FedPathError("WIRP comparison must provide the December terminal date")
    prior_target_upper = points[-1].implied_target_upper if points else config.target_upper_bound
    december_change_bp = 100.0 * (terminal.expected_target_upper - prior_target_upper)
    december_cumulative_bp = 100.0 * (terminal.expected_target_upper - config.target_upper_bound)
    points.append(_point(
        date_value=terminal_date, kind="terminal_anchor", change_bp=december_change_bp,
        cumulative_bp=december_cumulative_bp, config=config, wirp_by_date=wirp_by_date,
        decrease=None, no_change=None, increase=None,
        negative_tail=terminal.lower_tail_probability, positive_tail=terminal.upper_tail_probability,
    ))
    if abs(december_change_bp) > 50.0:
        result_diagnostics.append(_diagnostic("cross_market_path_inconsistency", "The terminal anchor implies a December step exceeding 50 bp.", config.terminal_event_slug))
    for point in points:
        if not all(math.isfinite(value) for value in (point.implied_change_bp, point.cumulative_change_bp, point.incremental_moves, point.cumulative_moves, point.implied_target_upper, point.implied_effective_rate)):
            raise FedPathError("fed-path point invariants failed")
    return FedPathResult(
        generated_at=generated_at, snapshot_fetched_at=snapshot_fetched_at,
        target_upper_bound_baseline=config.target_upper_bound,
        effective_rate_baseline=config.effective_rate_baseline,
        baseline_spread=round(config.target_upper_bound - config.effective_rate_baseline, 12),
        standard_move_bp=config.standard_move_bp,
        wirp_rows=config.wirp_rows,
        points=tuple(points), meeting_distributions=meetings, terminal=terminal,
        diagnostics=stable_unique_diagnostics(result_diagnostics),
    )
