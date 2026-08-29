"""Forward target-rate fan chart derived from the conditional meeting tree."""

from __future__ import annotations

import math
from collections.abc import Mapping
from html import escape


def _weighted_quantile(points: list[tuple[float, float]], probability: float) -> float:
    if not points or not 0.0 <= probability <= 1.0:
        raise ValueError("weighted quantile inputs are invalid")
    total = sum(weight for _, weight in points)
    if total <= 0:
        raise ValueError("weighted quantile requires positive mass")
    threshold = probability * total
    cumulative = 0.0
    for value, weight in sorted(points):
        cumulative += weight
        if cumulative + 1e-15 >= threshold:
            return value
    return max(value for value, _ in points)


def _fan_distributions(payload: Mapping[str, object]) -> list[dict[str, object]]:
    meetings = payload.get("meetings")
    tree = payload.get("conditional_tree")
    if not isinstance(meetings, list) or not isinstance(tree, Mapping):
        raise ValueError("meeting payload is incomplete")
    nodes = tree.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise ValueError("conditional tree nodes are missing")
    labels = ["Current"]
    for meeting in meetings:
        if not isinstance(meeting, Mapping):
            raise ValueError("meeting row is malformed")
        labels.append(str(meeting["date"]))

    output: list[dict[str, object]] = []
    for depth, label in enumerate(labels):
        points: list[tuple[float, float]] = []
        for node in nodes:
            if not isinstance(node, Mapping) or int(node.get("depth", -1)) != depth:
                continue
            path = node.get("realized_path")
            if not isinstance(path, list) or len(path) != depth:
                raise ValueError("conditional tree node path is invalid")
            rate = float(node["representative_target_upper"])
            points.append((rate, float(node["path_probability"])))
        if not points or not math.isclose(sum(weight for _, weight in points), 1.0, abs_tol=2e-9):
            raise ValueError("conditional tree depth does not sum to one")
        output.append({
            "label": label,
            "points": points,
            **{f"q{int(probability * 100):02d}": _weighted_quantile(points, probability) for probability in (.05, .10, .25, .50, .75, .90, .95)},
        })
    return output


def render_conditional_rate_fan_svg(payload: Mapping[str, object]) -> bytes:
    """Render weighted forward target-rate quantiles and all tree-node states."""
    distributions = _fan_distributions(payload)
    width, height = 1200, 720
    left, right, top, bottom = 105, 55, 105, 105
    plot_width, plot_height = width - left - right, height - top - bottom
    all_values = [value for item in distributions for value, _ in item["points"]]
    low = math.floor((min(all_values) - 0.20) * 4.0) / 4.0
    high = math.ceil((max(all_values) + 0.20) * 4.0) / 4.0
    xs = [left + index * plot_width / (len(distributions) - 1) for index in range(len(distributions))]

    def y(value: float) -> float:
        return top + plot_height * (high - value) / (high - low)

    def polygon(lower: str, upper: str) -> str:
        points = [(xs[index], y(float(item[lower]))) for index, item in enumerate(distributions)]
        points.extend((xs[index], y(float(distributions[index][upper]))) for index in reversed(range(len(distributions))))
        return " ".join(f"{x_value:.2f},{y_value:.2f}" for x_value, y_value in points)

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">',
        '<title id="title">Conditional Federal Reserve target-rate fan</title>',
        '<desc id="desc">A forward probability fan from the current target range through four meetings. Bands show weighted quantiles across the full conditional outcome tree.</desc>',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        '<text x="58" y="46" font-family="system-ui,sans-serif" font-size="28" font-weight="700" fill="#10243a">Conditional policy-rate fan</text>',
        f'<text x="58" y="76" font-family="system-ui,sans-serif" font-size="14" fill="#5d6b79">Target upper bound after realized meeting actions · {int(payload["conditional_tree"]["leaf_count"])} complete paths · market marginals preserved</text>',
    ]
    tick = math.ceil(low * 4) / 4
    while tick <= high + 1e-9:
        yy = y(tick)
        lines.extend((
            f'<line x1="{left}" y1="{yy:.2f}" x2="{width-right}" y2="{yy:.2f}" stroke="#d9e1e8" stroke-width="1"/>',
            f'<text x="{left-14}" y="{yy+5:.2f}" text-anchor="end" font-family="system-ui,sans-serif" font-size="12" fill="#5d6b79">{tick:.2f}%</text>',
        ))
        tick += 0.25
    lines.extend((
        f'<polygon points="{polygon("q05", "q95")}" fill="#dcecf7" opacity="0.95" data-fan-band="90"/>',
        f'<polygon points="{polygon("q10", "q90")}" fill="#a8d2ee" opacity="0.92" data-fan-band="80"/>',
        f'<polygon points="{polygon("q25", "q75")}" fill="#5fa9db" opacity="0.88" data-fan-band="50"/>',
    ))
    for index, item in enumerate(distributions):
        xx = xs[index]
        for rate, probability in item["points"]:
            radius = 2.0 + 10.0 * math.sqrt(max(0.0, probability))
            lines.append(
                f'<circle data-rate-state="{index}" cx="{xx:.2f}" cy="{y(rate):.2f}" r="{radius:.2f}" fill="#ffffff" fill-opacity="0.58" stroke="#0b1f33" stroke-opacity="0.28" stroke-width="1"><title>{escape(str(item["label"]))}: {rate:.3f}% ({probability:.2%} path mass)</title></circle>'
            )
    median_points = " ".join(f"{xs[index]:.2f},{y(float(item['q50'])):.2f}" for index, item in enumerate(distributions))
    lines.append(f'<polyline points="{median_points}" fill="none" stroke="#0b1f33" stroke-width="4" data-median-line="true"/>')
    for index, item in enumerate(distributions):
        xx = xs[index]
        label = "NOW" if index == 0 else str(item["label"])[5:]
        lines.extend((
            f'<line x1="{xx:.2f}" y1="{top}" x2="{xx:.2f}" y2="{top+plot_height}" stroke="#ffffff" stroke-opacity="0.7" stroke-width="1"/>',
            f'<circle cx="{xx:.2f}" cy="{y(float(item["q50"])):.2f}" r="5" fill="#0b1f33"/>',
            f'<text x="{xx:.2f}" y="{top+plot_height+31}" text-anchor="middle" font-family="system-ui,sans-serif" font-size="12" font-weight="650" fill="#10243a">{escape(label)}</text>',
            f'<text x="{xx:.2f}" y="{top+plot_height+50}" text-anchor="middle" font-family="system-ui,sans-serif" font-size="11" fill="#5d6b79">median {float(item["q50"]):.2f}%</text>',
        ))
    legend_y = height - 27
    for index, (label, fill) in enumerate((("90% range", "#dcecf7"), ("80% range", "#a8d2ee"), ("50% range", "#5fa9db"))):
        xx = 345 + index * 155
        lines.extend((
            f'<rect x="{xx}" y="{legend_y-10}" width="22" height="10" fill="{fill}"/>',
            f'<text x="{xx+30}" y="{legend_y}" font-family="system-ui,sans-serif" font-size="11" fill="#5d6b79">{label}</text>',
        ))
    lines.extend((
        f'<text x="{left}" y="{legend_y}" font-family="system-ui,sans-serif" font-size="11" fill="#5d6b79">Circles: individual tree nodes, sized by path mass</text>',
        f'<text x="{width-right}" y="{legend_y}" text-anchor="end" font-family="system-ui,sans-serif" font-size="11" fill="#5d6b79">50+ bp action tails represented at ±50 bp</text>',
        '</svg>',
    ))
    return ("\n".join(lines) + "\n").encode("utf-8")
