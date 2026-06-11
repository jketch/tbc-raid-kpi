"""Backfill full WEEK_DATA snapshots for past weeks WITHOUT touching the prod DB or the live HTML.

Why this exists: the rolling multi-week view needs each week's *full* mapped WEEK_DATA
(cache/week_data/<report>.json) to render a whole dashboard — the history DB only holds the KPI
subset. The snapshot cache only started recording mid-2026, so older weeks have no snapshot.

This rebuilds them from WCL (build_week_data → map_to_week_data → dump_week_data_cache) and is
deliberately:
  • READ-ONLY on the prod DB  — it only READS crit history (for the luck baseline); it never calls
    write_week, so the existing log-complete KPI rows for these weeks are preserved untouched.
  • NO HTML inject / NO deploy — it just writes the snapshot JSONs.

Combat log handling:
  • Without --log (default)     — WCL-durable only. Log-only KPIs (avoidable, drums, MC, friendly
    fire, death-recap timelines, tank lowest-HP) are thin; per-fight tank roles come from WCL and
    are hardened via the boss-melee signal (harden_tank_fights), so a feral/'Warden' tank still
    shows. WCL-durable KPIs (DPS, healing, deaths, tanks, crit, boss times) populate fully.
  • With --log <path>           — if you still have that week's WoWCombatLog.txt, produces a
    LOG-COMPLETE snapshot identical to a live weekly run: log-based tank roles (boss-melee ground
    truth) plus all log-only KPIs restored. Applies to a single report code.

Usage:
    python scripts/backfill_snapshots.py <REPORT> [<REPORT> ...]
    python scripts/backfill_snapshots.py <REPORT> --log logs/WoWCombatLog-MMDDYY_HHMMSS.txt
"""
import sys, os, argparse
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import wcl_auto_dashboard as W
from db_writer import crit_history, DB_PATH


def backfill(report_code: str, token: str, log_path: str = None) -> None:
    tag = "log-complete" if log_path else "no log"
    print(f"\n=== backfill snapshot: {report_code} ({tag}, read-only DB) ===")
    rep0 = W.gql(token, W.Q_REPORT, {"code": report_code})["reportData"]["report"]
    # Parse the log scoped to THIS report's bosses (ignores off-report DST clears), exactly as the
    # live pipeline does — so build_week_data uses log-based per-fight roles + log-only KPIs.
    log_data = None
    if log_path:
        allowed = {f["name"] for f in rep0["fights"] if f.get("kill")}
        log_data = W.parse_combat_log(log_path, allowed_bosses=allowed)
    crit_hist = crit_history(DB_PATH, exclude_report=report_code)        # READ-only
    wk = W.build_week_data(report_code, token, refresh_baseline=False,
                           log_data=log_data, report=rep0, history=crit_hist)
    if log_data:
        wk = W.merge_log_into_wcl(wk, log_data)                          # overlay log-only KPIs
    mapped = W.map_to_week_data(wk)
    W.reingest_loot(mapped)                                             # loot lives only in the live
    # pipeline's --loot step; re-attach this week's loot from the newest loot/*.csv (by raid date)
    # so the rebuilt snapshot keeps its Loot tab instead of silently dropping it.
    W.dump_week_data_cache(mapped)                                       # writes cache/week_data/<code>.json only
    m = mapped.get("meta", {})
    print(f"  ✓ {report_code} ({m.get('date')}) — {len(mapped.get('roster') or {})} players, "
          f"{m.get('kills')} kills  → snapshot cached")


def main():
    ap = argparse.ArgumentParser(
        description="Backfill WEEK_DATA snapshots for past weeks (read-only DB, no HTML/deploy).")
    ap.add_argument("reports", nargs="+", help="WCL report code(s)")
    ap.add_argument("--log", help="Path to that week's WoWCombatLog.txt for a LOG-COMPLETE snapshot "
                                  "(restores log-only KPIs + log-based tank roles). Single report only.")
    args = ap.parse_args()
    if args.log:
        if len(args.reports) != 1:
            ap.error("--log applies to a single week; pass exactly one report code with --log")
        if not os.path.isfile(args.log):
            ap.error(f"--log file not found: {args.log}")
    cid, csec = os.environ.get("WCL_CLIENT_ID"), os.environ.get("WCL_CLIENT_SECRET")
    if not cid or not csec:
        raise SystemExit("WCL_CLIENT_ID / WCL_CLIENT_SECRET not set (.env)")
    token = W.get_token(cid, csec)
    print(f"  ✓ token obtained · backfilling {len(args.reports)} week(s)"
          + (f" with log {args.log}" if args.log else ""))
    for c in args.reports:
        backfill(c, token, log_path=args.log)
    print(f"\n✓ done — {len(args.reports)} snapshot(s) in {W.WEEK_DATA_CACHE}")


if __name__ == "__main__":
    main()
