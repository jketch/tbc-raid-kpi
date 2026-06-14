"""Consumable-recognition audit (read-only). For a new content tier, run this against a kill report
to (1) surface any consumable buff-aura the recognition lists DON'T yet catch, and (2) sanity-check
per-player flask/elixir detection. Add any genuine "—UNRECOGNIZED—" flask/elixir IDs to
game_constants (FLASK_AURA_IDS / ELIXIR_AURA_IDS / FLASK_EFFECT_NAMES). No DB/HTML writes.

    python scripts/tools/probe_consumable_ids.py [REPORT ...]
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from wcl_client import get_token, gql                                    # noqa: E402
from wcl_fetchers import classify_pull_auras, merge_pull_consumables      # noqa: E402

REPORTS = sys.argv[1:] or ["J4Ba1j6VAPDmqCFp", "PqynTVBF67pN3Gtg"]
N_FIGHTS = 6

Q_FIGHTS = """
query($code:String!){ reportData{ report(code:$code){
  masterData{ actors(type:"Player"){ id name } }
  fights(killType:Kills){ id startTime endTime name }
}}}"""
Q_CI = """
query($code:String!,$s:Float!,$e:Float!){ reportData{ report(code:$code){
  events(startTime:$s,endTime:$e,filterExpression:"type='combatantinfo'",limit:100){ data }
}}}"""

def recognized(aura):
    """How a single aura classifies under the live recognition logic: flask/elixir/food/scroll/None."""
    c = classify_pull_auras([aura])
    if c["flask"]:   return "flask:" + c["flask"]
    if c["elixirs"]: return "elixir:" + c["elixirs"][0]
    if c["food"]:    return "food"
    if c["scrolls"]: return "scroll"
    return None

tok = get_token(os.environ["WCL_CLIENT_ID"], os.environ["WCL_CLIENT_SECRET"])
seen = {}        # (id, name) -> set(players)
per_player = {}  # report -> {player: best-of-night merged consumable dict}
for code in REPORTS:
    rep = gql(tok, Q_FIGHTS, {"code": code})["reportData"]["report"]
    actors = {a["id"]: a["name"] for a in rep["masterData"]["actors"]}
    pulls = {}  # who -> [per-pull classify dict] — aggregated best-of-night (matches production)
    for f in rep["fights"][:N_FIGHTS]:
        ev = gql(tok, Q_CI, {"code": code, "s": float(f["startTime"]), "e": float(f["endTime"])})
        for e in ev["reportData"]["report"]["events"]["data"]:
            who = actors.get(e.get("sourceID", -1), str(e.get("sourceID")))
            auras = e.get("auras") or []
            for a in auras:
                if isinstance(a, dict):
                    seen.setdefault((a.get("ability"), a.get("name", "")), set()).add(who)
            pulls.setdefault(who, []).append(classify_pull_auras(auras))
    per_player[code] = {who: merge_pull_consumables(cs) for who, cs in pulls.items()}

# 1) distinct auras, recognized vs not — the gap finder
print("=== distinct pull auras (recognized vs not) ===")
print(f"{'spellID':>8}  {'aura name':30}  {'recognized as':28}  nplayers")
print("-" * 90)
for (sid, name), players in sorted(seen.items(), key=lambda kv: (recognized(kv[0][1]) is not None, str(kv[0][0]))):
    print(f"{str(sid):>8}  {name:30}  {str(recognized({'ability': sid, 'name': name})):28}  {len(players)}")

# 2) per-player flask/elixir summary — the verification
print("\n=== per-player flask / elixir detection ===")
for code, players in per_player.items():
    print("---", code)
    for who in sorted(players):
        c = players[who]
        passes = bool(c["flask"]) or len(c["elixirs"]) >= 2
        print(f"  {'PASS' if passes else 'fail':4} {who:14} flask={c['flask']!r:34} elixirs={c['elixirs']}")
