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

# ── WCL endpoints ─────────────────────────────────────────────────────────────
WCL_TOKEN_URL = "https://fresh.warcraftlogs.com/oauth/token"
WCL_API_URL   = "https://www.warcraftlogs.com/api/v2/client"

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
    "Protection Warrior": 1.0,
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

# ── Avoidable damage ability map (for context in UI) ─────────────────────────
AVOIDABLE_ABILITIES = [
    "Whirlwind", "Cleave", "Shatter", "Scalding Water", "Cave In",
    "Burn", "Flame Strike", "Mortal Cleave", "Rain of Fire",
    "Flame Patch", "Falling", "Arcane Burst", "Arcane Orb",
    "Toxic Spores", "Spout", "Nether Vapor",
]

# Legend icon overrides — WoW reuses assets, so a few mechanics resolve to a
# misleading icon (e.g. Lurker's "Whirl" → an engineering wrench). The real icon
# from WCL is used for everything else; these are intentional clarity swaps.
ICON_OVERRIDES = {
    "Whirlwind":  "ability_whirlwind",     # Leotheras — WCL returns ability_gouge
    "Whirl":      "spell_nature_cyclone",  # Lurker Below — WCL returns trade_engineering
    "Ember Blast": "spell_fire_fireball",  # Al'ar death — WCL returns trade_engineering
}

# ── Friendly fire ─────────────────────────────────────────────────────────────
# FF is player→player damage, split into categories by WHY it happened:
#   mechanic    — clumping / didn't-spread (your positioning)
#   engineering — sapper splash hit teammates (the engineer's fault)
#   mc          — dealt while Mind Controlled (NOT your fault; the boss made you)
# Self-damage (Seal of the Martyr, Dark Rune) is excluded via the src≠dst check.
FF_MECHANICS = {
    "Wrath of the Astromancer",  # Solarian — marked player explodes; run out
    "Static Charge",             # arcs to nearby players; spread out
    "Shatter",                   # Gruul — proximity clumping damage
    "Conflagration",             # Al'ar — fire spreads to others
}
# Engineering explosives that splash onto the raid (confirmed player→player in logs).
FF_ENGINEERING = {"Goblin Sapper Charge", "Super Sapper Charge"}
# Charm/Mind-Control aura names (Kael'thas uses "Mind Control"). Matched by NAME for
# the mc_count/controller label. Trash charms use other names, so categorization also
# falls back to "damaged a teammate with a normal ability = charmed" (see below).
MC_AURAS = {"Mind Control", "Dominate Mind", "Possess", "Charm",
            "Domination"}  # Star Scryer (TK trash) charm — the classic "MC pack"
# Retaliation/reflect abilities — these hit whoever attacks the holder, so a player→
# player hit here is reflect, NOT friendly fire. Exclude them.
FF_REFLECT = {"Holy Shield", "Blessing of Sanctuary", "Thorns", "Retribution Aura",
              "Shield Spike", "Lightning Shield", "Fire Shield"}
FF_MIN_DMG = 10000   # mechanic-clumping floor; MC and sapper incidents always show

# ── Mind Control accountability ───────────────────────────────────────────────
# When a teammate is Mind Controlled, blame flips off the victim and onto the raid:
#   LIABILITY — you pressed an INTENTIONAL AoE button that landed on the controlled
#               ally. Single-target overlap doesn't count; pressing Hurricane does.
#   SAVE      — you CC'd the controlled ally (Cyclone et al.), neutralizing them
#               harmlessly so they can't wreck the raid and can't be blown up.
# Both are derived from the same per-player MC aura windows (mc_now).
AOE_ABILITIES = {
    "Chain Lightning", "Seed of Corruption", "Cleave", "Whirlwind", "Hurricane",
    "Arcane Explosion", "Blizzard", "Flamestrike", "Blast Wave", "Rain of Fire",
    "Hellfire", "Shadowfury", "Magma Totem", "Fire Nova", "Thunder Clap",
    "Holy Nova", "Mind Sear", "Consecration", "Volley", "Multi-Shot", "Explosive Trap",
}
# CC that mechanically lands on a charmed *player* (humanoid). Cyclone is the marquee.
CC_ABILITIES = {
    "Cyclone", "Polymorph", "Repentance", "Fear", "Psychic Scream",
    "Intimidating Shout", "Seduction",
}

# ── Consumables ───────────────────────────────────────────────────────────────
# Detected per player from buff-aura events in the combat log. Flask = the gold
# standard (any "Flask of …"); two battle/guardian elixirs ≈ one flask; "Well Fed"
# = a food buff. Elixir buff EFFECT names (as they appear in logs), curated from
# real Buffs-table data — extend as new ones show up.
FOOD_BUFF = "Well Fed"
ELIXIR_BUFFS = {
    "Mighty Agility", "Spellpower Elixir", "Healing Power", "Greater Versatility",
    "Elixir of Draenic Wisdom", "Onslaught Elixir", "Adept's Elixir", "Fel Strength Elixir",
    "Elixir of Major Strength", "Elixir of Major Agility", "Elixir of Major Firepower",
    "Elixir of Mastery", "Elixir of Major Defense", "Elixir of Major Fortitude",
    "Elixir of Major Mageblood", "Elixir of the Mongoose", "Elixir of Empowerment",
    "Elixir of Ironskin", "Elixir of Major Defense",
}

# Guardian (defensive/utility) elixirs — everything else in ELIXIR_BUFFS is a battle elixir.
# Used by the Raid-Prep score: flask = both slots (+4); else battle +2 / guardian +2.
GUARDIAN_ELIXIRS = {
    "Elixir of Draenic Wisdom", "Elixir of Major Defense", "Elixir of Major Fortitude",
    "Elixir of Major Mageblood", "Elixir of Empowerment", "Elixir of Ironskin",
    "Elixir of Mastery", "Earthen Elixir",
}

# Consumable usage — surfaced as an informational call-out (who's actually popping their
# cooldowns), NOT a compliance threshold. Detected from SPELL_CAST_SUCCESS by name.
def _consumable_category(spell: str) -> str:
    if spell.startswith("Create "):           return ""        # warlock making stones, not using
    if "Healthstone" in spell:                 return "healthstone"
    if spell in ("Dark Rune", "Demonic Rune"): return "rune"
    if "Flame Cap" in spell:                   return "flamecap"
    if "Nightmare Seed" in spell:              return "nightmare_seed"
    if "Potion" in spell:                      return "potion"
    if spell.startswith("Scroll of"):          return "scroll"
    return ""

# Combat potions log only their EFFECT buff, never a cast named "… Potion" — so the cast-name
# path above misses them. Only UNAMBIGUOUS potion-exclusive buff names belong here: "Haste"
# was excluded because procs/trinkets/drums share that name and over-count it ~10×.
POTION_BUFFS = {
    "Destruction",       # Destruction Potion (+120 spell dmg)
    "Insane Strength",   # Insane Strength Potion (+120 str, −crit)
    "Fel Regeneration",  # Fel Regeneration Potion
}
# Combat-pot effect buff → display name (for the compliance grid's Combat Pot cell).
POTION_NAME = {
    "Destruction": "Destruction Potion",
    "Insane Strength": "Insane Strength Potion",
    "Fel Regeneration": "Fel Regeneration Potion",
}

# Approx base mana cost of common TBC heal spells (max rank, pre-talent). Used to
# ESTIMATE mana spent on healing = casts × cost, for a healing-per-mana efficiency.
HEAL_MANA_COST = {
    "Flash Heal": 380, "Greater Heal": 825, "Renew": 430, "Circle of Healing": 450,
    "Prayer of Mending": 390, "Prayer of Healing": 1030, "Binding Heal": 690, "Heal": 305,
    "Chain Heal": 540, "Healing Wave": 620, "Lesser Healing Wave": 290, "Earth Shield": 460,
    "Rejuvenation": 415, "Regrowth": 675, "Lifebloom": 220, "Swiftmend": 340, "Healing Touch": 740,
    "Holy Light": 840, "Flash of Light": 180, "Holy Shock": 525,
}

# ── Local item crit cache (persisted across runs) ─────────────────────────────
ROOT_DIR   = Path(__file__).parent.parent  # Gaming/
CACHE_FILE = ROOT_DIR / "cache" / "item_crit_cache.json"
LOGS_DIR   = ROOT_DIR / "logs"
DASH_FILE  = ROOT_DIR / "dashboard" / "raid_kpi_dashboard.html"

