"""Collection-only Polymarket observer for complete FOMC event surfaces."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urlencode

from .client import ApiError, JsonResponse, PriceObservation, best_book_prices, valid_price
from .fed_path_client import FedPathClient
from .fed_path_config import load_fed_path_config
from .fed_path_models import FedPathConfig, MeetingConfig
from .fomc_event_collection import EventCollectionError, finite_number, sha256_file
from .parsing import MarketParseError, ParsedMarket, parse_market


@dataclass(frozen=True)
class ObservedCoordinate:
    event_slug: str
    coordinate_kind: str
    meeting_date: str | None
    label: str
    question: str
    yes_token: str
    raw_probability: float
    source: str
    quality: str
    market_status: str
    observed_at: str
    exchange_quote_timestamp: None
    exchange_quote_age_seconds: None
    exchange_timestamp_status: str
    liquidity: float | None
    best_bid: float | None
    best_ask: float | None
    spread: float | None
    diagnostic_codes: tuple[str, ...]


def _utc(now: Callable[[], datetime]) -> str:
    value = now()
    if value.tzinfo is None:
        raise EventCollectionError("observer clock must include an offset")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _strict_market_settings(path: Path) -> dict[str, object]:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate key: {key}")
            result[key] = value
        return result
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=lambda item: (_ for _ in ()).throw(ValueError(item)),
            object_pairs_hook=pairs,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise EventCollectionError(f"could not read market settings: {error}") from error
    if not isinstance(value, dict) or not isinstance(value.get("meetings"), list):
        raise EventCollectionError("market settings are invalid")
    return value


def load_observation_topology(path: Path) -> FedPathConfig:
    settings = _strict_market_settings(path)
    base = load_fed_path_config(path.parent / "fed_path.json")
    outcomes = base.meetings[0].outcomes
    meetings: list[MeetingConfig] = []
    for raw in settings["meetings"]:  # type: ignore[index]
        if not isinstance(raw, dict) or set(raw) != {"date", "event_slug"}:
            raise EventCollectionError("market meeting settings are invalid")
        try:
            meeting_date = date.fromisoformat(str(raw["date"]))
        except ValueError as error:
            raise EventCollectionError("market meeting date is invalid") from error
        slug = raw["event_slug"]
        if not isinstance(slug, str) or not slug:
            raise EventCollectionError("market event_slug is invalid")
        meetings.append(MeetingConfig(meeting_date, slug, outcomes))
    max_spread = finite_number(settings.get("max_spread"), "max_spread", minimum=0.0, maximum=1.0)
    terminal_slug = settings.get("terminal_event_slug")
    if terminal_slug != base.terminal_event_slug:
        raise EventCollectionError("terminal event does not match reviewed topology")
    return FedPathConfig(
        2, base.target_upper_bound, base.effective_rate_baseline, base.standard_move_bp,
        max_spread, tuple(meetings), base.terminal_event_slug, base.terminal_buckets,
    )


def _observable_binary(raw: dict[str, object]) -> ParsedMarket:
    if raw.get("closed") is not True:
        if raw.get("enableOrderBook") is not True:
            raise MarketParseError("active market is not order-book enabled")
        return parse_market(raw)
    if raw.get("active") is not False or raw.get("acceptingOrders") is not False:
        raise MarketParseError("closed market has contradictory active/order-taking state")
    arrays: list[list[object]] = []
    for field in ("outcomes", "clobTokenIds", "outcomePrices"):
        encoded = raw.get(field)
        if not isinstance(encoded, str):
            raise MarketParseError(f"{field} must be a JSON-encoded array")
        try:
            decoded = json.loads(encoded)
        except json.JSONDecodeError as error:
            raise MarketParseError(f"{field} must contain valid JSON") from error
        if not isinstance(decoded, list) or len(decoded) != 2:
            raise MarketParseError(f"{field} must contain exactly two entries")
        arrays.append(decoded)
    outcomes, tokens, prices = arrays
    yes = [index for index, value in enumerate(outcomes) if isinstance(value, str) and value.casefold() == "yes"]
    if len(yes) != 1 or sorted(str(value).casefold() for value in outcomes) != ["no", "yes"]:
        raise MarketParseError("closed market must have exact Yes/No outcomes")
    index = yes[0]
    token = tokens[index]
    numeric_prices = [valid_price(value) for value in prices]
    price = numeric_prices[index]
    liquidity = raw.get("liquidityNum")
    if not isinstance(token, str) or not token or price is None or any(value is None for value in numeric_prices):
        raise MarketParseError("closed market Yes coordinate is invalid")
    if not math.isclose(sum(value for value in numeric_prices if value is not None), 1.0, rel_tol=0.0, abs_tol=1e-9):
        raise MarketParseError("closed market outcome prices must be complementary")
    if liquidity is None:
        parsed_liquidity = 0.0  # Only a parser placeholder; the emitted closed coordinate stays null.
    elif isinstance(liquidity, bool) or not isinstance(liquidity, (int, float)) or not math.isfinite(float(liquidity)) or float(liquidity) < 0:
        raise MarketParseError("closed market liquidity is invalid")
    else:
        parsed_liquidity = float(liquidity)
    question, title = raw.get("question"), raw.get("groupItemTitle")
    if not isinstance(question, str) or not question or not isinstance(title, str) or not title:
        raise MarketParseError("closed market text fields are invalid")
    return ParsedMarket(question, title, token, price, parsed_liquidity, raw)


def _response(method: str, url: str, response: JsonResponse, observed_at: str) -> dict[str, object]:
    return {"method": method, "url": url, "status": response.status, "observed_at": observed_at, "body": response.data}


def _event_activity(event: Mapping[str, object]) -> dict[str, float | None]:
    result: dict[str, float | None] = {}
    for output, clob, fallback in (
        ("liquidity", "liquidityClob", "liquidity"),
        ("volume_24h", "volume24hrClob", "volume24hr"),
        ("volume_total", "volumeClob", "volume"),
    ):
        value = event.get(clob, event.get(fallback))
        if isinstance(value, bool):
            result[output] = None
            continue
        try:
            number = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            result[output] = None
            continue
        result[output] = number if math.isfinite(number) and number >= 0 else None
    return result


class FomcEventObserver:
    """Observe configured coordinates without constructing a forecast tree."""

    def __init__(self, client: FedPathClient | None = None, *, now: Callable[[], datetime] | None = None) -> None:
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.client = client or FedPathClient(now=self.now)

    def collect(self, config: FedPathConfig, *, markets_config_path: Path) -> dict[str, object]:
        started_at = _utc(self.now)
        raw_responses: list[dict[str, object]] = []
        events: dict[str, dict[str, object]] = {}
        parsed_by_slug: dict[str, dict[str, ParsedMarket]] = {}
        status_by_slug: dict[str, str] = {}
        sources = [(meeting.event_slug, tuple(item.label for item in meeting.outcomes)) for meeting in config.meetings]
        sources.append((config.terminal_event_slug, tuple(item.label for item in config.terminal_buckets)))
        for slug, labels in sources:
            url = f"{self.client.GAMMA_API_BASE}/events/slug/{slug}"
            response = self.client.http.get_json_response(url)
            observed = _utc(self.now)
            raw_responses.append(_response("GET", url, response, observed))
            event = response.data
            if not isinstance(event, dict) or event.get("slug") != slug or not isinstance(event.get("markets"), list):
                raise EventCollectionError(f"{slug} event response is invalid")
            markets = event["markets"]
            assert isinstance(markets, list)
            if any(not isinstance(item, dict) for item in markets):
                raise EventCollectionError(f"{slug} contains a non-object market")
            closed_flags = {item.get("closed") for item in markets if isinstance(item, dict)}
            if closed_flags == {False} and event.get("closed") is False and event.get("active") is True and event.get("enableOrderBook") is True:
                market_status = "active"
            elif closed_flags == {True} and event.get("closed") is True and event.get("active") is False:
                market_status = "closed"
            else:
                raise EventCollectionError(f"{slug} has a mixed or invalid lifecycle state")
            try:
                parsed = [_observable_binary(item) for item in markets if isinstance(item, dict)]
                by_label = FedPathClient._labels(slug, labels, parsed)
            except (MarketParseError, ValueError) as error:
                raise EventCollectionError(f"{slug} topology is invalid: {error}") from error
            events[slug] = event
            parsed_by_slug[slug] = by_label
            status_by_slug[slug] = market_status

        all_tokens = [
            parsed_by_slug[slug][label].yes_token
            for slug, labels in sources for label in labels
        ]
        if len(all_tokens) != len(set(all_tokens)):
            raise EventCollectionError("configured observation surface contains duplicate Yes tokens")

        active = [market for slug, labels in sources if status_by_slug[slug] == "active" for market in (parsed_by_slug[slug][label] for label in labels)]
        midpoint_url = f"{self.client.CLOB_API_BASE}/midpoints"
        midpoints: dict[str, str] = {}
        midpoint_failed = False
        if active:
            try:
                midpoints = self.client.fetch_midpoints(
                    [item.yes_token for item in active],
                    response_recorder=lambda response: raw_responses.append(dict(response)),
                )
            except ApiError:
                midpoint_failed = True
                raw_responses.append({
                    "method": "POST", "url": midpoint_url, "status": None,
                    "observed_at": _utc(self.now), "body": None, "error": "midpoint_fetch_failed",
                })

        coordinates: list[ObservedCoordinate] = []
        for slug, labels in sources:
            kind = "terminal" if slug == config.terminal_event_slug else "meeting"
            meeting_date = next((item.date.isoformat() for item in config.meetings if item.event_slug == slug), None)
            status = status_by_slug[slug]
            closed_prices = [parsed_by_slug[slug][label].gamma_yes_price for label in labels] if status == "closed" else []
            resolved = bool(closed_prices) and all(value in {0.0, 1.0} for value in closed_prices)
            if resolved and (closed_prices.count(1.0) != 1 or not math.isclose(sum(closed_prices), 1.0, abs_tol=1e-12)):
                raise EventCollectionError(f"{slug} has an invalid resolved winner topology")
            closed_state = "resolved" if resolved else "closed_pending_resolution"
            for label in labels:
                market = parsed_by_slug[slug][label]
                bid = ask = spread = None
                diagnostic_codes: list[str] = []
                if status == "closed":
                    observed = _utc(self.now)
                    price = market.gamma_yes_price
                    source = "gamma_resolution" if resolved else "gamma_pending_resolution"
                    quality = "closed_not_live"
                    market_status = closed_state
                else:
                    book_url = f"{self.client.CLOB_API_BASE}/book?{urlencode({'token_id': market.yes_token})}"
                    try:
                        response = self.client.http.get_json_response(book_url)
                        observed = _utc(self.now)
                        raw_responses.append(_response("GET", book_url, response, observed))
                        if not isinstance(response.data, dict):
                            raise ValueError("book must be an object")
                        bid, ask = best_book_prices(response.data)
                        if bid is None or ask is None:
                            raise ValueError("book is one-sided or empty")
                        spread = ask - bid
                        if spread < 0 or spread > config.max_spread + 1e-12:
                            raise ValueError("book spread is invalid")
                        midpoint = valid_price(midpoints.get(market.yes_token))
                        if midpoint is None or not bid <= midpoint <= ask:
                            raise ValueError("midpoint is missing or outside book")
                        price, source, quality = midpoint, "clob_midpoint", "good"
                    except (ApiError, ValueError) as error:
                        observed = _utc(self.now)
                        if isinstance(error, ApiError):
                            raw_responses.append({
                                "method": "GET", "url": book_url, "status": error.status,
                                "observed_at": observed, "body": None, "error": "book_fetch_failed",
                            })
                        gamma = valid_price(market.gamma_yes_price)
                        if gamma is None:
                            raise EventCollectionError(f"{slug}/{label} has no valid fallback price") from error
                        price, source, quality = gamma, "gamma", "degraded"
                        diagnostic_codes.append("gamma_fallback_price")
                        if midpoint_failed:
                            diagnostic_codes.append("midpoint_fetch_failed")
                        diagnostic_codes.append("book_quality_failed")
                    market_status = "active"
                coordinates.append(ObservedCoordinate(
                    slug, kind, meeting_date, label, market.question, market.yes_token, price,
                    source, quality, market_status, observed, None, None, "unavailable",
                    None if status == "closed" and market.raw.get("liquidityNum") is None else market.liquidity_num,
                    bid, ask, spread, tuple(sorted(set(diagnostic_codes))),
                ))
        completed_at = _utc(self.now)
        lifecycle_by_slug = {
            slug: next(iter({item.market_status for item in coordinates if item.event_slug == slug}))
            for slug, _ in sources
        }
        portable_coordinates = []
        for item in coordinates:
            row = asdict(item)
            row["diagnostic_codes"] = list(item.diagnostic_codes)
            portable_coordinates.append(row)
        return {
            "schema_version": 1,
            "collector_version": "fomc-event-observer-v1",
            "started_at": started_at,
            "completed_at": completed_at,
            "markets_config_sha256": sha256_file(markets_config_path),
            "api_bases": {"gamma": self.client.GAMMA_API_BASE, "clob": self.client.CLOB_API_BASE},
            "events": {slug: {"market_status": lifecycle_by_slug[slug], "activity": _event_activity(events[slug])} for slug, _ in sources},
            "coordinates": portable_coordinates,
            "raw_responses": raw_responses,
            "surface": {
                "coordinate_count": len(coordinates),
                "expected_coordinate_count": sum(len(labels) for _, labels in sources),
                "all_coordinates_complete": len(coordinates) == sum(len(labels) for _, labels in sources),
            },
        }
