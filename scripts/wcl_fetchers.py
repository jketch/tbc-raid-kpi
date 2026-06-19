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

import bisect
import json
from collections import defaultdict

from wcl_client import gql
from paths import ITEM_META_CACHE
from game_constants import (FOOD_BUFF, ELIXIR_BUFFS, HEAL_MANA_COST, EXTERNAL_ABILITIES,
                            MECHANIC_IDS, FLASK_AURA_IDS, ELIXIR_AURA_IDS, FLASK_EFFECT_NAMES,
                            SCROLL_AURA_IDS, SELF_BUFF_IGNORE, GROUP_BUFF_GEAR, ENG_SPELLS)
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


def _report(data, *path, default=None):
    """Null-safe descent into a gql() payload: `_report(data, "events")` ==
    `data["reportData"]["report"]["events"]` — except a PARTIAL payload (gql() returns
    usable data even when one alias/field errored; see wcl_client) degrades to `default`
    instead of a TypeError/KeyError that kills the whole weekly run. Every reportData
    chain in this module goes through here so one failed field thins ONE KPI, never all."""
    cur = ((data or {}).get("reportData") or {}).get("report")
    for k in path:
        cur = (cur or {}).get(k)
    return cur if cur is not None else default


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


def _consumable_from_aura(name: str, ability) -> tuple:
    """Recognize ONE pull aura → (kind, value): kind ∈ flask|elixir|food|scroll|None.

    ID-ANCHORED first (the `ability` spell-ID via FLASK_AURA_IDS / ELIXIR_AURA_IDS / SCROLL_AURA_IDS),
    then name fallbacks — the Anniversary client renames buff effects and several flasks/elixirs log
    under a bare effect name with no "Flask of …"/"Elixir of …" prefix; the Marks-of-Illidari "Shattrath
    Flask of X" line logs as "<Effect> of Shattrath" (distinct id+name) and is matched by suffix-strip.
    Order matters: flask before elixir, etc. A flask returns the canonical "Flask of …" name; an elixir
    returns the in-game (logged) name so the audit shows what the raider saw. Pure — unit-tested."""
    bn, aid = name or "", ability
    shat = bn[:-len(" of Shattrath")] if bn.endswith(" of Shattrath") else None
    if aid in FLASK_AURA_IDS:                                  return "flask",  FLASK_AURA_IDS[aid]
    if bn.startswith("Flask of"):                              return "flask",  bn
    if bn in FLASK_EFFECT_NAMES:                               return "flask",  FLASK_EFFECT_NAMES[bn]
    if shat in FLASK_EFFECT_NAMES:                             return "flask",  FLASK_EFFECT_NAMES[shat] + " (Shattrath)"
    if aid in ELIXIR_AURA_IDS:                                 return "elixir", bn or ELIXIR_AURA_IDS[aid][0]
    if bn == FOOD_BUFF:                                        return "food",   None
    if bn.startswith("Elixir of") or bn in ELIXIR_BUFFS:       return "elixir", bn
    if bn.endswith(" of Zanza"):                               return "elixir", bn   # Spirit/Swiftness/Sheen of Zanza — stat-buff consumables, count as an elixir slot
    if aid in SCROLL_AURA_IDS:                                 return "scroll", SCROLL_AURA_IDS[aid]
    if bn.startswith("Scroll of"):                             return "scroll", bn
    return None, None


def classify_pull_auras(auras: list) -> dict:
    """Classify a player's pull-time COMBATANT_INFO buff auras into {flask, food, elixirs, scrolls}.
    Per-aura recognition lives in _consumable_from_aura. Pure — unit-tested in tests/test_week_map.py."""
    flask_name, food = "", False
    elx, scr = set(), set()
    for a in auras:
        if not isinstance(a, dict):
            continue
        kind, val = _consumable_from_aura(a.get("name", "") or "", a.get("ability"))
        if   kind == "flask":  flask_name = val
        elif kind == "elixir": elx.add(val)
        elif kind == "food":   food = True
        elif kind == "scroll": scr.add(val)
    return {"flask": flask_name, "food": food, "elixirs": sorted(elx), "scrolls": sorted(scr)}


def group_buffs_provided(auras: list, own_id) -> dict:
    """Gear-provided GROUP buffs (Eye of the Night / Chain of the Twilight Owl) this player PROVIDED.
    The provider carries the party aura self-sourced (source == own_id); recipients get it other-sourced
    (so they're not credited). Returns {item: label}. Pure — unit-tested via the captured-data golden."""
    out = {}
    for a in auras:
        if isinstance(a, dict) and a.get("source") == own_id and a.get("ability") in GROUP_BUFF_GEAR:
            g = GROUP_BUFF_GEAR[a["ability"]]
            out[g["item"]] = g["label"]
    return out


def unrecognized_self_buffs(auras: list, own_id) -> list:
    """Canary: self-applied pull auras we DON'T recognize as a consumable / group-buff gear and that
    aren't a known class self-buff — i.e. candidate MISSED consumables (a new flask/elixir/scroll, or an
    Anniversary rename). Consumables are self-applied, so `source == own_id` filters out the raid-buff
    noise (blessings, brilliance, totems — all OTHER-sourced); SELF_BUFF_IGNORE strips the stable set of
    self-cast class buffs (forms/stances/armors/auras/aspects). Pure — unit-tested via the golden."""
    out = []
    for a in auras:
        if not isinstance(a, dict) or a.get("source") != own_id:
            continue
        nm, aid = a.get("name", "") or "", a.get("ability")
        if (nm and nm not in SELF_BUFF_IGNORE and aid not in GROUP_BUFF_GEAR
                and _consumable_from_aura(nm, aid)[0] is None):
            out.append(nm)
    return out


def merge_pull_consumables(per_pull: list) -> dict:
    """Aggregate a player's per-pull consumable dicts (classify_pull_auras + weapon_oil) into a
    best-of-night view. COMBATANT_INFO fires once per fight, and a raider often ISN'T fully buffed at
    the first pull (food/flask applied after, or they joined late), so sampling one fight under-credits
    prep. Booleans take ANY pull — had a flask/food/oil/scroll this raid ⇒ credit it (food & scrolls
    are 30-min and lapse mid-night, so 'any pull' is the fair read). The elixir SLOT uses the single
    BEST pull (max elixirs held at once) so two different-pull battle elixirs can't fake a
    battle+guardian pair. Pure — unit-tested in tests/test_week_map.py."""
    return {
        "flask":      next((c["flask"] for c in per_pull if c.get("flask")), ""),
        "food":       any(c.get("food") for c in per_pull),
        "elixirs":    max((c.get("elixirs") or [] for c in per_pull), key=len, default=[]),
        "scrolls":    sorted({s for c in per_pull for s in (c.get("scrolls") or [])}),
        "weapon_oil": any(c.get("weapon_oil") for c in per_pull),
        "ranged_scope": any(c.get("ranged_scope") for c in per_pull),   # hunter weapon enhancer
    }


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

    try:
        # COMBATANT_INFO fires at the START of each fight. Sample EVERY kill (not just fights[0]) —
        # raiders aren't always fully buffed at the first pull (food/flask applied after, late joiners),
        # so a single-fight sample under-credits prep. Gear/crit is stable ⇒ take the first event we see
        # per player; consumables are aggregated best-of-night by merge_pull_consumables.
        gear_map = {}
        ci_pulls = {}        # name -> [per-pull consumable dict (classify_pull_auras + weapon_oil)]
        gbuffs   = {}        # name -> {item: label} group buffs PROVIDED (best-of-night union)
        canary   = {}        # unrecognized self-buff name -> sample player (the consumable canary)
        total_events = 0
        for f in fights:
            data = gql(token, Q_COMBATANT_INFO, {
                "code": report_code,
                "startTime": float(f["startTime"]),
                "endTime":   float(f["endTime"]),
            })
            events = _report(data, "events", "data", default=[])
            total_events += len(events)
            for ev in events:
                sid  = ev.get("sourceID", -1)
                name = id_to_name.get(sid, str(sid))
                auras = ev.get("auras") or []
                if name not in gear_map:        # first appearance — gear/crit (stable across the night)
                    gear_map[name] = [{
                        "_crit_melee":  ev.get("critMelee",  0),
                        "_crit_spell":  ev.get("critSpell",  0),
                        "_crit_ranged": ev.get("critRanged", 0),
                        "_agility":     ev.get("agility",   0),   # primary-stat crit (melee/ranged)
                        "_intellect":   ev.get("intellect", 0),   # primary-stat crit (spell)
                        "gear":         ev.get("gear", []),
                    }]
                cons = classify_pull_auras(auras)
                # Weapon oil / sharpening stone = a TEMPORARY weapon enchant. Only weapons can
                # carry one, so any item with temporaryEnchant means they oiled/stoned a weapon.
                gear_items = ev.get("gear", []) or []
                cons["weapon_oil"] = any((it.get("temporaryEnchant") or 0)
                                         for it in gear_items if isinstance(it, dict))
                # A HUNTER's weapon enhancer is the ranged SCOPE (a PERMANENT enchant on the ranged
                # weapon — gear index 17), NOT a temp oil/stone (those go only on the melee weapons a
                # hunter never fights with). Captured for all; week_map uses it for hunters only.
                rng = gear_items[17] if len(gear_items) > 17 and isinstance(gear_items[17], dict) else {}
                cons["ranged_scope"] = bool(rng.get("permanentEnchant"))
                ci_pulls.setdefault(name, []).append(cons)
                provided = group_buffs_provided(auras, sid)
                if provided:
                    gbuffs.setdefault(name, {}).update(provided)
                for nm in unrecognized_self_buffs(auras, sid):
                    canary.setdefault(nm, name)
        print(f"  Found {total_events} combatantinfo events across {len(fights)} kill(s)")
        if canary:   # surface candidate MISSED consumables so a new tier/rename self-reports
            shown = ", ".join(f"{n} ({p})" for n, p in sorted(canary.items())[:12])
            more = "" if len(canary) <= 12 else f" (+{len(canary) - 12} more)"
            msg = (f"  consumable canary: {len(canary)} self-applied pull aura(s) not recognized as a "
                   f"consumable/known self-buff — if any is a new flask/elixir/scroll, add its spell-ID to "
                   f"game_constants (audit: scripts/tools/probe_consumable_ids.py): {shown}{more}")
            # The warning is cosmetic; never let a console-encoding hiccup on a non-UTF-8 stdout abort
            # the whole fetch (the outer except would otherwise drop ALL consumables for the run).
            try:
                print("  ⚠" + msg)
            except Exception:
                print(msg.encode("ascii", "replace").decode("ascii"))
        gear_map["__consumables__"] = {name: merge_pull_consumables(pulls)
                                       for name, pulls in ci_pulls.items()}
        gear_map["__group_buffs__"] = gbuffs
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
        result  = _report(data, "events", default={})
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
        fights = _report(gql(token, """query($c:String!){reportData{report(code:$c){
            fights{ id kill encounterID startTime endTime }}}}""",
            {"c": report_code}), "fights", default=[])
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
            t = _report(gql(token, """query($c:String!,$f:[Int]){reportData{report(code:$c){
                table(dataType: DamageDone, fightIDs:$f)}}}""",
                {"c": report_code, "f": ids}), "table", default={})
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
    players = {nm: {k: data[k].get(nm, {"total": 0, "active": 0}) for k in sels}
               for nm in sorted(names)}   # sorted: deterministic key order across runs
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
    md = _report(gql(token, """query($c:String!){reportData{report(code:$c){masterData{
        actors{ id name type subType petOwner }
        abilities{ gameID name icon } }}}}""",
             {"c": report_code}), "masterData")
    if not md:
        # masterData is load-bearing for ~7 consumers — an empty md would silently blank
        # them all, so fail LOUD here (the one place a partial payload must not degrade).
        raise RuntimeError(f"masterData unavailable for {report_code} (partial WCL response)")
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
        tbl = _report(blob, "table", default={})
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
            pd = _report(gql(token, Q, {"c": report_code, "f": fid}), "playerDetails", default={})
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
            rep = _report(gql(token, Q, {"c": report_code}), default={})
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
            rep = _report(gql(token, Q, {"c": report_code}), default={})
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
            rep = _report(gql(token, Q, {"c": report_code}), default={})
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


