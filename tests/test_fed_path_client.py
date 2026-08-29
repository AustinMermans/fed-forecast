import copy
import json
import tempfile
import unittest
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from fed_forecast.client import ApiError, HttpResponse
from fed_forecast.fed_path_client import (
    FedPathClient,
    FedPathFetchError,
    SnapshotReplayError,
    load_fed_path_snapshot,
)
from fed_forecast.fed_path_config import load_fed_path_config


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/fed_path_events.json"


@dataclass
class Call:
    method: str
    url: str
    body: bytes | None
    headers: Mapping[str, str]
    timeout: float


class FedTransport:
    def __init__(
        self,
        payload: dict[str, object] | None = None,
        *,
        midpoint: str = "0.40",
        book: object | None = None,
        fail_midpoint_post: int | None = None,
        raw_midpoint_body: bytes | None = None,
        fail_books: bool = False,
    ) -> None:
        self.payload = copy.deepcopy(payload or json.loads(FIXTURE.read_text()))
        self.midpoint, self.book = midpoint, book
        self.fail_midpoint_post = fail_midpoint_post
        self.raw_midpoint_body = raw_midpoint_body
        self.fail_books = fail_books
        self.calls: list[Call] = []

    def request(self, method: str, url: str, *, body: bytes | None, headers: Mapping[str, str], timeout: float) -> HttpResponse:
        self.calls.append(Call(method, url, body, headers, timeout))
        if "/events/slug/" in url:
            slug = url.rsplit("/", 1)[-1]
            return HttpResponse(200, json.dumps(self.payload["events"][slug]).encode(), {})
        if method == "POST" and url.endswith("/midpoints"):
            assert body is not None
            midpoint_posts = sum(
                call.method == "POST" and call.url.endswith("/midpoints")
                for call in self.calls
            )
            if (
                self.fail_midpoint_post is not None
                and midpoint_posts >= self.fail_midpoint_post
            ):
                return HttpResponse(500, b'{"error":"later batch failed"}', {})
            if self.raw_midpoint_body is not None:
                return HttpResponse(200, self.raw_midpoint_body, {})
            return HttpResponse(200, json.dumps({item["token_id"]: self.midpoint for item in json.loads(body)}).encode(), {})
        if "/book?" in url:
            if self.fail_books:
                return HttpResponse(500, b'{"error":"book unavailable"}', {})
            data = self.book if self.book is not None else {"bids": [{"price": "0.35"}], "asks": [{"price": "0.45"}]}
            return HttpResponse(200, json.dumps(data).encode(), {})
        raise AssertionError(f"unexpected request {method} {url}")


