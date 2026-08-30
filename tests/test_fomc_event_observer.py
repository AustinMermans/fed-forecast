from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from fed_forecast.client import HttpResponse
from fed_forecast.fed_path_client import FedPathClient
from fed_forecast.fomc_event_collection import EventCollectionError
from fed_forecast.fomc_event_observer import FomcEventObserver, load_observation_topology


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/fed_path_events.json"


def surface() -> tuple[dict[str, object], Path]:
    fixture = json.loads(FIXTURE.read_text())
    template_slug = next(slug for slug in fixture["events"] if slug != "what-will-the-fed-rate-be-at-the-end-of-2026")
    template = fixture["events"][template_slug]
    settings = json.loads((ROOT / "config/markets.json").read_text())
    events: dict[str, object] = {}
    for meeting_index, meeting in enumerate(settings["meetings"]):
        event = copy.deepcopy(template)
        event["slug"] = meeting["event_slug"]
        for market_index, market in enumerate(event["markets"]):
            tokens = json.loads(market["clobTokenIds"])
            market["clobTokenIds"] = json.dumps([f"{token}-{meeting_index}-{market_index}" for token in tokens])
        events[meeting["event_slug"]] = event
    terminal = copy.deepcopy(fixture["events"][settings["terminal_event_slug"]])
    for market_index, market in enumerate(terminal["markets"]):
        tokens = json.loads(market["clobTokenIds"])
        market["clobTokenIds"] = json.dumps([f"{token}-terminal-{market_index}" for token in tokens])
    events[settings["terminal_event_slug"]] = terminal
    return {"events": events}, ROOT / "config/markets.json"


class Transport:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def request(self, method: str, url: str, *, body: bytes | None, headers: Mapping[str, str], timeout: float) -> HttpResponse:
        if "/events/slug/" in url:
            slug = url.rsplit("/", 1)[-1]
            return HttpResponse(200, json.dumps(self.payload["events"][slug]).encode(), {})
        if method == "POST" and url.endswith("/midpoints"):
            assert body is not None
            return HttpResponse(200, json.dumps({row["token_id"]: "0.40" for row in json.loads(body)}).encode(), {})
        if "/book?" in url:
            return HttpResponse(200, b'{"bids":[{"price":"0.35"}],"asks":[{"price":"0.45"}]}', {})
        raise AssertionError(url)


def close_event(event: dict[str, object], *, resolved: bool) -> None:
    event["closed"] = True
    event["active"] = False
    markets = event["markets"]
    assert isinstance(markets, list)
    for index, market in enumerate(markets):
        assert isinstance(market, dict)
        market["closed"] = True
        market["active"] = False
        market["acceptingOrders"] = False
        if resolved:
            outcomes = json.loads(market["outcomes"])
            yes = outcomes.index("Yes")
            prices = [0.0, 0.0]
            prices[yes] = 1.0 if index == 0 else 0.0
            prices[1 - yes] = 0.0 if index == 0 else 1.0
            market["outcomePrices"] = json.dumps(prices)


class EventObserverTests(unittest.TestCase):
    def collect(self, payload: dict[str, object]) -> dict[str, object]:
        _, markets_path = surface()
        topology = load_observation_topology(markets_path)
        now = lambda: datetime(2026, 9, 16, 18, 5, tzinfo=timezone.utc)
        client = FedPathClient(Transport(payload), now=now)
        return FomcEventObserver(client, now=now).collect(topology, markets_config_path=markets_path)

    def test_all_active_surface_is_complete_and_contains_no_forecast_product(self) -> None:
        payload, _ = surface()
        result = self.collect(payload)
        self.assertEqual(result["surface"]["coordinate_count"], 35)
        self.assertTrue(result["surface"]["all_coordinates_complete"])
        self.assertEqual({row["market_status"] for row in result["coordinates"]}, {"active"})
        self.assertEqual({row["source"] for row in result["coordinates"]}, {"clob_midpoint"})
        self.assertNotIn("meeting_distributions", result)
        self.assertNotIn("conditional_tree", result)

    def test_closed_pending_and_resolved_surfaces_remain_complete_and_explicit(self) -> None:
        for resolved, expected, source in (
            (False, "closed_pending_resolution", "gamma_pending_resolution"),
            (True, "resolved", "gamma_resolution"),
        ):
            with self.subTest(resolved=resolved):
                payload, _ = surface()
                first = next(iter(payload["events"].values()))
                assert isinstance(first, dict)
                close_event(first, resolved=resolved)
                result = self.collect(payload)
                rows = [row for row in result["coordinates"] if row["event_slug"] == first["slug"]]
                self.assertEqual({row["market_status"] for row in rows}, {expected})
                self.assertEqual({row["source"] for row in rows}, {source})
                self.assertTrue(all(row["best_bid"] is None and row["exchange_quote_timestamp"] is None for row in rows))

    def test_mixed_lifecycle_and_invalid_resolved_winner_fail_closed(self) -> None:
        payload, _ = surface()
        first = next(iter(payload["events"].values()))
        assert isinstance(first, dict)
        first["markets"][0]["closed"] = True
        with self.assertRaisesRegex(EventCollectionError, "mixed"):
            self.collect(payload)

        payload, _ = surface()
        first = next(iter(payload["events"].values()))
        assert isinstance(first, dict)
        close_event(first, resolved=True)
        second = first["markets"][1]
        outcomes = json.loads(second["outcomes"])
        yes = outcomes.index("Yes")
        prices = [0.0, 0.0]; prices[yes] = 1.0; prices[1 - yes] = 0.0
        second["outcomePrices"] = json.dumps(prices)
        with self.assertRaisesRegex(EventCollectionError, "winner"):
            self.collect(payload)

    def test_closed_market_with_contradictory_order_taking_state_fails_closed(self) -> None:
        payload, _ = surface()
        first = next(iter(payload["events"].values()))
        assert isinstance(first, dict)
        close_event(first, resolved=False)
        first["markets"][0]["acceptingOrders"] = True
        with self.assertRaisesRegex(EventCollectionError, "contradictory"):
            self.collect(payload)
