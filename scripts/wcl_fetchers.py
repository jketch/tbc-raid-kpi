"""wcl_fetchers.py — the WCL v2 GraphQL fetch layer (extracted from wcl_auto_dashboard).

Every fetch_*/parse_* function that turns (token, report_code, fights) into structured
per-player dicts, plus the shared GraphQL query strings, the alias decoder, the pagination
cap, and the one-shot masterData superset (`fetch_master_data` — consumers take `md` maps,
never re-query). Moving in cluster-by-cluster (verbatim) across several commits.

Imports stay EXPLICIT (no `from game_constants import *`) so ruff's F821 undefined-name
check stays live here — it is the net that catches a moved function whose constant didn't
move with it. wcl_auto_dashboard re-exports everything, so W.<name> resolves unchanged.
"""
from __future__ import annotations

import json
from collections import defaultdict

from wcl_client import gql
from paths import ITEM_META_CACHE
from game_constants import FOOD_BUFF, ELIXIR_BUFFS, HEAL_MANA_COST
from roles import _fight_role

# Hard cap on paginated WCL event queries — guards against a runaway non-null nextPageTimestamp
# (a known WCL quirk the older loops defend against). A full clear is ~10-20 pages; 40 is slack.
MAX_EVENT_PAGES = 40


def _loads_alias(t):
    """Decode a WCL alias/table blob that may arrive as a JSON string or already-parsed
    object. Returns {} on a malformed blob so one bad fight-alias can't abort a whole
    batched query (the per-fight loops iterate many aliases from a single response)."""
    if isinstance(t, str):
        try:
            return json.loads(t)
        except (ValueError, TypeError):
            return {}
    return t or {}


# ══════════════════════════════════════════════════════════════════════════════
# WCL API helpers
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
# GraphQL query strings
# ══════════════════════════════════════════════════════════════════════════════

Q_REPORT = """
query GetReport($code: String!) {
  reportData {
    report(code: $code) {
      title
      startTime
      endTime
      zone { name id }
      fights(killType: Kills) {
        id name startTime endTime kill difficulty encounterID
      }
    }
  }
}
"""

Q_PLAYER_DETAILS = """
query GetPlayers($code: String!, $fightIDs: [Int]) {
  reportData {
    report(code: $code) {
      playerDetails(fightIDs: $fightIDs)
    }
  }
}
"""

Q_DAMAGE_TABLE = """
query GetDmgTable($code: String!, $fightIDs: [Int]) {
  reportData {
    report(code: $code) {
      table(dataType: DamageDone, fightIDs: $fightIDs, killType: Kills)
    }
  }
}
"""

Q_BUFFS_TABLE = """
query GetBuffs($code: String!, $fightIDs: [Int]) {
  reportData {
    report(code: $code) {
      table(dataType: Buffs, fightIDs: $fightIDs, killType: Kills, sourceID: -1)
    }
  }
}
"""

# Fetch COMBATANT_INFO events (gear per player at fight start)
Q_COMBATANT_INFO = """
query GetGear($code: String!, $startTime: Float!, $endTime: Float!) {
  reportData {
    report(code: $code) {
      events(
        startTime: $startTime
        endTime: $endTime
        filterExpression: "type='combatantinfo'"
        limit: 100
      ) {
        data
      }
    }
  }
}
"""

# Damage events sampled for hit-type counting (actual crit %)
Q_DAMAGE_EVENTS = """
query GetDmgEvents($code: String!, $fightIDs: [Int], $startTime: Float!, $endTime: Float!) {
  reportData {
    report(code: $code) {
      events(
        dataType: DamageDone
        fightIDs: $fightIDs
        startTime: $startTime
        endTime: $endTime
        limit: 10000
        hostilityType: Friendlies
      ) {
        data
        nextPageTimestamp
      }
    }
  }
}
"""

# ══════════════════════════════════════════════════════════════════════════════
# Parse damage table for actual crit %
# ══════════════════════════════════════════════════════════════════════════════

def parse_damage_table(raw_table) -> dict[str, dict]:
    """
    Extract per-player crit stats from the damage table blob.
    Returns { player_name: { actual_crit_pct, total_dmg, active_time_ms } }
    """
    result = {}
    try:
        if isinstance(raw_table, str):
            raw_table = json.loads(raw_table)
        entries = raw_table.get("data", {}).get("entries", [])
        for e in entries:
            name  = e.get("name", "")
            total = e.get("total", 0)
            # WCL uses different field names across versions — try all known variants
            hit  = e.get("hitCount",  e.get("hits",  e.get("normalHits",  0)))
            crit = e.get("critCount", e.get("crits", e.get("criticalHits", 0)))
            active_time = e.get("activeTime", e.get("activeTimeReduced", 0))
            denom = hit + crit
            actual_pct = round((crit / denom * 100), 1) if denom > 0 else 0.0
            result[name] = {
                "actual_crit_pct": actual_pct,
                "total_dmg":       total,
                "active_time_ms":  active_time,
                "hit_count":       hit,
                "crit_count":      crit,
            }
    except Exception as e:
        print(f"  Warning: could not parse damage table: {e}")
    return result


