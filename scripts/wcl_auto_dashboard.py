#!/usr/bin/env python3
"""
wcl_auto_dashboard.py — WarcraftLogs API → Raid KPI Dashboard auto-updater
TBC Anniversary edition

USAGE:
    python wcl_auto_dashboard.py <REPORT_CODE>
    python wcl_auto_dashboard.py <REPORT_CODE> --log WoWCombatLog.txt
    python wcl_auto_dashboard.py <REPORT_CODE> --out "C:/path/to/raid_kpi_dashboard.html"
    python wcl_auto_dashboard.py <REPORT_CODE> --dry-run   # print JSON only

    WCL report code = the code in the URL: fresh.warcraftlogs.com/reports/<CODE>
    --log  = optional path to WoWCombatLog.txt; fills in actual crit %, avoidable damage,
             interrupts, engineering, and drums that WCL API doesn't expose directly.

SETUP (one time):
    Set two environment variables from https://www.warcraftlogs.com/api/clients/
        WCL_CLIENT_ID=your_client_id
        WCL_CLIENT_SECRET=your_client_secret
    Or pass them as --client-id / --client-secret flags.

WHAT IT DOES:
    1. Pulls fight list + player gear from WCL v2 GraphQL
    2. Looks up crit rating on each item/gem/enchant (cached locally)
    3. Calculates EXPECTED crit % per player (TBC formula: rating / 22.08 + class base)
    4. Pulls hit/crit counts from damage table → ACTUAL crit %
    5. Luck KPI = actual − expected  (green = ran hot, orange = ran cold)
    6. Pulls deaths, damage totals, avoidable dmg, consumable uptimes
    7. Injects updated WEEK_DATA into the HTML dashboard
"""

import os, sys, json, re, time, argparse, base64, urllib.request, urllib.parse
from pathlib import Path
from typing import Optional
from collections import defaultdict

# Force UTF-8 console output — Windows defaults to cp1252, which can't encode the
# ✓/✅/▲ glyphs this script prints and crashes with UnicodeEncodeError.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

# Auto-install requests if missing
try:
    import requests as _req
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests", "--quiet"])
    import requests as _req

# Auto-load .env from Gaming root (parent of scripts/)
_env_file = Path(__file__).parent.parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ[_k.strip()] = _v.strip()  # .env always wins over system env vars

# ── WCL endpoints + transport ─────────────────────────────────────────────────
# OAuth + the GraphQL wrapper live in wcl_client (a pure transport leaf). Re-exported here
# so every existing W.gql / W.get_token / W.WCL_API_URL reference keeps resolving.
from wcl_client import WCL_TOKEN_URL, WCL_API_URL, get_token, gql

# ── TBC crit rating conversion ────────────────────────────────────────────────
# At level 70: every 22.08 crit rating = 1% crit (melee & spell, same value)
CRIT_RATING_PER_PCT = 22.08

# Crit "luck" is measured vs the player's OWN multi-week baseline (gold standard — controls
# for gear/spec/talents/buffs, leaving only RNG). Below this many prior weeks, show a
# "building baseline" state rather than a misleading number.
LUCK_MIN_WEEKS = 3

# ── Base crit % by class (before gear, after typical raid-spec talents) ───────
# These are approximate starting points; actual values vary with talent build.
CLASS_BASE_CRIT = {
    "DeathKnight": 0.0,   # not in TBC
    "Druid":       1.85,  # Cat/Bear; add ~4-5% from talents on top
    "Hunter":      3.65,
    "Mage":        3.29,
    "Paladin":     3.29,
    "Priest":      3.29,
    "Rogue":       5.0,
    "Shaman":      3.29,
    "Warlock":     3.29,
    "Warrior":     3.0,
}

# Extra crit % from common raid-spec talent builds (approximate)
SPEC_TALENT_CRIT = {
    "Feral Combat":    9.0,   # ~5 pts Sharpened Claws + Predatory Instincts
    "Balance":         5.0,
    "Marksmanship":    5.0,   # Killer Instinct 3/5 + Mortal Shots
    "Survival":        4.0,
    "Fire":            6.0,   # Critical Mass 5/5 (spells only, but we track spell crit)
    "Arcane":          3.0,
    "Frost":           3.0,
    "Holy":            3.0,   # Paladin
    "Retribution":     3.0,
    "Protection":      1.0,
    "Shadow":          3.0,
    "Discipline":      3.0,
    "Enhancement":     5.0,   # Elemental Devastation proc, Weapon Mastery
    "Elemental":       5.0,   # Call of Thunder 5/5
    "Restoration":     2.0,
    "Demonology":      3.0,
    "Affliction":      3.0,
    "Destruction":     5.0,   # Devastation 5/5
    "Arms":            5.0,   # Impale + Improved Hamstring
    "Fury":            3.0,
}

# ── Common TBC gem → crit rating lookup ───────────────────────────────────────
# Source: wowhead TBC items.  Only crit-granting gems listed.
GEM_CRIT_RATING: dict[int, int] = {
    # Pure crit gems
    23121: 8,   # Jagged Talasite (+8 Crit)
    24028: 8,   # Smooth Dawnstone (+8 Crit)
    32196: 8,   # Smooth Lionseye (+8 Crit) — Phase 5
    32767: 8,   # Smooth Pyrestone (+8 Crit) — Phase 5
    # Hybrid gems with crit
    23101: 4,   # Glinting Noble Topaz (+4 Hit, +4 Crit)
    24053: 4,   # Glinting Flame Spessarite (+4 Hit, +4 Crit) — earlier
    24065: 4,   # Balanced Nightseye (+4 Spell Dmg, +4 Crit)
    24067: 4,   # Luminous Noble Topaz (+4 Spell Dmg, +4 Crit)
    24071: 4,   # Radiant Deep Peridot (+4 Spell Hit, +4 Spell Crit)
    24051: 4,   # Potent Noble Topaz (+5 Spell Dmg, +3 Crit)
    32218: 4,   # Glinting Pyrestone (+4 Hit, +4 Crit) — Phase 5
    32215: 5,   # Potent Pyrestone (+6 Spell Dmg, +5 Crit)
    # Meta gems with crit proc (not rating, excluded)
}

# ── Common TBC enchant → crit rating lookup ───────────────────────────────────
# permanentEnchant IDs (WCL uses enchant spell IDs from the game)
ENCHANT_CRIT_RATING: dict[int, int] = {
    2981: 12,   # Enchant Weapon - Mongoose (proc, not fixed — 0 for math)
    2986: 24,   # Enchant Weapon - Greater Agility (ranged/melee; ~1% crit for hunters)
    3222: 20,   # Enchant Gloves - Major Strength
    3231: 15,   # Enchant Head - Glyph of Ferocity (35 AP, 10 Hit — 0 crit)
    # Cloak enchants
    2938: 0,    # Enchant Cloak - Subtlety (2% threat — 0 crit)
    1594: 12,   # Enchant Cloak - Stealth — not crit
    # Chest
    3832: 0,    # Enchant Chest - Major Resilience
    # Bracers
    2650: 0,    # Enchant Bracers - Assault (24 AP)
    # Shoulders
    35443: 10,  # Greater Inscription of Vengeance (30 AP, 14 Crit) — 14 rating
    35439: 8,   # Greater Inscription of the Oracle (22 Heal, 6 Mana/5) — 0
    35437: 15,  # Greater Inscription of Warding (20 dodge) — 0
    # Actually, shoulder inscription crit ratings:
    35440: 14,  # Greater Inscription of Vengeance: 14 crit rating
    35441: 10,  # Greater Inscription of the Blade: 10 crit
}
# Note: Enchant lookup is best-effort; uncached IDs treated as 0 crit.

# ── Curated game-data constants + combat-log parser (extracted modules) ──────────
# game_constants: all curated WoW name-sets (consumables, CC/MC/AoE, friendly-fire,
#   engineering, drums) + the T5 content seam. combat_log: the raw WoWCombatLog parser.
# Both are re-exported so every existing W.<name> reference keeps resolving.
from game_constants import *   # noqa: F401,F403 — curated name-sets, re-exported
from combat_log import parse_combat_log, _parse_ts, _consumable_category   # noqa: F401









# ── Local item crit cache (persisted across runs) ─────────────────────────────
ROOT_DIR   = Path(__file__).parent.parent  # Gaming/
CACHE_FILE = ROOT_DIR / "cache" / "item_crit_cache.json"
LOGS_DIR   = ROOT_DIR / "logs"
LOOT_DIR   = ROOT_DIR / "loot"   # ThatsBIS "received" CSV exports — newest *.csv auto-picked
DASH_FILE      = ROOT_DIR / "dashboard" / "raid_kpi_dashboard.html"
TEMPLATE_FILE  = ROOT_DIR / "dashboard" / "template.html"
DEFAULT_TITLE  = "Raid KPI Dashboard — TBC Anniversary"

# Per-week mapped WEEK_DATA snapshots — the source of record for OFFLINE reprocessing
# (reprocess.py --all) so a schema/trend/render change never needs a WCL re-run. One JSON
# per report, written every prod run after map_to_week_data() (gitignored, ~MB each).
WEEK_DATA_CACHE = ROOT_DIR / "cache" / "week_data"

# Performance metric = the native WCL PARSE % (rankPercent from report.rankings) — vs the FULL
# logged population, NOT the old top-100 cohort ratio (which made solid raiders read "below
# replacement"). See fetch_parse_percentiles. No cohort cache / baseline / TTL needed — WCL scores
# it server-side; we just read it. (The old healer_baseline.json + dps/tank_baseline.json caches are
# now orphaned and can be deleted from cache/.)

def load_cache() -> dict:
    if CACHE_FILE.exists():
        return json.loads(CACHE_FILE.read_text())
    return {}

def save_cache(cache: dict):
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, indent=2))


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

Q_ITEM_STATS = """
query GetItem($id: Int!) {
  gameData {
    item(id: $id) {
      id
      name
      stats { subType amount }
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
# Item crit rating lookup
# ══════════════════════════════════════════════════════════════════════════════

# WoW stat subType ID for Critical Strike Rating in TBC
STAT_CRIT_RATING = 96  # melee crit rating
STAT_SPELL_CRIT  = 24  # spell crit rating (some items split these)

def fetch_item_crit(token: str, item_id: int, cache: dict) -> int:
    """Return the crit rating on item_id (sum of all crit stat lines). Cached."""
    key = str(item_id)
    if key in cache:
        return cache[key]
    if item_id == 0:
        return 0
    try:
        data = gql(token, Q_ITEM_STATS, {"id": item_id})
        item = data.get("gameData", {}).get("item") or {}
        stats = item.get("stats") or []
        crit = sum(
            s["amount"] for s in stats
            if s.get("subType") in (STAT_CRIT_RATING, STAT_SPELL_CRIT)
        )
        cache[key] = crit
        time.sleep(0.1)  # be polite to the API
        return crit
    except Exception as e:
        print(f"  Warning: could not fetch item {item_id}: {e}")
        cache[key] = 0
        return 0


def gear_crit_rating(token: str, gear: list, cache: dict, role: str = "") -> int:
    """Return crit rating for a player. Uses WCL combatantinfo stats directly when available."""
    if gear and isinstance(gear[0], dict) and "_crit_spell" in gear[0]:
        d = gear[0]
        # Use role-appropriate crit stat
        if role in ("Caster", "Healer"):
            return d.get("_crit_spell", 0)
        elif role == "Physical":
            return d.get("_crit_melee", 0)
        else:
            return max(d.get("_crit_melee", 0), d.get("_crit_spell", 0))
    if gear and isinstance(gear[0], dict) and "_crit_rating" in gear[0]:
        return gear[0]["_crit_rating"]
    total = 0
    for slot in gear:
        item_id = slot.get("id", 0)
        if item_id and item_id > 0:
            total += fetch_item_crit(token, item_id, cache)

        # Gems
        for gem in (slot.get("gems") or []):
            gid = gem.get("id", 0)
            total += GEM_CRIT_RATING.get(gid, 0)

        # Permanent enchant
        eid = slot.get("permanentEnchant", 0)
        total += ENCHANT_CRIT_RATING.get(eid, 0)

    return total


# ══════════════════════════════════════════════════════════════════════════════
# Crit calculation
# ══════════════════════════════════════════════════════════════════════════════

# Primary-stat → crit conversion at level 70. The COMBATANT_INFO critMelee/critSpell fields
# are only the secondary crit RATING; most crit on a geared char comes from PRIMARY stats
# (agility for melee/ranged, intellect for spells). Omitting this made expected crit read far
# too low and every physical/caster looked "lucky" — this closes that gap.
AGI_PER_CRIT = {   # agility for +1% melee/ranged crit
    "Rogue": 29.0, "Druid": 25.0, "Hunter": 40.0, "Warrior": 33.3,
    "Shaman": 29.5, "Paladin": 25.0,
}
INT_PER_CRIT = {   # intellect for +1% spell crit
    "Mage": 59.5, "Warlock": 60.6, "Priest": 59.2, "Druid": 60.0,
    "Shaman": 59.2, "Paladin": 53.8,
}


def expected_crit(player_type: str, spec: str, crit_rating: int,
                  agility: int = 0, intellect: int = 0) -> float:
    """Expected crit % = class base + talent estimate + gear crit RATING + PRIMARY-stat crit.
    Physical specs add agility-derived crit; casters/healers add intellect-derived crit."""
    base   = CLASS_BASE_CRIT.get(player_type, 3.0)
    talent = SPEC_TALENT_CRIT.get(spec, 3.0)
    gear   = crit_rating / CRIT_RATING_PER_PCT
    stat   = 0.0
    if spec in CASTER_SPECS or spec in HEALER_SPECS:
        stat = intellect / INT_PER_CRIT.get(player_type, 60.0) if intellect else 0.0
    else:   # physical / tank
        stat = agility / AGI_PER_CRIT.get(player_type, 30.0) if agility else 0.0
    return round(base + talent + gear + stat, 2)


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
                           actors: list = None) -> dict[str, list]:
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

    print(f"  Fetching damage events for crit counting (this may take a moment)...")
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


# Spec → role classification, shared by build_week_data's reclassifier and per-fight roles.
TANK_SPECS     = {"Protection", "Feral Combat"}    # Feral only counts as tank if WCL agrees
HEALER_SPECS   = {"Holy", "Discipline", "Restoration"}
CASTER_SPECS   = {"Arcane", "Fire", "Frost", "Shadow", "Elemental",
                  "Demonology", "Affliction", "Destruction", "Balance"}
PHYSICAL_SPECS = {"Fury", "Arms", "Retribution", "Enhancement",
                  "Survival", "Marksmanship", "Beast Mastery",
                  "Combat", "Assassination", "Subtlety"}
def _nontank_role(spec: str) -> str:
    """Role to use for compliance when a Tank spec tanks < 50 % of fights (runs DPS consumes)."""
    if spec in HEALER_SPECS:  return "Healer"
    if spec in CASTER_SPECS:  return "Caster"
    if spec in PHYSICAL_SPECS: return "Physical"
    return "Physical"   # Feral Combat, Protection → physical melee when not tanking


def _nonheal_role(spec: str) -> str:
    """Role to use when a Healer spec heals < 50 % of fights (ran DPS most of the night).
    Resto shaman/druid → Caster by default; an Enhancement/Survival-style off-spec maps by
    its physical spec. Mirrors _nontank_role. Handles the resto-shaman-who-DPS'd case."""
    if spec in PHYSICAL_SPECS: return "Physical"
    if spec in CASTER_SPECS:   return "Caster"
    if spec in TANK_SPECS:     return "Physical"
    return "Caster"   # Restoration (shaman/druid), Holy/Disc → caster DPS when not healing