class NoHttpTransport:
    def request(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("snapshot replay must not make HTTP requests")


class FedPathClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_fed_path_config(ROOT / "config/fed_path.json")

    def client(self, transport: FedTransport | None = None) -> tuple[FedPathClient, FedTransport]:
        value = transport or FedTransport()
        return FedPathClient(value, now=lambda: datetime(2026, 7, 24, 12, tzinfo=timezone.utc)), value

    def test_fixture_preserves_complete_current_gamma_shapes_and_raw_label_anomalies(self) -> None:
        payload = json.loads(FIXTURE.read_text())
        events = payload["events"]
        self.assertEqual(set(events), {
            *(meeting.event_slug for meeting in self.config.meetings),
            self.config.terminal_event_slug,
        })
        for event in events.values():
            self.assertTrue(event["enableOrderBook"])
            self.assertNotIn("acceptingOrders", event)
            self.assertIn("description", event)
            self.assertIn("createdAt", event)
            for market in event["markets"]:
                self.assertTrue(market["enableOrderBook"])
                self.assertTrue(market["acceptingOrders"])
                self.assertIn("conditionId", market)
                self.assertIn("updatedAt", market)
        terminal_labels = {
            market["groupItemTitle"]
            for market in events[self.config.terminal_event_slug]["markets"]
        }
        self.assertIn("1.25", terminal_labels)
        self.assertIn("≥ 4.5%", terminal_labels)

    def test_collects_exact_topology_prefers_clob_and_preserves_raw_responses(self) -> None:
        client, transport = self.client()
        snapshot = client.fetch_snapshot(self.config)
        self.assertEqual([call.url for call in transport.calls[:4]], [
            f"https://gamma-api.polymarket.com/events/slug/{meeting.event_slug}" for meeting in self.config.meetings
        ] + [f"https://gamma-api.polymarket.com/events/slug/{self.config.terminal_event_slug}"])
        posts = [call for call in transport.calls if call.method == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertLessEqual(len(json.loads(posts[0].body or b"[]")), 50)
        self.assertEqual(len(snapshot.meeting_prices), 15)
        self.assertEqual(len(snapshot.terminal_prices), 15)
        self.assertEqual({item.source for item in snapshot.selected_prices}, {"clob_midpoint"})
        self.assertEqual(len(snapshot.events), 4)
        self.assertEqual(len(snapshot.raw_responses), len(transport.calls))
        self.assertTrue(all(row["body"] is not None for row in snapshot.raw_responses))

    def test_gamma_fallback_records_quality_diagnostics_after_invalid_book(self) -> None:
        client, _ = self.client(FedTransport(book={"bids": [], "asks": []}))
        snapshot = client.fetch_snapshot(self.config)
        self.assertEqual({item.source for item in snapshot.selected_prices}, {"gamma"})
        codes = {item.code for item in snapshot.diagnostics}
        self.assertIn("missing_book", codes)
        self.assertIn("gamma_fallback_price", codes)

        for transport in (
            FedTransport(book=[]),
            FedTransport(fail_books=True),
        ):
            with self.subTest(transport=type(transport).__name__):
                fallback = self.client(transport)[0].fetch_snapshot(self.config)
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "snapshot.json"
                    path.write_text(
                        json.dumps(fallback.to_dict(), allow_nan=False),
                        encoding="utf-8",
                    )
                    replay = load_fed_path_snapshot(path, self.config)
                self.assertEqual(replay.meeting_prices, fallback.meeting_prices)

    def test_rejects_every_event_and_child_eligibility_mutation(self) -> None:
        for target, mutation in (
            ("event", {"active": False}), ("event", {"closed": True}), ("event", {"enableOrderBook": False}),
            ("child", {"active": False}), ("child", {"closed": True}), ("child", {"enableOrderBook": False}), ("child", {"acceptingOrders": False}),
        ):
            with self.subTest(target=target, mutation=mutation):
                transport = FedTransport()
                event = transport.payload["events"][self.config.meetings[0].event_slug]
                assert isinstance(event, dict)
                if target == "event":
                    event.update(mutation)
                else:
                    event["markets"][0].update(mutation)
                client, _ = self.client(transport)
                with self.assertRaises(FedPathFetchError) as caught:
                    client.fetch_snapshot(self.config)
                self.assertTrue(caught.exception.partial_snapshot.raw_responses)
                self.assertIn("ineligible", str(caught.exception).lower())

    def test_rejects_duplicate_tokens_and_nonexact_event_topologies(self) -> None:
        for mutation in ("token", "label", "terminal"):
            with self.subTest(mutation=mutation):
                transport = FedTransport()
                july = transport.payload["events"][self.config.meetings[0].event_slug]
                assert isinstance(july, dict)
                if mutation == "token":
                    july["markets"][1]["clobTokenIds"] = july["markets"][0]["clobTokenIds"]
                elif mutation == "label":
                    july["markets"][1]["groupItemTitle"] = "50+ bps decrease"
                else:
                    terminal = transport.payload["events"][self.config.terminal_event_slug]
                    assert isinstance(terminal, dict)
                    terminal["markets"].pop()
                with self.assertRaises(FedPathFetchError):
                    self.client(transport)[0].fetch_snapshot(self.config)

    def test_replay_is_strict_identity_checked_and_performs_zero_http(self) -> None:
        snapshot = self.client()[0].fetch_snapshot(self.config)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            path.write_text(json.dumps(snapshot.to_dict(), allow_nan=False), encoding="utf-8")
            replay = load_fed_path_snapshot(path, self.config, transport=NoHttpTransport())
            self.assertEqual(replay.meeting_prices, snapshot.meeting_prices)
            self.assertEqual(replay.terminal_prices, snapshot.terminal_prices)
            payload = snapshot.to_dict()
            payload["unknown"] = True
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(SnapshotReplayError):
                load_fed_path_snapshot(path, self.config)
            payload = snapshot.to_dict()
            payload["config_sha256"] = "0" * 64
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(SnapshotReplayError, "identity"):
                load_fed_path_snapshot(path, self.config)
            path.write_text('{"schema_version":NaN}', encoding="utf-8")
            with self.assertRaises(SnapshotReplayError):
                load_fed_path_snapshot(path, self.config)
            valid = json.dumps(snapshot.to_dict(), allow_nan=False)
            path.write_text('{"schema_version":1,' + valid[1:], encoding="utf-8")
            with self.assertRaisesRegex(SnapshotReplayError, "duplicate JSON key"):
                load_fed_path_snapshot(path, self.config)

    def test_gamma_event_slug_books_and_binary_children_are_exact(self) -> None:
        for target, mutation in (
            ("event", {"slug": "wrong-slug"}),
            ("child", {"outcomes": '["Yes", "No", "Maybe"]', "clobTokenIds": '["a", "b", "c"]', "outcomePrices": '[".2", ".7", ".1"]'}),
        ):
            with self.subTest(target=target):
                transport = FedTransport()
                event = transport.payload["events"][self.config.meetings[0].event_slug]
                assert isinstance(event, dict)
                if target == "event":
                    event.update(mutation)
                else:
                    event["markets"][0].update(mutation)
                with self.assertRaises(FedPathFetchError):
                    self.client(transport)[0].fetch_snapshot(self.config)

        transport = FedTransport()
        event = transport.payload["events"][self.config.meetings[0].event_slug]
        assert isinstance(event, dict)
        market = event["markets"][0]
        title = market["groupItemTitle"]
        outcomes = json.loads(market["outcomes"])
        tokens = json.loads(market["clobTokenIds"])
        original_yes_token = tokens[outcomes.index("Yes")]
        for field in ("outcomes", "clobTokenIds", "outcomePrices"):
            market[field] = json.dumps(list(reversed(json.loads(market[field]))))
        snapshot = self.client(transport)[0].fetch_snapshot(self.config)
        observation = next(
            item
            for item in snapshot.selected_prices
            if item.source_id == self.config.meetings[0].event_slug and item.title == title
        )
        self.assertEqual(observation.yes_token, original_yes_token)

    def test_terminal_canonicalizes_real_label_anomalies_and_rejects_unknown_bucket(self) -> None:
        transport = FedTransport()
        terminal = transport.payload["events"][self.config.terminal_event_slug]
        assert isinstance(terminal, dict)
        terminal["markets"][1]["groupItemTitle"] = "1.25"
        snapshot = self.client(transport)[0].fetch_snapshot(self.config)
        self.assertIn("1.25%", snapshot.terminal_prices)
        terminal["markets"][1]["groupItemTitle"] = "5.0%"
        with self.assertRaises(FedPathFetchError):
            self.client(transport)[0].fetch_snapshot(self.config)

    def test_success_response_overflow_and_nonobject_event_are_diagnostic_failures(self) -> None:
        transport = FedTransport()
        first = self.config.meetings[0].event_slug
        transport.payload["events"][first] = []
        with self.assertRaises(FedPathFetchError) as caught:
            self.client(transport)[0].fetch_snapshot(self.config)
        self.assertIn("source_fetch_failed", {item.code for item in caught.exception.partial_snapshot.diagnostics})

    def test_snapshot_is_frozen_deeply_immutable_and_to_dict_is_detached(self) -> None:
        snapshot = self.client()[0].fetch_snapshot(self.config)
        with self.assertRaises((AttributeError, TypeError)):
            snapshot.events["other"] = {}  # type: ignore[index]
        payload = snapshot.to_dict()
        payload["events"].clear()
        payload["selected_prices"][0]["price"] = 0.0
        self.assertEqual(len(snapshot.events), 4)
        self.assertEqual(snapshot.selected_prices[0].price, .4)

    def test_more_than_fifty_midpoints_are_batched_and_second_failure_is_preserved(self) -> None:
        transport = FedTransport()
        client, _ = self.client(transport)
        responses: list[dict[str, object]] = []
        prices = client.fetch_midpoints([f"extra-{index}" for index in range(51)], response_recorder=responses.append)
        self.assertEqual(len(prices), 51)
        self.assertEqual([len(json.loads(call.body or b"[]")) for call in transport.calls if call.method == "POST"], [50, 1])
        self.assertEqual(len(responses), 2)

    def test_replay_rejects_each_raw_evidence_tamper(self) -> None:
        snapshot = self.client()[0].fetch_snapshot(self.config)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            def reject(mutator):
                payload = snapshot.to_dict()
                mutator(payload)
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(SnapshotReplayError):
                    load_fed_path_snapshot(path, self.config)
            reject(lambda value: value.__setitem__("raw_responses", []))
            reject(lambda value: value["raw_responses"][0].__setitem__("body", {"tampered": True}))
            reject(lambda value: value["raw_responses"][0].__setitem__("extra", True))
            reject(lambda value: value["raw_responses"][0].pop("body"))
            reject(lambda value: value["raw_responses"][0].__setitem__("retrieved_at", "2026-7-24T12:00:00Z"))
            reject(
                lambda value: value["raw_responses"].append(
                    {
                        **value["raw_responses"][0],
                        "url": "https://example.invalid/unexpected",
                    }
                )
            )
            reject(lambda value: value["raw_responses"][5].__setitem__("body", {}))
            reject(lambda value: value["selected_prices"][0].__setitem__("best_bid", .1))
            reject(lambda value: value["selected_prices"][0].__setitem__("quality", "degraded"))
            def gamma_for_valid_clob(value):
                item = value["selected_prices"][0]
                item.update({"source": "gamma", "quality": "degraded", "price": .05})
                value["meeting_prices"][0]["raw_probability"] = .05
            reject(gamma_for_valid_clob)

    def test_live_overflow_responses_produce_serializable_partial_snapshots(self) -> None:
        class OverflowTransport(FedTransport):
            def request(self, method, url, **kwargs):
                if "/events/slug/" in url or method == "POST":
                    return HttpResponse(200, b'{"value":1e999}', {})
                return super().request(method, url, **kwargs)
        for transport in (OverflowTransport(),):
            with self.assertRaises(FedPathFetchError) as caught:
                self.client(transport)[0].fetch_snapshot(self.config)
            json.dumps(caught.exception.partial_snapshot.to_dict(), allow_nan=False)

        for raw_body, diagnostic in (
            (b'{"one-token":1e999}', "midpoint_fetch_failed"),
            (b"[]", "midpoint_schema_failed"),
            (b'{"one-token":123}', "midpoint_schema_failed"),
        ):
            with self.subTest(raw_body=raw_body):
                midpoint_overflow = FedTransport(
                    raw_midpoint_body=raw_body,
                    book={"bids": [], "asks": []},
                )
                fallback = self.client(midpoint_overflow)[0].fetch_snapshot(
                    self.config
                )
                self.assertEqual(
                    {item.source for item in fallback.selected_prices},
                    {"gamma"},
                )
                self.assertIn(
                    diagnostic,
                    {item.code for item in fallback.diagnostics},
                )
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "snapshot.json"
                    path.write_text(
                        json.dumps(fallback.to_dict(), allow_nan=False),
                        encoding="utf-8",
                    )
                    replay = load_fed_path_snapshot(path, self.config)
                self.assertEqual(replay.meeting_prices, fallback.meeting_prices)

        failing = FedTransport(fail_midpoint_post=2)
        failing_client, _ = self.client(failing)
        preserved: list[dict[str, object]] = []
        with self.assertRaises(ApiError):
            failing_client.fetch_midpoints(
                [f"extra-{index}" for index in range(51)],
                response_recorder=preserved.append,
            )
        self.assertEqual(len(preserved), 1)
        self.assertEqual(len(preserved[0]["body"]), 50)

    def test_late_price_failure_preserves_completed_selected_observations(self) -> None:
        transport = FedTransport(book={"bids": [], "asks": []})
        september = transport.payload["events"][self.config.meetings[1].event_slug]
        sixth = next(
            market
            for market in september["markets"]
            if market["groupItemTitle"] == "50+ bps decrease"
        )
        sixth["outcomePrices"] = '["2.0", "0.0"]'
        with self.assertRaises(FedPathFetchError) as caught:
            self.client(transport)[0].fetch_snapshot(self.config)
        partial = caught.exception.partial_snapshot
        self.assertEqual(len(partial.selected_prices), 5)
        self.assertTrue(all(item.source == "gamma" for item in partial.selected_prices))
        json.dumps(partial.to_dict(), allow_nan=False)


if __name__ == "__main__":
    unittest.main()
