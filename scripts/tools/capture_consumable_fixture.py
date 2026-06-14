"""Capture a REAL COMBATANT_INFO payload as a hermetic golden fixture for the consumable pipeline
(tests/test_consumable_golden.py). This is the 'bless' step: it pulls live pull-time auras for a
curated player set across all kills, trims to {ability, name, source}, and records the EXPECTED
best-of-night consumable output (classify_pull_auras → merge_pull_consumables). The recorded expected
values must be HUMAN-VERIFIED against ground truth before committing (see the session that found the
first-pull-sampling + Anniversary-rename bugs). Re-run to refresh the golden after a real change.

    python scripts/tools/capture_consumable_fixture.py [REPORT] [Player ...]

Requires WCL creds. Writes tests/fixtures/combatant_info_golden.json. NOT imported by the pipeline."""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from wcl_client import get_token, gql                                  # noqa: E402
from wcl_fetchers import classify_pull_auras, merge_pull_consumables   # noqa: E402

REPORT  = sys.argv[1] if len(sys.argv) > 1 else "J4Ba1j6VAPDmqCFp"
# Curated to exercise every tricky case: best-of-night flask+food (Blunderdin), bare-name flask
# (Feelsbaldman = Supreme Power), renamed elixir + late flask (Alldorin = Major Shadow Power + Pure
# Death), two renamed elixirs (Moojerked), a genuine single-elixir FAIL (Philliam), and a mage +
# priest (Zetla, Shieldwagon) whose self-cast raid buffs exercise the canary's SELF_BUFF_IGNORE.
PLAYERS = sys.argv[2:] or ["Blunderdin", "Feelsbaldman", "Alldorin", "Moojerked", "Philliam",
                           "Zetla", "Shieldwagon"]
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..",
                   "tests", "fixtures", "combatant_info_golden.json")

Q_F = """query($code:String!){ reportData{ report(code:$code){
  masterData{ actors(type:"Player"){ id name } }
  fights(killType:Kills){ id startTime endTime name } }}}"""
Q_CI = """query($code:String!,$s:Float!,$e:Float!){ reportData{ report(code:$code){
  events(startTime:$s,endTime:$e,filterExpression:"type='combatantinfo'",limit:100){ data } }}}"""

tok = get_token(os.environ["WCL_CLIENT_ID"], os.environ["WCL_CLIENT_SECRET"])
rep = gql(tok, Q_F, {"code": REPORT})["reportData"]["report"]
actors = {a["id"]: a["name"] for a in rep["masterData"]["actors"]}
want = set(PLAYERS)

players = {p: {"pulls": []} for p in PLAYERS}
for f in rep["fights"]:
    ev = gql(tok, Q_CI, {"code": REPORT, "s": float(f["startTime"]), "e": float(f["endTime"])})
    for e in ev["reportData"]["report"]["events"]["data"]:
        nm = actors.get(e.get("sourceID", -1))
        if nm not in want:
            continue
        gear = e.get("gear", []) or []
        players[nm]["pulls"].append({
            "fight": f["name"],
            "sourceID": e.get("sourceID"),
            "weapon_oil": any((it.get("temporaryEnchant") or 0) for it in gear if isinstance(it, dict)),
            "auras": [{"ability": a.get("ability"), "name": a.get("name", ""), "source": a.get("source")}
                      for a in (e.get("auras") or []) if isinstance(a, dict)],
        })

# Record the EXPECTED best-of-night output (the golden) — verify by eye before committing.
for nm, p in players.items():
    per_pull = []
    for pull in p["pulls"]:
        c = classify_pull_auras(pull["auras"])
        c["weapon_oil"] = pull["weapon_oil"]
        per_pull.append(c)
    p["expected"] = merge_pull_consumables(per_pull)

doc = {
    "_meta": {
        "report": REPORT,
        "source": "live WCL COMBATANT_INFO, all kills",
        "note": "Real pull-time auras (trimmed to ability/name/source). `expected` = best-of-night "
                "classify_pull_auras+merge_pull_consumables, HUMAN-VERIFIED against ground truth. "
                "Re-bless with scripts/tools/capture_consumable_fixture.py. Hermetic golden — no network.",
    },
    "players": players,
}
os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, "w", encoding="utf-8") as fh:
    json.dump(doc, fh, indent=1, ensure_ascii=False, sort_keys=True)
print("wrote", os.path.normpath(OUT))
for nm, p in players.items():
    print(f"  {nm:13} pulls={len(p['pulls']):2}  expected={json.dumps(p['expected'])}")
