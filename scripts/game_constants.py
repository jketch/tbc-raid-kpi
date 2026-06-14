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
# Detected per player from pull-time COMBATANT_INFO buff auras. Recognition is ID-ANCHORED
# (FLASK_AURA_IDS / ELIXIR_AURA_IDS below, keyed on the aura's `ability` spell-ID) with the
# name sets here as a fallback net. Why both: the Anniversary (2.5) client RENAMES buff effects
# unpredictably (Mageblood logs as "Greater Versatility", Adept's as "Spellpower Elixir") AND
# several flasks/elixirs log under a bare EFFECT name with NO "Flask of …"/"Elixir of …" prefix
# (Flask of Supreme Power → "Supreme Power", Chromatic Wonder → "Chromatic Wonder", Major Shadow
# Power, Major Firepower …) — so neither the prefix check NOR a name allowlist alone is sufficient.
# The spell-ID is the only stable key. To extend for new content: run scripts/tools/
# probe_consumable_ids.py against a kill report, then add any "—unrecognized—" consumable IDs here.
FOOD_BUFF = "Well Fed"

# Elixir buff names as they appear in logs (effect-renamed + canonical). Fallback net only —
# the ID map (ELIXIR_AURA_IDS) is the primary recognition path.
ELIXIR_BUFFS = {
    # Anniversary effect-renamed forms (verified live):
    "Mighty Agility", "Spellpower Elixir", "Healing Power", "Greater Versatility",
    "Major Shadow Power", "Major Firepower", "Major Strength", "Major Armor", "Gift of Arthas",
    # canonical "Elixir of …" / classic names:
    "Elixir of Draenic Wisdom", "Onslaught Elixir", "Adept's Elixir", "Fel Strength Elixir",
    "Elixir of Major Strength", "Elixir of Major Agility", "Elixir of Major Firepower",
    "Elixir of Mastery", "Elixir of Major Defense", "Elixir of Major Fortitude",
    "Elixir of Major Mageblood", "Elixir of the Mongoose", "Elixir of Empowerment",
    "Elixir of Ironskin", "Greater Arcane Elixir",
}

# Guardian (defensive/utility) elixirs — everything else in ELIXIR_BUFFS is a battle elixir.
# REFERENCE ONLY: the compliance "both slots filled" test no longer keys off this allowlist (it
# uses the game rule that any two distinct elixir auras = one battle + one guardian — see
# week_map.build_consumable_compliance), because Anniversary RENAMES buffs and an incomplete list
# silently failed real 2-elixir raiders. NB the Mageblood guardian aura logs as "Greater Versatility"
# (spell 28509) on Anniversary, NOT "Elixir of Major Mageblood". Kept for documentation + the audit.
GUARDIAN_ELIXIRS = {
    "Elixir of Draenic Wisdom", "Elixir of Major Defense", "Elixir of Major Fortitude",
    "Elixir of Major Mageblood", "Greater Versatility", "Major Armor", "Gift of Arthas",
    "Elixir of Empowerment", "Elixir of Ironskin", "Elixir of Mastery", "Earthen Elixir",
}

# ── ID-anchored consumable recognition (PRIMARY path) ─────────────────────────────────────────
# Buff-aura spell-IDs (the `ability` field of a COMBATANT_INFO aura) → canonical flask name. ID
# keys are STABLE across renames/effect-name displays. ✅ = the ID was read off a live COMBATANT_INFO
# aura in our own reports (certain); the rest are verified against a Wowhead spell= record — re-check
# the exact ID the first time a not-yet-seen flask appears (the name net below also catches it).
FLASK_AURA_IDS = {
    17626: "Flask of the Titans",          # vanilla holdover (+HP)
    17627: "Flask of Distilled Wisdom",    # vanilla holdover (+mana)
    17628: "Flask of Supreme Power",       # ✅ live ("Supreme Power") — +70 spell dmg
    28518: "Flask of Fortification",       # tank — +500 HP, +10 def
    28519: "Flask of Mighty Restoration",  # healer — +25 mp5
    28520: "Flask of Relentless Assault",  # ✅ live — +120 AP
    28521: "Flask of Blinding Light",      # ✅ live — +80 holy/nature/arcane
    28540: "Flask of Pure Death",          # ✅ live — +80 shadow/fire/frost
    42735: "Flask of Chromatic Wonder",    # T5/T6 resist flask — +18 all stats, +35 all resist
}