# Healer "replacement-level" cohort cache (same-spec ranked parses per boss). The
# heavy rankings fetch is amortized here — weekly runs read it; refresh ~monthly.
BASELINE_CACHE = ROOT_DIR / "cache" / "healer_baseline.json"
BASELINE_TTL   = 30 * 86400   # 30 days

def load_cache() -> dict:
    if CACHE_FILE.exists():
        return json.loads(CACHE_FILE.read_text())
    return {}

def save_cache(cache: dict):
    CACHE_FILE.write_text(json.dumps(cache, indent=2))


# ══════════════════════════════════════════════════════════════════════════════
# WCL API helpers
# ══════════════════════════════════════════════════════════════════════════════

def get_token(client_id: str, client_secret: str) -> str:
    resp = _req.post(
        WCL_TOKEN_URL,
        data={"grant_type": "client_credentials"},
        auth=(client_id, client_secret),
        timeout=15
    )
    if not resp.ok:
        print(f"  Auth failed [{resp.status_code}]: {resp.text}")
        resp.raise_for_status()
    return resp.json()["access_token"]


def gql(token: str, query: str, variables: dict = None, retries: int = 3) -> dict:
    for attempt in range(retries):
        try:
            resp = _req.post(
                WCL_API_URL,
                json={"query": query, "variables": variables or {}},
                headers={"Authorization": f"Bearer {token}"},
                timeout=30
            )
            resp.raise_for_status()
            data = resp.json()
            if "errors" in data:
                raise RuntimeError(f"GraphQL errors: {data['errors']}")
            return data["data"]
        except Exception as e:
            if attempt == retries - 1:
                raise
            print(f"  Retry {attempt+1}/{retries} after error: {e}")
            time.sleep(2 ** attempt)


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
        if entries:
            # Debug first entry to reveal field names
            sample = {k: v for k, v in entries[0].items() if not isinstance(v, list)}
            print(f"  DEBUG dmg entry keys: {list(sample.keys())}")
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


def fetch_actual_crit(token: str, report_code: str, fights: list) -> dict[str, dict]:
    """
    Page through damage events across all kill fights and count hits vs crits per player.
    hitType: 1=normal, 2=crit, 4=absorb, 8=blocked, 16=glancing, 32=dodge, 64=parry
    Returns { sourceID: { name, hits, crits } }
    """
    if not fights:
        return {}

    start = float(min(f["startTime"] for f in fights))
    end   = float(max(f["endTime"]   for f in fights))
    fight_ids = [f["id"] for f in fights]

    counts: dict[int, dict] = {}   # { sourceID: {name, hits, crits} }
    next_ts = start
    pages = 0

    print(f"  Fetching damage events for crit counting (this may take a moment)...")
    while next_ts is not None and pages < 20:   # cap at 20 pages (~200k events)
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
            elif hit_type == 1:
                counts[sid]["hits"]  += 1

        if not next_ts:
            break

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


def fetch_healing_by_fight(token: str, report_code: str, kills: list, batch: int = 5) -> dict:
    """Per-fight Healing tables (needed for per-boss vs-replacement + heal-fight scoping).
    Uses GraphQL field ALIASING to fetch `batch` fights per HTTP request instead of one
    request each — collapses ~10 round-trips into ~2. Shared by the scoped healer metrics
    and compute_healer_war."""
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


def fetch_uptime_by_fight(token: str, report_code: str, kills: list, batch: int = 5) -> dict:
    """Per-fight active-time % per player, so a structurally-low fight (submerge/phase, e.g.
    Vashj P2 for casters, Lurker dives) is visible instead of silently dragging the raid-wide
    number. Aliased per-fight DamageDone. Returns { player: {boss: uptime_pct} }."""
    out = defaultdict(dict)
    fid_boss = {f["id"]: f["name"] for f in kills}
    fid_dur  = {f["id"]: (f["endTime"] - f["startTime"]) / 1000.0 for f in kills}
    for i in range(0, len(kills), batch):
        chunk = kills[i:i + batch]
        aliases = "\n".join(
            f'f{f["id"]}: table(dataType: DamageDone, fightIDs:[{int(f["id"])}])' for f in chunk)
        Q = f"query($c:String!){{reportData{{report(code:$c){{ {aliases} }}}}}}"
        try:
            rep = gql(token, Q, {"c": report_code})["reportData"]["report"]
        except Exception as ex:
            print(f"  Warning: uptime-by-fight batch failed: {ex}")
            continue
        for f in chunk:
            t = rep.get(f'f{f["id"]}')
            if isinstance(t, str):
                t = json.loads(t)
            dur = fid_dur.get(f["id"], 0) or 1
            for e in (t or {}).get("data", {}).get("entries", []):
                at = e.get("activeTime", 0) / 1000.0
                out[e.get("name")][fid_boss[f["id"]]] = round(at / dur * 100, 1)
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


def fetch_tank_metrics(token: str, report_code: str, kills: list, fight_roles: dict,
                       fight_durs: dict, heal_by_fight: dict) -> dict:
    """Tank survivability SCOPED to the fights each player TANKED (prot/ret-swap aware).
    DTPS (damage taken/sec) and healing-received/sec over their tank-fights, plus the
    fight count. Healing received is reused from the per-fight Healing tables (targets).
    Avoidance% (dodge/parry/block/miss) is a planned fast-follow — needs combat-log swing
    parsing scoped to tank windows, not in the WCL DamageTaken table."""
    tanks = {n for n, fr in fight_roles.items() if fr.get("Tank")}
    if not tanks:
        return {}
    agg = defaultdict(lambda: {"taken": 0, "dur": 0.0, "hrecv": 0, "fights": 0})
    QD = """query($c:String!,$f:Int!){reportData{report(code:$c){
        table(dataType: DamageTaken, fightIDs:[$f])}}}"""
    for f in kills:
        fid = f["id"]
        dur = fight_durs.get(fid, 0)
        fight_tanks = {n for n in tanks if fid in fight_roles.get(n, {}).get("Tank", [])}
        if not fight_tanks:
            continue
        try:
            t = gql(token, QD, {"c": report_code, "f": fid})["reportData"]["report"]["table"]
            if isinstance(t, str):
                t = json.loads(t)
        except Exception:
            t = {}
        for e in t.get("data", {}).get("entries", []):
            nm = e.get("name")
            if nm in fight_tanks:
                a = agg[nm]
                a["taken"]  += e.get("total", 0)
                a["dur"]    += dur
                a["fights"] += 1
        # healing received this fight (sum across every healer's targets that are tanks)
        for e in heal_by_fight.get(fid, []):
            for x in (e.get("targets") or []):
                if x.get("name") in fight_tanks:
                    agg[x["name"]]["hrecv"] += x.get("total", 0)
    out = {}
    for nm, a in agg.items():
        if a["dur"] <= 0:
            continue
        out[nm] = {
            "dtps":          round(a["taken"] / a["dur"]),
            "taken":         a["taken"],
            "hps_recv":      round(a["hrecv"] / a["dur"]),
            "fights_tanked": a["fights"],
        }
    return out


def compute_tank_metrics_from_log(log_data: dict, fight_roles: dict, fight_durs: dict,
                                  kills: list) -> dict:
    """Tank survivability from COMBAT-LOG tallies (zero API cost) — damage taken and
    healing received are already scoped per fight in parse_combat_log, so a prot/ret
    swapper's ret-fight damage never pollutes his tank DTPS. Boss-name keyed → fid."""
    dmg_taken = log_data.get("fight_dmg_taken", {})   # [boss][player]
    heal_recv = log_data.get("fight_heal_recv", {})   # [boss][player]
    fid_to_boss = {f["id"]: f["name"] for f in kills}
    tanks = {n for n, fr in fight_roles.items() if fr.get("Tank")}
    out = {}
    for nm in tanks:
        tank_fids = fight_roles[nm].get("Tank", [])
        dur = sum(fight_durs.get(fid, 0) for fid in tank_fids)
        if dur <= 0:
            continue
        taken = hrecv = 0
        for boss in {fid_to_boss.get(fid) for fid in tank_fids if fid_to_boss.get(fid)}:
            taken += dmg_taken.get(boss, {}).get(nm, 0)
            hrecv += heal_recv.get(boss, {}).get(nm, 0)
        out[nm] = {
            "dtps":          round(taken / dur),
            "taken":         taken,
            "hps_recv":      round(hrecv / dur),
            "fights_tanked": len(tank_fids),
        }
    return out


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
                "abilities": sorted(
                    [{"ability": a, "casts": c} for a, c in abils.items()],
                    key=lambda x: -x["casts"])[:8],
            })
        for role in by_player:
            by_player[role].sort(key=lambda x: -x["total"])
        return {"roles": roles, "players": dict(by_player)}
    except Exception as ex:
        print(f"  Warning: spell-usage fetch failed: {ex}")
        return {"roles": {}, "players": {}}