def fetch_gear_from_events(token: str, report_code: str, fights: list,
                           actors: list | None = None) -> dict[str, list]:
    """
    Pull COMBATANT_INFO events and return per-player gear crit rating shortcut.
    WCL exposes critMelee/critRanged/critSpell directly — no item lookup needed.
    Returns { player_name: [{"_crit_melee": N, "_crit_spell": N, "_crit_ranged": N, "gear": [...]}] }
    """
    if not fights:
        return {}
    # Build sourceID → name map from actors list
    id_to_name = {}
    if actors:
        for a in actors:
            if a.get("type") == "Player":
                id_to_name[a["id"]] = a["name"]

    first = fights[0]
    try:
        data = gql(token, Q_COMBATANT_INFO, {
            "code": report_code,
            "startTime": float(first["startTime"]),
            "endTime":   float(first["endTime"]),
        })
        events = data["reportData"]["report"]["events"]["data"]
        print(f"  Found {len(events)} combatantinfo events")
        if events:
            print(f"  Sample combatantinfo keys: {list(events[0].keys())}")
            print(f"  Sample gear field: {str(events[0].get('gear','MISSING'))[:200]}")
        gear_map = {}
        ci_consumables = {}
        for ev in events:
            sid  = ev.get("sourceID", -1)
            name = id_to_name.get(sid, str(sid))
            gear_map[name] = [{
                "_crit_melee":  ev.get("critMelee",  0),
                "_crit_spell":  ev.get("critSpell",  0),
                "_crit_ranged": ev.get("critRanged", 0),
                "_agility":     ev.get("agility",   0),   # primary-stat crit (melee/ranged)
                "_intellect":   ev.get("intellect", 0),   # primary-stat crit (spell)
                "gear":         ev.get("gear", []),
            }]
            # Pull-time auras are the accurate consumable source — they include
            # flasks/elixirs/food applied before the log started (no aura event).
            # Captures the FULL raid-buff spread for the consumable audit, not just y/n.
            flask_name, fd = "", False
            elx, scr = set(), set()
            for a in (ev.get("auras") or []):
                bn = a.get("name", "") if isinstance(a, dict) else ""
                if   bn.startswith("Flask of"):    flask_name = bn
                elif bn == FOOD_BUFF:              fd = True
                elif bn.startswith("Elixir of") or bn in ELIXIR_BUFFS: elx.add(bn)
                elif bn.startswith("Scroll of"):   scr.add(bn)
            # Weapon oil / sharpening stone = a TEMPORARY weapon enchant. Only weapons can
            # carry one, so any item with temporaryEnchant means they oiled/stoned a weapon.
            gear_items = ev.get("gear", []) or []
            wpn_enchanted = any((it.get("temporaryEnchant") or 0)
                                for it in gear_items if isinstance(it, dict))
            ci_consumables[name] = {"flask": flask_name, "food": fd,
                                    "elixirs": sorted(elx), "scrolls": sorted(scr),
                                    "weapon_oil": wpn_enchanted}
        gear_map["__consumables__"] = ci_consumables
        return gear_map
    except Exception as e:
        print(f"  Warning: combatantinfo fetch failed: {e}")
        return {}


def fetch_actual_crit(token: str, report_code: str, fights: list,
                      crit_track_ids: set | None = None) -> dict[str, dict]:
    """
    Page through damage events across all kill fights and count hits vs crits per player.
    hitType: 1=normal, 2=crit, 4=absorb, 8=blocked, 16=glancing, 32=dodge, 64=parry
    Returns { sourceID: { name, hits, crits, [sb_crit] } }

    crit_track_ids: an optional set of abilityGameIDs whose biggest single CRIT hit to record
    per player (free — reuses these same pages). Powers the warlock "biggest Shadow Bolt crit"
    toolkit metric. Stored as `sb_crit` on the per-source dict.
    """
    if not fights:
        return {}
    track = crit_track_ids or set()

    start = float(min(f["startTime"] for f in fights))
    end   = float(max(f["endTime"]   for f in fights))
    fight_ids = [f["id"] for f in fights]

    counts: dict[int, dict] = {}   # { sourceID: {name, hits, crits} }
    next_ts = start
    pages = 0

    print("  Fetching damage events for crit counting (this may take a moment)...")
    while next_ts is not None and pages < MAX_EVENT_PAGES:
        data = gql(token, Q_DAMAGE_EVENTS, {
            "code": report_code,
            "fightIDs": fight_ids,
            "startTime": next_ts,
            "endTime": end,
        })
        result  = data["reportData"]["report"]["events"]
        events  = result.get("data", [])
        next_ts = result.get("nextPageTimestamp")
        pages  += 1

        for ev in events:
            sid      = ev.get("sourceID", -1)
            hit_type = ev.get("hitType", 0)
            if sid < 0:
                continue
            # DOT / periodic ticks can't crit in TBC (Corruption, SW:P, Rend, Immolate…).
            # Counting them as non-crit hits tanks DOT-caster crit rates — exclude them so
            # "actual crit" reflects only crit-capable direct casts.
            if ev.get("tick"):
                continue
            if sid not in counts:
                counts[sid] = {"name": str(sid), "hits": 0, "crits": 0}
            if hit_type == 2:
                counts[sid]["crits"] += 1
                # biggest single crit of a tracked ability (warlock Shadow Bolt brag number)
                if track and ev.get("abilityGameID") in track:
                    amt = ev.get("amount", 0) or 0
                    if amt > counts[sid].get("sb_crit", 0):
                        counts[sid]["sb_crit"] = amt
            elif hit_type == 1:
                counts[sid]["hits"]  += 1

        if not next_ts:
            break

    if next_ts is not None:
        print(f"  ⚠ crit counting hit the {MAX_EVENT_PAGES}-page cap with more events "
              f"remaining — crit/luck for this week may be undercounted")
    print(f"  Processed {pages} page(s) of damage events")
    return counts


