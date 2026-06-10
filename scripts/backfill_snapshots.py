"""Backfill full WEEK_DATA snapshots for past weeks WITHOUT touching the prod DB or the live HTML.

Why this exists: the rolling multi-week view needs each week's *full* mapped WEEK_DATA
(cache/week_data/<report>.json) to render a whole dashboard — the history DB only holds the KPI
subset. The snapshot cache only started recording mid-2026, so older weeks have no snapshot.

This rebuilds them from WCL (build_week_data → map_to_week_data → dump_week_data_cache) and is
deliberately:
  • READ-ONLY on the prod DB  — it only READS crit history (for the luck baseline); it never calls
    write_week, so the existing log-complete KPI rows for these weeks are preserved untouched.
  • NO combat log              — those 180 MB logs are long gone, so log-only KPIs (avoidable, drums,
    MC, friendly fire, death-recap timelines) will be thin for backfilled weeks. WCL-durable KPIs
    (DPS, healing, deaths, tanks, crit, boss times, loot N/A) populate fully.
  • NO HTML inject / NO deploy — it just writes the snapshot JSONs.

Usage:
    python scripts/backfill_snapshots.py <REPORT> [<REPORT> ...]
"""
import sys, os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import wcl_auto_dashboard as W
from db_writer import crit_history, DB_PATH


def backfill(report_code: str, token: str) -> None:
    print(f"\n=== backfill snapshot: {report_code} (no log, read-only DB) ===")
    rep0 = W.gql(token, W.Q_REPORT, {"code": report_code})["reportData"]["report"]
    crit_hist = crit_history(DB_PATH, exclude_report=report_code)        # READ-only
    wk = W.build_week_data(report_code, token, refresh_baseline=False,
                           log_data=None, report=rep0, history=crit_hist)
    mapped = W.map_to_week_data(wk)
    W.dump_week_data_cache(mapped)                                       # writes cache/week_data/<code>.json only
    m = mapped.get("meta", {})
    print(f"  ✓ {report_code} ({m.get('date')}) — {len(mapped.get('roster') or {})} players, "
          f"{m.get('kills')} kills  → snapshot cached")


def main():
    codes = sys.argv[1:]
    if not codes:
        raise SystemExit("usage: python scripts/backfill_snapshots.py <REPORT> [<REPORT> ...]")
    cid, csec = os.environ.get("WCL_CLIENT_ID"), os.environ.get("WCL_CLIENT_SECRET")
    if not cid or not csec:
        raise SystemExit("WCL_CLIENT_ID / WCL_CLIENT_SECRET not set (.env)")
    token = W.get_token(cid, csec)
    print(f"  ✓ token obtained · backfilling {len(codes)} week(s)")
    for c in codes:
        backfill(c, token)
    print(f"\n✓ done — {len(codes)} snapshot(s) in {W.WEEK_DATA_CACHE}")


if __name__ == "__main__":
    main()