def _effective_role(role: str, spec: str, fights_tanked: int, fights_healed: int,
                    fights_total: int) -> str:
    """A player's role for the night by what they ACTUALLY did, not their roster slot.
    A Tank who tanked < 50% of fights, or a Healer who healed < 50%, ran DPS most of the
    night and is reclassified to their off-role. Everyone else keeps their roster role.
    Single source of truth for hybrid/spec-swap placement across all KPIs."""
    if fights_total > 0:
        if role == "Tank"   and fights_tanked / fights_total < 0.5: return _nontank_role(spec)
        if role == "Healer" and fights_healed / fights_total < 0.5: return _nonheal_role(spec)
    return role


def _fight_role(spec: str, bucket: str) -> str:
    """Per-fight role from the player's spec THAT fight — reliable for prot/ret and
    heal/dps swaps. WCL's tanks/healers/dps bucket is only a tiebreaker (it mis-buckets
    Prot warriors into dps), so we classify by spec and fall back to the bucket."""
    if spec in HEALER_SPECS:
        return "Healer"
    if spec == "Protection":
        return "Tank"
    if spec == "Feral Combat":
        return "Tank" if bucket == "tanks" else "dps"
    if not spec:
        return {"tanks": "Tank", "healers": "Healer", "dps": "dps"}[bucket]
    return "dps"   # all caster/physical DPS specs


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
                            tank_names: set = None) -> dict:
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


# Tank defensive cooldowns — WCL spell IDs → display name. Counted from Casts events
# scoped to kill-fight windows. Frenzied Regen (26999) has no aura, so Casts is the only
# source; Barkskin/Shield Wall/Last Stand/Lay on Hands likewise tracked by cast.
TANK_CD_IDS = [871, 12975, 26999, 22812, 27154, 1020, 498, 5573]
CD_NAMES = {871: "Shield Wall", 12975: "Last Stand", 26999: "Frenzied Regeneration",
            22812: "Barkskin", 27154: "Lay on Hands", 1020: "Divine Shield",
            498: "Divine Protection", 5573: "Divine Protection"}   # names match the combat log overlay

# WCL melee hitType enum (LOCKED against live data — confirmed by probing tank logs directly).
# Confirmed by probing this report's tanks: a crit-immune bear shows only {miss, hit,
# dodge, crushing}; paladins add {blocked, parry, crit}.
#   1 = normal hit   2 = crit            4 = blocked (partial, reduced)
#   15 = crushing    0/7/8 = miss/dodge/parry (zero damage)
HITTYPE_CRUSH = 15
HITTYPE_CRIT  = 2


def build_tank_scorecard_extended(token: str, report_code: str, kills: list,
                                  fight_roles: dict, fight_durs: dict,
                                  heal_by_fight: dict, actors: list, md: dict = None):
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
        md = fetch_master_data(token, report_code)
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
            rep = gql(token, Q, {"c": report_code})["reportData"]["report"]
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
    QE = """query($c:String!,$ids:[Int]!,$st:Float!,$en:Float!){reportData{report(code:$c){
        events(fightIDs:$ids, startTime:$st, endTime:$en, dataType: DamageTaken,
               hostilityType: Friendlies, limit: 10000){ data nextPageTimestamp }}}}"""
    st = win_s
    while True:
        try:
            ev = gql(token, QE, {"c": report_code, "ids": fids, "st": st, "en": win_e}
                     )["reportData"]["report"]["events"]
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
            ev = gql(token, QC, {"c": report_code, "ids": fids, "st": st, "en": win_e}
                     )["reportData"]["report"]["events"]
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
            rep = gql(token, Q, {"c": report_code})["reportData"]["report"]
        except Exception as ex:
            print(f"  Warning: raid-DPS batch failed: {ex}")
            continue
        for f in chunk:
            t = _loads_alias(rep.get(f'f{f["id"]}'))
            dur = fight_durs.get(f["id"], 0) or 1
            tot = sum(e.get("total", 0) for e in (t or {}).get("data", {}).get("entries", []))
            if tot:
                boss_raid_dps[fid_boss[f["id"]]] = round(tot / dur)

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
            "per_boss":      a["per_boss"],   # pull order; HTML sorts to encounter order
        }
    return out, boss_raid_dps


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


# ── CLASS TOOLKIT — each DPS's signature class-relative utility ───────────────
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

