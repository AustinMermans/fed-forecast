import json
import tempfile
import unittest
from contextlib import contextmanager
from datetime import date
from pathlib import Path

from fed_forecast.fed_path_config import FedPathConfigError, load_fed_path_config


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config/fed_path.json"


@contextmanager
def temporary_config(payload: dict[str, object]):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "fed_path.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        yield path


class FedPathConfigTests(unittest.TestCase):
    def payload(self) -> dict[str, object]:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

    def test_public_config_contains_only_machine_readable_market_topology(self) -> None:
        payload = self.payload()
        self.assertEqual(set(payload), {
            "schema_version", "target_upper_bound", "effective_rate_baseline",
            "standard_move_bp", "max_spread", "meetings", "terminal",
        })
        config = load_fed_path_config(CONFIG_PATH)
        self.assertEqual(config.schema_version, 2)
        self.assertEqual((config.target_upper_bound, config.effective_rate_baseline), (3.75, 3.628))
        self.assertEqual(config.meetings[0].date, date(2026, 7, 29))
        self.assertEqual(len(config.meetings), 3)
        self.assertEqual(len(config.terminal_buckets), 15)

    def test_rejects_unknown_keys_duplicates_and_nonchronological_meetings(self) -> None:
        payload = self.payload()
        payload["unknown"] = True
        with temporary_config(payload) as path, self.assertRaisesRegex(FedPathConfigError, "unknown"):
            load_fed_path_config(path)
        payload = self.payload()
        payload["meetings"][1]["event_slug"] = payload["meetings"][0]["event_slug"]
        with temporary_config(payload) as path, self.assertRaisesRegex(FedPathConfigError, "unique"):
            load_fed_path_config(path)
        payload = self.payload()
        payload["meetings"][1]["date"] = "2026-07-01"
        with temporary_config(payload) as path, self.assertRaisesRegex(FedPathConfigError, "chronological"):
            load_fed_path_config(path)

    def test_rejects_wrong_topologies_and_invalid_numbers(self) -> None:
        payload = self.payload()
        payload["meetings"][0]["outcomes"][0]["representative_bp"] = -75
        with temporary_config(payload) as path, self.assertRaisesRegex(FedPathConfigError, "five-outcome"):
            load_fed_path_config(path)
        payload = self.payload()
        payload["terminal"]["buckets"].pop()
        with temporary_config(payload) as path, self.assertRaisesRegex(FedPathConfigError, "15-bucket"):
            load_fed_path_config(path)
        payload = self.payload()
        payload["max_spread"] = float("nan")
        with temporary_config(payload) as path, self.assertRaisesRegex(FedPathConfigError, "finite"):
            load_fed_path_config(path)

    def test_rejects_boolean_or_old_schema_version(self) -> None:
        for value in (True, 1):
            payload = self.payload()
            payload["schema_version"] = value
            with self.subTest(value=value), temporary_config(payload) as path, self.assertRaisesRegex(FedPathConfigError, "schema_version"):
                load_fed_path_config(path)


if __name__ == "__main__":
    unittest.main()