# Buff-aura spell-ID → (canonical elixir name, slot). The compliance check uses the game-rule
# "2 distinct elixirs = both slots" (not this slot), so `slot` is documentation + future use.
ELIXIR_AURA_IDS = {
    # ── battle (offense slot) ──
    28490: ("Elixir of Major Strength",    "battle"),
    28491: ("Elixir of Healing Power",     "battle"),   # ✅ live ("Healing Power")
    28497: ("Elixir of Major Agility",     "battle"),   # ✅ live ("Mighty Agility")
    28501: ("Elixir of Major Firepower",   "battle"),
    28503: ("Elixir of Major Shadow Power", "battle"),  # ✅ live ("Major Shadow Power")
    33721: ("Adept's Elixir",              "battle"),    # ✅ live ("Spellpower Elixir")
    # ── guardian (defense slot) ──
    11371: ("Gift of Arthas",              "guardian"),  # ✅ live
    28502: ("Elixir of Major Defense",     "guardian"),  # logs as "Major Armor"
    28509: ("Elixir of Major Mageblood",   "guardian"),  # ✅ live ("Greater Versatility")
    39625: ("Elixir of Major Fortitude",   "guardian"),  # ✅ live
    39627: ("Elixir of Draenic Wisdom",    "guardian"),  # ✅ live
}

# Flask EFFECT names (the bare buff name, no "Flask of …" prefix) → canonical flask display name.
# Two jobs: (a) backstop the ID map for flasks that log under a bare effect name (Supreme Power,
# Chromatic Wonder, …) or whose live Anniversary buff-ID differs from the encoded one; (b) — combined
# with the " of Shattrath" suffix in the parser — recognize the Marks-of-Illidari "Shattrath Flask of
# …" line, whose buff logs as "<Effect> of Shattrath" (e.g. "Pure Death of Shattrath", spell 46837)
# and shares NO id/name with the crafted flask. Marks of Illidari drop in T6 raids → common there.
# Keys cover EVERY flask effect (incl. the ones that normally show the "Flask of " prefix) so the
# Shattrath suffix-match below resolves all of them.
FLASK_EFFECT_NAMES = {
    "Relentless Assault": "Flask of Relentless Assault",
    "Blinding Light":     "Flask of Blinding Light",
    "Pure Death":         "Flask of Pure Death",
    "Mighty Restoration": "Flask of Mighty Restoration",
    "Fortification":      "Flask of Fortification",
    "Supreme Power":      "Flask of Supreme Power",       # ✅ live (logs as bare "Supreme Power")
    "Distilled Wisdom":   "Flask of Distilled Wisdom",
    "Chromatic Wonder":   "Flask of Chromatic Wonder",    # the T5/T6 resist flask
}

# Stat-scroll buff-aura spell-IDs → canonical "Scroll of …" display. Scrolls log under a BARE stat
# name ("Agility", "Strength", "Armor", and "Versatility" — the Anniversary rename of Scroll of
# Spirit, 33080) with NO "Scroll of …" prefix, and those names are too generic to match safely by
# name — so scrolls are recognized by ID ONLY. The "V" rank (33077–33082) is the TBC raid scroll;
# the IV rank (12174/12179) is the vanilla holdover. A scroll is a minor positive-only prep extra.
SCROLL_AURA_IDS = {
    12174: "Scroll of Agility",      # ✅ live (rank IV, +17 agi)
    12175: "Scroll of Protection",   # ✅ live (rank IV, +200 armor; logs as "Armor")
    12179: "Scroll of Strength",     # ✅ live (rank IV, +17 str)
    33077: "Scroll of Agility",      # ✅ live (rank V, +20 agi)
    33078: "Scroll of Intellect",    # rank V (+20 int) — same Scroll-V block
    33079: "Scroll of Protection",   # ✅ live (rank V, +300 armor; logs as "Armor")
    33080: "Scroll of Spirit",       # ✅ live (rank V, +30 spi; logs as "Versatility" on Anniversary)
    33081: "Scroll of Stamina",      # rank V (+20 sta) — same Scroll-V block
    33082: "Scroll of Strength",     # ✅ live (rank V, +20 str)
}

