"""Immutable domain records for the curve forecaster."""

from dataclasses import dataclass
from datetime import date
from typing import Iterable


@dataclass(frozen=True)
class Scenario:
    id: int
    name: str
    prior: float
    delta_2y_bp: float
    delta_10y_bp: float
    rate_2y: float
    rate_5y: float
    rate_10y: float
    spread_2s10s: float
    shape: str
    direction: str
    slope_move: str
    source_label: str


@dataclass(frozen=True)
class Diagnostic:
    severity: str
    source_id: str | None
    code: str
    message: str


def stable_unique_diagnostics(
    diagnostics: Iterable[Diagnostic],
) -> tuple[Diagnostic, ...]:
    """Return unique diagnostics in first-seen order."""
    result: list[Diagnostic] = []
    seen: set[Diagnostic] = set()
    for diagnostic in diagnostics:
        if diagnostic in seen:
            continue
        seen.add(diagnostic)
        result.append(diagnostic)
    return tuple(result)


@dataclass(frozen=True)
class ExpectedPolicyBucket:
    kind: str
    rate: float


@dataclass(frozen=True)
class EvidenceSource:
    id: str
    kind: str
    weight: float
    event_slug: str
    evidence_horizon_end: date
    expected_policy_buckets: tuple[ExpectedPolicyBucket, ...] = ()


@dataclass(frozen=True)
class AppConfig:
    schema_version: int
    scenario_as_of: date
    scenario_horizon_end: date
    source_image: str
    source_sha256: str
    transcription_verified: bool
    policy_upper_bound: float
    policy_source_date: date
    policy_source_url: str
    max_spread: float
    scenarios: tuple[Scenario, ...]
    evidence_sources: tuple[EvidenceSource, ...]

    def source_metadata(self) -> dict[str, object]:
        return {
            "scenario_as_of": self.scenario_as_of.isoformat(),
            "scenario_horizon_end": self.scenario_horizon_end.isoformat(),
            "source_image": self.source_image,
            "source_sha256": self.source_sha256,
            "transcription_verified": self.transcription_verified,
            "policy_upper_bound": self.policy_upper_bound,
            "policy_source_date": self.policy_source_date.isoformat(),
            "policy_source_url": self.policy_source_url,
        }
