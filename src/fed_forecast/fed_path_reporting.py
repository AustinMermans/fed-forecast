"""Immutable artifacts and human-readable reporting for a Polymarket fed path."""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Mapping
import ctypes
from datetime import datetime, timezone
import errno
from pathlib import Path
import re
import secrets
import shutil
import sys
import warnings

from .fed_path_client import FedPathSnapshot
from .fed_path_models import FedPathResult
from .fed_path_svg import render_fed_path_svg_payload


DISCLAIMER = (
    "This is a Polymarket expected-value path from separately traded meeting "
    "outcomes, not a fed-funds futures curve or a joint policy-path "
    "distribution."
)


def _portable(value: object) -> object:
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON output contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        output: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, (str, int)):
                raise TypeError("JSON object keys must be strings or integers")
            name = str(key)
            if name in output:
                raise ValueError(f"JSON object key collision after conversion: {name}")
            output[name] = _portable(item)
        return output
    if isinstance(value, (tuple, list)):
        return [_portable(item) for item in value]
    raise TypeError(f"value of type {type(value).__name__} is not JSON portable")


def _json_bytes(payload: object) -> bytes:
    return (json.dumps(_portable(payload), allow_nan=False, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _run_id(now: datetime | None, suffix: str | None) -> str:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        raise ValueError("run time must include a UTC offset")
    timestamp = moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return f"{timestamp}-{suffix or secrets.token_hex(4)}"


def _write_file(path: Path, content: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _manifest(files: Mapping[str, bytes]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "files": {name: {"sha256": hashlib.sha256(content).hexdigest(), "size_bytes": len(content)} for name, content in files.items()},
    }


def _rename_exclusive(staging: Path, final: Path) -> None:
    """Atomically publish a directory only if its final name is still absent.

    A conventional ``os.replace`` has replace semantics and can therefore
    clobber another publisher between an existence check and the rename.  The
    platform primitives below provide no-replace semantics; unsupported
    platforms fail closed rather than risking an overwrite.
    """
    library = ctypes.CDLL(None, use_errno=True)
    source = os.fsencode(staging)
    destination = os.fsencode(final)
    if sys.platform == "darwin":
        function = getattr(library, "renameatx_np", None)
        if function is not None:
            function.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
            function.restype = ctypes.c_int
            result = function(-2, source, -2, destination, 0x00000004)  # AT_FDCWD, RENAME_EXCL
            if result == 0:
                return
    elif sys.platform.startswith("linux"):
        function = getattr(library, "renameat2", None)
        if function is not None:
            function.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
            function.restype = ctypes.c_int
            result = function(-100, source, -100, destination, 0x00000001)  # AT_FDCWD, RENAME_NOREPLACE
            if result == 0:
                return
    else:
        raise OSError(errno.ENOSYS, "exclusive directory rename is unsupported", str(final))
    failure = ctypes.get_errno()
    if failure == errno.EEXIST:
        raise FileExistsError(failure, os.strerror(failure), str(final))
    if failure == 0:
        failure = errno.ENOSYS
    raise OSError(failure, os.strerror(failure), str(final))


def _commit(output_dir: Path, run_name: str, files: Mapping[str, bytes]) -> Path:
    root = Path(output_dir)
    runs = root / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    staging = runs / f".{run_name}.tmp"
    final = runs / run_name
    if staging.exists() or final.exists():
        raise FileExistsError(f"run already exists: {run_name}")
    staging.mkdir()
    try:
        for name, content in files.items():
            _write_file(staging / name, content)
        _write_file(staging / "manifest.json", _json_bytes(_manifest(files)))
        _fsync_directory(staging)
        _rename_exclusive(staging, final)
        _fsync_directory(runs)
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return final


def _write_latest(output_dir: Path, run_path: str) -> bytes:
    candidate = Path(run_path)
    if (
        candidate.is_absolute() or "\\" in run_path or len(candidate.parts) != 2
        or candidate.parts[0] != "runs" or candidate.parts[1] in {"", ".", ".."}
        or candidate.parts[1].startswith(".")
    ):
        raise ValueError("run_path must be a safe runs/<run-id> relative path")
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    temporary = root / f".latest.{secrets.token_hex(4)}.tmp"
    content = _json_bytes({"schema_version": 1, "run_path": run_path})
    try:
        _write_file(temporary, content)
        os.replace(temporary, root / "latest.json")
        try:
            _fsync_directory(root)
        except OSError as error:
            try:
                warnings.warn(f"latest.json was published, but directory durability fsync failed: {error}", RuntimeWarning, stacklevel=2)
            except Exception:
                pass
    finally:
        if temporary.exists():
            temporary.unlink()
    return content


def _object(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError(f"{name}.to_dict() must return an object")
    return value


def _percent(value: object, places: int = 3) -> str:
    return f"{float(value):.{places}f}%"


def _render_report(snapshot: Mapping[str, object], result: Mapping[str, object]) -> str:
    points = result.get("points")
    distributions = result.get("meeting_distributions")
    terminal = result.get("terminal")
    diagnostics = result.get("diagnostics")
    selected = snapshot.get("selected_prices")
    wirp_rows = result.get("wirp_rows")
    if not isinstance(points, list) or not isinstance(distributions, list) or not isinstance(terminal, Mapping) or not isinstance(diagnostics, list) or not isinstance(selected, list) or not isinstance(wirp_rows, list):
        raise TypeError("fed-path payload is malformed")
    lines = [
        f"> {DISCLAIMER}", "", "# Polymarket federal-funds implied path", "",
        "## Provenance", "",
        f"- Snapshot fetched at: {result.get('snapshot_fetched_at')}",
        f"- Generated at: {result.get('generated_at')}",
        f"- WIRP reference image: `{snapshot.get('source_image')}`",
        f"- WIRP reference SHA-256: `{snapshot.get('source_sha256')}`", "",
        "## Assumptions and arithmetic", "",
        f"- Target-upper-bound baseline: {_percent(result.get('target_upper_bound_baseline'))}",
        f"- Effective-rate baseline: {_percent(result.get('effective_rate_baseline'))}",
        f"- Held-constant baseline spread: {_percent(result.get('baseline_spread'))}",
        f"- Standard move: {float(result.get('standard_move_bp')):.0f} bp.",
        "- % Hike/Cut = 100 × expected quarter-point moves; it is not a literal action probability.",
        "- Each meeting uses the normalized five-outcome distribution and its expected basis-point change.",
        "- Open-ended tails use tail-capped boundary representatives (±50 bp for meetings; 1.00% and 4.50% for terminal buckets).",
        "- December uses complete 15-bucket terminal normalization before its tail-capped expectation is calculated.",
        "- December is an independently traded terminal anchor, not a manufactured five-outcome meeting distribution.",
        "- Linearity of expectation does not make separately traded marginal markets a joint policy-path distribution.", "",
        "## WIRP-style path", "",
        "| Date | Type | % Hike/Cut | Cumulative moves | Implied effective rate |",
        "|---|---|---:|---:|---:|",
    ]
    for point in points:
        if not isinstance(point, Mapping):
            raise TypeError("point must be an object")
        lines.append(f"| {point['date']} | {point['kind']} | {float(point['incremental_moves']) * 100:+.3f}% | {float(point['cumulative_moves']):+.3f} | {_percent(point['implied_effective_rate'])} |")
    lines.extend(["", "## Meeting outcome probabilities", ""])
    for item in distributions:
        if not isinstance(item, Mapping):
            raise TypeError("meeting distribution must be an object")
        lines.extend((
            f"### {item['date']} — `{item['event_slug']}`", "",
            "| Outcome | Raw Yes price | Normalized probability | Representative change |",
            "|---|---:|---:|---:|",
        ))
        prices = item.get("prices")
        if not isinstance(prices, list):
            raise TypeError("meeting prices must be an array")
        for price in prices:
            if not isinstance(price, Mapping):
                raise TypeError("meeting price must be an object")
            lines.append(f"| {price['label']} | {float(price['raw_probability']):.6f} | {float(price['probability']):.3%} | {float(price['representative_bp']):+.0f} bp |")
        lines.extend((
            f"- Raw five-market total: {float(item['raw_total']):.9f}",
            f"- Decrease / no-change / increase: {float(item['decrease_probability']):.3%} / {float(item['no_change_probability']):.3%} / {float(item['increase_probability']):.3%}",
            f"- Negative / positive tail mass: {float(item['negative_tail_probability']):.3%} / {float(item['positive_tail_probability']):.3%}; tail_capped: {item['tail_capped']}", "",
        ))
    lines.extend(("## December terminal-anchor audit", "", f"- Event: `{terminal['event_slug']}`", f"- Raw 15-bucket total: {float(terminal['raw_total']):.9f}", f"- Expected target upper bound: {_percent(terminal['expected_target_upper'])}", f"- Effective-rate proxy: {_percent(terminal['effective_rate_proxy'])}", f"- Lower / upper tail probabilities: {float(terminal['lower_tail_probability']):.3%} / {float(terminal['upper_tail_probability']):.3%}; tail_capped: {terminal['tail_capped']}", "", "## WIRP comparison", "", "| Date | PM incremental moves | WIRP incremental moves | Δ incremental moves | PM cumulative moves | WIRP cumulative moves | Δ cumulative moves | WIRP implied-rate delta | PM implied rate | WIRP implied rate | Δ rate (bp) |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"))
    points_by_date: dict[str, Mapping[str, object]] = {}
    for point in points:
        if not isinstance(point, Mapping):
            raise TypeError("point must be an object")
        points_by_date[str(point["date"])] = point
    def move(value: object) -> str:
        return f"{float(value):+.3f}"
    for row in wirp_rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("date"), str):
            raise TypeError("WIRP row must be an object with a date")
        point = points_by_date.get(row["date"])
        if point is None:
            pm_incremental = pm_cumulative = pm_rate = incremental_delta = cumulative_delta = rate_delta = "unavailable"
        else:
            pm_incremental = move(point["incremental_moves"])
            pm_cumulative = move(point["cumulative_moves"])
            pm_rate = _percent(point["implied_effective_rate"])
            incremental_delta = move(float(point["incremental_moves"]) - float(row["incremental_moves"]))
            cumulative_delta = move(float(point["cumulative_moves"]) - float(row["cumulative_moves"]))
            rate_delta = f"{100 * (float(point['implied_effective_rate']) - float(row['implied_rate'])):+.3f}"
        lines.append(f"| {row['date']} | {pm_incremental} | {move(row['incremental_moves'])} | {incremental_delta} | {pm_cumulative} | {move(row['cumulative_moves'])} | {cumulative_delta} | {move(row['implied_rate_delta'])} | {pm_rate} | {_percent(row['implied_rate'])} | {rate_delta} |")
    lines.extend(("", "No comparable Polymarket meeting or terminal coverage is configured for 2027.", "", "## Price-quality audit", "", "| Source | Outcome/title | Question | Yes token | Price source | Quality | Price | Best bid | Best ask | Spread |", "|---|---|---|---|---|---|---:|---:|---:|---:|"))
    for item in selected:
        if not isinstance(item, Mapping):
            raise TypeError("selected price must be an object")
        def number_or_dash(value: object) -> str:
            return "—" if value is None else f"{float(value):.6f}"
        lines.append(f"| {item['source_id']} | {item['title']} | {item['question']} | `{item['yes_token']}` | {item['source']} | {item['quality']} | {float(item['price']):.6f} | {number_or_dash(item['best_bid'])} | {number_or_dash(item['best_ask'])} | {number_or_dash(item['spread'])} |")
    lines.extend(("", "## Diagnostics", ""))
    if diagnostics:
        for item in diagnostics:
            if not isinstance(item, Mapping):
                raise TypeError("diagnostic must be an object")
            lines.append(f"- **{item['severity']}** `{item['code']}` ({item['source_id'] or 'global'}): {item['message']}")
    else:
        lines.append("- None.")
    lines.extend(("", "## Analytical-support disclaimer", "", "This tool is analytical support, not investment advice.", ""))
    return "\n".join(lines)


def write_fed_path_success(output_dir: Path, snapshot: FedPathSnapshot, result: FedPathResult, *, now: datetime | None = None, suffix: str | None = None) -> Path:
    """Atomically publish one immutable successful fed-path run."""
    run_name = _run_id(now, suffix)
    snapshot_payload = _object(snapshot.to_dict(), "FedPathSnapshot")
    result_payload = _object(result.to_dict(), "FedPathResult")
    files = {
        "snapshot.json": _json_bytes(snapshot_payload),
        "fed_path.json": _json_bytes(result_payload),
        "report.md": _render_report(snapshot_payload, result_payload).encode("utf-8"),
        "fed_path.svg": render_fed_path_svg_payload(result_payload),
    }
    final = _commit(Path(output_dir), run_name, files)
    _write_latest(Path(output_dir), str(final.relative_to(Path(output_dir))))
    return final


def _failure_code(error: Exception) -> str:
    match = re.match(r"^([a-z][a-z0-9_]+)(?::|$)", str(error))
    return match.group(1) if match is not None else "run_failed"


def write_fed_path_failure(output_dir: Path, snapshot: FedPathSnapshot | None, error: Exception, *, now: datetime | None = None, suffix: str | None = None) -> Path | None:
    """Persist an available failed snapshot without publishing a latest pointer."""
    if snapshot is None:
        return None
    run_name = f"{_run_id(now, suffix)}-failed"
    files = {
        "snapshot.json": _json_bytes(_object(snapshot.to_dict(), "FedPathSnapshot")),
        "failure.json": _json_bytes({"error_type": type(error).__name__, "code": _failure_code(error), "message": str(error)}),
    }
    return _commit(Path(output_dir), run_name, files)
