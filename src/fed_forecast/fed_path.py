"""Pure arithmetic for a Polymarket-implied fed-funds path."""

import math
from decimal import Decimal

from .fed_path_models import (
    MeetingConfig,
    MeetingDistribution,
    MeetingPrice,
    NormalizedMeetingPrice,
)


class FedPathError(ValueError):
    """Raised when prices cannot form a valid fed-path calculation."""


def _finite(value: float, description: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise FedPathError(f"{description} must be finite")
    return float(value)


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
