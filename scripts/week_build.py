"""One way to build, finalize, and commit a week.

Collapses the three drifted producers (main / backfill_snapshots / reprocess) onto a shared spine:

    SOURCES ──▶ from_wcl()/from_snapshot()  ──▶ mapped
                            │
                            ▼
            finalize_week(mapped, sources)        ◀── the SINGLE funnel
              • resolve + ingest loot (one place)
              • validate against week_schema (the contract)
                            │
                            ▼
            commit_week(mapped, db_path, …)       ◀── the shared persistence tail
              • dump snapshot (test-gated, clean pre-enrich)
              • enrich_with_trends  (STRICTLY before write)
              • write_week  (db_writer's downgrade-guard protects richer rows)
              • inject_into_html (render only when asked)

`main`/`backfill`/`reprocess` become thin orchestrators that pick sources and which tail steps run.
This is what makes the loot-drop / role-drift / order-skew bug class structurally impossible.

Lazy `import wcl_auto_dashboard` inside functions avoids an import cycle (the core imports this for
main()). `week_schema` is a leaf, imported at top.
"""
import json
from pathlib import Path

import week_schema as ws


# ── 1. The ONE string-aware WEEK_DATA loader ────────────────────────────────────────────
# Replaces reprocess.load_week_data's non-string-aware matcher (which breaks on a `{`/`}`
# inside any boss/item/spell name). Same walker as publish.extract_week_data / inject_into_html.
def extract_week_data(html: str) -> dict:
    """Brace-match the `const WEEK_DATA = {…}` object out of an injected dashboard HTML string.
    String/escape aware, so a `{` or `}` inside a JSON string value never breaks the match."""
    m = html.find("const WEEK_DATA =")
    if m < 0:
        raise ValueError("no 'const WEEK_DATA =' in HTML")
    start = html.index("{", m)
    i, depth, in_str, esc = start, 0, False, False
    while i < len(html):
        c = html[i]
        if esc:
            esc = False
        elif c == "\\" and in_str:
            esc = True
        elif c == '"':
            in_str = not in_str
        elif not in_str:
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(html[start:i + 1])
        i += 1
    raise ValueError("unbalanced braces in WEEK_DATA")


def from_snapshot(path) -> dict:
    """Load a mapped WEEK_DATA from either a cache/week_data/*.json snapshot OR an injected HTML
    (auto-detected). The single entry point for the no-WCL (reprocess) producer."""
    text = Path(path).read_text(encoding="utf-8")
    if "const WEEK_DATA" in text:
        return extract_week_data(text)
    return json.loads(text)


# ── 2. The WCL producer (shared by main + backfill) ─────────────────────────────────────
def from_wcl(report_code, token, *, log_path=None, report=None, history=None,
            refresh_baseline=False):
    """Build a mapped WEEK_DATA from WCL (+ optional combat log). Returns (mapped, raw, log_data).
    `raw` is the pre-map merged wcl dict (publish summary / dry-run read it); `log_data` lets the
    caller pass `has_log` to finalize_week. Does NOT ingest loot or validate — that's finalize_week,
    so every producer gets loot the same way (post-map), killing the raw-vs-mapped loot drift."""
    import wcl_auto_dashboard as W
    if report is None:
        report = W.gql(token, W.Q_REPORT, {"code": report_code})["reportData"]["report"]
    log_data = None
    if log_path:
        allowed = {f["name"] for f in report["fights"] if f.get("kill")}
        log_data = W.parse_combat_log(log_path, allowed_bosses=allowed)
    raw = W.build_week_data(report_code, token, refresh_baseline=refresh_baseline,
                            log_data=log_data, report=report, history=history)
    if log_data:
        raw = W.merge_log_into_wcl(raw, log_data)
    mapped = W.map_to_week_data(raw)
    return mapped, raw, log_data


# ── 3. The single finalize funnel (EVERY producer calls this) ───────────────────────────
def finalize_week(mapped, *, has_log=None, loot_csv=None, do_validate=True, label=None):
    """Apply post-map external enrichment, then validate against the contract. Mutates and returns
    `mapped`. NEVER raises.

    Owns the ONE loot-ingestion point: re-attaches this week's loot from the newest loot/*.csv (or
    an explicit `loot_csv`) so main / backfill / reprocess / build_site can't disagree about loot.
    `has_log` (True/False/None) only sharpens the validation wording; reprocess passes None (it works
    from a snapshot and can't know the original sources), so it never emits false warnings."""
    import wcl_auto_dashboard as W
    W.reingest_loot(mapped, csv_path=loot_csv)        # no-op when the CSV has no awards that night
    if do_validate:
        has_loot = ws.is_populated(mapped, "loot")
        label = label or (mapped.get("meta") or {}).get("report_code", "week")
        print(ws.format_report(ws.validate(mapped, has_log=has_log, has_loot=has_loot), label=label))
    return mapped


# ── 4. The shared persistence tail (main + reprocess) ───────────────────────────────────
def commit_week(mapped, db_path, *, is_test=False, dump=True, enrich=True,
               write_db=True, render=False):
    """Persist a finalized week. Encodes the load-bearing order: dump (clean, pre-enrich) →
    enrich_with_trends → write_week → render. Returns `mapped`.

    Invariants:
      • enrich STRICTLY precedes write — enrich reads the prior week's DB row, and write_week's
        INSERT OR REPLACE would clobber it. (Asserted by ordering here; do not reorder.)
      • the snapshot dump is test-gated — `--test-db` must never pollute the canonical cache.
      • backfill stays DB-read-only by NOT calling this (it uses finalize_week + dump only).
    """
    import wcl_auto_dashboard as W
    import db_writer
    if dump and not is_test:
        W.dump_week_data_cache(mapped)                # clean mapped, BEFORE enrich adds delta_*/prev
    if enrich:
        W.enrich_with_trends(mapped, db_path)         # MUST run before write_week
    if write_db:
        try:
            db_writer.write_week(mapped, db_path)
        except Exception as e:
            print(f"  db write warning: {e}")
    if render:
        W.inject_into_html(mapped, W.DASH_FILE, mapped=mapped)
    return mapped
