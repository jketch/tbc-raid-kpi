"""Curated WoW name-sets the pipeline matches against — the single home for the
constant sets CLAUDE.md's Data Source Map enumerates. A pure data leaf: imported by
both combat_log.py and wcl_auto_dashboard.py (which re-exports them), so there is one
authoritative copy. Two sections: TIER-STABLE (TBC-wide) and T5 CONTENT (the only
raid-content-coupled constants — swap these for T6).
"""

# ══════════════════════════════════════════════════════════════════════════════
# TIER-STABLE — TBC-wide game data (no raid-content coupling)
# ══════════════════════════════════════════════════════════════════════════════

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

# AoE that does NOT count as a Mind-Control liability: a persistent ground effect (dropped
# before the charm) can't be retracted once an ally is MC'd into it — not an intentional cast
# at the controlled target. Still counts for friendly-fire clumping, just not MC blame.
MC_LIABLE_EXCLUDE = {"Consecration"}

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
    "Elixir of Ironskin",
}

# Guardian (defensive/utility) elixirs — everything else in ELIXIR_BUFFS is a battle elixir.
# Used by the Raid-Prep score: flask = both slots (+4); else battle +2 / guardian +2.
GUARDIAN_ELIXIRS = {
    "Elixir of Draenic Wisdom", "Elixir of Major Defense", "Elixir of Major Fortitude",
    "Elixir of Major Mageblood", "Elixir of Empowerment", "Elixir of Ironskin",
    "Elixir of Mastery", "Earthen Elixir",
}

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

# Combat pots whose effect-buff NAME is ambiguous (shared by trinket procs / drums), so we
# match the POTION'S buff SPELL ID instead of the name to avoid massive over-counting. Verified
# live: "Haste" maps to 4 spell IDs — only 28507 is the Haste Potion (item 22838); the rest are
# trinket procs (Dragonspine Trophy 34775, etc.). Free Action (6615) is the Vashj-Entangle escape
# pot. Keys are strings — combat-log spell-ID fields are strings. Share the combat-pot CD slot.
POTION_BUFF_IDS = {
    "28507": "Haste Potion",        # item 22838 — melee/caster haste combat pot
    "6615":  "Free Action Potion",  # situational survival (e.g. Lady Vashj Entangle)
}

# Protection (resist) potions — share the potion CD, used on T5 resist mechanics (Hydross
# nature/frost, Vashj, Leotheras, etc.). Their absorb-buff NAME is potion-exclusive EXCEPT
# "Shadow Protection", which collides with the Priest buff — so we exclude the known Priest
# spell IDs. (None appeared in the reference log, so these are wired by name from item data;
# the ID exclusion keeps the one real collision — the Priest buff — out of the count.)
PROTECTION_BUFFS = {
    "Nature Protection", "Frost Protection", "Fire Protection",
    "Arcane Protection", "Holy Protection", "Shadow Protection",
}

PROTECTION_EXCLUDE_IDS = {"25433", "39374"}   # Priest Shadow Protection / Prayer of Shadow Protection

# Approx base mana cost of common TBC heal spells (max rank, pre-talent). Used to
# ESTIMATE mana spent on healing = casts × cost, for a healing-per-mana efficiency.
HEAL_MANA_COST = {
    "Flash Heal": 380, "Greater Heal": 825, "Renew": 430, "Circle of Healing": 450,
    "Prayer of Mending": 390, "Prayer of Healing": 1030, "Binding Heal": 690, "Heal": 305,
    "Chain Heal": 540, "Healing Wave": 620, "Lesser Healing Wave": 290, "Earth Shield": 460,
    "Rejuvenation": 415, "Regrowth": 675, "Lifebloom": 220, "Swiftmend": 340, "Healing Touch": 740,
    "Holy Light": 840, "Flash of Light": 180, "Holy Shock": 525,
}


# ══════════════════════════════════════════════════════════════════════════════
# ███ T5 CONTENT (SSC / TK) — the ONLY raid-content-coupled constants ███████████
# ══════════════════════════════════════════════════════════════════════════════
# Zones/boss lists come from WCL dynamically; portraits derive from the encounter id;
# everything above is TBC-wide. Only the per-boss/per-mechanic knowledge below changes
# by tier. TO ADD T6 (Hyjal / Black Temple): extend AVOIDABLE_SPELL_NAMES with the new
# bosses' avoidable mechanics (+ ICON_OVERRIDES if WCL resolves a misleading icon).
# Nothing else needs editing — week_schema.py is tier-agnostic. If T6 grows large, lift
# this section into content_t6.py and select by report["zone"].
# ══════════════════════════════════════════════════════════════════════════════

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

ENG_SPELLS  = {30486:"Super Sapper",23506:"Goblin Sapper",4068:"Goblin Sapper",
               30461:"Adamantite Grenade",30216:"Fel Iron Bomb"}

# Engineering damage ability NAMES (as they appear in SPELL_DAMAGE).
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
