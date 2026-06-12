"""crit_model.py — the TBC expected-crit model + item-crit cache.

Everything needed to turn a player's gear/stats into an EXPECTED crit % (class base +
talent estimate + gear crit rating + primary-stat crit), plus the persisted per-item
crit-rating cache (cache/item_crit_cache.json) and its WCL item lookup.

Extracted from wcl_auto_dashboard, which re-exports every name so existing references
(build_week_data, merge_log_into_wcl, map_to_week_data) resolve unchanged.
"""
from __future__ import annotations

import json, time

from wcl_client import gql
from paths import CACHE_FILE
from roles import CASTER_SPECS, HEALER_SPECS

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


# ── Local item crit cache (persisted across runs) ─────────────────────────────

def load_cache() -> dict:
    if CACHE_FILE.exists():
        return json.loads(CACHE_FILE.read_text())
    return {}

def save_cache(cache: dict):
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, indent=2))


# ══════════════════════════════════════════════════════════════════════════════
# Item crit rating lookup
# ══════════════════════════════════════════════════════════════════════════════

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
