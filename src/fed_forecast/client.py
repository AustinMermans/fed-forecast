"""Public, read-only Polymarket HTTP client and raw snapshot collection."""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, Mapping, Protocol
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .models import AppConfig, Diagnostic
from .parsing import MarketParseError, ParsedMarket, parse_market


JsonValue = dict[str, object] | list[object]
Clock = Callable[[], datetime]


def _reject_json_constant(constant: str) -> None:
    raise ValueError(f"non-standard JSON constant: {constant}")


def _strict_json_loads(data: bytes) -> object:
    result = json.loads(data, parse_constant=_reject_json_constant)
    _validate_finite_json(result)
    return result


def _validate_finite_json(value: object) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite JSON number")
    if isinstance(value, dict):
        for item in value.values():
            _validate_finite_json(item)
    elif isinstance(value, list):
        for item in value:
            _validate_finite_json(item)


def valid_price(value: object) -> float | None:
    """Return a finite inclusive probability, or ``None`` for invalid input."""
    if isinstance(value, bool):
        return None
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(price) or not 0.0 <= price <= 1.0:
        return None
    return price


def best_book_prices(book: Mapping[str, object]) -> tuple[float | None, float | None]:
    """Select the best valid bid and ask from a CLOB book response."""
    def prices(side: object) -> list[float]:
        if not isinstance(side, list):
            return []
        result: list[float] = []
        for order in side:
            if not isinstance(order, dict):
                continue
            value = valid_price(order.get("price"))
            if value is not None:
                result.append(value)
        return result

    bids = prices(book.get("bids"))
    asks = prices(book.get("asks"))
    return (max(bids) if bids else None, min(asks) if asks else None)


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes
    headers: Mapping[str, str]


AttemptRecorder = Callable[[str, str, HttpResponse], None]


@dataclass(frozen=True)
class JsonResponse:
    status: int
    body: bytes
    headers: Mapping[str, str]
    data: JsonValue


class Transport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None,
        headers: Mapping[str, str],
        timeout: float,
    ) -> HttpResponse: ...


class UrllibTransport:
    """Standard-library transport that preserves HTTP error responses."""

    def request(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None,
        headers: Mapping[str, str],
        timeout: float,
    ) -> HttpResponse:
        request = Request(url, data=body, headers=dict(headers), method=method)
        try:
            with urlopen(request, timeout=timeout) as response:
                return HttpResponse(
                    response.status,
                    response.read(),
                    dict(response.headers.items()),
                )
        except HTTPError as error:
            return HttpResponse(error.code, error.read(), dict(error.headers.items()))


class ApiError(RuntimeError):
    """Typed API or response-validation failure."""

    def __init__(
        self,
        message: str,
        *,
        url: str,
        status: int | None = None,
        body: bytes | None = None,
    ) -> None:
        super().__init__(message)
        self.url = url
        self.status = status
        self.body = body


