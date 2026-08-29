"""Small New York Fed EFFR adapter used by the live forecast refresh."""

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from urllib.request import Request, urlopen


EFFR_URL = "https://markets.newyorkfed.org/api/rates/all/latest.json"


@dataclass(frozen=True)
class EffrObservation:
    effective_date: date
    rate: float
    target_from: float
    target_to: float


def _finite_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"EFFR {label} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"EFFR {label} must be a finite number")
    return number


def parse_latest_effr(payload: object) -> EffrObservation:
    """Extract and validate the EFFR row from the latest-rates response."""
    if not isinstance(payload, Mapping) or not isinstance(payload.get("refRates"), list):
        raise ValueError("EFFR response must contain a refRates array")
    rows = [row for row in payload["refRates"] if isinstance(row, Mapping) and row.get("type") == "EFFR"]
    if len(rows) != 1:
        raise ValueError("EFFR response must contain exactly one EFFR row")
    row = rows[0]
    try:
        effective_date = date.fromisoformat(row["effectiveDate"])  # type: ignore[arg-type]
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("EFFR effectiveDate must be an ISO date") from error
    rate = _finite_number(row.get("percentRate"), "percentRate")
    target_from = _finite_number(row.get("targetRateFrom"), "targetRateFrom")
    target_to = _finite_number(row.get("targetRateTo"), "targetRateTo")
    if not target_from <= rate <= target_to:
        raise ValueError("EFFR percentRate must fall within the target range")
    return EffrObservation(effective_date, rate, target_from, target_to)


def fetch_latest_effr_payload() -> object:
    """Fetch the latest official reference-rate payload from the New York Fed."""
    request = Request(EFFR_URL, headers={"Accept": "application/json", "User-Agent": "fed-forecast/0.1"})
    with urlopen(request, timeout=30) as response:  # noqa: S310 - fixed HTTPS endpoint
        body = response.read(1_000_001)
    if len(body) > 1_000_000:
        raise ValueError("EFFR response exceeds one megabyte")
    return json.loads(body)