def merge_actor_names(counts: dict, actors: list) -> dict[str, dict]:
    """Map sourceID counts back to player names using the masterData actors list."""
    id_to_name = {a["id"]: a["name"] for a in actors if a.get("type") == "Player"}
    result = {}
    for sid, data in counts.items():
        name = id_to_name.get(sid, str(sid))
        result[name] = data
        result[name]["name"] = name
    return result


def fetch_damage_by_selection(token: str, report_code: str) -> dict:
    """Per-player damage + active time for All / Bosses / Trash, mirroring the WCL DamageDone
    table: DPS = damage ÷ THAT selection's own fight time, uptime = activeTime ÷ that time.
    (The old single number divided whole-report damage by boss-only time — an inflated, mixed
    denominator.) Bosses = kill fights; Trash = fights with no encounterID; All = everything.
    Returns {durations:{all,boss,trash} (sec), players:{name:{all:{total,active},boss,trash}}}."""
    try:
        fights = gql(token, """query($c:String!){reportData{report(code:$c){
            fights{ id kill encounterID startTime endTime }}}}""",
            {"c": report_code})["reportData"]["report"]["fights"]
    except Exception as ex:
        print(f"  Warning: damage-by-selection fights failed: {ex}")
        return {}
    if not fights:
        return {}
    durms = {f["id"]: (f["endTime"] - f["startTime"]) for f in fights}
    sels = {
        "all":   [f["id"] for f in fights],
        "boss":  [f["id"] for f in fights if f.get("kill")],
        "trash": [f["id"] for f in fights if not f.get("encounterID")],
    }
    def table(ids):
        if not ids:
            return {}
        try:
            t = gql(token, """query($c:String!,$f:[Int]){reportData{report(code:$c){
                table(dataType: DamageDone, fightIDs:$f)}}}""",
                {"c": report_code, "f": ids})["reportData"]["report"]["table"]
            if isinstance(t, str):
                t = json.loads(t)
            return {e["name"]: {"total": e.get("total", 0), "active": e.get("activeTime", 0)}
                    for e in t.get("data", {}).get("entries", [])}
        except Exception as ex:
            print(f"  Warning: damage-by-selection table failed: {ex}")
            return {}
    data = {k: table(v) for k, v in sels.items()}
    durations = {k: round(sum(durms[i] for i in v) / 1000) for k, v in sels.items()}
    names = set().union(*(set(d) for d in data.values())) if data else set()
    players = {nm: {k: data[k].get(nm, {"total": 0, "active": 0}) for k in sels} for nm in names}
    print(f"  ✓ damage by selection: all={durations['all']}s boss={durations['boss']}s "
          f"trash={durations['trash']}s")
    return {"durations": durations, "players": players}


def fetch_master_data(token: str, report_code: str) -> dict:
    """ONE masterData fetch for the whole run. The actor/ability maps are needed by ~7 places
    (crit attribution, tank biggest-hit labels, class toolkit, mana returns, sunder, death
    recaps, ability icons) — each used to re-query masterData independently (#9 in the code
    review). This pulls the SUPERSET once — every actor (id/name/type/subType/petOwner, so pet
    energizes resolve to their owner) and every ability (gameID/name/icon) — and returns the
    precomputed lookups so consumers take maps, not a token.

    Returns a dict:
      actors    raw actor list (all types)        players  actors filtered to type==Player
      abilities raw ability list                  id2name  {actorID: name}  (all actors)
      name2id   {name: actorID}                   acts     {actorID: actor dict}  (carries petOwner)
      gid2name  {abilityGameID: name}             icons    {ability name: icon-slug}  (first wins)
    """
    md = gql(token, """query($c:String!){reportData{report(code:$c){masterData{
        actors{ id name type subType petOwner }
        abilities{ gameID name icon } }}}}""",
             {"c": report_code})["reportData"]["report"]["masterData"]
    actors    = md.get("actors") or []
    abilities = md.get("abilities") or []
    icons = {}
    for a in abilities:
        nm, ic = a.get("name"), a.get("icon")
        if nm and ic and nm not in icons:          # first-wins, matches old fetch_ability_icons layer 1
            icons[nm] = ic.replace(".jpg", "")
    return {
        "actors":    actors,
        "players":   [a for a in actors if a.get("type") == "Player"],   # == old actors(type:"Player")
        "abilities": abilities,
        "id2name":   {a["id"]: a["name"] for a in actors},
        "name2id":   {a["name"]: a["id"] for a in actors},
        "acts":      {a["id"]: a for a in actors},
        "gid2name":  {a["gameID"]: (a.get("name") or "") for a in abilities if a.get("gameID")},
        "icons":     icons,
    }


