"""Immutable domain records for the Polymarket fed-funds implied path."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType

from .models import Diagnostic, stable_unique_diagnostics


@dataclass(frozen=True)
class OutcomeConfig:
    label: str
    representative_bp: float


@dataclass(frozen=True)
class MeetingConfig:
    date: date
    event_slug: str
    outcomes: tuple[OutcomeConfig, ...]


@dataclass(frozen=True)
class TerminalBucketConfig:
    label: str
    kind: str
    representative_rate: float


@dataclass(frozen=True)
class WirpReferenceRow:
    date: date
    incremental_moves: float
    cumulative_moves: float
    implied_rate_delta: float
    implied_rate: float


@dataclass(frozen=True)
class FedPathConfig:
    schema_version: int
    pricing_date: date
    source_image: str
    source_sha256: str
    target_upper_bound: float
    effective_rate_baseline: float
    standard_move_bp: float
    max_spread: float
    meetings: tuple[MeetingConfig, ...]
    terminal_event_slug: str
    terminal_buckets: tuple[TerminalBucketConfig, ...]
    wirp_rows: tuple[WirpReferenceRow, ...]


@dataclass(frozen=True)
class MeetingPrice:
    meeting_date: date
    label: str
    raw_probability: float


@dataclass(frozen=True)
class NormalizedMeetingPrice:
    label: str
    representative_bp: float
    raw_probability: float
    probability: float

    def to_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "representative_bp": self.representative_bp,
            "raw_probability": self.raw_probability,
            "probability": self.probability,
        }


@dataclass(frozen=True)
class MeetingDistribution:
    config: MeetingConfig
    prices: tuple[NormalizedMeetingPrice, ...]
    raw_total: float
    expected_change_bp: float
    decrease_probability: float
    no_change_probability: float
    increase_probability: float
    negative_tail_probability: float
    positive_tail_probability: float
    tail_capped: bool = True


@dataclass(frozen=True)
class TerminalAnchor:
    event_slug: str
    raw_total: float
    probabilities: Mapping[str, float]
    expected_target_upper: float
    effective_rate_proxy: float
    lower_tail_probability: float
    upper_tail_probability: float
    tail_capped: bool = True

    def __post_init__(self) -> None:
        """Copy incoming probabilities before exposing a read-only mapping."""
        object.__setattr__(self, "probabilities", MappingProxyType(dict(self.probabilities)))

    def to_dict(self) -> dict[str, object]:
        return {
            "event_slug": self.event_slug,
            "raw_total": self.raw_total,
            "probabilities": dict(self.probabilities),
            "expected_target_upper": self.expected_target_upper,
            "effective_rate_proxy": self.effective_rate_proxy,
            "lower_tail_probability": self.lower_tail_probability,
            "upper_tail_probability": self.upper_tail_probability,
            "tail_capped": self.tail_capped,
        }


@dataclass(frozen=True)
class FedPathPoint:
    date: date
    kind: str
    implied_change_bp: float
    cumulative_change_bp: float
    incremental_moves: float
    cumulative_moves: float
    implied_target_upper: float
    implied_effective_rate: float
    decrease_probability: float | None
    no_change_probability: float | None
    increase_probability: float | None
    negative_tail_probability: float
    positive_tail_probability: float
    tail_capped: bool
    wirp_incremental_moves: float | None
    wirp_cumulative_moves: float | None
    wirp_implied_rate_delta: float | None
    wirp_implied_rate: float | None
    polymarket_minus_wirp_incremental_moves: float | None
    polymarket_minus_wirp_cumulative_moves: float | None
    polymarket_minus_wirp_implied_rate: float | None
    polymarket_minus_wirp_implied_rate_bp: float | None

    def to_dict(self) -> dict[str, object]:
        return {
            "date": self.date.isoformat(),
            "kind": self.kind,
            "implied_change_bp": self.implied_change_bp,
            "cumulative_change_bp": self.cumulative_change_bp,
            "incremental_moves": self.incremental_moves,
            "cumulative_moves": self.cumulative_moves,
            "implied_target_upper": self.implied_target_upper,
            "implied_effective_rate": self.implied_effective_rate,
            "decrease_probability": self.decrease_probability,
            "no_change_probability": self.no_change_probability,
            "increase_probability": self.increase_probability,
            "negative_tail_probability": self.negative_tail_probability,
            "positive_tail_probability": self.positive_tail_probability,
            "tail_capped": self.tail_capped,
            "wirp_incremental_moves": self.wirp_incremental_moves,
            "wirp_cumulative_moves": self.wirp_cumulative_moves,
            "wirp_implied_rate_delta": self.wirp_implied_rate_delta,
            "wirp_implied_rate": self.wirp_implied_rate,
            "polymarket_minus_wirp_incremental_moves": self.polymarket_minus_wirp_incremental_moves,
            "polymarket_minus_wirp_cumulative_moves": self.polymarket_minus_wirp_cumulative_moves,
            "polymarket_minus_wirp_implied_rate": self.polymarket_minus_wirp_implied_rate,
            "polymarket_minus_wirp_implied_rate_bp": self.polymarket_minus_wirp_implied_rate_bp,
        }


@dataclass(frozen=True)
class FedPathResult:
    generated_at: str
    snapshot_fetched_at: str
    target_upper_bound_baseline: float
    effective_rate_baseline: float
    baseline_spread: float
    standard_move_bp: float
    wirp_rows: tuple[WirpReferenceRow, ...]
    points: tuple[FedPathPoint, ...]
    meeting_distributions: tuple[MeetingDistribution, ...]
    terminal: TerminalAnchor
    diagnostics: tuple[Diagnostic, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "generated_at": self.generated_at,
            "snapshot_fetched_at": self.snapshot_fetched_at,
            "target_upper_bound_baseline": self.target_upper_bound_baseline,
            "effective_rate_baseline": self.effective_rate_baseline,
            "baseline_spread": self.baseline_spread,
            "standard_move_bp": self.standard_move_bp,
            "wirp_rows": [
                {
                    "date": item.date.isoformat(),
                    "incremental_moves": item.incremental_moves,
                    "cumulative_moves": item.cumulative_moves,
                    "implied_rate_delta": item.implied_rate_delta,
                    "implied_rate": item.implied_rate,
                }
                for item in self.wirp_rows
            ],
            "points": [point.to_dict() for point in self.points],
            "meeting_distributions": [
                {
                    "date": item.config.date.isoformat(),
                    "event_slug": item.config.event_slug,
                    "raw_total": item.raw_total,
                    "expected_change_bp": item.expected_change_bp,
                    "decrease_probability": item.decrease_probability,
                    "no_change_probability": item.no_change_probability,
                    "increase_probability": item.increase_probability,
                    "negative_tail_probability": item.negative_tail_probability,
                    "positive_tail_probability": item.positive_tail_probability,
                    "tail_capped": item.tail_capped,
                    "prices": [price.to_dict() for price in item.prices],
                }
                for item in self.meeting_distributions
            ],
            "terminal": self.terminal.to_dict(),
            "diagnostics": [
                {
                    "severity": item.severity,
                    "source_id": item.source_id,
                    "code": item.code,
                    "message": item.message,
                }
                for item in stable_unique_diagnostics(self.diagnostics)
            ],
        }
