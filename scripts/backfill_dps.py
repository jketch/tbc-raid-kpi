"""
backfill_dps.py — One-off / occasional backfill of the `dps` history table from WCL.

The `dps` table was added after several weeks of history already existed, so older weeks
have no per-player damage row and week-over-week delta_dps can't compute for them. This
script pulls each historical report's DamageDone table from WCL and writes the missing
rows, mirroring how the live pipeline derives DPS:

    dps    = total_dmg / Σ(kill durations in seconds)        # same denominator as the HTML
    uptime = active_time_ms / Σ(fight ms) * 100              # WCL activeTime
    pct_raid = total_dmg / raid_total * 100

SOURCE NOTE: the live pipeline overrides total_dmg with the COMBAT LOG value when a log is
present (logs include some add/range damage WCL's kill-scoped table excludes). This backfill
uses the WCL table only — so a week backfilled here is WCL-sourced, while a week run with a
log is combat-log-sourced. They diverge slightly; the seam is at the first log↔WCL boundary.
Fully-logged consecutive weeks (the normal cadence) are internally consistent.

Usage:
  python scripts\backfill_dps.py                 # gap-fill prod DB (cache/raid_history.db)
  python scripts\backfill_dps.py --test-db       # gap-fill cache/raid_history_test.db
  python scripts\backfill_dps.py --force         # rewrite EVERY week's dps row (overwrite)
  python scripts\backfill_dps.py --code XXXX     # only this report code
"""

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# Importing the main module also auto-loads .env (see its module top), so WCL_CLIENT_* resolve.
from wcl_auto_dashboard import get_token, gql, Q_REPORT, Q_DAMAGE_TABLE, parse_damage_table
from db_writer import SCHEMA, DB_PATH, DB_PATH_TEST
import os


def backfill_week(token: str, con: sqlite3.Connection, code: str, roles: dict) -> int:
    """Fetch one report's DamageDone table and upsert its dps rows. Returns rows written."""
    rep = gql(token, Q_REPORT, {"code": code})["reportData"]["report"]
    kills = [f for f in rep["fights"] if f.get("kill")]
    if not kills:
        print(f"    {code}: no kills in report — skipped")
        return 0
    fight_ids      = [f["id"] for f in kills]
    total_fight_ms = sum(f["endTime"] - f["startTime"] for f in kills)
    dur_s          = total_fight_ms / 1000.0

    dmg   = gql(token, Q_DAMAGE_TABLE, {"code": code, "fightIDs": fight_ids})
    stats = parse_damage_table(dmg["reportData"]["report"]["table"])
    raid_total = sum(s.get("total_dmg", 0) for s in stats.values()) or 0

    n = 0
    for name, s in stats.items():
        total = s.get("total_dmg", 0) or 0
        if total <= 0:
            continue
        dps    = round(total / dur_s, 2) if dur_s else 0
        uptime = round(s.get("active_time_ms", 0) / total_fight_ms * 100, 1) if total_fight_ms else None
        pct    = round(total / raid_total * 100, 2) if raid_total else 0
        con.execute("""
            INSERT OR REPLACE INTO dps (report_code, player, role, dps, total, pct_raid, uptime)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (code, name, roles.get(name), dps, total, pct, uptime))
        n += 1
    return n


def main():
    ap = argparse.ArgumentParser(description="Backfill the dps history table from WCL")
    ap.add_argument("--test-db", action="store_true", help="target raid_history_test.db")
    ap.add_argument("--force",   action="store_true", help="rewrite weeks that already have dps rows")
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
    con.executescript(SCHEMA)   # ensures the dps table exists on older DBs

    # Which weeks need backfilling?
    weeks = con.execute("SELECT report_code, date FROM weeks ORDER BY start_ms").fetchall()
    have  = {r[0] for r in con.execute("SELECT DISTINCT report_code FROM dps").fetchall()}
    todo  = []
    for code, date in weeks:
        if args.code and code != args.code:
            continue
        if code in have and not args.force:
            print(f"  • {code} ({date}) — already has dps rows, skipping (use --force to redo)")
            continue
        todo.append((code, date))

    if not todo:
        print("Nothing to backfill.")
        con.close()
        return

    print(f"Backfilling {len(todo)} week(s) into {Path(db_path).name} …")
    token = get_token(cid, secret)
    print("  ✓ WCL token obtained")

    total_rows = 0
    for code, date in todo:
        print(f"  → {code} ({date})")
        roles = {r[0]: r[1] for r in con.execute(
            "SELECT player, role FROM roster WHERE report_code=?", (code,)).fetchall()}
        try:
            written = backfill_week(token, con, code, roles)
            con.commit()
            total_rows += written
            print(f"    ✓ {written} player rows written")
        except Exception as e:
            con.rollback()
            print(f"    ⚠ failed: {e}")

    con.close()
    print(f"\nDone — {total_rows} dps rows across {len(todo)} week(s) → {Path(db_path).name}")


if __name__ == "__main__":
    main()