# Known self-APPLIED non-consumable auras — suppressed by the consumable canary
# (wcl_fetchers.unrecognized_self_buffs). The canary flags self-sourced pull auras it can't recognize
# as a consumable (a candidate missed flask/elixir/scroll). Consumables are self-applied, so OTHER-
# sourced raid buffs (the bulk — a blessing/brilliance/totem cast ON you) are auto-filtered; this set
# strips what's LEFT that's self-sourced: class self-buffs (forms/stances/armors/aspects/imbues), a
# paladin's OWN blessings/auras cast on himself, and passive gear-proc auras (trinkets/idols). Mostly
# stable across tiers; gear procs are the one part that grows (the canary's job is to surface new ones
# for a 30-sec triage: consumable → add to an ID map, else → add here). Names as the 2.5 client logs.
SELF_BUFF_IGNORE = {
    # warrior stances · shouts (self-sourced copy)
    "Battle Stance", "Defensive Stance", "Berserker Stance", "Battle Shout", "Commanding Shout",
    # druid forms · auras
    "Bear Form", "Dire Bear Form", "Cat Form", "Moonkin Form", "Travel Form", "Aquatic Form",
    "Flight Form", "Swift Flight Form", "Tree of Life", "Leader of the Pack", "Moonkin Aura",
    # paladin auras + a paladin's OWN blessings (self-cast → self-sourced) · seals
    "Devotion Aura", "Retribution Aura", "Concentration Aura", "Sanctity Aura", "Crusader Aura",
    "Fire Resistance Aura", "Frost Resistance Aura", "Shadow Resistance Aura", "Righteous Fury",
    "Blessing of Kings", "Blessing of Might", "Blessing of Wisdom", "Blessing of Salvation",
    "Blessing of Sanctuary", "Blessing of Light", "Blessing of Freedom", "Blessing of Protection",
    "Greater Blessing of Kings", "Greater Blessing of Might", "Greater Blessing of Wisdom",
    "Greater Blessing of Salvation", "Greater Blessing of Sanctuary",
    # mage / warlock / priest self-armor & forms
    "Molten Armor", "Mage Armor", "Ice Armor", "Frost Armor", "Fel Armor", "Demon Armor",
    "Demon Skin", "Soul Link", "Shadowform", "Inner Fire", "Vampiric Embrace",
    # hunter aspects
    "Aspect of the Hawk", "Aspect of the Pack", "Aspect of the Wild", "Aspect of the Monkey",
    "Aspect of the Cheetah", "Aspect of the Viper", "Aspect of the Beast",
    # shaman self imbues / states · self-shields
    "Windfury Weapon", "Flametongue Weapon", "Frostbrand Weapon", "Rockbiter Weapon",
    "Ghost Wolf", "Lightning Shield", "Water Shield",
    # rogue
    "Slice and Dice", "Stealth",
    # raid buffs the provider also gets on himself (self-sourced for the CASTER): mage/priest/druid
    "Arcane Intellect", "Arcane Brilliance",
    "Power Word: Fortitude", "Prayer of Fortitude", "Divine Spirit", "Prayer of Spirit",
    "Shadow Protection", "Prayer of Shadow Protection",
    "Mark of the Wild", "Gift of the Wild", "Thorns",
    # NB JC group-buff necks (Eye of the Night / Chain of the Twilight Owl) are NOT here — they're
    # recognized as real raid utility via GROUP_BUFF_GEAR below (the canary skips them), and CREDITED
    # in the Raider Score, not suppressed as noise.
}

