"""Daily adapter for rolling the pinned fed-path research configuration forward."""

from __future__ import annotations

import csv
import io
import json
import math
import os
import secrets
import sys
import argparse
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Mapping
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from .fed_path import compute_fed_path, compute_meeting_distribution
from .fed_path_client import FedPathClient, FedPathFetchError
from .fed_path_config import load_fed_path_config
from .fed_path_models import FedPathConfig
from .fed_path_reporting import write_fed_path_failure, write_fed_path_success


EFFR_URL = "https://markets.newyorkfed.org/api/rates/all/latest.json"


@dataclass(frozen=True)
class EffrObservation:
    """Latest official effective federal funds rate and target range."""

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
    """Extract and validate the EFFR row from the New York Fed latest-rates response."""
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


def build_daily_config(base: FedPathConfig, observation: EffrObservation, *, as_of: date) -> FedPathConfig:
    """Apply current policy baselines and retain meetings that have not yet concluded."""
    if observation.effective_date > as_of:
        raise ValueError("EFFR observation cannot be in the future")
    return replace(
        base,
        target_upper_bound=observation.target_to,
        effective_rate_baseline=observation.rate,
        meetings=tuple(meeting for meeting in base.meetings if meeting.date >= as_of),
    )


_HISTORY_FIELDS = (
    "run_id", "generated_at", "snapshot_fetched_at", "target_upper_bound_baseline",
    "effective_rate_baseline", "point_date", "point_kind", "implied_change_bp",
    "cumulative_change_bp", "implied_target_upper", "implied_effective_rate",
)


def _history_rows(output_dir: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for result_path in sorted((output_dir / "runs").glob("*/fed_path.json")):
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("points"), list):
            raise ValueError(f"fed-path history source is malformed: {result_path}")
        common = {
            "run_id": result_path.parent.name,
            "generated_at": payload.get("generated_at"),
            "snapshot_fetched_at": payload.get("snapshot_fetched_at"),
            "target_upper_bound_baseline": payload.get("target_upper_bound_baseline"),
            "effective_rate_baseline": payload.get("effective_rate_baseline"),
        }
        for point in payload["points"]:
            if not isinstance(point, dict):
                raise ValueError(f"fed-path point is malformed: {result_path}")
            rows.append(common | {
                "point_date": point.get("date"),
                "point_kind": point.get("kind"),
                "implied_change_bp": point.get("implied_change_bp"),
                "cumulative_change_bp": point.get("cumulative_change_bp"),
                "implied_target_upper": point.get("implied_target_upper"),
                "implied_effective_rate": point.get("implied_effective_rate"),
            })
    rows.sort(key=lambda row: (str(row["generated_at"]), str(row["run_id"]), str(row["point_date"])))
    return rows


def rebuild_history(output_dir: Path) -> Path:
    """Atomically rebuild a long-form CSV index from immutable successful runs."""
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=_HISTORY_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(_history_rows(root))
    target = root / "history.csv"
    temporary = root / f".history.{secrets.token_hex(4)}.tmp"
    try:
        with temporary.open("x", encoding="utf-8", newline="") as handle:
            handle.write(buffer.getvalue())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return target


def fetch_latest_effr_payload() -> object:
    """Fetch the New York Fed latest reference-rate payload."""
    request = Request(EFFR_URL, headers={"Accept": "application/json", "User-Agent": "fed-forecast/0.1"})
    with urlopen(request, timeout=30) as response:  # noqa: S310 - fixed HTTPS endpoint
        body = response.read(1_000_001)
    if len(body) > 1_000_000:
        raise ValueError("EFFR response exceeds one megabyte")
    return json.loads(body)


def run_daily_fed_path(
    config_path: Path,
    output_dir: Path,
    *,
    now: datetime | None = None,
    effr_fetcher: Callable[[], object] = fetch_latest_effr_payload,
    client_factory: Callable[[], FedPathClient] = FedPathClient,
) -> Path:
    """Run one rolling live forecast and refresh its long-form history."""
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        raise ValueError("daily run time must include a UTC offset")
    base = load_fed_path_config(Path(config_path))
    observation = parse_latest_effr(effr_fetcher())
    as_of = moment.astimezone(ZoneInfo("America/Los_Angeles")).date()
    config = build_daily_config(base, observation, as_of=as_of)
    snapshot = None
    try:
        snapshot = client_factory().fetch_snapshot(config)
        distributions = tuple(
            compute_meeting_distribution(
                meeting,
                tuple(price for price in snapshot.meeting_prices if price.meeting_date == meeting.date),
            )
            for meeting in config.meetings
        )
        generated_at = moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        result = compute_fed_path(
            config,
            distributions,
            snapshot.terminal_prices,
            generated_at=generated_at,
            snapshot_fetched_at=snapshot.fetched_at,
            diagnostics=snapshot.diagnostics,
        )
        run_dir = write_fed_path_success(output_dir, snapshot, result, now=moment)
    except FedPathFetchError as error:
        write_fed_path_failure(output_dir, error.partial_snapshot, error, now=moment)
        raise
    except Exception as error:
        if snapshot is not None:
            write_fed_path_failure(output_dir, snapshot, error, now=moment)
        raise
    rebuild_history(output_dir)
    return run_dir


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run and archive the rolling daily Polymarket fed path.")
    parser.add_argument("--config", type=Path, default=Path("config/fed_path.json"))
    parser.add_argument("--output-dir", type=Path, default=Path("outputs/fed-path-history"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        run_dir = run_daily_fed_path(args.config, args.output_dir)
    except Exception as error:
        print(f"daily fed-path failed: {error}", file=sys.stderr)
        return 2
    print(f"Daily fed-path run: {run_dir.resolve()}")
    print(f"History CSV: {(args.output_dir / 'history.csv').resolve()}")
    return 0
