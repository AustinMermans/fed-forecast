"""Pure meeting-action decomposition and one-shock-at-a-time path scenarios."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import date
from itertools import product

from .fed_path_models import FedPathConfig, MeetingDistribution


class MeetingScenarioError(ValueError):
    """Raised when market marginals cannot support the scenario calculation."""


DEFAULT_TREE_SETTINGS = {
    "dependence_strength": 0.35,
    "dependence_decay": 0.70,
    "rake_tolerance": 1e-10,
    "rake_max_iterations": 2000,
}


def _finite(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MeetingScenarioError(f"{label} must be finite")
    result = float(value)
    if not math.isfinite(result):
        raise MeetingScenarioError(f"{label} must be finite")
    return result


def _terminal_expectation(config: FedPathConfig, raw_prices: Mapping[str, float]) -> dict[str, object]:
    expected_labels = [bucket.label for bucket in config.terminal_buckets]
    if set(raw_prices) != set(expected_labels) or len(raw_prices) != len(expected_labels):
        raise MeetingScenarioError("terminal market must contain the exact configured buckets")
    raw = {label: _finite(raw_prices[label], f"terminal {label}") for label in expected_labels}
    if any(not 0.0 <= value <= 1.0 for value in raw.values()):
        raise MeetingScenarioError("terminal prices must lie within [0, 1]")
    total = sum(raw.values())
    if total <= 0:
        raise MeetingScenarioError("terminal raw total must be positive")
    probabilities = {label: raw[label] / total for label in expected_labels}
    expected_upper = sum(
        probabilities[bucket.label] * bucket.representative_rate
        for bucket in config.terminal_buckets
    )
    return {
        "date": None,
        "event_slug": config.terminal_event_slug,
        "raw_total": total,
        "probabilities": probabilities,
        "expected_target_upper": expected_upper,
        "expected_effective_rate": expected_upper - (config.target_upper_bound - config.effective_rate_baseline),
    }


def _categories(meeting: MeetingDistribution) -> list[dict[str, object]]:
    prices = meeting.prices
    groups = (
        ("down", prices[:2]),
        ("unchanged", prices[2:3]),
        ("up", prices[3:]),
    )
    output = []
    for name, group in groups:
        probability = sum(item.probability for item in group)
        if probability <= 0:
            conditional = None
        else:
            conditional = sum(item.probability * item.representative_bp for item in group) / probability
        output.append({
            "category": name,
            "probability": probability,
            "conditional_change_bp": conditional,
        })
    return output


def _tree_settings(raw: Mapping[str, object] | None) -> dict[str, float | int]:
    values: dict[str, object] = {**DEFAULT_TREE_SETTINGS, **({} if raw is None else dict(raw))}
    expected = set(DEFAULT_TREE_SETTINGS)
    if set(values) != expected:
        raise MeetingScenarioError("conditional tree settings have invalid fields")
    strength = _finite(values["dependence_strength"], "dependence_strength")
    decay = _finite(values["dependence_decay"], "dependence_decay")
    tolerance = _finite(values["rake_tolerance"], "rake_tolerance")
    iterations = values["rake_max_iterations"]
    if isinstance(iterations, bool) or not isinstance(iterations, int):
        raise MeetingScenarioError("rake_max_iterations must be an integer")
    if strength < 0 or not 0 <= decay <= 1 or tolerance <= 0 or iterations <= 0:
        raise MeetingScenarioError("conditional tree settings are outside their valid ranges")
    return {
        "dependence_strength": strength,
        "dependence_decay": decay,
        "rake_tolerance": tolerance,
        "rake_max_iterations": iterations,
    }


def _build_conditional_tree(
    config: FedPathConfig,
    meeting_rows: list[dict[str, object]],
    raw_settings: Mapping[str, object] | None,
) -> dict[str, object]:
    """Build a conditional five-outcome tree while preserving every quote.

    The joint distribution is the minimum-relative-entropy distribution around
    a configurable persistent policy-stance kernel. Iterative proportional
    fitting then restores every quoted meeting marginal exactly. The separately
    traded year-end level market is deliberately excluded: it is an independent
    comparison, not a constraint on a lattice of discrete meeting actions.
    """
    settings = _tree_settings(raw_settings)
    categories = ("down_50plus", "down_25", "unchanged", "up_25", "up_50plus")
    scores = (-1.0, -0.5, 0.0, 0.5, 1.0)
    outcome_count = len(categories)
    meeting_probabilities: list[tuple[float, ...]] = []
    meeting_actions: list[tuple[float, ...]] = []
    for row in meeting_rows:
        prices = row.get("prices")
        if not isinstance(prices, list) or len(prices) != outcome_count:
            raise MeetingScenarioError("conditional tree requires five ordered meeting prices")
        probabilities = tuple(float(item["probability"]) for item in prices)
        if any(value <= 0.0 for value in probabilities):
            raise MeetingScenarioError("conditional tree requires positive probability for every exact action")
        actions = tuple(float(item["representative_bp"]) for item in prices)
        if actions != (-50.0, -25.0, 0.0, 25.0, 50.0):
            raise MeetingScenarioError("conditional tree requires ordered -50/-25/0/+25/+50 representatives")
        meeting_probabilities.append(probabilities)
        meeting_actions.append(actions)

    outcome_paths = list(product(range(outcome_count), repeat=len(meeting_rows)))
    log_kernels: list[float] = []
    for path in outcome_paths:
        persistence = 0.0
        for left in range(len(path)):
            for right in range(left + 1, len(path)):
                lag = right - left - 1
                persistence += (float(settings["dependence_decay"]) ** lag) * scores[path[left]] * scores[path[right]]
        log_kernels.append(float(settings["dependence_strength"]) * persistence)
    maximum_log_kernel = max(log_kernels)
    weights = []
    for path, log_kernel in zip(outcome_paths, log_kernels, strict=True):
        marginal_product = 1.0
        for index, outcome in enumerate(path):
            marginal_product *= meeting_probabilities[index][outcome]
        weights.append(marginal_product * math.exp(max(-700.0, log_kernel - maximum_log_kernel)))
    total = sum(weights)
    if total <= 0:
        raise MeetingScenarioError("conditional tree coupling produced zero support")
    weights = [value / total for value in weights]

    dimensions = len(meeting_rows)
    targets = [tuple(item) for item in meeting_probabilities]
    converged = False
    iterations_used = 0
    max_error = math.inf
    for iteration in range(int(settings["rake_max_iterations"])):
        for dimension in range(dimensions):
            size = outcome_count
            totals = [0.0] * size
            for state_index, path in enumerate(outcome_paths):
                category_index = path[dimension]
                totals[category_index] += weights[state_index]
            factors = [targets[dimension][index] / totals[index] if totals[index] > 0 else 0.0 for index in range(size)]
            for state_index, path in enumerate(outcome_paths):
                category_index = path[dimension]
                weights[state_index] *= factors[category_index]
        max_error = 0.0
        for dimension in range(dimensions):
            size = outcome_count
            totals = [0.0] * size
            for state_index, path in enumerate(outcome_paths):
                category_index = path[dimension]
                totals[category_index] += weights[state_index]
            max_error = max(max_error, *(abs(totals[index] - targets[dimension][index]) for index in range(size)))
        iterations_used = iteration + 1
        if max_error <= float(settings["rake_tolerance"]):
            converged = True
            break
    if not converged:
        raise MeetingScenarioError(f"conditional tree raking did not converge; max error {max_error:.3g}")

    def matching_total(prefix: tuple[int, ...]) -> float:
        return sum(weight for weight, path in zip(weights, outcome_paths, strict=True) if path[:len(prefix)] == prefix)

    nodes: list[dict[str, object]] = []
    node_ids: dict[tuple[int, ...], str] = {}
    for depth in range(len(meeting_rows) + 1):
        for prefix in product(range(outcome_count), repeat=depth):
            node_ids[prefix] = "root" if not prefix else "_".join(categories[index] for index in prefix)
    for depth in range(len(meeting_rows) + 1):
        for prefix in product(range(outcome_count), repeat=depth):
            probability = matching_total(prefix)
            realized_actions = [meeting_actions[index][outcome] for index, outcome in enumerate(prefix)]
            representative_upper = config.target_upper_bound + sum(realized_actions) / 100.0
            rate_distribution = [{"rate": representative_upper, "probability": 1.0}]
            next_probabilities = None
            branches: list[dict[str, object]] = []
            if depth < len(meeting_rows):
                denominator = probability
                conditional = [matching_total(prefix + (outcome,)) / denominator for outcome in range(outcome_count)]
                next_probabilities = dict(zip(categories, conditional, strict=True))
                for outcome, branch_probability in enumerate(conditional):
                    child_prefix = prefix + (outcome,)
                    branches.append({
                        "category": categories[outcome],
                        "representative_action_bp": meeting_actions[depth][outcome],
                        "conditional_probability": branch_probability,
                        "path_probability": matching_total(child_prefix),
                        "child_node_id": node_ids[child_prefix],
                    })
            nodes.append({
                "node_id": node_ids[prefix],
                "parent_node_id": None if not prefix else node_ids[prefix[:-1]],
                "depth": depth,
                "realized_path": [categories[index] for index in prefix],
                "path_probability": probability,
                "last_realized_date": None if not prefix else meeting_rows[depth - 1]["date"],
                "next_meeting_date": None if depth == len(meeting_rows) else meeting_rows[depth]["date"],
                "next_probabilities": next_probabilities,
                "action_implied_target_upper": representative_upper,
                "representative_target_upper": representative_upper,
                "rate_distribution": rate_distribution,
                "branches": branches,
            })

    adjacent_tables = []
    for index in range(len(meeting_rows) - 1):
        rows = []
        for current_outcome in range(outcome_count):
            numerator = [0.0] * outcome_count
            denominator = 0.0
            for weight, path in zip(weights, outcome_paths, strict=True):
                if path[index] != current_outcome:
                    continue
                denominator += weight
                numerator[path[index + 1]] += weight
            rows.append({
                "realized_category": categories[current_outcome],
                "realized_probability": meeting_probabilities[index][current_outcome],
                "next_probabilities": dict(zip(categories, (value / denominator for value in numerator), strict=True)),
            })
        adjacent_tables.append({
            "realized_meeting_date": meeting_rows[index]["date"],
            "next_meeting_date": meeting_rows[index + 1]["date"],
            "rows": rows,
        })

    leaf_paths = []
    for path in outcome_paths:
        prefix = tuple(path)
        node = next(item for item in nodes if item["node_id"] == node_ids[prefix])
        leaf_paths.append({
            "path": [categories[index] for index in path],
            "path_probability": node["path_probability"],
            "representative_target_upper_after_last_meeting": node["representative_target_upper"],
        })
    leaf_paths.sort(key=lambda item: -float(item["path_probability"]))
    return {
        "model": "marginal-preserving conditional five-outcome tree",
        "settings": settings,
        "raking": {"converged": converged, "iterations": iterations_used, "max_marginal_error": max_error},
        "quoted_marginals_preserved": True,
        "root_node_id": "root",
        "node_count": len(nodes),
        "leaf_count": len(leaf_paths),
        "nodes": nodes,
        "adjacent_conditional_tables": adjacent_tables,
        "leaf_paths": leaf_paths,
        "interpretation": {
            "observed": "All five exact meeting-action probabilities are taken from normalized quoted markets.",
            "modeled": "Conditional future probabilities are generated by a persistent policy-stance coupling, then raked back to every observed meeting marginal.",
            "not_identified": "The conditional transition matrices are assumptions disciplined by market marginals, not directly traded conditional probabilities.",
            "year_end_market": "The separately traded year-end rate distribution is reported as an independent comparison and does not constrain this action tree.",
        },
    }


def compute_meeting_scenarios(
    config: FedPathConfig,
    meetings: tuple[MeetingDistribution, ...],
    terminal_prices: Mapping[str, float],
    *,
    terminal_date: date,
    generated_at: str,
    snapshot_fetched_at: str,
    tree_settings: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Calculate marginal meeting forecasts and isolated action-shock paths.

    A scenario replaces one meeting's expected move by the conditional mean of
    its down/unchanged/up category. Every later meeting's own action
    distribution is held fixed and the surprise carries forward one-for-one.
    The separately traded year-end level is retained only as a comparison.
    """
    if len(meetings) != len(config.meetings) or not meetings:
        raise MeetingScenarioError("meeting distributions must match the configured meetings")
    if tuple(item.config for item in meetings) != config.meetings:
        raise MeetingScenarioError("meeting distributions are out of order or mismatched")
    if tuple(sorted(item.config.date for item in meetings)) != tuple(item.config.date for item in meetings):
        raise MeetingScenarioError("meetings must be chronological")
    terminal = _terminal_expectation(config, terminal_prices)
    terminal["date"] = terminal_date.isoformat()
    spread = config.target_upper_bound - config.effective_rate_baseline

    meeting_rows: list[dict[str, object]] = []
    baseline_points: list[dict[str, object]] = []
    current_upper = config.target_upper_bound
    for meeting in meetings:
        before = current_upper
        current_upper += meeting.expected_change_bp / 100.0
        categories = _categories(meeting)
        row = {
            "date": meeting.config.date.isoformat(),
            "event_slug": meeting.config.event_slug,
            "raw_total": meeting.raw_total,
            "expected_change_bp": meeting.expected_change_bp,
            "decrease_probability": meeting.decrease_probability,
            "no_change_probability": meeting.no_change_probability,
            "increase_probability": meeting.increase_probability,
            "expected_target_upper_before": before,
            "expected_target_upper_after": current_upper,
            "expected_effective_rate_after": current_upper - spread,
            "categories": categories,
            "prices": [price.to_dict() for price in meeting.prices],
        }
        meeting_rows.append(row)
        baseline_points.append({
            "date": meeting.config.date.isoformat(), "kind": "meeting_action",
            "expected_target_upper": current_upper,
            "expected_effective_rate": current_upper - spread,
        })

    scenarios: list[dict[str, object]] = []
    for index, meeting in enumerate(meeting_rows):
        next_meeting = meeting_rows[index + 1] if index + 1 < len(meeting_rows) else None
        baseline_change = float(meeting["expected_change_bp"])
        shock_date = date.fromisoformat(str(meeting["date"]))
        for category in meeting["categories"]:
            scenario_change = category["conditional_change_bp"]
            shock = None if scenario_change is None else float(scenario_change) - baseline_change
            downstream = []
            if shock is not None:
                for point in baseline_points:
                    point_date = date.fromisoformat(str(point["date"]))
                    if point_date < shock_date:
                        continue
                    baseline_upper = float(point["expected_target_upper"])
                    mechanical_upper = baseline_upper + shock / 100.0
                    downstream.append({
                        "date": point["date"], "kind": point["kind"],
                        "baseline_expected_target_upper": baseline_upper,
                        "mechanical_expected_target_upper": mechanical_upper,
                        "anchor_respecting_expected_target_upper": mechanical_upper,
                        "mechanical_change_bp": shock,
                        "anchor_respecting_change_bp": shock,
                    })
            scenarios.append({
                "shock_meeting_date": meeting["date"],
                "shock_event_slug": meeting["event_slug"],
                "category": category["category"],
                "scenario_probability": category["probability"],
                "scenario_change_bp": scenario_change,
                "baseline_expected_change_bp": baseline_change,
                "surprise_vs_baseline_bp": shock,
                "next_meeting_date": None if next_meeting is None else next_meeting["date"],
                "next_meeting_expected_change_bp": None if next_meeting is None else next_meeting["expected_change_bp"],
                "next_meeting_probabilities_unchanged": None if next_meeting is None else {
                    "down": next_meeting["decrease_probability"],
                    "unchanged": next_meeting["no_change_probability"],
                    "up": next_meeting["increase_probability"],
                },
                "next_meeting_baseline_target_upper": None if next_meeting is None else next_meeting["expected_target_upper_after"],
                "next_meeting_mechanical_target_upper": None if next_meeting is None or shock is None else float(next_meeting["expected_target_upper_after"]) + shock / 100.0,
                "downstream": downstream,
            })

    conditional_tree = _build_conditional_tree(config, meeting_rows, tree_settings)
    return {
        "schema_version": 1,
        "generated_at": generated_at,
        "snapshot_fetched_at": snapshot_fetched_at,
        "target_upper_bound_baseline": config.target_upper_bound,
        "effective_rate_baseline": config.effective_rate_baseline,
        "baseline_spread": spread,
        "terminal_anchor": terminal,
        "meetings": meeting_rows,
        "baseline_path": baseline_points,
        "scenarios": scenarios,
        "conditional_tree": conditional_tree,
        "methodology": {
            "identified": "Each meeting's five normalized action probabilities (-50+, -25, 0, +25, +50+) and expected action.",
            "not_identified": "Conditional future action probabilities such as P(October action | September outcome) are not identified by separate marginal markets.",
            "shock_only": "Replace one meeting expected action with that category's conditional mean and leave every other meeting action distribution unchanged.",
            "year_end_comparison": "The independently traded end-2026 target-rate distribution is reported beside, but never imposed on, the discrete meeting-action path.",
            "conditional_tree": "A marginal-preserving five-outcome joint model makes each future meeting distribution conditional on the full realized path. Exact action magnitudes remain separate; shock-only grouped paths remain as a benchmark.",
        },
    }
