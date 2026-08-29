#!/usr/bin/env python3
"""Fail closed when the generated public rate-path bundle is incomplete."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
from fed_forecast.historical_transitions_evidence import validate_evidence_summary  # noqa: E402


def _load(path: Path) -> dict[str, object]:
    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in values:
            if key in result:
                raise ValueError(f"duplicate JSON key in {path}: {key}")
            result[key] = value
        return result
    value = json.loads(path.read_bytes(), object_pairs_hook=pairs, parse_constant=lambda item: (_ for _ in ()).throw(ValueError(f"non-finite JSON value in {path}: {item}")))
    def finite(item: object) -> None:
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError(f"non-finite JSON value in {path}")
        if isinstance(item, dict):
            for nested in item.values(): finite(nested)
        elif isinstance(item, list):
            for nested in item: finite(nested)
    finite(value)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def validate(site: Path) -> None:
    required = (
        site / "index.html",
        site / "methodology.html",
        site / "assets" / "styles.css",
        site / "assets" / "dashboard.js",
        site / "assets" / "methodology.js",
        site / "assets" / "methodology.css",
        site / "assets" / "branch-decomposition.js",
        site / "data" / "dashboard.json",
        site / "data" / "forecast-replay.json",
        site / "data" / "evidence-summary.json",
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise ValueError(f"missing public files: {missing}")

    dashboard = _load(site / "data" / "dashboard.json")
    replay = _load(site / "data" / "forecast-replay.json")
    evidence_path = site / "data" / "evidence-summary.json"
    evidence_bytes = evidence_path.read_bytes()
    if len(evidence_bytes) > 16 * 1024:
        raise ValueError("public evidence summary exceeds 16 KiB")
    evidence = _load(evidence_path)
    validate_evidence_summary(evidence)
    contract = dashboard.get("evidence_summary")
    expected_contract = {
        "url": "data/evidence-summary.json",
        "schema_version": 2,
        "sha256": hashlib.sha256(evidence_bytes).hexdigest(),
        "generated_at": evidence["summary_generated_at"],
        "legacy_model_sha256": evidence["legacy_canonical_15_30"]["provenance"]["model_sha256"],
        "legacy_cutoff_at": evidence["legacy_canonical_15_30"]["provenance"]["data_cutoff_at"],
    }
    if not isinstance(contract, dict) or contract != expected_contract:
        raise ValueError("dashboard evidence contract is stale or malformed")
    if (site / "data" / "dashboard.json").stat().st_size >= 1024 * 1024:
        raise ValueError("dashboard.json exceeds 1 MiB")
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
    terminal = policy.get("terminal_anchor")
    if not isinstance(terminal, dict) or not isinstance(terminal.get("quote_quality"), dict):
        raise ValueError("year-end rate market quality is missing")
    tree = policy.get("tree")
    if not isinstance(tree, dict) or not isinstance(tree.get("nodes"), list):
        raise ValueError("current conditional tree is missing")
    for node in tree["nodes"]:
        distribution = node.get("rate_distribution") if isinstance(node, dict) else None
        if not isinstance(distribution, list) or not distribution:
            raise ValueError("conditional node rate distribution is missing")
        if not math.isclose(sum(float(item["probability"]) for item in distribution), 1.0, abs_tol=1e-9):
            raise ValueError("conditional node rate distribution does not sum to one")

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
