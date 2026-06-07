"""
backfill_tank.py — Targeted backfill of the tank scorecard v2 tables from WCL.

`tank_scorecard` / `tank_boss_dtps` (and the `boss_times.encounter_id` column) were added
after several weeks of history existed, so older weeks have no tank rows and the week-over-week
tank DTPS trend can't compute for them.

Tank v2 is **WCL-durable** (DTPS, per-boss, phys/magic school split, crush/crit mitigation,
avoidance, defensive cooldowns, biggest hit all come from the WCL API), so a week can be
backfilled WITHOUT its combat log. The only log-only extra (lowest-HP%-survived) is display
enrichment and isn't persisted, so nothing is lost by a logless backfill.

SAFETY: this writes ONLY `tank_scorecard`, `tank_boss_dtps`, and the `encounter_id` column of
`boss_times`. It never touches the log-sourced tables (consumables, drums, interrupts,
avoidable, engineering, MC), so backfilling a week whose log is gone can't degrade them.

Usage:
  python scripts\backfill_tank.py                 # gap-fill prod DB (cache/raid_history.db)
  python scripts\backfill_tank.py --test-db       # gap-fill cache/raid_history_test.db
  python scripts\backfill_tank.py --force         # rewrite EVERY week's tank rows
  python scripts\backfill_tank.py --code XXXX     # only this report code
"""

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# Importing the main module also auto-loads .env, so WCL_CLIENT_* resolve.
from wcl_auto_dashboard import (get_token, gql, Q_REPORT, fetch_fight_roles,
                                fetch_healing_by_fight, build_tank_scorecard_extended,
                                fetch_deaths_split)
from db_writer import SCHEMA, DB_PATH, DB_PATH_TEST

Q_ACTORS = """query($c:String!){reportData{report(code:$c){
    masterData{ actors(type:"Player"){ id name type subType } }}}}"""


def backfill_week(token: str, con: sqlite3.Connection, code: str) -> int:
    """Fetch one report's WCL tank metrics and upsert its tank rows. Returns tanks written."""
    rep   = gql(token, Q_REPORT, {"code": code})["reportData"]["report"]
    kills = [f for f in rep["fights"] if f.get("kill")]
    if not kills:
        print(f"    {code}: no kills — skipped")
        return 0

    actors = gql(token, Q_ACTORS, {"c": code})["reportData"]["report"]["masterData"]["actors"]
    # WCL per-fight roles (no combat log here) — identifies who tanked which fights.
    fight_roles, fight_durs = fetch_fight_roles(token, code, kills)
    heal_by_fight = fetch_healing_by_fight(token, code, kills)
    tank_metrics, _raid_dps = build_tank_scorecard_extended(
        token, code, kills, fight_roles, fight_durs, heal_by_fight, actors)
    # per-player boss deaths for the tank_scorecard.deaths column
    death_boss, _trash, _recaps, _by_boss = fetch_deaths_split(token, code)

    n = 0
    for nm, tm in tank_metrics.items():
        bh = tm.get("biggest_hit") or {}
        con.execute("""
            INSERT OR REPLACE INTO tank_scorecard
              (report_code, player, dtps, taken, hps_recv, deaths, phys_pct, magic_pct,
               crush_count, crit_count, avoid_pct, biggest_hit, cooldowns)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (code, nm, tm.get("dtps"), tm.get("taken"), tm.get("hps_recv"),
              death_boss.get(nm, 0), tm.get("phys_pct"), tm.get("magic_pct"),
              tm.get("crush_count"), tm.get("crit_count"), tm.get("avoid_pct"),
              bh.get("amount"), json.dumps(tm.get("cooldowns") or {})))
        for pb in (tm.get("per_boss") or []):
            con.execute("""
                INSERT OR REPLACE INTO tank_boss_dtps
                  (report_code, player, boss, dtps, taken, seconds)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (code, nm, pb.get("boss"), pb.get("dtps"), pb.get("taken"), pb.get("seconds")))
        n += 1

    # boss_times.encounter_id — rows already exist from the original weekly run; just set the id.
    for f in kills:
        con.execute("UPDATE boss_times SET encounter_id=? WHERE report_code=? AND boss=?",
                    (f.get("encounterID"), code, f["name"]))
    return n


def main():
    ap = argparse.ArgumentParser(description="Backfill the tank scorecard v2 tables from WCL")
    ap.add_argument("--test-db", action="store_true", help="target raid_history_test.db")
    ap.add_argument("--force",   action="store_true", help="rewrite weeks that already have tank rows")
    ap.add_argument("--code",    help="only backfill this single report code")
    args = ap.parse_args()

    db_path = DB_PATH_TEST if args.test_db else DB_PATH
    if not Path(db_path).exists():
        print(f"ERROR: DB not found: {db_path}")
        sys.exit(1)

    cid, secret = os.getenv("WCL_CLIENT_ID"), os.getenv("WCL_CLIENT_SECRET")
    if not cid or not secret:
        print("ERROR: WCL_CLIENT_ID / WCL_CLIENT_SECRET not set (check .env)")
        sys.exit(1)

    con = sqlite3.connect(db_path)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)   # ensures the tank tables exist on older DBs

    weeks = con.execute("SELECT report_code, date FROM weeks ORDER BY start_ms").fetchall()
    have  = {r[0] for r in con.execute("SELECT DISTINCT report_code FROM tank_scorecard").fetchall()}
    todo  = []
    for code, date in weeks:
        if args.code and code != args.code:
            continue
        if code in have and not args.force:
            print(f"  • {code} ({date}) — already has tank rows, skipping (use --force to redo)")
            continue
        todo.append((code, date))

    if not todo:
        print("Nothing to backfill.")
        con.close()
        return

    print(f"Backfilling {len(todo)} week(s) into {Path(db_path).name} …")
    token = get_token(cid, secret)
    print("  ✓ WCL token obtained")

    total = 0
    for code, date in todo:
        print(f"  → {code} ({date})")
        try:
            written = backfill_week(token, con, code)
            con.commit()
            total += written
            print(f"    ✓ {written} tank rows written")
        except Exception as e:
            con.rollback()
            print(f"    ⚠ failed: {e}")

    con.close()
    print(f"\nDone — {total} tanks across {len(todo)} week(s) → {Path(db_path).name}")


if __name__ == "__main__":
    main()
