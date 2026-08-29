"""Immutable domain records for the Polymarket fed-funds meeting model."""

from dataclasses import dataclass
from datetime import date


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
class FedPathConfig:
    schema_version: int
    target_upper_bound: float
    effective_rate_baseline: float
    standard_move_bp: float
    max_spread: float
    meetings: tuple[MeetingConfig, ...]
    terminal_event_slug: str
    terminal_buckets: tuple[TerminalBucketConfig, ...]


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