# ── Gear readiness audit (Prep) — enchant + gem + item-level compliance ─────────────────
# WCL gear[] (rides on the DamageDone table) gives every equipped item with its enchant, filled gems,
# and item level. Socket COUNT (to find EMPTY sockets) isn't in WCL, so we source `nsockets` from
# wowhead's item XML (cached in cache/item_meta_cache.json — fetched once per item, then free).
# Degrades gracefully: a missing/failed socket lookup just drops that item from the empty-socket tally,
# and the enchant + item-level half is pure WCL (works even if wowhead is unreachable).
# (Socket-count cache path: paths.ITEM_META_CACHE.)
# WCL equipment slot index → name. Enchantable = slots a raider is expected to enchant every week
# (conservative: rings/offhand/ranged are conditional on class/profession → excluded so we never raise
# a false "missing enchant"). Shirt/tabard are excluded from the item-level average.
_GEAR_SLOT = {0: "Head", 1: "Neck", 2: "Shoulder", 3: "Shirt", 4: "Chest", 5: "Waist", 6: "Legs",
              7: "Feet", 8: "Wrist", 9: "Hands", 10: "Ring", 11: "Ring", 12: "Trinket", 13: "Trinket",
              14: "Back", 15: "Main Hand", 16: "Off Hand", 17: "Ranged", 18: "Tabard"}
_ENCHANTABLE_SLOTS = {0, 2, 4, 6, 7, 8, 9, 14, 15}
_ILVL_SKIP_SLOTS   = {3, 18}


def _item_sockets(item_id, cache) -> int:
    """nsockets for item_id from wowhead's item XML (cached in `cache`). None if unknown/unfetchable."""
    key = str(item_id)
    if key in cache:
        return cache[key].get("nsockets")
    if not item_id:
        return None
    try:
        import re as _re
        import requests as _rq
        r = _rq.get(f"https://www.wowhead.com/tbc/item={int(item_id)}?xml", timeout=10,
                    headers={"User-Agent": "Mozilla/5.0"})
        m = _re.search(r'"nsockets":(\d+)', r.text)
        n = int(m.group(1)) if m else 0
        cache[key] = {"nsockets": n}
        return n
    except Exception:
        cache[key] = {"nsockets": None}
        return None


def fetch_gear_audit(token: str, report_code: str, kills: list) -> dict:
    """Per-raider gear-readiness audit (Prep tier): item level + enchant compliance + gem/empty-socket
    compliance. Gear comes from ONE DamageDone table (gear is identical all night); socket counts from
    wowhead (cached). Pure WCL+wowhead, runs every week, no combat log. {} when there are no kills."""
    if not kills:
        return {}
    big = max(kills, key=lambda f: f["endTime"] - f["startTime"])
    try:
        blob = gql(token,
                   "query($c:String!,$f:Int!){reportData{report(code:$c){"
                   "table(dataType: DamageDone, fightIDs:[$f])}}}",
                   {"c": report_code, "f": int(big["id"])})
        tbl = blob["reportData"]["report"]["table"]
        if isinstance(tbl, str):
            tbl = json.loads(tbl)
        entries = (tbl.get("data") or {}).get("entries") or []
    except Exception as e:
        print(f"  Warning: gear audit fetch failed: {e}")
        return {}
    try:
        cache = json.loads(ITEM_META_CACHE.read_text())
    except Exception:
        cache = {}
    out = []
    for e in entries:
        if e.get("type") == "Pet" or not isinstance(e.get("gear"), list):
            continue
        gear = [it for it in e["gear"] if it.get("id")]
        if not gear:
            continue
        ilvl_items = [it for it in gear
                      if it.get("slot") not in _ILVL_SKIP_SLOTS and it.get("itemLevel")]
        ilvl = round(sum(it["itemLevel"] for it in ilvl_items) / len(ilvl_items)) if ilvl_items else 0
        missing = sorted({_GEAR_SLOT.get(it.get("slot"), str(it.get("slot")))
                          for it in gear
                          if it.get("slot") in _ENCHANTABLE_SLOTS and not it.get("permanentEnchant")})
        gems_filled = sockets_total = empty = 0
        for it in gear:
            filled = len(it.get("gems") or [])
            gems_filled += filled
            ns = _item_sockets(it.get("id"), cache)
            if ns:
                sockets_total += ns
                empty += max(0, ns - filled)
        out.append({
            "name": e.get("name"), "ilvl": ilvl,
            "ench_ok": len(_ENCHANTABLE_SLOTS) - len(missing),
            "ench_total": len(_ENCHANTABLE_SLOTS), "missing_enchants": missing,
            "gems_filled": gems_filled, "sockets_total": sockets_total, "empty_sockets": empty,
        })
    try:
        ITEM_META_CACHE.write_text(json.dumps(cache))
    except Exception:
        pass
    if not out:
        return {}
    avg = round(sum(p["ilvl"] for p in out) / len(out))
    prepped = sum(1 for p in out if not p["missing_enchants"] and not p["empty_sockets"])
    print(f"  ✓ gear audit: {len(out)} raiders, avg ilvl {avg}, {prepped} fully prepped")
    return {"players": out, "raid_avg_ilvl": avg, "fully_prepped": prepped, "total": len(out)}