# Hard cap on paginated WCL event queries — guards against a runaway non-null nextPageTimestamp
# (a known WCL quirk the older loops defend against). A full clear is ~10-20 pages; 40 is slack.
MAX_EVENT_PAGES = 40

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
        if g("wf_totem", 0) > 0:                              # enhance — Windfury + twisting
            goa, sw = g("goa_totem", 0), g("wf_swaps", 0)
            twisting = sw >= 20 and goa >= 10                 # alternating both air totems
            cell = {"label": "Windfury", "value": str(g("wf_totem", 0)), "num": g("wf_totem", 0),
                    "title": (f"Windfury Totem drops" + (f" · {goa} Grace of Air" if goa else "")
                              + (f" · {sw} WF↔GoA swaps (twisting)" if twisting else "") + bl_d),
                    "icon_ability": "Windfury Totem", "fallback": "spell_nature_windfury"}
            if twisting:
                cell["tag"] = "🌀 twist"
            return cell
        up = g("tow_up")
        if up is not None:                                   # elemental — modeled ToW uptime
            return {"label": "ToW uptime", "value": f"~{up:g}%", "num": up,
                    "title": (f"Totem of Wrath uptime — modeled from recast cadence "
                              f"(totem buffs aren't logged as auras in 2.5)" + bl_d),
                    "icon_ability": "Totem of Wrath", "fallback": "spell_fire_totemofwrath"}
        air = g("woa_totem", 0) + g("tow_totem", 0)
        if air > 0:                                           # ele w/o ToW casts — air totems
            return {"label": "Air totems", "value": str(air), "num": air,
                    "title": f"Wrath of Air + Totem of Wrath drops" + bl_d,
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
                    "title": f"Mangle casts" + (f" · {rb} Battle Rez" if rb else ""),
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
        rep = gql(token, Q, {"c": report_code})["reportData"]["report"]
        for al, nm in alias2name.items():
            t = _loads_alias(rep.get(al))
            for a in (t or {}).get("data", {}).get("auras", []):
                up[nm] += a.get("totalUptime", 0)
        for nm in {n for n in alias2name.values()}:
            counts[nm][out_key] = round(up[nm] / kdur * 100, 1)
    except Exception as ex:
        print(f"  Warning: {label}-uptime batch fetch failed: {ex}")


def build_class_toolkit(token, report_code, kills, md: dict = None):
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
        md = fetch_master_data(token, report_code)
    # All fights (incl. trash) for the whole-report Arcane Explosion pass below — kills-only
    # would undercount the mage AE meme. masterData (actors/abilities) comes from the shared md.
    try:
        rep = gql(token, """query($c:String!){reportData{report(code:$c){
            fights{id startTime endTime}}}}""",
                 {"c": report_code})["reportData"]["report"]
    except Exception as ex:
        print(f"  Warning: class-toolkit fights fetch failed: {ex}")
        return {}
    id2name = md["id2name"]
    name2id = md["name2id"]
    # abilityGameID → canonical toolkit key (via the name map; unions all ranks)
    gid2key = {}
    ae_ids  = []                                     # Arcane Explosion ranks (whole-report meme)
    snd_ids = []                                     # Slice and Dice (rogue uptime)
    bs_ids  = []                                     # Battle Shout (warrior uptime, self-target)
    for a in (md.get("abilities") or []):
        nm_a = (a.get("name") or "")
        key = TOOLKIT_ABILITIES.get(nm_a.lower())
        if key:
            gid2key[a.get("gameID")] = key
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
    # Totem cast timestamps for shaman analytics: enhance WF↔GoA twisting + ele ToW uptime.
    TOTEM_TS = {"wf_totem", "goa_totem", "tow_totem"}
    tstamps  = defaultdict(lambda: defaultdict(list)) # [name][key] = [cast timestamps]
    cur = st
    try:
        for _pg in range(MAX_EVENT_PAGES):
            ev = gql(token, QC, {"c": report_code, "ids": fids, "st": cur, "en": en}
                     )["reportData"]["report"]["events"]
            for d in ev.get("data", []):
                if d.get("type") != "cast":
                    continue
                key = gid2key.get(d.get("abilityGameID"))
                if key:
                    nm = id2name.get(d.get("sourceID"))
                    if nm:
                        counts[nm][key] += 1
                        if key in TOTEM_TS and d.get("timestamp") is not None:
                            tstamps[nm][key].append(d["timestamp"])
            nx = ev.get("nextPageTimestamp")
            if not nx:
                break
            cur = nx
    except Exception as ex:
        print(f"  Warning: class-toolkit events failed: {ex}")

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
                    ev = gql(token, QA, {"c": report_code, "ids": allids, "st": cur,
                                         "en": a_en, "a": float(aid)})["reportData"]["report"]["events"]
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
    return {nm: dict(d) for nm, d in counts.items()}


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
            raw = gql(token, Q, {"c": report_code})["reportData"]["report"]["rankings"]
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


def fetch_saves(token: str, report_code: str, kills: list, md: dict = None) -> dict:
    """Per-player protective/external casts ON ALLIES — the 'saving others' kit (paladin Hand of
    Protection / Sacrifice / Freedom / Salvation, Lay on Hands on others, Cleanse + dispels, druid
    Rebirth, warlock Soulstone res, priest Pain Suppression…). Pure WCL Casts events filtered to
    EXTERNAL_ABILITIES (name-keyed; Anniversary-verified) with a friendly-PLAYER target that isn't
    the caster — so self-casts, untargeted totems, and Environment-target trinket procs all drop.
    Returns {name: {save, dispel, utility, total, targets:{ability:{target:count}}}}.
    WCL-durable; no combat log needed (a future pass can flag CLUTCH saves by cross-referencing the
    target's HP at cast time from the log)."""
    if md is None:
        md = fetch_master_data(token, report_code)
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
            ev = gql(token, Q, {"c": report_code, "ids": fids, "st": st, "en": win_e}
                     )["reportData"]["report"]["events"]
        except Exception as ex:
            print(f"  Warning: saves Casts events failed: {ex}")
            break
        for d in ev.get("data", []):
            if d.get("type") != "cast":
                continue
            cat = EXTERNAL_ABILITIES.get(gid2name.get(d.get("abilityGameID"), ""))
            if not cat:
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


def fetch_ability_icons(token: str, report_code: str, fight_ids: list, md: dict = None) -> dict:
    """Map ability name → real WCL icon slug (no .jpg). Two layers:
    1. masterData abilities — covers EVERY ability in the report, including casts that
       never hit the raid (heals, interrupted spells like Holy Smite / Great Heal).
    2. DamageTaken table — authoritative icon for whatever actually hit the raid; overrides
       layer 1 to sidestep the wrong-spell-ID / reused-asset problem for raid-facing hits."""
    if md is None:
        md = fetch_master_data(token, report_code)
    # layer 1 — masterData (precomputed in md; so interrupted/healing casts resolve an icon too)
    icons = dict(md["icons"])
    # layer 2 — DamageTaken (authoritative for raid hits; overrides layer 1)
    if fight_ids:
        Q = """query($c:String!,$f:[Int]){reportData{report(code:$c){
            table(dataType: DamageTaken, fightIDs:$f, hostilityType:Friendlies)}}}"""
        try:
            t = gql(token, Q, {"c": report_code, "f": fight_ids})["reportData"]["report"]["table"]
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


def fetch_deaths_split(token: str, report_code: str, md: dict = None):
    """Curated deaths from the WCL Deaths table (WCL excludes Hunter Feign Death,
    unlike raw combat-log UNIT_DIED). Split boss vs trash by each death's fight —
    boss fights carry an encounterID, trash fights don't.
    Returns (boss_deaths, trash_deaths, recaps, deaths_by_boss): per-PLAYER boss/trash
    counts, a per-player list of killing blows {boss, killer, amount, overkill} for the
    death drill-down, and a per-BOSS death tally {boss_name: count} for the Overview tiles."""
    Qf = """query($c:String!){reportData{report(code:$c){fights{ id name encounterID kill }}}}"""
    fights = gql(token, Qf, {"c": report_code})["reportData"]["report"]["fights"]
    fid_is_boss = {f["id"]: bool(f["encounterID"]) for f in fights}
    fid_is_kill = {f["id"]: bool(f.get("kill")) for f in fights}
    fid_name    = {f["id"]: f.get("name", "") for f in fights}

    # id → name maps to label the per-death recap timeline (abilities + ALL actors, including
    # NPCs so boss-ability sources resolve) — from the shared masterData fetch.
    if md is None:
        md = fetch_master_data(token, report_code)
    abil_name  = md["gid2name"]
    actor_name = md["id2name"]

    Qd = """query($c:String!,$f:[Int]){reportData{report(code:$c){
        table(dataType: Deaths, fightIDs:$f)}}}"""
    t = gql(token, Qd, {"c": report_code, "f": list(fid_is_boss)})["reportData"]["report"]["table"]
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
            abid = ev.get("abilityGameID")
            sid  = ev.get("sourceID")
            ability = abil_name.get(abid) or ("Melee" if abid in (0, 1, None) else "(spell)")
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
# Faerie Fire normal OR feral; CoE OR CoS). Uptime = UNION of every matching aura's bands
# / fight duration, so two fills covering different windows add up correctly. GUIDs + icons
# verified against live TBC 2.5 Debuffs-table data (hostilityType: Enemies) — not memory.
# `soft` flags proc-based debuffs (ISB, Crusader) whose natural uptime ceiling is lower, so
# the dashboard grades them on a gentler scale instead of reading a healthy 60% as "failing".
DEBUFF_SLOTS = [
    {"key": "coe",    "label": "Curse of Elements",  "cat": "Magic",   "guids": [27228, 27229], "icon": "spell_shadow_chilltouch"},
    {"key": "sweav",  "label": "Shadow Weaving",     "cat": "Magic",   "guids": [15258],        "icon": "spell_shadow_blackplague"},
    {"key": "isb",    "label": "Shadow Vuln. (ISB)", "cat": "Magic",   "guids": [17800],        "icon": "spell_shadow_shadowbolt", "soft": True},
    {"key": "sunder", "label": "Sunder / Expose",    "cat": "Armor",   "guids": [25225, 26866], "icon": "ability_warrior_riposte"},
    {"key": "ff",     "label": "Faerie Fire",        "cat": "Armor",   "guids": [26993, 27011], "icon": "spell_nature_faeriefire"},
    {"key": "creck",  "label": "Curse of Reckless.", "cat": "Armor",   "guids": [27226],        "icon": "spell_shadow_unholystrength"},
    {"key": "jow",    "label": "Judge: Wisdom",      "cat": "Utility", "guids": [27164],        "icon": "spell_holy_righteousnessaura"},
    {"key": "jotc",   "label": "Judge: Crusader",    "cat": "Utility", "guids": [27159],        "icon": "spell_holy_holysmite", "soft": True},
]

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
            rep = gql(token, Q, {"c": report_code})["reportData"]["report"]
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

def fetch_mana_returns(token: str, report_code: str, kills: list, md: dict = None) -> dict:
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
        md = fetch_master_data(token, report_code)   # actors carry petOwner (totem→shaman resolution)
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
            ev = gql(token, QR, {"c": report_code, "ids": fids, "st": cur, "en": en}
                     )["reportData"]["report"]["events"]
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
                    ev = gql(token, QI, {"c": report_code, "ids": fids, "st": cur, "en": en,
                                         "a": float(aid)})["reportData"]["report"]["events"]
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


def fetch_sunder_armor(token: str, report_code: str, kills: list, md: dict = None) -> dict:
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
        md = fetch_master_data(token, report_code)
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
                ev = gql(token, QD, {"c": report_code, "ids": fids, "st": cur, "en": en,
                                     "a": float(aid)})["reportData"]["report"]["events"]
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


# ══════════════════════════════════════════════════════════════════════════════
# Build WEEK_DATA
# ══════════════════════════════════════════════════════════════════════════════

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


def build_week_data(report_code: str, token: str,
                    log_data: dict = None, report: dict = None, history: dict = None) -> dict:
    cache = load_cache()
    print(f"\n[1/5] Fetching report metadata: {report_code}")
    if report is None:   # may be pre-fetched by main() to avoid a duplicate call
        report = gql(token, Q_REPORT, {"code": report_code})["reportData"]["report"]

    zone      = (report.get("zone") or {}).get("name", "Unknown")  # WCL returns zone:null
    start_ms  = report["startTime"]                                 # until it classifies a report
    start_dt  = time.strftime("%b %d, %Y %H:%M", time.localtime(start_ms / 1000))
    kills     = [f for f in report["fights"] if f.get("kill")]
    fight_ids = [f["id"] for f in kills]

    # Group kill times by zone for the header pills. `boss_times` stays a flat
    # {name: seconds} (the HTML reads it as a number); `boss_meta` is the additive parallel
    # key carrying portrait/encounter id, per-boss deaths and raid DPS for the new tiles.
    boss_times: dict[str, int] = {}
    boss_meta:  dict[str, dict] = {}
    for f in kills:
        duration_s = (f["endTime"] - f["startTime"]) // 1000
        boss_times[f["name"]] = duration_s
        boss_meta[f["name"]] = {"seconds": duration_s,
                                "encounter_id": f.get("encounterID"),
                                "deaths": 0}
    print(f"   Zone: {zone}  |  {len(kills)} kills  |  Fights: {fight_ids}")

    # ── Player details + gear ──────────────────────────────────────────────
    # ONE masterData fetch, shared by every consumer below (crit attribution, tank biggest-hit,
    # toolkit, mana returns, sunder, death recaps, ability icons) — see fetch_master_data. The
    # crit/damage attribution path uses the Player-filtered actor list (md["players"]) exactly as
    # before (pets must NOT be in the crit name map); pet-aware consumers use the full md["actors"].
    md        = fetch_master_data(token, report_code)
    actors    = md["players"]                       # == old actors(type:"Player")
    _abils    = md["abilities"]
    sb_ids    = {a.get("gameID") for a in _abils if (a.get("name") or "") == "Shadow Bolt"}

    print(f"\n[2/5] Fetching player details & gear (fight_ids={fight_ids[:3]}...)")
    pd_data = gql(token, Q_PLAYER_DETAILS, {"code": report_code, "fightIDs": fight_ids})
    raw_pd  = pd_data["reportData"]["report"]["playerDetails"]
    # playerDetails is a raw JSON blob: { "data": { "playerDetails": { "dps": [...], "tanks": [...], "healers": [...] } } }
    if isinstance(raw_pd, str):
        raw_pd = json.loads(raw_pd)
    pd = raw_pd.get("data", {}).get("playerDetails", raw_pd.get("data", raw_pd))

    # Flatten all players into a unified list
    players = []
    role_map = {"tanks": "Tank", "healers": "Healer", "dps": "Physical"}
    for role_key, role_label in role_map.items():
        for p in (pd.get(role_key) or []):
            # spec is in specs[0]["spec"]
            specs = p.get("specs") or []
            spec  = specs[0]["spec"] if specs else ""
            # gear lives inside combatantInfo[0]["gear"] when the logger captured COMBATANT_INFO
            ci    = p.get("combatantInfo") or []
            gear  = ci[0].get("gear", []) if ci else []
            players.append({
                "name": p["name"],
                "type": p.get("type", ""),
                "spec": spec,
                "role": role_label,
                "gear": gear,
                "potion_use":      p.get("potionUse", 0),
                "healthstone_use": p.get("healthstoneUse", 0),
            })
    # Deduplicate — WCL sometimes lists the same player in multiple role buckets.
    # Keep the entry whose role bucket matches the spec (DPS spec in tanks group = wrong).
    seen = {}
    for p in players:
        name = p["name"]
        if name not in seen:
            seen[name] = p
        else:
            # Prefer whichever has a more specific spec set, or the later one
            # (later entries tend to be the correct DPS assignment)
            existing = seen[name]
            # If existing is Tank/Healer but spec looks like DPS, replace it
            bad_tank_specs = {"Fury","Arms","Retribution","Enhancement","Survival",
                              "Marksmanship","Beast Mastery","Combat","Assassination",
                              "Subtlety","Arcane","Fire","Frost","Shadow","Elemental",
                              "Demonology","Affliction","Destruction","Balance"}
            if existing["role"] == "Tank" and existing["spec"] in bad_tank_specs:
                seen[name] = p
    players = list(seen.values())
    print(f"  Found {len(players)} unique players")

    # ── Gear from COMBATANT_INFO events (more reliable than playerDetails.combatantInfo) ──
    gear_by_name = fetch_gear_from_events(token, report_code, kills, actors)
    ci_consumables = gear_by_name.pop("__consumables__", {})   # pull-time flask/food/elixir
    for p in players:
        if not p["gear"] and p["name"] in gear_by_name:
            p["gear"] = gear_by_name[p["name"]]

    # Fix role classification — WCL sometimes puts DPS specs in "tanks" or "healers".
    # (spec sets are module-level constants, shared with fetch_fight_roles)
    for p in players:
        spec = p.get("spec", "")
        cls  = p.get("type", "")
        # Re-classify based on spec — more reliable than WCL's role bucket
        if spec in CASTER_SPECS:
            p["role"] = "Caster"
        elif spec in PHYSICAL_SPECS:
            p["role"] = "Physical"
        elif spec in HEALER_SPECS:
            p["role"] = "Healer"
        elif spec in TANK_SPECS and p["role"] in ("Tank", "Physical"):
            p["role"] = "Tank"  # only keep as Tank if WCL already said so

    # ── Calculate expected crit from gear ─────────────────────────────────
    print(f"\n[3/5] Calculating gear crit for {len(players)} players (may take ~30s, items are cached)...")
    for p in players:
        name = p["name"]
        if name in gear_by_name:
            # Prefer combatantinfo crit stats — critMelee/critSpell direct from WCL, no item lookup
            ci = gear_by_name[name][0]
            if p["role"] in ("Caster", "Healer"):
                crit_r = ci.get("_crit_spell", 0)
            elif p["role"] in ("Physical", "Tank"):
                crit_r = ci.get("_crit_melee", 0)
            else:
                crit_r = max(ci.get("_crit_melee", 0), ci.get("_crit_spell", 0))
        else:
            crit_r = gear_crit_rating(token, p["gear"], cache)
        ci = gear_by_name.get(name, [{}])[0]
        agi, intel = ci.get("_agility", 0), ci.get("_intellect", 0)
        p["agility"], p["intellect"] = agi, intel
        p["gear_crit_rating"] = crit_r
        p["expected_crit_pct"] = expected_crit(p["type"], p["spec"], crit_r, agi, intel)
        print(f"   {p['name']:18s}  gear_crit={crit_r:4d}  agi={agi:4d}  expected={p['expected_crit_pct']:.1f}%")
    save_cache(cache)

    # ── Actual crit from damage events + deaths table ─────────────────────
    print(f"\n[4/5] Fetching damage events & death table...")
    crit_counts_by_id = fetch_actual_crit(token, report_code, kills, crit_track_ids=sb_ids)
    crit_by_name      = merge_actor_names(crit_counts_by_id, actors)

    # Curated deaths (no Feign Death) from WCL, split boss vs trash by fight, + killing blows.
    death_boss, death_trash, death_recaps, deaths_by_boss = fetch_deaths_split(token, report_code, md)
    for b, n in deaths_by_boss.items():
        if b in boss_meta:
            boss_meta[b]["deaths"] = n

    # Overlay real HP% (from the combat log) onto each death recap, and build the healer
    # reaction-time heatmap — both from parse_combat_log's hp_samples / log_deaths (zero API).
    heal_reaction = {}
    if log_data:
        hp_tl = compute_death_hp_timelines(log_data)
        for nm, recaps in death_recaps.items():
            buckets = defaultdict(list)
            for lt in hp_tl.get(nm, []):
                buckets[lt["boss"]].append(lt["timeline"])
            for r in recaps:
                q = buckets.get(r.get("boss"))
                if q:
                    r["hp_timeline"] = q.pop(0)
        heal_reaction = compute_reaction_times(log_data)

    # Also pull damage totals from table (still useful even without crit breakdown)
    dmg_data     = gql(token, Q_DAMAGE_TABLE, {"code": report_code, "fightIDs": fight_ids})
    actual_stats = parse_damage_table(dmg_data["reportData"]["report"]["table"])

    # Attach actual crit (from events) + damage totals + deaths to players
    for p in players:
        ev_stats  = crit_by_name.get(p["name"], {})
        hits, crits = ev_stats.get("hits", 0), ev_stats.get("crits", 0)
        denom = hits + crits
        actual_pct = round(crits / denom * 100, 1) if denom > 0 else 0.0

        dmg_stats = actual_stats.get(p["name"], {})
        p["actual_crit_pct"]  = actual_pct
        p["total_dmg"]        = dmg_stats.get("total_dmg", 0)
        p["active_time_ms"]   = dmg_stats.get("active_time_ms", 0)
        p["deaths"]           = death_boss.get(p["name"], 0)
        p["deaths_trash"]     = death_trash.get(p["name"], 0)
        p["luck_delta"]       = round(p["actual_crit_pct"] - p["expected_crit_pct"], 2)

        # Gold-standard luck: this week's crit vs the player's OWN rolling baseline. A
        # sustained step above baseline reads as a gear upgrade; a one-week spike as luck —
        # the weekly `series` (sparkline) tells them apart. Needs MIN_WEEKS of history.
        h = (history or {}).get(p["name"])
        p["crit_baseline"] = h["mean"] if h else None
        p["crit_weeks"]    = h["n"] if h else 0
        p["crit_std"]      = h["std"] if h else None
        p["crit_series"]   = (h["series"] + [actual_pct]) if h else [actual_pct]
        p["crit_prev"]     = h["prev"] if h else None
        p["luck_hist"]     = (round(actual_pct - h["mean"], 1)
                              if h and h["n"] >= LUCK_MIN_WEEKS else None)

    # ── Total fight duration (for active time %) ───────────────────────────
    total_fight_ms = sum(f["endTime"] - f["startTime"] for f in kills)
    for p in players:
        p["active_time_pct"] = round(p["active_time_ms"] / total_fight_ms * 100, 1) if total_fight_ms else 0.0

    # ── Per-fight roles (spec-swap aware) — heal early/DPS late, prot/ret splits ──
    # Prefer combat-log ground truth (free, boss-melee tank signal beats WCL spec labels);
    # fall back to the per-fight playerDetails API only if no log or names don't line up.
    fight_roles, fight_durs = None, None
    if log_data and log_data.get("fight_roles_log"):
        fr, fd, cov = build_fight_roles_from_log(log_data["fight_roles_log"], kills)
        if cov >= 0.5:
            fight_roles, fight_durs = fr, fd
            print(f"   Per-fight roles from combat log ({cov*100:.0f}% boss-name match) — no API cost")
    if fight_roles is None:
        print("   Per-fight roles from WCL playerDetails (API)")
        fight_roles, fight_durs = fetch_fight_roles(token, report_code, kills)
        # WCL's per-fight spec labels drop ferals ('Warden') and undercount tanks, so harden
        # per-fight tank attribution from the boss-melee signal — restricted to known roster
        # tanks, additive only (see harden_tank_fights). Keeps log-less weeks from blanking a
        # tank, per the WCL-Durability Principle. (The log role path already has this signal.)
        roster_tanks = {p["name"] for p in players if p.get("role") == "Tank"}
        harden_tank_fights(token, report_code, kills, fight_roles, roster_tanks)
    # Report bosses with NO combat-log coverage (e.g. logging started mid-session) — these
    # are missing from the log-sourced KPIs, so surface them rather than silently dropping.
    logged_bosses = set((log_data or {}).get("fight_roles_log", {}).keys())
    log_missing = [f["name"] for f in kills if f["name"] not in logged_bosses] if log_data else []
    if log_missing:
        print(f"   ⚠ no combat-log coverage for: {', '.join(log_missing)}")
    n_kills = len(kills)
    for p in players:
        fr = fight_roles.get(p["name"], {})
        p["fights_healed"] = len(fr.get("Healer", []))
        p["fights_tanked"] = len(fr.get("Tank", []))
        p["fights_dps"]    = len(fr.get("dps", []))
        p["fights_total"]  = n_kills
    # Anyone who tanked ≥1 fight counts as a tank target for healers' tank% split.
    tank_names = {n for n, fr in fight_roles.items() if fr.get("Tank")}

    # Healer throughput/efficiency metrics — SCOPED to each healer's heal-fights.
    heal_by_fight   = fetch_healing_by_fight(token, report_code, kills)
    healing_metrics = compute_healing_metrics(heal_by_fight, fight_roles, fight_durs, tank_names)
    healing_spells  = fetch_healing_spells(token, report_code, kills, actors)
    healer_mana     = fetch_healer_mana(token, report_code, fight_ids)
    # Performance metric — native WCL PARSE % (rankPercent vs the FULL logged population, not the
    # old top-100 cohort ratio). ONE fetch (report.rankings) covers all roles: the dps parse feeds
    # the DPS table AND tank threat; the hps parse feeds healers. Held in the same dps_war/tank_war/
    # healer_war keys the downstream emit/DB/render already read (values are now 0–100, not ratios).
    parse_pct  = fetch_parse_percentiles(token, report_code, kills)
    dps_war    = {nm: p["dps"] for nm, p in parse_pct.items() if p.get("dps") is not None}
    tank_war   = dps_war                                   # tank threat = their own dps parse %
    healer_war = {nm: p["hps"] for nm, p in parse_pct.items() if p.get("hps") is not None}
    # Tank scorecard — WCL is the durable source of record, run EVERY week (per-boss DTPS,
    # mitigation, school split, cooldowns, biggest hit, raid DPS for the boss tiles). The
    # combat log, when present, only adds lowest-HP%-survived; a missing log never blanks it.
    tank_metrics, boss_raid_dps = build_tank_scorecard_extended(
        token, report_code, kills, fight_roles, fight_durs, heal_by_fight, actors, md)
    for b, d in boss_raid_dps.items():
        if b in boss_meta:
            boss_meta[b]["raid_dps"] = d
    if log_data:
        # enrichment: lowest HP% each tank dropped to per boss (from the HP%-sample timeline)
        hp_samples = log_data.get("hp_samples", {})
        for nm, tm in tank_metrics.items():
            lows = {b: min(s[1] for s in samples)
                    for b, samples in hp_samples.get(nm, {}).items() if samples}
            if lows:
                tm["lowest_hp"] = lows
        # cooldown completeness: the WCL Casts query is kill-scoped (misses CDs popped on wipes) and
        # keyed by a fixed ID set; the combat log catches every defensive CD by NAME across the whole
        # night. Max-merge so neither source loses a cast (belt-and-suspenders).
        cd_casts = log_data.get("cd_casts", {})
        for nm, tm in tank_metrics.items():
            for cd, c in cd_casts.get(nm, {}).items():
                tm["cooldowns"][cd] = max(tm["cooldowns"].get(cd, 0), c)
        # active-mitigation EXECUTION signals (log-only): cast-rate / Lacerate-uptime per the tank's
        # boss-melee-taken time. Powers the tank Execution pillar (see _perfRows).
        _bms = log_data.get("boss_melee_sec", {})
        _mc  = log_data.get("mitig_cast", {})
        _lac = log_data.get("lacerate_pct", {})
        for nm, tm in tank_metrics.items():
            tm["boss_melee_sec"] = _bms.get(nm, 0)
            tm["mitig_casts"]    = _mc.get(nm, 0)
            tm["lacerate_pct"]   = _lac.get(nm, 0)   # Lacerate uptime % of active-melee time (bear)
    role_spells     = fetch_role_spell_usage(token, report_code, fight_ids, players)
    # Per-fight DamageDone — one paginated fetch feeds the uptime heatmap.
    dmg_by_fight    = fetch_damage_by_fight(token, report_code, kills)
    uptime_by_fight = damage_uptime_by_fight(dmg_by_fight, kills)        # {name: {boss: uptime%}}
    for p in players:
        p["uptime_by_fight"] = [{"boss": b, "uptime": u}
                                for b, u in uptime_by_fight.get(p["name"], {}).items()]
    # Healer casting-uptime per fight — reuses heal_by_fight (already fetched above), no extra API.
    healer_uptime = healer_uptime_by_fight(heal_by_fight, kills)
    for p in players:
        p["healer_uptime_by_fight"] = [{"boss": b, "uptime": u}
                                       for b, u in healer_uptime.get(p["name"], {}).items()]
    # Raid debuff coverage — pure WCL, per boss (CoE/Misery/Shadow Weaving/ISB + armor + judgements)
    debuff_coverage = fetch_debuff_coverage(token, report_code, kills)
    # Class toolkit — each DPS's signature class-relative utility (cast-based, pure WCL)
    mana_returns  = fetch_mana_returns(token, report_code, kills, md)
    sunder_armor  = fetch_sunder_armor(token, report_code, kills, md)
    saves         = fetch_saves(token, report_code, kills, md)   # protective/external casts on allies
    damage_by_sel = fetch_damage_by_selection(token, report_code)
    class_toolkit = build_class_toolkit(token, report_code, kills, md)
    # Fold the biggest Shadow Bolt crit (tracked free during the crit pass) into the toolkit
    # counts so the warlock metric resolver reads it like any other value.
    for nm, cd in crit_by_name.items():
        if cd.get("sb_crit"):
            class_toolkit.setdefault(nm, {})["sb_crit"] = cd["sb_crit"]
    # Fold the shadow priest's VT mana from the mana-returns batteries (the VT source already
    # paged Resources energize events there) so the toolkit doesn't run a SECOND full Resources
    # pass. The VT provider total == the old toolkit vt_mana sum (same energize events, same
    # source — VT credits the priest directly, no pet mapping). See build_class_toolkit.
    for _bat in (mana_returns or {}).get("batteries", []):
        if _bat.get("label") == MANA_SOURCES[0]["label"]:    # "Vampiric Touch"
            for _prov in _bat.get("providers", []):
                class_toolkit.setdefault(_prov["name"], {})["vt_mana"] = _prov["mana"]
    print(f"  ✓ class toolkit: {len(class_toolkit)} players with utility casts")

    # ── Assemble WEEK_DATA ─────────────────────────────────────────────────
    print(f"\n[5/5] Assembling WEEK_DATA...")

    # Crit lists by role
    def crit_list(role):
        return [
            {"name": p["name"], "actual": p["actual_crit_pct"], "expected": p["expected_crit_pct"], "luck": p["luck_delta"]}
            for p in sorted(players, key=lambda x: -x["actual_crit_pct"])
            if p["role"] == role and p["actual_crit_pct"] > 0
        ]

    # Deaths list
    deaths_list = [
        {"name": p["name"], "role": p["role"], "deaths": p["deaths"]}
        for p in sorted(players, key=lambda x: -x["deaths"])
        if p["deaths"] > 0
    ]

    # Damage list
    dmg_list = [
        {"name": p["name"], "role": p["role"], "total_dmg": p["total_dmg"], "active_pct": p["active_time_pct"]}
        for p in sorted(players, key=lambda x: -x["total_dmg"])
        if p["total_dmg"] > 0
    ]

    week_data = {
        "meta": {
            "report_code": report_code,
            "date": start_dt,
            "start_ms": start_ms,         # chronological sort key for the history DB
            "zone": zone,
            "kills": len(kills),
            "fight_ids": fight_ids,
            "log_missing": log_missing,   # report bosses absent from the combat log
        },
        "boss_times": boss_times,
        "boss_meta":  boss_meta,   # additive: portrait/encounter id + per-boss deaths/raid DPS
        "crit_casters":  crit_list("Caster"),
        "crit_physical": crit_list("Physical"),
        "crit_tanks":    crit_list("Tank"),
        "crit_healers":  crit_list("Healer"),
        "deaths": deaths_list,
        "damage": dmg_list,
        "players": [
            {
                "name": p["name"], "role": p["role"], "class": p["type"], "spec": p["spec"],
                "expected_crit": p["expected_crit_pct"],
                "actual_crit":   p["actual_crit_pct"],
                "luck_delta":    p["luck_delta"],
                "gear_crit_rating": p["gear_crit_rating"],
                "total_dmg":     p["total_dmg"],
                "active_pct":    p["active_time_pct"],
                "deaths":        p["deaths"],
                "deaths_trash":  p.get("deaths_trash", 0),
                "fights_total":  p.get("fights_total", 0),
                "fights_healed": p.get("fights_healed", 0),
                "fights_tanked": p.get("fights_tanked", 0),
                "fights_dps":    p.get("fights_dps", 0),
                "uptime_by_fight": p.get("uptime_by_fight", []),
                "healer_uptime_by_fight": p.get("healer_uptime_by_fight", []),
                "crit_baseline": p.get("crit_baseline"),
                "crit_weeks":    p.get("crit_weeks", 0),
                "crit_std":      p.get("crit_std"),
                "crit_series":   p.get("crit_series", []),
                "crit_prev":     p.get("crit_prev"),
                "luck_hist":     p.get("luck_hist"),
            }
            for p in players
        ],
        # ability name → real WCL icon slug, for the Hall of Shame legend
        "ability_icons": fetch_ability_icons(token, report_code, fight_ids, md),
        # accurate pull-time consumables (from combatantinfo auras)
        "ci_consumables": ci_consumables,
        # per-player healing throughput/efficiency + per-spell breakdown + mana
        "healing_metrics": healing_metrics,
        "healing_spells":  healing_spells,
        "healer_mana":     healer_mana,
        "healer_war":      healer_war,
        # Performance metric — WCL parse % (rankPercent, 0–100) per player; folded onto
        # damageBySelection / tankScorecard rows in map_to_week_data (kept in the legacy
        # dps_war/tank_war/healer_war keys + vs_replacement field, which now hold the percentile).
        "dps_war":         dps_war,
        "tank_war":        tank_war,
        # per-fight roles (spec-swap aware) + per-fight durations for tank scorecard
        "fight_roles":     fight_roles,
        "fight_durs":      fight_durs,
        "tank_metrics":    tank_metrics,
        # per-player killing blows for the death drill-down
        "death_recaps":    death_recaps,
        # healer reaction-time heatmap (median s to first heal after dropping <50% HP)
        "heal_reaction":   heal_reaction,
        # top abilities cast per role + per player
        "role_spells":     role_spells.get("roles", {}),
        "player_spells":   role_spells.get("players", {}),
        # per-boss uptime of key DPS-amplifying raid debuffs (pure WCL)
        "debuff_coverage": debuff_coverage,
        # per-player signature class-utility cast counts (pure WCL Casts)
        "class_toolkit":   class_toolkit,
        # mana returned to the raid, per provider (mana-battery leaderboard)
        "mana_returns":    mana_returns,
        # per-player Sunder Armor quality (effective/refreshed/wasted, pure WCL)
        "sunder_armor":    sunder_armor,
        # protective/external casts on allies (paladin Hands, dispels, battle-res) — "saving others"
        "saves":           saves,
        # per-player damage + active time split All/Bosses/Trash (WCL-style DPS denominator)
        "damage_by_sel":   damage_by_sel,
    }

    return week_data


# ══════════════════════════════════════════════════════════════════════════════
# Map WCL data → WEEK_DATA format expected by the HTML dashboard
# ══════════════════════════════════════════════════════════════════════════════

def _tally_spells(names, icons=None):
    """Tally a list of interrupted spell NAMES into [{spell, n, icon}], most-kicked first.
    `icons` is the ability-name → WCL icon-slug map, so each cast carries its real game icon."""
    icons = icons or {}
    agg = {}
    for s in (names or []):
        agg[s] = agg.get(s, 0) + 1
    return sorted(({"spell": s, "n": n, "icon": icons.get(s, "")} for s, n in agg.items()),
                  key=lambda x: -x["n"])


def _ice_player_spells(player_spells, icons):
    """Attach a WCL icon slug to every ability in the per-player spell-usage map, so the
    'Spell Usage by Player' card can render the same icon pills as the interrupt list."""
    icons = icons or {}
    return {
        role: [{**pl, "abilities": [{**a, "icon": icons.get(a.get("ability"), "")}
                                     for a in (pl.get("abilities") or [])]}
               for pl in plist]
        for role, plist in (player_spells or {}).items()
    }


def build_consumable_compliance(consumable_usage):
    """Reshape consumableUsage into the role-split compliance grid shape — a pure transform,
    no new queries. Each row → {name, role, flask, food, weapon, combat_pots, alt_pot}.
    Flask/Elixirs passes on a flask OR both elixir slots (battle AND guardian)."""
    out = []
    for e in consumable_usage:
        elixirs = e.get("elixirs") or []
        has_guardian = any(x in GUARDIAN_ELIXIRS for x in elixirs)
        has_battle   = any(x not in GUARDIAN_ELIXIRS for x in elixirs)
        flask_ok = bool(e.get("flask")) or (has_battle and has_guardian)
        # Alt pot priority: Nightmare Seed > Flame Cap > Dark/Demonic Rune (show the item name)
        if   e.get("nightmare_seed"): alt_pot = "Nightmare Seed"
        elif e.get("flamecap"):       alt_pot = "Flame Cap"
        elif e.get("rune_name"):      alt_pot = e["rune_name"]
        elif e.get("rune"):           alt_pot = "Mana Rune"   # used but specific name not captured
        else:                         alt_pot = None
        out.append({
            "name":  e["name"],
            "role":  e.get("role", ""),
            "flask": flask_ok,
            # show-our-work: the SPECIFIC items behind each ✓, so the grid is auditable
            "flask_name": e.get("flask") or None,      # the flask, when one is present
            "elixirs":    elixirs,                     # the 2-elixir path (battle + guardian)
            "food":  bool(e.get("food")),
            "weapon": bool(e.get("weapon_oil")),
            "combat_pots": e.get("combat_pots", []),   # all combat pots popped (may be empty)
            "alt_pot": alt_pot,
            # fight-distribution context — used by JS to bucket hybrid players correctly
            "effective_role": e.get("effective_role", e.get("role", "")),
            "fights_tanked":  e.get("fights_tanked", 0),
            "fights_healed":  e.get("fights_healed", 0),
            "fights_total":   e.get("fights_total", 0),
        })
    return out


def map_to_week_data(wcl: dict) -> dict:
    """Convert WCL API output to the WEEK_DATA structure the HTML renders from."""
    players = wcl.get("players", [])

    def _eff(p):
        """Effective (played) role for a player dict from the `players` whitelist."""
        return _effective_role(p.get("role", ""), p.get("spec", ""),
                               p.get("fights_tanked", 0), p.get("fights_healed", 0),
                               p.get("fights_total", 0))

    _toolkit_counts = wcl.get("class_toolkit", {})
    _tk_icons       = wcl.get("ability_icons", {})
    _kill_min       = sum((wcl.get("boss_times") or {}).values()) / 60.0
    _dps_war        = wcl.get("dps_war", {})    # name → WCL dps parse % (DPS table cell)
    _tank_war       = wcl.get("tank_war", {})   # name → WCL dps parse % (tank threat cell)

    def _toolkit_cell(p):
        """Signature class-utility cell for a damage row, or None. Resolves the per-class
        metric from cast counts + attaches a live icon and a ⚔ off-role annotation for
        hybrids (a tank/healer who mostly DPS'd), mirroring the consumables report."""
        m = _toolkit_metric(p.get("class", ""), p.get("spec", ""),
                            _toolkit_counts.get(p["name"], {}), _kill_min)
        if not m:
            return None
        icon = _tk_icons.get(m["icon_ability"]) or m["fallback"]
        cell = {"label": m["label"], "value": m["value"], "title": m["title"], "icon": icon}
        if m.get("num") is not None:
            cell["num"] = m["num"]          # raw numeric, for week-over-week trending
        if m.get("tag"):
            cell["tag"] = m["tag"]
        # hybrid tag: this metric belongs to someone whose roster role isn't what they played
        if _eff(p) != p.get("role") and p.get("fights_dps", 0) > 0:
            cell["off"] = f"{p.get('fights_dps', 0)}/{p.get('fights_total', 0)}"
        return cell

    def crit_list(role):
        return [
            {"name": p["name"], "crit": p["actual_crit"]}
            for p in sorted(players, key=lambda x: -x["actual_crit"])
            if p["role"] == role and p["actual_crit"] > 0
        ]

    # Deaths — boss + trash, from the combat-log encounter-window split
    _recaps = wcl.get("death_recaps") or {}
    death_list = [
        {"name": p["name"], "role": p["role"],
         "total": p.get("deaths", 0) + p.get("deaths_trash", 0),
         "trash": p.get("deaths_trash", 0),
         "recap": _recaps.get(p["name"], [])}
        for p in sorted(players, key=lambda x: -(x.get("deaths", 0) + x.get("deaths_trash", 0)))
        if (p.get("deaths", 0) + p.get("deaths_trash", 0)) > 0
    ]

    # Luck KPI tiles — luck is now vs the player's OWN multi-week baseline (gold standard).
    # `expected`/`luck_delta` (gear-modeled) are kept as a stored reference only.
    luck_tiles = [
        {
            "name": p["name"], "role": p["role"],
            "actual": p["actual_crit"], "expected": p["expected_crit"],
            "baseline": p.get("crit_baseline"), "weeks": p.get("crit_weeks", 0),
            "std": p.get("crit_std"), "series": p.get("crit_series", []),
            "prev": p.get("crit_prev"),
            "luck": p.get("luck_hist"),   # actual − baseline; None until LUCK_MIN_WEEKS
        }
        for p in sorted(players, key=lambda x: -(x.get("luck_hist") if x.get("luck_hist") is not None else -999))
        if p["actual_crit"] > 0
    ]

    # Consumables → role-split COMPLIANCE grid (binary ✓/✗ per role). A pure reshape of
    # consumableUsage via build_consumable_compliance — consum_list is assigned just after
    # consum_usage is built (below).

    # ── Hall of Shame: avoidable damage (per-mechanic) + legend + friendly fire ──
    roster_idx = {p["name"]: p for p in players}
    def _srcs(d):
        return sorted([{"ability": a, "dmg": v} for a, v in (d or {}).items()],
                      key=lambda x: -x["dmg"])
    def _avsrc(p):
        hits = p.get("avoidable_hits") or {}
        return sorted(
            [{"ability": a, "dmg": v, "hits": hits.get(a, [])}
             for a, v in (p.get("avoidable_sources") or {}).items()],
            key=lambda x: -x["dmg"])
    avoidable_list = [
        {"name": p["name"], "role": p["role"], "class": p.get("class", ""),
         "dmg": p.get("avoidable_dmg", 0), "sources": _avsrc(p)}
        for p in sorted(players, key=lambda x: -x.get("avoidable_dmg", 0))
        if p.get("avoidable_dmg", 0) > 0
    ]
    # mechanic → {boss, icon} for the legend (icon: real WCL slug, with overrides)
    _icons     = wcl.get("ability_icons", {})
    _mech_boss = wcl.get("avoidable_mech_boss", {})
    _mechs_seen = {a for p in players for a in (p.get("avoidable_sources") or {})}
    avoidable_mechanics = {
        m: {"boss": _mech_boss.get(m, ""),
            "icon": ICON_OVERRIDES.get(m) or _icons.get(m, "")}
        for m in _mechs_seen
    }
    # friendly fire (source side) — join with roster for class/role coloring
    friendly_fire = [
        {"name": f["name"],
         "role":  roster_idx.get(f["name"], {}).get("role", ""),
         "class": roster_idx.get(f["name"], {}).get("class", ""),
         "dmg": f.get("dmg", 0), "incidents": f.get("incidents", 0),
         "category": "mechanic", "cats": f.get("cats", {}),
         "hits": f.get("hits", []),
         "mechanics": f.get("mechanics", {}),
         "bosses": f.get("bosses", []),
         "victims": f.get("victims", []),
         "sources": _srcs(f.get("mechanics"))}
        for f in wcl.get("friendly_fire", [])
        if f.get("dmg", 0) >= FF_MIN_DMG
    ]

    # MC accountability — Saves (CC'd a controlled ally) and Liabilities (AoE'd one).
    def _mc_row(f):
        return {**f,
                "role":  roster_idx.get(f["name"], {}).get("role", ""),
                "class": roster_idx.get(f["name"], {}).get("class", "")}
    mc_saves  = [_mc_row(f) for f in wcl.get("mc_saves", [])]
    mc_liable = [_mc_row(f) for f in wcl.get("mc_liable", [])]

    # Consumable AUDIT — the full raid-buff spread. Persistent buffs (flask/elixirs/food/
    # scrolls) come from the pull-time COMBATANT_INFO snapshot (catches pre-applied); active
    # items (pots/runes/healthstones) come from cast counts. One row per raider.
    ci_use = wcl.get("ci_consumables", {})
    cu_use = wcl.get("consum_use", {})
    lbl_use = wcl.get("consum_label", {})
    consum_names = set(ci_use) | set(cu_use)
    consum_usage = []
    for n in consum_names:
        c = ci_use.get(n, {})
        u = cu_use.get(n, {})
        lb = lbl_use.get(n, {})
        flask = c.get("flask", "")
        elixirs = c.get("elixirs", [])
        food = bool(c.get("food"))
        p_info        = roster_idx.get(n, {})
        role          = p_info.get("role", "")
        spec          = p_info.get("spec", "")
        fights_tanked = p_info.get("fights_tanked", 0)
        fights_healed = p_info.get("fights_healed", 0)
        fights_total  = p_info.get("fights_total", 0)
        # Hybrid/spec-swap: a tank who mostly DPS'd (or a healer who mostly DPS'd) is
        # evaluated against their off-role's threshold, not their roster slot's.
        effective_role = _effective_role(role, spec, fights_tanked, fights_healed, fights_total)
        consum_usage.append({
            "name": n,
            "role":  role,
            "class": p_info.get("class", p_info.get("type", "")),
            "flask": flask, "elixirs": elixirs, "food": food,
            "scrolls": c.get("scrolls", []), "weapon_oil": bool(c.get("weapon_oil")),
            "potion": u.get("potion", 0), "rune": u.get("rune", 0),
            "healthstone": u.get("healthstone", 0),
            "stones_made": u.get("stones_made", 0),   # warlock raid provision (utility): Create/Ritual of Souls
            "fear_ward":   u.get("fear_ward", 0),      # holy/disc priest anti-fear utility (cast count)
            "blessing":    u.get("blessing", 0),       # paladin Greater Blessing casts (raid buff provision)
            "judge_util":  u.get("judge_util", 0),     # paladin JoW/JoL upkeep (mana/healing to the raid)
            # specific item names for the compliance grid (None if unused)
            "combat_pots": lb.get("combat_pots", []),   # all combat pots popped (Haste/Destruction/Free Action…)
            "rune_name": lb.get("rune"),
            "flamecap": bool(u.get("flamecap")),
            "nightmare_seed": bool(u.get("nightmare_seed")),
            # prepared = has a flask (or 2 elixirs) AND food — the BiS baseline
            "prepared": bool(flask or len(elixirs) >= 2) and food,
            # fight-distribution context for compliance edge cases
            "effective_role": effective_role,
            "fights_tanked": fights_tanked,
            "fights_healed": fights_healed,
            "fights_total": fights_total,
        })
    # least-prepared first (missing flask/food bubbles up — that's the accountability angle)
    consum_usage.sort(key=lambda x: (x["prepared"], bool(x["flask"] or x["elixirs"]), x["food"],
                                     x["name"]))
    consum_list = build_consumable_compliance(consum_usage)

    # Healer scorecard — actual healers only (role=Healer), ranked by effective HPS.
    # Throughput + overheal% (efficiency) + activity% differentiate them; crit luck doesn't.
    heal_metrics = wcl.get("healing_metrics", {})
    heal_spells  = wcl.get("healing_spells", {})
    heal_mana    = wcl.get("healer_mana", {})
    heal_war     = wcl.get("healer_war", {})
    # Driven by who actually HEALED (>=1 heal fight), not by primary role — so a
    # spec-swapper like a heal-early/DPS-late player still shows, scoped to heal fights.
    healing = sorted(
        ({"name": nm,
          "role":  roster_idx.get(nm, {}).get("role", "Healer"),
          "class": roster_idx.get(nm, {}).get("class", ""),
          "eff_hps": m.get("eff_hps", 0), "eff_heal": m.get("eff_heal", 0),
          "overheal_pct": m.get("overheal_pct", 0), "activity_pct": m.get("activity_pct", 0),
          "tank_pct": m.get("tank_pct", 0), "top_spell": m.get("top_spell", ""),
          "fights_healed": m.get("fights_healed", 0),
          "fights_total":  roster_idx.get(nm, {}).get("fights_total", 0),
          "mana_eff": round(m.get("eff_heal", 0) / heal_mana[nm], 1) if heal_mana.get(nm) else 0,
          "vs_replacement": heal_war.get(nm),   # WCL hps parse % (0–100); None ⇒ not ranked yet
          "spells": heal_spells.get(nm, [])[:8]}
         for nm, m in heal_metrics.items() if m.get("eff_heal", 0) > 0),
        key=lambda x: -x["eff_hps"])

    # Tank scorecard — scoped to each player's TANK fights (prot/ret-swap aware), ranked
    # by damage taken/sec. Lower DTPS = better avoidance/mitigation for the same content.
    tank_metrics = wcl.get("tank_metrics", {})
    tank_scorecard = sorted(
        ({"name": nm, "role": "Tank",
          "class": roster_idx.get(nm, {}).get("class", ""),
          "dtps": tm.get("dtps", 0), "taken": tm.get("taken", 0),
          "hps_recv": tm.get("hps_recv", 0),
          "fights_tanked": tm.get("fights_tanked", 0),
          "fights_total":  roster_idx.get(nm, {}).get("fights_total", 0),
          "deaths": roster_idx.get(nm, {}).get("deaths", 0),
          # v2 — survivability depth (all WCL-durable except lowest_hp, which is log enrichment)
          "phys_pct": tm.get("phys_pct", 0), "magic_pct": tm.get("magic_pct", 0),
          "crush_count": tm.get("crush_count", 0), "crit_count": tm.get("crit_count", 0),
          "avoid_pct": tm.get("avoid_pct", 0),
          "biggest_hit": tm.get("biggest_hit"),
          "cooldowns": tm.get("cooldowns", {}),
          # tank Execution inputs (log-only): active-mitigation cast-rate / Lacerate uptime per
          # boss-melee-taken time (the normalization validated in discovery).
          "mitig_casts":    tm.get("mitig_casts", 0),
          "boss_melee_sec": tm.get("boss_melee_sec", 0),
          "lacerate_pct":   tm.get("lacerate_pct", 0),
          "per_boss": tm.get("per_boss", []),
          "lowest_hp": tm.get("lowest_hp", {}),
          # Performance pillar (two parts): threat = the tank's WCL dps PARSE % (WCL ranks tanks by
          # dps); survivability = an ABSOLUTE grade from the mitigation signals above (parse % can't
          # measure mitigation). vs_replacement holds the parse % (0–100); None/0 ⇒ not ranked yet.
          "vs_replacement": _tank_war.get(nm),
          "survival": _tank_survival_grade(tm, roster_idx.get(nm, {}).get("deaths", 0),
                                           roster_idx.get(nm, {}).get("class", ""))}
         for nm, tm in tank_metrics.items()),
        key=lambda x: -x["dtps"])

    meta = wcl.get("meta", {})

    return {
        "meta": {
            "date":        meta.get("date", ""),
            "start_ms":    meta.get("start_ms"),
            "zone":        meta.get("zone", ""),
            "kills":       meta.get("kills", 0),
            "report_code": meta.get("report_code", ""),
            "log_missing": meta.get("log_missing", []),
        },
        # name → identity, so the UI can color names/bars by WoW class
        "roster": {
            p["name"]: {"class": p.get("class", ""), "spec": p.get("spec", ""), "role": p.get("role", "")}
            for p in players
        },
        "consumables":  consum_list,
        "drums":        wcl.get("drums_log", []),
        "avoidableDmg":        avoidable_list,
        "avoidableMechanics":  avoidable_mechanics,
        "friendlyFire":        friendly_fire,
        "mcSaves":             mc_saves,
        "mcLiable":            mc_liable,
        "consumableUsage":     consum_usage,
        # Healthstone accountability (trended): raid-wide stones used + how many of the
        # raiders who died never popped one. {total_used, died_total, died_no_stone}.
        "healthstoneStats":    (lambda hs, dd: {
            "total_used":    sum(hs.values()),
            "died_total":    len(dd),
            "died_no_stone": sum(1 for p in dd if hs.get(p["name"], 0) == 0),
        })({p["name"]: p.get("healthstone", 0) for p in consum_usage},
           [p for p in death_list if p.get("total", 0) > 0]),
        "healing":             healing,
        "tankScorecard":       tank_scorecard,
        "roleSpells":          wcl.get("role_spells", {}),
        "playerSpells":        _ice_player_spells(wcl.get("player_spells", {}), _icons),
        # DPS report = actual damage-dealers only (effective_role Physical/Caster) — tanks and
        # healers who happened to deal damage are filtered out (they have their own surfaces).
        # A hybrid who mostly DPS'd (e.g. prot pally who twisted) stays via effective_role.
        # Expanded past the old top-10 so utility-DPS classes (Balance, etc.) appear with their
        # Class Toolkit cell; the DPS table has a scroll-y cap so length is handled.
        "damage": sorted(
            ({"name": p["name"], "role": p["role"], "effective_role": _eff(p),
              "total_dmg": p.get("total_dmg", 0),
              "active_pct": p.get("active_pct", 0),
              "toolkit": _toolkit_cell(p),
              "uptime_by_fight": p.get("uptime_by_fight", {})}
             for p in players
             if p.get("total_dmg", 0) > 0 and _eff(p) in ("Physical", "Caster")),
            key=lambda x: -x["total_dmg"])[:25],
        # Full roster of damage-dealers (NOT sliced to top 10) — powers the Uptime-by-Fight
        # heatmap so every attacker's per-boss uptime shows, while `damage` above stays a
        # top-10 DPS leaderboard. Same shape as `damage` minus active_pct. effective_role
        # lets the heatmap filter to who actually DPS'd (e.g. a prot pally who twisted).
        "uptimeByFight": sorted(
            ({"name": p["name"], "role": p["role"], "effective_role": _eff(p),
              "total_dmg": p.get("total_dmg", 0),
              "uptime_by_fight": p.get("uptime_by_fight", {})}
             for p in players if p.get("total_dmg", 0) > 0),
            key=lambda x: -x["total_dmg"]),
        # Healer casting-uptime per fight — the Healers & Tanks tab analog of uptimeByFight.
        # Filtered by effective_role so a resto player who mostly DPS'd drops out of the
        # healer chart (and into the DPS one).
        "healerUptimeByFight": sorted(
            ({"name": p["name"], "role": p["role"], "effective_role": _eff(p),
              "uptime_by_fight": p.get("healer_uptime_by_fight", [])}
             for p in players if _eff(p) == "Healer"),
            key=lambda x: x["name"]),
        "deaths":       death_list,
        "casterCrit":   crit_list("Caster"),
        "physicalCrit": crit_list("Physical"),
        "tankCrit":     crit_list("Tank"),
        "healerCrit":   crit_list("Healer"),
        "luckKPI":      luck_tiles,
        "engineering":  sorted(
            [{"name": p["name"], "role": p["role"], "eng": p.get("eng", {}),
              "dmg": p.get("eng_dmg", 0)}   # real sapper/bomb damage from the log
             for p in players if p.get("eng")],
            key=lambda x: -x["dmg"]
        ),
        "interrupts":   sorted(
            [{"name": p["name"], "role": p["role"], "count": p.get("interrupt_count", 0),
              # which casts they actually stopped (combat-log interrupt_list → spell tally)
              "spells": _tally_spells(p.get("interrupt_list"), _icons)}
             for p in players if p.get("interrupt_count", 0) > 0],
            key=lambda x: -x["count"]
        ),
        "boss_times":   wcl.get("boss_times", {}),
        "boss_meta":    wcl.get("boss_meta", {}),
        "healReaction": wcl.get("heal_reaction", {}),
        "debuffCoverage": wcl.get("debuff_coverage", {}),
        "sunderArmor":    wcl.get("sunder_armor", {}),
        # "saving others" — protective/external casts on allies, ranked by saves then total.
        # Positive call-out surface (no shame); empty list ⇒ a quiet week, not a bug.
        "saves": (lambda sv: sorted(
            ({"name": nm, "role": roster_idx.get(nm, {}).get("role", ""),
              "class": roster_idx.get(nm, {}).get("class", ""),
              "save": d.get("save", 0), "dispel": d.get("dispel", 0),
              "utility": d.get("utility", 0), "total": d.get("total", 0),
              "targets": d.get("targets", {})}
             for nm, d in sv.items() if d.get("total", 0) > 0),
            key=lambda x: (-x["save"], -x["total"], x["name"])))(wcl.get("saves", {}) or {}),
        "manaReturns":    wcl.get("mana_returns", {}),
        "loot":           wcl.get("loot_data", {}),   # this-week loot, external ThatsBIS CSV (degrades to {})
        # DPS table data split All/Bosses/Trash — each with its own WCL-style denominator.
        # DPS-only (effective_role Physical/Caster); toolkit cell carried per player.
        "damageBySelection": (lambda ds: {
            "durations": ds.get("durations", {}),
            "players": [
                {"name": p["name"], "role": p["role"], "effective_role": _eff(p),
                 "toolkit": _toolkit_cell(p),
                 # Performance pillar: the player's WCL dps PARSE % (rankPercent, 0–100 vs all logged
                 # parses). None ⇒ report not ranked yet; the cell renders "—".
                 "vs_replacement": _dps_war.get(p["name"]),
                 "all":   ds.get("players", {}).get(p["name"], {}).get("all",   {"total": 0, "active": 0}),
                 "boss":  ds.get("players", {}).get(p["name"], {}).get("boss",  {"total": 0, "active": 0}),
                 "trash": ds.get("players", {}).get(p["name"], {}).get("trash", {"total": 0, "active": 0})}
                for p in players
                if _eff(p) in ("Physical", "Caster")
                and ds.get("players", {}).get(p["name"], {}).get("all", {}).get("total", 0) > 0
            ],
        })(wcl.get("damage_by_sel", {}) or {}),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Inject WEEK_DATA into HTML dashboard
# ══════════════════════════════════════════════════════════════════════════════

def enrich_with_trends(week_data: dict, db_path) -> dict:
    """Attach week-over-week delta_* fields to the per-player items of a MAPPED week_data
    dict, by reading the PREVIOUS week's values from the history DB.

    Must run AFTER build_week_data()/map_to_week_data() and BEFORE write_week() — otherwise
    this week's row overwrites last week's before we can read it. Never throws: any DB error
    (missing file, missing table on an older DB, no prior week) just yields no deltas, and the
    HTML degrades silently (the fmtDelta() helper renders nothing for undefined values).

    db_path: the SAME DB the run will write to (DB_PATH or DB_PATH_TEST), so --test-db reads
    and writes the test DB consistently.
    """
    import sqlite3
    try:
        path = Path(db_path)
        if not path.exists():
            return week_data                      # first ever run — nothing to compare to
        con = sqlite3.connect(path)
        try:
            current = (week_data.get("meta") or {}).get("report_code")
            # Prior week = the most recent week STRICTLY BEFORE this one by CHRONOLOGICAL
            # start_ms (epoch ms). Must be `start_ms < this week's start_ms`, NOT merely "any
            # other report" — build_site.py reuses this to enrich EARLIER weeks for the rolling
            # multi-week view, where "most recent other report" would be a FUTURE week and trend
            # each past week against the latest (e.g. June 1 vs June 8). Each week trends against
            # its OWN predecessor. (start_ms is the canonical sort key; the date string misorders.)
            cur_ms = (week_data.get("meta") or {}).get("start_ms")
            if cur_ms is None:
                r0 = con.execute("SELECT start_ms FROM weeks WHERE report_code=?", (current,)).fetchone()
                cur_ms = r0[0] if r0 else None
            if cur_ms is None:
                # No chronological anchor — fall back to the global-latest other report.
                row = con.execute(
                    "SELECT report_code, date, kills, start_ms FROM weeks WHERE report_code != ? "
                    "ORDER BY start_ms DESC, date DESC LIMIT 1",
                    (current,)
                ).fetchone()
            else:
                # Prefer an anchored prior (start_ms < this week). A legacy row with NULL
                # start_ms (predates the migration) would be silently excluded by a bare
                # `start_ms < ?` — so include it as a LOWER-priority fallback so we trend
                # against it rather than returning nothing. `ORDER BY (start_ms IS NULL)`
                # keeps anchored priors first; the NULL row is chosen only if none exist.
                row = con.execute(
                    "SELECT report_code, date, kills, start_ms FROM weeks "
                    "WHERE report_code != ? AND (start_ms < ? OR start_ms IS NULL) "
                    "ORDER BY (start_ms IS NULL), start_ms DESC LIMIT 1",
                    (current, cur_ms)
                ).fetchone()
            if not row:
                return week_data                  # only one week of history
            prev, prev_date, prev_kills, prev_ms = row[0], row[1], row[2], row[3]
            if prev_ms is None:
                print(f"  ⚠ trends: prior week {prev} has no start_ms (predates migration) — "
                      f"baseline ordering is best-effort; run `reprocess.py --all` to anchor it")

            # Stale-prior-week guard: a delta reads the prior week's row, so if that row predates a
            # schema bump (missing new columns) the delta SILENTLY blanks. Warn loudly instead — the
            # fix is `reprocess.py --all`, which re-stamps every week to the current schema_version.
            try:
                import db_writer as _dbw
                _sv = con.execute("SELECT schema_version FROM weeks WHERE report_code=?", (prev,)).fetchone()
                _sv = _sv[0] if _sv else None
                if _sv is None or _sv < _dbw.SCHEMA_VERSION:
                    print(f"  ⚠ trends: prior week {prev} predates schema v{_dbw.SCHEMA_VERSION} "
                          f"(row is v{_sv}) — some delta_* may be blank; run `reprocess.py --all`")
            except sqlite3.OperationalError:
                pass   # older DB without the schema_version column

            def pv(table, player, col):
                """Previous-week value, or None if absent (also tolerates a missing
                table/column on an older DB that predates this metric)."""
                try:
                    r = con.execute(
                        f"SELECT {col} FROM {table} WHERE report_code=? AND player=?",
                        (prev, player)
                    ).fetchone()
                    return r[0] if r and r[0] is not None else None
                except sqlite3.OperationalError:
                    return None

            # ── damage: trend BOSS DPS (boss damage / boss time), matching the live DPS
            #    table's WCL-style denominator. Compare against the `dps` table's dps (also
            #    boss DPS now). Delta lands on each damageBySelection player (shown in the
            #    Bosses view + the header pill).
            dsel = week_data.get("damageBySelection") or {}
            durs = dsel.get("durations") or {}
            # delta DPS / uptime / total for EACH selection (All/Bosses/Trash), comparing the
            # matching prior-week columns (boss = base dps/total/uptime; all_*/trash_* added).
            SEL_COLS = {"boss":  ("dps", "total", "uptime"),
                        "all":   ("all_dps", "all_total", "all_uptime"),
                        "trash": ("trash_dps", "trash_total", "trash_uptime")}
            for it in (dsel.get("players") or []):
                nm = it.get("name")
                for sel, (c_dps, c_total, c_up) in SEL_COLS.items():
                    d = durs.get(sel) or 0
                    sd = it.get(sel)
                    if not d or sd is None:
                        continue
                    pd, pu, pt = pv("dps", nm, c_dps), pv("dps", nm, c_up), pv("dps", nm, c_total)
                    if pd is not None:
                        sd["delta_dps"] = round(sd.get("total", 0) / d - pd, 1)
                    if pu is not None:
                        sd["delta_uptime"] = round(sd.get("active", 0) / 1000 / d * 100 - pu, 1)
                    if pt is not None:
                        sd["delta_total"] = round(sd.get("total", 0) - pt, 1)

            # ── DPS parse %: delta the percentile vs last week (top-level on each dsel player).
            for it in (dsel.get("players") or []):
                pw = pv("dps", it.get("name"), "war")
                if pw is not None and it.get("vs_replacement"):
                    it["delta_vs_replacement"] = round(it["vs_replacement"] - pw, 2)

            # ── class toolkit: delta the signature metric, but ONLY when the metric KIND
            #    (label) matches last week — comparing Windfury casts to ToW uptime is nonsense.
            for it in (dsel.get("players") or []):
                tk = it.get("toolkit")
                if not tk or tk.get("num") is None:
                    continue
                try:
                    r = con.execute("SELECT label, num FROM class_toolkit WHERE report_code=? AND player=?",
                                    (prev, it.get("name"))).fetchone()
                except sqlite3.OperationalError:
                    r = None
                if r and r[0] == tk.get("label") and r[1] is not None:
                    tk["delta"] = round(tk["num"] - r[1], 1)

            # ── mana returns: delta per provider, per source (matched on player+source).
            for b in (week_data.get("manaReturns") or {}).get("batteries", []):
                for prov in b.get("providers", []):
                    try:
                        r = con.execute(
                            "SELECT mana FROM mana_returns WHERE report_code=? AND player=? AND source=?",
                            (prev, prov["name"], b.get("label"))).fetchone()
                    except sqlite3.OperationalError:
                        r = None
                    if r and r[0] is not None:
                        prov["delta_mana"] = prov.get("mana", 0) - r[0]

            # ── sunder armor: delta total + effective stacks built, per warrior.
            for p in (week_data.get("sunderArmor") or {}).get("players", []):
                try:
                    r = con.execute("SELECT total, effective FROM sunder_armor WHERE report_code=? AND player=?",
                                    (prev, p.get("name"))).fetchone()
                except sqlite3.OperationalError:
                    r = None
                if r:
                    if r[0] is not None:
                        p["delta_total"] = p.get("total", 0) - r[0]
                    if r[1] is not None:
                        p["delta_effective"] = p.get("effective", 0) - r[1]

            # ── healthstones: raid-level deltas (total used + # who died never popping).
            hs = week_data.get("healthstoneStats")
            if hs:
                try:
                    r = con.execute("SELECT hs_used, hs_died_no_stone FROM weeks WHERE report_code=?",
                                    (prev,)).fetchone()
                except sqlite3.OperationalError:
                    r = None
                if r:
                    if r[0] is not None and hs.get("total_used") is not None:
                        hs["delta_used"] = hs["total_used"] - r[0]
                    if r[1] is not None and hs.get("died_no_stone") is not None:
                        hs["delta_no_stone"] = hs["died_no_stone"] - r[1]

            # ── healing: four trended columns (HPS, overheal, activity, mana-efficiency).
            for it in (week_data.get("healing") or []):
                nm = it.get("name")
                for cur_field, db_col, delta_field in (
                    ("eff_hps",      "eff_hps",      "delta_hps"),
                    ("overheal_pct", "overheal_pct", "delta_overheal"),
                    ("activity_pct", "activity_pct", "delta_activity"),
                    ("mana_eff",     "mana_eff",     "delta_hp_per_mana"),
                ):
                    p = pv("healing", nm, db_col)
                    if p is not None:
                        it[delta_field] = round((it.get(cur_field) or 0) - p, 1)

            # ── single-field KPIs: (week key, table, current field, db col, delta field)
            #    (luck is intentionally NOT trended: the `luck_kpi.luck` column is NULL by
            #    design — luck is derived at render time from the multi-week crit baseline,
            #    not persisted — so a week-over-week DB delta is meaningless.)
            for wk_key, table, cur_field, db_col, delta_field in (
                ("avoidableDmg",  "avoidable_dmg",  "dmg",   "dmg",   "delta_dmg"),
                ("deaths",        "deaths",         "total", "total", "delta_deaths"),
                ("drums",         "drums",          "score", "score", "delta_drums"),
                ("saves",         "saves",          "save",  "save",  "delta_save"),
                # tank DTPS (lower better → HTML renders ▼ green via fmtDelta(...,false)).
                # Absent until 2 weeks of the new tank_scorecard table exist (no backfill).
                ("tankScorecard", "tank_scorecard", "dtps",  "dtps",  "delta_dtps"),
            ):
                for it in (week_data.get(wk_key) or []):
                    p = pv(table, it.get("name"), db_col)
                    if p is not None:
                        it[delta_field] = round((it.get(cur_field) or 0) - p, 1)

            # ── tank threat parse % + survivability grade — dedicated (survival is nested under
            #    .score, and the parse-% delta wants its own pass).
            for it in (week_data.get("tankScorecard") or []):
                nm = it.get("name")
                pw = pv("tank_scorecard", nm, "war")
                if pw is not None and it.get("vs_replacement"):
                    it["delta_vs_replacement"] = round(it["vs_replacement"] - pw, 2)
                ps, sv = pv("tank_scorecard", nm, "survival"), it.get("survival")
                if ps is not None and isinstance(sv, dict) and sv.get("score") is not None:
                    sv["delta_score"] = round(sv["score"] - ps, 1)

            # ── prior-week header context (powers the "vs last week" header pills).
            #    Kill-time delta is summed over the bosses cleared in BOTH weeks so it's
            #    fight-count agnostic — comparing raw totals would unfairly reward a week
            #    that simply downed fewer bosses. Negative delta = faster this week.
            prev_bt = {r[0]: r[1] for r in con.execute(
                "SELECT boss, seconds FROM boss_times WHERE report_code=?", (prev,)).fetchall()}
            cur_bt  = week_data.get("boss_times") or {}
            common  = [b for b in cur_bt if b in prev_bt]
            # Per-boss kill-time delta for the Overview tiles (negative = faster this week).
            # Folded in here — same start_ms-ordered prior week as the header pill — rather
            # than a separate ORDER BY date pass (which misorders; see memory).
            for b, meta in (week_data.get("boss_meta") or {}).items():
                if b in prev_bt and meta.get("seconds") is not None:
                    meta["delta_seconds"] = round(meta["seconds"] - prev_bt[b], 1)
            prev_ctx = {"report_code": prev, "date": prev_date, "kills": prev_kills}
            if common:
                prev_ctx["delta_kill_secs"] = round(
                    sum(cur_bt[b] for b in common) - sum(prev_bt[b] for b in common), 1)
                prev_ctx["common_bosses"] = len(common)
            week_data["prev"] = prev_ctx
        finally:
            con.close()
    except Exception as e:
        print(f"  [trends] warning: {e}")
    return week_data


def dump_week_data_cache(mapped: dict) -> None:
    """Persist a mapped WEEK_DATA dict to cache/week_data/<report>.json — the offline-reprocess
    source of record (reprocess.py --all rebuilds the whole DB + trends from these, zero WCL).

    Cache the CLEAN mapped data (call BEFORE enrich_with_trends) so a from-scratch reprocess
    recomputes deltas fresh instead of inheriting stale ones. The mapped dict is already
    json-serializable (inject_into_html json.dumps's it). Never raises — a cache miss must not
    break the run (same discipline as the db_writer write_week warning)."""
    try:
        code = (mapped.get("meta") or {}).get("report_code")
        if not code:
            print("  ⚠ week_data cache: no report_code in meta — skipping")
            return
        WEEK_DATA_CACHE.mkdir(parents=True, exist_ok=True)
        out = WEEK_DATA_CACHE / f"{code}.json"
        out.write_text(json.dumps(mapped, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"  ✓ cached WEEK_DATA → week_data/{out.name}")
    except Exception as e:
        print(f"  ⚠ week_data cache write failed: {e}")


def reingest_loot(mapped: dict, csv_path: str = None) -> dict:
    """Re-attach a week's loot to a MAPPED week_data from a ThatsBIS 'received' CSV, keyed on the
    raid-night date (from meta.start_ms). Loot is normally ingested ONLY by the live pipeline's
    `--loot` step (main()), so the OFFLINE rebuild paths — backfill_snapshots.py (rebuilds a
    snapshot from WCL) and reprocess.py --all (re-renders/re-persists from snapshots) — would
    silently DROP loot from any week they touch unless they call this. Newest loot/*.csv is
    auto-picked. No-op (loot untouched) if there's no CSV or no awards dated that night, so it
    never clobbers existing loot with emptiness on a date the CSV doesn't cover. Never raises."""
    try:
        if csv_path is None:
            csvs = sorted(LOOT_DIR.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
            if not csvs:
                return mapped
            csv_path = str(csvs[0])
        sm = (mapped.get("meta") or {}).get("start_ms")
        if sm is None:
            return mapped
        raid_date = time.strftime("%Y-%m-%d", time.localtime(sm / 1000))
        import loot_parser
        ld = loot_parser.parse_loot(csv_path, raid_date)
        if ld:
            mapped["loot"] = ld
            print(f"  + loot re-ingested: {ld['total']} items to {len(ld['players'])} raiders on {raid_date}")
    except Exception as e:
        print(f"  loot re-ingest warning: {e}")
    return mapped


def inject_into_html(week_data: dict, html_path: Path, mapped: dict = None):
    """Read template.html, inject WEEK_DATA, write to html_path (the gitignored output).

    Always reads from TEMPLATE_FILE so the output is never the source for the next run.
    Substitutes {{DASHBOARD_TITLE}} from the DASHBOARD_TITLE env var (default fallback).
    mapped: an already-mapped (and possibly trend-enriched) WEEK_DATA dict. When given,
    it is injected verbatim instead of re-mapping `week_data` — this preserves delta_*
    fields added by enrich_with_trends(). Omit it and the old behavior is unchanged."""
    src = TEMPLATE_FILE if TEMPLATE_FILE.exists() else html_path
    html = src.read_text(encoding="utf-8")

    # Substitute the dashboard title placeholder
    title = os.environ.get("DASHBOARD_TITLE") or DEFAULT_TITLE
    html = html.replace("{{DASHBOARD_TITLE}}", title)

    if mapped is None:
        mapped = map_to_week_data(week_data)
    new_json = json.dumps(mapped, indent=2, ensure_ascii=False)

    # Find the start of const WEEK_DATA = {
    marker = "const WEEK_DATA ="
    start  = html.find(marker)
    if start == -1:
        print("  ERROR: could not find 'const WEEK_DATA =' in HTML — skipping injection")
        return

    # Walk forward from { counting brackets to find the matching }
    brace_start = html.index("{", start)
    depth, i = 0, brace_start
    in_str, escape = False, False
    while i < len(html):
        ch = html[i]
        if escape:
            escape = False
        elif ch == "\\" and in_str:
            escape = True
        elif ch == '"' and not escape:
            in_str = not in_str
        elif not in_str:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    break
        i += 1

    # Replace from marker through the closing }; (plus optional semicolon)
    end = i + 1
    if html[end:end+1] == ";":
        end += 1

    updated = html[:start] + f"const WEEK_DATA = {new_json};" + html[end:]
    html_path.write_text(updated, encoding="utf-8")
    print(f"\n✅ Dashboard updated: {html_path}")









def merge_log_into_wcl(wcl_data: dict, log_data: dict) -> dict:
    """Overlay combat log stats onto WCL API data.

    The WCL API only populates combatantinfo crit fields for DPS roles, so tanks
    and healers come back with gear_crit_rating=0. The local combat log's
    COMBATANT_INFO carries per-school crit for everyone, so we use it to backfill
    any player whose WCL gear crit is still 0 — without disturbing the DPS values
    WCL already got right.
    """
    log_players = log_data.get("players", {})
    for p in wcl_data.get("players", []):
        log = log_players.get(p["name"], {})
        if not log:
            continue

        # Pick the role-appropriate crit school from the log — same logic as the
        # WCL combatantinfo path (spell for casters/healers, melee otherwise).
        role = p.get("role", "")
        if role in ("Caster", "Healer"):
            log_gear_crit = log.get("_crit_spell", 0)
        elif role in ("Physical", "Tank"):
            log_gear_crit = log.get("_crit_melee", 0)
        else:
            log_gear_crit = max(log.get("_crit_melee", 0), log.get("_crit_spell", 0))

        # Backfill expected crit only where WCL didn't supply gear crit (gap-fill).
        if log_gear_crit > 0 and p.get("gear_crit_rating", 0) == 0:
            p["gear_crit_rating"] = log_gear_crit
            p["expected_crit"]    = expected_crit(
                p.get("class", ""), p.get("spec", ""), log_gear_crit,
                log.get("_agility", 0), p.get("intellect", 0))
            p["luck_delta"]       = round(
                p.get("actual_crit", 0) - p["expected_crit"], 2)

        # Other log-sourced fields (log is authoritative for these).
        # NB: deaths come from the WCL table now (feign-free) — do NOT overwrite from the log.
        p["interrupt_count"] = log.get("interrupt_count", 0)
        p["interrupt_list"]  = log.get("interrupt_list", [])
        p["eng"]             = log.get("eng", {})
        p["eng_dmg"]         = log.get("eng_dmg", 0)
        p["avoidable_dmg"]     = log.get("avoidable_dmg", 0)
        p["avoidable_sources"] = log.get("avoidable_sources", {})
        p["avoidable_hits"]    = log.get("avoidable_hits", {})

    # Overwrite drums with log data (more reliable than WCL API for this)
    if log_data.get("drums"):
        wcl_data["drums_log"] = log_data["drums"]

    # Hall-of-Shame extras from the log (top-level, not per-player)
    wcl_data["friendly_fire"]       = log_data.get("friendly_fire", [])
    wcl_data["avoidable_mech_boss"] = log_data.get("avoidable_mech_boss", {})
    wcl_data["log_consumables"]     = log_data.get("consumables", {})
    wcl_data["mc_saves"]            = log_data.get("mc_saves", [])
    wcl_data["mc_liable"]           = log_data.get("mc_liable", [])
    wcl_data["consum_use"]          = log_data.get("consum_use", {})
    wcl_data["consum_label"]        = log_data.get("consum_label", {})

    # Melee auto-attack swings → into the spell-usage breakdown. The WCL Casts table omits
    # auto-attacks, so melee classes were missing their single biggest "action". Combat-log
    # only (degrades silently without a log). Inject a "Melee" row per player + per role.
    swings = log_data.get("melee_swings", {})
    if swings:
        role_of = {p["name"]: p.get("role", "") for p in wcl_data.get("players", [])}
        # force the conventional white-melee icon (the WCL layer maps "Melee" to a specific
        # weapon icon, which reads oddly as a generic auto-attack row)
        wcl_data.setdefault("ability_icons", {})["Melee"] = "ability_meleedamage"
        pspells = wcl_data.get("player_spells", {})
        rspells = wcl_data.get("role_spells", {})
        role_melee = defaultdict(lambda: {"casts": 0, "players": set()})
        for nm, sw in swings.items():
            if sw <= 0:
                continue
            role = role_of.get(nm)
            if role not in ("Physical", "Tank", "Caster", "Healer"):
                continue
            plist = pspells.setdefault(role, [])
            entry = next((p for p in plist if p["name"] == nm), None)
            if entry is None:
                entry = {"name": nm, "role": role, "total": 0, "abilities": []}
                plist.append(entry)
            abils = [a for a in entry.get("abilities", []) if a.get("ability") != "Melee"]
            abils.append({"ability": "Melee", "casts": sw})
            entry["abilities"] = sorted(abils, key=lambda x: -x["casts"])[:20]
            entry["total"] = entry.get("total", 0) + sw
            role_melee[role]["casts"]  += sw
            role_melee[role]["players"].add(nm)
        for role, mm in role_melee.items():
            rlist = [a for a in rspells.get(role, []) if a.get("ability") != "Melee"]
            rlist.append({"ability": "Melee", "casts": mm["casts"], "players": len(mm["players"])})
            rspells[role] = sorted(rlist, key=lambda x: -x["casts"])[:12]
        for role in pspells:
            pspells[role].sort(key=lambda x: -x.get("total", 0))
        wcl_data["player_spells"] = pspells
        wcl_data["role_spells"]   = rspells

    return wcl_data


def print_summary(week_data: dict):
    print("\n" + "="*60)
    print(f"REPORT: {week_data['meta']['report_code']}  |  {week_data['meta']['date']}")
    print(f"Zone: {week_data['meta']['zone']}  |  Kills: {week_data['meta']['kills']}")
    print("\n─── Luck KPI (actual crit − expected crit) ───")
    for p in sorted(week_data["players"], key=lambda x: -x["luck_delta"]):
        bar = "▲" if p["luck_delta"] > 0 else ("▼" if p["luck_delta"] < 0 else "─")
        print(f"  {bar} {p['name']:18s}  expected={p['expected_crit']:5.1f}%  "
              f"actual={p['actual_crit']:5.1f}%  delta={p['luck_delta']:+.1f}%")
    print("\n─── Deaths ───")
    for d in week_data["deaths"]:
        print(f"  {d['name']:18s}  {d['deaths']} deaths")


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="WCL → Raid KPI Dashboard updater")
    parser.add_argument("report_code", help="WCL report code (from URL)")
    parser.add_argument("--client-id",     default=os.getenv("WCL_CLIENT_ID"),     help="WCL V2 Client ID")
    parser.add_argument("--client-secret", default=os.getenv("WCL_CLIENT_SECRET"), help="WCL V2 Client Secret")
    parser.add_argument("--out", default=str(DASH_FILE),
                        help="Path to HTML dashboard to update")
    # Auto-find most recent .txt in logs/ if not specified
    _auto_log = None
    _log_files = sorted(LOGS_DIR.glob("WoWCombatLog*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
    if _log_files:
        _auto_log = str(_log_files[0])
    parser.add_argument("--log", default=_auto_log,
                        help="Path to WoWCombatLog.txt (default: newest WoWCombatLog*.txt in logs/)")
    # Auto-find newest ThatsBIS loot CSV in loot/ — external/optional; absent = Loot card hides.
    _auto_loot = None
    _loot_files = sorted(LOOT_DIR.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True) \
        if LOOT_DIR.exists() else []
    if _loot_files:
        _auto_loot = str(_loot_files[0])
    parser.add_argument("--loot", default=_auto_loot,
                        help="Path to ThatsBIS received-loot CSV (default: newest *.csv in loot/)")
    parser.add_argument("--dry-run",  action="store_true", help="Print JSON only, don't write HTML")
    parser.add_argument("--test-db",  action="store_true", help="Write to raid_history_test.db instead of prod")
    args = parser.parse_args()

    if not args.client_id or not args.client_secret:
        print("ERROR: Set WCL_CLIENT_ID and WCL_CLIENT_SECRET env vars, or pass --client-id / --client-secret")
        sys.exit(1)

    print(f"Authenticating with WCL...")
    token = get_token(args.client_id, args.client_secret)
    print("  ✓ Token obtained")

    # Fetch the report's kill list once, up front — used to (a) scope the combat log to
    # THIS report's bosses (ignore off-report DST clears like Gruul/HKM) and (b) feed
    # build_week_data so it doesn't re-fetch.
    rep0 = gql(token, Q_REPORT, {"code": args.report_code})["reportData"]["report"]
    # Per-player crit baseline from history (gold-standard 'luck' = this week vs your own
    # multi-week average). Read from the SAME db the run will write to, excluding this report.
    from db_writer import crit_history, DB_PATH, DB_PATH_TEST
    db_path = DB_PATH_TEST if args.test_db else DB_PATH
    crit_hist = crit_history(db_path, exclude_report=args.report_code)

    # Build → finalize → commit through the shared week_build spine — the SAME path backfill and
    # reprocess use, so the live run can't drift from them (the loot-drop / role / order bug class).
    # from_wcl parses the log (scoped to rep0's bosses) and feeds build_week_data → no double fetch.
    import week_build as wb
    mapped, week_data, log_data = wb.from_wcl(
        args.report_code, token, log_path=args.log, report=rep0,
        history=crit_hist)

    # finalize_week owns the ONE loot-ingestion point (replacing a raw-dict loot_data block) plus
    # the contract check. Pass the resolved --loot path so an explicit CSV isn't overridden by mtime.
    wb.finalize_week(mapped, has_log=log_data is not None, loot_csv=args.loot, label=args.report_code)

    print_summary(week_data)

    if args.dry_run:
        print("\n─── WCL_AUTO_DATA JSON ───")
        print(json.dumps(week_data, indent=2, ensure_ascii=False))
    else:
        # commit: dump (clean, test-gated) → enrich (strictly before write) → write_week. Then render
        # to --out explicitly (commit_week renders to DASH_FILE; main honors a custom --out path).
        wb.commit_week(mapped, db_path, is_test=args.test_db, dump=True, render=False)
        inject_into_html(week_data, Path(args.out), mapped=mapped)
        print(f"\nRun next time with:")
        print(f"  python wcl_auto_dashboard.py {args.report_code} --out \"{args.out}\"")


if __name__ == "__main__":
    main()
