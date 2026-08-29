from __future__ import annotations

import json
import math
import unittest
from dataclasses import replace
from datetime import date, timedelta

from fed_forecast.historical_transitions import (
    CATEGORIES,
    HistoricalTransitionError,
    TransitionObservation,
    apply_potential,
    build_historical_transition_result,
    candidate_surprises,
    empirical_identification,
    evaluate_production_gates,
    fit_potential,
    ipf_joint,
    no_update_prediction,
    realized_surprise,
    scalar_persistence_prediction,
    smooth_support,
    surface_metrics,
    walk_forward_validate,
)


def observation(index: int, category: str | None = None) -> TransitionObservation:
    category = category or CATEGORIES[index % 3]
    current_date = date(2022, 1, 1) + timedelta(days=42 * index)
    next_date = current_date + timedelta(days=42)
    current_options = (
        (0.18, 0.62, 0.20),
        (0.27, 0.48, 0.25),
        (0.22, 0.51, 0.27),
        (0.31, 0.44, 0.25),
    )
    next_options = (
        (0.24, 0.54, 0.22),
        (0.29, 0.49, 0.22),
        (0.20, 0.55, 0.25),
    )
    post_by_category = {
        "down": (0.48, 0.43, 0.09),
        "unchanged": (0.20, 0.64, 0.16),
        "up": (0.08, 0.39, 0.53),
    }
    realized_actions = {"down": -25.0, "unchanged": 0.0, "up": 25.0}
    return TransitionObservation(
        current_meeting_id=f"meeting-{index:02d}",
        next_meeting_id=f"meeting-{index + 1:02d}",
        current_meeting_date=current_date,
        next_meeting_date=next_date,
        current_pre=current_options[index % len(current_options)],
        current_candidate_actions_bp=(-25.0, 0.0, 25.0),
        realized_category=category,
        realized_action_bp=realized_actions[category],
        next_pre=next_options[index % len(next_options)],
        next_post=post_by_category[category],
        next_realized_category=CATEGORIES[(index + 1) % 3],
    )