def fetch_fight_roles(token: str, report_code: str, kills: list):
    """Per-fight role for each player — the fix for spec-swappers (heal early, DPS late;
    or prot on some pulls, ret on others). WCL's aggregate playerDetails collapses a night
    to ONE role; querying per fight + reading each player's per-fight SPEC recovers what
    they actually did each pull.
    Returns ({name: {"Healer":[fid..], "Tank":[fid..], "dps":[fid..]}}, {fid: duration_s})."""
    roles = defaultdict(lambda: {"Healer": [], "Tank": [], "dps": []})
    durs  = {}
    Q = """query($c:String!,$f:Int!){reportData{report(code:$c){
        playerDetails(fightIDs:[$f], killType:Kills)}}}"""
    for f in kills:
        fid = f["id"]
        durs[fid] = (f["endTime"] - f["startTime"]) / 1000.0
        try:
            pd = gql(token, Q, {"c": report_code, "f": fid})["reportData"]["report"]["playerDetails"]
            if isinstance(pd, str):
                pd = json.loads(pd)
            pd = pd.get("data", {}).get("playerDetails", pd.get("data", pd))
        except Exception:
            continue
        for bucket in ("tanks", "healers", "dps"):
            for p in (pd.get(bucket) or []):
                specs = p.get("specs") or []
                spec  = specs[0].get("spec", "") if specs else ""
                roles[p["name"]][_fight_role(spec, bucket)].append(fid)
    return {n: dict(d) for n, d in roles.items()}, durs


def build_fight_roles_from_log(log_roles: dict, kills: list):
    """Convert combat-log per-fight roles (keyed by boss NAME) into the fid-keyed shape
    used downstream, by matching boss names to WCL kills. Free (no API), and the boss-melee
    tank signal is immune to WCL's spec-label quirks. Returns (fight_roles, fight_durs).
    Coverage is reported so the caller can fall back to the API path if names don't line up."""
    name_to_fids = defaultdict(list)
    durs = {}
    for f in kills:
        name_to_fids[f["name"]].append(f["id"])
        durs[f["id"]] = (f["endTime"] - f["startTime"]) / 1000.0
    roles = defaultdict(lambda: {"Healer": [], "Tank": [], "dps": []})
    matched = 0
    for boss, rr in (log_roles or {}).items():
        fids = name_to_fids.get(boss, [])
        if not fids:
            continue
        matched += 1
        for role, names in rr.items():
            for nm in names:
                for fid in fids:
                    roles[nm].setdefault(role, []).append(fid)
    coverage = matched / len(log_roles) if log_roles else 0.0
    return {n: dict(d) for n, d in roles.items()}, durs, coverage


def harden_tank_fights(token: str, report_code: str, kills: list,
                       fight_roles: dict, tank_names: set, batch: int = 5):
    """WCL-durable per-fight tank attribution — the fix for ferals ('Warden') and other
    specs WCL's per-fight playerDetails mislabels, which otherwise drop a real tank from the
    scorecard on log-less (backfilled) weeks. A boss's main-hand 'Melee' auto-attack only ever
    lands on its current target, so boss-melee-taken is a near-pure tank signal. We restrict
    detection to known roster tanks (`tank_names`) — that removes the only false positives the
    raw signal has (a DPS threat-slip or an add's melee on a non-tank) — and we ONLY ADD fights
    (union with the spec path, never remove), so this can't regress today's numbers. The
    combat-log role path already carries this signal, so this runs only on the API fallback.
    Mutates `fight_roles` in place (adds fids to each tank's 'Tank' list, drops them from
    'dps'/'Healer'). Cheap: ~2 batched DamageTaken-table queries."""
    if not tank_names:
        return
    added = 0
    for i in range(0, len(kills), batch):
        chunk = kills[i:i + batch]
        aliases = "\n".join(
            f'f{f["id"]}: table(dataType: DamageTaken, fightIDs:[{int(f["id"])}], hostilityType: Friendlies)'
            for f in chunk)
        Q = f"query($c:String!){{reportData{{report(code:$c){{ {aliases} }}}}}}"
        try:
            rep = gql(token, Q, {"c": report_code})["reportData"]["report"]
        except Exception as ex:
            print(f"  Warning: tank-harden DamageTaken batch failed: {ex}")
            continue
        for f in chunk:
            fid = f["id"]
            t = _loads_alias(rep.get(f'f{fid}'))
            # boss-melee taken, restricted to roster tanks
            melee = {}
            for e in (t or {}).get("data", {}).get("entries", []):
                nm = e.get("name")
                if nm not in tank_names:
                    continue
                m = sum(ab.get("total", 0) for ab in (e.get("abilities") or [])
                        if ab.get("name") == "Melee")
                if m > 0:
                    melee[nm] = m
            if not melee:
                continue
            top = max(melee.values())
            for nm, m in melee.items():
                if m < top * 0.15:        # didn't tank this fight (incidental/threat-slip melee)
                    continue
                fr = fight_roles.setdefault(nm, {"Healer": [], "Tank": [], "dps": []})
                tank_l = fr.setdefault("Tank", [])
                if fid not in tank_l:
                    tank_l.append(fid)
                    added += 1
                # a fight they tanked isn't a fight they DPS'd/healed
                for b in ("dps", "Healer"):
                    if fid in fr.get(b, []):
                        fr[b].remove(fid)
    if added:
        print(f"   Tank attribution hardened from WCL DamageTaken: +{added} tank-fight(s)")


