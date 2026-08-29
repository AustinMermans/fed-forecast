"""Command-line entry point for Fed Forecast."""

from __future__ import annotations

import argparse

from .meeting_scenarios_cli import meeting_scenarios_main
from .historical_transitions_cli import historical_transitions_main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fed-forecast")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("refresh", help="collect and archive the current Fed path")
    commands.add_parser("historical-transitions", help="run or verify the inactive transition diagnostic")
    args, remainder = parser.parse_known_args(argv)
    if args.command == "refresh":
        return meeting_scenarios_main(remainder)
    if args.command == "historical-transitions":
        return historical_transitions_main(remainder)
    return 2


raise SystemExit(main())
