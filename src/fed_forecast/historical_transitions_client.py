"""Strict public collection and reconstruction for historical FOMC transitions.

This module intentionally stops at immutable evidence and reconstructed meeting
observations.  Estimation, reporting, CLI wiring, and conditional-tree loading
live in later stages.
"""

from __future__ import annotations

import hashlib
import json
import math
import platform
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping, Sequence
from zoneinfo import TZPATH, ZoneInfo

from .client import ApiError, JsonHttpClient, JsonResponse, Transport, valid_price
from .historical_transitions import TransitionObservation


CATEGORIES = ("down", "unchanged", "up")
NY = ZoneInfo("America/New_York")


class HistoricalConfigError(ValueError):
    """The historical-transition configuration is malformed or inconsistent."""


class HistoricalSnapshotError(ValueError):
    """A stored snapshot is not strict, portable evidence for this config."""


class HistoricalFetchError(RuntimeError):
    """Public collection failed before a complete snapshot could be produced."""


class HistoricalIntegrityError(ValueError):
    """Official decisions and resolved market topology fail a cohort invariant."""


@dataclass(frozen=True)
class OfficialDecision:
    meeting_date: date
    before_lower: float
    before_upper: float
    after_lower: float
    after_upper: float
    source_url: str
    source_date: date

    @property
    def change_bp(self) -> float:
        return 100.0 * (self.after_upper - self.before_upper)

    @property
    def category(self) -> str:
        return "down" if self.change_bp < 0 else "up" if self.change_bp > 0 else "unchanged"

    def to_dict(self) -> dict[str, object]:
        return {
            "date": self.meeting_date.isoformat(),
            "before": [self.before_lower, self.before_upper],
            "after": [self.after_lower, self.after_upper],
            "change_bp": self.change_bp,
            "category": self.category,
            "source_url": self.source_url,
            "source_date": self.source_date.isoformat(),
        }


@dataclass(frozen=True)
class HistoricalConfig:
    schema_version: int
    series_id: int
    topology_rules_version: str
    category_order: tuple[str, str, str]
    pre_cutoff_ny: time
    decision_cutoff_ny: time
    decision_post_cutoff_ny: time
    primary_post_cutoff_ny: time
    event_profile_cutoffs_ny: tuple[time, ...]
    max_quote_age_minutes: int
    max_surface_dispersion_minutes: int
    raw_total_bounds: tuple[float, float]
    strict_raw_total_bounds: tuple[float, float]
    child_action_floor: float
    support_floor: float
    penalty: float
    penalty_sensitivity: tuple[float, ...]
    optimizer: Mapping[str, object]
    ipf: Mapping[str, object]
    walk_forward: Mapping[str, object]
    production_gates: Mapping[str, object]
    official_decisions: tuple[OfficialDecision, ...]
    official_source_url_template: str

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "series_id": self.series_id,
            "topology_rules_version": self.topology_rules_version,
            "category_order": list(self.category_order),
            "pre_cutoff_ny": self.pre_cutoff_ny.strftime("%H:%M"),
            "decision_cutoff_ny": self.decision_cutoff_ny.strftime("%H:%M"),
            "decision_post_cutoff_ny": self.decision_post_cutoff_ny.strftime("%H:%M"),
            "primary_post_cutoff_ny": self.primary_post_cutoff_ny.strftime("%H:%M"),
            "event_profile_cutoffs_ny": [item.strftime("%H:%M") for item in self.event_profile_cutoffs_ny],
            "max_quote_age_minutes": self.max_quote_age_minutes,
            "max_surface_dispersion_minutes": self.max_surface_dispersion_minutes,
            "raw_total_bounds": list(self.raw_total_bounds),
            "strict_raw_total_bounds": list(self.strict_raw_total_bounds),
            "child_action_floor": self.child_action_floor,
            "support_floor": self.support_floor,
            "penalty": self.penalty,
            "penalty_sensitivity": list(self.penalty_sensitivity),
            "optimizer": dict(self.optimizer),
            "ipf": dict(self.ipf),
            "walk_forward": dict(self.walk_forward),
            "production_gates": dict(self.production_gates),
            "official_decisions": [item.to_dict() for item in self.official_decisions],
            "official_source_url_template": self.official_source_url_template,
        }


def _freeze(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _thaw_mapping(value: Mapping[str, object]) -> dict[str, object]:
    thawed = _thaw(value)
    if not isinstance(thawed, dict):
        raise TypeError("mapping thaw did not produce a dictionary")
    return thawed


@dataclass(frozen=True)
class HistoricalSnapshot:
    schema_version: int
    config_sha256: str
    fetched_at: str
    gamma_api_base: str
    clob_api_base: str
    series_id: int
    topology_rules_version: str
    official_decision_ledger: tuple[Mapping[str, object], ...]
    official_decision_sha256: str
    series: Mapping[str, object]
    topology_ledger: tuple[Mapping[str, object], ...]
    topology_blind_sha256: str
    history_windows: tuple[Mapping[str, object], ...]
    runtime_provenance: Mapping[str, object]
    raw_responses: tuple[Mapping[str, object], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "official_decision_ledger", tuple(_freeze(dict(item)) for item in self.official_decision_ledger))
        object.__setattr__(self, "series", _freeze(dict(self.series)))
        object.__setattr__(self, "topology_ledger", tuple(_freeze(dict(item)) for item in self.topology_ledger))
        object.__setattr__(self, "history_windows", tuple(_freeze(dict(item)) for item in self.history_windows))
        object.__setattr__(self, "runtime_provenance", _freeze(dict(self.runtime_provenance)))
        object.__setattr__(self, "raw_responses", tuple(_freeze(dict(item)) for item in self.raw_responses))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "config_sha256": self.config_sha256,
            "fetched_at": self.fetched_at,
            "gamma_api_base": self.gamma_api_base,
            "clob_api_base": self.clob_api_base,
            "series_id": self.series_id,
            "topology_rules_version": self.topology_rules_version,
            "official_decision_ledger": _thaw(self.official_decision_ledger),
            "official_decision_sha256": self.official_decision_sha256,
            "series": _thaw(self.series),
            "topology_ledger": _thaw(self.topology_ledger),
            "topology_blind_sha256": self.topology_blind_sha256,
            "history_windows": _thaw(self.history_windows),
            "runtime_provenance": _thaw(self.runtime_provenance),
            "raw_responses": _thaw(self.raw_responses),
        }


_CONFIG_KEYS = {
    "schema_version", "series_id", "topology_rules_version", "category_order",
    "pre_cutoff_ny", "decision_cutoff_ny", "decision_post_cutoff_ny",
    "primary_post_cutoff_ny", "event_profile_cutoffs_ny", "max_quote_age_minutes",
    "max_surface_dispersion_minutes", "raw_total_bounds",
    "strict_raw_total_bounds", "child_action_floor", "support_floor", "penalty",
    "penalty_sensitivity", "optimizer", "ipf", "walk_forward",
    "production_gates", "official_decisions", "official_source_url_template",
}