def _healer_count(token, code, fight_id):
    """How many healers a ranked parse's raid fielded — for comp-matching the cohort.
    One playerDetails query per ranking; only called during a cohort (re)fetch, so the
    monthly baseline cache amortizes it. Returns None on failure (caller degrades to
    duration-only matching)."""
    Q = """query($c:String!,$f:Int!){reportData{report(code:$c){
        playerDetails(fightIDs:[$f], killType:Kills)}}}"""
    try:
        pd = gql(token, Q, {"c": code, "f": fight_id})["reportData"]["report"]["playerDetails"]
        if isinstance(pd, str):
            pd = json.loads(pd)
        healers = pd.get("data", {}).get("playerDetails", {}).get("healers", [])
        return len(healers) or None
    except Exception:
        return None


# Bump when the cached sample shape changes so old entries are refetched lazily.
BASELINE_VER = 2   # v2 adds per-sample healer count for comp-matching


def _cohort_samples(token, cache, enc_id, cls, spec, refresh, now):
    """Cached same-spec ranked-parse samples [{duration, hps, healers}] for one
    (boss, class, spec). Fetches WCL rankings only when missing/stale/old-schema — the
    cache amortizes the heavy call (rankings + one healer-count query per sample)."""
    key = f"{enc_id}:{cls}:{spec}"
    ent = cache.get(key)
    if ent and not refresh and ent.get("ver") == BASELINE_VER \
            and (now - ent.get("sampled_at", 0)) < BASELINE_TTL:
        return ent["samples"]
    Q = """query($e:Int!,$cl:String!,$sp:String!){worldData{encounter(id:$e){
        characterRankings(className:$cl, specName:$sp, metric:hps)}}}"""
    try:
        cr = gql(token, Q, {"e": enc_id, "cl": cls, "sp": spec})["worldData"]["encounter"]["characterRankings"]
        if isinstance(cr, str):
            cr = json.loads(cr)
        # Cap at 50 (down from 100) — each sample now costs a healer-count query.
        raw = [r for r in cr.get("rankings", []) if r.get("size") == 25][:50]
        samples = []
        for r in raw:
            rep = r.get("report") or {}
            code, fid = rep.get("code"), rep.get("fightID")
            healers = _healer_count(token, code, fid) if code and fid else None
            samples.append({"duration": (r.get("duration", 0) or 0) / 1000.0,
                            "hps": r.get("amount", 0), "healers": healers})
        cache[key] = {"sampled_at": now, "ver": BASELINE_VER, "samples": samples}
        return samples
    except Exception as ex:
        print(f"  Warning: cohort fetch {key} failed: {ex}")
        cache[key] = {"sampled_at": now, "ver": BASELINE_VER, "samples": []}
        return []


def compute_healer_war(token, heal_by_fight, kills, players, fight_roles, refresh=False):
    """Each healer's effective HPS per boss vs the median of same-spec ranked parses on
    that boss (within ±15s duration) = 'replacement level'. Averaged across bosses gives
    a 'vs replacement' ratio (1.15 = 15% above the cohort median). Cohort cached monthly.
    Only fights where the player was actually a HEALER count (spec-swap aware), and only
    cohort parses with a similar healer count (comp-matched). Cohort = *logged* parses
    (skews skilled), so ~1.0 is solid, not average."""
    import statistics
    healer_spec = {p["name"]: (p.get("type") or p.get("class"), p.get("spec"))
                   for p in players if p.get("role") == "Healer" and p.get("spec")}
    if not healer_spec:
        return {}
    our_healers = sum(1 for p in players if p.get("role") == "Healer")
    cache = {}
    if BASELINE_CACHE.exists():
        try: cache = json.loads(BASELINE_CACHE.read_text())
        except Exception: cache = {}
    now = time.time()
    ratios = defaultdict(list)
    for f in kills:
        enc = f.get("encounterID")
        if not enc:
            continue
        dur_s = (f["endTime"] - f["startTime"]) / 1000.0
        for e in heal_by_fight.get(f["id"], []):
            nm = e.get("name")
            if nm not in healer_spec:
                continue
            # spec-swap gate: only count fights this player actually healed
            if f["id"] not in fight_roles.get(nm, {}).get("Healer", []):
                continue
            hps = (e.get("total", 0) / dur_s) if dur_s else 0
            if hps <= 0:
                continue
            cls, spec = healer_spec[nm]
            samp = _cohort_samples(token, cache, enc, cls, spec, refresh, now)
            # Comp-match: same fight length (±15s) AND similar healer count (±1), so a
            # 5-heal kill is benchmarked against ~5-heal kills, not HPS-inflated 3-heal
            # parses. Fall back to duration-only if the comp filter is too sparse (<3).
            comp = [s["hps"] for s in samp
                    if abs(s["duration"] - dur_s) <= 15
                    and (s.get("healers") is None or abs(s["healers"] - our_healers) <= 1)]
            if len(comp) < 3:
                comp = [s["hps"] for s in samp if abs(s["duration"] - dur_s) <= 15]
            if len(comp) >= 3:
                med = statistics.median(comp)
                if med > 0:
                    ratios[nm].append(hps / med)
    try:
        BASELINE_CACHE.write_text(json.dumps(cache, indent=2))
    except Exception:
        pass
    return {nm: round(sum(r) / len(r), 2) for nm, r in ratios.items() if r}


def fetch_ability_icons(token: str, report_code: str, fight_ids: list) -> dict:
    """Map ability name → real WCL icon slug (no .jpg) from the DamageTaken table.
    This is the authoritative icon for whatever actually hit the raid, so it
    sidesteps the wrong-spell-ID / reused-asset problem."""
    if not fight_ids:
        return {}
    Q = """query($c:String!,$f:[Int]){reportData{report(code:$c){
        table(dataType: DamageTaken, fightIDs:$f, hostilityType:Friendlies)}}}"""
    try:
        t = gql(token, Q, {"c": report_code, "f": fight_ids})["reportData"]["report"]["table"]
        if isinstance(t, str):
            t = json.loads(t)
        icons = {}
        for e in t.get("data", {}).get("entries", []):
            for ab in (e.get("abilities") or []):
                nm = ab.get("name")
                ic = ab.get("icon")
                if nm and ic and nm not in icons:
                    icons[nm] = ic.replace(".jpg", "")
        return icons
    except Exception as e:
        print(f"  Warning: ability-icon fetch failed: {e}")
        return {}


def fetch_deaths_split(token: str, report_code: str):
    """Curated deaths from the WCL Deaths table (WCL excludes Hunter Feign Death,
    unlike raw combat-log UNIT_DIED). Split boss vs trash by each death's fight —
    boss fights carry an encounterID, trash fights don't.
    Returns (boss_deaths, trash_deaths, recaps): counts per player, plus a per-player
    list of killing blows {boss, killer, amount, overkill} for the death drill-down."""
    Qf = """query($c:String!){reportData{report(code:$c){fights{ id name encounterID }}}}"""
    fights = gql(token, Qf, {"c": report_code})["reportData"]["report"]["fights"]
    fid_is_boss = {f["id"]: bool(f["encounterID"]) for f in fights}
    fid_name    = {f["id"]: f.get("name", "") for f in fights}

    # id → name maps to label the per-death recap timeline (abilities + ALL actors,
    # including NPCs so boss-ability sources resolve). One cheap query; failure is non-fatal.
    abil_name, actor_name = {}, {}
    try:
        md = gql(token, """query($c:String!){reportData{report(code:$c){masterData{
            abilities{ gameID name } actors{ id name } }}}}""",
                 {"c": report_code})["reportData"]["report"]["masterData"]
        abil_name  = {a["gameID"]: a.get("name", "") for a in (md.get("abilities") or []) if a.get("gameID")}
        actor_name = {a["id"]: a.get("name", "")     for a in (md.get("actors")    or []) if a.get("id")}
    except Exception as ex:
        print(f"  Warning: death-recap name map fetch failed: {ex}")

    Qd = """query($c:String!,$f:[Int]){reportData{report(code:$c){
        table(dataType: Deaths, fightIDs:$f)}}}"""
    t = gql(token, Qd, {"c": report_code, "f": list(fid_is_boss)})["reportData"]["report"]["table"]
    if isinstance(t, str):
        t = json.loads(t)
    boss, trash = {}, {}
    recaps = defaultdict(list)
    for e in t.get("data", {}).get("entries", []):
        nm  = e.get("name")
        fid = e.get("fight")
        d = boss if fid_is_boss.get(fid) else trash
        d[nm] = d.get(nm, 0) + 1
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
    return boss, trash, dict(recaps)


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
# Build WEEK_DATA
# ══════════════════════════════════════════════════════════════════════════════

