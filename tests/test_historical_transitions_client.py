import copy
import json
import tempfile
import unittest
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Mapping

from fed_forecast.client import HttpResponse
from fed_forecast.historical_transitions_client import (
    HistoricalConfigError,
    HistoricalIntegrityError,
    HistoricalSnapshotError,
    HistoricalTransitionsClient,
    load_historical_config,
    load_historical_snapshot,
    reconstruct_observations,
)


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config/historical_transitions.json"
JUNE = date(2026, 6, 17)
JULY = date(2026, 7, 29)


def _market(event: str, index: int, label: str, winner: bool) -> dict[str, object]:
    return {
        "id": f"{event}-m{index}",
        "question": f"Will the Fed announce a {label} at this meeting?",
        "groupItemTitle": label,
        "outcomes": json.dumps(["Yes", "No"]),
        "clobTokenIds": json.dumps([f"{event}-yes-{index}", f"{event}-no-{index}"]),
        "outcomePrices": json.dumps(["1" if winner else "0", "0" if winner else "1"]),
    }


def _event(name: str, meeting: date, winner_index: int = 2, *, neg_risk: bool = True) -> dict[str, object]:
    labels = [
        "50+ bps decrease", "25 bps decrease", "No change",
        "25 bps increase", "50+ bps increase",
    ]
    return {
        "id": name,
        "slug": f"fed-decision-{name}",
        "endDate": meeting.isoformat() + "T00:00:00Z",
        "negRisk": neg_risk,
        "markets": [_market(name, index, label, index == winner_index) for index, label in enumerate(labels)],
    }


def _series() -> dict[str, object]:
    return {"id": "35", "events": [_event("june", JUNE), _event("july", JULY)]}


@dataclass
class Call:
    method: str
    url: str
    body: bytes | None


class HistoryTransport:
    def __init__(self, series: dict[str, object] | None = None) -> None:
        self.series = copy.deepcopy(series or _series())
        self.calls: list[Call] = []
        self.pre = {
            **{f"june-yes-{i}": value for i, value in enumerate((.05, .10, .70, .10, .05))},
            **{f"july-yes-{i}": value for i, value in enumerate((.10, .20, .50, .15, .05))},
        }
        self.post = {
            **{f"june-yes-{i}": value for i, value in enumerate((.04, .08, .72, .11, .05))},
            **{f"july-yes-{i}": value for i, value in enumerate((.05, .10, .60, .20, .05))},
        }

    def request(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None,
        headers: Mapping[str, str],
        timeout: float,
    ) -> HttpResponse:
        del headers, timeout
        self.calls.append(Call(method, url, body))
        if method == "GET" and "/events?series_id=35" in url:
            assert url == "https://gamma-api.polymarket.com/events?series_id=35&closed=true&limit=200&order=endDate&ascending=true"
            return HttpResponse(200, json.dumps(self.series["events"]).encode(), {})
        if method == "POST" and url.endswith("/batch-prices-history"):
            assert body is not None
            payload = json.loads(body)
            self.assert_batch(payload)
            pre = payload["start_ts"] + 9 * 60
            post = payload["end_ts"] - 5 * 60
            history = {
                token: [{"t": pre, "p": self.pre[token]}, {"t": post, "p": self.post[token]}]
                for token in payload["markets"]
            }
            return HttpResponse(200, json.dumps({"history": history}).encode(), {})
        raise AssertionError(f"unexpected request {method} {url}")

    @staticmethod
    def assert_batch(payload: dict[str, object]) -> None:
        assert set(payload) == {"markets", "start_ts", "end_ts", "fidelity"}
        assert isinstance(payload["markets"], list)
        assert len(payload["markets"]) <= 20
        assert payload["fidelity"] == 1