def fetch_healing_by_fight(token: str, report_code: str, kills: list, batch: int = 5) -> dict:
    """Per-fight Healing tables (heal-fight scoping for the scoped healer metrics + tank healing
    received). Uses GraphQL field ALIASING to fetch `batch` fights per HTTP request instead of one
    request each — collapses ~10 round-trips into ~2."""
    by = {}
    for i in range(0, len(kills), batch):
        chunk = kills[i:i + batch]
        aliases = "\n".join(
            f'f{f["id"]}: table(dataType: Healing, fightIDs:[{int(f["id"])}])' for f in chunk)
        Q = f"query($c:String!){{reportData{{report(code:$c){{ {aliases} }}}}}}"
        try:
            rep = gql(token, Q, {"c": report_code})["reportData"]["report"]
            for f in chunk:
                t = rep.get(f'f{f["id"]}')
                if isinstance(t, str):
                    t = json.loads(t)
                by[f["id"]] = (t or {}).get("data", {}).get("entries", [])
        except Exception as ex:
            print(f"  Warning: healing-by-fight batch failed: {ex}")
            for f in chunk:
                by[f["id"]] = []
    return by


def fetch_damage_by_fight(token: str, report_code: str, kills: list, batch: int = 5) -> dict:
    """Per-fight DamageDone tables (needed for per-boss vs-replacement DPS/Tank WAR + the
    uptime heatmap). Aliased like fetch_healing_by_fight — `batch` fights per HTTP request.
    Returns {fid: [entries]}. ONE fetch feeds both damage_uptime_by_fight and the WAR
    computations (was a separate fetch_uptime_by_fight pagination of these same tables)."""
    by = {}
    for i in range(0, len(kills), batch):
        chunk = kills[i:i + batch]
        aliases = "\n".join(
            f'f{f["id"]}: table(dataType: DamageDone, fightIDs:[{int(f["id"])}])' for f in chunk)
        Q = f"query($c:String!){{reportData{{report(code:$c){{ {aliases} }}}}}}"
        try:
            rep = gql(token, Q, {"c": report_code})["reportData"]["report"]
            for f in chunk:
                t = rep.get(f'f{f["id"]}')
                if isinstance(t, str):
                    t = json.loads(t)
                by[f["id"]] = (t or {}).get("data", {}).get("entries", [])
        except Exception as ex:
            print(f"  Warning: damage-by-fight batch failed: {ex}")
            for f in chunk:
                by[f["id"]] = []
    return by


def damage_uptime_by_fight(dmg_by_fight: dict, kills: list) -> dict:
    """Per-fight active-time % per player, so a structurally-low fight (submerge/phase, e.g.
    Vashj P2 for casters, Lurker dives) is visible instead of silently dragging the raid-wide
    number. Derived from the already-fetched per-fight DamageDone tables (dmg_by_fight) — the
    damage analog of healer_uptime_by_fight, so NO extra API call. Returns { player: {boss: pct} }."""
    out = defaultdict(dict)
    fid_boss = {f["id"]: f["name"] for f in kills}
    fid_dur  = {f["id"]: (f["endTime"] - f["startTime"]) / 1000.0 for f in kills}
    for fid, entries in (dmg_by_fight or {}).items():
        boss = fid_boss.get(fid)
        dur  = fid_dur.get(fid, 0) or 1
        if not boss:
            continue
        for e in (entries or []):
            at = e.get("activeTime", 0) / 1000.0
            out[e.get("name")][boss] = round(at / dur * 100, 1)
    return dict(out)


