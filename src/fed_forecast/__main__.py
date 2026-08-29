"""Command-line entry point for Fed Forecast."""

from __future__ import annotations

import argparse

from .meeting_scenarios_cli import meeting_scenarios_main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fed-forecast")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("refresh", help="collect and archive the current Fed path")
    args, remainder = parser.parse_known_args(argv)
    if args.command == "refresh":
        return meeting_scenarios_main(remainder)
    return 2


raise SystemExit(main())