class HistoricalTransitionTests(unittest.TestCase):
    def test_observation_is_deeply_immutable_and_serializes_explicitly(self) -> None:
        mutable_current = [0.2, 0.6, 0.2]
        item = TransitionObservation(
            current_meeting_id="a",
            next_meeting_id="b",
            current_meeting_date=date(2024, 1, 1),
            next_meeting_date=date(2024, 2, 1),
            current_pre=mutable_current,  # type: ignore[arg-type]
            current_candidate_actions_bp=[-25, 0, 25],  # type: ignore[arg-type]
            realized_category="unchanged",
            realized_action_bp=0,
            next_pre=[0.1, 0.7, 0.2],  # type: ignore[arg-type]
            next_post=[0.15, 0.65, 0.2],  # type: ignore[arg-type]
        )
        mutable_current[0] = 0.9
        self.assertEqual(item.current_pre, (0.2, 0.6, 0.2))
        payload = item.to_dict()
        payload["current_pre"][0] = 0.9  # type: ignore[index]
        self.assertEqual(item.current_pre, (0.2, 0.6, 0.2))
        self.assertEqual(item.to_dict()["current_meeting_date"], "2024-01-01")
        json.dumps(item.to_dict(), allow_nan=False)

    def test_observation_rejects_invalid_topology_and_probabilities(self) -> None:
        with self.assertRaises(HistoricalTransitionError):
            replace(observation(0), current_candidate_actions_bp=(0.0, -25.0, 25.0))
        with self.assertRaises(HistoricalTransitionError):
            replace(observation(0), next_pre=(0.0, 0.0, 0.0))
        with self.assertRaises(HistoricalTransitionError):
            replace(observation(0), next_meeting_date=observation(0).current_meeting_date)

    def test_support_smoothing_and_surface_metrics_are_hand_calculable(self) -> None:
        smoothed = smooth_support((0.0, 0.0, 1.0), floor=0.005)
        self.assertAlmostEqual(sum(smoothed), 1.0)
        self.assertGreater(smoothed[0], 0.0)
        self.assertGreater(smoothed[1], 0.0)
        same = surface_metrics((0.2, 0.5, 0.3), (0.2, 0.5, 0.3))
        entropy = -sum(value * math.log(value) for value in (0.2, 0.5, 0.3))
        self.assertAlmostEqual(same.cross_entropy, entropy)
        self.assertAlmostEqual(same.kl_divergence, 0.0)
        self.assertAlmostEqual(same.total_variation, 0.0)

    def test_candidate_and_realized_surprises_are_distinct(self) -> None:
        item = replace(
            observation(2, "up"),
            current_pre=(0.2, 0.5, 0.3),
            realized_action_bp=50.0,
        )
        self.assertEqual(candidate_surprises(item), (-1.1, -0.1, 0.9))
        self.assertAlmostEqual(realized_surprise(item), 1.9)

    def test_ipf_joint_preserves_marginals_and_apply_uses_candidate_surprise(self) -> None:
        item = observation(2, "up")
        theta = (
            (0.30, -0.10, -0.20),
            (-0.05, 0.15, -0.10),
            (-0.20, -0.05, 0.25),
        )
        current = smooth_support(item.current_pre)
        future = smooth_support(item.next_pre)
        joint = ipf_joint(item.current_pre, item.next_pre, candidate_surprises(item), theta)
        for row in range(3):
            self.assertAlmostEqual(sum(joint[row]), current[row], places=10)
        for column in range(3):
            self.assertAlmostEqual(sum(joint[row][column] for row in range(3)), future[column], places=10)
        prediction = apply_potential(item, theta)
        fifty_bp = replace(item, realized_action_bp=50.0)
        self.assertEqual(prediction, apply_potential(fifty_bp, theta))
        self.assertAlmostEqual(sum(prediction), 1.0)

    def test_no_update_and_scalar_persistence_are_separate_benchmarks(self) -> None:
        item = observation(2, "up")
        no_update = no_update_prediction(item)
        scalar = scalar_persistence_prediction(item, strength=0.35)
        self.assertAlmostEqual(sum(no_update), 1.0)
        self.assertAlmostEqual(sum(scalar), 1.0)
        self.assertNotEqual(no_update, scalar)

    def test_fit_is_deterministic_row_centered_and_improves_surface_fit(self) -> None:
        sample = tuple(observation(index) for index in range(9))
        options = {
            "penalty": 0.01,
            "step_size": 0.1,
            "tolerance": 1e-4,
            "max_iterations": 150,
        }
        first = fit_potential(sample, **options)
        second = fit_potential(tuple(reversed(sample)), **options)
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertTrue(first.converged)
        for row in first.theta:
            self.assertAlmostEqual(sum(row), 0.0, places=12)
        learned = sum(
            surface_metrics(apply_potential(item, first.theta), item.next_post).cross_entropy
            for item in sample
        ) / len(sample)
        baseline = sum(
            surface_metrics(no_update_prediction(item), item.next_post).cross_entropy
            for item in sample
        ) / len(sample)
        self.assertLess(learned, baseline)
        json.dumps(first.to_dict(), allow_nan=False)

    def test_average_objective_does_not_change_when_sample_is_replicated(self) -> None:
        base = tuple(observation(index) for index in range(6))
        replicated = tuple(
            replace(
                item,
                current_meeting_id=f"copy-{copy}-{item.current_meeting_id}",
                next_meeting_id=f"copy-{copy}-{item.next_meeting_id}",
            )
            for copy in range(2)
            for item in base
        )
        options = {
            "penalty": 0.02,
            "step_size": 0.1,
            "tolerance": 1e-4,
            "max_iterations": 150,
        }
        first = fit_potential(base, **options)
        second = fit_potential(replicated, **options)
        self.assertEqual(first.theta, second.theta)
        self.assertAlmostEqual(first.objective, second.objective)

    def test_duplicate_transition_cannot_inflate_training_or_gate_counts(self) -> None:
        item = observation(0)
        with self.assertRaises(HistoricalTransitionError):
            fit_potential((item, item))

    def test_walk_forward_enforces_chronological_count_and_row_gates(self) -> None:
        sample = tuple(observation(index) for index in range(10))
        result = walk_forward_validate(
            tuple(reversed(sample)),
            min_training=6,
            min_row_training=2,
            penalty=0.02,
            step_size=0.1,
            tolerance=1e-3,
            max_iterations=80,
            bootstrap_minimum=30,
            bootstrap_replicates=20,
        )
        self.assertEqual(result.eligible_transitions, 10)
        self.assertEqual(result.scored_transitions, 4)
        self.assertEqual(result.skipped_transitions, 6)
        self.assertTrue(all(fold.status == "skipped" for fold in result.folds[:6]))
        self.assertEqual(result.folds[6].training_count, 6)
        self.assertEqual(result.folds[6].row_counts, (2, 2, 2))
        self.assertEqual(
            tuple(item.label for item in result.folds[6].benchmarks),
            ("no_update", "scalar_persistence", "historical_potential"),
        )
        self.assertEqual(result.bootstrap.state, "unavailable_insufficient_scored_transitions")
        self.assertEqual(len(result.aggregate), 3)
        json.dumps(result.to_dict(), allow_nan=False)

    def test_production_gate_fails_closed_for_thin_unidentified_history(self) -> None:
        sample = tuple(observation(index) for index in range(9))
        model = fit_potential(
            sample,
            penalty=0.02,
            step_size=0.1,
            tolerance=1e-3,
            max_iterations=80,
        )
        gates = evaluate_production_gates(
            sample,
            model,
            provenance_valid=True,
            replay_valid=True,
        )
        self.assertFalse(gates.eligible)
        self.assertEqual(gates.status, "diagnostic_only")
        self.assertIn("minimum_transition_count_not_met", gates.failures)
        self.assertIn("minimum_down_row_count_not_met", gates.failures)
        self.assertIn("minimum_unchanged_row_count_not_met", gates.failures)
        self.assertIn("minimum_up_row_count_not_met", gates.failures)
        self.assertIn("timing_destination_not_identified", gates.failures)
        self.assertEqual(len(gates.singular_values), 6)
        json.dumps(gates.to_dict(), allow_nan=False)

        mismatched = replace(model, training_count=999)
        mismatch_gate = evaluate_production_gates(
            sample,
            mismatched,
            provenance_valid=True,
            replay_valid=True,
        )
        self.assertIn("model_training_sample_mismatch", mismatch_gate.failures)

    def test_result_uses_one_timing_identification_state_everywhere(self) -> None:
        result = build_historical_transition_result(
            tuple(observation(index) for index in range(6)),
            timing_destination_identification="identified",
            penalty_sensitivities=(2.0,),
            floor_sensitivities=(0.01,),
            walk_forward_min_training=3,
            walk_forward_min_per_row=1,
            step_size=0.1,
            tolerance=1e-3,
            max_iterations=50,
        )
        payload = result.to_dict()
        self.assertEqual(payload["timing_destination_identification"], "identified")
        self.assertEqual(payload["model"]["timing_destination_identification"], "identified")
        self.assertEqual(payload["production_gates"]["timing_destination_identification"], "identified")
        self.assertTrue(all(item["model"]["timing_destination_identification"] == "identified" for item in payload["sensitivities"]))
        self.assertNotIn("timing_destination_not_identified", payload["limitations"])

    def test_empirical_identification_is_deterministic(self) -> None:
        sample = tuple(observation(index) for index in range(12))
        model = fit_potential(
            sample,
            penalty=0.02,
            step_size=0.1,
            tolerance=1e-3,
            max_iterations=80,
        )
        first = empirical_identification(sample, model)
        second = empirical_identification(tuple(reversed(sample)), model)
        self.assertEqual(first, second)
        self.assertEqual(len(first[1]), 6)
        self.assertTrue(all(value >= 0.0 for value in first[1]))


if __name__ == "__main__":
    unittest.main()
