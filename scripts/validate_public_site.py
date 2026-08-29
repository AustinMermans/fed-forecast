#!/usr/bin/env python3
"""Fail closed when the generated public rate-path bundle is incomplete."""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime
from pathlib import Path


def _load(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def validate(site: Path) -> None:
    required = (
        site / "index.html",
        site / "assets" / "styles.css",
        site / "assets" / "dashboard.js",
        site / "data" / "dashboard.json",
        site / "data" / "forecast-replay.json",
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise ValueError(f"missing public files: {missing}")

    dashboard = _load(site / "data" / "dashboard.json")
    replay = _load(site / "data" / "forecast-replay.json")
    policy = dashboard.get("policy")
    if not isinstance(policy, dict):
        raise ValueError("dashboard policy is missing")
    meetings = policy.get("meetings")
    if not isinstance(meetings, list) or len(meetings) < 2:
        raise ValueError("current forecast requires at least two future meetings")
    dates = [str(item.get("date")) for item in meetings if isinstance(item, dict)]
    if dates != sorted(dates) or len(set(dates)) != len(dates):
        raise ValueError("meeting dates must be unique and chronological")
    urls = policy.get("source_urls")
    if not isinstance(urls, dict):
        raise ValueError("source URLs are missing")
    for meeting in meetings:
        if not isinstance(meeting, dict):
            raise ValueError("meeting row must be an object")
        prices = meeting.get("prices")
        if not isinstance(prices, list) or len(prices) != 5:
            raise ValueError(f"{meeting.get('date')} does not have five outcomes")
        total = sum(float(item["probability"]) for item in prices if isinstance(item, dict))
        if not math.isclose(total, 1.0, abs_tol=1e-9):
            raise ValueError(f"{meeting.get('date')} probabilities sum to {total}")
        slug = meeting.get("event_slug")
        if not isinstance(slug, str) or not str(urls.get(slug, "")).startswith("https://polymarket.com/event/"):
            raise ValueError(f"{meeting.get('date')} has no direct market link")

    vintages = replay.get("vintages")
    if not isinstance(vintages, list) or len(vintages) < 30:
        raise ValueError("replay archive is unexpectedly short")
    stamps = [datetime.fromisoformat(str(row["generated_at"]).replace("Z", "+00:00")) for row in vintages if isinstance(row, dict)]
    if stamps != sorted(stamps):
        raise ValueError("replay timestamps are not chronological")
    if replay.get("window_days") != 183:
        raise ValueError("replay must retain a fixed 183-day horizon")
    for vintage in vintages:
        if not isinstance(vintage, dict):
            raise ValueError("replay vintage must be an object")
        if vintage.get("kind") == "event_checkpoint":
            raise ValueError("unprovenanced event checkpoints are not publishable")
        if vintage.get("kind") != "historical_daily":
            continue
        frame_time = datetime.fromisoformat(str(vintage["generated_at"]).replace("Z", "+00:00")).timestamp()
        for meeting in vintage.get("meetings", []):
            if not isinstance(meeting, dict):
                raise ValueError("historical meeting must be an object")
            source_time = meeting.get("source_timestamp")
            age = meeting.get("quote_age_seconds")
            if not isinstance(source_time, (int, float)) or not isinstance(age, (int, float)):
                raise ValueError("historical meeting lacks quote provenance")
            if source_time > frame_time + 1 or age < 0 or age > 7 * 86400:
                raise ValueError("historical quote violates information-set or staleness rules")
            if meeting.get("quote_status") != "reconstructed_daily" or meeting.get("source_kind") != "polymarket_clob_price_history":
                raise ValueError("historical reconstruction status is missing")

    for path in site.rglob("*"):
        if path.is_file() and path.suffix in {".html", ".js", ".json"}:
            text = path.read_text(encoding="utf-8")
            if "/Users/" in text or "/home/runner/" in text or "curve_forecaster" in text:
                raise ValueError(f"local or legacy reference leaked into {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("site", type=Path, nargs="?", default=Path("site"))
    args = parser.parse_args()
    validate(args.site.resolve())
    print(f"validated {args.site.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
