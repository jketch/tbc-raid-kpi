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

from wcl_client import gql
from paths import ITEM_META_CACHE
from game_constants import FOOD_BUFF, ELIXIR_BUFFS

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