def healer_uptime_by_fight(heal_by_fight: dict, kills: list) -> dict:
    """Per-fight healer CASTING activity % ({name: {boss: pct}}) — the healing analog of
    fetch_uptime_by_fight. Reuses the already-fetched raw heal_by_fight ({fid: [entries]}),
    so NO extra API call. Each entry's activeTime / fight_duration = the share of the fight
    the healer was actively casting. Filtered to Healer role downstream (at emit time)."""
    out = defaultdict(dict)
    fid_boss = {f["id"]: f["name"] for f in kills}
    fid_dur  = {f["id"]: (f["endTime"] - f["startTime"]) / 1000.0 for f in kills}
    for fid, entries in (heal_by_fight or {}).items():
        boss = fid_boss.get(fid)
        dur  = fid_dur.get(fid, 0) or 1
        if not boss:
            continue
        for e in (entries or []):
            name = e.get("name")
            at   = e.get("activeTime", 0) / 1000.0
            if name and at > 0:
                out[name][boss] = round(at / dur * 100, 1)
    return dict(out)


def compute_healing_metrics(heal_by_fight: dict, fight_roles: dict, fight_durs: dict,
                            tank_names: set | None = None) -> dict:
    """Healing throughput + efficiency SCOPED to each healer's heal-fights only.
    A spec-swapper (heal early, DPS late) is judged on the fights they actually healed,
    so activity/HPS reflect their healing window — not the whole raid. Adds fights_healed."""
    tank_names = tank_names or set()
    agg = defaultdict(lambda: {"eff": 0, "over": 0, "active_ms": 0, "dur": 0.0,
                               "tank_h": 0, "tot_t": 0, "spells": defaultdict(int), "fights": 0})
    for fid, entries in heal_by_fight.items():
        dur = fight_durs.get(fid, 0)
        for e in entries:
            nm = e.get("name")
            # only credit this fight if the player was classified a HEALER on it
            if fid not in fight_roles.get(nm, {}).get("Healer", []):
                continue
            a = agg[nm]
            a["eff"]       += e.get("total", 0)
            a["over"]      += e.get("overheal", 0)
            a["active_ms"] += e.get("activeTime", 0)
            a["dur"]       += dur
            a["fights"]    += 1
            for x in (e.get("targets") or []):
                a["tot_t"] += x.get("total", 0)
                if x.get("name") in tank_names:
                    a["tank_h"] += x.get("total", 0)
            for ab in (e.get("abilities") or []):
                a["spells"][ab.get("name")] += ab.get("total", 0)
    out = {}
    for nm, a in agg.items():
        if a["dur"] <= 0:
            continue
        raw = a["eff"] + a["over"]
        top = max(a["spells"].items(), key=lambda x: x[1])[0] if a["spells"] else ""
        out[nm] = {
            "eff_heal":      a["eff"],
            "eff_hps":       round(a["eff"] / a["dur"]),
            "overheal_pct":  round(a["over"] / raw * 100, 1) if raw else 0.0,
            "activity_pct":  round(a["active_ms"] / 1000.0 / a["dur"] * 100, 1),
            "tank_pct":      round(a["tank_h"] / a["tot_t"] * 100, 1) if a["tot_t"] else 0.0,
            "top_spell":     top,
            "fights_healed": a["fights"],
        }
    return out


def fetch_healing_spells(token: str, report_code: str, fights: list, actors: list) -> dict:
    """Per-healer per-spell breakdown from healing events: casts, effective, overheal%,
    heal-per-cast, crit%. HoT ticks (tick=true) count toward healing but not casts/crit
    (they can't crit in TBC). Returns { player: [ {spell, casts, eff, per_cast, overheal_pct, crit_pct} ] }."""
    if not fights:
        return {}
    id_to_name = {a["id"]: a["name"] for a in (actors or []) if a.get("type") == "Player"}
    fight_ids = [f["id"] for f in fights]
    start = float(min(f["startTime"] for f in fights))
    end   = float(max(f["endTime"]   for f in fights))

    # ability guid → name from the Healing table (events only carry the numeric id)
    abil_name = {}
    try:
        QT = """query($c:String!,$f:[Int]){reportData{report(code:$c){
            table(dataType: Healing, fightIDs:$f, killType:Kills)}}}"""
        tt = gql(token, QT, {"c": report_code, "f": fight_ids})["reportData"]["report"]["table"]
        if isinstance(tt, str):
            tt = json.loads(tt)
        for e in tt.get("data", {}).get("entries", []):
            for ab in (e.get("abilities") or []):
                if ab.get("guid"):
                    abil_name[ab["guid"]] = ab.get("name")
    except Exception:
        pass

    Q = """query($c:String!,$f:[Int],$s:Float!,$e:Float!){reportData{report(code:$c){
        events(dataType: Healing, fightIDs:$f, startTime:$s, endTime:$e, limit:10000,
               hostilityType:Friendlies){ data nextPageTimestamp }}}}"""
    agg = defaultdict(lambda: defaultdict(lambda: {"casts": 0, "eff": 0, "over": 0, "crits": 0}))
    nxt, pages = start, 0
    while nxt is not None and pages < 25:
        d = gql(token, Q, {"c": report_code, "f": fight_ids, "s": nxt, "e": end})["reportData"]["report"]["events"]
        evs = d.get("data", []); nxt = d.get("nextPageTimestamp"); pages += 1
        for ev in evs:
            sid = ev.get("sourceID")
            if sid is None:
                continue
            a = agg[sid][ev.get("abilityGameID")]
            a["eff"]  += ev.get("amount", 0)
            a["over"] += ev.get("overheal", 0)
            if not ev.get("tick"):                 # direct heal = a cast (HoT ticks excluded)
                a["casts"] += 1
                if ev.get("hitType") == 2:
                    a["crits"] += 1
        if not nxt:
            break

    out = {}
    for sid, spells in agg.items():
        name = id_to_name.get(sid)
        if not name:
            continue
        rows = []
        for aid, s in spells.items():
            raw = s["eff"] + s["over"]
            if s["eff"] <= 0:
                continue
            rows.append({
                "spell":        abil_name.get(aid, str(aid)),
                "casts":        s["casts"],
                "eff":          s["eff"],
                "per_cast":     round(s["eff"] / s["casts"]) if s["casts"] else 0,
                "overheal_pct": round(s["over"] / raw * 100, 1) if raw else 0.0,
                "crit_pct":     round(s["crits"] / s["casts"] * 100, 1) if s["casts"] else 0.0,
            })
        out[name] = sorted(rows, key=lambda x: -x["eff"])
    return out


