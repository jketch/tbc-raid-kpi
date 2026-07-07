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


# ── Identity guard: a combat log must temporally COVER the report it overlays ────────────
# TBC bosses repeat every raid night, so parse_combat_log's boss-NAME scope alone cannot reject a
# same-boss log from a DIFFERENT week. A leftover log handed to the wrong report (main()'s logs/
# fallback or backfill --log) would then overlay its log-only KPIs — MC, friendly fire, drums,
# avoidable, death recaps — onto that week (the Jun-1-log-on-May-weeks contamination, 2026-06-14).
# The TIME window catches what the name can't. Reuses log_discovery's date-anchored scale (the
# discover_log path already proves the math). Fails CLOSED — a log we can't time-place must not merge.
def _log_covers_report(log_path, report) -> bool:
    """True iff the combat log at log_path covers ≥ MATCH_FLOOR of the report's time window.
    A non-covering (wrong-week) or unsniffable log is REJECTED so the caller degrades to WCL-only
    instead of silently merging fabricated log data."""
    import log_discovery as ld
    try:
        S, E = ld.to_log_scale(report["startTime"]), ld.to_log_scale(report["endTime"])
    except (KeyError, TypeError):
        return True   # report carries no window (shouldn't happen via Q_REPORT) — don't block
    window = max(E - S, 1.0)
    name = Path(log_path).name
    rng = ld.sniff_log_range(Path(log_path))
    if not rng:
        print(f"  [LOG] REJECTED {name}: unreadable timestamps — building WCL-only")
        return False
    overlap = max(0.0, min(E, rng[1]) - max(S, rng[0])) / window
    if overlap < ld.MATCH_FLOOR:
        print(f"  [LOG] REJECTED {name}: covers {overlap:.0%} of the report window "
              f"(< {ld.MATCH_FLOOR:.0%}) — not this week's log; building WCL-only")
        return False
    return True


# ── 2. The WCL producer (shared by main + backfill) ─────────────────────────────────────
def from_wcl(report_code, token, *, log_path=None, report=None, history=None):
    """Build a mapped WEEK_DATA from WCL (+ optional combat log). Returns (mapped, raw, log_data).
    `raw` is the pre-map merged wcl dict (publish summary / dry-run read it); `log_data` lets the
    caller pass `has_log` to finalize_week. Does NOT ingest loot or validate — that's finalize_week,
    so every producer gets loot the same way (post-map), killing the raw-vs-mapped loot drift."""
    import wcl_auto_dashboard as W
    if report is None:
        report = W.gql(token, W.Q_REPORT, {"code": report_code})["reportData"]["report"]
        if report is None:   # WCL returns report: null for an unknown code — fail with the fix, not a NoneType traceback
            raise SystemExit(f"ERROR: WCL report '{report_code}' not found — check the code "
                             "(fresh.warcraftlogs.com/reports/<CODE>)")
    log_data = None
    if log_path and _log_covers_report(log_path, report):
        # Subtract off-content warmup bosses (Gruul/HKM bundled into the SAME report) so the
        # log-only KPIs (avoidable, FF, drums, consum-use) match the WCL-side exclusion —
        # otherwise a bundled Gruul's Shatter leaks into avoidable/FF while WCL sections drop it.
        allowed = ({f["name"] for f in report["fights"] if f.get("kill")}
                   - W.EXCLUDED_ENCOUNTERS)
        log_data = W.parse_combat_log(log_path, allowed_bosses=allowed)
    raw = W.build_week_data(report_code, token,
                            log_data=log_data, report=report, history=history)
    if log_data:
        raw = W.merge_log_into_wcl(raw, log_data)
    mapped = W.map_to_week_data(raw)
    return mapped, raw, log_data


# ── 3. The single finalize funnel (EVERY producer calls this) ───────────────────────────
def finalize_week(mapped: ws.WeekData, *, has_log=None, loot_csv=None, do_validate=True, label=None) -> ws.WeekData:
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
def commit_week(mapped: ws.WeekData, db_path, *, is_test=False, dump=True, enrich=True,
               write_db=True, render=False) -> ws.WeekData:
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