class NoHttpTransport:
    def request(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("replay must not use HTTP")


class HistoricalTransitionsClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_historical_config(CONFIG_PATH)

    def fetch(self, transport: HistoryTransport | None = None):
        value = transport or HistoryTransport()
        client = HistoricalTransitionsClient(
            value,
            now=lambda: datetime(2026, 8, 28, 20, 0, tzinfo=timezone.utc),
        )
        return client.fetch_snapshot(self.config), value

    def test_loads_pinned_config_and_validates_official_upper_bound_moves(self) -> None:
        self.assertEqual(self.config.series_id, 35)
        self.assertEqual(self.config.category_order, ("down", "unchanged", "up"))
        september = next(item for item in self.config.official_decisions if item.meeting_date == date(2024, 9, 18))
        self.assertEqual(september.change_bp, -50.0)
        self.assertEqual(september.category, "down")
        self.assertIn("20240918", september.source_url)

        payload = json.loads(CONFIG_PATH.read_text())
        payload["official_decisions"][0]["after"] = [0.25, 0.75]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(HistoricalConfigError, "Range-width|range-width"):
                load_historical_config(path)
            path.write_text('{"schema_version":1,"nested":[1e400]}', encoding="utf-8")
            with self.assertRaisesRegex(HistoricalConfigError, "finite"):
                load_historical_config(path)

    def test_fetches_series_and_bounded_batches_then_reconstructs_exact_surfaces(self) -> None:
        snapshot, transport = self.fetch()
        posts = [call for call in transport.calls if call.method == "POST"]
        self.assertEqual(len(posts), 1)
        request = json.loads(posts[0].body or b"{}")
        self.assertEqual(len(request["markets"]), 10)
        self.assertLessEqual(len(request["markets"]), 20)
        self.assertEqual(snapshot.topology_blind_sha256, snapshot.to_dict()["topology_blind_sha256"])
        self.assertEqual(len(snapshot.topology_ledger), 10)

        observations, topology, exclusions = reconstruct_observations(snapshot, self.config)
        self.assertEqual(len(observations), 1)
        observation = observations[0]
        self.assertEqual((observation.current_meeting_date, observation.next_meeting_date), (JUNE, JULY))
        self.assertEqual(observation.realized_category, "unchanged")
        self.assertEqual(observation.realized_action_bp, 0.0)
        diagnostic = next(row for row in topology if row.get("record_type") == "surface_diagnostic")
        for actual, expected in zip(diagnostic["current_pre_raw"], (.15, .70, .15)):
            self.assertAlmostEqual(actual, expected)
        for actual, expected in zip(diagnostic["next_pre_raw"], (.30, .50, .20)):
            self.assertAlmostEqual(actual, expected)
        for actual, expected in zip(diagnostic["next_post_raw"], (.15, .60, .25)):
            self.assertAlmostEqual(actual, expected)
        self.assertAlmostEqual(sum(observation.current_pre), 1.0)
        for actual, expected in zip(
            observation.current_candidate_actions_bp,
            (-33.333333333333336, 0.0, 33.333333333333336),
        ):
            self.assertAlmostEqual(actual, expected)
        self.assertEqual(len(topology), 11)
        self.assertTrue(any(row["reason"] == "missing_consecutive_primary_topology" for row in exclusions))

    def test_child_floor_defines_candidate_action_for_raw_zero_category(self) -> None:
        transport = HistoryTransport()
        transport.pre["june-yes-0"] = 0.0
        transport.pre["june-yes-1"] = 0.0
        transport.pre["june-yes-2"] = 0.8
        transport.pre["june-yes-3"] = 0.15
        transport.pre["june-yes-4"] = 0.05
        snapshot, _ = self.fetch(transport)
        observations, topology, _ = reconstruct_observations(snapshot, self.config)
        observation = observations[0]
        diagnostic = next(row for row in topology if row.get("record_type") == "surface_diagnostic")
        self.assertEqual(diagnostic["child_action_fallback_categories"], ["down"])
        self.assertAlmostEqual(observation.current_candidate_actions_bp[0], -37.5)
        self.assertEqual(observation.current_pre[0], 0.0)
        self.assertGreater(diagnostic["current_pre_smoothed"][0], 0.0)

    def test_post_cutoff_is_strict_and_never_carries_pre_quote(self) -> None:
        snapshot, _ = self.fetch()
        payload = snapshot.to_dict()
        window = payload["history_windows"][0]
        token = "july-yes-0"
        post_lower = window["end_ts"] - 10 * 60
        window["history"][token][1]["t"] = post_lower
        post_evidence = next(row for row in payload["raw_responses"] if row["method"] == "POST")
        post_evidence["body"]["history"][token][1]["t"] = post_lower
        encoded = json.dumps(post_evidence["body"]).encode()
        post_evidence["body_hex"] = encoded.hex()
        post_evidence["body_sha256"] = self.hash_bytes(encoded)
        rebuilt = self.snapshot_from(payload)
        observations, _, exclusions = reconstruct_observations(rebuilt, self.config)
        self.assertFalse(observations)
        self.assertTrue(any("no synchronized quote" in row["reason"] for row in exclusions))

    def test_official_resolution_mismatch_is_integrity_failure_not_selection(self) -> None:
        series = _series()
        june = series["events"][0]
        for market in june["markets"]:
            market["outcomePrices"] = json.dumps(["0", "1"])
        june["markets"][3]["outcomePrices"] = json.dumps(["1", "0"])
        snapshot, _ = self.fetch(HistoryTransport(series))
        with self.assertRaises(HistoricalIntegrityError):
            reconstruct_observations(snapshot, self.config)

    def test_blind_topology_hash_does_not_encode_terminal_winner(self) -> None:
        unchanged, _ = self.fetch(HistoryTransport(_series()))
        alternate_series = _series()
        alternate_series["events"][0] = _event("june", JUNE, winner_index=3)
        alternate, _ = self.fetch(HistoryTransport(alternate_series))
        self.assertEqual(unchanged.topology_blind_sha256, alternate.topology_blind_sha256)

    def test_replay_is_strict_network_free_and_hash_checked(self) -> None:
        snapshot, _ = self.fetch()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            path.write_text(json.dumps(snapshot.to_dict(), allow_nan=False), encoding="utf-8")
            replay = load_historical_snapshot(path, self.config)
            self.assertEqual(replay.to_dict(), snapshot.to_dict())
            payload = snapshot.to_dict()
            payload["topology_blind_sha256"] = "0" * 64
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(HistoricalSnapshotError, "topology"):
                load_historical_snapshot(path, self.config)
            path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
            with self.assertRaisesRegex(HistoricalSnapshotError, "duplicate"):
                load_historical_snapshot(path, self.config)
            path.write_text('{"schema_version":1,"nested":[1e400]}', encoding="utf-8")
            with self.assertRaisesRegex(HistoricalSnapshotError, "finite"):
                load_historical_snapshot(path, self.config)
        NoHttpTransport()  # The replay API exposes no transport parameter.

    def snapshot_from(self, payload: dict[str, object]):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            topology = [
                {key: value for key, value in row.items() if key != "yes_resolution_price"}
                for row in payload["topology_ledger"]
            ]
            payload["topology_blind_sha256"] = self.hash_value(topology)
            path.write_text(json.dumps(payload, allow_nan=False), encoding="utf-8")
            return load_historical_snapshot(path, self.config)

    @staticmethod
    def hash_value(value: object) -> str:
        import hashlib
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()

    @staticmethod
    def hash_bytes(value: bytes) -> str:
        import hashlib
        return hashlib.sha256(value).hexdigest()


if __name__ == "__main__":
    unittest.main()