# Gear-provided GROUP buffs — a "use" item that buffs the provider's whole PARTY. Keyed by the AURA
# spell-ID that lands on party members. The PROVIDER (who clicked it) carries the aura SELF-sourced, so
# detection reuses the self-source signal (wcl_fetchers.group_buffs_provided). This is genuine raid
# utility (like a totem), so it's CREDITED positive-only in the Raider Score Utility pillar — not
# suppressed. Extensible: drop a future group-buff item here (a +haste/+crit neck, a relic) and it
# slots into detection + scoring automatically. JC = Jewelcrafting BoP caster necks (Wowhead verified).
GROUP_BUFF_GEAR = {
    31033: {"item": "Eye of the Night",          "label": "+34 spell power (party)"},   # JC neck (item 24116)
    31035: {"item": "Chain of the Twilight Owl",  "label": "+2% spell crit (party)"},    # JC neck (item 24121)
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
    "28515": "Ironshield Potion",   # +2500 armor (item 22849) — defensive/tank combat pot, buff-only
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

# Protective / external abilities cast ON ALLIES — the "saving others" kit. NAME-keyed (WCL uses the
# base name); a cast only counts when its TARGET is another player (self-casts and Environment
# targets are dropped — the latter strips trinket procs like "Blessing of the Silver Crescent" and
# untargeted totems). REACTIVE only: maintained blessings (Kings/Might/Wisdom/Sanctuary/Salvation),
# routine shields (Power Word: Shield, Earth Shield) and already-surfaced utility (Innervate,
# Misdirection — on the Mana/Toolkit cards) are deliberately EXCLUDED so the signal is "clutch help",
# not upkeep. Names VERIFIED live (2026-06-11, Anniversary): it uses "Hand of Protection" / "Hand of
# Salvation", NOT the pre-rename "Blessing of …". Pre-rename variants kept for safety; names absent
# from the probe (Sacrifice, Divine Intervention, Soulstone Resurrection, Pain Suppression, dispels)
# are standard TBC names — they'll match if cast, else no-op.
EXTERNAL_ABILITIES = {
    # ── save: emergency protection / battle-res on an ally ──
    "Hand of Protection":     "save",     # paladin BoP — physical immunity (verified gid 10278)
    "Blessing of Protection": "save",     # pre-rename variant
    "Lay on Hands":           "save",     # paladin full-heal (verified gid 27154; counts only on others)
    "Hand of Sacrifice":      "save",     # paladin damage soak (Anniversary "Hand of" naming)
    "Blessing of Sacrifice":  "save",
    "Divine Intervention":    "save",     # paladin — sacrifice self to save an ally
    "Rebirth":                "save",     # druid combat res (verified gid 34342)
    "Soulstone Resurrection": "save",     # warlock combat res
    "Pain Suppression":       "save",     # priest external damage-reduction CD
    # ── dispel: strip a harmful effect off an ally ──
    "Cleanse":            "dispel",       # paladin (verified gid 39078)
    "Purify":             "dispel",       # priest (disease/poison)
    "Dispel Magic":       "dispel",       # priest (harmful magic off a friendly)
    "Abolish Poison":     "dispel",       # druid (verified gid 2893)
    "Abolish Disease":    "dispel",       # priest
    "Cure Poison":        "dispel",       # shaman
    "Cure Disease":       "dispel",       # shaman
    "Remove Curse":       "dispel",       # mage / druid
    "Remove Lesser Curse":"dispel",       # mage
    "Tranquilizing Shot": "dispel",       # hunter — strip a boss enrage
    # ── utility: reactive help that isn't a heal/dispel ──
    "Hand of Salvation":   "utility",     # paladin threat dump on a pulling DPS (verified gid 1038)
    "Blessing of Freedom": "utility",     # paladin snare/root break on an ally (verified gid 1044)
    "Tremor Totem":        "utility",     # shaman fear/charm/sleep break
}

# Tank defensive cooldowns matched by NAME in the combat log (whole-night, catches CDs popped on
# wipes that the kill-scoped WCL Casts query misses, and needs no spell-ID guessing). Overlaid onto
# the tank scorecard's `cooldowns`. Divine Shield id verified 1020; names match the log exactly.
DEFENSIVE_CD_NAMES = {"Shield Wall", "Last Stand", "Frenzied Regeneration", "Barkskin",
                      "Lay on Hands", "Divine Shield", "Divine Protection"}


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

# ── Mechanic Compliance — the SHARED ability-ID map (backlog #2 + #7) ─────────
# WCL fight name → {abilityGameID: mechanic display name}. The ID-based twin of
# AVOIDABLE_SPELL_NAMES (SAME curated mechanics — keep the two in sync so the WCL headline
# and the combat-log drill-down agree on what counts). Every ID below was VERIFIED against
# live DamageTaken data (scripts/tools/probe_mechanic_ids.py on report J4Ba1j6VAPDmqCFp,
# 2026-06-12) or carried over from the doc-verified table in docs/TBC_RAID_MECHANICS.md §8
# — never from memory. Multiple IDs may map to one mechanic name (rank/variant splits).
# Powers fetch_mechanic_compliance (WCL DamageTaken events per kill fight, zero log) and
# the avoidable-damage WCL fallback when no combat log was transferred.
MECHANIC_IDS = {
    "Hydross the Unstable": {},     # deliberately empty — no curated avoidable mechanic
    "The Lurker Below": {
        37433: "Spout",                      # doc-verified (no hits the probe week)
        37363: "Whirl",                      # live-verified
        37478: "Geyser",                     # live-verified
        37284: "Scalding Water",             # live-verified (melee-mandatory during submerge — exec pillar excludes it)
    },
    "Leotheras the Blind": {
        37641: "Whirlwind",                  # live-verified
        37675: "Chaos Blast",                # live-verified
    },
    "Morogrim Tidewalker": {
        37852: "Watery Grave Explosion",     # live-verified (38028 is the grave AURA, not the splash)
    },
    "Fathom-Lord Karathress": {
        38445: "Sear Nova",                  # live-verified (Caribdis)
    },
    "Lady Vashj": {
        38316: "Entangle",                   # live-verified (Static Charge stays in FF, not here)
    },
    "Void Reaver": {
        34190: "Arcane Orb",                 # live-verified (was (ID unverified) in the doc)
    },
    "High Astromancer Solarian": {
        33009: "Blinding Light",             # live-verified
        42787: "Wrath of the Astromancer",   # live-verified (33045 is the bomb DEBUFF; 42787 is the splash)
    },
    "Al'ar": {
        35383: "Flame Patch",                # live-verified
        34121: "Flame Buffet",               # doc-verified (tank stacks when no melee on platform)
    },
    "Kael'thas Sunstrider": {
        35859: "Nether Vapor",               # live-verified
        35873: "Nether Beam",                # live-verified
        36834: "Arcane Disruption",          # live-verified
        36822: "Shock Barrier",              # live-verified
        37018: "Conflagration",              # live-verified (Capernian)
        36982: "Whirlwind",                  # live-verified (advisor phase)
    },
    "Gruul the Dragonkiller": {
        36240: "Cave In",                    # doc-verified (off-report DST clears stay excluded)
    },
}
