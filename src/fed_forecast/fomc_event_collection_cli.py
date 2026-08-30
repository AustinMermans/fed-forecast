"""CLI for granular, collection-only FOMC event observations."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .fomc_event_collection import (
    EventCollectionError, EventSlot, infer_event_slot, load_event_calendar,
    event_slots, sha256_file, validate_runner_lateness,
)
from .fomc_event_collection_reporting import archive_observation, audit_archive, verify_run_directory
from .fomc_event_observer import FomcEventObserver, load_observation_topology


def _timestamp(value: str, name: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise EventCollectionError(f"{name} must be RFC3339") from error
    if result.tzinfo is None:
        raise EventCollectionError(f"{name} must include an offset")
    return result


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _commit_sha(value: str) -> str:
    if len(value) != 40 or any(item not in "0123456789abcdef" for item in value):
        raise EventCollectionError("code commit SHA is invalid")
    return value


def _write_github_output(path: Path | None, values: dict[str, str]) -> None:
    if path is None:
        return
    with path.open("a", encoding="utf-8") as stream:
        for key, value in values.items():
            if "\n" in value or "\r" in value:
                raise EventCollectionError("GitHub output values must be one line")
            stream.write(f"{key}={value}\n")


def _config_provenance(calendar: Path, markets: Path) -> dict[str, str]:
    return {
        "calendar_sha256": sha256_file(calendar),
        "markets_config_sha256": sha256_file(markets),
        "fed_path_config_sha256": sha256_file(markets.parent / "fed_path.json"),
    }


def _gate(args: argparse.Namespace) -> int:
    calendar = load_event_calendar(args.calendar, args.markets)
    run_created = _timestamp(args.run_created_at, "run_created_at")
    actual_start = _timestamp(args.actual_start_at, "actual_start_at") if args.actual_start_at else _now()
    slot = infer_event_slot(calendar, run_created)
    eligible = slot is not None
    lateness: float | None = None
    if slot is not None:
        lateness = validate_runner_lateness(calendar, slot, actual_start)
    payload = {
        "eligible": eligible,
        "event_id": slot.meeting.event_id if slot else None,
        "slot": slot.scheduled_at.isoformat() if slot else None,
        "slot_key": slot.slot_key if slot else None,
        "phase": slot.phase if slot else None,
        "runner_lateness_seconds": lateness,
    }
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    _write_github_output(args.github_output, {
        "eligible": str(eligible).lower(),
        "event_id": slot.meeting.event_id if slot else "",
        "slot": slot.scheduled_at.isoformat() if slot else "",
        "slot_key": slot.slot_key if slot else "",
        "phase": slot.phase if slot else "",
    })
    return 0


def _collect(args: argparse.Namespace) -> int:
    calendar = load_event_calendar(args.calendar, args.markets)
    run_created = _timestamp(args.run_created_at, "run_created_at")
    actual_start = _timestamp(args.actual_start_at, "actual_start_at") if args.actual_start_at else _now()
    slot = infer_event_slot(calendar, run_created)
    if slot is None:
        raise EventCollectionError("scheduled collection is outside the reviewed event gate")
    validate_runner_lateness(calendar, slot, actual_start)
    github = {
        "repository": args.repository,
        "workflow": args.workflow,
        "run_id": args.github_run_id,
        "run_attempt": args.github_run_attempt,
        "run_number": args.github_run_number,
        "head_sha": args.head_sha,
        "ref": args.ref,
        "event_name": args.event_name,
        "cron": args.cron,
    }
    provenance = _config_provenance(args.calendar, args.markets) | {"code_commit_sha": _commit_sha(args.head_sha)}
    topology = load_observation_topology(args.markets)
    observation = FomcEventObserver().collect(topology, markets_config_path=args.markets)
    run_id = f"{slot.scheduled_at.astimezone(timezone.utc).strftime('%Y%m%dT%H%MZ')}-{args.github_run_id}-{args.github_run_attempt}"
    result = archive_observation(
        args.archive, observation, slot, topology, run_id=run_id, run_created_at=run_created,
        actual_start_at=actual_start, github=github, provenance=provenance,
    )
    event_root = args.archive / slot.meeting.event_id
    run_relative = result.run_directory.relative_to(event_root).as_posix()
    pointer_relative = result.pointer_path.relative_to(event_root).as_posix()
    _write_github_output(args.github_output, {
        "status": result.status,
        "run_path": run_relative,
        "pointer_path": pointer_relative,
    })
    print(json.dumps({
        "status": result.status,
        "event_id": slot.meeting.event_id,
        "slot": slot.scheduled_at.isoformat(),
        "run_directory": str(result.run_directory),
        "pointer_path": str(result.pointer_path),
    }, sort_keys=True, separators=(",", ":")))
    return 0


def _verify(args: argparse.Namespace) -> int:
    topology = load_observation_topology(args.markets)
    calendar = load_event_calendar(args.calendar, args.markets)
    provenance = _config_provenance(args.calendar, args.markets)
    if args.run_directory:
        if not args.event_id or not args.slot_key:
            raise EventCollectionError("run verification requires --event-id and --slot-key")
        meeting = next((item for item in calendar.meetings if item.event_id == args.event_id), None)
        slot = next((item for item in event_slots(calendar, meeting) if item.slot_key == args.slot_key), None) if meeting else None
        if slot is None:
            raise EventCollectionError("run verification slot is outside the reviewed calendar")
        payload = verify_run_directory(args.run_directory, topology, expected_slot=slot, expected_provenance=provenance)
    elif args.event_id:
        payload = audit_archive(args.archive, args.event_id, topology, calendar, expected_provenance=provenance)
    else:
        raise EventCollectionError("verify requires --run-directory or --event-id")
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect granular FOMC event-window market observations without running the forecast model.")
    commands = parser.add_subparsers(dest="operation", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--calendar", type=Path, default=Path("config/fomc_event_collection.json"))
    common.add_argument("--markets", type=Path, default=Path("config/markets.json"))
    gate = commands.add_parser("gate", parents=[common])
    gate.add_argument("--run-created-at", required=True)
    gate.add_argument("--actual-start-at")
    gate.add_argument("--github-output", type=Path)
    collect = commands.add_parser("collect", parents=[common])
    collect.add_argument("--archive", type=Path, required=True)
    collect.add_argument("--run-created-at", required=True)
    collect.add_argument("--actual-start-at")
    collect.add_argument("--repository", required=True)
    collect.add_argument("--workflow", required=True)
    collect.add_argument("--github-run-id", required=True)
    collect.add_argument("--github-run-attempt", type=int, required=True)
    collect.add_argument("--github-run-number", type=int, required=True)
    collect.add_argument("--head-sha", required=True)
    collect.add_argument("--ref", required=True)
    collect.add_argument("--event-name", required=True)
    collect.add_argument("--cron", required=True)
    collect.add_argument("--github-output", type=Path)
    verify = commands.add_parser("verify")
    verify.add_argument("--calendar", type=Path, default=Path("config/fomc_event_collection.json"))
    verify.add_argument("--markets", type=Path, default=Path("config/markets.json"))
    verify.add_argument("--archive", type=Path, default=Path("fomc-events"))
    verify.add_argument("--run-directory", type=Path)
    verify.add_argument("--event-id")
    verify.add_argument("--slot-key")
    return parser


def fomc_event_collection_main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.operation == "gate":
            return _gate(args)
        if args.operation == "collect":
            return _collect(args)
        if args.operation == "verify":
            return _verify(args)
    except Exception as error:
        print(f"FOMC event collection failed: {error}", file=sys.stderr)
        return 2
    return 2
