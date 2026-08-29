"""Live Polymarket collection and strict replay for the fed-path command."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping
from urllib.parse import urlencode

from .client import ApiError, JsonHttpClient, JsonResponse, PriceObservation, Transport, best_book_prices, valid_price
from .fed_path_models import FedPathConfig, MeetingPrice
from .models import Diagnostic
from .parsing import MarketParseError, ParsedMarket, parse_market, parse_rate_bucket


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _finite_json(value: object) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("JSON numbers must be finite")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings")
            _finite_json(item)
    elif isinstance(value, list):
        for item in value:
            _finite_json(item)


def _json(value: object) -> bytes:
    return json.dumps(value, default=lambda item: item.isoformat(), sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _config_digest(config: FedPathConfig) -> str:
    return hashlib.sha256(_json(asdict(config))).hexdigest()


def _freeze(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True)
class FedPathSnapshot:
    schema_version: int
    config_sha256: str
    source_image: str
    source_sha256: str
    target_upper_bound: float
    effective_rate_baseline: float
    standard_move_bp: float
    max_spread: float
    fetched_at: str
    gamma_api_base: str
    clob_api_base: str
    events: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    raw_responses: tuple[Mapping[str, object], ...] = ()
    midpoints: Mapping[str, str] = field(default_factory=dict)
    books: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    selected_prices: tuple[PriceObservation, ...] = ()
    meeting_prices: tuple[MeetingPrice, ...] = ()
    terminal_prices: Mapping[str, float] = field(default_factory=dict)
    diagnostics: tuple[Diagnostic, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "events", _freeze(dict(self.events)))
        object.__setattr__(self, "raw_responses", tuple(_freeze(dict(item)) for item in self.raw_responses))
        object.__setattr__(self, "midpoints", _freeze(dict(self.midpoints)))
        object.__setattr__(self, "books", _freeze(dict(self.books)))
        object.__setattr__(self, "selected_prices", tuple(self.selected_prices))
        object.__setattr__(self, "meeting_prices", tuple(self.meeting_prices))
        object.__setattr__(self, "terminal_prices", _freeze(dict(self.terminal_prices)))
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "config_sha256": self.config_sha256,
            "source_image": self.source_image,
            "source_sha256": self.source_sha256,
            "target_upper_bound": self.target_upper_bound,
            "effective_rate_baseline": self.effective_rate_baseline,
            "standard_move_bp": self.standard_move_bp,
            "max_spread": self.max_spread,
            "fetched_at": self.fetched_at,
            "gamma_api_base": self.gamma_api_base,
            "clob_api_base": self.clob_api_base,
            "events": _thaw(self.events),
            "raw_responses": _thaw(self.raw_responses),
            "midpoints": _thaw(self.midpoints),
            "books": _thaw(self.books),
            "selected_prices": [asdict(item) for item in self.selected_prices],
            "meeting_prices": [{"meeting_date": item.meeting_date.isoformat(), "label": item.label, "raw_probability": item.raw_probability} for item in self.meeting_prices],
            "terminal_prices": _thaw(self.terminal_prices),
            "diagnostics": [asdict(item) for item in self.diagnostics],
        }


@dataclass
class _SnapshotBuilder:
    schema_version: int
    config_sha256: str
    source_image: str
    source_sha256: str
    target_upper_bound: float
    effective_rate_baseline: float
    standard_move_bp: float
    max_spread: float
    fetched_at: str
    gamma_api_base: str
    clob_api_base: str
    events: dict[str, dict[str, object]] = field(default_factory=dict)
    raw_responses: list[dict[str, object]] = field(default_factory=list)
    midpoints: dict[str, str] = field(default_factory=dict)
    books: dict[str, dict[str, object]] = field(default_factory=dict)
    selected_prices: list[PriceObservation] = field(default_factory=list)
    meeting_prices: tuple[MeetingPrice, ...] = ()
    terminal_prices: dict[str, float] = field(default_factory=dict)
    diagnostics: list[Diagnostic] = field(default_factory=list)

    def freeze(self) -> FedPathSnapshot:
        return FedPathSnapshot(
            self.schema_version, self.config_sha256, self.source_image, self.source_sha256,
            self.target_upper_bound, self.effective_rate_baseline, self.standard_move_bp,
            self.max_spread, self.fetched_at, self.gamma_api_base, self.clob_api_base,
            self.events, tuple(self.raw_responses), self.midpoints, self.books,
            tuple(self.selected_prices), self.meeting_prices, self.terminal_prices,
            tuple(self.diagnostics),
        )


class FedPathFetchError(RuntimeError):
    def __init__(self, partial_snapshot: FedPathSnapshot, message: str = "fed-path fetch failed") -> None:
        super().__init__(message)
        self.partial_snapshot = partial_snapshot


class SnapshotReplayError(ValueError):
    """A stored fed-path snapshot was not a portable matching snapshot."""


class FedPathClient:
    GAMMA_API_BASE = "https://gamma-api.polymarket.com"
    CLOB_API_BASE = "https://clob.polymarket.com"

    def __init__(self, transport: Transport | None = None, *, sleep: Callable[[float], None] | None = None, now: Callable[[], datetime] | None = None) -> None:
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.http = JsonHttpClient(transport, sleep=sleep or (lambda _: None), now=self.now)

    def fetch_snapshot(self, config: FedPathConfig) -> FedPathSnapshot:
        snapshot = self._new_snapshot(config)
        candidates: list[tuple[str, str, ParsedMarket]] = []
        sources = [(meeting.event_slug, tuple(item.label for item in meeting.outcomes)) for meeting in config.meetings]
        sources.append((config.terminal_event_slug, tuple(item.label for item in config.terminal_buckets)))
        for slug, labels in sources:
            response = self._event(snapshot, slug)
            try:
                self._validate_event(response, slug)
                markets = response["markets"]
                assert isinstance(markets, list)
                parsed = [self._parse_child(item) if isinstance(item, dict) else self._invalid("market is not an object") for item in markets]
                by_label = self._labels(slug, labels, parsed)
                if len(parsed) != len(labels) or set(by_label) != set(labels):
                    self._invalid("event market topology is not the configured exact topology")
                if len(by_label) != len(parsed):
                    self._invalid("event market topology has duplicate labels")
                snapshot.events[slug] = response
                candidates.extend((slug, label, by_label[label]) for label in labels)
            except (MarketParseError, ValueError, KeyError) as error:
                snapshot.diagnostics.append(Diagnostic("error", slug, "ineligible_event", str(error)))
                raise FedPathFetchError(snapshot.freeze(), f"ineligible event {slug}: {error}") from error
        tokens = [market.yes_token for _, _, market in candidates]
        if len(tokens) != len(set(tokens)):
            snapshot.diagnostics.append(Diagnostic("error", None, "duplicate_yes_token", "Configured markets share a Yes token."))
            raise FedPathFetchError(snapshot.freeze(), "ineligible event topology: duplicate Yes token")
        self._midpoints(snapshot, tokens)
        selected: dict[tuple[str, str], PriceObservation] = {}
        for slug, label, market in candidates:
            try:
                selected[(slug, label)] = self._select(snapshot, slug, market, config.max_spread)
                snapshot.selected_prices.append(selected[(slug, label)])
            except FedPathFetchError as error:
                raise FedPathFetchError(snapshot.freeze(), str(error)) from error
        snapshot.meeting_prices = tuple(
            MeetingPrice(meeting.date, outcome.label, selected[(meeting.event_slug, outcome.label)].price)
            for meeting in config.meetings for outcome in meeting.outcomes
        )
        snapshot.terminal_prices.update({bucket.label: selected[(config.terminal_event_slug, bucket.label)].price for bucket in config.terminal_buckets})
        return snapshot.freeze()

    def fetch_midpoints(
        self, token_ids: list[str], *, response_recorder: Callable[[dict[str, object]], None] | None = None,
    ) -> dict[str, str]:
        """Fetch CLOB midpoints in documented batches of no more than fifty."""
        result: dict[str, str] = {}
        url = f"{self.CLOB_API_BASE}/midpoints"
        for start in range(0, len(token_ids), 50):
            response = self.http.post_json_response(url, [{"token_id": token} for token in token_ids[start:start + 50]])
            _finite_json(response.data)
            if not isinstance(response.data, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in response.data.items()):
                raise ApiError("midpoint response must map strings to strings", url=url, status=response.status, body=response.body)
            result.update(response.data)
            if response_recorder is not None:
                response_recorder(self._response("POST", url, response))
        return result

    def _event(self, snapshot: _SnapshotBuilder, slug: str) -> dict[str, object]:
        url = f"{self.GAMMA_API_BASE}/events/slug/{slug}"
        try:
            response = self.http.get_json_response(url)
            snapshot.raw_responses.append(self._response("GET", url, response))
            _finite_json(response.data)
        except (ApiError, ValueError) as error:
            if isinstance(error, ApiError):
                snapshot.raw_responses.append(self._error("GET", error))
            snapshot.diagnostics.append(Diagnostic("error", slug, "source_fetch_failed", str(error)))
            raise FedPathFetchError(snapshot.freeze(), f"could not fetch {slug}: {error}") from error
        if not isinstance(response.data, dict):
            snapshot.diagnostics.append(Diagnostic("error", slug, "source_fetch_failed", "Gamma event must be an object"))
            raise FedPathFetchError(snapshot.freeze(), f"ineligible event {slug}: Gamma event must be an object")
        return response.data

    @staticmethod
    def _invalid(message: str) -> None:
        raise ValueError(message)

    @staticmethod
    def _parse_child(raw: dict[str, object]) -> ParsedMarket:
        if raw.get("enableOrderBook") is not True:
            raise MarketParseError("market is not order-book enabled")
        parsed = parse_market(raw)
        try:
            outcomes = json.loads(raw["outcomes"])
            tokens = json.loads(raw["clobTokenIds"])
            prices = json.loads(raw["outcomePrices"])
        except (TypeError, json.JSONDecodeError) as error:
            raise MarketParseError("market binary arrays are invalid") from error
        if (
            not all(isinstance(item, list) and len(item) == 2 for item in (outcomes, tokens, prices))
            or sorted(item.casefold() for item in outcomes if isinstance(item, str)) != ["no", "yes"]
        ):
            raise MarketParseError("market must have the exact binary Yes/No topology")
        return parsed

    @staticmethod
    def _labels(slug: str, expected: tuple[str, ...], parsed: list[ParsedMarket]) -> dict[str, ParsedMarket]:
        if slug != "what-will-the-fed-rate-be-at-the-end-of-2026":
            result = {item.title: item for item in parsed}
        else:
            buckets: dict[tuple[str, float], str] = {}
            for label in expected:
                bucket = parse_rate_bucket(label, "")
                buckets[(bucket.kind, bucket.rate)] = label
            result = {}
            for item in parsed:
                bucket = parse_rate_bucket(item.title, item.question)
                label = buckets.get((bucket.kind, bucket.rate))
                if label is None:
                    raise ValueError("terminal market has an unknown canonical bucket")
                if label in result:
                    raise ValueError("terminal market has duplicate canonical buckets")
                result[label] = item
        if len(parsed) != len(expected) or set(result) != set(expected):
            raise ValueError("event market topology is not the configured exact topology")
        return result

    @staticmethod
    def _validate_event(event: Mapping[str, object], slug: str) -> None:
        if event.get("slug") != slug:
            raise ValueError("event response slug does not match requested slug")
        for field, expected in (("active", True), ("closed", False), ("enableOrderBook", True)):
            if event.get(field) is not expected:
                raise ValueError(f"event {slug} is ineligible: {field}")
        if not isinstance(event.get("markets"), list):
            raise ValueError("event markets must be an array")

    def _midpoints(self, snapshot: _SnapshotBuilder, tokens: list[str]) -> None:
        url = f"{self.CLOB_API_BASE}/midpoints"
        for start in range(0, len(tokens), 50):
            payload = [{"token_id": token} for token in tokens[start:start + 50]]
            try:
                response = self.http.post_json_response(url, payload)
                snapshot.raw_responses.append(self._response("POST", url, response))
                _finite_json(response.data)
            except ApiError as error:
                snapshot.raw_responses.append(self._error("POST", error))
                snapshot.diagnostics.append(Diagnostic("warning", None, "midpoint_fetch_failed", str(error)))
                continue
            if not isinstance(response.data, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in response.data.items()):
                snapshot.diagnostics.append(Diagnostic("warning", None, "midpoint_schema_failed", "CLOB midpoint response must map strings to strings."))
                continue
            snapshot.midpoints.update(response.data)

    def _select(self, snapshot: _SnapshotBuilder, source_id: str, market: ParsedMarket, max_spread: float) -> PriceObservation:
        url = f"{self.CLOB_API_BASE}/book?{urlencode({'token_id': market.yes_token})}"
        bid = ask = spread = None
        issue: str | None = None
        try:
            response = self.http.get_json_response(url)
            snapshot.raw_responses.append(self._response("GET", url, response))
            _finite_json(response.data)
            if not isinstance(response.data, dict):
                raise ValueError("book response must be an object")
            snapshot.books[market.yes_token] = response.data
            bid, ask = best_book_prices(response.data)
            if bid is None and ask is None:
                issue = "missing_book"
            elif bid is None or ask is None:
                issue = "one_sided_book"
            else:
                spread = ask - bid
                if spread < 0.0 or spread > max_spread + 1e-12:
                    issue = "excessive_spread"
        except (ApiError, ValueError) as error:
            if isinstance(error, ApiError):
                snapshot.raw_responses.append(self._error("GET", error))
            snapshot.books[market.yes_token] = {"error": str(error)}
            issue = "missing_book"
        midpoint = valid_price(snapshot.midpoints.get(market.yes_token))
        if issue is None and midpoint is not None and bid is not None and ask is not None and not bid <= midpoint <= ask:
            issue = "midpoint_outside_book"
        if issue is None and midpoint is not None:
            return PriceObservation(source_id, market.question, market.title, market.yes_token, midpoint, "clob_midpoint", "good", self._utc_now(), market.liquidity_num, bid, ask, spread)
        snapshot.diagnostics.append(Diagnostic("warning", source_id, issue or "price_quality_failed", f"CLOB price quality failed for {market.question}"))
        gamma = valid_price(market.gamma_yes_price)
        if gamma is None:
            raise FedPathFetchError(snapshot.freeze(), f"ineligible market {market.title}: no valid price")
        snapshot.diagnostics.append(Diagnostic("warning", source_id, "gamma_fallback_price", f"Used Gamma outcomePrices fallback for {market.question}"))
        return PriceObservation(source_id, market.question, market.title, market.yes_token, gamma, "gamma", "degraded", self._utc_now(), market.liquidity_num, bid, ask, spread)

    def _new_snapshot(self, config: FedPathConfig) -> _SnapshotBuilder:
        return _SnapshotBuilder(1, _config_digest(config), config.source_image, config.source_sha256, config.target_upper_bound, config.effective_rate_baseline, config.standard_move_bp, config.max_spread, self._utc_now(), self.GAMMA_API_BASE, self.CLOB_API_BASE)

    def _utc_now(self) -> str:
        return self.now().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _response(self, method: str, url: str, response: JsonResponse) -> dict[str, object]:
        return {"method": method, "url": url, "status": response.status, "retrieved_at": self._utc_now(), "body": response.data}

    def _error(self, method: str, error: ApiError) -> dict[str, object]:
        body: object = None
        if error.body is not None:
            try:
                body = json.loads(error.body, parse_constant=_reject_constant)
                _finite_json(body)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                body = error.body.decode("utf-8", errors="replace")
        return {"method": method, "url": error.url, "status": error.status, "retrieved_at": self._utc_now(), "body": body, "error": str(error)}


_SNAPSHOT_KEYS = frozenset(FedPathSnapshot.__dataclass_fields__)
_PRICE_KEYS = frozenset(PriceObservation.__dataclass_fields__)
_MEETING_PRICE_KEYS = frozenset({"meeting_date", "label", "raw_probability"})
_DIAGNOSTIC_KEYS = frozenset({"severity", "source_id", "code", "message"})


def _exact(value: object, keys: frozenset[str], name: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        raise SnapshotReplayError(f"{name} has an invalid schema")
    return value


def _finite_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise SnapshotReplayError(f"{name} must be a finite number")
    return float(value)


def _timestamp(value: object, name: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", value) is None:
        raise SnapshotReplayError(f"{name} must be a UTC Z timestamp")
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise SnapshotReplayError(f"{name} is invalid") from error
    return value


def load_fed_path_snapshot(path: Path, config: FedPathConfig, *, transport: Transport | None = None, project_root: Path | None = None) -> FedPathSnapshot:
    """Load only portable standard JSON, revalidate identity, and never use HTTP."""
    del transport  # Explicitly prove this API cannot route replay through a transport.
    try:
        raw = json.loads(
            Path(path).read_text(encoding="utf-8"),
            parse_constant=_reject_constant,
            object_pairs_hook=_reject_duplicate_keys,
        )
        _finite_json(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise SnapshotReplayError(f"snapshot is not strict standard JSON: {error}") from error
    payload = _exact(raw, _SNAPSHOT_KEYS, "snapshot")
    identity = (payload["config_sha256"], payload["source_image"], payload["source_sha256"], payload["target_upper_bound"], payload["effective_rate_baseline"], payload["standard_move_bp"], payload["max_spread"])
    expected = (_config_digest(config), config.source_image, config.source_sha256, config.target_upper_bound, config.effective_rate_baseline, config.standard_move_bp, config.max_spread)
    if identity != expected:
        raise SnapshotReplayError("snapshot identity does not match the active configuration")
    if isinstance(payload["schema_version"], bool) or payload["schema_version"] != 1:
        raise SnapshotReplayError("snapshot metadata is invalid")
    _timestamp(payload["fetched_at"], "fetched_at")
    if payload["gamma_api_base"] != FedPathClient.GAMMA_API_BASE or payload["clob_api_base"] != FedPathClient.CLOB_API_BASE:
        raise SnapshotReplayError("snapshot API bases do not match the pinned public endpoints")
    root = Path(project_root) if project_root is not None else Path(__file__).resolve().parents[2]
    image = (root / config.source_image).resolve()
    if root.resolve() not in image.parents or not image.is_file() or hashlib.sha256(image.read_bytes()).hexdigest() != config.source_sha256:
        raise SnapshotReplayError("active reference image identity does not match configuration")
    for name in ("events", "midpoints", "books", "terminal_prices"):
        if not isinstance(payload[name], dict) or any(not isinstance(key, str) for key in payload[name]):
            raise SnapshotReplayError(f"snapshot {name} is invalid")
    if not isinstance(payload["raw_responses"], list) or not isinstance(payload["selected_prices"], list) or not isinstance(payload["meeting_prices"], list) or not isinstance(payload["diagnostics"], list):
        raise SnapshotReplayError("snapshot collection fields are invalid")
    _validate_raw_responses(payload["raw_responses"])
    prices: list[PriceObservation] = []
    for item in payload["selected_prices"]:
        row = _exact(item, _PRICE_KEYS, "selected price")
        numeric = ("price", "liquidity_num", "best_bid", "best_ask", "spread")
        for key in numeric:
            if row[key] is not None:
                _finite_number(row[key], f"selected price {key}")
        if not all(isinstance(row[key], str) for key in ("source_id", "question", "title", "yes_token", "source", "quality", "retrieved_at")):
            raise SnapshotReplayError("selected price text fields are invalid")
        if not 0.0 <= _finite_number(row["price"], "selected price") <= 1.0 or not 0.0 <= _finite_number(row["liquidity_num"], "selected liquidity"):
            raise SnapshotReplayError("selected price values are out of range")
        _timestamp(row["retrieved_at"], "selected price retrieved_at")
        prices.append(PriceObservation(**row))  # type: ignore[arg-type]
    meetings: list[MeetingPrice] = []
    for item in payload["meeting_prices"]:
        row = _exact(item, _MEETING_PRICE_KEYS, "meeting price")
        try:
            from datetime import date
            value_date = date.fromisoformat(row["meeting_date"])
        except (TypeError, ValueError) as error:
            raise SnapshotReplayError("meeting price date is invalid") from error
        if not isinstance(row["label"], str):
            raise SnapshotReplayError("meeting price label is invalid")
        probability = _finite_number(row["raw_probability"], "meeting raw probability")
        if not 0.0 <= probability <= 1.0:
            raise SnapshotReplayError("meeting raw probability is out of range")
        meetings.append(MeetingPrice(value_date, row["label"], probability))
    terminal = {key: _finite_number(value, "terminal raw probability") for key, value in payload["terminal_prices"].items()}
    if any(not 0.0 <= value <= 1.0 for value in terminal.values()):
        raise SnapshotReplayError("terminal raw probability is out of range")
    diagnostics: list[Diagnostic] = []
    for item in payload["diagnostics"]:
        row = _exact(item, _DIAGNOSTIC_KEYS, "diagnostic")
        if not isinstance(row["severity"], str) or row["source_id"] is not None and not isinstance(row["source_id"], str) or not isinstance(row["code"], str) or not isinstance(row["message"], str):
            raise SnapshotReplayError("diagnostic is invalid")
        diagnostics.append(Diagnostic(**row))  # type: ignore[arg-type]
    snapshot = FedPathSnapshot(1, payload["config_sha256"], payload["source_image"], payload["source_sha256"], _finite_number(payload["target_upper_bound"], "target upper baseline"), _finite_number(payload["effective_rate_baseline"], "effective baseline"), _finite_number(payload["standard_move_bp"], "standard move"), _finite_number(payload["max_spread"], "maximum spread"), payload["fetched_at"], payload["gamma_api_base"], payload["clob_api_base"], payload["events"], payload["raw_responses"], payload["midpoints"], payload["books"], prices, tuple(meetings), terminal, diagnostics)  # type: ignore[arg-type]
    _validate_replayed_topology(snapshot, config)
    return snapshot


def _validate_replayed_topology(snapshot: FedPathSnapshot, config: FedPathConfig) -> None:
    expected_meetings = {(meeting.date, outcome.label) for meeting in config.meetings for outcome in meeting.outcomes}
    actual_meetings = {(item.meeting_date, item.label) for item in snapshot.meeting_prices}
    if len(snapshot.meeting_prices) != 15 or actual_meetings != expected_meetings:
        raise SnapshotReplayError("meeting replay topology does not match configuration")
    if set(snapshot.terminal_prices) != {item.label for item in config.terminal_buckets}:
        raise SnapshotReplayError("terminal replay topology does not match configuration")
    expected: list[tuple[str, str, ParsedMarket]] = []
    sources = [(meeting.event_slug, tuple(item.label for item in meeting.outcomes)) for meeting in config.meetings]
    sources.append((config.terminal_event_slug, tuple(item.label for item in config.terminal_buckets)))
    if set(snapshot.events) != {slug for slug, _ in sources}:
        raise SnapshotReplayError("replayed events do not match the configured sources")
    raw_success = {
        (row["method"], row["url"]): row["body"]
        for row in snapshot.raw_responses
        if (
            "error" not in row
            and row["status"] is not None
            and 200 <= row["status"] < 300
        )
    }
    midpoint_body: dict[str, str] = {}
    for row in snapshot.raw_responses:
        if (
            row["method"] == "POST"
            and row["url"] == f"{FedPathClient.CLOB_API_BASE}/midpoints"
            and "error" not in row
            and row["status"] is not None
            and 200 <= row["status"] < 300
        ):
            if (
                not isinstance(row["body"], Mapping)
                or any(
                    not isinstance(key, str) or not isinstance(value, str)
                    for key, value in row["body"].items()
                )
            ):
                # Live collection records a finite schema-invalid 2xx body,
                # emits midpoint_schema_failed, and degrades to Gamma.
                continue
            midpoint_body.update(row["body"])
    if midpoint_body != dict(snapshot.midpoints):
        raise SnapshotReplayError("midpoint raw responses do not reconstruct stored midpoints")
    for slug, labels in sources:
        event = _thaw(snapshot.events[slug])
        if not isinstance(event, dict):
            raise SnapshotReplayError("replayed event is not an object")
        try:
            FedPathClient._validate_event(event, slug)
            if _thaw(raw_success.get(("GET", f"{FedPathClient.GAMMA_API_BASE}/events/slug/{slug}"))) != event:
                raise ValueError("event GET raw body does not exactly match stored event")
            markets = event.get("markets")
            if not isinstance(markets, list):
                raise ValueError("markets")
            parsed = [FedPathClient._parse_child(item) for item in markets if isinstance(item, dict)]
            if len(parsed) != len(markets):
                raise ValueError("market object")
            by_label = FedPathClient._labels(slug, labels, parsed)
        except (MarketParseError, ValueError) as error:
            raise SnapshotReplayError(f"replayed event topology is invalid: {error}") from error
        expected.extend((slug, label, by_label[label]) for label in labels)
    if len(snapshot.selected_prices) != 30 or len(expected) != 30:
        raise SnapshotReplayError("replay must contain exactly thirty selected observations")
    allowed_urls = {
        *(f"{FedPathClient.GAMMA_API_BASE}/events/slug/{slug}" for slug, _ in sources),
        f"{FedPathClient.CLOB_API_BASE}/midpoints",
        *(
            f"{FedPathClient.CLOB_API_BASE}/book?"
            f"{urlencode({'token_id': market.yes_token})}"
            for _, _, market in expected
        ),
    }
    if any(row["url"] not in allowed_urls for row in snapshot.raw_responses):
        raise SnapshotReplayError("raw responses contain an unexpected endpoint")
    selected_by_token: dict[str, PriceObservation] = {}
    for observation, (slug, label, market) in zip(snapshot.selected_prices, expected, strict=True):
        if (observation.source_id, observation.question, observation.title, observation.yes_token) != (slug, market.question, market.title, market.yes_token):
            raise SnapshotReplayError("selected observation does not reconcile to the stored Gamma market")
        if observation.yes_token in selected_by_token:
            raise SnapshotReplayError("selected observations contain duplicate tokens")
        selected_by_token[observation.yes_token] = observation
        book_url = (
            f"{FedPathClient.CLOB_API_BASE}/book?"
            f"{urlencode({'token_id': observation.yes_token})}"
        )
        book = snapshot.books.get(observation.yes_token)
        if not isinstance(book, Mapping):
            raise SnapshotReplayError("selected observation lacks stored book evidence")
        if _thaw(book) != _raw_book_evidence(snapshot.raw_responses, book_url):
            raise SnapshotReplayError(
                "book raw response does not match stored book evidence"
            )
        if observation.source == "clob_midpoint":
            midpoint = valid_price(snapshot.midpoints.get(observation.yes_token))
            if midpoint is None or midpoint != observation.price:
                raise SnapshotReplayError("CLOB selected observation lacks matching midpoint/book evidence")
            _validate_observation(observation, market, slug, snapshot.midpoints, book, config.max_spread)
        elif observation.source == "gamma":
            if observation.price != market.gamma_yes_price or observation.quality != "degraded":
                raise SnapshotReplayError("Gamma fallback observation does not reconcile")
            _validate_observation(observation, market, slug, snapshot.midpoints, book, config.max_spread)
        else:
            raise SnapshotReplayError("selected observation has an unknown source")
    expected_meeting_values = tuple((item.meeting_date, item.label, item.raw_probability) for item in snapshot.meeting_prices)
    actual_meeting_values = tuple((meeting.date, label, selected_by_token[market.yes_token].price) for meeting in config.meetings for label, market in ((outcome.label, next(item for slug, name, item in expected if slug == meeting.event_slug and name == outcome.label)) for outcome in meeting.outcomes))
    if expected_meeting_values != actual_meeting_values:
        raise SnapshotReplayError("meeting inputs do not reconcile to selected observations")
    for bucket in config.terminal_buckets:
        market = next(item for slug, label, item in expected if slug == config.terminal_event_slug and label == bucket.label)
        if snapshot.terminal_prices[bucket.label] != selected_by_token[market.yes_token].price:
            raise SnapshotReplayError("terminal inputs do not reconcile to selected observations")


def _validate_raw_responses(rows: list[object]) -> None:
    if not rows:
        raise SnapshotReplayError("raw responses must not be empty")
    for item in rows:
        if not isinstance(item, dict):
            raise SnapshotReplayError("raw response must be an object")
        expected = {"method", "url", "status", "retrieved_at", "body"}
        if set(item) not in (expected, expected | {"error"}):
            raise SnapshotReplayError("raw response has an invalid schema")
        if item["method"] not in ("GET", "POST") or not isinstance(item["url"], str) or isinstance(item["status"], bool) or item["status"] is not None and not isinstance(item["status"], int):
            raise SnapshotReplayError("raw response metadata is invalid")
        successful = "error" not in item and (
            isinstance(item["status"], int)
            and 200 <= item["status"] < 300
        )
        if successful and "error" in item:
            raise SnapshotReplayError("successful raw response cannot contain an error")
        if not successful and (
            not isinstance(item.get("error"), str) or not item["error"]
        ):
            raise SnapshotReplayError("failed raw response must contain an error")
        _timestamp(item["retrieved_at"], "raw response retrieved_at")
        _finite_json(item["body"])


def _raw_book_evidence(
    rows: tuple[Mapping[str, object], ...],
    url: str,
) -> dict[str, object]:
    matches = [
        row
        for row in rows
        if row["method"] == "GET" and row["url"] == url
    ]
    if len(matches) != 1:
        raise SnapshotReplayError(
            "each selected token must have exactly one raw book response"
        )
    row = matches[0]
    status = row["status"]
    if "error" not in row and isinstance(status, int) and 200 <= status < 300:
        body = _thaw(row["body"])
        if isinstance(body, dict):
            return body
        return {"error": "book response must be an object"}
    return {"error": row["error"]}


def _validate_observation(observation: PriceObservation, market: ParsedMarket, source_id: str, midpoints: Mapping[str, str], book: Mapping[str, object], max_spread: float) -> None:
    raw_book = _thaw(book)
    if not isinstance(raw_book, dict):
        raise SnapshotReplayError("stored book is not an object")
    bid, ask = best_book_prices(raw_book)
    spread = None if bid is None or ask is None else ask - bid
    issue = "missing_book" if bid is None and ask is None else "one_sided_book" if bid is None or ask is None else "excessive_spread" if spread is not None and (spread < 0 or spread > max_spread + 1e-12) else None
    midpoint = valid_price(midpoints.get(market.yes_token))
    if issue is None and midpoint is not None and bid is not None and ask is not None and not bid <= midpoint <= ask:
        issue = "midpoint_outside_book"
    if issue is None and midpoint is not None:
        expected = (midpoint, "clob_midpoint", "good")
    else:
        expected = (market.gamma_yes_price, "gamma", "degraded")
    actual = (observation.price, observation.source, observation.quality)
    if actual != expected or (observation.best_bid, observation.best_ask, observation.spread, observation.liquidity_num) != (bid, ask, spread, market.liquidity_num):
        raise SnapshotReplayError("selected price audit does not match reconstructed price-quality selection")
