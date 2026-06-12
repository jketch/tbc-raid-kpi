"""Probe avoidable-mechanic spell IDs from a WCL report (zero-write, read-only).

Purpose
-------
`docs/TBC_RAID_MECHANICS.md` §8 carries a table of VERIFIED avoidable-damage spell IDs, but a
number of mechanics are still marked **(ID unverified)** because no primary source listed a clean
ID (M'uru's whole kit, Mother Shahraz Fatal Attraction, several Kael/Illidan abilities, …). This
tool reads the live truth straight from a kill log: for each boss kill in a report it pages
`events(dataType: DamageTaken)`, keeps only the events whose TARGET is a raid player, and
aggregates by `abilityGameID` → who hit the raid, how hard, how many players. That ranked list IS
the candidate "who ate the mechanic" ID map — promote the rows you recognise into §8.

This is the same discipline that built `DEBUFF_SLOTS`: confirm the ID against live data, never
guess. It writes NOTHING (no DB, no HTML, no cache) — pure recon, like a `--dry-run`.

Usage
-----
    python scripts\probe_mechanic_ids.py REPORTCODE                 # all kills, top 25 abilities each
    python scripts\probe_mechanic_ids.py REPORTCODE --boss Brutallus # one encounter (name substring)
    python scripts\probe_mechanic_ids.py REPORTCODE --top 40         # deeper list per boss
    python scripts\probe_mechanic_ids.py REPORTCODE --min-players 3  # only abilities that hit >=3 players
    python scripts\probe_mechanic_ids.py REPORTCODE --json out.json  # also dump structured JSON
    python scripts\probe_mechanic_ids.py REPORTCODE --include-trash  # include non-kill fights too

Notes
-----
- WCL-durable by construction: needs only the API (no combat log), per the WCL-Durability Principle.
- Self-damage / environment is filtered out (source must be an enemy NPC, target must be a Player).
- Pets are excluded from the "players hit" tally (target type Player only) so the count reads cleanly.
- hitType is surfaced per ability (1 hit / 2 crit / 4 block / 15 crush; 0/7/8 = miss/dodge/parry) so
  you can tell a binary-resist nuke from a physical tank hit.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict

# stdout UTF-8 (Windows cp1252 console chokes on ✓/▲/box glyphs) — mirrors the main pipeline.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# Run as `python scripts\probe_mechanic_ids.py ...` — make the sibling wcl_client importable.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wcl_client import get_token, gql  # noqa: E402


# ──────────────────────────────────────────────────────────────────────────────
# .env loading (WCL_CLIENT_ID / WCL_CLIENT_SECRET) — search the usual spots.
# ──────────────────────────────────────────────────────────────────────────────
def load_env() -> tuple[str, str]:
    cid = os.environ.get("WCL_CLIENT_ID")
    csec = os.environ.get("WCL_CLIENT_SECRET")
    if cid and csec:
        return cid, csec
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "..", ".env"),          # repo-root/.env (most likely)
        os.path.join(here, "..", "..", ".env"),     # Gaming/.env (per CLAUDE.md)
        os.path.join(os.getcwd(), ".env"),
    ]
    env: dict[str, str] = {}
    for path in candidates:
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    env[k.strip()] = v.strip().strip('"').strip("'")
            break
    cid = cid or env.get("WCL_CLIENT_ID")
    csec = csec or env.get("WCL_CLIENT_SECRET")
    if not (cid and csec):
        sys.exit("ERROR: WCL_CLIENT_ID / WCL_CLIENT_SECRET not found in env or a nearby .env file.")
    return cid, csec


# ──────────────────────────────────────────────────────────────────────────────
# GraphQL
# ──────────────────────────────────────────────────────────────────────────────
Q_META = """
query ($code: String!) {
  reportData {
    report(code: $code) {
      title
      fights(killType: All) { id name kill startTime endTime encounterID difficulty }
      masterData {
        actors { id name type petOwner }
        abilities { gameID name type }
      }
    }
  }
}
"""

Q_DT_EVENTS = """
query ($code: String!, $fight: Int!, $start: Float!, $end: Float!) {
  reportData {
    report(code: $code) {
      events(fightIDs: [$fight], dataType: DamageTaken, startTime: $start, endTime: $end, limit: 10000) {
        data
        nextPageTimestamp
      }
    }
  }
}
"""


def fetch_meta(token: str, code: str) -> dict:
    rep = gql(token, Q_META, {"code": code})["reportData"]["report"]
    return rep


def fetch_damage_taken(token: str, code: str, fight: dict) -> list[dict]:
    """Page all DamageTaken events for one fight window."""
    out: list[dict] = []
    start = float(fight["startTime"])
    end = float(fight["endTime"])
    cursor = start
    pages = 0
    while True:
        pages += 1
        if pages > 200:  # hard safety cap — a single fight should never need this many pages
            print(f"    (page cap hit on fight {fight['id']}; stopping)")
            break
        res = gql(token, Q_DT_EVENTS, {"code": code, "fight": fight["id"], "start": cursor, "end": end})
        block = res["reportData"]["report"]["events"]
        data = block.get("data") or []
        out.extend(data)
        nxt = block.get("nextPageTimestamp")
        if not nxt or nxt <= cursor:
            break
        cursor = float(nxt)
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Aggregation
# ──────────────────────────────────────────────────────────────────────────────
HITTYPE = {0: "miss", 1: "hit", 2: "crit", 3: "absorb", 4: "block", 5: "block", 6: "absorb",
           7: "dodge", 8: "parry", 10: "immune", 14: "partial", 15: "crush"}


def aggregate_fight(events: list[dict], player_ids: set[int], abil_name: dict[int, str]) -> list[dict]:
    """Reduce DamageTaken events (player targets only) to per-ability rows."""
    by_ability: dict[int, dict] = {}
    for ev in events:
        tid = ev.get("targetID")
        if tid not in player_ids:           # keep only damage TAKEN BY raid players
            continue
        aid = ev.get("abilityGameID")
        if aid is None:
            continue
        row = by_ability.get(aid)
        if row is None:
            row = by_ability[aid] = {
                "id": aid,
                "name": abil_name.get(aid, f"#{aid}"),
                "dmg": 0,
                "events": 0,
                "players": set(),
                "hittypes": defaultdict(int),
                "max_hit": 0,
            }
        amt = ev.get("amount", 0) or 0
        row["dmg"] += amt
        row["events"] += 1
        row["players"].add(tid)
        row["max_hit"] = max(row["max_hit"], amt)
        ht = ev.get("hitType")
        if ht is not None:
            row["hittypes"][HITTYPE.get(ht, str(ht))] += 1
    rows = list(by_ability.values())
    for r in rows:
        r["players_hit"] = len(r["players"])
        r["players"] = sorted(r["players"])
        r["hittypes"] = dict(r["hittypes"])
    rows.sort(key=lambda r: (r["players_hit"], r["dmg"]), reverse=True)
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description="Probe avoidable-mechanic spell IDs from a WCL report.")
    ap.add_argument("report", help="WCL report code")
    ap.add_argument("--boss", help="only this encounter (case-insensitive name substring)")
    ap.add_argument("--top", type=int, default=25, help="abilities to show per boss (default 25)")
    ap.add_argument("--min-players", type=int, default=1, help="only abilities that hit >= N players")
    ap.add_argument("--include-trash", action="store_true", help="include non-kill fights")
    ap.add_argument("--json", metavar="PATH", help="also dump structured results to this JSON file")
    args = ap.parse_args()

    cid, csec = load_env()
    print(f"Authenticating against fresh.warcraftlogs.com …")
    token = get_token(cid, csec)
    print(f"Fetching report {args.report} …")
    rep = fetch_meta(token, args.report)
    print(f"Report: {rep.get('title')!r}\n")

    md = rep["masterData"]
    player_ids = {a["id"] for a in md["actors"] if a.get("type") == "Player"}
    abil_name = {a["gameID"]: a["name"] for a in md["abilities"]}

    fights = rep["fights"]
    if not args.include_trash:
        fights = [f for f in fights if f.get("kill")]
    if args.boss:
        needle = args.boss.lower()
        fights = [f for f in fights if needle in (f.get("name") or "").lower()]
    if not fights:
        sys.exit("No matching fights (try --include-trash, or check the --boss name).")

    # Group fights by boss name (a boss may have several pull attempts; we probe kills by default).
    by_boss: dict[str, list[dict]] = defaultdict(list)
    for f in fights:
        by_boss[f.get("name") or f"encounter {f.get('encounterID')}"].append(f)

    dump: dict[str, list[dict]] = {}
    for boss, bfights in by_boss.items():
        enc = bfights[0].get("encounterID")
        print("=" * 78)
        print(f"  {boss}   (encounterID {enc}, {len(bfights)} fight(s))")
        print("=" * 78)
        merged: dict[int, dict] = {}
        for f in bfights:
            evs = fetch_damage_taken(token, args.report, f)
            for r in aggregate_fight(evs, player_ids, abil_name):
                m = merged.get(r["id"])
                if m is None:
                    merged[r["id"]] = r
                    r["players"] = set(r["players"])
                else:
                    m["dmg"] += r["dmg"]
                    m["events"] += r["events"]
                    m["max_hit"] = max(m["max_hit"], r["max_hit"])
                    m["players"] = set(m["players"]) | set(r["players"])
                    for k, v in r["hittypes"].items():
                        m["hittypes"][k] = m["hittypes"].get(k, 0) + v
        rows = list(merged.values())
        for r in rows:
            r["players_hit"] = len(r["players"])
            r["players"] = sorted(r["players"])
        rows.sort(key=lambda r: (r["players_hit"], r["dmg"]), reverse=True)
        rows = [r for r in rows if r["players_hit"] >= args.min_players][: args.top]

        print(f"  {'spell ID':>9}  {'players':>7}  {'events':>6}  {'total dmg':>11}  {'max hit':>8}  "
              f"ability  ·  hitTypes")
        print(f"  {'-'*9}  {'-'*7}  {'-'*6}  {'-'*11}  {'-'*8}  {'-'*40}")
        for r in rows:
            ht = ", ".join(f"{k}:{v}" for k, v in sorted(r["hittypes"].items(), key=lambda x: -x[1]))
            print(f"  {r['id']:>9}  {r['players_hit']:>7}  {r['events']:>6}  {r['dmg']:>11,}  "
                  f"{r['max_hit']:>8,}  {r['name']}  ·  {ht}")
        print()
        dump[boss] = [
            {k: r[k] for k in ("id", "name", "players_hit", "events", "dmg", "max_hit", "hittypes")}
            for r in rows
        ]

    print("Reading guide for §8: an ability that hits MANY players for real damage (not a tank-only")
    print("physical hit) is your avoidable-mechanic candidate. Cross-check the ID + name, then promote")
    print("it into docs/TBC_RAID_MECHANICS.md §8 (replace the matching (ID unverified) row).")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"report": args.report, "title": rep.get("title"), "bosses": dump}, fh, indent=2)
        print(f"\nWrote structured results → {args.json}")


if __name__ == "__main__":
    main()
