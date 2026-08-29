import hashlib
import json
import shutil
import tempfile
import unittest
from contextlib import contextmanager
from datetime import date
from pathlib import Path

from fed_forecast.fed_path_config import FedPathConfigError, load_fed_path_config


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config/fed_path.json"
IMAGE_PATH = PROJECT_ROOT / "docs/source/wirp-fed-funds-2026-07-24.jpg"


@contextmanager
def temporary_project(payload: dict[str, object], image: Path | None = IMAGE_PATH):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        path = root / "config/fed_path.json"
        path.parent.mkdir()
        path.write_text(json.dumps(payload), encoding="utf-8")
        if image is not None:
            destination = root / "docs/source/wirp-fed-funds-2026-07-24.jpg"
            destination.parent.mkdir(parents=True)
            shutil.copyfile(image, destination)
        yield path, root


class FedPathConfigTests(unittest.TestCase):
    def payload(self) -> dict[str, object]:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

    def test_default_values_are_exact_and_reference_is_pinned(self) -> None:
        config = load_fed_path_config(CONFIG_PATH)
        self.assertEqual(config.schema_version, 1)
        self.assertEqual(config.pricing_date, date(2026, 7, 24))
        self.assertEqual(config.source_image, "docs/source/wirp-fed-funds-2026-07-24.jpg")
        self.assertEqual(config.source_sha256, "4c298af9537ae26c22917d6f3f48eb9fd5ed2654be9ce17499b9b218d2397d34")
        self.assertEqual(hashlib.sha256(IMAGE_PATH.read_bytes()).hexdigest(), config.source_sha256)
        self.assertEqual((config.target_upper_bound, config.effective_rate_baseline), (3.75, 3.628))
        self.assertEqual((config.standard_move_bp, config.max_spread), (25.0, 0.1))
        self.assertEqual(
            [(meeting.date, meeting.event_slug) for meeting in config.meetings],
            [
                (date(2026, 7, 29), "fed-decision-in-july-181"),
                (date(2026, 9, 16), "fed-decision-in-september-762"),
                (date(2026, 10, 28), "fed-decision-in-october-20260617190323537"),
            ],
        )
        self.assertEqual(
            [(outcome.label, outcome.representative_bp) for outcome in config.meetings[0].outcomes],
            [("50+ bps decrease", -50.0), ("25 bps decrease", -25.0), ("No change", 0.0), ("25 bps increase", 25.0), ("50+ bps increase", 50.0)],
        )
        self.assertEqual(config.terminal_event_slug, "what-will-the-fed-rate-be-at-the-end-of-2026")
        self.assertEqual(len(config.terminal_buckets), 15)
        self.assertEqual([(bucket.kind, bucket.representative_rate) for bucket in config.terminal_buckets], [("lte", 1.0), *( ("exact", rate) for rate in (1.25, 1.5, 1.75, 2.0, 2.25, 2.5, 2.75, 3.0, 3.25, 3.5, 3.75, 4.0, 4.25)), ("gte", 4.5)])

    def test_wirp_transcription_is_exact(self) -> None:
        config = load_fed_path_config(CONFIG_PATH)
        self.assertEqual(
            [(row.date, row.incremental_moves, row.cumulative_moves, row.implied_rate_delta, row.implied_rate) for row in config.wirp_rows],
            [
                (date(2026, 7, 29), .337, .337, .084, 3.713), (date(2026, 9, 16), .710, 1.047, .262, 3.890),
                (date(2026, 10, 28), .270, 1.317, .329, 3.958), (date(2026, 12, 9), .385, 1.701, .425, 4.054),
                (date(2027, 1, 27), .185, 1.887, .472, 4.100), (date(2027, 3, 17), .243, 2.130, .532, 4.161),
                (date(2027, 4, 28), .107, 2.237, .559, 4.188), (date(2027, 6, 9), -.002, 2.234, .559, 4.187),
                (date(2027, 7, 28), -.058, 2.177, .544, 4.173), (date(2027, 9, 15), -.125, 2.052, .513, 4.141),
                (date(2027, 10, 27), -.085, 1.967, .492, 4.120),
            ],
        )

    def test_rejects_unknown_keys_and_unsafe_source_paths(self) -> None:
        payload = self.payload()
        payload["unknown"] = True
        with temporary_project(payload) as (path, root):
            with self.assertRaisesRegex(FedPathConfigError, "unknown"):
                load_fed_path_config(path, root)
        for unsafe_path in ("/tmp/reference.jpg", "../docs/source/reference.jpg", "docs/../source/reference.jpg"):
            payload = self.payload()
            payload["source_image"] = unsafe_path
            with temporary_project(payload) as (path, root):
                with self.assertRaisesRegex(FedPathConfigError, "relative path without traversal"):
                    load_fed_path_config(path, root)

    def test_rejects_missing_or_tampered_reference_bytes(self) -> None:
        payload = self.payload()
        with temporary_project(payload, None) as (path, root):
            with self.assertRaisesRegex(FedPathConfigError, "source image"):
                load_fed_path_config(path, root)
        with temporary_project(payload) as (path, root):
            image = root / payload["source_image"]
            image.write_bytes(image.read_bytes() + b"tampered")
            with self.assertRaisesRegex(FedPathConfigError, "SHA-256"):
                load_fed_path_config(path, root)

    def test_rejects_substitution_of_another_safe_path_with_identical_bytes(self) -> None:
        payload = self.payload()
        payload["source_image"] = "docs/source/other-reference.jpg"
        with temporary_project(payload) as (path, root):
            source = root / "docs/source/wirp-fed-funds-2026-07-24.jpg"
            replacement = root / payload["source_image"]
            shutil.copyfile(source, replacement)
            with self.assertRaisesRegex(FedPathConfigError, "source_image"):
                load_fed_path_config(path, root)

    def test_rejects_duplicates_and_nonchronological_meetings(self) -> None:
        for field, value in (("event_slug", "fed-decision-in-july-181"), ("date", "2026-07-29")):
            payload = self.payload()
            payload["meetings"][1][field] = value
            with temporary_project(payload) as (path, root):
                with self.assertRaisesRegex(FedPathConfigError, "unique"):
                    load_fed_path_config(path, root)
        payload = self.payload()
        payload["meetings"][1]["date"] = "2026-07-01"
        with temporary_project(payload) as (path, root):
            with self.assertRaisesRegex(FedPathConfigError, "chronological"):
                load_fed_path_config(path, root)
        payload = self.payload()
        payload["meetings"][0]["outcomes"][1]["label"] = "No change"
        with temporary_project(payload) as (path, root):
            with self.assertRaisesRegex(FedPathConfigError, "labels must be unique"):
                load_fed_path_config(path, root)

    def test_rejects_altered_but_unique_approved_meeting_identity(self) -> None:
        for key, value in (("date", "2026-09-17"), ("event_slug", "other-unique-event")):
            payload = self.payload()
            payload["meetings"][1][key] = value
            with self.subTest(key=key), temporary_project(payload) as (path, root):
                with self.assertRaisesRegex(FedPathConfigError, "approved meeting identities"):
                    load_fed_path_config(path, root)

    def test_rejects_wrong_frozen_topologies_and_baselines(self) -> None:
        for key, value in (("target_upper_bound", 3.5), ("effective_rate_baseline", 3.63), ("standard_move_bp", 50.0), ("max_spread", 0.2)):
            payload = self.payload()
            payload[key] = value
            with temporary_project(payload) as (path, root):
                with self.assertRaises(FedPathConfigError):
                    load_fed_path_config(path, root)
        payload = self.payload()
        payload["meetings"][0]["outcomes"][0]["representative_bp"] = -75
        with temporary_project(payload) as (path, root):
            with self.assertRaisesRegex(FedPathConfigError, "five-outcome topology"):
                load_fed_path_config(path, root)
        payload = self.payload()
        payload["terminal"]["buckets"].pop()
        with temporary_project(payload) as (path, root):
            with self.assertRaisesRegex(FedPathConfigError, "15-bucket topology"):
                load_fed_path_config(path, root)

    def test_rejects_non_finite_numbers_and_altered_wirp_rows(self) -> None:
        payload = self.payload()
        payload["max_spread"] = float("nan")
        with temporary_project(payload) as (path, root):
            with self.assertRaisesRegex(FedPathConfigError, "finite"):
                load_fed_path_config(path, root)
        payload = self.payload()
        payload["wirp_rows"][0]["implied_rate"] = 3.714
        with temporary_project(payload) as (path, root):
            with self.assertRaisesRegex(FedPathConfigError, "WIRP"):
                load_fed_path_config(path, root)

    def test_rejects_boolean_schema_version(self) -> None:
        payload = self.payload()
        payload["schema_version"] = True
        with temporary_project(payload) as (path, root):
            with self.assertRaisesRegex(FedPathConfigError, "schema_version"):
                load_fed_path_config(path, root)


if __name__ == "__main__":
    unittest.main()
