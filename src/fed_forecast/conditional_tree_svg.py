"""Accessible SVG view of the marginal-preserving conditional Fed tree."""

from __future__ import annotations

from collections.abc import Mapping
from html import escape


COLORS = {
    "down_50plus": "#873f4f",
    "down_25": "#d16b5a",
    "unchanged": "#c8b978",
    "up_25": "#5d9fa1",
    "up_50plus": "#4fc694",
}
CATEGORIES = ("down_50plus", "down_25", "unchanged", "up_25", "up_50plus")
LABELS = {
    "down_50plus": "-50+",
    "down_25": "-25",
    "unchanged": "0",
    "up_25": "+25",
    "up_50plus": "+50+",
}


def _stacked_bar(lines: list[str], probabilities: Mapping[str, object], x: float, y: float, width: float, height: float, tag: str) -> None:
    cursor = x
    for category in CATEGORIES:
        probability = float(probabilities[category])
        segment = width * probability
        lines.append(
            f'<rect data-conditional-bar="{escape(tag)}-{category}" x="{cursor:.1f}" y="{y:.1f}" width="{segment:.1f}" height="{height:.1f}" fill="{COLORS[category]}"/>'
        )
        if segment >= 33:
            text_color = "#ffffff" if category != "unchanged" else "#182737"
            lines.append(
                f'<text x="{cursor + segment / 2:.1f}" y="{y + height / 2 + 4:.1f}" text-anchor="middle" font-family="system-ui, sans-serif" font-size="11" font-weight="650" fill="{text_color}">{probability:.1%}</text>'
            )
        cursor += segment


