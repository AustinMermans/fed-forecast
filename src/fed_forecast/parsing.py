"""Strict parsing of Polymarket Gamma market records."""

from dataclasses import dataclass
import json
import math
import re


class MarketParseError(ValueError):
    """Raised when a Gamma market is malformed or ineligible."""


@dataclass(frozen=True)
class RateBucket:
    kind: str
    rate: float

    def __post_init__(self) -> None:
        if self.kind not in {"exact", "lte", "gte"}:
            raise ValueError(f"unsupported rate bucket kind: {self.kind}")
        if not math.isfinite(self.rate):
            raise ValueError("rate bucket must be finite")

    def contains(self, value: float) -> bool:
        if self.kind == "exact":
            return math.isclose(value, self.rate, rel_tol=0.0, abs_tol=1e-9)
        if self.kind == "lte":
            return value <= self.rate
        return value >= self.rate


def parse_rate_bucket(label: str, question: str) -> RateBucket:
    """Parse a policy-rate outcome label."""
    title_match = re.fullmatch(r"\s*(?:(≤|≥)\s*)?(\d+(?:\.\d+)?)%\s*", label)
    if title_match:
        symbol, rate = title_match.groups()
        kind = {"≤": "lte", "≥": "gte", None: "exact"}[symbol]
        return RateBucket(kind, float(rate))
    question_match = re.fullmatch(
        r"Will the upper bound of the target federal funds rate be "
        r"(\d+(?:\.\d+)?)% at the end of 2026\?",
        question,
    )
    if question_match:
        return RateBucket("exact", float(question_match.group(1)))
    raise ValueError("market does not contain a valid policy-rate bucket")


@dataclass(frozen=True)
class ParsedMarket:
    question: str
    title: str
    yes_token: str
    gamma_yes_price: float
    liquidity_num: float
    raw: dict[str, object]


def parse_market(raw: dict[str, object]) -> ParsedMarket:
    """Parse an eligible binary Gamma market."""
    if raw.get("active") is not True:
        raise MarketParseError("market is not active")
    if raw.get("closed") is not False:
        raise MarketParseError("market is closed")
    if raw.get("acceptingOrders") is not True:
        raise MarketParseError("market is not accepting orders")

    arrays: list[list[object]] = []
    for field in ("outcomes", "clobTokenIds", "outcomePrices"):
        encoded = raw.get(field)
        if not isinstance(encoded, str):
            raise MarketParseError(f"{field} must be a JSON-encoded array")
        try:
            decoded = json.loads(encoded)
        except json.JSONDecodeError as error:
            raise MarketParseError(f"{field} must contain valid JSON") from error
        if not isinstance(decoded, list):
            raise MarketParseError(f"{field} must decode to an array")
        arrays.append(decoded)
    outcomes, tokens, prices = arrays
    if not outcomes or len(outcomes) != len(tokens) or len(tokens) != len(prices):
        raise MarketParseError("market outcomes, tokens, and prices must align")
    yes_indexes = [
        index
        for index, outcome in enumerate(outcomes)
        if isinstance(outcome, str) and outcome.strip().casefold() == "yes"
    ]
    if len(yes_indexes) != 1:
        raise MarketParseError("market must contain exactly one Yes outcome")
    yes_index = yes_indexes[0]
    yes_token = tokens[yes_index]
    if not isinstance(yes_token, str) or not yes_token:
        raise MarketParseError("Yes outcome token must be a non-empty string")
    numeric_prices: list[float] = []
    for price in prices:
        try:
            numeric_price = float(price)
        except (TypeError, ValueError) as error:
            raise MarketParseError("outcome prices must be finite") from error
        if not math.isfinite(numeric_price):
            raise MarketParseError("outcome prices must be finite")
        numeric_prices.append(numeric_price)
    yes_price = numeric_prices[yes_index]
    liquidity = raw.get("liquidityNum")
    if (
        isinstance(liquidity, bool)
        or not isinstance(liquidity, (int, float))
        or not math.isfinite(float(liquidity))
        or float(liquidity) < 0.0
    ):
        raise MarketParseError("liquidityNum must be a finite non-negative number")
    question = raw.get("question")
    title = raw.get("groupItemTitle")
    if not isinstance(question, str) or not question:
        raise MarketParseError("question must be a non-empty string")
    if not isinstance(title, str) or not title:
        raise MarketParseError("groupItemTitle must be a non-empty string")
    return ParsedMarket(
        question=question,
        title=title,
        yes_token=yes_token,
        gamma_yes_price=yes_price,
        liquidity_num=float(liquidity),
        raw=raw,
    )
