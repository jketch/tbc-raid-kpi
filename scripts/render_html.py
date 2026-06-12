"""render_html.py — inject WEEK_DATA into the dashboard HTML.

inject_into_html() reads dashboard/template.html (the tracked source), substitutes the
title placeholder, and splices the mapped WEEK_DATA over the `const WEEK_DATA = {...};`
block with a string/escape-aware BRACE-DEPTH walk — NOT a regex (a regex truncates at the
first `};` inside the data and corrupts it; do not "simplify" this). Extracted verbatim
from wcl_auto_dashboard, which re-exports it (week_build/build_site/preview keep calling
W.inject_into_html, preserving the test monkeypatch seam).
"""
from __future__ import annotations

import os, json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:                       # resolved only by a type checker — no runtime import / cycle
    from week_schema import WeekData

from paths import TEMPLATE_FILE, DEFAULT_TITLE
from week_map import map_to_week_data

def inject_into_html(week_data: dict, html_path: Path, mapped: WeekData | None = None):
    """Read template.html, inject WEEK_DATA, write to html_path (the gitignored output).

    Always reads from TEMPLATE_FILE so the output is never the source for the next run.
    Substitutes {{DASHBOARD_TITLE}} from the DASHBOARD_TITLE env var (default fallback).
    mapped: an already-mapped (and possibly trend-enriched) WEEK_DATA dict. When given,
    it is injected verbatim instead of re-mapping `week_data` — this preserves delta_*
    fields added by enrich_with_trends(). Omit it and the old behavior is unchanged."""
    src = TEMPLATE_FILE if TEMPLATE_FILE.exists() else html_path
    html = src.read_text(encoding="utf-8")

    # Substitute the dashboard title placeholder
    title = os.environ.get("DASHBOARD_TITLE") or DEFAULT_TITLE
    html = html.replace("{{DASHBOARD_TITLE}}", title)

    if mapped is None:
        mapped = map_to_week_data(week_data)
    new_json = json.dumps(mapped, indent=2, ensure_ascii=False)

    # Find the start of const WEEK_DATA = {
    marker = "const WEEK_DATA ="
    start  = html.find(marker)
    if start == -1:
        print("  ERROR: could not find 'const WEEK_DATA =' in HTML — skipping injection")
        return

    # Walk forward from { counting brackets to find the matching }
    brace_start = html.index("{", start)
    depth, i = 0, brace_start
    in_str, escape = False, False
    while i < len(html):
        ch = html[i]
        if escape:
            escape = False
        elif ch == "\\" and in_str:
            escape = True
        elif ch == '"' and not escape:
            in_str = not in_str
        elif not in_str:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    break
        i += 1

    # Replace from marker through the closing }; (plus optional semicolon)
    end = i + 1
    if html[end:end+1] == ";":
        end += 1

    updated = html[:start] + f"const WEEK_DATA = {new_json};" + html[end:]
    html_path.write_text(updated, encoding="utf-8")
    print(f"\n✅ Dashboard updated: {html_path}")
