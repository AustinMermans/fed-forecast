"""Accessible SVG dashboard for meeting decomposition and shock-only scenarios."""

from __future__ import annotations

from collections.abc import Mapping
from html import escape


COLORS = {"down": "#3b82b8", "unchanged": "#a7adb5", "up": "#c05a3e"}


def render_meeting_scenarios_svg(payload: Mapping[str, object]) -> bytes:
    meetings = payload.get("meetings")
    scenarios = payload.get("scenarios")
    if not isinstance(meetings, list) or not meetings or not isinstance(scenarios, list):
        raise ValueError("meeting scenario payload is malformed")
    width, height = 1200, 850
    left, right = 90, 55
    plot_width = width - left - right
    top_y, probability_height = 125, 245
    group_width = plot_width / len(meetings)
    bar_width = 42
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">',
        '<title id="title">Federal Reserve meeting probabilities and isolated action scenarios</title>',
        '<desc id="desc">Top panel decomposes each meeting into down, unchanged and up probabilities. Bottom panel shows the expected target upper bound at the next meeting if one meeting action category is substituted while all later action probabilities remain unchanged.</desc>',
        '<rect width="100%" height="100%" fill="#fbfaf7"/>',
        '<text x="55" y="48" font-family="Georgia, serif" font-size="29" font-weight="700" fill="#182737">Fed meeting map: marginals and one-shock scenarios</text>',
        f'<text x="55" y="78" font-family="system-ui, sans-serif" font-size="14" fill="#596777">Snapshot {escape(str(payload.get("snapshot_fetched_at")))} · target upper bound {float(payload["target_upper_bound_baseline"]):.3f}%</text>',
        '<text x="55" y="112" font-family="system-ui, sans-serif" font-size="15" font-weight="650" fill="#182737">1 · Current normalized action probabilities</text>',
        f'<line x1="{left}" y1="{top_y + probability_height}" x2="{width-right}" y2="{top_y + probability_height}" stroke="#4d5966"/>',
    ]
    for tick in (0, .25, .50, .75, 1.0):
        y = top_y + probability_height * (1 - tick)
        lines.extend((
            f'<line x1="{left}" y1="{y:.1f}" x2="{width-right}" y2="{y:.1f}" stroke="#d9dde1" stroke-width="1"/>',
            f'<text x="{left-12}" y="{y+4:.1f}" text-anchor="end" font-family="system-ui, sans-serif" font-size="11" fill="#66717d">{tick:.0%}</text>',
        ))
    for index, meeting in enumerate(meetings):
        if not isinstance(meeting, Mapping) or not isinstance(meeting.get("categories"), list):
            raise ValueError("meeting row is malformed")
        center = left + group_width * (index + .5)
        for offset, category in zip((-1, 0, 1), meeting["categories"], strict=True):
            if not isinstance(category, Mapping):
                raise ValueError("meeting category is malformed")
            name = str(category["category"])
            probability = float(category["probability"])
            bar_height = probability_height * probability
            x = center + offset * (bar_width + 8) - bar_width / 2
            y = top_y + probability_height - bar_height
            lines.extend((
                f'<rect data-bar="{escape(str(meeting["date"]))}-{name}" x="{x:.1f}" y="{y:.1f}" width="{bar_width}" height="{bar_height:.1f}" rx="3" fill="{COLORS[name]}"/>',
                f'<text x="{x+bar_width/2:.1f}" y="{max(y-7, top_y+11):.1f}" text-anchor="middle" font-family="system-ui, sans-serif" font-size="11" font-weight="650" fill="#283746">{probability:.1%}</text>',
                f'<text x="{x+bar_width/2:.1f}" y="{top_y+probability_height+20}" text-anchor="middle" font-family="system-ui, sans-serif" font-size="10" fill="#66717d">{name[0].upper()}</text>',
            ))
        lines.extend((
            f'<text x="{center:.1f}" y="{top_y+probability_height+43}" text-anchor="middle" font-family="system-ui, sans-serif" font-size="12" font-weight="650" fill="#283746">{escape(str(meeting["date"]))}</text>',
            f'<text x="{center:.1f}" y="{top_y+probability_height+61}" text-anchor="middle" font-family="system-ui, sans-serif" font-size="11" fill="#66717d">E[action] {float(meeting["expected_change_bp"]):+.1f} bp</text>',
        ))

    next_rows = [item for item in scenarios if isinstance(item, Mapping) and item.get("next_meeting_date") is not None]
    shock_dates = []
    for item in next_rows:
        if item["shock_meeting_date"] not in shock_dates:
            shock_dates.append(item["shock_meeting_date"])
    bottom_top, bottom_height = 510, 235
    lines.extend((
        '<text x="55" y="492" font-family="system-ui, sans-serif" font-size="15" font-weight="650" fill="#182737">2 · Next-meeting expected target upper bound under one isolated action</text>',
        '<text x="1145" y="492" text-anchor="end" font-family="system-ui, sans-serif" font-size="11" fill="#66717d">Future D/N/U probabilities held fixed</text>',
    ))
    all_rates = [
        float(item["next_meeting_mechanical_target_upper"])
        for item in next_rows if item.get("next_meeting_mechanical_target_upper") is not None
    ] + [float(item["next_meeting_baseline_target_upper"]) for item in next_rows]
    low, high = min(all_rates) - .04, max(all_rates) + .04
    for tick_index in range(5):
        value = low + (high - low) * tick_index / 4
        y = bottom_top + bottom_height * (high - value) / (high - low)
        lines.extend((
            f'<line x1="{left}" y1="{y:.1f}" x2="{width-right}" y2="{y:.1f}" stroke="#d9dde1"/>',
            f'<text x="{left-12}" y="{y+4:.1f}" text-anchor="end" font-family="system-ui, sans-serif" font-size="11" fill="#66717d">{value:.2f}%</text>',
        ))
    scenario_group_width = plot_width / max(len(shock_dates), 1)
    for index, shock_date in enumerate(shock_dates):
        rows = [item for item in next_rows if item["shock_meeting_date"] == shock_date]
        center = left + scenario_group_width * (index + .5)
        baseline = float(rows[0]["next_meeting_baseline_target_upper"])
        baseline_y = bottom_top + bottom_height * (high - baseline) / (high - low)
        lines.extend((
            f'<line x1="{center-105:.1f}" y1="{baseline_y:.1f}" x2="{center+105:.1f}" y2="{baseline_y:.1f}" stroke="#7b8490" stroke-dasharray="5 4"/>',
            f'<text x="{center+109:.1f}" y="{baseline_y+4:.1f}" font-family="system-ui, sans-serif" font-size="10" fill="#66717d">base {baseline:.3f}%</text>',
        ))
        for offset, row in zip((-1, 0, 1), rows, strict=True):
            category = str(row["category"])
            rate = float(row["next_meeting_mechanical_target_upper"])
            x = center + offset * 64
            y = bottom_top + bottom_height * (high - rate) / (high - low)
            lines.extend((
                f'<line x1="{x:.1f}" y1="{baseline_y:.1f}" x2="{x:.1f}" y2="{y:.1f}" stroke="{COLORS[category]}" stroke-width="2"/>',
                f'<circle data-scenario="{escape(str(shock_date))}-{category}" cx="{x:.1f}" cy="{y:.1f}" r="7" fill="{COLORS[category]}"/>',
                f'<text x="{x:.1f}" y="{y-12:.1f}" text-anchor="middle" font-family="system-ui, sans-serif" font-size="11" font-weight="650" fill="#283746">{rate:.3f}%</text>',
                f'<text x="{x:.1f}" y="{bottom_top+bottom_height+22}" text-anchor="middle" font-family="system-ui, sans-serif" font-size="11" fill="#66717d">{category[0].upper()}</text>',
            ))
        lines.extend((
            f'<text x="{center:.1f}" y="{bottom_top+bottom_height+45}" text-anchor="middle" font-family="system-ui, sans-serif" font-size="12" font-weight="650" fill="#283746">{escape(str(shock_date))} → {escape(str(rows[0]["next_meeting_date"]))}</text>',
        ))
    lines.extend((
        '<rect x="55" y="812" width="11" height="11" fill="#3b82b8"/><text x="72" y="822" font-family="system-ui, sans-serif" font-size="11" fill="#66717d">Down</text>',
        '<rect x="125" y="812" width="11" height="11" fill="#a7adb5"/><text x="142" y="822" font-family="system-ui, sans-serif" font-size="11" fill="#66717d">Unchanged</text>',
        '<rect x="235" y="812" width="11" height="11" fill="#c05a3e"/><text x="252" y="822" font-family="system-ui, sans-serif" font-size="11" fill="#66717d">Up</text>',
        '<text x="1145" y="822" text-anchor="end" font-family="system-ui, sans-serif" font-size="11" fill="#66717d">Scenario levels are mechanical, not conditional repricing forecasts.</text>',
        '</svg>',
    ))
    return ("\n".join(lines) + "\n").encode("utf-8")