def build_week_data(report_code: str, token: str, refresh_baseline: bool = False,
                    log_data: dict = None, report: dict = None, history: dict = None) -> dict:
    cache = load_cache()
    print(f"\n[1/5] Fetching report metadata: {report_code}")
    if report is None:   # may be pre-fetched by main() to avoid a duplicate call
        report = gql(token, Q_REPORT, {"code": report_code})["reportData"]["report"]

    zone      = report["zone"]["name"]
    start_ms  = report["startTime"]
    start_dt  = time.strftime("%b %d, %Y %H:%M", time.localtime(start_ms / 1000))
    kills     = [f for f in report["fights"] if f.get("kill")]
    fight_ids = [f["id"] for f in kills]

    # Group kill times by zone for the header pills
    boss_times: dict[str, int] = {}
    for f in kills:
        duration_s = (f["endTime"] - f["startTime"]) // 1000
        boss_times[f["name"]] = duration_s
    print(f"   Zone: {zone}  |  {len(kills)} kills  |  Fights: {fight_ids}")

    # ── Player details + gear ──────────────────────────────────────────────
    # Fetch actor list (needed to map sourceID → name for crit events)
    actor_data = gql(token, """
    query GetActors($code: String!) {
      reportData { report(code: $code) {
        masterData { actors(type: "Player") { id name type subType } }
      }}
    }""", {"code": report_code})
    actors = actor_data["reportData"]["report"]["masterData"]["actors"]

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
    crit_counts_by_id = fetch_actual_crit(token, report_code, kills)
    crit_by_name      = merge_actor_names(crit_counts_by_id, actors)

    # Curated deaths (no Feign Death) from WCL, split boss vs trash by fight, + killing blows.
    death_boss, death_trash, death_recaps = fetch_deaths_split(token, report_code)

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
    healer_war      = compute_healer_war(token, heal_by_fight, kills, players,
                                         fight_roles, refresh=refresh_baseline)
    # Tank metrics: prefer combat-log tallies (free + naturally per-fight scoped);
    # fall back to the WCL DamageTaken API only when no log.
    if log_data and log_data.get("fight_dmg_taken"):
        tank_metrics = compute_tank_metrics_from_log(log_data, fight_roles, fight_durs, kills)
    else:
        tank_metrics = fetch_tank_metrics(token, report_code, kills, fight_roles,
                                          fight_durs, heal_by_fight)
    role_spells     = fetch_role_spell_usage(token, report_code, fight_ids, players)
    uptime_by_fight = fetch_uptime_by_fight(token, report_code, kills)   # {name: {boss: uptime%}}
    for p in players:
        p["uptime_by_fight"] = [{"boss": b, "uptime": u}
                                for b, u in uptime_by_fight.get(p["name"], {}).items()]

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
        "ability_icons": fetch_ability_icons(token, report_code, fight_ids),
        # accurate pull-time consumables (from combatantinfo auras)
        "ci_consumables": ci_consumables,
        # per-player healing throughput/efficiency + per-spell breakdown + mana
        "healing_metrics": healing_metrics,
        "healing_spells":  healing_spells,
        "healer_mana":     healer_mana,
        "healer_war":      healer_war,
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
    }

    return week_data


# ══════════════════════════════════════════════════════════════════════════════
# Map WCL data → WEEK_DATA format expected by the HTML dashboard
# ══════════════════════════════════════════════════════════════════════════════

