"""replay_render.py — offline refactor-proof: render a week's dashboard with ZERO side effects.

Renders the dashboard HTML from the OFFLINE raw cache — no WCL calls, no DB writes, no touch
of dashboard/raid_kpi_dashboard.html:  cache/wcl/<code>.json → map_to_week_data → finalize
(loot re-ingest + contract check) → enrich_with_trends (READ-only on the prod DB) →
inject_into_html(<out>).

Usage:
    python scripts/replay_render.py <out.html> [report_code]    # default: newest raw cache

Proof workflow (output is byte-deterministic since db60d00):
  - "did my map/trends/render/template change alter the output?" → render before + after the
    change and `fc /b` the two files (or diff against cache/replay_baseline.html).
  - Fetch-layer changes (wcl_fetchers / combat_log) can NOT be proven here — the frozen raw
    cache never runs them; use two live `--dry-run`s on a fixed report instead.
"""
import sys, json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wcl_auto_dashboard as W
import week_build as wb
import db_writer


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    out = Path(sys.argv[1])
    if len(sys.argv) > 2:
        src = W.WCL_CACHE / f"{sys.argv[2]}.json"
    else:
        caches = sorted(W.WCL_CACHE.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not caches:
            raise SystemExit(f"no raw caches in {W.WCL_CACHE} — seed via a prod run or backfill_snapshots.py")
        src = caches[0]
    if not src.exists():
        raise SystemExit(f"no raw cache: {src}")
    raw = json.loads(src.read_text(encoding="utf-8"))
    if isinstance(raw.get("fight_durs"), dict):  # JSON round-trip: rehydrate int keys (mirrors reprocess.py)
        raw["fight_durs"] = {int(k): v for k, v in raw["fight_durs"].items()}
    wd = W.map_to_week_data(raw)
    wb.finalize_week(wd, has_log=None)
    W.enrich_with_trends(wd, db_writer.DB_PATH)   # READ-only
    W.inject_into_html(wd, out, mapped=wd)


if __name__ == "__main__":
    main()