class JsonHttpClient:
    USER_AGENT = "fed-forecast/0.1"

    def __init__(
        self,
        transport: Transport | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        now: Clock | None = None,
        timeout: float = 15.0,
        attempt_recorder: AttemptRecorder | None = None,
    ) -> None:
        self.transport = transport or UrllibTransport()
        self.sleep = sleep
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.timeout = timeout
        self.attempt_recorder = attempt_recorder

    def get_json(self, url: str) -> JsonValue:
        return self.get_json_response(url).data

    def post_json(self, url: str, payload: object) -> JsonValue:
        return self.post_json_response(url, payload).data

    def get_json_response(self, url: str) -> JsonResponse:
        return self._request_json("GET", url, None)

    def post_json_response(self, url: str, payload: object) -> JsonResponse:
        return self._request_json(
            "POST",
            url,
            json.dumps(
                payload,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8"),
        )

    def _request_json(self, method: str, url: str, body: bytes | None) -> JsonResponse:
        headers = {"User-Agent": self.USER_AGENT, "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        for attempt in range(3):
            try:
                response = self.transport.request(
                    method,
                    url,
                    body=body,
                    headers=headers,
                    timeout=self.timeout,
                )
            except Exception as error:
                raise ApiError(f"request failed: {error}", url=url) from error
            if self.attempt_recorder is not None:
                self.attempt_recorder(method, url, response)
            if 200 <= response.status < 300:
                try:
                    result = _strict_json_loads(response.body)
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
                    raise ApiError(
                        "response was not valid JSON",
                        url=url,
                        status=response.status,
                        body=response.body,
                    ) from error
                if not isinstance(result, (dict, list)):
                    raise ApiError(
                        "JSON response must be an object or array",
                        url=url,
                        status=response.status,
                        body=response.body,
                    )
                return JsonResponse(
                    response.status,
                    response.body,
                    response.headers,
                    result,
                )
            transient = response.status == 429 or 500 <= response.status <= 599
            if transient and attempt < 2:
                self.sleep(self._retry_delay(response.headers, attempt))
                continue
            raise ApiError(
                f"HTTP {response.status}",
                url=url,
                status=response.status,
                body=response.body,
            )
        raise AssertionError("unreachable")

    def _retry_delay(self, headers: Mapping[str, str], attempt: int) -> float:
        fallback = min(float(2**attempt), 2.0)
        retry_after = next(
            (value for key, value in headers.items() if key.casefold() == "retry-after"),
            None,
        )
        if retry_after is None:
            return fallback
        try:
            delay = float(retry_after)
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(retry_after)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                delay = (retry_at - self.now()).total_seconds()
            except (TypeError, ValueError, OverflowError):
                delay = fallback
        if not math.isfinite(delay):
            return fallback
        return min(max(delay, 0.0), 2.0)


@dataclass(frozen=True)
class PriceObservation:
    source_id: str
    question: str
    title: str
    yes_token: str
    price: float
    source: str
    quality: str
    retrieved_at: str
    liquidity_num: float
    best_bid: float | None
    best_ask: float | None
    spread: float | None


@dataclass
class RawSnapshot:
    config_sha256: str
    source_image: str
    source_sha256: str
    transcription_verified: bool
    scenario_as_of: str
    scenario_horizon_end: str
    policy_upper_bound: float
    policy_source_date: str
    policy_source_url: str
    fetched_at: str
    gamma_api_base: str
    clob_api_base: str
    events: dict[str, dict[str, object]] = field(default_factory=dict)
    raw_responses: list[dict[str, object]] = field(default_factory=list)
    midpoints: dict[str, str] = field(default_factory=dict)
    books: dict[str, dict[str, object]] = field(default_factory=dict)
    prices: list[PriceObservation] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class SnapshotFetchError(RuntimeError):
    def __init__(self, partial_snapshot: RawSnapshot) -> None:
        super().__init__("no Polymarket event source could be fetched")
        self.partial_snapshot = partial_snapshot


class PolymarketClient:
    GAMMA_API_BASE = "https://gamma-api.polymarket.com"
    CLOB_API_BASE = "https://clob.polymarket.com"

    def __init__(
        self,
        transport: Transport | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        now: Clock | None = None,
    ) -> None:
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.http = JsonHttpClient(transport, sleep=sleep, now=self.now)

    def fetch_midpoints(
        self,
        token_ids: list[str],
        *,
        response_recorder: Callable[[JsonResponse], None] | None = None,
    ) -> dict[str, str]:
        result: dict[str, str] = {}
        url = f"{self.CLOB_API_BASE}/midpoints"
        for start in range(0, len(token_ids), 50):
            batch = token_ids[start : start + 50]
            payload = [{"token_id": token_id} for token_id in batch]
            http_response = self.http.post_json_response(url, payload)
            response = http_response.data
            if not isinstance(response, dict):
                raise ApiError(
                    "midpoint response must be an object",
                    url=url,
                    status=http_response.status,
                    body=http_response.body,
                )
            for token_id, midpoint in response.items():
                if not isinstance(token_id, str) or not isinstance(midpoint, str):
                    raise ApiError(
                        "midpoint entries must map strings to strings",
                        url=url,
                        status=http_response.status,
                        body=http_response.body,
                    )
                result[token_id] = midpoint
            if response_recorder is not None:
                response_recorder(http_response)
        return result

    def fetch_snapshot(self, config: AppConfig) -> RawSnapshot:
        snapshot = self._new_snapshot(config)
        candidates: list[tuple[str, ParsedMarket]] = []
        for source in config.evidence_sources:
            url = f"{self.GAMMA_API_BASE}/events/slug/{source.event_slug}"
            try:
                http_response = self.http.get_json_response(url)
                response = http_response.data
                if not isinstance(response, dict):
                    raise ApiError(
                        "Gamma event response must be an object",
                        url=url,
                        status=http_response.status,
                        body=http_response.body,
                    )
            except ApiError as error:
                snapshot.raw_responses.append(
                    self._error_response("GET", error, self._utc_now())
                )
                snapshot.diagnostics.append(
                    Diagnostic(
                        "warning",
                        source.id,
                        "source_fetch_failed",
                        f"Could not fetch Gamma event {source.event_slug}: {error}",
                    )
                )
                continue
            snapshot.raw_responses.append(
                {
                    "method": "GET",
                    "url": url,
                    "status": http_response.status,
                    "retrieved_at": self._utc_now(),
                    "body": response,
                }
            )
            markets = response.get("markets")
            if not isinstance(markets, list):
                snapshot.diagnostics.append(
                    Diagnostic(
                        "warning",
                        source.id,
                        "source_fetch_failed",
                        "Gamma event markets must be an array",
                    )
                )
                continue
            snapshot.events[source.id] = response
            for index, raw_market in enumerate(markets):
                if not isinstance(raw_market, dict):
                    snapshot.diagnostics.append(
                        Diagnostic(
                            "warning",
                            source.id,
                            "market_parse_failed",
                            f"Market {index} is not an object",
                        )
                    )
                    continue
                try:
                    candidates.append((source.id, parse_market(raw_market)))
                except MarketParseError as error:
                    ineligible = (
                        raw_market.get("active") is not True
                        or raw_market.get("closed") is not False
                        or raw_market.get("acceptingOrders") is not True
                    )
                    snapshot.diagnostics.append(
                        Diagnostic(
                            "warning",
                            source.id,
                            (
                                "market_not_accepting_orders"
                                if ineligible
                                else "market_parse_failed"
                            ),
                            f"Skipped market {raw_market.get('question', index)!r}: {error}",
                        )
                    )

        if not snapshot.events:
            raise SnapshotFetchError(snapshot)

        token_ids = [market.yes_token for _, market in candidates]
        if token_ids:
            def record_midpoint_response(http_response: JsonResponse) -> None:
                response = http_response.data
                assert isinstance(response, dict)
                snapshot.midpoints.update(response)
                snapshot.raw_responses.append(
                    {
                        "method": "POST",
                        "url": f"{self.CLOB_API_BASE}/midpoints",
                        "status": http_response.status,
                        "retrieved_at": self._utc_now(),
                        "body": response,
                    }
                )

            try:
                snapshot.midpoints.update(
                    self.fetch_midpoints(
                        token_ids,
                        response_recorder=record_midpoint_response,
                    )
                )
            except ApiError as error:
                snapshot.raw_responses.append(
                    self._error_response("POST", error, self._utc_now())
                )
                snapshot.diagnostics.append(
                    Diagnostic(
                        "warning",
                        None,
                        "price_quality_failed",
                        f"Could not fetch CLOB midpoints: {error}",
                    )
                )

        for source_id, market in candidates:
            self._collect_price(snapshot, source_id, market, config.max_spread)
        return snapshot

    def _new_snapshot(self, config: AppConfig) -> RawSnapshot:
        config_payload = json.dumps(
            asdict(config),
            default=lambda value: value.isoformat(),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return RawSnapshot(
            config_sha256=hashlib.sha256(config_payload).hexdigest(),
            source_image=config.source_image,
            source_sha256=config.source_sha256,
            transcription_verified=config.transcription_verified,
            scenario_as_of=config.scenario_as_of.isoformat(),
            scenario_horizon_end=config.scenario_horizon_end.isoformat(),
            policy_upper_bound=config.policy_upper_bound,
            policy_source_date=config.policy_source_date.isoformat(),
            policy_source_url=config.policy_source_url,
            fetched_at=self._utc_now(),
            gamma_api_base=self.GAMMA_API_BASE,
            clob_api_base=self.CLOB_API_BASE,
        )

    def _collect_price(
        self,
        snapshot: RawSnapshot,
        source_id: str,
        market: ParsedMarket,
        max_spread: float,
    ) -> None:
        book_url = f"{self.CLOB_API_BASE}/book?{urlencode({'token_id': market.yes_token})}"
        best_bid: float | None = None
        best_ask: float | None = None
        spread: float | None = None
        quality_code: str | None = None
        try:
            http_response = self.http.get_json_response(book_url)
            response = http_response.data
            if not isinstance(response, dict):
                raise ApiError(
                    "book response must be an object",
                    url=book_url,
                    status=http_response.status,
                    body=http_response.body,
                )
            snapshot.books[market.yes_token] = response
            snapshot.raw_responses.append(
                {
                    "method": "GET",
                    "url": book_url,
                    "status": http_response.status,
                    "retrieved_at": self._utc_now(),
                    "body": response,
                }
            )
            best_bid, best_ask = self._best_prices(response)
            if best_bid is None and best_ask is None:
                quality_code = "missing_book"
            elif best_bid is None or best_ask is None:
                quality_code = "one_sided_book"
            else:
                spread = best_ask - best_bid
                if spread < 0.0 or spread > max_spread + 1e-12:
                    quality_code = "excessive_spread"
        except ApiError as error:
            snapshot.raw_responses.append(
                self._error_response("GET", error, self._utc_now())
            )
            snapshot.books[market.yes_token] = {
                "error": str(error),
                "status": error.status,
            }
            quality_code = "missing_book"

        midpoint = self._valid_price(snapshot.midpoints.get(market.yes_token))
        if (
            quality_code is None
            and midpoint is not None
            and best_bid is not None
            and best_ask is not None
            and not best_bid <= midpoint <= best_ask
        ):
            quality_code = "midpoint_outside_book"
        if quality_code is None and midpoint is not None:
            snapshot.prices.append(
                PriceObservation(
                    source_id,
                    market.question,
                    market.title,
                    market.yes_token,
                    midpoint,
                    "clob_midpoint",
                    "good",
                    self._utc_now(),
                    market.liquidity_num,
                    best_bid,
                    best_ask,
                    spread,
                )
            )
            return
        if quality_code is None:
            quality_code = "price_quality_failed"
        snapshot.diagnostics.append(
            Diagnostic(
                "warning",
                source_id,
                quality_code,
                f"CLOB price quality failed for {market.question}",
            )
        )
        gamma_price = self._valid_price(market.gamma_yes_price)
        if gamma_price is None:
            snapshot.diagnostics.append(
                Diagnostic(
                    "warning",
                    source_id,
                    "price_quality_failed",
                    f"No valid CLOB or Gamma Yes price for {market.question}",
                )
            )
            return
        snapshot.prices.append(
            PriceObservation(
                source_id,
                market.question,
                market.title,
                market.yes_token,
                gamma_price,
                "gamma",
                "degraded",
                self._utc_now(),
                market.liquidity_num,
                best_bid,
                best_ask,
                spread,
            )
        )
        snapshot.diagnostics.append(
            Diagnostic(
                "warning",
                source_id,
                "gamma_fallback_price",
                f"Used Gamma outcomePrices fallback for {market.question}",
            )
        )

    @staticmethod
    def _best_prices(book: dict[str, object]) -> tuple[float | None, float | None]:
        return best_book_prices(book)

    @staticmethod
    def _valid_price(value: object) -> float | None:
        return valid_price(value)

    def _utc_now(self) -> str:
        return self.now().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _error_response(
        method: str,
        error: ApiError,
        retrieved_at: str,
    ) -> dict[str, object]:
        body: object = None
        if error.body is not None:
            try:
                body = _strict_json_loads(error.body)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                body = error.body.decode("utf-8", errors="replace")
        return {
            "method": method,
            "url": error.url,
            "status": error.status,
            "retrieved_at": retrieved_at,
            "body": body,
            "error": str(error),
        }
