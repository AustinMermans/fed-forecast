#!/usr/bin/env python3
"""Validate one granular FOMC event archive."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from fed_forecast.fomc_event_collection_reporting import audit_archive
from fed_forecast.fomc_event_collection import load_event_calendar, sha256_file
from fed_forecast.fomc_event_observer import load_observation_topology


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("event_id")
    parser.add_argument("--markets", type=Path, default=Path("config/markets.json"))
    parser.add_argument("--calendar", type=Path, default=Path("config/fomc_event_collection.json"))
    args = parser.parse_args()
    provenance = {
        "calendar_sha256": sha256_file(args.calendar),
        "markets_config_sha256": sha256_file(args.markets),
        "fed_path_config_sha256": sha256_file(args.markets.parent / "fed_path.json"),
    }
    print(json.dumps(audit_archive(
        args.archive, args.event_id, load_observation_topology(args.markets),
        load_event_calendar(args.calendar, args.markets), expected_provenance=provenance,
    ), sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
