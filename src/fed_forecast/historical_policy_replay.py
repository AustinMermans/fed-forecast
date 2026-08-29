"""Daily historical Polymarket policy surfaces and forward-rate fans."""

from __future__ import annotations

import math
import re
from datetime import date, datetime, timezone
from itertools import product
from typing import Mapping, Sequence


DISPLAY_BUCKETS = (
    "50+ bps decrease",
    "25 bps decrease",
    "No change",
    "25 bps increase",
    "50+ bps increase",
)


def action_from_label(label: str) -> float | None:
    """Map an official FOMC market group label to its representative bp move."""
    text = re.sub(r"\s+", " ", label.strip().casefold())
    if text == "other":
        return None
    if "no change" in text:
        return 0.0
    match = re.search(r"(\d+)\s*\+?\s*bps?", text)
    if not match:
        raise ValueError(f"unrecognized FOMC outcome label: {label!r}")
    magnitude = float(match.group(1))
    if "decrease" in text:
        return -magnitude
    if "increase" in text:
        return magnitude
    raise ValueError(f"unrecognized FOMC outcome direction: {label!r}")


def display_probabilities(outcomes: Sequence[Mapping[str, object]]) -> dict[str, float]:
    """Aggregate native outcome probabilities into the dashboard's five columns."""
    result = {label: 0.0 for label in DISPLAY_BUCKETS}
    for item in outcomes:
        action = float(item["representative_bp"])
        probability = float(item["probability"])
        if action <= -50:
            key = DISPLAY_BUCKETS[0]
        elif action < 0:
            key = DISPLAY_BUCKETS[1]
        elif action == 0:
            key = DISPLAY_BUCKETS[2]
        elif action < 50:
            key = DISPLAY_BUCKETS[3]
        else:
            key = DISPLAY_BUCKETS[4]
        result[key] += probability
    return result


def _quantile(distribution: Mapping[float, float], level: float) -> float:
    total = sum(distribution.values())
    cumulative = 0.0
    for value, weight in sorted(distribution.items()):
        cumulative += weight / total
        if cumulative >= level - 1e-12:
            return value
    return max(distribution)


def _summary(distribution: Mapping[float, float]) -> dict[str, float]:
    total = sum(distribution.values())
    return {
        "q05": _quantile(distribution, 0.05),
        "q25": _quantile(distribution, 0.25),
        "q50": _quantile(distribution, 0.50),
        "q75": _quantile(distribution, 0.75),
        "q95": _quantile(distribution, 0.95),
        "mean": sum(value * weight for value, weight in distribution.items()) / total,
    }


def forward_fan(
    baseline: float,
    meetings: Sequence[Mapping[str, object]],
    *,
    vintage_date: str,
    terminal: Mapping[str, object] | None = None,
    dependence_strength: float = 0.55,
    dependence_decay: float = 0.72,
    tolerance: float = 1e-10,
    max_iterations: int = 500,
) -> list[dict[str, object]]:
    """Build a marginal-preserving meeting-only joint path distribution.

    The native action buckets may vary across meetings. A modest persistent-
    stance kernel supplies dependence; iterative proportional fitting restores
    each quoted meeting marginal exactly.
    """
    if not meetings:
        raise ValueError("forward fan needs at least one meeting")
    actions: list[tuple[float, ...]] = []
    marginals: list[tuple[float, ...]] = []
    for meeting in meetings:
        native = meeting.get("native_outcomes")
        if not isinstance(native, list) or not native:
            raise ValueError("meeting is missing native outcomes")
        meeting_actions = tuple(float(item["representative_bp"]) for item in native)
        meeting_probabilities = tuple(float(item["probability"]) for item in native)
        if any(value < 0 or not math.isfinite(value) for value in meeting_probabilities):
            raise ValueError("meeting probabilities must be finite and non-negative")
        total = sum(meeting_probabilities)
        if total <= 0:
            raise ValueError("meeting probabilities have zero support")
        actions.append(meeting_actions)
        marginals.append(tuple(value / total for value in meeting_probabilities))

    states = list(product(*(range(len(values)) for values in actions)))
    weights: list[float] = []
    for state in states:
        marginal = math.prod(marginals[index][outcome] for index, outcome in enumerate(state))
        persistence = sum(
            dependence_decay ** (right - left - 1)
            * max(-1.5, min(1.5, actions[left][state[left]] / 50.0))
            * max(-1.5, min(1.5, actions[right][state[right]] / 50.0))
            for left in range(len(state))
            for right in range(left + 1, len(state))
        )
        weights.append(marginal * math.exp(max(-700.0, dependence_strength * persistence)))
    total = sum(weights)
    weights = [value / total for value in weights]

    for _ in range(max_iterations):
        for dimension, target in enumerate(marginals):
            totals = [0.0] * len(target)
            for state_index, state in enumerate(states):
                totals[state[dimension]] += weights[state_index]
            factors = [target[index] / value if value else 0.0 for index, value in enumerate(totals)]
            for state_index, state in enumerate(states):
                weights[state_index] *= factors[state[dimension]]
        maximum_error = 0.0
        for dimension, target in enumerate(marginals):
            totals = [0.0] * len(target)
            for state_index, state in enumerate(states):
                totals[state[dimension]] += weights[state_index]
            maximum_error = max(maximum_error, *(abs(a - b) for a, b in zip(totals, target, strict=True)))
        if maximum_error <= tolerance:
            break
    else:
        raise ValueError(f"historical path raking did not converge: {maximum_error:.3g}")

    output: list[dict[str, object]] = [
        {"date": vintage_date, "kind": "vintage", **_summary({baseline: 1.0})}
    ]
    for horizon, meeting in enumerate(meetings):
        distribution: dict[float, float] = {}
        for weight, state in zip(weights, states, strict=True):
            move = sum(actions[index][state[index]] for index in range(horizon + 1))
            rate = baseline + move / 100.0
            distribution[rate] = distribution.get(rate, 0.0) + weight
        output.append({"date": str(meeting["date"]), "kind": "meeting", **_summary(distribution)})
    # ``terminal`` is accepted for backward-compatible archive construction,
    # but deliberately does not alter the meeting-action fan. The independently
    # traded year-end level belongs in a separate comparison series.
    return sorted(output, key=lambda item: (str(item["date"]), str(item["kind"])))


def baseline_for_day(day: date, decisions: Sequence[Mapping[str, object]]) -> float:
    """Return the official target-range upper bound prevailing on a date."""
    eligible = [item for item in decisions if date.fromisoformat(str(item["date"])) <= day]
    if eligible:
        return float(eligible[-1]["after"][1])  # type: ignore[index]
    future = [item for item in decisions if date.fromisoformat(str(item["date"])) > day]
    if not future:
        raise ValueError("decision ledger cannot anchor historical baseline")
    return float(future[0]["before"][1])  # type: ignore[index]


def iso_at_source_time(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")
