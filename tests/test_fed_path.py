import math
import unittest
from pathlib import Path

from fed_forecast.fed_path import FedPathError, compute_meeting_distribution
from fed_forecast.fed_path_config import load_fed_path_config
from fed_forecast.fed_path_models import MeetingPrice


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class MeetingDistributionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.meeting = load_fed_path_config(PROJECT_ROOT / "config/fed_path.json").meetings[0]

    def prices(self, values: tuple[float, ...]) -> tuple[MeetingPrice, ...]:
        return tuple(
            MeetingPrice(self.meeting.date, outcome.label, value)
            for outcome, value in zip(reversed(self.meeting.outcomes), reversed(values), strict=True)
        )

    def test_normalizes_complete_prices_before_expected_value(self) -> None:
        result = compute_meeting_distribution(self.meeting, self.prices((.05, .15, .4, .3, .2)))
        self.assertEqual(result.raw_total, 1.1)
        self.assertEqual([item.label for item in result.prices], [item.label for item in self.meeting.outcomes])
        self.assertTrue(math.isclose(sum(item.probability for item in result.prices), 1.0, abs_tol=1e-12))
        self.assertTrue(math.isclose(result.expected_change_bp, 10.227272727272727, abs_tol=1e-12))
        self.assertEqual((result.negative_tail_probability, result.positive_tail_probability), (.045454545454545456, .18181818181818182))

    def test_rejects_nonexact_topology_and_invalid_totals(self) -> None:
        prices = list(self.prices((.2, .2, .2, .2, .2)))
        prices[-1] = MeetingPrice(self.meeting.date, "unknown", .2)
        with self.assertRaisesRegex(FedPathError, "topology"):
            compute_meeting_distribution(self.meeting, tuple(prices))
        with self.assertRaisesRegex(FedPathError, "raw total"):
            compute_meeting_distribution(self.meeting, self.prices((0, 0, 0, 0, 0)))

    def test_rejects_invalid_probability_and_date(self) -> None:
        with self.assertRaisesRegex(FedPathError, "within"):
            compute_meeting_distribution(self.meeting, self.prices((.2, .2, .2, .2, 1.2)))
        prices = list(self.prices((.2, .2, .2, .2, .2)))
        prices[0] = MeetingPrice(self.meeting.date.replace(day=self.meeting.date.day + 1), prices[0].label, .2)
        with self.assertRaisesRegex(FedPathError, "date"):
            compute_meeting_distribution(self.meeting, tuple(prices))


if __name__ == "__main__":
    unittest.main()