def fetch_healing_spells(token: str, report_code: str, fights: list, actors: list,
                         md: dict | None = None) -> dict:
    """Per-healer per-spell breakdown from healing events: casts, effective, overheal%,
    heal-per-cast, crit%. HoT ticks (tick=true) count toward healing but not casts/crit
    (they can't crit in TBC) — a row with casts 0 is HoT-tick-only (per_cast is None there,
    rendered as ticks). Downranked casts (downranking is core TBC healing) keep their own
    row, labeled "(downranked)" so twin rows are distinguishable AND don't collide on the
    healing_spells DB primary key (player, spell).
    Returns { player: [ {spell, casts, eff, per_cast, overheal_pct, crit_pct} ] }."""
    if not fights:
        return {}
    id_to_name = {a["id"]: a["name"] for a in (actors or []) if a.get("type") == "Player"}
    fight_ids = [f["id"] for f in fights]
    start = float(min(f["startTime"] for f in fights))
    end   = float(max(f["endTime"]   for f in fights))

    # ability guid → name: masterData covers EVERY id in the report (incl. low ranks and
    # racials like Gift of the Naaru that a name-from-Healing-table-only map missed — those
    # rendered as raw numeric ids); the Healing table overlay stays as the precise layer.
    abil_name = dict((md or {}).get("gid2name") or {})
    try:
        QT = """query($c:String!,$f:[Int]){reportData{report(code:$c){
            table(dataType: Healing, fightIDs:$f, killType:Kills)}}}"""
        tt = _report(gql(token, QT, {"c": report_code, "f": fight_ids}), "table", default={})
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
        d = _report(gql(token, Q, {"c": report_code, "f": fight_ids, "s": nxt, "e": end}), "events", default={})
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
        # Rank disambiguation: same spell NAME under several gameIDs = downranking (TBC rank
        # chains ascend by id, so the HIGHEST id is the top rank and keeps the plain name;
        # lower ids get "(downranked)"). Identical twin rows confused readers and silently
        # collided on the healing_spells DB primary key.
        by_label = defaultdict(list)
        for aid, s in spells.items():
            if s["eff"] > 0:
                by_label[abil_name.get(aid, str(aid))].append((aid if isinstance(aid, int) else -1, aid, s))
        rows = []
        for label, variants in by_label.items():
            variants.sort(key=lambda v: -v[0])             # highest rank (id) first
            for i, (_, aid, s) in enumerate(variants):
                raw = s["eff"] + s["over"]
                suffix = "" if i == 0 else (" (downranked)" if i == 1 else f" (downranked {i})")
                rows.append({
                    "spell":        label + suffix,
                    "casts":        s["casts"],
                    "eff":          s["eff"],
                    # casts 0 = HoT-tick-only healing (Renew/Rejuv) — a 0 "per cast" reads as
                    # broken, so it's None and the drill renders "ticks" instead.
                    "per_cast":     round(s["eff"] / s["casts"]) if s["casts"] else None,
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
        t = _report(gql(token, Q, {"c": report_code, "f": fight_ids}), "table", default={})
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
        t = _report(gql(token, Q, {"c": report_code, "f": fight_ids}), "table", default={})
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


# Tank defensive cooldowns — WCL spell IDs → display name. Counted from Casts events
# scoped to kill-fight windows. Frenzied Regen (26999) has no aura, so Casts is the only
# source; Barkskin/Shield Wall/Last Stand/Lay on Hands likewise tracked by cast.
TANK_CD_IDS = [871, 12975, 26999, 22845, 22812, 27154, 1020, 498, 5573]
CD_NAMES = {871: "Shield Wall", 12975: "Last Stand", 26999: "Frenzied Regeneration",
            22845: "Frenzied Regeneration",   # lower-rank bear Frenzied Regen (live-verified in masterData)
            22812: "Barkskin", 27154: "Lay on Hands", 1020: "Divine Shield",
            498: "Divine Protection", 5573: "Divine Protection"}   # names match the combat log overlay

# The CD cast IDs above double as their self-buff AURA IDs in TBC (verified live: Divine Shield 1020,
# Barkskin 22812, Frenzied Regen 26999/22845 all report Buffs-table uptime when used) — so per-CD aura
# UPTIME% is a durable, accurate metric with no guessed durations. EXCEPTION: Lay on Hands (27154) is an
# instant full-heal with no mitigation aura, so it never produces uptime and stays a count-only CD.
CD_NO_AURA = {27154}   # Lay on Hands — instant heal, no buff window
# CDs with no meaningful damage-COVERAGE window: Lay on Hands (instant heal) + Divine Shield (1020,
# an IMMUNITY that drops threat — the tank faces ~0 damage during it; it's a threat-drop / mechanic
# dodge, not a tank-through mitigation, so a coverage ratio reads a misleading 0×). Both stay count-only.
CD_NO_COVERAGE = CD_NO_AURA | {1020}

# WCL melee hitType enum (LOCKED against live data — confirmed by probing tank logs directly).
# Confirmed by probing this report's tanks: a crit-immune bear shows only {miss, hit,
# dodge, crushing}; paladins add {blocked, parry, crit}.
#   1 = normal hit   2 = crit            4 = blocked (partial, reduced)
#   15 = crushing    0/7/8 = miss/dodge/parry (zero damage)
HITTYPE_CRUSH = 15
HITTYPE_CRIT  = 2


def build_tank_scorecard_extended(token: str, report_code: str, kills: list,
                                  fight_roles: dict, fight_durs: dict,
                                  heal_by_fight: dict, actors: list, md: dict | None = None):
    """WCL-durable tank survivability — the source of record, run EVERY week (no combat
    log needed). One consolidated kill-fight pass, scoped to the fights each player TANKED
    (prot/ret-swap aware):
      • DamageTaken tables (aliased)  → DTPS, dmg taken, phys/magic school split, per-boss
      • DamageTaken events (one fightID-scoped paginated query) → melee mitigation
        (crushing/crit counts + avoidance%) and the single biggest hit taken
      • Casts events (one query)      → defensive cooldown counts (TANK_CD_IDS)
      • DamageDone tables (aliased)   → per-boss RAID DPS (feeds the Overview boss tiles)
    Healing received is reused from the per-fight Healing tables (targets). The combat log,
    when present, adds lowest-HP%-survived as enrichment in build_week_data — it is never
    load-bearing here. Returns (tank_metrics: {name: {...}}, boss_raid_dps: {boss: dps})."""
    tanks = {n for n, fr in fight_roles.items() if fr.get("Tank")}
    if not tanks:
        return {}, {}
    id2name  = {a["id"]: a["name"] for a in actors}
    name2id  = {a["name"]: a["id"] for a in actors}
    tank_ids = {name2id[n] for n in tanks if n in name2id}
    # fights each tank actually TANKED — scopes mitigation + biggest hit to the same window
    # as DTPS/per-boss, so a bear's off-tank cleave doesn't pollute his survivability stats.
    tank_fids = {n: set(fr.get("Tank", [])) for n, fr in fight_roles.items() if fr.get("Tank")}
    fid_boss = {f["id"]: f["name"] for f in kills}
    fids     = [f["id"] for f in kills]
    win_s    = min(f["startTime"] for f in kills)
    win_e    = max(f["endTime"]   for f in kills)

    # ability gameID → name, for labeling the biggest hit (from the shared masterData fetch)
    if md is None:
        # one masterData fetch per run (b9be8d9) — a silent re-fetch here is how that rule
        # erodes, so fail loud instead. Pass build_week_data's shared `md`.
        raise ValueError("md is required — pass the shared fetch_master_data() result")
    abil_name = md["gid2name"]

    agg = defaultdict(lambda: {"taken": 0, "dur": 0.0, "hrecv": 0, "fights": 0,
                               "phys": 0, "magic": 0, "per_boss": [],
                               "crush": 0, "crit": 0, "avoid": 0, "melee": 0,
                               "biggest": {"amount": 0, "ability": "", "boss": ""},
                               "cooldowns": {}})

    # ── 1) DamageTaken TABLES — DTPS / taken / school split / per-boss (aliased, batch 5) ──
    for i in range(0, len(kills), 5):
        chunk = kills[i:i + 5]
        aliases = "\n".join(
            f'f{f["id"]}: table(dataType: DamageTaken, fightIDs:[{int(f["id"])}], hostilityType: Friendlies)'
            for f in chunk)
        Q = f"query($c:String!){{reportData{{report(code:$c){{ {aliases} }}}}}}"
        try:
            rep = _report(gql(token, Q, {"c": report_code}), default={})
        except Exception as ex:
            print(f"  Warning: tank DamageTaken batch failed: {ex}")
            continue
        for f in chunk:
            fid = f["id"]
            dur = fight_durs.get(fid, 0)
            fight_tanks = {n for n in tanks if fid in fight_roles.get(n, {}).get("Tank", [])}
            if not fight_tanks:
                continue
            t = _loads_alias(rep.get(f'f{fid}'))
            for e in (t or {}).get("data", {}).get("entries", []):
                nm = e.get("name")
                if nm not in fight_tanks:
                    continue
                total = e.get("total", 0)
                a = agg[nm]
                a["taken"]  += total
                a["dur"]    += dur
                a["fights"] += 1
                # school split: ability `type` is the damage school (1 = physical)
                phys = sum(ab.get("total", 0) for ab in (e.get("abilities") or [])
                           if ab.get("type") == 1)
                a["phys"]  += phys
                a["magic"] += max(total - phys, 0)
                if dur > 0:
                    a["per_boss"].append({"boss": fid_boss.get(fid, ""),
                                          "dtps": round(total / dur), "taken": total,
                                          "seconds": round(dur)})
            # healing received this fight (across every healer's tank targets)
            for e in heal_by_fight.get(fid, []):
                for x in (e.get("targets") or []):
                    if x.get("name") in fight_tanks:
                        agg[x["name"]]["hrecv"] += x.get("total", 0)

    # ── 2) DamageTaken EVENTS — mitigation + biggest hit (one paginated, fight-scoped) ──
    # Also retain the UNMITIGATED damage timeline per tank (ts, unmitigatedAmount) — the raw incoming
    # the tank FACED — which powers the defensive-CD coverage value (_tank_cd_value): a well-timed CD
    # covers a window of heavy unmitigated incoming. unmitigatedAmount is live-verified on 2.5 events.
    dmg_ev      = defaultdict(list)          # name → [(ts, unmitigated)] (chronological)
    unmit_total = defaultdict(int)           # name → total unmitigated taken (baseline numerator)
    QE = """query($c:String!,$ids:[Int]!,$st:Float!,$en:Float!){reportData{report(code:$c){
        events(fightIDs:$ids, startTime:$st, endTime:$en, dataType: DamageTaken,
               hostilityType: Friendlies, limit: 10000){ data nextPageTimestamp }}}}"""
    st = win_s
    while True:
        try:
            ev = _report(gql(token, QE, {"c": report_code, "ids": fids, "st": st, "en": win_e}),
                         "events", default={})
        except Exception as ex:
            print(f"  Warning: tank DamageTaken events failed: {ex}")
            break
        for d in ev.get("data", []):
            tid = d.get("targetID")
            if tid not in tank_ids:
                continue
            nm = id2name.get(tid)
            if not nm or d.get("fight") not in tank_fids.get(nm, ()):
                continue   # only fights this player actually tanked
            a = agg[nm]
            amt = d.get("amount", 0) or 0
            um = d.get("unmitigatedAmount")
            um = um if um is not None else amt        # fall back to taken when WCL omits unmitigated
            _ts = d.get("timestamp")
            if _ts is not None:
                dmg_ev[nm].append((_ts, um))
            unmit_total[nm] += um
            if amt > a["biggest"]["amount"]:
                a["biggest"] = {"amount": amt,
                                "ability": abil_name.get(d.get("abilityGameID")) or "Melee",
                                "boss": fid_boss.get(d.get("fight"), "")}
            if d.get("abilityGameID") == 1:          # boss white melee — where crush/crit/avoid live
                a["melee"] += 1
                ht = d.get("hitType")
                if ht == HITTYPE_CRUSH: a["crush"] += 1
                elif ht == HITTYPE_CRIT: a["crit"] += 1
                if amt == 0:            a["avoid"] += 1
        nx = ev.get("nextPageTimestamp")
        if not nx:
            break
        st = nx

    # ── 3) Casts EVENTS — defensive cooldown counts (one paginated query) ──
    QC = """query($c:String!,$ids:[Int]!,$st:Float!,$en:Float!){reportData{report(code:$c){
        events(fightIDs:$ids, startTime:$st, endTime:$en, dataType: Casts,
               limit: 10000){ data nextPageTimestamp }}}}"""
    st = win_s
    while True:
        try:
            ev = _report(gql(token, QC, {"c": report_code, "ids": fids, "st": st, "en": win_e}),
                         "events", default={})
        except Exception as ex:
            print(f"  Warning: tank Casts events failed: {ex}")
            break
        for d in ev.get("data", []):
            if d.get("type") != "cast":
                continue
            gid = d.get("abilityGameID")
            sid = d.get("sourceID")
            if gid in CD_NAMES and sid in tank_ids:
                nm = id2name.get(sid)
                if nm:
                    cd = CD_NAMES[gid]
                    agg[nm]["cooldowns"][cd] = agg[nm]["cooldowns"].get(cd, 0) + 1
        nx = ev.get("nextPageTimestamp")
        if not nx:
            break
        st = nx

    # ── 4) DamageDone TABLES — per-boss raid DPS for the Overview tiles (aliased) ──
    boss_raid_dps = {}
    for i in range(0, len(kills), 5):
        chunk = kills[i:i + 5]
        aliases = "\n".join(
            f'f{f["id"]}: table(dataType: DamageDone, fightIDs:[{int(f["id"])}])' for f in chunk)
        Q = f"query($c:String!){{reportData{{report(code:$c){{ {aliases} }}}}}}"
        try:
            rep = _report(gql(token, Q, {"c": report_code}), default={})
        except Exception as ex:
            print(f"  Warning: raid-DPS batch failed: {ex}")
            continue
        for f in chunk:
            t = _loads_alias(rep.get(f'f{f["id"]}'))
            dur = fight_durs.get(f["id"], 0) or 1
            tot = sum(e.get("total", 0) for e in (t or {}).get("data", {}).get("entries", []))
            if tot:
                boss_raid_dps[fid_boss[f["id"]]] = round(tot / dur)

    # ── 5) defensive-CD COVERAGE VALUE — 'reverse bloodlust': unmitigated damage faced during each
    #       CD's aura window vs the tank's baseline incoming (well-timed CD → >1×; lull → low) ──
    fid_end = {f["id"]: f["endTime"] for f in kills}   # fight boundaries — keeps a CD window from a
    cd_val = _tank_cd_value(token, report_code, fids, win_s, win_e, fid_end, agg, dmg_ev,   # trash-gap
                            unmit_total, tank_ids, id2name)                                  # mispair

    # ── assemble ──
    out = {}
    for nm, a in agg.items():
        if a["dur"] <= 0:
            continue
        school = a["phys"] + a["magic"]
        out[nm] = {
            "dtps":          round(a["taken"] / a["dur"]),
            "taken":         a["taken"],
            "hps_recv":      round(a["hrecv"] / a["dur"]),
            "fights_tanked": a["fights"],
            "phys_pct":      round(a["phys"]  / school * 100, 1) if school else 0,
            "magic_pct":     round(a["magic"] / school * 100, 1) if school else 0,
            "crush_count":   a["crush"],
            "crit_count":    a["crit"],
            "avoid_pct":     round(a["avoid"] / a["melee"] * 100, 1) if a["melee"] else 0,
            "biggest_hit":   a["biggest"] if a["biggest"]["amount"] > 0 else None,
            "cooldowns":     a["cooldowns"],
            "cd_value":      cd_val.get(nm, {}),   # per-CD coverage ratio (unmit faced ÷ baseline)
            "per_boss":      a["per_boss"],   # pull order; HTML sorts to encounter order
        }
    return out, boss_raid_dps


def _cd_cover(wins, ev_list, base_dps):
    """Pure: coverage ratio for one CD's aura windows. Sum the UNMITIGATED damage the tank faced inside
    the windows ÷ window-seconds = the window's incoming rate; ÷ baseline = the ratio (>1 ⇒ the CD
    covered heavier-than-average incoming = well-timed; <1 ⇒ a lull). wins=[(start,end)] ms, ev_list =
    [(ts, unmit)] sorted ascending by ts, base_dps = baseline unmitigated DTPS. None if not computable."""
    if not wins or base_dps <= 0:
        return None
    tot_unmit, tot_sec = 0, 0.0
    for ws, we in wins:
        if we <= ws:
            continue
        tot_sec += (we - ws) / 1000
        i = bisect.bisect_left(ev_list, (ws, float("-inf")))   # first event with ts ≥ window start
        while i < len(ev_list) and ev_list[i][0] <= we:
            tot_unmit += ev_list[i][1]
            i += 1
    if tot_sec <= 0:
        return None
    return round((tot_unmit / tot_sec) / base_dps, 2)


def _cd_windows(evs, fid_end, max_cd_ms=25000):
    """Pure: reconstruct defensive-CD aura windows from one (tank, CD) Buffs-event stream. ROBUST to the
    kill-scoped events query dropping a removebuff that expired in a trash gap between bosses — which would
    otherwise pair an applybuff to a LATER boss's removebuff (a giant phantom window that craters the
    coverage value). Pairs applybuff→removebuff WITHIN THE SAME FIGHT only; an orphaned applybuff (missing
    or cross-fight removebuff) is capped at max_cd_ms and clamped to its fight's end (every TBC defensive CD
    is ≤ ~20s, so the cap only ever bounds a broken pairing, never a real window). evs = [(type, ts, fight)]
    chronological; fid_end = {fight: endTime ms}. Returns [(start, end)]."""
    evs = sorted(evs, key=lambda e: (e[1] if e[1] is not None else 0))
    out, band = [], None                          # band = (start_ts, fight)
    def _close(s0, f0, end_ts):
        end = min(end_ts, s0 + max_cd_ms, fid_end.get(f0, s0 + max_cd_ms))
        if end > s0:
            out.append((s0, end))
    for typ, ts, fight in evs:
        if ts is None:
            continue
        if typ == "applybuff":
            if band:                              # prior band never closed (removebuff dropped) → cap it
                _close(band[0], band[1], band[0] + max_cd_ms)
            band = (ts, fight)
        elif typ == "removebuff" and band:
            s0, f0 = band; band = None
            _close(s0, f0, ts if fight == f0 else s0 + max_cd_ms)   # cross-fight removebuff ⇒ cap, ignore it
    if band:
        _close(band[0], band[1], band[0] + max_cd_ms)
    return out


def _tank_cd_value(token, report_code, fids, win_s, win_e, fid_end, agg, dmg_ev, unmit_total, tank_ids, id2name):
    """Per-tank defensive-CD COVERAGE VALUE — the 'reverse bloodlust'. For each CD's aura WINDOW (Buffs
    events applybuff→removebuff, fight-scoped + capped via _cd_windows), the UNMITIGATED damage the tank
    FACED during the window ÷ window-seconds, over their baseline unmitigated DTPS. >1× = the CD covered
    heavier-than-average incoming (well-timed on a spike); <1× = popped in a lull. Lay on Hands + Divine
    Shield (CD_NO_COVERAGE) have no measurable mitigation window → absent (count-only). Pure WCL, no combat
    log. One Buffs-events query per CD aura. Returns {name: {cd_name: cover_ratio}}."""
    raw = defaultdict(list)                       # (name, cd_name) → [(type, ts, fight)]
    QB = """query($c:String!,$ids:[Int]!,$st:Float!,$en:Float!,$a:Float!){reportData{report(code:$c){
        events(fightIDs:$ids, startTime:$st, endTime:$en, dataType: Buffs, abilityID:$a,
               limit: 10000){ data nextPageTimestamp }}}}"""
    try:
        for aid in TANK_CD_IDS:
            if aid in CD_NO_COVERAGE:
                continue
            cd_name = CD_NAMES[aid]
            cur = win_s
            for _pg in range(MAX_EVENT_PAGES):
                ev = _report(gql(token, QB, {"c": report_code, "ids": fids, "st": cur, "en": win_e,
                                             "a": float(aid)}), "events", default={})
                for d in ev.get("data", []):
                    sid = d.get("sourceID")
                    if sid not in tank_ids:        # self-buff: tank is the source (and target)
                        continue
                    nm = id2name.get(sid)
                    if nm:
                        raw[(nm, cd_name)].append((d.get("type"), d.get("timestamp"), d.get("fight")))
                nx = ev.get("nextPageTimestamp")
                if not nx:
                    break
                cur = nx
    except Exception as ex:
        print(f"  Warning: tank CD-value buffs failed: {ex}")
        return {}
    res = defaultdict(dict)
    for (nm, cd_name), evs in sorted(raw.items()):   # sorted → deterministic
        wins = _cd_windows(evs, fid_end)
        dur = agg.get(nm, {}).get("dur", 0)
        base_dps = (unmit_total.get(nm, 0) / dur) if dur > 0 else 0
        cover = _cd_cover(sorted(wins), dmg_ev.get(nm) or [], base_dps)
        if cover is not None:
            res[nm][cd_name] = cover
    return {nm: dict(d) for nm, d in sorted(res.items())}


def _median(xs):
    s = sorted(xs); n = len(s)
    if not n: return 0.0
    m = n // 2
    return float(s[m]) if n % 2 else (s[m-1] + s[m]) / 2.0


def compute_death_hp_timelines(log_data: dict, window_s: float = 12.0) -> dict:
    """Per REAL player death (HP bottomed out ~0 in a boss window), the preceding ~window_s
    of HP% samples as a recap curve. {player: [{boss, death_ts, timeline:[{t,hp,kind,killing}]}]}
    in chronological order. Feign Death (HP never reaches 0) is filtered out."""
    hp_samples = (log_data or {}).get("hp_samples", {})
    log_deaths = (log_data or {}).get("log_deaths", {})
    out = {}
    for nm, deaths in log_deaths.items():
        per_boss = hp_samples.get(nm, {})
        recaps = []
        for dts, boss in sorted(deaths, key=lambda x: x[0]):
            win = sorted((s for s in per_boss.get(boss, []) if dts - window_s <= s[0] <= dts + 0.5),
                         key=lambda x: x[0])
            # Real death = the player was in genuine danger in-window (<40% HP at some point).
            # Excludes Feign Death (HP stays up). Melee killing blows don't log the player's
            # final 0% sample (advanced block = the boss), so synthesize the plunge to 0.
            if not win or min(s[1] for s in win) >= 40:
                continue
            tl = [{"t": round(s[0] - dts, 1), "hp": s[1], "kind": s[2]} for s in win]
            if tl[-1]["hp"] > 5:
                tl.append({"t": 0.0, "hp": 0, "kind": "dmg"})
            tl[-1]["killing"] = True
            recaps.append({"boss": boss, "death_ts": dts, "timeline": tl})
        if recaps:
            out[nm] = recaps
    return out


def compute_reaction_times(log_data: dict, danger: int = 50, cap_s: float = 10.0) -> dict:
    """Healing-team responsiveness per raider per boss: median seconds from a raider dropping
    below `danger`% HP to the next heal landing on them. A death while waiting counts as cap_s
    (worst-case) and flags the cell. {player: {boss: {"median": s, "n": k, "died": bool}}}.
    This is the raid's response to each raider — not individual-healer blame (triage/range/HoTs
    confound that)."""
    hp_samples = (log_data or {}).get("hp_samples", {})
    log_deaths = (log_data or {}).get("log_deaths", {})
    out = {}
    for nm, per_boss in hp_samples.items():
        deaths_by_boss = defaultdict(list)
        for dts, boss in log_deaths.get(nm, []):
            deaths_by_boss[boss].append(dts)
        for boss, samples in per_boss.items():
            samples = sorted(samples, key=lambda x: x[0])
            dts_list = sorted(deaths_by_boss.get(boss, []))
            reactions, died_any, in_danger, t0, prev = [], False, False, None, 100
            for ts, hp, kind in samples:
                if not in_danger:
                    if kind == "dmg" and hp < danger and prev >= danger:
                        in_danger, t0 = True, ts
                else:
                    if any(t0 <= d <= ts for d in dts_list):
                        reactions.append(cap_s); died_any = True; in_danger = False
                    elif kind == "heal":                      # first DIRECT heal = the reaction
                        reactions.append(round(min(ts - t0, cap_s), 1)); in_danger = False
                    elif hp >= danger + 5:                    # recovered via HoT/passive, no direct
                        in_danger = False                     # heal → excluded (HoTs aren't penalized)
                prev = hp
            if in_danger and any(d >= t0 for d in dts_list):
                reactions.append(cap_s); died_any = True
            if reactions:
                out.setdefault(nm, {})[boss] = {
                    "median": round(_median(reactions), 1), "n": len(reactions),
                    "died": died_any, "samples": [round(r, 1) for r in reactions]}
    return out


# ── CLASS TOOLKIT — each DPS's signature class-relative utility ───────────────
# Rotation-share denominator abilities (the template.html PERF_PRIMARY denoms) — counted per player
# from the full Casts-EVENTS pass in build_class_toolkit, because the Casts TABLE that feeds
# playerSpells truncates to a player's top-6 (dropping Execute). Matched by exact ability NAME (all
# ranks share a name). ★ KEEP IN SYNC with PERF_PRIMARY in dashboard/template.html (the union of every
# spec's {ability} + denom[]).
ROTATION_CAST_ABILITIES = {
    "Arcane Blast", "Frostbolt", "Fireball", "Fire Blast",                 # mage arcane
    "Shadow Bolt", "Incinerate", "Soul Fire", "Conflagrate",              # warlock destruction
    "Steady Shot", "Arcane Shot", "Multi-Shot",                          # hunter MM/BM
    "Execute", "Bloodthirst", "Whirlwind", "Slam", "Mortal Strike",      # warrior fury/arms
}
# One contextual metric per class: "did you bring your kit?" Cast-based & spec-agnostic
# (so off-spec play is captured for ANYONE who cast the ability — no spec gating), sourced
# from the WCL Casts table (the durable headline; combat log can enrich later). Match is by
# ability NAME (lowercased), unioning rank suffixes. Each name maps to a canonical counter key.
TOOLKIT_ABILITIES = {
    "remove lesser curse":   "decurse_mage",     # Mage (kept as a detail)
    "remove curse":          "decurse_druid",    # Balance druid
    "bloodlust":             "bloodlust",        # Shaman (detail)
    "heroism":               "bloodlust",        # Shaman (Alliance name)
    "windfury totem":        "wf_totem",         # Enhance shaman (signature)
    "grace of air totem":    "goa_totem",        # Enhance shaman (twist partner / detail)
    "wrath of air totem":    "woa_totem",        # Ele shaman
    "totem of wrath":        "tow_totem",        # Ele shaman
    "misdirection":          "misdirect",        # Hunter
    "tranquilizing shot":    "tranq",            # Hunter
    "slice and dice":        "snd",              # Rogue
    "battle shout":          "bshout",           # Warrior
    "sunder armor":          "sunder",           # Warrior
    "seal of command":       "soc",              # Ret paladin (seal twisting)
    "innervate":             "innervate",        # Druid
    "rebirth":               "rebirth",          # Druid (battle rez)
    "mangle (cat)":          "mangle",           # Feral druid
    "mangle (bear)":         "mangle",           # Feral druid
    "mangle":                "mangle",           # Feral druid (rank-agnostic)
}


# class (+ spec where it matters) → signature metric. `kind`: count | per_min | pair.
# `icon` is an ability NAME → resolved to a live WCL icon via the ability_icons map (always
# resolves), with `fallback` as the Zamimg slug if the report never logged that ability.
def _toolkit_metric(cls, spec, c, kill_min):
    """Resolve a player's signature class-toolkit cell from their cast counts `c`
    ({canonical_key: count}) and the night's total kill minutes. Returns a dict
    {label, value, title, icon_ability, fallback} or None when the class has no signature."""
    g = c.get
    if cls == "Mage":
        # AE-spam leaderboard (whole report, trash included). Decurse trails as a detail.
        dc = g("decurse_mage", 0)
        return {"label": "Arcane Explosions", "value": str(g("ae", 0)), "num": g("ae", 0),
                "title": "Arcane Explosion casts — whole night" + (f" · {dc} Decurses" if dc else ""),
                "icon_ability": "Arcane Explosion", "fallback": "spell_nature_wispsplode"}
    if cls == "Warlock":
        # Bragging-rights number: the single biggest Shadow Bolt crit landed.
        sb = g("sb_crit", 0)
        return {"label": "Top SB crit", "value": (f"{sb:,}" if sb else "—"), "num": sb,
                "title": "Biggest single Shadow Bolt critical hit", "icon_ability": "Shadow Bolt",
                "fallback": "spell_shadow_shadowbolt"}
    if cls == "Shaman":
        bl = g("bloodlust", 0)
        bl_d = f" · {bl} Bloodlust" if bl else ""
        if g("wf_totem", 0) > 0:                              # enhance — Windfury uptime PROVIDED to party
            goa, sw, drops = g("goa_totem", 0), g("wf_swaps", 0), g("wf_totem", 0)
            up, goa_up = g("wf_up", 0), g("goa_up", 0)        # air-slot-exclusive modeled uptime%
            tw_min = round(sw / kill_min, 1) if kill_min else 0   # WF↔GoA twists per minute
            twisting = sw >= 20 and goa >= 10                 # alternating both air totems
            # Show BOTH the party WF uptime AND the twist rate (the two halves of the over-twist-OOM ↔
            # under-twist-low-WF balance an enh shaman is managing).
            cell = {"label": "Party WF · twists", "value": f"~{up:g}% · {tw_min:g}/min", "num": up,
                    "swaps_min": tw_min, "wf_up": up, "goa_up": goa_up,   # for the Performance twist-cadence score
                    "title": (f"Windfury Totem uptime PROVIDED TO HIS PARTY ({drops} drops, {tw_min:g} "
                              "twists/min) — air-slot-exclusive (WF drops while Grace of Air is up; totem "
                              "buffs aren't logged as auras in 2.5, so modeled from casts). The shaman never "
                              "benefits himself (WF weapon enchant suppresses it)."
                              + (f" · {goa} Grace of Air (~{goa_up:g}% up)" if goa else "")
                              + (f" · {sw} WF↔GoA swaps (twisting → WF down for GoA windows)" if twisting else "") + bl_d),
                    "icon_ability": "Windfury Totem", "fallback": "spell_nature_windfury"}
            if twisting:
                cell["tag"] = "🌀 twist"
            return cell
        up = g("tow_up")
        if up is not None:                                   # elemental — modeled ToW uptime
            return {"label": "ToW uptime", "value": f"~{up:g}%", "num": up,
                    "title": ("Totem of Wrath uptime — modeled from recast cadence "
                              "(totem buffs aren't logged as auras in 2.5)" + bl_d),
                    "icon_ability": "Totem of Wrath", "fallback": "spell_fire_totemofwrath"}
        air = g("woa_totem", 0) + g("tow_totem", 0)
        if air > 0:                                           # ele w/o ToW casts — air totems
            return {"label": "Air totems", "value": str(air), "num": air,
                    "title": "Wrath of Air + Totem of Wrath drops" + bl_d,
                    "icon_ability": "Wrath of Air Totem", "fallback": "spell_nature_slowingtotem"}
        return {"label": "Bloodlust", "value": str(bl), "num": bl,   # resto-who-DPS'd / no totems
                "title": "Bloodlust/Heroism casts", "icon_ability": "Bloodlust",
                "fallback": "spell_nature_bloodlust"}
    if cls == "Hunter":
        md, tq = g("misdirect", 0), g("tranq", 0)
        return {"label": "MD · Tranq", "value": f"{md} · {tq}", "num": md + tq,
                "title": f"{md} Misdirections · {tq} Tranquilizing Shots",
                "icon_ability": "Misdirection", "fallback": "ability_hunter_misdirection"}
    if cls == "Rogue":
        up = g("snd_up")
        return {"label": "Slice & Dice",
                "value": (f"{up:g}%" if up is not None else str(g("snd", 0))),
                "num": (up if up is not None else g("snd", 0)),
                "title": (f"Slice and Dice uptime ({g('snd', 0)} casts)" if up is not None
                          else "Slice and Dice casts"),
                "icon_ability": "Slice and Dice", "fallback": "ability_rogue_slicedice"}
    if cls == "Warrior":
        sun, up = g("sunder", 0), g("bshout_up")
        sun_d = f" · {sun} Sunders" if sun else ""
        return {"label": "Battle Shout",
                "value": (f"{up:g}%" if up is not None else str(g("bshout", 0))),
                "num": (up if up is not None else g("bshout", 0)),
                "title": (f"Battle Shout uptime on self ({g('bshout', 0)} casts){sun_d}" if up is not None
                          else f"Battle Shout casts{sun_d}"),
                "icon_ability": "Battle Shout", "fallback": "ability_warrior_battleshout"}
    if cls == "Paladin":
        # Spec-agnostic: WCL labels TBC builds by name (Justicar/Protection/Retribution), so
        # gate on the ACT, not the label — a prot pally who twists (Blunderdin, 869 SoC) shows.
        rate = (g("soc", 0) / kill_min) if kill_min else 0
        return {"label": "Seal twists", "value": f"{rate:.0f}/min", "num": round(rate, 1),
                "title": f"{g('soc', 0)} Seal of Command casts — twist cadence", "icon_ability": "Seal of Command",
                "fallback": "spell_holy_championsbond"}
    if cls == "Druid":
        # Feral vs Balance disambiguated by what they cast (WCL spec = build name, unreliable):
        # any Mangle casts → feral; otherwise show the caster's Innervate utility.
        if g("mangle", 0) > 0:
            rb = g("rebirth", 0)
            return {"label": "Mangles", "value": str(g("mangle", 0)), "num": g("mangle", 0),
                    "title": "Mangle casts" + (f" · {rb} Battle Rez" if rb else ""),
                    "icon_ability": "Mangle (Cat)", "fallback": "ability_druid_mangle2"}
        rb, dc = g("rebirth", 0), g("decurse_druid", 0)
        extra = " · ".join(x for x in [f"{rb} Rez" if rb else "", f"{dc} Decurse" if dc else ""] if x)
        return {"label": "Innervates", "value": str(g("innervate", 0)), "num": g("innervate", 0),
                "title": "Innervates given" + (f" · {extra}" if extra else ""),
                "icon_ability": "Innervate", "fallback": "spell_nature_lightning"}
    if cls == "Priest":
        # Shadow priest mana battery: mana returned to the raid via Vampiric Touch. Holy/Disc
        # never cast VT → vt_mana 0 → no cell (they live on the healer scorecard).
        vt = g("vt_mana", 0)
        if vt > 0:
            # VT's authentic TBC client icon is spell_holy_stoicism (a TBC quirk — it only
            # got the shadow-drain art in later expansions); that's what WCL's masterData maps.
            return {"label": "Mana battery", "value": f"{round(vt/1000)}k", "num": round(vt/1000),
                    "title": f"{vt:,} mana returned to the raid via Vampiric Touch",
                    "icon_ability": "Vampiric Touch", "fallback": "spell_holy_stoicism"}
        return None
    return None   # Holy/Disc Priest → healer scorecard; others: no DPS signature


def _buff_uptime_batch(token, report_code, fids, players, ability_ids, counts, kdur,
                       out_key, label, *, self_target):
    """Per-player self-buff uptime% (Buffs-table totalUptime ÷ kill time), for SnD / Battle
    Shout. Issues ONE aliased query covering every (player, ability-rank) pair instead of a
    query per pair (was players×ranks round-trips) — the WCL totalUptime values are unchanged,
    only batched (#10). `players` is [(name, sourceID)]; self_target also pins targetID=source
    (Battle Shout uptime on self, the raid-coverage proxy). Writes counts[name][out_key]."""
    if not players or not ability_ids:
        return
    fids_lit = ",".join(str(int(x)) for x in fids)
    alias2name, clauses = {}, []
    for pi, (nm, sid) in enumerate(players):
        for ai, aid in enumerate(ability_ids):
            al = f"u{pi}_{ai}"
            alias2name[al] = nm
            tgt = f", targetID:{int(sid)}" if self_target else ""
            clauses.append(f'{al}: table(dataType: Buffs, fightIDs:[{fids_lit}], '
                           f'sourceID:{int(sid)}{tgt}, abilityID:{float(aid)})')
    Q = "query($c:String!){reportData{report(code:$c){" + " ".join(clauses) + "}}}"
    up = defaultdict(float)
    try:
        rep = _report(gql(token, Q, {"c": report_code}), default={})
        for al, nm in alias2name.items():
            t = _loads_alias(rep.get(al))
            for a in (t or {}).get("data", {}).get("auras", []):
                up[nm] += a.get("totalUptime", 0)
        for nm in sorted(set(alias2name.values())):   # sorted: deterministic key order across runs
            counts[nm][out_key] = round(up[nm] / kdur * 100, 1)
    except Exception as ex:
        print(f"  Warning: {label}-uptime batch fetch failed: {ex}")


def build_class_toolkit(token, report_code, kills, md: dict | None = None):
    """Per-player cast counts of every TOOLKIT_ABILITIES spell, from WCL Casts EVENTS over
    the kill windows (durable; runs every week, no combat log). Returns
    {name: {canonical_key: count}}. The per-class metric resolution happens in
    map_to_week_data (where class/spec live).

    NB: the Casts *table* truncates to a player's top-5 abilities, so low-frequency utility
    casts (Misdirection, Innervate, Bloodlust, Decurse, Soulstone…) never surface there —
    we read raw cast EVENTS and map abilityGameID → name via masterData (rank-safe)."""
    if not kills:
        return {}
    fids = [f["id"] for f in kills]
    st   = min(f["startTime"] for f in kills)
    en   = max(f["endTime"]   for f in kills)
    if md is None:
        # one masterData fetch per run (b9be8d9) — a silent re-fetch here is how that rule
        # erodes, so fail loud instead. Pass build_week_data's shared `md`.
        raise ValueError("md is required — pass the shared fetch_master_data() result")
    # All fights (incl. trash) for the whole-report Arcane Explosion pass below — kills-only
    # would undercount the mage AE meme. masterData (actors/abilities) comes from the shared md.
    try:
        rep = _report(gql(token, """query($c:String!){reportData{report(code:$c){
            fights{id startTime endTime}}}}""",
                 {"c": report_code}), default={})
    except Exception as ex:
        print(f"  Warning: class-toolkit fights fetch failed: {ex}")
        return {}
    id2name = md["id2name"]
    name2id = md["name2id"]
    # abilityGameID → canonical toolkit key (via the name map; unions all ranks)
    gid2key = {}
    gid2rot = {}                                     # gameID → ROTATION ability NAME (untruncated counts)
    ae_ids  = []                                     # Arcane Explosion ranks (whole-report meme)
    snd_ids = []                                     # Slice and Dice (rogue uptime)
    bs_ids  = []                                     # Battle Shout (warrior uptime, self-target)
    for a in (md.get("abilities") or []):
        nm_a = (a.get("name") or "")
        key = TOOLKIT_ABILITIES.get(nm_a.lower())
        if key:
            gid2key[a.get("gameID")] = key
        if nm_a in ROTATION_CAST_ABILITIES:         # rotation-share denom abilities (PERF_PRIMARY) by name
            gid2rot[a.get("gameID")] = nm_a
        if nm_a == "Arcane Explosion":
            ae_ids.append(a.get("gameID"))
        if nm_a == "Slice and Dice":
            snd_ids.append(a.get("gameID"))
        if nm_a == "Battle Shout":
            bs_ids.append(a.get("gameID"))
    QC = """query($c:String!,$ids:[Int]!,$st:Float!,$en:Float!){reportData{report(code:$c){
        events(fightIDs:$ids, startTime:$st, endTime:$en, dataType: Casts,
               limit: 10000){ data nextPageTimestamp }}}}"""
    counts = defaultdict(lambda: defaultdict(int))   # [name][canonical_key] = casts
    rot    = defaultdict(lambda: defaultdict(int))   # [name][rotation ability NAME] = casts (untruncated)
    # Totem cast timestamps for shaman analytics: enhance WF↔GoA twisting + ele ToW uptime.
    TOTEM_TS = {"wf_totem", "goa_totem", "tow_totem"}
    tstamps  = defaultdict(lambda: defaultdict(list)) # [name][key] = [cast timestamps]
    cur = st
    try:
        for _pg in range(MAX_EVENT_PAGES):
            ev = _report(gql(token, QC, {"c": report_code, "ids": fids, "st": cur, "en": en}),
                         "events", default={})
            for d in ev.get("data", []):
                if d.get("type") != "cast":
                    continue
                gid = d.get("abilityGameID")
                key = gid2key.get(gid)
                rkey = gid2rot.get(gid)
                if key or rkey:
                    nm = id2name.get(d.get("sourceID"))
                    if nm:
                        if key:
                            counts[nm][key] += 1
                            if key in TOTEM_TS and d.get("timestamp") is not None:
                                tstamps[nm][key].append(d["timestamp"])
                        if rkey:
                            rot[nm][rkey] += 1
            nx = ev.get("nextPageTimestamp")
            if not nx:
                break
            cur = nx
    except Exception as ex:
        print(f"  Warning: class-toolkit events failed: {ex}")

    # Execute does NOT emit Casts events in 2.5 (damage-only — live-verified: 0 cast events, present only
    # in DamageDone), so the casts loop above can't see it. Count its DamageDone hits (1 hit ≈ 1 cast) so
    # the warrior rotation-share has it (the "gaming warriors maximize Execute" signal). One abilityID-
    # filtered DamageDone-events query per Execute rank — cheap (~1 page).
    ex_ids = sorted({a.get("gameID") for a in (md.get("abilities") or []) if (a.get("name") or "") == "Execute"})
    if ex_ids:
        QX = """query($c:String!,$ids:[Int]!,$st:Float!,$en:Float!,$a:Float!){reportData{report(code:$c){
            events(fightIDs:$ids, startTime:$st, endTime:$en, dataType: DamageDone, abilityID:$a,
                   limit: 10000){ data nextPageTimestamp }}}}"""
        try:
            for aid in ex_ids:
                cur = st
                for _pg in range(MAX_EVENT_PAGES):
                    ev = _report(gql(token, QX, {"c": report_code, "ids": fids, "st": cur, "en": en,
                                                 "a": float(aid)}), "events", default={})
                    for d in ev.get("data", []):
                        if d.get("type") == "damage":          # one damage event ≈ one Execute cast
                            nm = id2name.get(d.get("sourceID"))
                            if nm:
                                rot[nm]["Execute"] += 1
                    nx = ev.get("nextPageTimestamp")
                    if not nx:
                        break
                    cur = nx
        except Exception as ex:
            print(f"  Warning: class-toolkit Execute-damage pass failed: {ex}")

    # Shaman totem analytics from the timestamps above:
    #  • Enhance — WF↔GoA twisting: # of swaps between the two (mutually-exclusive) air totems.
    #    A non-twister parks one totem (0 swaps); a twister alternates every few seconds.
    #  • Elemental — Totem of Wrath uptime, MODELED: 2.5 doesn't log totem pulse buffs as auras,
    #    so estimate from recast cadence (ToW lasts 120s → each cast covers a 120s band, clamped
    #    to its fight; a cast within 120s of the pull implies pre-pull coverage back to start).
    for nm, tk in tstamps.items():
        wf, goa = tk.get("wf_totem", []), tk.get("goa_totem", [])
        if wf:
            seq = sorted([(t, "w") for t in wf] + [(t, "g") for t in goa])
            counts[nm]["wf_swaps"] = sum(1 for i in range(1, len(seq)) if seq[i][1] != seq[i-1][1])
            # Windfury Totem UPTIME the shaman PROVIDES TO HIS PARTY — AIR-SLOT-EXCLUSIVE (a GoA twist
            # drops WF), so GoA casts truncate the WF bands. A parker reads ~100%; a WF↔GoA twister reads
            # lower BY DESIGN (WF is genuinely down during GoA windows). The shaman never benefits from
            # his own totem (his WF weapon enchant suppresses it), so this is purely a party-provided
            # signal. GoA uptime surfaced alongside for the twist tradeoff.
            counts[nm]["wf_up"]  = _air_totem_uptime(wf, goa, kills, 120_000)
            counts[nm]["goa_up"] = _totem_uptime(sorted(goa), kills, 120_000)
        tow = tk.get("tow_totem", [])
        if tow:
            counts[nm]["tow_up"] = _totem_uptime(sorted(tow), kills, 120_000)

    # Arcane Explosion — WHOLE report (trash included), server-filtered by abilityID so it's
    # cheap (~2 pages). The mage AE-spam leaderboard wants the full-night number, not kills-only.
    allf = rep.get("fights") or []
    if ae_ids and allf:
        allids = [f["id"] for f in allf]
        a_st, a_en = min(f["startTime"] for f in allf), max(f["endTime"] for f in allf)
        QA = """query($c:String!,$ids:[Int]!,$st:Float!,$en:Float!,$a:Float!){reportData{report(code:$c){
            events(fightIDs:$ids, startTime:$st, endTime:$en, dataType: Casts, abilityID:$a,
                   limit: 10000){ data nextPageTimestamp }}}}"""
        try:
            for aid in ae_ids:
                cur = a_st
                for _pg in range(MAX_EVENT_PAGES):
                    ev = _report(gql(token, QA, {"c": report_code, "ids": allids, "st": cur,
                                                 "en": a_en, "a": float(aid)}), "events", default={})
                    for d in ev.get("data", []):
                        if d.get("type") == "cast":
                            nm = id2name.get(d.get("sourceID"))
                            if nm:
                                counts[nm]["ae"] += 1
                    nx = ev.get("nextPageTimestamp")
                    if not nx:
                        break
                    cur = nx
        except Exception as ex:
            print(f"  Warning: class-toolkit AE pass failed: {ex}")

    # Slice & Dice UPTIME% per rogue — a far better signal than cast count. Self-buff, so the
    # aura-centric Buffs table needs a sourceID filter to attribute per player. Denominator =
    # total kill time (a rogue who sat fights reads slightly low — acceptable). Keyed off anyone
    # who cast SnD (a rogue), so no roster/class lookup needed here.
    if snd_ids:
        kdur = sum(f["endTime"] - f["startTime"] for f in kills) or 1
        rogues = [(nm, name2id[nm]) for nm, d in counts.items()
                  if d.get("snd") and name2id.get(nm) is not None]
        _buff_uptime_batch(token, report_code, fids, rogues, snd_ids, counts, kdur,
                           "snd_up", "SnD", self_target=False)

    # Battle Shout UPTIME% per warrior. A raid buff, so source-only sums across every buffed
    # player (meaningless) — filter to the warrior as BOTH source and target (uptime on self,
    # the proxy for raid Battle Shout coverage). Keyed off anyone who cast Battle Shout.
    if bs_ids:
        kdur = sum(f["endTime"] - f["startTime"] for f in kills) or 1
        warriors = [(nm, name2id[nm]) for nm, d in counts.items()
                    if d.get("bshout") and name2id.get(nm) is not None]
        _buff_uptime_batch(token, report_code, fids, warriors, bs_ids, counts, kdur,
                           "bshout_up", "Battle Shout", self_target=True)

    # Vampiric Touch mana battery (the shadow priest's signature toolkit metric) is NOT scanned
    # here — fetch_mana_returns already pages Resources energize events and sums VT per provider
    # (MANA_SOURCES[0]). build_week_data folds that provider total into counts[priest]["vt_mana"]
    # after both run, so this avoids a SECOND full Resources pagination. (See the fold below.)
    # Fold the UNTRUNCATED rotation-ability counts (for rotation-share / Execute) under "_rot" — the
    # Casts TABLE that feeds playerSpells truncates to a player's top-6, dropping Execute; these come
    # from the full Casts-EVENTS pass above, so they're complete. sorted → deterministic key order.
    for nm, rd in rot.items():
        counts[nm]["_rot"] = {a: rd[a] for a in sorted(rd)}
    return {nm: dict(d) for nm, d in sorted(counts.items())}


def fetch_engineering_casts(token, report_code, kills, md: dict | None = None):
    """WCL-durable engineering headline — per-player count of engineering item casts (sappers,
    grenades, bombs) from Casts EVENTS over the kill windows, server-filtered by the curated
    ENG_SPELLS ability IDs. Pure WCL, runs every week with NO combat log; the log overlay
    (merge_log_into_wcl / _engineering_rows) stays primary when present and adds the real
    sapper/bomb damage. Returns {name: {ability_name: count}}.

    Clones build_class_toolkit's abilityID-filtered Casts-events pass (the Arcane Explosion
    loop) — server filtering by a known small ID set keeps it ~1 page per ability."""
    if not kills:
        return {}
    if md is None:
        # one masterData fetch per run — pass build_week_data's shared `md` (never re-fetch).
        raise ValueError("md is required — pass the shared fetch_master_data() result")
    fids = [f["id"] for f in kills]
    st   = min(f["startTime"] for f in kills)
    en   = max(f["endTime"]   for f in kills)
    id2name = md["id2name"]
    QE = """query($c:String!,$ids:[Int]!,$st:Float!,$en:Float!,$a:Float!){reportData{report(code:$c){
        events(fightIDs:$ids, startTime:$st, endTime:$en, dataType: Casts, abilityID:$a,
               limit: 10000){ data nextPageTimestamp }}}}"""
    counts = defaultdict(lambda: defaultdict(int))   # [name][ability_name] = casts
    try:
        for aid, aname in sorted(ENG_SPELLS.items()):   # sorted → deterministic dict insertion order
            cur = st
            for _pg in range(MAX_EVENT_PAGES):
                ev = _report(gql(token, QE, {"c": report_code, "ids": fids, "st": cur,
                                             "en": en, "a": float(aid)}), "events", default={})
                for d in ev.get("data", []):
                    if d.get("type") == "cast":
                        nm = id2name.get(d.get("sourceID"))
                        if nm:
                            counts[nm][aname] += 1
                nx = ev.get("nextPageTimestamp")
                if not nx:
                    break
                cur = nx
    except Exception as ex:
        print(f"  Warning: engineering casts fetch failed: {ex}")
        return {}
    return {nm: dict(d) for nm, d in sorted(counts.items())}


def _dedup_lust_windows(casts, gap_ms=30000):
    """Collapse lust CASTS into 30s WINDOWS — simultaneous casts (two shamans lusting together) are one
    window; a separate later cast (split lust, pull + execute) is its own. `casts` = [(ts, caster)] for
    one fight. Returns [[window_start_ts, [casters]]] in time order. Pure (testable)."""
    windows = []
    for ts, nm in sorted(casts):
        if windows and ts - windows[-1][0] < gap_ms:
            if nm not in windows[-1][1]:
                windows[-1][1].append(nm)
        else:
            windows.append([ts, [nm]])
    return windows


def fetch_bloodlust_windows(token, report_code, kills, md: dict | None = None):
    """Bloodlust/Heroism WINDOW VALUE — for each 30s lust window: the raid-DPS uplift over the fight
    baseline (was it a well-timed burn?) and the ≈boss-HP at cast (pull-burn vs execute-save). Pure WCL:
    a cheap abilityID-filtered Casts query for the lust casts + DamageDone TABLE windows (one aliased
    query per lust-fight — bounded, no event paging). The HP figure is DAMAGE-progress based (cumulative
    raid damage ÷ total) — ≈ boss HP on single-target fights, a proxy on add/heal/phase fights — the
    honest 'where in the kill' signal, NOT exact boss HP. Returns
      {fights:[{boss, encounter_id, windows:[{at_sec, casters, window_dps, baseline_dps, uplift_pct,
      hp_pct}]}]} — {} when no lust was cast."""
    if not kills:
        return {}
    if md is None:
        raise ValueError("md is required — pass the shared fetch_master_data() result")
    id2name = md["id2name"]
    bl_ids = sorted({a.get("gameID") for a in (md.get("abilities") or [])
                     if (a.get("name") or "") in ("Bloodlust", "Heroism")})
    if not bl_ids:
        print("  ✓ bloodlust windows: no Bloodlust/Heroism cast in report")
        return {}
    fids = [f["id"] for f in kills]
    st = min(f["startTime"] for f in kills)
    en = max(f["endTime"]   for f in kills)
    casts = defaultdict(list)            # fid → [(ts, caster)]
    QC = """query($c:String!,$ids:[Int]!,$st:Float!,$en:Float!,$a:Float!){reportData{report(code:$c){
        events(fightIDs:$ids, startTime:$st, endTime:$en, dataType: Casts, abilityID:$a,
               limit: 5000){ data nextPageTimestamp }}}}"""
    try:
        for aid in bl_ids:
            cur = st
            for _pg in range(MAX_EVENT_PAGES):
                ev = _report(gql(token, QC, {"c": report_code, "ids": fids, "st": cur, "en": en,
                                             "a": float(aid)}), "events", default={})
                for d in ev.get("data", []):
                    if d.get("type") == "cast":
                        fid, ts, nm = d.get("fight"), d.get("timestamp"), id2name.get(d.get("sourceID"))
                        if fid is not None and ts is not None and nm:
                            casts[fid].append((ts, nm))
                nx = ev.get("nextPageTimestamp")
                if not nx:
                    break
                cur = nx
    except Exception as ex:
        print(f"  Warning: bloodlust casts fetch failed: {ex}")
        return {}

    fid_meta = {f["id"]: (f["name"], f.get("encounterID"), f["startTime"], f["endTime"]) for f in kills}
    fights_out = []
    for f in kills:
        fid = f["id"]
        windows = _dedup_lust_windows(casts.get(fid, []))
        if not windows:
            continue
        boss, enc, f_s, f_e = fid_meta[fid]
        dur_s = (f_e - f_s) / 1000 or 1
        clauses = [f'base: table(dataType: DamageDone, fightIDs:[{int(fid)}])']
        for wi, (T, _c) in enumerate(windows):
            wend = min(T + 30000, f_e)
            clauses.append(f'w{wi}: table(dataType: DamageDone, fightIDs:[{int(fid)}], startTime:{int(T)}, endTime:{int(wend)})')
            clauses.append(f'c{wi}: table(dataType: DamageDone, fightIDs:[{int(fid)}], startTime:{int(f_s)}, endTime:{int(T)})')
        Q = "query($c:String!){reportData{report(code:$c){" + " ".join(clauses) + "}}}"
        try:
            rep = _report(gql(token, Q, {"c": report_code}), default={})
        except Exception as ex:
            print(f"  Warning: bloodlust window table failed on {boss}: {ex}")
            continue

        def _tot(alias):
            t = _loads_alias(rep.get(alias))
            return sum(e.get("total", 0) for e in (t or {}).get("data", {}).get("entries", []))

        total_dmg = _tot("base")
        baseline_dps = round(total_dmg / dur_s) if dur_s else 0
        wout = []
        for wi, (T, casters) in enumerate(windows):
            wend = min(T + 30000, f_e)
            wsec = (wend - T) / 1000 or 1
            wdps = round(_tot(f"w{wi}") / wsec)
            progress = (_tot(f"c{wi}") / total_dmg * 100) if total_dmg else 0
            wout.append({"at_sec": round((T - f_s) / 1000, 1), "casters": sorted(casters),
                         "window_dps": wdps, "baseline_dps": baseline_dps,
                         "uplift_pct": round((wdps / baseline_dps - 1) * 100, 1) if baseline_dps else 0,
                         "hp_pct": round(max(0.0, 100 - progress), 1)})   # ≈ boss HP at cast (damage proxy)
        fights_out.append({"boss": boss, "encounter_id": enc, "windows": wout})
    nwin = sum(len(f["windows"]) for f in fights_out)
    print(f"  ✓ bloodlust windows: {nwin} window(s) across {len(fights_out)} fight(s)")
    return {"fights": fights_out}


def _tank_survival_grade(tm: dict, deaths: int, cls: str = "") -> dict:
    """Absolute tank survivability grade (0–100) from WCL-durable mitigation signals — NOT a
    cohort percentile (WCL exposes no damage-taken ranking, and tank DTPS is MT/OT-confounded;
    TBC tanks are judged on hard thresholds instead). Crit-immunity is the load-bearing binary
    check (a boss crit taken = not defense/resilience-capped). Returns {score, flags:[...]} so the
    officer view can show the *why*, not just the number.

    Class-aware where it matters: bears (Druid) cannot block, so they CAN'T reach 102.4%
    uncrushable — crushing blows are unavoidable mechanics for them, not a failure, so no crush
    penalty. Warr/pala (can Shield Block) are still graded on crushes. v1 and TUNABLE: the crit/
    death/CD weights are first-cut. Inputs are all already on the tankScorecard row (WCL-durable)."""
    crit  = tm.get("crit_count", 0) or 0
    crush = tm.get("crush_count", 0) or 0
    score = 100
    flags = []
    if crit > 0:                                  # uncrittable — the hard gear check
        score -= min(40, 20 + crit * 10)
        flags.append(f"{crit} crit{'s' if crit > 1 else ''} taken — not crit-immune")
    if crush > 0 and cls != "Druid":              # bears can't block → crushing is unavoidable
        score -= min(25, crush)
        flags.append(f"{crush} crushing blow{'s' if crush > 1 else ''}")
    if deaths > 0:                                # a tank death is the clearest failure
        score -= min(30, deaths * 15)
        flags.append(f"died {deaths}×" if deaths > 1 else "died once")
    # NOTE: defensive-CD usage moved to the EXECUTION pillar (it's an input/skill signal); Survival
    # is now PURE OUTCOMES — crushes/crits/deaths. CD discipline lives in _perfRows' tank exec.
    return {"score": max(0, score), "flags": flags}


def _merge_bands(bands: list) -> float:
    """Total covered time (ms) of a set of {startTime,endTime} intervals, merging overlaps."""
    if not bands:
        return 0.0
    iv = sorted(([b.get("startTime", 0), b.get("endTime", 0)] for b in bands), key=lambda x: x[0])
    covered, cs, ce = 0.0, iv[0][0], iv[0][1]
    for s, e in iv[1:]:
        if s <= ce:
            ce = max(ce, e)
        else:
            covered += ce - cs
            cs, ce = s, e
    return covered + (ce - cs)

def _totem_uptime(ts: list, kills: list, dur_ms: int) -> float:
    """Modeled uptime% of a totem from its recast timestamps. Each cast covers a `dur_ms`
    band (the totem's duration), clamped to the fight; a cast within `dur_ms` of the pull
    implies the totem was pre-dropped, so coverage extends back to fight start. Bands merged
    per fight, summed over all kills. (TBC 2.5 doesn't log totem pulse buffs as auras, so this
    cadence model is the best available — present it as an estimate.)"""
    covered = total = 0
    for f in kills:
        s, e = f["startTime"], f["endTime"]
        total += (e - s)
        casts = [t for t in ts if s <= t <= e]
        if not casts:
            continue
        bands = [{"startTime": max(s, t), "endTime": min(e, t + dur_ms)} for t in casts]
        if casts[0] - s <= dur_ms:                      # pre-pull drop → cover the lead-in
            bands.append({"startTime": s, "endTime": min(e, casts[0])})
        covered += _merge_bands(bands)
    return round(covered / total * 100, 1) if total else 0


def _air_totem_uptime(wf: list, goa: list, kills: list, dur_ms: int) -> float:
    """AIR-SLOT-EXCLUSIVE Windfury Totem uptime% — the WF coverage a shaman PROVIDES TO HIS PARTY,
    modeled from casts (2.5 doesn't log totem auras + WCL exposes no party membership, so cast
    reconstruction is the only signal). Windfury and Grace of Air share the ONE exclusive air-totem
    slot, so dropping GoA REMOVES Windfury: each WF cast covers a band ending at the EARLIEST of the
    next air-slot cast after it (a WF re-drop OR a GoA drop), `cast+dur_ms`, or fight end. GoA casts
    only TRUNCATE WF, never add coverage — so a WF↔GoA twister correctly reads LOWER than a parker.
    `goa=[]` reproduces _totem_uptime exactly (the no-twist case)."""
    air = sorted(set(wf) | set(goa))                    # every air-slot drop = a truncation boundary
    covered = total = 0
    for f in kills:
        s, e = f["startTime"], f["endTime"]
        total += (e - s)
        wfc = sorted(t for t in wf if s <= t <= e)
        if not wfc:
            continue
        bands = []
        for t in wfc:
            nxt = next((a for a in air if a > t), None)         # next air drop strictly after this WF
            end = min(e, t + dur_ms, nxt if nxt is not None else e)
            bands.append({"startTime": max(s, t), "endTime": end})
        if wfc[0] - s <= dur_ms:                                # pre-pull WF still up at the pull…
            cut = next((a for a in air if s < a < wfc[0]), wfc[0])   # …until the first GoA in the gap
            bands.append({"startTime": s, "endTime": cut})
        covered += _merge_bands(bands)
    return round(covered / total * 100, 1) if total else 0


def fetch_parse_percentiles(token: str, report_code: str, kills: list) -> dict:
    """Per-player WCL PARSE % (rankPercent — the 'All Stars' percentile vs the FULL logged
    population, not just the top-100 leaderboard) averaged across the night's kills. This is the
    colored number on the WCL report page — 50 = a typical logged raider, 95+ = elite. Sourced
    from report.rankings: the `dps` metric covers DPS *and* tanks (tank threat = their dps parse);
    `hps` covers healers. Returns {name: {"dps": pct|None, "hps": pct|None}}.

    Replaces the old cohort-ratio WAR: that compared to the top-100 parses (≈99th percentile), so a
    solid raider read ~0.7 'below replacement'; this scores against everyone, so the same raider
    reads ~75. bracketPercent (ilvl-adjusted) is null on TBC Anniversary logs → rankPercent is
    authoritative. Values are None when WCL hasn't RANKED the report yet (a report pulled minutes
    after raid) — the cell then degrades to '—' until a later run picks the ranking up."""
    fids = [f["id"] for f in kills]
    if not fids:
        return {}
    acc = defaultdict(lambda: {"dps": [], "hps": []})
    for metric in ("dps", "hps"):
        Q = "query($c:String!){reportData{report(code:$c){rankings(playerMetric:" + metric + ")}}}"
        try:
            raw = _report(gql(token, Q, {"c": report_code}), "rankings", default={})
        except Exception as ex:
            print(f"  Warning: report.rankings({metric}) failed: {ex}")
            continue
        if isinstance(raw, str):
            raw = json.loads(raw)
        for blk in (raw.get("data") or []):
            if not blk.get("kill"):
                continue
            for _role, rv in (blk.get("roles") or {}).items():
                for c in (rv.get("characters") or []):
                    rp = c.get("rankPercent")
                    if rp is not None and c.get("name"):
                        acc[c["name"]][metric].append(rp)
    out = {nm: {"dps": round(sum(v["dps"]) / len(v["dps"])) if v["dps"] else None,
                "hps": round(sum(v["hps"]) / len(v["hps"])) if v["hps"] else None}
           for nm, v in acc.items()}
    n = sum(1 for v in out.values() if v["dps"] is not None or v["hps"] is not None)
    print(f"  ✓ parse percentiles: {n} players ranked" if n else
          "  ⚠ parse percentiles: report not ranked by WCL yet (cells show —)")
    return out




def fetch_saves(token: str, report_code: str, kills: list, md: dict | None = None) -> dict:
    """Per-player protective/external casts ON ALLIES — the 'saving others' kit (paladin Hand of
    Protection / Sacrifice / Freedom / Salvation, Lay on Hands on others, Cleanse + dispels, druid
    Rebirth, warlock Soulstone res, priest Pain Suppression…). Pure WCL Casts events filtered to
    EXTERNAL_ABILITIES (name-keyed; Anniversary-verified) with a friendly-PLAYER target that isn't
    the caster — so self-casts, untargeted totems, and Environment-target trinket procs all drop.
    Returns {name: {save, dispel, utility, total, targets:{ability:{target:count}}}}.
    WCL-durable; no combat log needed (a future pass can flag CLUTCH saves by cross-referencing the
    target's HP at cast time from the log)."""
    if md is None:
        # one masterData fetch per run (b9be8d9) — a silent re-fetch here is how that rule
        # erodes, so fail loud instead. Pass build_week_data's shared `md`.
        raise ValueError("md is required — pass the shared fetch_master_data() result")
    gid2name, id2name, act = md["gid2name"], md["id2name"], md["acts"]
    fids = [f["id"] for f in kills]
    if not fids:
        return {}
    win_s = min(f["startTime"] for f in kills)
    win_e = max(f["endTime"]   for f in kills)
    Q = """query($c:String!,$ids:[Int]!,$st:Float!,$en:Float!){reportData{report(code:$c){
        events(fightIDs:$ids, startTime:$st, endTime:$en, dataType: Casts, limit: 10000){
            data nextPageTimestamp }}}}"""
    out = defaultdict(lambda: {"save": 0, "dispel": 0, "utility": 0, "total": 0,
                               "targets": defaultdict(lambda: defaultdict(int))})
    st = win_s
    while True:
        try:
            ev = _report(gql(token, Q, {"c": report_code, "ids": fids, "st": st, "en": win_e}),
                         "events", default={})
        except Exception as ex:
            print(f"  Warning: saves Casts events failed: {ex}")
            break
        for d in ev.get("data", []):
            if d.get("type") != "cast":
                continue
            cat = EXTERNAL_ABILITIES.get(gid2name.get(d.get("abilityGameID"), ""))
            if not cat or cat == "dispel":
                # dispels now live in their OWN card (fetch_dispels, from Dispels events — actual
                # successful removals + offensive purges), so they're filtered off "Saving Others"
                # to avoid double-counting. Saves here = protective saves + reactive utility only.
                continue
            sid, tid = d.get("sourceID"), d.get("targetID")
            if tid is None or tid == sid:                      # self / untargeted → not "on an ally"
                continue
            if act.get(sid, {}).get("type") != "Player":       # caster must be a raider
                continue
            if act.get(tid, {}).get("type") != "Player":       # target must be a friendly player
                continue
            nm, tgt = id2name.get(sid), id2name.get(tid)
            if not nm or not tgt:
                continue
            r = out[nm]
            r[cat] += 1
            r["total"] += 1
            r["targets"][gid2name.get(d.get("abilityGameID"), "")][tgt] += 1
        nx = ev.get("nextPageTimestamp")
        if not nx:
            break
        st = nx
    return {nm: {"save": r["save"], "dispel": r["dispel"], "utility": r["utility"],
                 "total": r["total"],
                 "targets": {ab: dict(tg) for ab, tg in r["targets"].items()}}
            for nm, r in out.items()}


def fetch_interrupts(token: str, report_code: str, kills: list, md: dict | None = None) -> dict:
    """WCL-durable interrupt HEADLINE — per-interrupter count + which enemy casts were stopped.
    From events(dataType: Interrupts): the table() form returns null on the 2.5 Anniversary client,
    but the events query works (recon 2026-06-11, docs/WCL_API_SURFACE.md). Each interrupt event
    carries `extraAbilityGameID` = the spell that got cut, so we get the same "spells stopped" tally
    the combat log gave — now WCL-sourced. Pet interrupts (Felhunter Spell Lock) credit the owning
    raider via petOwner. The combat log stays a SILENT fallback in map_to_week_data (a missing log
    thins but never blanks this KPI). Returns {name: {count, spells:{interruptedSpellName: n}}}."""
    if not kills:
        return {}
    if md is None:
        # one masterData fetch per run (b9be8d9) — a silent re-fetch here is how that rule
        # erodes, so fail loud instead. Pass build_week_data's shared `md`.
        raise ValueError("md is required — pass the shared fetch_master_data() result")
    gid2name, acts = md["gid2name"], md["acts"]

    def src_player(sid):
        a = acts.get(sid, {})
        if a.get("type") == "Pet":                  # Felhunter Spell Lock → credit the warlock
            a = acts.get(a.get("petOwner"), {})
        return a.get("name") if a.get("type") == "Player" else None

    fids = [f["id"] for f in kills]
    win_s = min(f["startTime"] for f in kills)
    win_e = max(f["endTime"]   for f in kills)
    Q = """query($c:String!,$ids:[Int]!,$st:Float!,$en:Float!){reportData{report(code:$c){
        events(fightIDs:$ids, startTime:$st, endTime:$en, dataType: Interrupts, limit: 10000){
            data nextPageTimestamp }}}}"""
    out = defaultdict(lambda: {"count": 0, "spells": defaultdict(int)})
    st = win_s
    for _pg in range(MAX_EVENT_PAGES):
        try:
            ev = _report(gql(token, Q, {"c": report_code, "ids": fids, "st": st, "en": win_e}),
                         "events", default={})
        except Exception as ex:
            print(f"  Warning: interrupts events failed: {ex}")
            break
        for d in ev.get("data", []):
            if d.get("type") != "interrupt":
                continue
            nm = src_player(d.get("sourceID"))
            if not nm:
                continue
            r = out[nm]
            r["count"] += 1
            stopped = gid2name.get(d.get("extraAbilityGameID"), "")
            if stopped:
                r["spells"][stopped] += 1
        nx = ev.get("nextPageTimestamp")
        if not nx:
            break
        st = nx
    return {nm: {"count": r["count"], "spells": dict(r["spells"])} for nm, r in out.items()}


# Harmful debuffs where reaction time is genuinely clutch (cleansed ASAP, not batched like stacking
# poisons) — surfaced with their cleanse latency. Mind Control is the verified anchor (the Kael break,
# already highlighted). By NAME (rank-stable); extend as more SSC/TK panic-dispels are name-verified live.
DANGEROUS_DISPELS = {"Mind Control"}


def fetch_dispels(token: str, report_code: str, kills: list, md: dict | None = None) -> dict:
    """Who-dispelled-what — a NEW WCL-durable Utility signal from events(dataType: Dispels) (table()
    is null on 2.5; events works — recon 2026-06-11). Each event: {sourceID, targetID, abilityGameID
    (the dispel), extraAbilityGameID (the REMOVED aura), isBuff}. Split by target side:
      • cleanse — harmful effect stripped off a friendly (Cleanse / Abolish / Remove Curse / Devour
        Magic on an ally). The defensive, "saved a teammate" half.
      • purge   — buff stripped off an ENEMY (shaman Purge, priest Dispel Magic, hunter Tranquilizing
        Shot enrage-strip, Felhunter Devour Magic). The offensive half — has no home elsewhere.
    Pet dispels credit the owning raider via petOwner. hostilityType defaults to Friendlies (source
    side), so only raider-cast dispels are returned. RESPONSIVENESS: each cleanse is matched to the
    debuff's land time (companion Debuffs-events query, see _dispel_latency) → per-dispeller median
    reaction + the clutch (DANGEROUS_DISPELS) breaks. Returns
      {name: {cleanse, purge, total, removed:{auraName:n}, targets:{allyName:n},
              lat_median_sec, lat_count, clutch:[{aura,sec}]}}  (targets = cleanse recipients; purge
    targets are bosses/adds, surfaced via the removed-aura names instead; lat_* None when nothing was
    timeable — e.g. all debuffs pre-applied before the logged window)."""
    if not kills:
        return {}
    if md is None:
        # one masterData fetch per run (b9be8d9) — a silent re-fetch here is how that rule
        # erodes, so fail loud instead. Pass build_week_data's shared `md`.
        raise ValueError("md is required — pass the shared fetch_master_data() result")
    gid2name, id2name, acts = md["gid2name"], md["id2name"], md["acts"]

    def src_player(sid):
        a = acts.get(sid, {})
        if a.get("type") == "Pet":                  # Felhunter Devour Magic → credit the warlock
            a = acts.get(a.get("petOwner"), {})
        return a.get("name") if a.get("type") == "Player" else None

    fids = [f["id"] for f in kills]
    win_s = min(f["startTime"] for f in kills)
    win_e = max(f["endTime"]   for f in kills)
    Q = """query($c:String!,$ids:[Int]!,$st:Float!,$en:Float!){reportData{report(code:$c){
        events(fightIDs:$ids, startTime:$st, endTime:$en, dataType: Dispels, limit: 10000){
            data nextPageTimestamp }}}}"""
    out = defaultdict(lambda: {"cleanse": 0, "purge": 0, "total": 0,
                               "removed": defaultdict(int), "targets": defaultdict(int)})
    cleanse_events = []          # (dispeller, ally_targetID, removed_aura_guid, dispel_ts) — for latency
    st = win_s
    for _pg in range(MAX_EVENT_PAGES):
        try:
            ev = _report(gql(token, Q, {"c": report_code, "ids": fids, "st": st, "en": win_e}),
                         "events", default={})
        except Exception as ex:
            print(f"  Warning: dispels events failed: {ex}")
            break
        for d in ev.get("data", []):
            if d.get("type") != "dispel":
                continue
            nm = src_player(d.get("sourceID"))
            if not nm:
                continue
            tid = d.get("targetID")
            tgt_is_enemy = acts.get(tid, {}).get("type") == "NPC"
            r = out[nm]
            r["total"] += 1
            removed = gid2name.get(d.get("extraAbilityGameID"), "")
            if removed:
                r["removed"][removed] += 1
            if tgt_is_enemy:                         # buff stripped off a boss/add
                r["purge"] += 1
            else:                                    # harmful effect cleansed off an ally
                r["cleanse"] += 1
                tgt = id2name.get(tid, "")
                if tgt:
                    r["targets"][tgt] += 1
                cleanse_events.append((nm, tid, d.get("extraAbilityGameID"), d.get("timestamp")))
        nx = ev.get("nextPageTimestamp")
        if not nx:
            break
        st = nx

    # ── Dispel RESPONSIVENESS — latency from a harmful debuff LANDING on an ally to being cleansed.
    # Needs the apply side (the Dispels stream only has the removal), so one companion Debuffs-events
    # query per cleansed aura (Friendlies) gives the applydebuff timestamps; match each cleanse to the
    # most-recent prior apply on the same (aura, target). Pure WCL — degrades silently to no latency.
    lat, clutch = _dispel_latency(token, report_code, fids, win_s, win_e, cleanse_events, gid2name)

    res = {}
    for nm, r in out.items():
        lats = lat.get(nm, [])
        res[nm] = {"cleanse": r["cleanse"], "purge": r["purge"], "total": r["total"],
                   "removed": dict(r["removed"]), "targets": dict(r["targets"]),
                   "lat_median_sec": round(_median(lats) / 1000, 1) if lats else None,
                   "lat_count": len(lats),
                   "clutch": sorted(clutch.get(nm, []), key=lambda x: x["sec"])}
    return res


def _dispel_latency(token, report_code, fids, win_s, win_e, cleanse_events, gid2name):
    """Match each cleanse to the harmful debuff's LAND time → per-dispeller cleanse latencies (ms) +
    the clutch (DANGEROUS_DISPELS) breaks with their seconds. One Debuffs-events query per cleansed
    aura (Friendlies, applydebuff = the land). Returns (lat:{name:[ms]}, clutch:{name:[{aura,sec}]})."""
    cleansed_guids = sorted({g for (_n, _t, g, _ts) in cleanse_events if g})
    if not cleansed_guids:
        return {}, {}
    applies = defaultdict(list)        # (guid, targetID) → [applydebuff timestamps]
    QA = """query($c:String!,$ids:[Int]!,$st:Float!,$en:Float!,$a:Float!){reportData{report(code:$c){
        events(fightIDs:$ids, startTime:$st, endTime:$en, dataType: Debuffs, hostilityType: Friendlies,
               abilityID:$a, limit: 10000){ data nextPageTimestamp }}}}"""
    try:
        for g in cleansed_guids:
            cur = win_s
            for _pg in range(MAX_EVENT_PAGES):
                ev = _report(gql(token, QA, {"c": report_code, "ids": fids, "st": cur, "en": win_e,
                                             "a": float(g)}), "events", default={})
                for d in ev.get("data", []):
                    if d.get("type") == "applydebuff" and d.get("targetID") is not None \
                            and d.get("timestamp") is not None:
                        applies[(g, d["targetID"])].append(d["timestamp"])
                nx = ev.get("nextPageTimestamp")
                if not nx:
                    break
                cur = nx
    except Exception as ex:
        print(f"  Warning: dispel-latency apply events failed: {ex}")
        return {}, {}
    return _latency_match(cleanse_events, applies, gid2name)


def _latency_match(cleanse_events, applies, gid2name):
    """Pure matcher: each cleanse → the harmful debuff's most-recent prior LAND on that (aura, target),
    so latency = dispel − land. `applies` is {(guid, targetID): [applydebuff timestamps]}. A cleanse
    with no recorded apply (the debuff was pre-applied before the logged window) is skipped — it can't
    be timed. Returns (lat:{name:[latency_ms]}, clutch:{name:[{aura,sec}]} for DANGEROUS_DISPELS)."""
    lat, clutch = defaultdict(list), defaultdict(list)
    for (name, tid, g, dts) in cleanse_events:
        if g is None or dts is None:
            continue
        arr = applies.get((g, tid))
        if not arr:
            continue                   # debuff was pre-applied before the logged window — can't time it
        ats = max((a for a in arr if a is not None and a <= dts), default=None)
        if ats is None:
            continue
        ms = dts - ats
        lat[name].append(ms)
        aura = gid2name.get(g, "")
        if aura in DANGEROUS_DISPELS:
            clutch[name].append({"aura": aura, "sec": round(ms / 1000, 1)})
    return lat, clutch


def fetch_ability_icons(token: str, report_code: str, fight_ids: list, md: dict | None = None) -> dict:
    """Map ability name → real WCL icon slug (no .jpg). Two layers:
    1. masterData abilities — covers EVERY ability in the report, including casts that
       never hit the raid (heals, interrupted spells like Holy Smite / Great Heal).
    2. DamageTaken table — authoritative icon for whatever actually hit the raid; overrides
       layer 1 to sidestep the wrong-spell-ID / reused-asset problem for raid-facing hits."""
    if md is None:
        # one masterData fetch per run (b9be8d9) — a silent re-fetch here is how that rule
        # erodes, so fail loud instead. Pass build_week_data's shared `md`.
        raise ValueError("md is required — pass the shared fetch_master_data() result")
    # layer 1 — masterData (precomputed in md; so interrupted/healing casts resolve an icon too)
    icons = dict(md["icons"])
    # layer 2 — DamageTaken (authoritative for raid hits; overrides layer 1)
    if fight_ids:
        Q = """query($c:String!,$f:[Int]){reportData{report(code:$c){
            table(dataType: DamageTaken, fightIDs:$f, hostilityType:Friendlies)}}}"""
        try:
            t = _report(gql(token, Q, {"c": report_code, "f": fight_ids}), "table", default={})
            if isinstance(t, str):
                t = json.loads(t)
            for e in t.get("data", {}).get("entries", []):
                for ab in (e.get("abilities") or []):
                    nm, ic = ab.get("name"), ab.get("icon")
                    if nm and ic:
                        icons[nm] = ic.replace(".jpg", "")
        except Exception as e:
            print(f"  Warning: ability-icon fetch failed: {e}")
    return icons


def fetch_deaths_split(token: str, report_code: str, md: dict | None = None):
    """Curated deaths from the WCL Deaths table (WCL excludes Hunter Feign Death,
    unlike raw combat-log UNIT_DIED). Split boss vs trash by each death's fight —
    boss fights carry an encounterID, trash fights don't.
    Returns (boss_deaths, trash_deaths, recaps, deaths_by_boss): per-PLAYER boss/trash
    counts, a per-player list of killing blows {boss, killer, amount, overkill} for the
    death drill-down, and a per-BOSS death tally {boss_name: count} for the Overview tiles."""
    Qf = """query($c:String!){reportData{report(code:$c){fights{ id name encounterID kill }}}}"""
    fights = _report(gql(token, Qf, {"c": report_code}), "fights", default=[])
    if not fights:                                 # partial payload — degrade to an empty section
        return {}, {}, {}, {}
    fid_is_boss = {f["id"]: bool(f["encounterID"]) for f in fights}
    fid_is_kill = {f["id"]: bool(f.get("kill")) for f in fights}
    fid_name    = {f["id"]: f.get("name", "") for f in fights}

    # id → name maps to label the per-death recap timeline (abilities + ALL actors, including
    # NPCs so boss-ability sources resolve) — from the shared masterData fetch.
    if md is None:
        # one masterData fetch per run (b9be8d9) — a silent re-fetch here is how that rule
        # erodes, so fail loud instead. Pass build_week_data's shared `md`.
        raise ValueError("md is required — pass the shared fetch_master_data() result")
    abil_name  = md["gid2name"]
    actor_name = md["id2name"]

    Qd = """query($c:String!,$f:[Int]){reportData{report(code:$c){
        table(dataType: Deaths, fightIDs:$f)}}}"""
    t = _report(gql(token, Qd, {"c": report_code, "f": list(fid_is_boss)}), "table", default={})
    if isinstance(t, str):
        t = json.loads(t)
    boss, trash = {}, {}
    deaths_by_boss = defaultdict(int)   # per-encounter tally for the Overview boss tiles
    recaps = defaultdict(list)
    for e in t.get("data", {}).get("entries", []):
        nm  = e.get("name")
        fid = e.get("fight")
        d = boss if fid_is_boss.get(fid) else trash
        d[nm] = d.get(nm, 0) + 1
        # tile tally counts the KILL pull only — wipe-attempt deaths would inflate it
        # (a 25-man wipe is 25 deaths), which reads as alarm rather than signal.
        if fid_is_kill.get(fid):
            deaths_by_boss[fid_name.get(fid, "")] += 1
        kb  = e.get("killingBlow") or {}
        evs = e.get("events") or []
        # events are NEWEST-first → the fatal hit is the LATEST timestamp, not evs[-1]
        kbe = max(evs, key=lambda x: x.get("timestamp", 0)) if evs else {}
        if len(recaps[nm]) < 15:
            recaps[nm].append({
                "boss":     fid_name.get(fid, "") if fid_is_boss.get(fid) else "Trash",
                "killer":   kb.get("name", "?"),
                "amount":   kbe.get("amount", 0),
                "overkill": kbe.get("overkill", 0),
                # window context — was it a burst or a heal gap? (damage/healing are
                # objects {total,...} in the Deaths table, so pull .total)
                "window_dmg":  (e.get("damage")  or {}).get("total", 0) if isinstance(e.get("damage"),  dict) else (e.get("damage")  or 0),
                "window_heal": (e.get("healing") or {}).get("total", 0) if isinstance(e.get("healing"), dict) else (e.get("healing") or 0),
                "window_s":    round((e.get("deathWindow", 0) or 0) / 1000.0, 1),
                # the final-seconds blow-by-blow (damage + healing), HP reconstructed
                "timeline":    _build_death_timeline(evs, abil_name, actor_name),
            })
    return boss, trash, dict(recaps), dict(deaths_by_boss)


def _build_death_timeline(evs, abil_name, actor_name, max_events=22):
    """Compact the WCL death-recap events into [{t,type,amt,over,ability,src,hp,mx}],
    timestamps RELATIVE to the killing blow (0.0s). HP from each event's target
    resources. Fully defensive — returns [] on any malformed/absent input so a bad
    event can never break the weekly run."""
    try:
        if not evs:
            return []
        # WCL returns death-recap events NEWEST-first — sort ascending so the timeline
        # reads oldest→killing-blow and times are relative to the (latest) fatal hit.
        evs = sorted(evs, key=lambda e: e.get("timestamp", 0))
        death_ts = evs[-1].get("timestamp", 0)
        pts = []
        for ev in evs:
            typ = ev.get("type")
            if   typ == "damage": kind = "dmg"
            elif typ == "heal":   kind = "heal"
            else:                 continue
            amt  = int(ev.get("amount", 0) or 0)
            over = ev.get("overkill") if kind == "dmg" else ev.get("overheal")
            over = max(0, int(over or 0))
            # Deaths-table events embed an `ability` OBJECT ({name, guid, abilityIcon}) — NOT the
            # abilityGameID field regular events carry (probed live 2026-06-13; reading only
            # abilityGameID labeled every recap hit "Melee"). The embedded name is authoritative;
            # the masterData map stays as fallback for any event that lacks it.
            abil = ev.get("ability") or {}
            abid = abil.get("guid", ev.get("abilityGameID"))
            sid  = ev.get("sourceID")
            ability = (abil.get("name") or abil_name.get(abid)
                       or ("Melee" if abid in (0, 1, None) else "(spell)"))
            pt = {"t": round((ev.get("timestamp", death_ts) - death_ts) / 1000.0, 1),
                  "type": kind, "amt": amt, "over": over,
                  "ability": ability, "src": actor_name.get(sid) or "—"}
            hp = ev.get("hitPoints")
            mx = ev.get("maxHitPoints")
            if hp is not None: pt["hp"] = int(hp)
            if mx:             pt["mx"] = int(mx)
            pts.append(pt)
        if pts:
            pts[-1]["killing"] = True
        return pts[-max_events:]
    except Exception:
        return []


# ══════════════════════════════════════════════════════════════════════════════
# Debuff coverage — uptime of key DPS-amplifying raid debuffs on each boss (pure WCL)
# ══════════════════════════════════════════════════════════════════════════════

# Each slot = one raid responsibility; ANY of its GUIDs satisfies it (Sunder OR Expose;
# Faerie Fire normal OR feral; CoE ranks 27228/27229). Uptime = UNION of every matching aura's bands
# (NB: there is no "Curse of Shadow" in TBC — it was folded into Curse of the Elements, which already
#  covers Shadow. See docs/TBC_RAID_MECHANICS.md.)
# / fight duration, so two fills covering different windows add up correctly. GUIDs + icons
# verified against live TBC 2.5 Debuffs-table data (hostilityType: Enemies) — not memory.
# `soft` flags proc-based debuffs (ISB, Crusader) whose natural uptime ceiling is lower, so
# the dashboard grades them on a gentler scale instead of reading a healthy 60% as "failing".
DEBUFF_SLOTS = [
    {"key": "coe",    "label": "Curse of Elements",  "cat": "Magic",   "guids": [27228, 27229], "icon": "spell_shadow_chilltouch"},
    {"key": "sweav",  "label": "Shadow Weaving",     "cat": "Magic",   "guids": [15258],        "icon": "spell_shadow_blackplague"},
    {"key": "isb",    "label": "Shadow Vuln. (ISB)", "cat": "Magic",   "guids": [17800],        "icon": "spell_shadow_shadowbolt", "soft": True},
    {"key": "misery", "label": "Misery",             "cat": "Magic",   "guids": [33200],        "icon": "spell_shadow_misery"},
    {"key": "sunder", "label": "Sunder / Expose",    "cat": "Armor",   "guids": [25225, 26866], "icon": "ability_warrior_riposte"},
    {"key": "ff",     "label": "Faerie Fire",        "cat": "Armor",   "guids": [26993, 27011], "icon": "spell_nature_faeriefire"},
    {"key": "creck",  "label": "Curse of Reckless.", "cat": "Armor",   "guids": [27226],        "icon": "spell_shadow_unholystrength"},
    {"key": "exposew","label": "Expose Weakness",    "cat": "Armor",   "guids": [34501],        "icon": "ability_rogue_findweakness"},  # Survival hunter, +AP for all physical (live-verified guid)
    # NOTE: Blood Frenzy is NOT trackable as a debuff slot. It's a hidden passive talent ("Aura is
    # hidden", Wowhead 29859) — the +4% physical is baked into Rend/Deep Wounds with no separate aura.
    # 29859 is the talent ID, never an enemy aura, so a slot for it would read 0% forever. The only
    # signal would be (talent-specced warrior) + Deep Wounds/Rend uptime as a proxy — and Deep Wounds is
    # applied by ANY warrior regardless of the talent, so that proxy over-credits. Left out by design.
    {"key": "jow",    "label": "Judge: Wisdom",      "cat": "Utility", "guids": [27164],        "icon": "spell_holy_righteousnessaura"},
    {"key": "jotc",   "label": "Judge: Crusader",    "cat": "Utility", "guids": [27159],        "icon": "spell_holy_holysmite", "soft": True},
]

# Max stack for the genuinely STACKING raid debuffs — these are the ones whose "ramp" is a real story
# (how fast the raid built the full debuff). Everything else is single-application: "ramp" degrades to
# time-to-first-applied (was it up at the pull or late?). ISB is proc-based/noisy, so it's left single
# on purpose (a declared "never hit N" would be RNG noise). Live-verified: Shadow Weaving stacks to 5.
DEBUFF_STACK_MAX = {"sweav": 5, "sunder": 5}

def fetch_debuff_coverage(token: str, report_code: str, kills: list) -> dict:
    """WCL-durable raid debuff coverage — runs EVERY week, no combat log needed. For each
    boss kill, the % of fight time each key DPS-amplifying debuff was up on an enemy (WCL
    Debuffs table, hostilityType: Enemies). A slot's uptime is the UNION of every matching
    aura's bands, so alternate fills (Sunder/Expose, Faerie Fire normal/feral) combine.
    Returns {slots:[{key,label,cat,icon,soft}],
             bosses:[{boss,encounter_id,seconds,coverage:{key:pct}}],
             raid_avg:{key:pct}}  — or {} when there are no kills."""
    if not kills:
        return {}
    guid_slots = {}                         # guid → [slot keys it satisfies]
    for s in DEBUFF_SLOTS:
        for g in s["guids"]:
            guid_slots.setdefault(g, []).append(s["key"])
    live_icon = {}                          # slot key → live WCL icon slug (preferred)
    bosses = []
    for i in range(0, len(kills), 5):
        chunk = kills[i:i + 5]
        aliases = "\n".join(
            f'f{f["id"]}: table(dataType: Debuffs, hostilityType: Enemies, fightIDs:[{int(f["id"])}])'
            for f in chunk)
        Q = f"query($c:String!){{reportData{{report(code:$c){{ {aliases} }}}}}}"
        try:
            rep = _report(gql(token, Q, {"c": report_code}), default={})
        except Exception as ex:
            print(f"  Warning: debuff-coverage batch failed: {ex}")
            rep = {}
        for f in chunk:
            fid = f["id"]
            dur_ms = (f["endTime"] - f["startTime"]) or 1
            t = _loads_alias(rep.get(f'f{fid}'))
            auras = (t or {}).get("data", {}).get("auras", []) if t else []
            slot_bands = {s["key"]: [] for s in DEBUFF_SLOTS}
            for a in auras:
                for key in guid_slots.get(a.get("guid"), ()):
                    slot_bands[key].extend(a.get("bands") or [])
                    if a.get("abilityIcon"):
                        live_icon.setdefault(key, a["abilityIcon"].replace(".jpg", ""))
            coverage = {s["key"]: round(_merge_bands(slot_bands[s["key"]]) / dur_ms * 100, 1)
                        for s in DEBUFF_SLOTS}
            bosses.append({"boss": f["name"], "encounter_id": f.get("encounterID"),
                           "seconds": round(dur_ms / 1000), "coverage": coverage})
    raid_avg = {}
    for s in DEBUFF_SLOTS:
        vals = [b["coverage"][s["key"]] for b in bosses]
        raid_avg[s["key"]] = round(sum(vals) / len(vals), 1) if vals else 0
    slots_out = [{"key": s["key"], "label": s["label"], "cat": s["cat"],
                  "icon": live_icon.get(s["key"], s["icon"]), "soft": s.get("soft", False)}
                 for s in DEBUFF_SLOTS]
    print(f"  ✓ debuff coverage: {len(bosses)} bosses, {len(DEBUFF_SLOTS)} debuffs tracked")
    return {"slots": slots_out, "bosses": bosses, "raid_avg": raid_avg}


# Raid-facing mana batteries (energize the raid) — what refills the healer corps + casters,
# split by source so each totem/ability is its own leaderboard. Self counts (the provider is a
# raid member). Self-only sources (mana gems, Dark/Demonic Rune, Evocation, Life Tap, Spiritual
# Attunement) and Judgement of Wisdom (its energize goes to the ATTACKER, not the paladin, and
# feeds melee/casters) are out. Innervate is tracked separately — it emits no mana event (it
# boosts spirit regen, logged as the target's passive ticks), so it's a cast COUNT, not mana.
MANA_SOURCES = [
    {"match": "Vampiric Touch",  "label": "Vampiric Touch",    "icon": "spell_holy_stoicism"},
    {"match": "Mana Tide Totem", "label": "Mana Tide Totem",   "icon": "spell_frost_summonwaterelemental_2"},
    {"match": "Mana Spring",     "label": "Mana Spring Totem", "icon": "spell_nature_manaregentotem"},
]
INNERVATE_ICON = "spell_nature_lightning"

def fetch_mana_returns(token: str, report_code: str, kills: list, md: dict | None = None) -> dict:
    """Mana RETURNED TO THE RAID, grouped by SOURCE — the mana-battery leaderboards for the
    Healers & Tanks tab. From WCL Resources `resourcechange` energize events (resourceChangeType
    0 = mana); provider is owner-resolved (totems log as a pet → credit the shaman via petOwner;
    VT logs the priest directly). Self mana counts (the provider is part of the raid). Innervate
    is added as a cast count (it emits no mana event). Returns
      {batteries:[{label,icon,total,providers:[{name,mana,receivers:[{name,mana}]}]}],
       innervate:{icon, casters:[{name,count,targets:[name]}]}}."""
    if not kills:
        return {}
    if md is None:
        # one masterData fetch per run (b9be8d9) — a silent re-fetch here is how that rule
        # erodes, so fail loud instead. Pass build_week_data's shared `md`.
        raise ValueError("md is required — pass the shared fetch_master_data() result")
    acts = md["acts"]
    def owner(sid):
        a = acts.get(sid, {})
        return acts.get(a.get("petOwner"), {}).get("name") if a.get("petOwner") else a.get("name")
    gid2src, innv_ids = {}, []
    for a in (md.get("abilities") or []):
        nm = a.get("name") or ""
        for s in MANA_SOURCES:
            if s["match"] in nm:
                gid2src[a.get("gameID")] = s
                break
        if nm == "Innervate":
            innv_ids.append(a.get("gameID"))
    fids = [f["id"] for f in kills]
    st   = min(f["startTime"] for f in kills)
    en   = max(f["endTime"]   for f in kills)
    # ── batteries: Resources energize events, per source → provider → mana + receivers ──
    bysrc = {s["label"]: defaultdict(lambda: {"mana": 0, "recv": defaultdict(int)})
             for s in MANA_SOURCES}
    QR = """query($c:String!,$ids:[Int]!,$st:Float!,$en:Float!){reportData{report(code:$c){
        events(fightIDs:$ids, startTime:$st, endTime:$en, dataType: Resources,
               limit: 10000){ data nextPageTimestamp }}}}"""
    cur = st
    try:
        for _pg in range(MAX_EVENT_PAGES):
            ev = _report(gql(token, QR, {"c": report_code, "ids": fids, "st": cur, "en": en}),
                         "events", default={})
            for d in ev.get("data", []):
                if d.get("type") != "resourcechange" or d.get("resourceChangeType") != 0:
                    continue
                amt = d.get("resourceChange", 0) or 0
                src = gid2src.get(d.get("abilityGameID"))
                if amt <= 0 or not src:
                    continue
                pname = owner(d.get("sourceID"))
                if not pname:
                    continue
                slot = bysrc[src["label"]][pname]
                slot["mana"] += amt                          # self included (raid member)
                rname = acts.get(d.get("targetID"), {}).get("name")
                if rname:
                    slot["recv"][rname] += amt
            nx = ev.get("nextPageTimestamp")
            if not nx:
                break
            cur = nx
    except Exception as ex:
        print(f"  Warning: mana-returns events failed: {ex}")
    batteries = []
    for s in MANA_SOURCES:
        provs = bysrc[s["label"]]
        if not provs:
            continue
        rows = []
        for nm, d in provs.items():
            recv = sorted(({"name": r, "mana": m} for r, m in d["recv"].items() if r != nm),
                          key=lambda x: -x["mana"])[:3]
            rows.append({"name": nm, "mana": d["mana"], "receivers": recv})
        rows.sort(key=lambda x: -x["mana"])
        batteries.append({"label": s["label"], "icon": s["icon"],
                          "total": sum(r["mana"] for r in rows), "providers": rows})

    # ── Innervate: cast COUNT per druid + targets (no mana event exists) ──
    innv = defaultdict(lambda: {"count": 0, "targets": defaultdict(int)})
    if innv_ids:
        QI = """query($c:String!,$ids:[Int]!,$st:Float!,$en:Float!,$a:Float!){reportData{report(code:$c){
            events(fightIDs:$ids, startTime:$st, endTime:$en, dataType: Casts, abilityID:$a,
                   limit: 10000){ data nextPageTimestamp }}}}"""
        try:
            for aid in innv_ids:
                cur = st
                for _pg in range(MAX_EVENT_PAGES):
                    ev = _report(gql(token, QI, {"c": report_code, "ids": fids, "st": cur, "en": en,
                                                 "a": float(aid)}), "events", default={})
                    for d in ev.get("data", []):
                        if d.get("type") != "cast":
                            continue
                        nm = owner(d.get("sourceID"))
                        if not nm:
                            continue
                        innv[nm]["count"] += 1
                        tg = acts.get(d.get("targetID"), {}).get("name")
                        if tg and tg != nm:
                            innv[nm]["targets"][tg] += 1
                    nx = ev.get("nextPageTimestamp")
                    if not nx:
                        break
                    cur = nx
        except Exception as ex:
            print(f"  Warning: innervate fetch failed: {ex}")
    casters = sorted(({"name": nm, "count": d["count"],
                       "targets": [t for t, _ in sorted(d["targets"].items(), key=lambda x: -x[1])[:3]]}
                      for nm, d in innv.items()), key=lambda x: -x["count"])

    print(f"  ✓ mana returns: {len(batteries)} battery sources, {len(casters)} innervaters")
    return {"batteries": batteries, "innervate": {"icon": INNERVATE_ICON, "casters": casters}}


def fetch_sunder_armor(token: str, report_code: str, kills: list, md: dict | None = None) -> dict:
    """Per-player Sunder Armor quality — pure WCL, runs every week (no combat log). Sourced from
    the WCL `Debuffs` event stream for the Sunder Armor aura (over kill fights, enemy targets).
    Attribution is by `sourceID`, so it credits anyone who builds the stack, including a prot tank
    applying it via Devastate. (Rogue Expose Armor is a different debuff → excluded.)

      • effective = applydebuff + applydebuffstack  (applications that BUILT a stack, 1→5)
      • refreshed = refreshdebuff                   (upkeep casts on an already-existing stack)
      • total     = effective + refreshed           (every Sunder application by this player)

    The Debuffs stream is the authoritative source here: the WCL Casts stream does NOT reconcile
    1:1 with it (per-warrior cast counts come out *below* the stacks actually applied — e.g. 28
    stacks built from only 23 recorded casts), so a casts-minus-landed "wasted" figure would go
    negative and is intentionally not computed. Refresh framing is neutral upkeep, not a fault.

    Returns {players:[{name,total,effective,refreshed}]} sorted by total desc — {} when no kills."""
    if not kills:
        return {}
    if md is None:
        # one masterData fetch per run (b9be8d9) — a silent re-fetch here is how that rule
        # erodes, so fail loud instead. Pass build_week_data's shared `md`.
        raise ValueError("md is required — pass the shared fetch_master_data() result")
    id2name = md["id2name"]
    sunder_ids = [a.get("gameID") for a in (md.get("abilities") or [])
                  if (a.get("name") or "") == "Sunder Armor"]
    if not sunder_ids:
        print("  ✓ sunder armor: no Sunder Armor applications in report")
        return {}

    fids = [f["id"] for f in kills]
    st   = min(f["startTime"] for f in kills)
    en   = max(f["endTime"]   for f in kills)
    stats = defaultdict(lambda: {"effective": 0, "refreshed": 0})
    QD = """query($c:String!,$ids:[Int]!,$st:Float!,$en:Float!,$a:Float!){reportData{report(code:$c){
        events(fightIDs:$ids, startTime:$st, endTime:$en, dataType: Debuffs, hostilityType: Enemies,
               abilityID:$a, limit: 10000){ data nextPageTimestamp }}}}"""
    try:
        for aid in sunder_ids:
            cur = st
            for _pg in range(MAX_EVENT_PAGES):
                ev = _report(gql(token, QD, {"c": report_code, "ids": fids, "st": cur, "en": en,
                                             "a": float(aid)}), "events", default={})
                for d in ev.get("data", []):
                    nm = id2name.get(d.get("sourceID"))
                    if not nm:
                        continue
                    t = d.get("type")
                    if t in ("applydebuff", "applydebuffstack"):
                        stats[nm]["effective"] += 1
                    elif t == "refreshdebuff":
                        stats[nm]["refreshed"] += 1
                nx = ev.get("nextPageTimestamp")
                if not nx:
                    break
                cur = nx
    except Exception as ex:
        print(f"  Warning: sunder-armor debuff events failed: {ex}")

    players = []
    for nm, s in stats.items():
        total = s["effective"] + s["refreshed"]
        if total == 0:
            continue
        players.append({"name": nm, "total": total,
                        "effective": s["effective"], "refreshed": s["refreshed"]})
    players.sort(key=lambda x: -x["total"])
    print(f"  ✓ sunder armor: {len(players)} sunderers")
    return {"players": players}


def fetch_expose_armor(token: str, report_code: str, kills: list, md: dict | None = None) -> dict:
    """Per-rogue Expose Armor UPTIME on bosses — a Combat rogue's armor-debuff contribution. Expose
    Armor fills the SAME raid debuff slot as warrior Sunder Armor (the two are mutually exclusive — a
    rogue assigned Expose holds the slot so the warriors don't Sunder), but it's a DIFFERENT debuff, so
    fetch_sunder_armor excludes it and the per-rogue contribution went uncredited. Pure WCL: WCL Debuffs
    EVENTS for Expose Armor (over kill fights, enemy targets) → reconstruct each source's applied bands
    (applydebuff → removedebuff, per source+target so multi-add Expose doesn't double-count), merge,
    divide by total boss fight time. Attribution by `sourceID`. Returns
      {players:[{name, uptime, applications}]} sorted by uptime desc — {} when no kills / no Expose."""
    if not kills:
        return {}
    if md is None:
        raise ValueError("md is required — pass the shared fetch_master_data() result")
    id2name = md["id2name"]
    expose_ids = [a.get("gameID") for a in (md.get("abilities") or [])
                  if (a.get("name") or "") == "Expose Armor"]
    if not expose_ids:
        print("  ✓ expose armor: no Expose Armor applications in report")
        return {}

    fids = [f["id"] for f in kills]
    st   = min(f["startTime"] for f in kills)
    en   = max(f["endTime"]   for f in kills)
    total_ms = sum(f["endTime"] - f["startTime"] for f in kills) or 1
    bands = defaultdict(list)              # name → [{startTime,endTime}] (merged later)
    opened = {}                            # (sourceID, targetID) → band start ts
    apps   = defaultdict(int)              # name → apply+refresh count (maintenance volume)
    QD = """query($c:String!,$ids:[Int]!,$st:Float!,$en:Float!,$a:Float!){reportData{report(code:$c){
        events(fightIDs:$ids, startTime:$st, endTime:$en, dataType: Debuffs, hostilityType: Enemies,
               abilityID:$a, limit: 10000){ data nextPageTimestamp }}}}"""
    try:
        for aid in expose_ids:
            cur = st
            for _pg in range(MAX_EVENT_PAGES):
                ev = _report(gql(token, QD, {"c": report_code, "ids": fids, "st": cur, "en": en,
                                             "a": float(aid)}), "events", default={})
                for d in ev.get("data", []):
                    sid = d.get("sourceID")
                    nm = id2name.get(sid)
                    if not nm:
                        continue
                    t, ts, tid = d.get("type"), d.get("timestamp"), d.get("targetID")
                    key = (sid, tid)
                    if t == "applydebuff":
                        opened[key] = ts
                        apps[nm] += 1
                    elif t == "refreshdebuff":
                        apps[nm] += 1
                        opened.setdefault(key, ts)        # keep an open band alive
                    elif t == "removedebuff":
                        s0 = opened.pop(key, None)
                        if s0 is not None:
                            bands[nm].append({"startTime": s0, "endTime": ts})
                nx = ev.get("nextPageTimestamp")
                if not nx:
                    break
                cur = nx
    except Exception as ex:
        print(f"  Warning: expose-armor debuff events failed: {ex}")
    # close any band still up at the end of the logged window (debuff lasted to fight end)
    for (sid, tid), s0 in opened.items():
        nm = id2name.get(sid)
        if nm:
            bands[nm].append({"startTime": s0, "endTime": en})

    players = []
    for nm in sorted(set(bands) | set(apps)):
        up = round(_merge_bands(bands.get(nm, [])) / total_ms * 100, 1)
        if up <= 0 and apps.get(nm, 0) == 0:
            continue
        players.append({"name": nm, "uptime": up, "applications": apps.get(nm, 0)})
    players.sort(key=lambda x: (-x["uptime"], -x["applications"], x["name"]))
    print(f"  ✓ expose armor: {len(players)} rogue(s) maintaining the armor slot")
    return {"players": players}


# ── v2 INPUT-BASED PERFORMANCE: per-player MAINTAIN uptime ─────────────────────────────────────
# The MAINTAIN ability class (DPS_SPEC_PERFORMANCE_FRAMEWORK §1/§7) — DoTs and self-buffs whose
# UPTIME% (not cast count) is the honest signal: a cast-count over-credits clipping and can't see a
# lapsed DoT (the Vampiric Touch proof in §6 — a good priest shows ~100% uptime with FEW recasts,
# which a cast-count ranks BELOW a clipper). Also carries the spec-baseline UTILITY debuffs (handoff
# §C) a designated provider is INVITED for — same uptime fetch, scored in the Utility pillar.
#   kind "debuff"   = a debuff on the enemy (Debuffs table, hostilityType: Enemies, by sourceID).
#   kind "selfbuff" = a buff on self (Buffs table, by sourceID — the _buff_uptime_batch path).
# Names match the log / playerSpells verbatim; resolved name→gameID via md (rank-union) so ranks and
# Anniversary renames don't break it (the build_class_toolkit pattern). Moonkin Aura is intentionally
# ABSENT — it's a passive form aura (never cast), so the casts-applier prepass can't see it; the
# boomkin's baseline-utility credit rides on Improved Faerie Fire (cast, +spell-hit) instead.
MAINTAIN_UPTIME_ABILITIES = {
    # ── MAINTAIN (Performance overlay) ──
    "Corruption": "debuff", "Unstable Affliction": "debuff", "Siphon Life": "debuff",
    "Immolate": "debuff", "Shadow Word: Pain": "debuff", "Vampiric Touch": "debuff",
    "Moonfire": "debuff", "Insect Swarm": "debuff", "Serpent Sting": "debuff",
    "Flame Shock": "debuff", "Rupture": "debuff",
    "Slice and Dice": "selfbuff", "Lightning Shield": "selfbuff",
    # ── spec-baseline UTILITY credits (handoff §C) — scored in the Utility pillar, NOT Performance ──
    "Curse of the Elements": "debuff",   # Affliction — the dedicated CoE holder
    "Faerie Fire": "debuff",             # Balance — Improved Faerie Fire (+3% spell hit)
    "Expose Weakness": "debuff",         # Survival hunter — on-crit +AP for all physical
}


def fetch_maintain_uptime(token, report_code, kills, md: dict | None = None) -> dict:
    """Per-player UPTIME% of each tracked MAINTAIN ability + spec-baseline UTILITY debuff
    (MAINTAIN_UPTIME_ABILITIES) — the input-based Performance metric's v2 overlay. Uptime is the
    maintenance signal a cast-count can't give (clipping vs a lapsed DoT). Pure WCL, no combat log.

    Two-step (cheap): ONE Casts-events pagination over the kills builds {ability → who APPLIED it}
    (a non-provider never appears, so no roster lookup is needed), then ONE batched Debuffs/Buffs
    *table* query reads each real (provider, ability) uptime — debuffs as the merged enemy band
    union (≈ 'up on the boss'; a multi-add fight inflates it, capped at 100), self-buffs as the
    self totalUptime. Returns {name: {ability_name: uptime_pct}} — {} when no kills / no providers.

    Mirrors fetch_debuff_coverage (Debuffs table on Enemies + _merge_bands) and _buff_uptime_batch
    (sourceID-filtered self-aura totalUptime). Source-filtered, so attribution is exact."""
    if not kills:
        return {}
    if md is None:
        raise ValueError("md is required — pass the shared fetch_master_data() result")
    fids = [f["id"] for f in kills]
    st   = min(f["startTime"] for f in kills)
    en   = max(f["endTime"]   for f in kills)
    kdur = sum(f["endTime"] - f["startTime"] for f in kills) or 1
    id2name = md["id2name"]
    # tracked name (canonical) → [gameIDs] (rank-union); and gameID → canonical name (for the prepass)
    want = {n.lower(): n for n in MAINTAIN_UPTIME_ABILITIES}
    name_ids = defaultdict(list)
    gid2canon = {}
    for a in (md.get("abilities") or []):
        canon = want.get((a.get("name") or "").lower())
        if canon and a.get("gameID") is not None:
            name_ids[canon].append(a["gameID"])
            gid2canon[a["gameID"]] = canon
    if not gid2canon:
        return {}
    # ── prepass: who applied each tracked aura (one full Casts-events pagination, like toolkit) ──
    QC = """query($c:String!,$ids:[Int]!,$st:Float!,$en:Float!){reportData{report(code:$c){
        events(fightIDs:$ids, startTime:$st, endTime:$en, dataType: Casts,
               limit: 10000){ data nextPageTimestamp }}}}"""
    appliers = defaultdict(set)             # canonical ability name → {sourceID}
    cur = st
    try:
        for _pg in range(MAX_EVENT_PAGES):
            ev = _report(gql(token, QC, {"c": report_code, "ids": fids, "st": cur, "en": en}),
                         "events", default={})
            for d in ev.get("data", []):
                if d.get("type") != "cast":
                    continue
                canon = gid2canon.get(d.get("abilityGameID"))
                if canon and d.get("sourceID") in id2name:
                    appliers[canon].add(d["sourceID"])
            nx = ev.get("nextPageTimestamp")
            if not nx:
                break
            cur = nx
    except Exception as ex:
        print(f"  Warning: maintain-uptime casts prepass failed: {ex}")
        return {}
    if not appliers:
        print("  ✓ maintain uptime: no tracked maintenance abilities cast")
        return {}
    # ── uptime: aliased Debuffs/Buffs-table queries over the real (provider, ability) pairs only ──
    fids_lit = ",".join(str(int(x)) for x in fids)
    alias2 = {}                             # alias → (name, canonical ability, kind)
    clauses = []
    for canon in sorted(appliers):
        kind = MAINTAIN_UPTIME_ABILITIES[canon]
        for sid in sorted(appliers[canon]):
            for aid in sorted(name_ids[canon]):
                al = f"u{len(alias2)}"
                alias2[al] = (id2name.get(sid), canon, kind)
                if kind == "debuff":
                    # NB: sourceID + hostilityType:Enemies returns 0 on the Debuffs table (live-verified —
                    # the two filters don't compose); sourceID alone already restricts to the source's
                    # ENEMY targets (these tracked debuffs are enemy-only), so drop hostilityType. The
                    # returned auras are per enemy target — merge their bands (union ≈ 'up on the boss').
                    clauses.append(f'{al}: table(dataType: Debuffs, '
                                   f'fightIDs:[{fids_lit}], sourceID:{int(sid)}, abilityID:{float(aid)})')
                else:
                    clauses.append(f'{al}: table(dataType: Buffs, fightIDs:[{fids_lit}], '
                                   f'sourceID:{int(sid)}, abilityID:{float(aid)})')
    covered = defaultdict(lambda: defaultdict(float))   # name → ability → covered ms
    keys = list(alias2)
    BATCH = 18                               # cap aliases/query for the WCL complexity budget
    for b in range(0, len(keys), BATCH):
        sub = keys[b:b + BATCH]
        Q = "query($c:String!){reportData{report(code:$c){" + \
            " ".join(clauses[b:b + BATCH]) + "}}}"
        try:
            rep = _report(gql(token, Q, {"c": report_code}), default={})
        except Exception as ex:
            print(f"  Warning: maintain-uptime uptime batch failed: {ex}")
            continue
        for al in sub:
            nm, canon, kind = alias2[al]
            if not nm:
                continue
            t = _loads_alias(rep.get(al))
            auras = (t or {}).get("data", {}).get("auras", []) if t else []
            if kind == "debuff":
                bands = []
                for a in auras:
                    bands.extend(a.get("bands") or [])
                covered[nm][canon] = max(covered[nm][canon], _merge_bands(bands))
            else:
                covered[nm][canon] = max(covered[nm][canon],
                                         sum(a.get("totalUptime", 0) for a in auras))
    res = {}
    for nm in sorted(covered):
        res[nm] = {ab: round(min(100.0, covered[nm][ab] / kdur * 100), 1)
                   for ab in sorted(covered[nm]) if covered[nm][ab] > 0}
        if not res[nm]:
            del res[nm]
    print(f"  ✓ maintain uptime: {len(res)} player(s), "
          f"{len(MAINTAIN_UPTIME_ABILITIES)} abilities tracked")
    return res


def _stack_ramp(evs, f_start, f_end, declared_max):
    """Reconstruct one (debuff, enemy-target) stack timeline → (peak, ramp_ts, uptime_at_threshold_ms).
    `evs` is [(ts, type, stack)] for a single debuff GUID on a single target within one fight. The
    THRESHOLD that counts as "established" is declared_max only when the debuff actually stacked
    (peak ≥ 2) — otherwise 1, so a single-application debuff (or a stacker that flatlined at 1) is
    measured as time-to-first-up. applydebuffstack carries the NEW stack count (live-verified)."""
    evs = sorted(evs, key=lambda e: (e[0] if e[0] is not None else 0))   # stable: WCL is chronological
    stack = peak = 0
    for ts, typ, stk in evs:
        if typ == "applydebuff":        stack = max(stack, 1)
        elif typ == "applydebuffstack": stack = stk or (stack + 1)
        elif typ == "removedebuff":     stack = 0
        peak = max(peak, stack)
    threshold = declared_max if (declared_max > 1 and peak >= 2) else 1
    stack, enter, ramp_ts, up_ms = 0, None, None, 0
    for ts, typ, stk in evs:
        if typ == "applydebuff":        stack = max(stack, 1)
        elif typ == "applydebuffstack": stack = stk or (stack + 1)
        elif typ == "removedebuff":     stack = 0
        # refreshdebuff leaves the stack unchanged
        if stack >= threshold and enter is None:
            enter = ts
            if ramp_ts is None:
                ramp_ts = ts
        elif stack < threshold and enter is not None:
            up_ms += (ts - enter); enter = None
    if enter is not None:                # still up at fight end
        up_ms += (f_end - enter)
    return peak, ramp_ts, up_ms


def fetch_debuff_ramp_speed(token: str, report_code: str, kills: list, md: dict | None = None) -> dict:
    """Generalized debuff RAMP SPEED — how fast the raid ESTABLISHED each tracked debuff after the
    pull, per boss. For STACKING debuffs (Shadow Weaving / Sunder, DEBUFF_STACK_MAX) ramp = time to
    MAX stack; single-application debuffs degrade to time-to-first-applied (up at the pull, or late?).
    Plus the % of the fight the debuff was held AT full effect. Pure WCL Debuffs EVENTS (per-target
    stack timeline), runs every week with no combat log — the durable twin of debuff *coverage*.

    Per slot the representative target is the enemy that reached the highest stack (the boss, where the
    raid stacks the debuff — trash adds rarely build it). Returns
      {ramps:[{key,label,cat,icon,soft,stacking,max_stack,
               bosses:[{boss,encounter_id,ramp_sec,uptime_pct,reached,peak}],
               med_ramp_sec, avg_uptime_pct, reached_count, total_bosses}]}  (stacking + high-signal
    first) — {} when there are no kills. NB the TBC debuff-cap caveat: a debuff bumped past the cap can
    read low uptime despite being cast; a ramp time beside near-zero uptime is a probable cap victim."""
    if not kills:
        return {}
    if md is None:
        raise ValueError("md is required — pass the shared fetch_master_data() result")
    fids = [f["id"] for f in kills]
    st   = min(f["startTime"] for f in kills)
    en   = max(f["endTime"]   for f in kills)
    fid_boss = {f["id"]: f["name"] for f in kills}
    fid_enc  = {f["id"]: f.get("encounterID") for f in kills}
    fid_s    = {f["id"]: f["startTime"] for f in kills}
    fid_e    = {f["id"]: f["endTime"]   for f in kills}
    md_gids  = {a.get("gameID") for a in (md.get("abilities") or [])}
    guid_slot = {g: s["key"] for s in DEBUFF_SLOTS for g in s["guids"]}
    present   = [g for g in guid_slot if g in md_gids]    # prune to debuffs that actually appeared

    raw = defaultdict(list)               # (slot_key, fid, guid, targetID) → [(ts, type, stack)]
    QD = """query($c:String!,$ids:[Int]!,$st:Float!,$en:Float!,$a:Float!){reportData{report(code:$c){
        events(fightIDs:$ids, startTime:$st, endTime:$en, dataType: Debuffs, hostilityType: Enemies,
               abilityID:$a, limit: 10000){ data nextPageTimestamp }}}}"""
    try:
        for g in sorted(present):
            skey = guid_slot[g]
            cur = st
            for _pg in range(MAX_EVENT_PAGES):
                ev = _report(gql(token, QD, {"c": report_code, "ids": fids, "st": cur, "en": en,
                                             "a": float(g)}), "events", default={})
                for d in ev.get("data", []):
                    fid, tid = d.get("fight"), d.get("targetID")
                    if fid in fid_s and tid is not None:
                        raw[(skey, fid, g, tid)].append((d.get("timestamp"), d.get("type"), d.get("stack")))
                nx = ev.get("nextPageTimestamp")
                if not nx:
                    break
                cur = nx
    except Exception as ex:
        print(f"  Warning: debuff ramp-speed events failed: {ex}")
        return {}

    ramps_out = []
    for s in DEBUFF_SLOTS:                # DEBUFF_SLOTS order; re-sorted high-signal-first below
        skey = s["key"]
        declared = DEBUFF_STACK_MAX.get(skey, 1)
        bosses = []
        for f in kills:
            fid = f["id"]
            f_start, f_end = fid_s[fid], fid_e[fid]
            dur_ms = (f_end - f_start) or 1
            cands = [_stack_ramp(evs, f_start, f_end, declared)
                     for (sk, ff, g, tid), evs in raw.items() if sk == skey and ff == fid]
            cands = [c for c in cands if c[0] > 0]      # peak > 0 — debuff actually present on this boss
            if not cands:
                continue
            peak, ramp_ts, up_ms = max(cands, key=lambda c: (c[0], -(c[1] or 1e18), c[2]))   # boss = highest peak, earliest
            threshold = declared if (declared > 1 and peak >= 2) else 1
            reached = ramp_ts is not None and peak >= threshold
            bosses.append({"boss": fid_boss[fid], "encounter_id": fid_enc[fid],
                           "ramp_sec": round((ramp_ts - f_start) / 1000, 1) if reached else None,
                           "uptime_pct": round(up_ms / dur_ms * 100, 1),
                           "reached": reached, "peak": peak})
        if not bosses:
            continue
        got = [b["ramp_sec"] for b in bosses if b["reached"] and b["ramp_sec"] is not None]
        # present stacking by OBSERVED behavior: a "stacking" slot actually held by single-application
        # Expose (peak 1 everywhere) reads as single — "time-to-max" would be a lie there.
        observed_peak = max((b["peak"] for b in bosses), default=0)
        really_stacking = declared > 1 and observed_peak >= 2
        ramps_out.append({
            "key": skey, "label": s["label"], "cat": s["cat"], "icon": s["icon"],
            "soft": s.get("soft", False), "stacking": really_stacking,
            "max_stack": declared if really_stacking else 1,
            "bosses": bosses,
            # MEDIAN, not mean — a single late re-application on a long multi-phase fight (Kael/Vashj
            # execute) skews the mean badly (e.g. Curse of Recklessness reads ~30s when it was pulled
            # at +1s on every normal fight). The median reflects the typical-fight ramp.
            "med_ramp_sec": round(_median(got), 1) if got else None,
            "avg_uptime_pct": round(sum(b["uptime_pct"] for b in bosses) / len(bosses), 1),
            "reached_count": sum(1 for b in bosses if b["reached"]),
            "total_bosses": len(bosses),
        })
    # stacking + non-soft first (the real ramp stories); proc-based/soft debuffs last; slot order within
    ramps_out.sort(key=lambda r: (0 if r["stacking"] else 1, 1 if r["soft"] else 0))
    print(f"  ✓ debuff ramp speed: {len(ramps_out)} debuff(s) with ramp data")
    return {"ramps": ramps_out}


def fetch_mechanic_compliance(token: str, report_code: str, kills: list, md: dict | None = None) -> dict:
    """Per-boss "who ate the mechanic" — the WCL-durable headline for avoidable damage
    (backlog #2 + #7). For each kill fight whose encounter has entries in
    game_constants.MECHANIC_IDS, pull DamageTaken EVENTS filtered server-side to those
    ability IDs (filterExpression keeps it to ~1 page/fight) and aggregate per mechanic →
    per player: hits + damage. Player targets only (pets dropped via md); multiple IDs
    mapping to one mechanic name merge into one row. Runs every week with zero combat log.

    Returns {boss: {"encounter_id": id, "mechanics": {mech: {"total": dmg, "events": n,
    "players": {name: {"hits": h, "dmg": d}}}}}} — bosses/mechanics with no hits omitted."""
    if md is None:
        # one masterData fetch per run (b9be8d9) — a silent re-fetch here is how that rule
        # erodes, so fail loud instead. Pass build_week_data's shared `md`.
        raise ValueError("md is required — pass the shared fetch_master_data() result")
    player_ids = {a["id"] for a in md["players"]}
    id2name = md["id2name"]
    Q = """query($c:String!,$f:Int!,$s:Float!,$e:Float!,$x:String!){reportData{report(code:$c){
        events(dataType: DamageTaken, fightIDs:[$f], startTime:$s, endTime:$e,
               filterExpression:$x, limit:10000){ data nextPageTimestamp }}}}"""
    out = {}
    for f in kills:
        ids = MECHANIC_IDS.get(f.get("name") or "", {})
        if not ids:
            continue
        expr = "ability.id in (" + ", ".join(str(i) for i in sorted(ids)) + ")"
        boss = out.setdefault(f["name"], {"encounter_id": f.get("encounterID"), "mechanics": {}})
        st, pages = float(f["startTime"]), 0
        while st is not None and pages < MAX_EVENT_PAGES:
            pages += 1
            try:
                ev = _report(gql(token, Q, {"c": report_code, "f": int(f["id"]), "s": st,
                                            "e": float(f["endTime"]), "x": expr}), "events", default={})
            except Exception as e:
                print(f"  Warning: mechanic compliance fetch failed ({f.get('name')}): {e}")
                break
            for d in (ev.get("data") or []):
                tid = d.get("targetID")
                if tid not in player_ids:
                    continue
                mech = ids.get(d.get("abilityGameID"))
                if not mech:
                    continue
                m = boss["mechanics"].setdefault(mech, {"total": 0, "events": 0, "players": {}})
                amt = d.get("amount", 0) or 0
                m["total"] += amt
                m["events"] += 1
                p = m["players"].setdefault(id2name.get(tid, str(tid)), {"hits": 0, "dmg": 0})
                p["hits"] += 1
                p["dmg"] += amt
            nx = ev.get("nextPageTimestamp")
            st = float(nx) if nx else None
    out = {b: v for b, v in out.items() if v["mechanics"]}
    n_mech = sum(len(v["mechanics"]) for v in out.values())
    print(f"  ✓ mechanic compliance: {len(out)} bosses, {n_mech} mechanics tracked"
          if out else "  ✓ mechanic compliance: no mapped mechanics hit anyone (clean week)")
    return out