def fetch_healer_mana(token: str, report_code: str, fight_ids: list) -> dict:
    """Estimated mana spent ON HEALING per player = heal casts × spell cost. Cast counts
    come from the WCL Casts table (counts HoT applications too, unlike heal events).
    Returns { player: mana_spent }. It's an estimate (flat max-rank costs, pre-talent)."""
    if not fight_ids:
        return {}
    Q = """query($c:String!,$f:[Int]){reportData{report(code:$c){
        table(dataType: Casts, fightIDs:$f, killType:Kills)}}}"""
    try:
        t = gql(token, Q, {"c": report_code, "f": fight_ids})["reportData"]["report"]["table"]
        if isinstance(t, str):
            t = json.loads(t)
        out = {}
        for e in t.get("data", {}).get("entries", []):
            spent = 0
            for ab in (e.get("abilities") or []):
                cost = HEAL_MANA_COST.get(ab.get("name"))
                if cost:                                    # only heal spells contribute
                    spent += ab.get("total", 0) * cost
            if spent:
                out[e.get("name")] = spent
        return out
    except Exception as ex:
        print(f"  Warning: mana fetch failed: {ex}")
        return {}


def fetch_role_spell_usage(token, report_code, fight_ids, players):
    """Top abilities CAST per role + per player — what each role/player is actually doing.
    Aggregates the Casts table. Returns
      { "roles":   { role: [{ability, casts, players}] },
        "players": { role: [{name, role, total, abilities:[{ability, casts}]}] } }."""
    if not fight_ids:
        return {"roles": {}, "players": {}}
    role_of = {p["name"]: p.get("role", "") for p in players}
    Q = """query($c:String!,$f:[Int]){reportData{report(code:$c){
        table(dataType: Casts, fightIDs:$f, killType:Kills)}}}"""
    try:
        t = gql(token, Q, {"c": report_code, "f": fight_ids})["reportData"]["report"]["table"]
        if isinstance(t, str):
            t = json.loads(t)
        agg    = defaultdict(lambda: defaultdict(int))   # [role][ability] = casts
        users  = defaultdict(lambda: defaultdict(set))   # [role][ability] = {players}
        pcasts = defaultdict(lambda: defaultdict(int))   # [name][ability]  = casts
        for e in t.get("data", {}).get("entries", []):
            nm0 = e.get("name")
            role = role_of.get(nm0)
            if role not in ("Caster", "Physical", "Tank", "Healer"):
                continue
            for ab in (e.get("abilities") or []):
                nm, n = ab.get("name"), ab.get("total", 0)
                if nm and n:
                    agg[role][nm] += n
                    users[role][nm].add(nm0)
                    pcasts[nm0][nm] += n
        roles = {}
        for role, abils in agg.items():
            roles[role] = sorted(
                [{"ability": a, "casts": c, "players": len(users[role][a])} for a, c in abils.items()],
                key=lambda x: -x["casts"])[:12]
        by_player = defaultdict(list)
        for nm0, abils in pcasts.items():
            role = role_of.get(nm0)
            total = sum(abils.values())
            by_player[role].append({
                "name": nm0, "role": role, "total": total,
                # keep a fuller list (cards show the top 3; the click-through drill shows all)
                "abilities": sorted(
                    [{"ability": a, "casts": c} for a, c in abils.items()],
                    key=lambda x: -x["casts"])[:20],
            })
        for role in by_player:
            by_player[role].sort(key=lambda x: -x["total"])
        return {"roles": roles, "players": dict(by_player)}
    except Exception as ex:
        print(f"  Warning: spell-usage fetch failed: {ex}")
        return {"roles": {}, "players": {}}
