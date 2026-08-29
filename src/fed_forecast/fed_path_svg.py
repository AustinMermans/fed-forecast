"""Deterministic, self-contained SVG for the Polymarket fed-path report."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from html import escape

from .fed_path_models import FedPathResult


_WIDTH = 960
_HEIGHT = 540
_LEFT = 74
_RIGHT = 170
_TOP = 70
_BOTTOM = 96
_PLOT_WIDTH = _WIDTH - _LEFT - _RIGHT
_PLOT_HEIGHT = _HEIGHT - _TOP - _BOTTOM


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _points(payload: Mapping[str, object]) -> list[Mapping[str, object]]:
    value = payload.get("points")
    if not isinstance(value, list) or not value:
        raise ValueError("fed-path result must contain points")
    if not all(isinstance(item, Mapping) for item in value):
        raise TypeError("fed-path points must be objects")
    return list(value)


def _scale(values: Sequence[float]) -> tuple[float, float]:
    low = min(values)
    high = max(values)
    span = high - low
    padding = max(span * 0.10, 0.05)
    return low - padding, high + padding


def _x(index: int, count: int) -> float:
    return _LEFT + (_PLOT_WIDTH * index / max(count - 1, 1))


def _y(value: float, low: float, high: float) -> float:
    return _TOP + _PLOT_HEIGHT * (high - value) / (high - low)


def _polyline(points: Sequence[tuple[float, float]]) -> str:
    return " ".join(f"{x:.3f},{y:.3f}" for x, y in points)


def render_fed_path_svg_payload(payload: Mapping[str, object]) -> bytes:
    """Render an SVG from the explicit ``FedPathResult.to_dict()`` payload."""
    points = _points(payload)
    standard_move_bp = _number(payload.get("standard_move_bp"), "standard move")
    dates: list[str] = []
    effective: list[float] = []
    cumulative: list[float] = []
    wirp: list[float | None] = []
    for item in points:
        date = item.get("date")
        if not isinstance(date, str) or not date.startswith("2026-"):
            raise ValueError("SVG only accepts 2026 Polymarket points")
        dates.append(date)
        effective.append(_number(item.get("implied_effective_rate"), "implied effective rate"))
        cumulative.append(_number(item.get("cumulative_moves"), "cumulative moves"))
        reference = item.get("wirp_implied_rate")
        wirp.append(None if reference is None else _number(reference, "WIRP implied rate"))

    rate_low, rate_high = _scale([*effective, *(item for item in wirp if item is not None)])
    moves_low, moves_high = _scale([*cumulative, 0.0])
    positions = [_x(index, len(points)) for index in range(len(points))]
    effective_line = _polyline([(x, _y(value, rate_low, rate_high)) for x, value in zip(positions, effective, strict=True)])
    wirp_points = [(x, _y(value, rate_low, rate_high)) for x, value in zip(positions, wirp, strict=True) if value is not None]
    diagnostics = payload.get("diagnostics", [])
    messages = []
    if isinstance(diagnostics, list):
        for item in diagnostics:
            if isinstance(item, Mapping) and isinstance(item.get("message"), str):
                messages.append(item["message"])
    description = (
        "Polymarket implied effective-rate path in blue, cumulative expected "
        "quarter-point moves in orange, and supplied 2026 WIRP implied rates "
        "in gray dashes. " + " ".join(messages)
    )
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_WIDTH}" height="{_HEIGHT}" viewBox="0 0 {_WIDTH} {_HEIGHT}" role="img" aria-labelledby="title desc">',
        "<title id=\"title\">Polymarket expected federal-funds path</title>",
        f"<desc id=\"desc\">{escape(description)}</desc>",
        '<rect width="100%" height="100%" fill="white"/>',
        f'<text x="{_LEFT}" y="30" font-family="system-ui, sans-serif" font-size="20" font-weight="600">Polymarket expected federal-funds path</text>',
        f'<text x="{_LEFT}" y="52" font-family="system-ui, sans-serif" font-size="12" fill="#555">Blue: implied effective rate · Orange: cumulative {standard_move_bp:g} bp moves · Gray dashed: WIRP</text>',
        f'<line x1="{_LEFT}" y1="{_TOP + _PLOT_HEIGHT}" x2="{_LEFT + _PLOT_WIDTH}" y2="{_TOP + _PLOT_HEIGHT}" stroke="#333"/>',
        f'<line x1="{_LEFT}" y1="{_TOP}" x2="{_LEFT}" y2="{_TOP + _PLOT_HEIGHT}" stroke="#333"/>',
        f'<line x1="{_LEFT + _PLOT_WIDTH}" y1="{_TOP}" x2="{_LEFT + _PLOT_WIDTH}" y2="{_TOP + _PLOT_HEIGHT}" stroke="#333"/>',
        f'<text x="{_WIDTH - 8}" y="{_TOP - 10}" text-anchor="end" font-family="system-ui, sans-serif" font-size="11" fill="#8a4516">Cumulative moves ({standard_move_bp:g} bp)</text>',
    ]
    zero_y = _y(0.0, moves_low, moves_high)
    lines.extend((
        f'<line x1="{_LEFT}" y1="{zero_y:.3f}" x2="{_LEFT + _PLOT_WIDTH}" y2="{zero_y:.3f}" stroke="#8a4516" stroke-width="1" stroke-dasharray="3 3"/>',
        f'<text x="{_WIDTH - 8}" y="{zero_y + 4:.3f}" text-anchor="end" font-family="system-ui, sans-serif" font-size="11" fill="#8a4516">0 moves</text>',
    ))
    bar_width = min(62.0, _PLOT_WIDTH / max(2 * len(points), 1))
    for index, (x, value, date) in enumerate(zip(positions, cumulative, dates, strict=True)):
        value_y = _y(value, moves_low, moves_high)
        top = min(zero_y, value_y)
        height = abs(zero_y - value_y)
        lines.extend((
            f'<rect x="{x - bar_width / 2:.3f}" y="{top:.3f}" width="{bar_width:.3f}" height="{height:.3f}" fill="#ed7d31" opacity="0.72"/>',
            f'<text x="{x:.3f}" y="{_TOP + _PLOT_HEIGHT + 22}" text-anchor="middle" font-family="system-ui, sans-serif" font-size="11">{escape(date)}</text>',
            f'<text x="{x:.3f}" y="{_TOP + _PLOT_HEIGHT + 39}" text-anchor="middle" font-family="system-ui, sans-serif" font-size="10" fill="#8a4516">{value:+.3f} moves</text>',
        ))
    for label, value in ((f"{rate_high:.3f}%", rate_high), (f"{rate_low:.3f}%", rate_low)):
        lines.append(f'<text x="{_LEFT - 8}" y="{_y(value, rate_low, rate_high) + 4:.3f}" text-anchor="end" font-family="system-ui, sans-serif" font-size="11">{label}</text>')
    for label, value in ((f"{moves_high:+.3f}", moves_high), (f"{moves_low:+.3f}", moves_low)):
        lines.append(f'<text x="{_WIDTH - 8}" y="{_y(value, moves_low, moves_high) + 4:.3f}" text-anchor="end" font-family="system-ui, sans-serif" font-size="11" fill="#8a4516">{label}</text>')
    lines.append(f'<polyline points="{effective_line}" fill="none" stroke="#1565c0" stroke-width="3"/>')
    for x, value in zip(positions, effective, strict=True):
        lines.append(f'<circle cx="{x:.3f}" cy="{_y(value, rate_low, rate_high):.3f}" r="4" fill="#1565c0"/>')
    if wirp_points:
        lines.append(f'<polyline points="{_polyline(wirp_points)}" fill="none" stroke="#808080" stroke-width="2" stroke-dasharray="7 5"/>')
    lines.append("</svg>")
    return ("\n".join(lines) + "\n").encode("utf-8")


def render_fed_path_svg(result: FedPathResult) -> bytes:
    """Render the deterministic accessible SVG for one fed-path calculation."""
    payload = result.to_dict()
    if not isinstance(payload, Mapping):
        raise TypeError("FedPathResult.to_dict() must return an object")
    return render_fed_path_svg_payload(payload)