def build_consumable_compliance(consumable_usage):
    """Reshape consumableUsage into the role-split compliance grid shape — a pure transform,
    no new queries. Each row → {name, role, flask, food, weapon, combat_pot, alt_pot}.
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
            "food":  bool(e.get("food")),
            "weapon": bool(e.get("weapon_oil")),
            "combat_pot": e.get("combat_pot_name"),   # specific potion name or None
            "alt_pot": alt_pot,
        })
    return out


def map_to_week_data(wcl: dict) -> dict:
    """Convert WCL API output to the WEEK_DATA structure the HTML renders from."""
    players = wcl.get("players", [])

    def crit_list(role):
        return [
            {"name": p["name"], "crit": p["actual_crit"]}
            for p in sorted(players, key=lambda x: -x["actual_crit"])
            if p["role"] == role and p["actual_crit"] > 0
        ]

    # Avoidable damage — use total_dmg as a proxy when real avoidable data unavailable
    avoid_list = [
        {"name": p["name"], "role": p["role"], "dmg": p["total_dmg"]}
        for p in sorted(players, key=lambda x: -x["total_dmg"])
        if p["total_dmg"] > 0
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
    # Friendly fire is now clumping/positioning only (sappers dropped — self-damage;
    # MC blame moved to the aggressor in mcLiability below).
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
        consum_usage.append({
            "name": n,
            "role":  roster_idx.get(n, {}).get("role", ""),
            "class": roster_idx.get(n, {}).get("class", ""),
            "flask": flask, "elixirs": elixirs, "food": food,
            "scrolls": c.get("scrolls", []), "weapon_oil": bool(c.get("weapon_oil")),
            "potion": u.get("potion", 0), "rune": u.get("rune", 0),
            "healthstone": u.get("healthstone", 0),
            # specific item names for the compliance grid (None if unused)
            "combat_pot_name": lb.get("combat_pot"),
            "rune_name": lb.get("rune"),
            "flamecap": bool(u.get("flamecap")),
            "nightmare_seed": bool(u.get("nightmare_seed")),
            # prepared = has a flask (or 2 elixirs) AND food — the BiS baseline
            "prepared": bool(flask or len(elixirs) >= 2) and food,
        })
    # least-prepared first (missing flask/food bubbles up — that's the accountability angle)
    consum_usage.sort(key=lambda x: (x["prepared"], bool(x["flask"] or x["elixirs"]), x["food"],
                                     x["name"]))
    # Role-split compliance grid (binary ✓/✗) — replaces the old prep score.
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
          "vs_replacement": heal_war.get(nm, 0),
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
          "deaths": roster_idx.get(nm, {}).get("deaths", 0)}
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
        "healing":             healing,
        "tankScorecard":       tank_scorecard,
        "roleSpells":          wcl.get("role_spells", {}),
        "playerSpells":        wcl.get("player_spells", {}),
        "damage": sorted(
            ({"name": p["name"], "role": p["role"], "total_dmg": p.get("total_dmg", 0),
              "active_pct": p.get("active_pct", 0),
              "uptime_by_fight": p.get("uptime_by_fight", {})}
             for p in players if p.get("total_dmg", 0) > 0),
            key=lambda x: -x["total_dmg"])[:10],
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
            [{"name": p["name"], "count": p.get("interrupt_count", 0)}
             for p in players if p.get("interrupt_count", 0) > 0],
            key=lambda x: -x["count"]
        ),
        "boss_times":   wcl.get("boss_times", {}),
        "healReaction": wcl.get("heal_reaction", {}),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Inject WEEK_DATA into HTML dashboard
# ══════════════════════════════════════════════════════════════════════════════

def inject_into_html(week_data: dict, html_path: Path):
    """Replace const WEEK_DATA = {...}; in the HTML using bracket counting."""
    html   = html_path.read_text(encoding="utf-8")
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


# ══════════════════════════════════════════════════════════════════════════════
# Combat log parser — supplements WCL data with local log file
# ══════════════════════════════════════════════════════════════════════════════

ENG_SPELLS  = {30486:"Super Sapper",23506:"Goblin Sapper",4068:"Goblin Sapper",
               30461:"Adamantite Grenade",30216:"Fel Iron Bomb"}
# Engineering damage ability NAMES (as they appear in SPELL_DAMAGE) — for REAL
# engineering damage to bosses instead of the old count×30000 estimate.
ENG_DMG_NAMES = {"Super Sapper Charge", "Goblin Sapper Charge", "Fel Iron Bomb",
                 "Adamantite Grenade", "The Big One", "Dense Dynamite", "Goblin Mortar",
                 "Cobalt Frag Bomb", "Adamantite Frag Bomb"}
DRUM_SPELLS = {35476:"Drums of Battle",35475:"Drums of Battle",
               35477:"Drums of War",35478:"Drums of Restoration"}

# Avoidable mechanics matched by SPELL NAME (stable across patches). Curated against
# real DamageTaken data: ONLY mechanics players can dodge/reposition to avoid — never
# raid-wide pulses (Pounding, Earthquake, Forked Lightning) or self/tank damage
# (Seal of the Martyr, Shadow Word: Death). Rule of thumb: if it hit most of the raid,
# it isn't a fair call-out. Grouped by encounter.
AVOIDABLE_SPELL_NAMES = {
    # Void Reaver
    "Arcane Orb",               # dodge the rolling orb
    # The Lurker Below
    "Spout",                    # move out of the rotating spout
    "Whirl",                    # move out of the submerge whirl
    "Geyser",
    "Entangle",
    # Leotheras the Blind
    "Whirlwind",                # run from the demon's whirlwind
    "Chaos Blast",              # demon-phase fire — don't get caught
    # Morogrim Tidewalker
    "Watery Grave Explosion",   # spread so the grave doesn't chain
    "Scalding Water",           # move out of the pool
    # High Astromancer Solarian
    "Wrath of the Astromancer", # run out with the bomb (also tracked as friendly fire)
    # Al'ar
    "Conflagration",            # run out of the fire patch
    "Sear Nova",                # spread for the nova
    "Flame Patch",              # don't stand in fire
    "Flame Buffet",
    # Kael'thas / Tempest Keep
    "Nether Vapor",
    "Nether Beam",
    "Blinding Light",
    "Arcane Disruption",
    "Shock Barrier",
    # Gruul's Lair
    "Cave In",                  # move out of the targeted cave-in
    # Dropped (not avoidable): Pounding, Forked Lightning, Earthquake (raid-wide),
    # Arcane Missiles (RNG-targeted cast), Poison Bolt (add damage), Holy Nova (noise).
}

def _parse_ts(ts_str):
    m = re.match(r'\d+/\d+/\d+ (\d+):(\d+):(\d+)\.(\d+)', ts_str)
    if m:
        h,mi,s,ms = m.groups()
        return int(h)*3600 + int(mi)*60 + int(s) + int(ms)/1000
    return 0

def parse_combat_log(log_path: str, allowed_bosses=None) -> dict:
    """
    Parse WoWCombatLog.txt and return a dict with per-player stats
    to be merged with WCL API data.
    Returns: { player_name: { actual_crit, deaths, interrupts, eng, drums, avoidable_dmg } }

    `allowed_bosses`: if given (the WCL report's kill boss names), any encounter NOT in it
    (e.g. off-report DST clears like Gruul / High King Maulgar that share the night's log)
    is treated as an EXCLUSION window — every event inside it is skipped, so the dashboard
    stays scoped to the report code you entered.
    """
    print(f"\n[LOG] Parsing combat log: {log_path}")
    from collections import defaultdict

    # Pass 1: identify all successful-kill encounter windows, split into in-report (kept)
    # vs off-report (excluded).
    all_kills = []
    with open(log_path, encoding="utf-8", errors="replace") as f:
        current = None
        for line in f:
            parts = re.split(r'\s{2}', line.strip(), maxsplit=1)
            if len(parts) != 2: continue
            ts_str, data = parts
            fields = data.split(",")
            ev = fields[0]
            if ev == "ENCOUNTER_START":
                current = {"name": fields[2].strip('"'), "start": _parse_ts(ts_str)}
            elif ev == "ENCOUNTER_END" and current:
                if len(fields) > 5 and fields[5].strip() == "1":
                    current["end"] = _parse_ts(ts_str)
                    all_kills.append(current)
                current = None

    if allowed_bosses is not None:
        kills    = [k for k in all_kills if k["name"] in allowed_bosses]
        excluded = [k for k in all_kills if k["name"] not in allowed_bosses]
    else:
        kills, excluded = all_kills, []
    excl_windows = [(k["start"], k["end"]) for k in excluded]
    if excluded:
        print(f"  [LOG] ignoring {len(excluded)} off-report fight(s): "
              f"{', '.join(k['name'] for k in excluded)}")

    def in_excluded(ts):
        return any(s <= ts <= e for s, e in excl_windows)

    kill_windows = [(k["start"], k["end"], k["name"]) for k in kills]
    def in_kill(ts):
        return any(s <= ts <= e for s,e,_ in kill_windows)
    def which_boss(ts):
        for s,e,nm in kill_windows:
            if s <= ts <= e:
                return nm
        return None

    # Pass 2: collect stats
    player_names = {}        # guid → first name (strip realm)
    gear_crit_by_guid = {}   # guid → {_crit_melee, _crit_ranged, _crit_spell} from COMBATANT_INFO
    gear_by_name = {}        # name → {_crit_melee, _crit_ranged, _crit_spell} (resolved after parse)
    swing_crits  = defaultdict(int)
    swing_hits   = defaultdict(int)
    spell_crits  = defaultdict(int)
    spell_hits   = defaultdict(int)
    dmg_totals   = defaultdict(int)
    interrupts   = defaultdict(list)
    # Per-fight role signals (by boss name) — ground truth for spec-swap-aware roles,
    # immune to WCL's tank spec-label quirks (Warden/Justicar). See classify below.
    fight_melee_taken  = defaultdict(lambda: defaultdict(int))  # [boss][player] = boss MELEE dmg taken (TANK signal)
    fight_healing_done = defaultdict(lambda: defaultdict(int))  # [boss][player] = effective healing (HEALER signal)
    fight_damage_done  = defaultdict(lambda: defaultdict(int))  # [boss][player] = damage to creatures (DPS signal)
    fight_dmg_taken    = defaultdict(lambda: defaultdict(int))  # [boss][player] = ALL dmg taken (tank DTPS)
    fight_heal_recv    = defaultdict(lambda: defaultdict(int))  # [boss][player] = healing received (tank soak)
    eng_usage    = defaultdict(lambda: defaultdict(int))
    eng_dmg      = defaultdict(int)   # real engineering damage to enemies
    drums_cast   = defaultdict(int)
    drums_buffs  = defaultdict(int)
    avoidable    = defaultdict(lambda: defaultdict(int))   # [player][mechanic] = dmg
    avoid_hits   = defaultdict(lambda: defaultdict(list))  # [player][mechanic] = [{t,amt}] drill-down
    mech_boss    = defaultdict(lambda: defaultdict(int))   # [mechanic][boss]   = dmg (attribution)
    friendly_fire = defaultdict(lambda: {"dmg": 0, "incidents": 0, "hits": [],
                                         "cats": defaultdict(int), "mechanics": defaultdict(int),
                                         "victims": set(), "bosses": set()})
    mc_now    = set()              # player GUIDs currently Mind Controlled
    mc_save_credit = defaultdict(set)  # MC'd GUID → casters already credited a save this episode
    mc_count  = defaultdict(int)   # times each player was MC'd (by name)
    mc_source = {}                 # player name → who controlled them (last seen)
    consum_use = defaultdict(lambda: defaultdict(int))  # [player][category] = use count
    consum_label = defaultdict(dict)  # [player][category] = specific item name (first seen)
    # MC accountability — blame flips onto the raid when a teammate is controlled.
    mc_saves  = defaultdict(lambda: {"count": 0, "spells": defaultdict(int),
                                     "targets": defaultdict(int), "hits": []})  # by caster
    mc_liable = defaultdict(lambda: {"dmg": 0, "hits": 0, "kills": 0,
                                     "spells": defaultdict(int), "victims": defaultdict(int),
                                     "events": []})  # by aggressor
    last_aoe_on = {}               # MC'd victim GUID → (aggressor, spell, amt, ts) for killing-blow
    consumes  = defaultdict(lambda: {"flask": False, "food": False, "elixirs": set()})
    # HP% timeline: players log HP as a PERCENT (maxHP field == "100") in the advanced
    # block. Sampled on damage taken / heals received / own melee → dense per-player track.
    hp_samples = defaultdict(lambda: defaultdict(list))  # [player][boss] = [(ts, hp_pct, kind)]
    log_deaths = defaultdict(list)                       # [player] = [(ts, boss)] real UNIT_DIED in a boss window

    with open(log_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            parts = re.split(r'\s{2}', line.strip(), maxsplit=1)
            if len(parts) != 2: continue
            ts_str, data = parts
            fields = data.split(",")
            ev = fields[0]
            if len(fields) < 6: continue
            ts = _parse_ts(ts_str)
            if in_excluded(ts): continue   # skip off-report fights (Gruul / HKM DST clears)

            # Track player name from any event. COMBATANT_INFO's field[2] is the
            # faction (a number), not a name — skip it so we don't poison the map.
            if "Player-" in fields[1] and len(fields) > 2 and ev != "COMBATANT_INFO":
                guid = fields[1]
                if guid not in player_names:
                    player_names[guid] = fields[2].strip('"').split("-")[0]

            # Parse COMBATANT_INFO for per-school gear crit rating. Key by GUID and
            # resolve to a name after the loop — a player's name may not be known
            # yet when their COMBATANT_INFO line appears at the pull.
            # TBC 2.5.x CLEU stat order after GUID,faction:
            #   [3]str [4]agi [5]sta [6]int [7]spi [8]dodge [9]parry [10]block
            #   [11]critMelee [12]critRanged [13]critSpell
            if ev == "COMBATANT_INFO" and "Player-" in fields[1]:
                try:
                    gear_crit_by_guid[fields[1]] = {
                        "_crit_melee":  int(fields[11]),
                        "_crit_ranged": int(fields[12]),
                        "_crit_spell":  int(fields[13]),
                        "_agility":     int(fields[4]),    # [4]=agi, for stat-derived crit backfill
                    }
                except: pass

            # Mind Control windows — a controlled player's damage to the raid is the
            # boss's fault, not theirs. Track who's currently MC'd (boss OR trash) by
            # aura name so it generalizes (Kael'thas, Fel Reaver room, etc.).
            if ev == "SPELL_AURA_APPLIED" and len(fields) > 10 and "Player-" in fields[5] \
                    and fields[10].strip('"') in MC_AURAS:
                if fields[5] not in mc_now:        # fresh control episode → reset save credits
                    mc_save_credit.pop(fields[5], None)
                mc_now.add(fields[5])
                dn = player_names.get(fields[5], fields[5])
                mc_count[dn] += 1
                mc_source[dn] = fields[2].strip('"')
            elif ev == "SPELL_AURA_REMOVED" and len(fields) > 10 and "Player-" in fields[5] \
                    and fields[10].strip('"') in MC_AURAS:
                mc_now.discard(fields[5])
                mc_save_credit.pop(fields[5], None)

            # MC SAVE — a raider lands CC on a currently-controlled teammate, parking
            # them harmlessly (Cyclone et al.). Credit the caster. Cyclone is the marquee.
            if ev == "SPELL_AURA_APPLIED" and len(fields) > 10 \
                    and "Player-" in fields[1] and "Player-" in fields[5] \
                    and fields[1] != fields[5] and fields[5] in mc_now \
                    and fields[10].strip('"') in CC_ABILITIES:
                caster = player_names.get(fields[1], fields[1])
                tgt    = player_names.get(fields[5], fields[5])
                spell  = fields[10].strip('"')
                # Dedupe: one save per caster per controlled ally per MC episode — re-casting
                # CC to keep them parked isn't a second save.
                if caster not in mc_save_credit[fields[5]]:
                    mc_save_credit[fields[5]].add(caster)
                    rec = mc_saves[caster]
                    rec["count"] += 1; rec["spells"][spell] += 1; rec["targets"][tgt] += 1
                    if len(rec["hits"]) < 40:
                        rec["hits"].append({"t": ts_str.split()[1][:8] if " " in ts_str else "",
                                            "spell": spell, "target": tgt})

            # Consumables — flask / food / elixirs from buff auras. Any apply/refresh/
            # remove means the player had it (flasks persist through death; food/elixirs
            # get re-applied, so these events catch them).
            if ev in ("SPELL_AURA_APPLIED", "SPELL_AURA_REFRESH", "SPELL_AURA_REMOVED") \
                    and len(fields) > 10 and "Player-" in fields[5]:
                bn = fields[10].strip('"')
                if bn.startswith("Flask of") or bn == FOOD_BUFF \
                        or bn in ELIXIR_BUFFS or bn.startswith("Elixir of"):
                    cn = player_names.get(fields[5], fields[5])
                    if bn.startswith("Flask of"):                       consumes[cn]["flask"] = True
                    elif bn == FOOD_BUFF:                               consumes[cn]["food"]  = True
                    else:                                               consumes[cn]["elixirs"].add(bn)

            # Consumable USAGE (informational call-out): healthstones, runes, pots, scrolls.
            # One SPELL_CAST_SUCCESS per activation = a clean use count.
            if ev == "SPELL_CAST_SUCCESS" and "Player-" in fields[1] and len(fields) > 10:
                _spell = fields[10].strip('"')
                _cat = _consumable_category(_spell)
                if _cat:
                    _pn = player_names.get(fields[1], fields[1])
                    consum_use[_pn][_cat] += 1
                    consum_label[_pn].setdefault(_cat, _spell)   # specific name: Dark Rune / Flame Cap / Nightmare Seed

            # Combat potions (Destruction, Haste, …) log only their effect BUFF, not a "… Potion"
            # cast — count each APPLIED as one potion use.
            if ev == "SPELL_AURA_APPLIED" and len(fields) > 10 and "Player-" in fields[5] \
                    and fields[10].strip('"') in POTION_BUFFS:
                _eff = fields[10].strip('"')
                _pn = player_names.get(fields[5], fields[5])
                consum_use[_pn]["potion"] += 1
                consum_label[_pn].setdefault("combat_pot", POTION_NAME.get(_eff, _eff + " Potion"))

            # Player→player damage. Two distinct accountability paths:
            #   (1) TARGET is Mind Controlled → a raider AoE'd the controlled ally.
            #       LIABLE — but only for intentional AoE (AOE_ABILITIES); single-target
            #       overlap doesn't count. Killing blow tracked via last_aoe_on + UNIT_DIED.
            #   (2) neither MC'd → clumping/positioning friendly fire (FF_MECHANICS).
            # Sapper self-damage and a controlled player's own swings are NOT shamed.
            if ev in ("SPELL_DAMAGE", "SPELL_PERIODIC_DAMAGE", "RANGE_DAMAGE") \
                    and "Player-" in fields[1] and len(fields) > 5 and "Player-" in fields[5] \
                    and fields[1] != fields[5]:
                ff_spell = fields[10].strip('"') if len(fields) > 10 else ""
                try:    amt = int(fields[30]) if len(fields) > 30 else 0
                except: amt = 0
                if amt > 0 and ff_spell not in FF_REFLECT:
                    if fields[5] in mc_now:                 # (1) aggressor hit a controlled ally
                        if ff_spell in AOE_ABILITIES:
                            agg = player_names.get(fields[1], fields[1])
                            vic = player_names.get(fields[5], fields[5])
                            rec = mc_liable[agg]
                            rec["dmg"] += amt; rec["hits"] += 1
                            rec["spells"][ff_spell] += amt; rec["victims"][vic] += amt
                            last_aoe_on[fields[5]] = (agg, ff_spell, amt,
                                ts_str.split()[1][:8] if " " in ts_str else "", vic)
                            if len(rec["events"]) < 40:
                                rec["events"].append({"t": ts_str.split()[1][:8] if " " in ts_str else "",
                                                      "amt": amt, "spell": ff_spell, "victim": vic, "kill": False})
                    elif fields[1] not in mc_now and ff_spell in FF_MECHANICS:  # (2) clumping
                        ff = friendly_fire[player_names.get(fields[1], fields[1])]
                        ff["dmg"] += amt; ff["incidents"] += 1
                        ff["cats"]["mechanic"] += amt
                        ff["mechanics"][ff_spell] += amt
                        ff["victims"].add(player_names.get(fields[5], fields[5]))
                        _fb = which_boss(ts)
                        if _fb: ff["bosses"].add(_fb)
                        if len(ff["hits"]) < 40:
                            ff["hits"].append({"t": ts_str.split()[1][:8] if " " in ts_str else "",
                                               "amt": amt, "mech": ff_spell, "cat": "mechanic",
                                               "boss": which_boss(ts) or ""})

            # Killing blow on a controlled teammate — credit the marquee shame to whoever
            # landed the last intentional-AoE hit on the MC'd victim before they died.
            if ev == "UNIT_DIED" and len(fields) > 5 and "Player-" in fields[5] \
                    and fields[5] in mc_now and fields[5] in last_aoe_on:
                agg, spell, amt, t, vic = last_aoe_on.pop(fields[5])
                rec = mc_liable[agg]
                rec["kills"] += 1
                for e in rec["events"]:
                    if e["spell"] == spell and e["victim"] == vic and not e["kill"]:
                        e["kill"] = True
                        break

            # Player death in a boss window — for the HP-recap timeline + reaction censoring.
            # (Real-death filter via HP→0 happens later; Feign Death keeps HP up and is dropped.)
            if ev == "UNIT_DIED" and len(fields) > 5 and fields[5].startswith("Player-"):
                _db = which_boss(ts)
                if _db:
                    log_deaths[player_names.get(fields[5], fields[5])].append((ts, _db))

            if not in_kill(ts): continue

            src_guid = fields[1]
            dst_guid = fields[5] if len(fields) > 5 else ""
            src_name = player_names.get(src_guid, src_guid)
            dst_name = player_names.get(dst_guid, dst_guid)
            src_is_player = "Player-" in src_guid
            dst_is_player = "Player-" in dst_guid
            dst_is_boss   = "Creature-" in dst_guid
            src_is_creature = "Creature-" in src_guid

            # ── Per-fight role signals + tank survivability (by boss name) ──────
            _boss = which_boss(ts)
            if _boss:
                # Incoming damage to a player → tank DTPS; SWING share = the tank signal
                if dst_is_player and src_is_creature and ev in \
                        ("SWING_DAMAGE", "SPELL_DAMAGE", "SPELL_PERIODIC_DAMAGE", "RANGE_DAMAGE"):
                    idx = 27 if ev == "SWING_DAMAGE" else 30
                    try:
                        amt = int(fields[idx])
                        fight_dmg_taken[_boss][dst_name] += amt
                        if ev == "SWING_DAMAGE":
                            fight_melee_taken[_boss][dst_name] += amt   # in the boss's face = TANK
                    except: pass
                # Healing: done by source (HEALER signal) + received by dest (tank soak).
                # Advanced-log SPELL_HEAL layout (this client): amount=[31], overheal=[32].
                elif ev in ("SPELL_HEAL", "SPELL_PERIODIC_HEAL"):
                    try:
                        eff = max(int(fields[31]) - int(fields[32]), 0)
                        if src_is_player: fight_healing_done[_boss][src_name] += eff
                        if dst_is_player: fight_heal_recv[_boss][dst_name]   += eff
                    except: pass
                # DPS: player damage to creatures (any school)
                elif ev in ("SWING_DAMAGE", "SPELL_DAMAGE", "SPELL_PERIODIC_DAMAGE", "RANGE_DAMAGE") \
                        and src_is_player and dst_is_boss:
                    idx = 27 if ev == "SWING_DAMAGE" else 30
                    try:    fight_damage_done[_boss][src_name] += int(fields[idx])
                    except: pass

                # HP% sample — the advanced block carries the relevant unit's HP as a percent
                # (maxHP field == "100" for players). SWING → source unit; SPELL/RANGE/HEAL →
                # target unit. So this captures damage TAKEN, heals RECEIVED, and own melee.
                if ev == "SWING_DAMAGE":               gi, hi, mi, _kind = 9, 11, 12, "dmg"
                elif ev in ("SPELL_DAMAGE", "SPELL_PERIODIC_DAMAGE", "RANGE_DAMAGE"):
                                                        gi, hi, mi, _kind = 12, 14, 15, "dmg"
                elif ev == "SPELL_HEAL":               gi, hi, mi, _kind = 12, 14, 15, "heal"   # direct heal
                elif ev == "SPELL_PERIODIC_HEAL":      gi, hi, mi, _kind = 12, 14, 15, "hot"    # HoT tick
                else:                                   gi = None
                if gi is not None and len(fields) > mi and fields[mi] == "100" \
                        and fields[gi].startswith("Player-"):
                    try:
                        _hpct = int(fields[hi])
                        if 0 <= _hpct <= 100:
                            _nm = src_name if fields[gi] == src_guid else dst_name
                            hp_samples[_nm][_boss].append((ts, _hpct, _kind))
                    except: pass

            # Swing damage → crit
            if ev == "SWING_DAMAGE" and src_is_player and dst_is_boss:
                try:
                    amt = int(fields[27])
                    dmg_totals[src_name] += amt
                    if fields[-3].strip() == "1": swing_crits[src_name] += 1
                    else: swing_hits[src_name] += 1
                except: pass

            # Spell damage → crit
            if ev == "SPELL_DAMAGE" and src_is_player and dst_is_boss:
                try:
                    amt = int(fields[30])
                    dmg_totals[src_name] += amt
                    if fields[-4].strip() == "1": spell_crits[src_name] += 1
                    else: spell_hits[src_name] += 1
                    # real engineering damage (sappers/bombs), tracked separately
                    if (fields[10].strip('"') if len(fields) > 10 else "") in ENG_DMG_NAMES:
                        eng_dmg[src_name] += amt
                except: pass

            # Interrupts: PLAYER (src) interrupts CREATURE (dst)
            if ev == "SPELL_INTERRUPT" and src_is_player and not dst_is_player:
                interrupted = fields[13].strip('"') if len(fields) > 13 else "?"
                interrupts[src_name].append(interrupted)

            # Engineering
            if ev == "SPELL_CAST_SUCCESS" and src_is_player:
                try:
                    sid = int(fields[9])
                    if sid in ENG_SPELLS:
                        eng_usage[src_name][ENG_SPELLS[sid]] += 1
                except: pass

            # Drums cast count
            if ev == "SPELL_CAST_SUCCESS" and src_is_player:
                try:
                    sid = int(fields[9])
                    if sid in DRUM_SPELLS:
                        drums_cast[src_name] += 1
                except: pass

            # Drum buffs applied
            if ev == "SPELL_AURA_APPLIED" and "BUFF" in data:
                try:
                    sid = int(fields[9])
                    if sid in DRUM_SPELLS:
                        drums_buffs[src_name] += 1
                except: pass

            # Avoidable damage taken — matched by spell name (reliable across patches).
            # Tracked per mechanic and attributed to the boss whose kill window it lands in.
            if ev in ("SPELL_DAMAGE","SPELL_PERIODIC_DAMAGE") and dst_is_player and not src_is_player:
                spell_name = fields[10].strip('"') if len(fields) > 10 else ""
                if spell_name in AVOIDABLE_SPELL_NAMES:
                    try:
                        amt = int(fields[30]) if len(fields) > 30 else 0
                        avoidable[dst_name][spell_name] += amt
                        hh = avoid_hits[dst_name][spell_name]
                        if len(hh) < 40:   # cap per (player, mechanic) to bound HTML size
                            hh.append({"t": ts_str.split()[1][:8] if " " in ts_str else "", "amt": amt})
                        boss = which_boss(ts)
                        if boss:
                            mech_boss[spell_name][boss] += amt
                    except: pass

    # Resolve COMBATANT_INFO crit (keyed by GUID) to player names now that the
    # full GUID→name map is known.
    for guid, crits in gear_crit_by_guid.items():
        name = player_names.get(guid)
        if name:
            gear_by_name[name] = crits

    # Build per-player output. Include players who only appear in COMBATANT_INFO
    # (e.g. healers who deal no boss damage) so their gear crit can be backfilled.
    all_names = (set(swing_crits) | set(spell_crits) | set(dmg_totals)
                 | set(gear_by_name) | set(avoidable))
    result = {}
    for name in all_names:
        total_hits  = swing_hits[name] + spell_hits[name]
        total_crits = swing_crits[name] + spell_crits[name]
        denom = total_hits + total_crits
        crits = gear_by_name.get(name, {})
        result[name] = {
            "actual_crit":     round(total_crits / denom * 100, 1) if denom > 0 else 0.0,
            "total_dmg":       dmg_totals[name],
            "interrupt_count": len(interrupts.get(name, [])),
            "interrupt_list":  interrupts.get(name, []),
            "eng":             dict(eng_usage.get(name, {})),
            "eng_dmg":         eng_dmg.get(name, 0),
            "avoidable_dmg":   sum(avoidable.get(name, {}).values()),
            "avoidable_sources": dict(avoidable.get(name, {})),
            "avoidable_hits":  {m: list(hs) for m, hs in avoid_hits.get(name, {}).items()},
            "_crit_melee":     crits.get("_crit_melee", 0),
            "_crit_ranged":    crits.get("_crit_ranged", 0),
            "_crit_spell":     crits.get("_crit_spell", 0),
            "_agility":        crits.get("_agility", 0),
        }

    drums_out = []
    for name, casts in sorted(drums_cast.items(), key=lambda x: -x[1]):
        buffs = drums_buffs.get(name, 0)
        drums_out.append({
            "name": name,
            "casts": casts,
            "total": casts,   # alias the dashboard reads as the per-drummer "Total"
            "buffs": buffs,
            "buffs_per_drum": round(buffs / casts, 2) if casts > 0 else 0,
            "score": buffs,
        })

    # Friendly-fire offenders (source side): who damaged teammates, how much, how often
    ff_out = []
    for name, d in sorted(friendly_fire.items(), key=lambda x: -x[1]["dmg"]):
        cats = dict(d["cats"])
        ff_out.append({
            "name": name,
            "dmg": d["dmg"],
            "incidents": d["incidents"],
            "cats": cats,                                   # {mc, engineering, mechanic}
            "category": max(cats, key=cats.get) if cats else "",   # dominant cause
            "mechanics": dict(d["mechanics"]),
            "hits": list(d.get("hits", [])),                # drill-down detail
            "victims": sorted(d.get("victims", set())),     # who got splashed (context)
            "bosses": sorted(d.get("bosses", set())),       # which fights (context)
            "mc_count": mc_count.get(name, 0),
            "mc_by": mc_source.get(name, ""),
        })

    # MC accountability — Saves (CC'd a controlled ally) and Liabilities (AoE'd one).
    mc_saves_out = []
    for name, d in sorted(mc_saves.items(), key=lambda x: -x[1]["count"]):
        mc_saves_out.append({
            "name": name, "count": d["count"],
            "spells": dict(d["spells"]), "targets": dict(d["targets"]),
            "hits": list(d.get("hits", [])),
        })
    mc_liable_out = []
    for name, d in sorted(mc_liable.items(), key=lambda x: (-x[1]["kills"], -x[1]["dmg"])):
        mc_liable_out.append({
            "name": name, "dmg": d["dmg"], "hits": d["hits"], "kills": d["kills"],
            "spells": dict(d["spells"]), "victims": dict(d["victims"]),
            "events": list(d.get("events", [])),
        })

    # Dominant boss per avoidable mechanic, for the legend's "what fight" label
    mech_boss_out = {m: max(bosses.items(), key=lambda x: x[1])[0]
                     for m, bosses in mech_boss.items() if bosses}

    consumes_out = {n: {"flask": c["flask"], "food": c["food"], "elixirs": sorted(c["elixirs"])}
                    for n, c in consumes.items()}

    # ── Per-fight roles (by boss name) from combat-log ground truth ──────────────
    # Tank  = took a real share of the boss's MELEE (immune to WCL spec-label quirks).
    # Healer= effective healing is a meaningful share of the raid's AND exceeds own damage.
    # DPS   = dealt damage and is neither. This replaces ~10 per-fight playerDetails calls.
    fight_roles_log = {}
    _bosses = set(fight_melee_taken) | set(fight_healing_done) | set(fight_damage_done)
    for boss in _bosses:
        melee = fight_melee_taken.get(boss, {})
        heald = fight_healing_done.get(boss, {})
        dmgd  = fight_damage_done.get(boss, {})
        max_melee  = max(melee.values()) if melee else 0
        total_heal = sum(heald.values()) or 1
        roles = {"Tank": [], "Healer": [], "dps": []}
        for p in (set(melee) | set(heald) | set(dmgd)):
            mt, hd, dd = melee.get(p, 0), heald.get(p, 0), dmgd.get(p, 0)
            if max_melee and mt >= max(max_melee * 0.25, 20000):
                roles["Tank"].append(p)
            elif hd >= total_heal * 0.04 and hd > dd:
                roles["Healer"].append(p)
            elif dd > 0:
                roles["dps"].append(p)
        fight_roles_log[boss] = roles

    print(f"  [LOG] {len(result)} players · {len(kills)} kill fights parsed · "
          f"{len(ff_out)} clumping FF · {len(mc_liable_out)} MC-liable · {len(mc_saves_out)} MC-savers")
    return {"players": result, "drums": drums_out, "fights": kills,
            "friendly_fire": ff_out, "avoidable_mech_boss": mech_boss_out,
            "consumables": consumes_out,
            "mc_saves": mc_saves_out, "mc_liable": mc_liable_out,
            "fight_roles_log": fight_roles_log,
            "fight_dmg_taken": {b: dict(v) for b, v in fight_dmg_taken.items()},
            "fight_heal_recv": {b: dict(v) for b, v in fight_heal_recv.items()},
            "consum_use": {n: dict(v) for n, v in consum_use.items()},
            "consum_label": {n: dict(v) for n, v in consum_label.items()},
            "hp_samples": {n: {b: list(s) for b, s in bs.items()} for n, bs in hp_samples.items()},
            "log_deaths": {n: list(d) for n, d in log_deaths.items()}}


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
        if log.get("total_dmg", 0) > 0:   p["total_dmg"]  = log["total_dmg"]
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
    _log_files = sorted(LOGS_DIR.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
    if _log_files:
        _auto_log = str(_log_files[0])
    parser.add_argument("--log", default=_auto_log,
                        help="Path to WoWCombatLog.txt (default: newest file in logs/)")
    parser.add_argument("--dry-run",  action="store_true", help="Print JSON only, don't write HTML")
    parser.add_argument("--test-db",  action="store_true", help="Write to raid_history_test.db instead of prod")
    parser.add_argument("--refresh-baseline", action="store_true",
                        help="Force-refresh the healer replacement-level cohort cache (else ~monthly)")
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
    allowed_bosses = {f["name"] for f in rep0["fights"] if f.get("kill")}

    # Parse the combat log FIRST so build_week_data can use its (free) per-fight roles
    # instead of ~10 playerDetails API calls.
    log_data = parse_combat_log(args.log, allowed_bosses=allowed_bosses) if args.log else None

    # Per-player crit baseline from history (gold-standard 'luck' = this week vs your own
    # multi-week average). Read from the SAME db the run will write to, excluding this report.
    from db_writer import crit_history, DB_PATH_TEST, DB_PATH
    crit_hist = crit_history(DB_PATH_TEST if args.test_db else DB_PATH,
                             exclude_report=args.report_code)

    week_data = build_week_data(args.report_code, token, refresh_baseline=args.refresh_baseline,
                                log_data=log_data, report=rep0, history=crit_hist)

    # Supplement with the rest of the combat-log stats
    if log_data:
        week_data = merge_log_into_wcl(week_data, log_data)

    print_summary(week_data)

    if args.dry_run:
        print("\n─── WCL_AUTO_DATA JSON ───")
        print(json.dumps(week_data, indent=2, ensure_ascii=False))
    else:
        inject_into_html(week_data, Path(args.out))
        from db_writer import write_week, DB_PATH_TEST
        # db_writer consumes the mapped WEEK_DATA shape (luckKPI/avoidableDmg/…),
        # not the raw wcl dict — map once more (pure transform, no API calls).
        write_week(map_to_week_data(week_data), db_path=DB_PATH_TEST if args.test_db else None)
        print(f"\nRun next time with:")
        print(f"  python wcl_auto_dashboard.py {args.report_code} --out \"{args.out}\"")


if __name__ == "__main__":
    main()