def _strict_json(path: Path, error_type: type[ValueError]) -> object:
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
        result = json.loads(
            Path(path).read_text(encoding="utf-8"),
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicates,
        )
        _finite_json(result)
        return result
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise error_type(f"could not read strict JSON: {error}") from error


def _finite_json(value: object) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("JSON numbers must be finite")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("JSON keys must be strings")
            _finite_json(item)
    elif isinstance(value, list):
        for item in value:
            _finite_json(item)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
        default=lambda item: item.isoformat(),
    ).encode("utf-8")


def _sha(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _blind_topology(rows: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    """Return topology-only evidence, excluding terminal outcome prices."""
    return [
        {key: _thaw(value) for key, value in row.items() if key != "yes_resolution_price"}
        for row in rows
    ]


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise HistoricalConfigError(f"{name} must be a finite number")
    return float(value)


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise HistoricalConfigError(f"{name} must be a positive integer")
    return value


def _clock(value: object, name: str) -> time:
    if not isinstance(value, str) or re.fullmatch(r"\d{2}:\d{2}", value) is None:
        raise HistoricalConfigError(f"{name} must be HH:MM")
    try:
        return time.fromisoformat(value)
    except ValueError as error:
        raise HistoricalConfigError(f"{name} must be HH:MM") from error


def _bounds(value: object, name: str) -> tuple[float, float]:
    if not isinstance(value, list) or len(value) != 2:
        raise HistoricalConfigError(f"{name} must contain two numbers")
    lower, upper = (_number(item, name) for item in value)
    if not 0.0 < lower <= 1.0 <= upper or lower >= upper:
        raise HistoricalConfigError(f"{name} must bracket one")
    return lower, upper


def _settings(value: object, keys: set[str], name: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        raise HistoricalConfigError(f"{name} has an invalid schema")
    for key, item in value.items():
        _number(item, f"{name}.{key}")
    return MappingProxyType(dict(value))


def load_historical_config(path: Path) -> HistoricalConfig:
    """Load and validate the pinned historical-transition configuration."""
    raw = _strict_json(Path(path), HistoricalConfigError)
    if not isinstance(raw, dict) or set(raw) != _CONFIG_KEYS:
        raise HistoricalConfigError("configuration has an invalid top-level schema")
    if raw["schema_version"] != 1 or isinstance(raw["schema_version"], bool):
        raise HistoricalConfigError("schema_version must be 1")
    if raw["series_id"] != 35 or isinstance(raw["series_id"], bool):
        raise HistoricalConfigError("series_id must be the approved public FOMC series 35")
    order = raw["category_order"]
    if order != list(CATEGORIES):
        raise HistoricalConfigError("category_order must be down, unchanged, up")
    topology = raw["topology_rules_version"]
    template = raw["official_source_url_template"]
    if not isinstance(topology, str) or not topology or not isinstance(template, str) or "{yyyymmdd}" not in template:
        raise HistoricalConfigError("topology version and official URL template are invalid")
    child_floor = _number(raw["child_action_floor"], "child_action_floor")
    support_floor = _number(raw["support_floor"], "support_floor")
    if not 0.0 < child_floor < support_floor < 1 / 3:
        raise HistoricalConfigError("support floors must be ordered inside (0, 1/3)")
    raw_decisions = raw["official_decisions"]
    if not isinstance(raw_decisions, list) or not raw_decisions:
        raise HistoricalConfigError("official_decisions must be a non-empty array")
    decisions: list[OfficialDecision] = []
    for row in raw_decisions:
        if not isinstance(row, dict) or set(row) != {"date", "before", "after"}:
            raise HistoricalConfigError("official decision has an invalid schema")
        try:
            meeting_date = date.fromisoformat(row["date"])
        except (TypeError, ValueError) as error:
            raise HistoricalConfigError("official decision date must be ISO") from error
        before = row["before"]
        after = row["after"]
        if not isinstance(before, list) or not isinstance(after, list) or len(before) != 2 or len(after) != 2:
            raise HistoricalConfigError("official before/after ranges must have two bounds")
        bl, bu = (_number(item, "official before bound") for item in before)
        al, au = (_number(item, "official after bound") for item in after)
        if not bl < bu or not al < au:
            raise HistoricalConfigError("official target ranges must have positive width")
        if not math.isclose(al - bl, au - bu, abs_tol=1e-9, rel_tol=0.0):
            raise HistoricalConfigError("range-width changes are unsupported")
        change_bp = 100.0 * (au - bu)
        if not math.isclose(change_bp / 25.0, round(change_bp / 25.0), abs_tol=1e-9, rel_tol=0.0):
            raise HistoricalConfigError("official target changes must be 25 bp multiples")
        ymd = meeting_date.strftime("%Y%m%d")
        decisions.append(OfficialDecision(meeting_date, bl, bu, al, au, template.format(yyyymmdd=ymd), meeting_date))
    if [item.meeting_date for item in decisions] != sorted(item.meeting_date for item in decisions) or len({item.meeting_date for item in decisions}) != len(decisions):
        raise HistoricalConfigError("official decisions must be unique and chronological")
    for previous, current in zip(decisions, decisions[1:]):
        if not (
            math.isclose(previous.after_lower, current.before_lower, abs_tol=1e-9, rel_tol=0.0)
            and math.isclose(previous.after_upper, current.before_upper, abs_tol=1e-9, rel_tol=0.0)
        ):
            raise HistoricalConfigError("official target ranges must form a continuous ledger")
    max_age = _positive_int(raw["max_quote_age_minutes"], "max_quote_age_minutes")
    max_dispersion = _positive_int(raw["max_surface_dispersion_minutes"], "max_surface_dispersion_minutes")
    if max_age != 10 or max_dispersion > max_age:
        raise HistoricalConfigError("quote age must be 10 minutes and dispersion no larger")
    clocks = (
        _clock(raw["pre_cutoff_ny"], "pre_cutoff_ny"),
        _clock(raw["decision_cutoff_ny"], "decision_cutoff_ny"),
        _clock(raw["decision_post_cutoff_ny"], "decision_post_cutoff_ny"),
        _clock(raw["primary_post_cutoff_ny"], "primary_post_cutoff_ny"),
    )
    if clocks != (time(13, 0), time(14, 0), time(14, 15), time(15, 30)):
        raise HistoricalConfigError("historical quote cutoffs must match the reviewed 13:00/14:00/14:15/15:30 design")
    profile_raw = raw["event_profile_cutoffs_ny"]
    if not isinstance(profile_raw, list) or profile_raw != ["14:05", "14:15", "14:30", "15:00", "15:30"]:
        raise HistoricalConfigError("event_profile_cutoffs_ny must match the reviewed five-point event profile")
    event_profile = tuple(_clock(item, "event_profile_cutoffs_ny") for item in profile_raw)
    raw_bounds = _bounds(raw["raw_total_bounds"], "raw_total_bounds")
    strict_bounds = _bounds(raw["strict_raw_total_bounds"], "strict_raw_total_bounds")
    if strict_bounds[0] < raw_bounds[0] or strict_bounds[1] > raw_bounds[1]:
        raise HistoricalConfigError("strict raw-total bounds must be nested within primary bounds")
    penalty = _number(raw["penalty"], "penalty")
    if not isinstance(raw["penalty_sensitivity"], list):
        raise HistoricalConfigError("penalty_sensitivity must be an array")
    penalty_sensitivity = tuple(_number(item, "penalty_sensitivity") for item in raw["penalty_sensitivity"])
    if penalty <= 0.0 or not penalty_sensitivity or any(item <= 0.0 for item in penalty_sensitivity):
        raise HistoricalConfigError("penalties must be positive")
    optimizer = _settings(raw["optimizer"], {"initial_step", "tolerance", "max_iterations"}, "optimizer")
    ipf = _settings(raw["ipf"], {"tolerance", "max_iterations"}, "ipf")
    walk_forward = _settings(raw["walk_forward"], {"minimum_training", "minimum_per_row"}, "walk_forward")
    production_gates = _settings(raw["production_gates"], {"minimum_transitions", "minimum_per_row", "maximum_condition_number"}, "production_gates")
    for settings, keys, name in (
        (optimizer, ("initial_step", "tolerance"), "optimizer"),
        (ipf, ("tolerance",), "ipf"),
        (production_gates, ("maximum_condition_number",), "production_gates"),
    ):
        if any(_number(settings[key], f"{name}.{key}") <= 0.0 for key in keys):
            raise HistoricalConfigError(f"{name} numeric controls must be positive")
    for settings, keys, name in (
        (optimizer, ("max_iterations",), "optimizer"),
        (ipf, ("max_iterations",), "ipf"),
        (walk_forward, ("minimum_training", "minimum_per_row"), "walk_forward"),
        (production_gates, ("minimum_transitions", "minimum_per_row"), "production_gates"),
    ):
        for key in keys:
            setting = settings[key]
            if isinstance(setting, bool) or not isinstance(setting, int) or setting <= 0:
                raise HistoricalConfigError(f"{name}.{key} must be a positive integer")
    return HistoricalConfig(
        1, 35, topology, CATEGORIES,
        clocks[0], clocks[1], clocks[2], clocks[3], event_profile,
        max_age, max_dispersion, raw_bounds, strict_bounds,
        child_floor, support_floor, penalty, penalty_sensitivity,
        optimizer, ipf, walk_forward, production_gates,
        tuple(decisions), template,
    )


def _config_sha(config: HistoricalConfig) -> str:
    return _sha(config.to_dict())


def _runtime_provenance() -> dict[str, object]:
    sources = tuple(
        Path(__file__).with_name(name)
        for name in (
            "historical_transitions_client.py",
            "historical_transitions.py",
            "historical_transitions_cli.py",
            "historical_transitions_reporting.py",
        )
    )
    timezone_bytes = None
    for root in TZPATH:
        candidate = Path(root) / "America" / "New_York"
        if candidate.is_file():
            timezone_bytes = candidate.read_bytes()
            break
    if timezone_bytes is None:
        raise HistoricalSnapshotError("could not identify America/New_York timezone data")
    return {
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "python_executable_basename": Path(sys.executable).name,
        "zoneinfo_key": NY.key,
        "zoneinfo_sha256": hashlib.sha256(timezone_bytes).hexdigest(),
        "zoneinfo_size_bytes": len(timezone_bytes),
        "source_sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sources},
    }


def current_historical_runtime_provenance() -> dict[str, object]:
    """Return the exact code and timezone identity required for a valid replay."""
    return _runtime_provenance()


def _ledger(config: HistoricalConfig) -> tuple[dict[str, object], ...]:
    return tuple(item.to_dict() for item in config.official_decisions)


def _json_array(value: object, name: str) -> list[object]:
    if isinstance(value, (list, tuple)):
        result = list(value)
    elif isinstance(value, str):
        try:
            result = json.loads(value)
        except json.JSONDecodeError as error:
            raise ValueError(f"{name} must contain valid JSON") from error
    else:
        raise ValueError(f"{name} must be an array or JSON-encoded array")
    if not isinstance(result, list):
        raise ValueError(f"{name} must decode to an array")
    return result


def _event_date(event: Mapping[str, object]) -> date:
    for key in ("eventDate", "endDate", "endDateIso"):
        value = event.get(key)
        if isinstance(value, str) and len(value) >= 10:
            try:
                return date.fromisoformat(value[:10])
            except ValueError:
                pass
    raise ValueError("event has no valid meeting date")


_CATEGORY_TERMS = {
    "down": re.compile(r"\b(?:decrease|decreases|cut|cuts|lower|lowers)\b", re.I),
    "unchanged": re.compile(r"\b(?:no\s+change|unchanged|pause|hold)\b", re.I),
    "up": re.compile(r"\b(?:increase|increases|hike|hikes|raise|raises)\b", re.I),
}


def _category(text: str) -> str:
    found = [name for name, pattern in _CATEGORY_TERMS.items() if pattern.search(text)]
    if len(found) != 1:
        raise ValueError("market label has ambiguous or missing D/H/U mapping")
    return found[0]


def _action_bucket(text: str, category: str) -> tuple[float, float | None, float | None]:
    if category == "unchanged":
        return 0.0, 0.0, 0.0
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:\+|or\s+more)?\s*(?:bps?|basis\s+points?)", text, re.I)
    if match is None:
        raise ValueError("directional market does not declare an action magnitude")
    magnitude = float(match.group(1))
    if not math.isfinite(magnitude) or magnitude <= 0 or not math.isclose(magnitude / 25.0, round(magnitude / 25.0), abs_tol=1e-9):
        raise ValueError("action magnitude must be a positive 25 bp multiple")
    open_ended = "+" in match.group(0) or re.search(r"or\s+more|at\s+least", text, re.I) is not None
    signed = -magnitude if category == "down" else magnitude
    if category == "down" and open_ended:
        return signed, None, signed
    if category == "up" and open_ended:
        return signed, signed, None
    return signed, signed, signed


def _contains(row: Mapping[str, object], value: float) -> bool:
    lower = row.get("interval_lower_bp")
    upper = row.get("interval_upper_bp")
    if lower is not None and (isinstance(lower, bool) or not isinstance(lower, (int, float))):
        return False
    if upper is not None and (isinstance(upper, bool) or not isinstance(upper, (int, float))):
        return False
    return (lower is None or value >= float(lower) - 1e-9) and (upper is None or value <= float(upper) + 1e-9)


def _parse_market(event: Mapping[str, object], market: object, meeting_date: date) -> dict[str, object]:
    if not isinstance(market, Mapping):
        raise ValueError("event market must be an object")
    question = market.get("question")
    label = market.get("groupItemTitle") or market.get("title")
    if not isinstance(question, str) or not question or not isinstance(label, str) or not label:
        raise ValueError("market question and group label must be non-empty strings")
    outcomes = _json_array(market.get("outcomes"), "outcomes")
    tokens = _json_array(market.get("clobTokenIds"), "clobTokenIds")
    prices = _json_array(market.get("outcomePrices"), "outcomePrices")
    if not outcomes or len(outcomes) != len(tokens) or len(tokens) != len(prices):
        raise ValueError("outcomes, clobTokenIds, and outcomePrices must align")
    yes = [i for i, item in enumerate(outcomes) if isinstance(item, str) and item.strip().casefold() == "yes"]
    if len(yes) != 1 or not isinstance(tokens[yes[0]], str) or not tokens[yes[0]]:
        raise ValueError("market must have exactly one aligned Yes token")
    parsed_prices = [valid_price(item) for item in prices]
    if any(item is None for item in parsed_prices):
        raise ValueError("outcomePrices must be finite probabilities")
    category = _category(f"{label} {question}")
    representative, lower, upper = _action_bucket(f"{label} {question}", category)
    return {
        "event_id": str(event.get("id", "")),
        "event_slug": str(event.get("slug", "")),
        "meeting_date": meeting_date.isoformat(),
        "neg_risk": event.get("negRisk"),
        "market_id": str(market.get("id", "")),
        "question": question,
        "group_label": label,
        "outcomes": outcomes,
        "token_ids": tokens,
        "yes_token": tokens[yes[0]],
        "yes_resolution_price": parsed_prices[yes[0]],
        "category": category,
        "representative_action_bp": representative,
        "interval_lower_bp": lower,
        "interval_upper_bp": upper,
        "mapping_decision": "mapped",
        "topology_cohort": "primary" if event.get("negRisk") is True else "legacy",
        "exclusion_reason": None if event.get("negRisk") is True else "legacy_non_negrisk",
    }


def _validate_partition(rows: Sequence[Mapping[str, object]]) -> None:
    if {str(row["category"]) for row in rows} != set(CATEGORIES):
        raise ValueError("event does not cover all D/H/U categories")
    for action in range(-500, 501, 25):
        matches = sum(_contains(row, float(action)) for row in rows)
        if matches != 1:
            raise ValueError("action buckets do not form a complete non-overlapping 25 bp partition")


def _topology(series: Mapping[str, object], config: HistoricalConfig) -> tuple[tuple[dict[str, object], ...], dict[date, list[dict[str, object]]]]:
    events = series.get("events")
    if not isinstance(events, (list, tuple)):
        raise HistoricalFetchError("Gamma series must contain an events array")
    official_dates = {item.meeting_date for item in config.official_decisions}
    ledger: list[dict[str, object]] = []
    candidates_by_date: dict[date, list[list[dict[str, object]]]] = {}
    for raw_event in events:
        if not isinstance(raw_event, Mapping):
            ledger.append({"mapping_decision": "excluded", "exclusion_reason": "event_not_object"})
            continue
        try:
            meeting_date = _event_date(raw_event)
            event_id = raw_event.get("id")
            slug = raw_event.get("slug")
            markets = raw_event.get("markets")
            if not isinstance(event_id, (str, int)) or not str(event_id) or not isinstance(slug, str) or not slug:
                raise ValueError("event id and slug are required")
            if not isinstance(markets, (list, tuple)) or not markets:
                raise ValueError("event markets must be a non-empty array")
            if meeting_date not in official_dates:
                raise ValueError("event date is not in the official scheduled decision ledger")
            rows = [_parse_market(raw_event, market, meeting_date) for market in markets]
            if len({str(row["yes_token"]) for row in rows}) != len(rows):
                raise ValueError("event contains duplicate Yes tokens")
            _validate_partition(rows)
            ledger.extend(rows)
            candidates_by_date.setdefault(meeting_date, []).append(rows)
        except ValueError as error:
            ledger.append({
                "event_id": str(raw_event.get("id", "")),
                "event_slug": str(raw_event.get("slug", "")),
                "meeting_date": str(raw_event.get("eventDate") or raw_event.get("endDate") or ""),
                "neg_risk": raw_event.get("negRisk"),
                "mapping_decision": "excluded",
                "topology_cohort": "excluded",
                "exclusion_reason": str(error),
            })
    by_date: dict[date, list[dict[str, object]]] = {}
    for meeting_date, event_rows in candidates_by_date.items():
        primary = [rows for rows in event_rows if rows[0].get("topology_cohort") == "primary"]
        if len(primary) == 1:
            by_date[meeting_date] = primary[0]
        elif len(primary) > 1:
            for rows in primary:
                for row in rows:
                    row["mapping_decision"] = "excluded"
                    row["topology_cohort"] = "excluded"
                    row["exclusion_reason"] = "multiple_primary_events_for_date"
    ledger.sort(key=lambda row: (str(row.get("meeting_date", "")), str(row.get("event_slug", "")), str(row.get("market_id", ""))))
    return tuple(ledger), by_date


def _timestamp_at(meeting_date: date, clock: time) -> int:
    return int(datetime.combine(meeting_date, clock, NY).timestamp())


class HistoricalTransitionsClient:
    """Collect public Gamma series events and bounded CLOB history batches."""

    GAMMA_API_BASE = "https://gamma-api.polymarket.com"
    CLOB_API_BASE = "https://clob.polymarket.com"

    def __init__(
        self,
        transport: Transport | None = None,
        *,
        http: JsonHttpClient | None = None,
        sleep: Callable[[float], None] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if transport is not None and http is not None:
            raise ValueError("provide transport or http, not both")
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.http = http or JsonHttpClient(transport, sleep=sleep or (lambda _: None), now=self.now)

    def _now(self) -> str:
        return self.now().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _record(self, method: str, url: str, response: JsonResponse, payload: object | None = None) -> dict[str, object]:
        row: dict[str, object] = {
            "method": method, "url": url, "status": response.status,
            "retrieved_at": self._now(), "response_headers": dict(response.headers),
            "body": response.data, "body_hex": response.body.hex(),
            "body_sha256": hashlib.sha256(response.body).hexdigest(),
        }
        if payload is not None:
            row["request"] = payload
        return row

    def fetch_snapshot(self, config: HistoricalConfig) -> HistoricalSnapshot:
        """Fetch series 35 and histories needed for synchronized adjacent edges."""
        raw_responses: list[dict[str, object]] = []
        series_url = (
            f"{self.GAMMA_API_BASE}/events?series_id={config.series_id}"
            "&closed=true&limit=200&order=endDate&ascending=true"
        )
        try:
            series_response = self.http.get_json_response(series_url)
        except ApiError as error:
            raise HistoricalFetchError(f"could not fetch Gamma series events {config.series_id}: {error}") from error
        raw_responses.append(self._record("GET", series_url, series_response))
        if not isinstance(series_response.data, list):
            raise HistoricalFetchError("Gamma series-events response must be an array")
        _finite_json(series_response.data)
        series: dict[str, object] = {"id": str(config.series_id), "events": series_response.data}
        ledger, by_date = _topology(series, config)
        topology_hash = _sha(_blind_topology(ledger))
        windows: list[dict[str, object]] = []
        decisions = [item.meeting_date for item in config.official_decisions]
        url = f"{self.CLOB_API_BASE}/batch-prices-history"
        for current_date, next_date in zip(decisions, decisions[1:]):
            current_rows, next_rows = by_date.get(current_date), by_date.get(next_date)
            if not current_rows or not next_rows:
                continue
            if any(row.get("topology_cohort") != "primary" for row in (*current_rows, *next_rows)):
                continue
            tokens = sorted({str(row["yes_token"]) for row in (*current_rows, *next_rows)})
            start = _timestamp_at(current_date, config.pre_cutoff_ny) - 60 * config.max_quote_age_minutes
            end = _timestamp_at(current_date, config.primary_post_cutoff_ny)
            combined: dict[str, list[dict[str, object]]] = {}
            for offset in range(0, len(tokens), 20):
                batch = tokens[offset:offset + 20]
                payload = {"markets": batch, "start_ts": start, "end_ts": end, "fidelity": 1}
                if len(batch) > 20:
                    raise AssertionError("CLOB batch exceeds documented maximum")
                try:
                    response = self.http.post_json_response(url, payload)
                except ApiError as error:
                    raise HistoricalFetchError(f"could not fetch CLOB history for {current_date}: {error}") from error
                raw_responses.append(self._record("POST", url, response, payload))
                parsed = _parse_batch_history(response.data, batch)
                for token, points in parsed.items():
                    combined[token] = points
            windows.append({
                "decision_date": current_date.isoformat(),
                "next_meeting_date": next_date.isoformat(),
                "start_ts": start,
                "end_ts": end,
                "requested_tokens": tokens,
                "history": combined,
            })
        official = _ledger(config)
        return HistoricalSnapshot(
            1, _config_sha(config), self._now(), self.GAMMA_API_BASE,
            self.CLOB_API_BASE, config.series_id, config.topology_rules_version,
            official, _sha(list(official)), series, ledger,
            topology_hash, tuple(windows), _runtime_provenance(), tuple(raw_responses),
        )


def _parse_batch_history(value: object, requested: Sequence[str]) -> dict[str, list[dict[str, object]]]:
    if not isinstance(value, dict) or set(value) != {"history"} or not isinstance(value["history"], dict):
        raise HistoricalFetchError("CLOB batch history response has an invalid schema")
    history = value["history"]
    if set(history) - set(requested):
        raise HistoricalFetchError("CLOB batch history returned an unrequested token")
    result: dict[str, list[dict[str, object]]] = {}
    for token in requested:
        raw_points = history.get(token, [])
        if not isinstance(raw_points, list):
            raise HistoricalFetchError("CLOB token history must be an array")
        points: list[dict[str, object]] = []
        previous = -1
        for item in raw_points:
            if not isinstance(item, dict) or set(item) != {"t", "p"}:
                raise HistoricalFetchError("CLOB history point has an invalid schema")
            timestamp = item["t"]
            try:
                price = float(item["p"])
            except (TypeError, ValueError) as error:
                raise HistoricalFetchError(f"CLOB history price is non-numeric for token {token}: {item!r}") from error
            if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or not math.isfinite(float(timestamp)) or int(timestamp) != timestamp or not math.isfinite(price):
                raise HistoricalFetchError(f"CLOB history point is invalid for token {token}: {item!r}")
            stamp = int(timestamp)
            if stamp <= previous:
                raise HistoricalFetchError("CLOB history points must be strictly chronological")
            previous = stamp
            # Preserve finite out-of-range public marks in the immutable snapshot.
            # Surface reconstruction rejects those coordinates through valid_price
            # rather than aborting the entire multi-year collection.
            points.append({"t": stamp, "p": price})
        result[token] = points
    return result


def _surface(
    rows: Sequence[Mapping[str, object]],
    history: Mapping[str, object],
    cutoff: int,
    config: HistoricalConfig,
    *,
    strictly_after: int | None = None,
) -> tuple[tuple[float, float, float], tuple[float, float, float], float, tuple[int, ...], tuple[float, float, float], tuple[str, ...]]:
    selected: list[tuple[Mapping[str, object], float, int]] = []
    oldest = cutoff - 60 * config.max_quote_age_minutes
    for row in rows:
        token = str(row["yes_token"])
        raw_points = history.get(token)
        if not isinstance(raw_points, (list, tuple)):
            raise ValueError(f"missing history for token {token}")
        candidates: list[tuple[int, float]] = []
        for item in raw_points:
            if not isinstance(item, Mapping):
                continue
            stamp, price = item.get("t"), valid_price(item.get("p"))
            if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or int(stamp) != stamp or price is None:
                continue
            timestamp = int(stamp)
            if oldest <= timestamp <= cutoff and (strictly_after is None or timestamp > strictly_after):
                candidates.append((timestamp, price))
        if not candidates:
            raise ValueError(f"no synchronized quote for token {token}")
        timestamp, price = max(candidates)
        selected.append((row, price, timestamp))
    timestamps = tuple(item[2] for item in selected)
    if max(timestamps) - min(timestamps) > 60 * config.max_surface_dispersion_minutes:
        raise ValueError("surface quote dispersion exceeds configured maximum")
    category_prices: dict[str, float] = {name: 0.0 for name in CATEGORIES}
    action_numerators: dict[str, float] = {name: 0.0 for name in CATEGORIES}
    action_denominators: dict[str, float] = {name: 0.0 for name in CATEGORIES}
    raw_category_mass: dict[str, float] = {name: 0.0 for name in CATEGORIES}
    for row, price, _ in selected:
        category = str(row["category"])
        category_prices[category] += price
        raw_category_mass[category] += price
        weight = max(price, config.child_action_floor)
        representative = row["representative_action_bp"]
        if isinstance(representative, bool) or not isinstance(representative, (int, float)):
            raise ValueError("representative action must be numeric")
        action_numerators[category] += weight * float(representative)
        action_denominators[category] += weight
    total = sum(category_prices.values())
    if not config.raw_total_bounds[0] <= total <= config.raw_total_bounds[1]:
        raise ValueError(f"surface raw total {total:.6f} is outside configured bounds")
    raw_values = tuple(category_prices[name] / total for name in CATEGORIES)
    raw = (raw_values[0], raw_values[1], raw_values[2])
    smoothed_values = [max(item, config.support_floor) for item in raw]
    smoothed_total = sum(smoothed_values)
    smoothed = (
        smoothed_values[0] / smoothed_total,
        smoothed_values[1] / smoothed_total,
        smoothed_values[2] / smoothed_total,
    )
    action_values = tuple(action_numerators[name] / action_denominators[name] for name in CATEGORIES)
    actions = (action_values[0], action_values[1], action_values[2])
    fallback = tuple(name for name in CATEGORIES if math.isclose(raw_category_mass[name], 0.0, abs_tol=0.0))
    return raw, smoothed, total, timestamps, actions, fallback


def _bucket_surface(
    rows: Sequence[Mapping[str, object]],
    history: Mapping[str, object],
    cutoff: int,
    config: HistoricalConfig,
    *,
    strictly_after: int | None = None,
) -> dict[str, object]:
    """Preserve the full action-size distribution at one synchronized cutoff."""
    selected: list[tuple[Mapping[str, object], float, int]] = []
    oldest = cutoff - 60 * config.max_quote_age_minutes
    for row in rows:
        token = str(row["yes_token"])
        points = history.get(token)
        if not isinstance(points, (list, tuple)):
            raise ValueError(f"missing history for token {token}")
        candidates = []
        for item in points:
            if not isinstance(item, Mapping):
                continue
            stamp, price = item.get("t"), valid_price(item.get("p"))
            if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or int(stamp) != stamp or price is None:
                continue
            timestamp = int(stamp)
            if oldest <= timestamp <= cutoff and (strictly_after is None or timestamp > strictly_after):
                candidates.append((timestamp, price))
        if not candidates:
            raise ValueError(f"no synchronized quote for token {token}")
        timestamp, price = max(candidates)
        selected.append((row, price, timestamp))
    timestamps = [item[2] for item in selected]
    if max(timestamps) - min(timestamps) > 60 * config.max_surface_dispersion_minutes:
        raise ValueError("bucket-surface quote dispersion exceeds configured maximum")
    total = sum(item[1] for item in selected)
    if not config.raw_total_bounds[0] <= total <= config.raw_total_bounds[1]:
        raise ValueError(f"bucket-surface raw total {total:.6f} is outside configured bounds")
    buckets = [{
        "label": str(row["group_label"]),
        "category": str(row["category"]),
        "representative_action_bp": float(str(row["representative_action_bp"])),
        "interval_lower_bp": row.get("interval_lower_bp"),
        "interval_upper_bp": row.get("interval_upper_bp"),
        "probability": price / total,
        "quote_timestamp": timestamp,
    } for row, price, timestamp in selected]
    expected_action = sum(float(str(row["representative_action_bp"])) * price / total for row, price, _ in selected)
    down_tail_identified = any(float(str(row["representative_action_bp"])) <= -50.0 for row, _, _ in selected)
    up_tail_identified = any(float(str(row["representative_action_bp"])) >= 50.0 for row, _, _ in selected)
    down_tail = sum(price / total for row, price, _ in selected if float(str(row["representative_action_bp"])) <= -50.0) if down_tail_identified else None
    up_tail = sum(price / total for row, price, _ in selected if float(str(row["representative_action_bp"])) >= 50.0) if up_tail_identified else None
    return {
        "cutoff_timestamp": cutoff,
        "raw_total": total,
        "expected_action_bp": expected_action,
        "down_50plus_probability": down_tail,
        "up_50plus_probability": up_tail,
        "down_50plus_identified": down_tail_identified,
        "up_50plus_identified": up_tail_identified,
        "buckets": buckets,
    }


def _resolved_category(rows: Sequence[Mapping[str, object]], decision: OfficialDecision) -> str:
    winners = [row for row in rows if row.get("yes_resolution_price") == 1.0]
    losers = [row for row in rows if row.get("yes_resolution_price") == 0.0]
    if len(winners) != 1 or len(losers) != len(rows) - 1:
        raise ValueError("current event does not have a terminal one-hot resolution")
    winner = winners[0]
    if not _contains(winner, decision.change_bp) or winner.get("category") != decision.category:
        raise HistoricalIntegrityError(
            f"resolved bucket for {decision.meeting_date} does not contain official {decision.change_bp:+.1f} bp move"
        )
    return decision.category


def reconstruct_observations(
    snapshot: HistoricalSnapshot,
    config: HistoricalConfig,
) -> tuple[tuple[TransitionObservation, ...], tuple[Mapping[str, object], ...], tuple[Mapping[str, object], ...]]:
    """Reconstruct synchronized adjacent transitions without widening windows."""
    _validate_snapshot_identity(snapshot, config)
    _, by_date = _topology(snapshot.series, config)
    windows = {date.fromisoformat(str(item["decision_date"])): item for item in snapshot.history_windows}
    decisions = {item.meeting_date: item for item in config.official_decisions}
    ordered = [item.meeting_date for item in config.official_decisions]
    observations: list[TransitionObservation] = []
    exclusions: list[dict[str, object]] = []
    # Validate every mapped primary terminal event, including the final event
    # that appears only as a successor and events that cannot form a pair.
    for meeting_date, rows in sorted(by_date.items()):
        if rows and all(row.get("topology_cohort") == "primary" for row in rows):
            _resolved_category(rows, decisions[meeting_date])
    surface_diagnostics: list[dict[str, object]] = []
    for current_date, next_date in zip(ordered, ordered[1:]):
        current_rows, next_rows = by_date.get(current_date), by_date.get(next_date)
        base = {"current_meeting_date": current_date.isoformat(), "next_meeting_date": next_date.isoformat()}
        if not current_rows or not next_rows:
            exclusions.append({**base, "reason": "missing_consecutive_primary_topology"})
            continue
        if any(row.get("topology_cohort") != "primary" for row in (*current_rows, *next_rows)):
            exclusions.append({**base, "reason": "legacy_or_mixed_topology"})
            continue
        window = windows.get(current_date)
        if window is None or window.get("next_meeting_date") != next_date.isoformat():
            exclusions.append({**base, "reason": "missing_history_window"})
            continue
        history = window.get("history")
        if not isinstance(history, Mapping):
            exclusions.append({**base, "reason": "invalid_history_window"})
            continue
        try:
            realized = _resolved_category(current_rows, decisions[current_date])
            pre = _timestamp_at(current_date, config.pre_cutoff_ny)
            post = _timestamp_at(current_date, config.primary_post_cutoff_ny)
            post_lower = post - 60 * config.max_quote_age_minutes
            a_raw, a, a_total, a_times, actions, fallback = _surface(current_rows, history, pre, config)
            b_raw, b, b_total, b_times, _, _ = _surface(next_rows, history, pre, config)
            q_raw, _, q_total, q_times, _, _ = _surface(next_rows, history, post, config, strictly_after=post_lower)
            current_bucket_pre = _bucket_surface(current_rows, history, pre, config)
            next_bucket_pre = _bucket_surface(next_rows, history, pre, config)
            decision_timestamp = _timestamp_at(current_date, config.decision_cutoff_ny)
            event_profile: dict[str, object] = {}
            for profile_cutoff in config.event_profile_cutoffs_ny:
                label = profile_cutoff.strftime("%H:%M")
                cutoff_timestamp = _timestamp_at(current_date, profile_cutoff)
                lower = max(decision_timestamp, cutoff_timestamp - 60 * config.max_quote_age_minutes)
                try:
                    category_raw, _, category_total, category_times, _, _ = _surface(
                        next_rows, history, cutoff_timestamp, config, strictly_after=lower,
                    )
                    bucket_profile = _bucket_surface(
                        next_rows, history, cutoff_timestamp, config, strictly_after=lower,
                    )
                    event_profile[label] = {
                        "category_probabilities": list(category_raw),
                        "category_raw_total": category_total,
                        "category_timestamps": list(category_times),
                        **bucket_profile,
                        "error": None,
                    }
                except ValueError as profile_error:
                    event_profile[label] = {"error": str(profile_error)}
            current_event = current_rows[0]
            next_event = next_rows[0]
            observations.append(TransitionObservation(
                current_meeting_id=str(current_event["event_id"]),
                next_meeting_id=str(next_event["event_id"]),
                current_meeting_date=current_date,
                next_meeting_date=next_date,
                current_pre=a_raw,
                current_candidate_actions_bp=actions,
                realized_category=realized,
                realized_action_bp=decisions[current_date].change_bp,
                next_pre=b_raw,
                next_post=q_raw,
                topology_cohort="primary_negrisk",
                next_realized_category=decisions[next_date].category,
            ))
            surface_diagnostics.append({
                "record_type": "surface_diagnostic",
                "current_meeting_id": str(current_event["event_id"]),
                "next_meeting_id": str(next_event["event_id"]),
                "current_event_slug": str(current_event["event_slug"]),
                "next_event_slug": str(next_event["event_slug"]),
                "current_meeting_date": current_date.isoformat(),
                "next_meeting_date": next_date.isoformat(),
                "category_order": list(CATEGORIES),
                "current_pre_raw": list(a_raw),
                "current_pre_smoothed": list(a),
                "current_pre_raw_total": a_total,
                "current_pre_timestamps": list(a_times),
                "next_pre_raw": list(b_raw),
                "next_pre_smoothed": list(b),
                "next_pre_raw_total": b_total,
                "next_pre_timestamps": list(b_times),
                "next_post_raw": list(q_raw),
                "next_post_raw_total": q_total,
                "next_post_timestamps": list(q_times),
                "current_pre_action_buckets": current_bucket_pre,
                "next_pre_action_buckets": next_bucket_pre,
                "event_time_profile": event_profile,
                "strict_raw_total_bounds_passed": all(
                    config.strict_raw_total_bounds[0] <= total <= config.strict_raw_total_bounds[1]
                    for total in (a_total, b_total, q_total)
                ),
                "child_action_fallback_categories": list(fallback),
            })
        except HistoricalIntegrityError:
            raise
        except ValueError as error:
            exclusions.append({**base, "reason": str(error)})
    topology_rows = tuple(_thaw_mapping(item) for item in snapshot.topology_ledger) + tuple(surface_diagnostics)
    return tuple(observations), topology_rows, tuple(exclusions)


_SNAPSHOT_KEYS = set(HistoricalSnapshot.__dataclass_fields__)


def _validate_snapshot_identity(snapshot: HistoricalSnapshot, config: HistoricalConfig) -> None:
    if snapshot.schema_version != 1 or snapshot.config_sha256 != _config_sha(config):
        raise HistoricalSnapshotError("snapshot identity does not match active configuration")
    if snapshot.series_id != config.series_id or snapshot.topology_rules_version != config.topology_rules_version:
        raise HistoricalSnapshotError("snapshot series/topology identity is invalid")
    if snapshot.gamma_api_base != HistoricalTransitionsClient.GAMMA_API_BASE or snapshot.clob_api_base != HistoricalTransitionsClient.CLOB_API_BASE:
        raise HistoricalSnapshotError("snapshot public API bases are invalid")
    official = _ledger(config)
    if _sha(_thaw(snapshot.official_decision_ledger)) != snapshot.official_decision_sha256 or tuple(_thaw_mapping(item) for item in snapshot.official_decision_ledger) != official:
        raise HistoricalSnapshotError("snapshot official decision ledger is invalid")
    if _sha(_blind_topology(snapshot.topology_ledger)) != snapshot.topology_blind_sha256:
        raise HistoricalSnapshotError("snapshot blind topology hash is invalid")
    if _thaw(snapshot.runtime_provenance) != _runtime_provenance():
        raise HistoricalSnapshotError("snapshot runtime/code provenance does not match this replay environment")


def _validate_raw_evidence(raw: Mapping[str, object], config: HistoricalConfig) -> None:
    responses = raw["raw_responses"]
    if not isinstance(responses, list):
        raise HistoricalSnapshotError("snapshot raw_responses must be an array")
    decoded: list[tuple[Mapping[str, object], object]] = []
    for row in responses:
        if not isinstance(row, dict):
            raise HistoricalSnapshotError("snapshot raw response must be an object")
        expected = {"method", "url", "status", "retrieved_at", "response_headers", "body", "body_hex", "body_sha256"}
        if row.get("method") == "POST":
            expected.add("request")
        if set(row) != expected:
            raise HistoricalSnapshotError("snapshot raw response has an invalid schema")
        if row["method"] not in {"GET", "POST"} or isinstance(row["status"], bool) or not isinstance(row["status"], int):
            raise HistoricalSnapshotError("snapshot raw response method/status is invalid")
        if not isinstance(row["url"], str) or not isinstance(row["response_headers"], dict):
            raise HistoricalSnapshotError("snapshot raw response URL/headers are invalid")
        if not isinstance(row["body_hex"], str) or not isinstance(row["body_sha256"], str):
            raise HistoricalSnapshotError("snapshot raw response body evidence is invalid")
        try:
            body_bytes = bytes.fromhex(row["body_hex"])
            parsed = json.loads(body_bytes, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
            _finite_json(parsed)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise HistoricalSnapshotError("snapshot raw response body is not strict JSON") from error
        if hashlib.sha256(body_bytes).hexdigest() != row["body_sha256"] or parsed != row["body"]:
            raise HistoricalSnapshotError("snapshot raw response body/hash mismatch")
        decoded.append((row, parsed))

    events_url = (
        f"{HistoricalTransitionsClient.GAMMA_API_BASE}/events?series_id={config.series_id}"
        "&closed=true&limit=200&order=endDate&ascending=true"
    )
    gamma = [parsed for row, parsed in decoded if row["method"] == "GET" and row["url"] == events_url]
    series = raw["series"]
    if len(gamma) != 1 or not isinstance(series, dict) or gamma[0] != series.get("events"):
        raise HistoricalSnapshotError("snapshot Gamma evidence does not reconcile to stored series")

    post_rows = [(row, parsed) for row, parsed in decoded if row["method"] == "POST"]
    used_posts: set[int] = set()
    history_windows = raw["history_windows"]
    if not isinstance(history_windows, list):
        raise HistoricalSnapshotError("snapshot history_windows must be an array")
    for window in history_windows:
        if not isinstance(window, dict):
            raise HistoricalSnapshotError("snapshot history window must be an object")
        requested = window["requested_tokens"]
        if not isinstance(requested, list) or any(not isinstance(item, str) for item in requested):
            raise HistoricalSnapshotError("snapshot requested tokens are invalid")
        combined: dict[str, object] = {}
        for index, (row, parsed) in enumerate(post_rows):
            request = row.get("request")
            if not isinstance(request, dict):
                raise HistoricalSnapshotError("snapshot CLOB request evidence is invalid")
            markets = request.get("markets")
            if (
                request.get("start_ts") != window["start_ts"]
                or request.get("end_ts") != window["end_ts"]
                or request.get("fidelity") != 1
                or not isinstance(markets, list)
                or not 0 < len(markets) <= 20
                or not set(markets).issubset(set(requested))
            ):
                continue
            if row["url"] != f"{HistoricalTransitionsClient.CLOB_API_BASE}/batch-prices-history":
                raise HistoricalSnapshotError("snapshot CLOB endpoint is invalid")
            if not isinstance(parsed, dict) or not isinstance(parsed.get("history"), dict):
                raise HistoricalSnapshotError("snapshot CLOB response evidence is invalid")
            for token, points in parsed["history"].items():
                if token in combined:
                    raise HistoricalSnapshotError("snapshot CLOB batches overlap")
                combined[token] = points
            used_posts.add(index)
        if set(combined) != set(requested) or combined != window["history"]:
            raise HistoricalSnapshotError("snapshot CLOB evidence does not reconcile to history window")
    if used_posts != set(range(len(post_rows))):
        raise HistoricalSnapshotError("snapshot contains unassociated CLOB response evidence")


def load_historical_snapshot(path: Path, config: HistoricalConfig) -> HistoricalSnapshot:
    """Load strict standard JSON, validate evidence hashes, and perform no HTTP."""
    raw = _strict_json(Path(path), HistoricalSnapshotError)
    if not isinstance(raw, dict) or set(raw) != _SNAPSHOT_KEYS:
        raise HistoricalSnapshotError("snapshot has an invalid top-level schema")
    if isinstance(raw["schema_version"], bool) or raw["schema_version"] != 1 or not isinstance(raw["schema_version"], int):
        raise HistoricalSnapshotError("snapshot schema_version must be integer 1")
    if isinstance(raw["series_id"], bool) or raw["series_id"] != 35 or not isinstance(raw["series_id"], int):
        raise HistoricalSnapshotError("snapshot series_id must be integer 35")
    for name in (
        "config_sha256", "gamma_api_base", "clob_api_base", "topology_rules_version",
        "official_decision_sha256", "topology_blind_sha256",
    ):
        if not isinstance(raw[name], str) or not raw[name]:
            raise HistoricalSnapshotError(f"snapshot {name} must be a non-empty string")
    for name in ("config_sha256", "official_decision_sha256", "topology_blind_sha256"):
        if re.fullmatch(r"[0-9a-f]{64}", raw[name]) is None:
            raise HistoricalSnapshotError(f"snapshot {name} must be a lowercase SHA-256")
    for name in ("official_decision_ledger", "topology_ledger", "history_windows", "raw_responses"):
        if not isinstance(raw[name], list) or any(not isinstance(item, dict) for item in raw[name]):
            raise HistoricalSnapshotError(f"snapshot {name} must be an array of objects")
    if not isinstance(raw["series"], dict):
        raise HistoricalSnapshotError("snapshot series must be an object")
    if not isinstance(raw["runtime_provenance"], dict):
        raise HistoricalSnapshotError("snapshot runtime_provenance must be an object")
    fetched = raw["fetched_at"]
    if not isinstance(fetched, str) or not fetched.endswith("Z"):
        raise HistoricalSnapshotError("snapshot fetched_at must be a UTC Z timestamp")
    try:
        datetime.fromisoformat(fetched[:-1] + "+00:00")
    except ValueError as error:
        raise HistoricalSnapshotError("snapshot fetched_at is invalid") from error
    for window in raw["history_windows"]:
        if set(window) != {"decision_date", "next_meeting_date", "start_ts", "end_ts", "requested_tokens", "history"}:
            raise HistoricalSnapshotError("snapshot history window has an invalid schema")
        requested = window.get("requested_tokens")
        history = window.get("history")
        if not isinstance(requested, list) or any(not isinstance(item, str) for item in requested) or not isinstance(history, dict):
            raise HistoricalSnapshotError("snapshot history window is invalid")
        _parse_batch_history({"history": history}, requested)
    _validate_raw_evidence(raw, config)
    snapshot = HistoricalSnapshot(
        int(raw["schema_version"]), str(raw["config_sha256"]), fetched,
        str(raw["gamma_api_base"]), str(raw["clob_api_base"]), int(raw["series_id"]),
        str(raw["topology_rules_version"]), tuple(raw["official_decision_ledger"]),
        str(raw["official_decision_sha256"]), raw["series"], tuple(raw["topology_ledger"]),
        str(raw["topology_blind_sha256"]), tuple(raw["history_windows"]), raw["runtime_provenance"], tuple(raw["raw_responses"]),
    )
    _validate_snapshot_identity(snapshot, config)
    rebuilt_ledger, _ = _topology(snapshot.series, config)
    if rebuilt_ledger != tuple(_thaw_mapping(item) for item in snapshot.topology_ledger):
        raise HistoricalSnapshotError("stored topology ledger does not reconcile to Gamma evidence")
    return snapshot