def render_conditional_tree_svg(payload: Mapping[str, object]) -> bytes:
    tree = payload.get("conditional_tree")
    meetings = payload.get("meetings")
    if not isinstance(tree, Mapping) or not isinstance(meetings, list) or len(meetings) < 2:
        raise ValueError("conditional tree payload is malformed")
    nodes = tree.get("nodes")
    tables = tree.get("adjacent_conditional_tables")
    if not isinstance(nodes, list) or not isinstance(tables, list) or not tables:
        raise ValueError("conditional tree nodes are missing")
    node_by_id = {str(node["node_id"]): node for node in nodes if isinstance(node, Mapping)}
    root = node_by_id["root"]
    width, height = 1400, 1100
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">',
        '<title id="title">Conditional Federal Reserve meeting tree</title>',
        '<desc id="desc">A five-branch tree in which each exact realized action magnitude changes the probabilities at every future meeting while all current quoted marginals remain preserved.</desc>',
        '<rect width="100%" height="100%" fill="#fbfaf7"/>',
        '<text x="55" y="50" font-family="Georgia, serif" font-size="30" font-weight="700" fill="#182737">Fed path as a conditional event tree</text>',
        f'<text x="55" y="80" font-family="system-ui, sans-serif" font-size="14" fill="#596777">Snapshot {escape(str(payload.get("snapshot_fetched_at")))} · {int(tree["node_count"])} nodes · {int(tree["leaf_count"])} full paths · quoted marginals preserved</text>',
        '<text x="55" y="122" font-family="system-ui, sans-serif" font-size="16" font-weight="650" fill="#182737">1 · September realization reprices October</text>',
        '<circle cx="112" cy="260" r="36" fill="#182737"/>',
        '<text x="112" y="256" text-anchor="middle" font-family="system-ui, sans-serif" font-size="12" font-weight="700" fill="#ffffff">TODAY</text>',
        f'<text x="112" y="275" text-anchor="middle" font-family="system-ui, sans-serif" font-size="10" fill="#dbe5ed">{escape(str(meetings[0]["date"]))}</text>',
    ]
    child_y = {category: 140.0 + index * 60.0 for index, category in enumerate(CATEGORIES)}
    for category in CATEGORIES:
        child = node_by_id[category]
        y = child_y[category]
        branch = next(item for item in root["branches"] if item["category"] == category)
        lines.extend((
            f'<path d="M 148 260 C 220 260, 228 {y:.1f}, 300 {y:.1f}" fill="none" stroke="{COLORS[category]}" stroke-width="3"/>',
            f'<text x="215" y="{(260+y)/2-7:.1f}" text-anchor="middle" font-family="system-ui, sans-serif" font-size="12" font-weight="650" fill="{COLORS[category]}">{float(branch["conditional_probability"]):.1%}</text>',
            f'<rect x="300" y="{y-23:.1f}" width="142" height="46" rx="8" fill="{COLORS[category]}"/>',
            f'<text x="371" y="{y-2:.1f}" text-anchor="middle" font-family="system-ui, sans-serif" font-size="13" font-weight="700" fill="#ffffff">SEP {LABELS[category]} BP</text>',
            f'<text x="371" y="{y+14:.1f}" text-anchor="middle" font-family="system-ui, sans-serif" font-size="9" fill="#ffffff">exact action branch</text>',
            f'<line x1="442" y1="{y:.1f}" x2="515" y2="{y:.1f}" stroke="#7e8a96" stroke-width="2"/>',
            f'<text x="525" y="{y-18:.1f}" font-family="system-ui, sans-serif" font-size="11" font-weight="650" fill="#182737">OCT conditional -50+ / -25 / 0 / +25 / +50+</text>',
        ))
        _stacked_bar(lines, child["next_probabilities"], 525, y - 8, 350, 24, f"sep-{category}-oct")
        lines.extend((
            f'<text x="895" y="{y-5:.1f}" font-family="system-ui, sans-serif" font-size="10" fill="#596777">conditional year-end</text>',
            f'<text x="895" y="{y+13:.1f}" font-family="system-ui, sans-serif" font-size="14" font-weight="700" fill="#182737">{float(child["conditional_terminal_expected_upper"]):.3f}%</text>',
        ))
    lines.extend((
        '<rect x="1030" y="135" width="305" height="252" rx="9" fill="#eef2f5"/>',
        '<text x="1050" y="164" font-family="system-ui, sans-serif" font-size="13" font-weight="700" fill="#182737">How to read this</text>',
        '<text x="1050" y="191" font-family="system-ui, sans-serif" font-size="11" fill="#596777">1. Take the September branch.</text>',
        '<text x="1050" y="216" font-family="system-ui, sans-serif" font-size="11" fill="#596777">2. Replace October marginal odds with</text>',
        '<text x="1064" y="233" font-family="system-ui, sans-serif" font-size="11" fill="#596777">the branch-specific stacked bar.</text>',
        '<text x="1050" y="258" font-family="system-ui, sans-serif" font-size="11" fill="#596777">3. Continue from the full-history node;</text>',
        '<text x="1064" y="275" font-family="system-ui, sans-serif" font-size="11" fill="#596777">all later probabilities reprice again.</text>',
        '<text x="1050" y="305" font-family="system-ui, sans-serif" font-size="11" fill="#596777">The one-dimensional market odds remain</text>',
        '<text x="1050" y="322" font-family="system-ui, sans-serif" font-size="11" fill="#596777">the weighted average across branches.</text>',
        '<text x="1050" y="355" font-family="system-ui, sans-serif" font-size="10" font-weight="650" fill="#c05a3e">Transitions are modeled, not traded.</text>',
        '<line x1="55" y1="430" x2="1345" y2="430" stroke="#d9dde1"/>',
        '<text x="55" y="467" font-family="system-ui, sans-serif" font-size="16" font-weight="650" fill="#182737">2 · One-step conditional tables farther down the tree</text>',
        '<text x="1345" y="467" text-anchor="end" font-family="system-ui, sans-serif" font-size="11" fill="#596777">These rows marginalize over earlier history; JSON nodes retain the complete path.</text>',
    ))
    panel_width = 620
    for panel_index, table in enumerate(tables[1:3]):
        x0 = 55 + panel_index * 665
        lines.extend((
            f'<rect x="{x0}" y="490" width="{panel_width}" height="330" rx="9" fill="#eef2f5"/>',
            f'<text x="{x0+20}" y="522" font-family="system-ui, sans-serif" font-size="14" font-weight="700" fill="#182737">{escape(str(table["realized_meeting_date"]))} -> {escape(str(table["next_meeting_date"]))}</text>',
        ))
        for row_index, row in enumerate(table["rows"]):
            y = 572 + row_index * 54
            category = str(row["realized_category"])
            lines.extend((
                f'<rect x="{x0+20}" y="{y-22}" width="112" height="38" rx="6" fill="{COLORS[category]}"/>',
                f'<text x="{x0+76}" y="{y+2}" text-anchor="middle" font-family="system-ui, sans-serif" font-size="11" font-weight="700" fill="#ffffff">{LABELS[category]} BP</text>',
            ))
            _stacked_bar(lines, row["next_probabilities"], x0 + 152, y - 16, 420, 26, f"{panel_index}-{category}")
    lines.extend((
        '<text x="55" y="865" font-family="system-ui, sans-serif" font-size="16" font-weight="650" fill="#182737">3 · Model discipline</text>',
        f'<text x="55" y="898" font-family="system-ui, sans-serif" font-size="12" fill="#596777">Persistent-stance strength {float(tree["settings"]["dependence_strength"]):.2f} · decay {float(tree["settings"]["dependence_decay"]):.2f} · terminal-consistency sigma {float(tree["settings"]["terminal_consistency_sigma_bp"]):.0f} bp</text>',
        f'<text x="55" y="922" font-family="system-ui, sans-serif" font-size="12" fill="#596777">Iterative proportional fitting converged in {int(tree["raking"]["iterations"])} iterations; maximum marginal error {float(tree["raking"]["max_marginal_error"]):.2e}.</text>',
        '<text x="55" y="954" font-family="system-ui, sans-serif" font-size="12" fill="#182737">Observed:</text>',
        '<text x="126" y="954" font-family="system-ui, sans-serif" font-size="12" fill="#596777">five exact action marginals at each meeting and the terminal marginal.</text>',
        '<text x="55" y="978" font-family="system-ui, sans-serif" font-size="12" fill="#182737">Modeled:</text>',
        '<text x="126" y="978" font-family="system-ui, sans-serif" font-size="12" fill="#596777">how one exact action changes the odds at all later dates.</text>',
        '<text x="55" y="1026" font-family="system-ui, sans-serif" font-size="11" fill="#596777">Action colors run from -50+ bp through -25, unchanged, +25 and +50+ bp.</text>',
        '<text x="1345" y="1026" text-anchor="end" font-family="system-ui, sans-serif" font-size="11" fill="#596777">Research model · open-ended tails represented at +/-50 bp</text>',
        '</svg>',
    ))
    return ("\n".join(lines) + "\n").encode("utf-8")
