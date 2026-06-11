# TBC Raid Mechanics — verified reference

**Purpose.** A sourced catalog of the raid-relevant mechanics this dashboard reasons about: why a
high-end raid brings each class/spec, and the exact buffs/auras/debuffs/curses they provide. This is
the **source of truth** for the Utility-pillar design and the per-spec weighting tiers — work from
this, not from memory. (Built after two memory slips: "Curse of Shadow" doesn't exist in TBC, and
Shadow Weaving was initially missed.)

**Provenance.**
- **Debuffs** are anchored to `scripts/wcl_auto_dashboard.py` → `DEBUFF_SLOTS`, whose GUIDs + icons
  are *verified against live TBC 2.5 Debuffs-table data* (the in-repo comment is explicit about this).
  That list is authoritative; this doc must not contradict it.
- **Curse of the Elements** effect confirmed on Wowhead (spell 27228): +10% damage from
  Arcane/Fire/Frost/**Shadow**, −88 resist to those schools. There is **no separate Curse of Shadow**
  in TBC — it was folded into CoE (Curse of Shadow was a Vanilla curse).
- Aura/buff percentages are standard TBC 2.4.3 / 2.5 values (Wowhead / Warcraft Tavern). Where a
  number is load-bearing for scoring, re-verify before hard-coding a weight.

Client = TBC Anniversary "fresh" (2.5.x ≈ 2.4.3 content). T5 = SSC + TK.

---

## 1. The raid debuff economy (authoritative — from `DEBUFF_SLOTS`)

A target carries a limited debuff set; these are the raid-DPS-amplifying ones. Each is tagged with its
**ownership model**, which drives how the Utility index credits it (see §4):

| Debuff | Spell ID(s) | Effect | Provider(s) | Category | Ownership model |
|---|---|---|---|---|---|
| **Curse of the Elements** | 27228 / 27229 | +10% Arcane/Fire/Frost/Shadow dmg taken, −88 resist | Warlock (curse slot) | Magic | **Shared/assigned** → excluded from individual ranking; raid-coverage only |
| **Shadow Weaving** | 15258 | +2% shadow dmg taken per stack, 5 stacks = **+10%** | Shadow Priest (auto-applied by shadow spells) | Magic | **Single-maintainer** → credit holder by uptime, null others |
| **Shadow Vulnerability (ISB)** | 17800 | **+20% shadow dmg taken** | Destro Warlock (procs off Improved Shadow Bolt crits) | Magic | **Single-maintainer (proc)** → soft uptime ceiling |
| **Sunder Armor / Expose Armor** | 25225 / 26866 | armor reduction (Sunder stacks ×5; Expose = one big application) | Warriors (Sunder, collaborative) / Rogue (Expose, exclusive) | Armor | **Collaborative stack** (Sunder) → ramp-speed + 5-stack uptime, no spam bonus |
| **Faerie Fire** | 26993 / 27011 | armor reduction (+3% spell hit if Improved, Balance) | Druid (Balance/Feral) | Armor | **Single-maintainer** → credit holder by uptime, null others |
| **Curse of Recklessness** | 27226 | −800 armor, +AP, target can't flee | Warlock (curse slot) | Armor | **Shared/assigned curse** (competes with CoE for the slot) |
| **Misery** | **33200** | +5% magic dmg taken | Shadow Priest (applied by shadow DoTs) | Magic | **Single-maintainer** |
| **Judgement of Wisdom** | 27164 | attackers restore mana on hit | Paladin (judgement) | Utility | **Single-maintainer** (the assigned judging paladin) |
| **Judgement of the Crusader** | 27159 | +holy dmg taken; powers Seal/Judgement dmg | Paladin (judgement) | Utility | **Single-maintainer (proc/soft)** |

> **Misery added, Blood Frenzy NOT trackable (2026-06-11, both settled against live data).** Misery's
> on-target debuff GUID is **33200** (live-probed; it's a real aura, ~41% uptime in the sample) — *not*
> the talent ID 33198. **Blood Frenzy has no trackable aura at all:** Wowhead flags it "Aura is hidden"
> — it's a passive that bakes +4% physical into Rend/Deep Wounds via a hidden proc. A full 47-aura dump
> of a boss confirmed it: Deep Wounds (12721) is present, but no "Blood Frenzy" aura exists. 29859 is the
> *talent*, never an enemy aura, so a slot would read 0% forever; tracking the carrier (Deep Wounds/Rend)
> over-credits because every warrior applies those regardless of the talent. **Left out by design** — the
> only honest source would be (combatantinfo talent = BF-specced) × Deep-Wounds uptime, and WCL's talent
> data came back unreadable (`specID 0`). **Expose Weakness** (Survival hunter, live GUID **34501**, +AP
> for all physical) is now a tracked slot. **Mortal Strike −50% healing debuff** (30330) is genuine but
> **conditional** — proven valuable this raid only where an enemy actually heals: live enemy-healing dump
> showed **Fathom-Guard Caribdis 131k** (Karathress healer-advisor) and **Kael's Cosmic Infuser 320k+30k**;
> on a non-healing boss (Vashj) its 100% uptime is *meaningless* (Mortal Strike applies the debuff as a
> side effect). So MS healing-reduction must be tracked **per-boss, gated to fights with a healing enemy**
> — not a flat raid-wide slot. Deferred until per-boss conditional slots exist.

## 1c. Situational "good-raider" signals (low-frequency, credit-when-present, never penalize)

Not raid-amp debuffs and not high-uptime — but real discretionary execution/glue. Surfaced in the
full enemy-aura dump:
| Signal | guid | What it means |
|---|---|---|
| **Growl** | 6795 | a taunt landed — pet taunt or, deliberately, a **peel** (bear/pet pulling a mob off a squishy) |
| **Challenging Roar** | 5209 | bear **AoE emergency taunt** — grabbing loose adds off the raid (a reactive save) |
| **Frost Grenade** | 39965 | engineering grenade — active profession utility (damage + snare/stun on adds) |
| **Netherweave Net** | 31367 | thrown net — **root/snare on an add** (tailoring item, anyone) |

These are *exactly* the discretionary plays the Utility/Execution design wants to reward — low uptime
is the expected footprint of a reactive play, **not** noise. Caveat: attribution context matters
(a hunter PET's auto-Growl on its own target ≠ a deliberate peel; a bear Challenging Roar to save the
raid is). Credit lightly with context; absence is never a penalty.

## 1b. Mitigation debuffs (survival side — NOT in the DPS-amp chart)

These reduce *incoming* boss damage; they feed tank Survival / raid health, not raid DPS, so they live
outside `DEBUFF_SLOTS`. Same ownership logic (assigned, non-stacking, credit the holder).

| Debuff | Effect | Providers | Notes |
|---|---|---|---|
| **Attack-power reduction** | boss melee hits softer | **Demoralizing Shout** (Warrior, AoE, live 25203) · **Demoralizing Roar** (Bear, AoE, live 26998) · **Curse of Weakness** (Warlock, single-target) | **All three do NOT stack** — only the largest AP reduction applies. Improved Demo Shout is usually strongest and "free"; **Curse of Weakness wastes the warlock's curse slot** (which wants CoE) → last-resort provider. Hunter pet Screech (−AP) is a *separate* mechanic and stacks. |
| **Curse of Recklessness (caveat)** | −armor **but +AP** | Warlock | Shares the AP-modifier mechanic — can **overwrite Demo Shout and raise boss damage on the tank**. Real "armor-for-DPS vs extra tank damage" tradeoff on hard hitters. |
| **Scorpid Sting** | −5% boss chance to hit (melee/ranged) | Hunter | One sting per hunter → **competes with Serpent Sting (DPS)**. An avoidance/mitigation tool; many bosses immune; situational. |
| **Curse of Tongues** | +cast time on the boss | Warlock | Cast-slow utility (curse slot); fight-specific. |
| **Disarm** | removes weapon (melee dmg drop) | Warrior | **Effectively a non-tool in T5** — raid bosses are generally disarm-immune or don't wield a weapon (disarm does nothing), and disarm-duration is reduced on those it lands on. Don't plan T5 mitigation around it. |

**Curse slot rule (important for warlocks).** A warlock holds **one** curse per target. CoE (magic
amp, caster comps), Curse of Recklessness (armor, physical comps), and Curse of Tongues (boss
cast-slow) are the *raid-utility* curses; Curse of Agony / Curse of Doom are *personal-DPS* curses
(their value is the warlock's own damage → it lands in **Performance**, not Utility). With 3+ warlocks,
curse duty is **assigned** — so a warlock on Agony/Doom is not penalized for not holding CoE, and the
CoE holder isn't double-credited. This is the canonical "assigned responsibility" case.

---

## 2. Why a high-end raid brings each class/spec

For each: **mandate** (the pillar that defines value) and **raid utility** (what they give others).
Util tier: **0** = selfish output, **1** = damage-first with a real raid buff riding along, **2** =
support-first (you invite them *for* the utility; personal parse is secondary).

### Warrior
- **Protection (Tank)** — *mandate:* Survival + mitigation-Execution (Shield Block, high armor,
  Defensive Stance); best physical-mitigation main tank. *Utility:* Commanding Shout (raid HP),
  **Sunder Armor**, Thunder Clap (attack-speed slow), Demoralizing Shout, Spell Reflect. **Tier: Med.**
- **Fury (DPS)** — *mandate:* Performance (top BiS physical DPS). *Utility:* **Battle Shout** (raid AP),
  **Sunder Armor** upkeep, Commanding Shout, emergency off-tank. **Tier: 1.**
- **Arms (DPS)** — *mandate:* Performance + a marquee debuff. *Utility:* **Blood Frenzy** (+4% physical
  dmg taken on the target — a top debuff for physical-cleave comps; **Arms-only**), Battle Shout,
  Sunder, Mortal Strike (healing-reduction). Often invited *for* Blood Frenzy. **Tier: 1 (→2 in a
  physical-heavy comp).**

### Paladin
- **Protection (Tank)** — *mandate:* Survival + mitigation-Execution; **best multi-target/AoE threat**
  tank (Consecration + Holy Shield + Righteous Fury + Blessing of Sanctuary). *Utility:* high for a
  tank — Greater Blessings, Judgement of Wisdom/Light, Blessing of Sanctuary. **Tier: Med-High.**
- **Holy (Healer)** — *mandate:* Performance (efficient single-target/tank healing, Holy Light).
  *Utility:* **the blessing package** (Greater Blessing of Kings/Wisdom/Might/Salvation), **Judgement
  of Light/Wisdom**, Cleanse, **Lay on Hands**, Divine Shield/Protection, Blessing of Protection/
  Sacrifice/Freedom (externals), Divine Intervention. Partly brought *for* blessings. **Tier: High.**
- **Retribution (DPS)** — *mandate:* **Utility-first** (TBC ret personal DPS is low-tier). *Utility:*
  Greater Blessings to the melee group, **Judgement of Wisdom** (caster/healer mana battery),
  **Sanctity Aura** (+10% holy dmg to party, Improved), Improved Blessing of Might, Judgement of the
  Crusader. The classic buff-bot. **Tier: 2.**

### Hunter
- **Beast Mastery (DPS)** — *mandate:* Performance (top raw DPS early-tier). *Utility:* **Misdirection**
  (threat transfer), **Tranquilizing Shot** (removes boss enrage/frenzy — fight-critical), Hunter's
  Mark, Aspect of the Wild (nature resist), **Ferocious Inspiration** (pet, +3% party damage). **Tier: 1.**
- **Marksmanship (DPS)** — adds **Trueshot Aura** (raid AP buff) + **Silencing Shot** (interrupt) on
  top of MD/Tranq. **Tier: 1 (utility-rich).**
- **Survival (DPS)** — *mandate:* Performance + a physical-comp amp. *Utility:* **Expose Weakness**
  (on crit, target takes +AP scaled by the hunter's agility — boosts *all* physical attackers) +
  MD + Tranq. **Tier: 1 (→2 in a physical comp).**

### Rogue
- **Combat (DPS)** — *mandate:* Performance (top-tier sustained physical DPS). *Utility:* **Kick**
  (interrupt), **Expose Armor** (armor debuff alternative to Sunder, if assigned), Blade Flurry
  (cleave). Selfish damage. **Tier: 0** (1 if assigned Expose Armor).

### Priest
- **Holy (Healer)** — *mandate:* Performance (premier raid throughput: Circle of Healing, Prayer of
  Healing, Prayer of Mending). *Utility:* **Fear Ward** (fear immunity — fight-critical; the holy
  signature), dispels, Prayer of Fortitude/Spirit (raid stamina/spirit). **Tier: Med.**
- **Discipline (Healer)** — *mandate:* delivers cooldowns. *Utility:* **Power Infusion** (+20% spell
  haste to one caster — a top mage/lock/healer), **Pain Suppression** (external dmg reduction),
  efficient single-target. **Tier: High.**
- **Shadow (DPS/Hybrid)** — *mandate:* **Utility-first** (personal parse is mid-tier and *understates*
  them). *Utility:* **Vampiric Touch** (party mana battery), **Vampiric Embrace** (raid healing),
  **Misery** (+5% magic dmg taken), **Shadow Weaving** (+10% shadow). The caster-group enabler — most
  of its value shows up in *other* casters' parses. **Tier: 2.**

### Shaman (every shaman brings **Bloodlust/Heroism** — the single biggest raid DPS cooldown)
- **Restoration (Healer)** — *mandate:* Performance (Chain Heal — best group heal in TBC). *Utility:*
  enormous — **Bloodlust**, Mana Spring, Healing Stream, Wrath of Air (caster group +spell dmg),
  **Mana Tide** (Resto talent, group mana). **Tier: High.**
- **Elemental (DPS)** — *mandate:* caster-group enabler. *Utility:* **Totem of Wrath** (+3% spell crit
  & +3% spell hit to the party — defines caster groups), Bloodlust, Wrath of Air, **Unleashed Rage**?
  (no — that's Enhance). **Tier: 2.**
- **Enhancement (DPS)** — *mandate:* melee-group enabler. *Utility:* **Windfury Totem** (huge melee
  AP/attack-speed proc — defines the melee group), **Unleashed Rage** (+10% party AP, Enhance talent),
  Strength of Earth, Grace of Air, Bloodlust. **Tier: 2.**

### Mage
- **Arcane (DPS)** — *mandate:* Performance (top burst/sustained caster). *Utility:* **Arcane
  Intellect/Brilliance** (raid int/mana), **conjured food & water** (raid mana sustain), Counterspell
  (interrupt), Remove Lesser Curse (decurse), Amplify/Dampen Magic, Spellsteal. **Tier: 1** (sustain).
- **Fire (DPS)** — top single-target on many fights; **Improved Scorch** (+fire dmg taken debuff,
  maintained by a fire mage for a fire-comp). **Tier: 1** in a fire comp, else 0.

### Warlock
- **Destruction (DPS)** — *mandate:* Performance (S-tier ST in the shadow-stacked comp + cleave).
  *Utility:* **Curse of the Elements** (or Recklessness, by assignment), **Improved Shadow Bolt**
  (the +20% ISB shadow-vuln debuff), healthstones/soulstones, Shadowfury (AoE stun), banish,
  Soulshatter (threat dump). **Tier: 0.**
- **Affliction (DPS)** — sustained + curse maintenance + Seed of Corruption AoE; same utility family
  (curses are assigned, stones). **Tier: 0.**
- *(Warlock utility = "Support Index": Ritual of Souls + Soulstone applications/battle-res + capped
  healthstones + Soulshatter. NOT raw "stones made" volume. See the Raider-Score memory.)*

### Druid
- **Feral — Bear (Tank)** — *mandate:* Survival + mitigation-Execution (huge armor/HP cushion; great
  for magic and sustained physical). *Utility:* **Mangle** (raid +bleed/physical amp), **Faerie Fire**
  (armor), Innervate, **Rebirth** (battle-res). **Tier: Med.**
- **Feral — Cat (DPS)** — brought largely for **Mangle** uptime (physical amp) + Faerie Fire + Rebirth
  + Innervate. **Tier: 1 (utility-flavored).**
- **Balance (Boomkin)** — *mandate:* caster enabler. *Utility:* **Moonkin Aura** (+5% party spell crit),
  **Improved Faerie Fire** (+3% spell hit), **Innervate** (mana lifeline), **Rebirth**, Insect Swarm,
  decurse. **Tier: 2.**
- **Restoration (Healer)** — *mandate:* Performance (HoT-blanket raid smoothing). *Utility:*
  **Innervate**, **Rebirth**, Tranquility, **Leader of the Pack**? (no — that's Feral), Mark of the
  Wild (raid stats), decurse. **Tier: Med-High.**

---

## 3. Raid-wide buff/aura catalog (non-debuff)

| Buff/Aura | Effect | Source spec | Type |
|---|---|---|---|
| Battle Shout | raid attack power | Warrior | active cast (maintain) |
| Commanding Shout | raid max health | Warrior | active cast |
| Greater Blessing of Kings | +10% stats | Paladin | maintained buff |
| Greater Blessing of Wisdom / Might / Salvation | mp5 / AP / −threat | Paladin | maintained buff |
| Sanctity Aura (Improved) | +10% holy dmg to party | Ret Paladin | aura (passive) |
| Trueshot Aura | raid attack power | MM Hunter | aura (passive) |
| Ferocious Inspiration | +3% party damage | BM Hunter pet | proc aura |
| Moonkin Aura | +5% party spell crit | Balance Druid | aura (passive) |
| Leader of the Pack | +5% party melee/ranged crit | Feral Druid | aura (passive) |
| Mark of the Wild | raid stats/resist | Druid | maintained buff |
| Totem of Wrath | +3% spell crit & +3% spell hit (party) | Ele Shaman | totem |
| Windfury Totem | melee AP/attack-speed proc (party) | Enhance Shaman | totem |
| Wrath of Air Totem | +spell damage (party) | Shaman | totem |
| Strength of Earth / Grace of Air | str / agility (party) | Shaman | totem |
| Mana Spring / Healing Stream | mp5 / hps (party) | Shaman | totem |
| Bloodlust / Heroism | +30% haste, 40s, raid cooldown | Shaman | active cooldown |
| Unleashed Rage | +10% party attack power | Enhance Shaman | proc aura |
| Power Infusion | +20% spell haste, one target | Disc Priest | active cooldown |
| Prayer of Fortitude / Spirit | raid stamina / spirit | Priest | maintained buff |
| Arcane Intellect / Brilliance | int / mana (raid) | Mage | maintained buff |
| Conjured food & water | raid mana/health sustain | Mage | consumable supply |
| Blood Pact | raid stamina (Imp Imp) | Warlock | pet aura |

**Passive vs discretionary** (drives Utility credit — discretionary scores, passive is table-stakes):
- **Passive / table-stakes:** auras (Moonkin, Trueshot, Sanctity, Leader of the Pack), dropped totems
  (ToW, Windfury), maintained buffs (blessings, Int, MotW). You get them by being the class and showing
  up. **Low credit.**
- **Discretionary / reactive:** **Innervate, Rebirth/battle-res, Soulstone-res, debuff maintenance
  (FF / Shadow Weaving / Misery / Sunder ramp), dispels, interrupts, Tranq Shot, Misdirection,
  peel-taunts** — plus two off-role classes worth calling out:
  - **Off-role healing** — a *non-healer* hard-casting a heal to save a teammate (a ret/enhance/ele
    Flash of Light or Healing Wave, a boomkin Healing Touch/Regrowth, a shadow priest swapping to a
    Greater Heal, a clutch PW:Shield). **Clean, high-value, measurable:** WCL healing events by a player
    whose `effective_role` ≠ Healer. **Filter to hard-cast heal spells** — exclude *passive* leech
    (Vampiric Embrace, Judgement of Light, Improved Leader of the Pack procs), which aren't a choice.
  - **Off-role CC / stun-interrupts on adds** — **Hammer of Justice** (Paladin's only interrupt; no
    kick), **Kidney Shot** (Rogue), Intimidating Shout, Fear, Banish, Frost Nova. Raid bosses are
    **stun/CC-immune**, so these do nothing on a boss — their value is **add control + interrupting add
    casts**. Measurability is *partial*: dedicated interrupts (Kick / Counterspell / Pummel /
    Earth Shock) land clean in WCL's Interrupts table; **stun-interrupts log less reliably** — credit
    where detectable, don't penalize absence.

  Attention + selflessness. **This carries the Utility pillar.**

---

## 4. Mapping to the Utility pillar (the design contract)

Three measurement shapes for debuff maintenance:
1. **Single-maintainer uptime** — Faerie Fire, Shadow Weaving, Misery, ISB. Credit the holder by
   uptime; **null** everyone below a contribution floor (assigned elsewhere — never a 0).
2. **Collaborative ramp + 5-stack uptime** — Sunder, Expose Armor. Credit builders/holders by
   **time-to-5-stacks + 5-stack uptime**, *not* application count. No bonus for refreshes past 5.
3. **Excluded** — CoE and the assigned curse family (Recklessness, Tongues): un-rankable, everyone
   benefits equally; raid-coverage tracked, but not individually scored.

Discretionary actions (Innervate, battle-res, dispels, interrupts, peels) are value-weighted and
**capped per action** so volume/stat-padding can't run away. Reward where it plausibly mattered, not
raw spam.

Per-spec **utility tier** (0/1/2 above) sets how much the resulting index counts vs Performance; the
role archetype (DPS/Healer/Tank) sets the other pillar weights. See the `raider-score-kpi-design`
memory for the live weighting model.

---

## Sources
- In-repo `scripts/wcl_auto_dashboard.py` → `DEBUFF_SLOTS` (GUIDs verified against live TBC 2.5 data).
- [Curse of the Elements — Wowhead TBC (spell 27228)](https://www.wowhead.com/tbc/spell=27228/curse-of-the-elements)
- [Curse of Shadow — Wowpedia](https://wowpedia.fandom.com/wiki/Curse_of_Shadow) (Vanilla curse, folded into CoE for TBC)
- [Warlock Curses — Warcraft Tavern (TBC)](https://www.warcrafttavern.com/tbc/guides/warlock-curses/)
- [Buffs & Debuffs by Class — Wowhead TBC](https://www.wowhead.com/tbc/guide/raid-buffs-debuffs-by-class-wow-burning-crusade-classic)

> Aura/buff percentages are standard TBC values; **re-verify any number before encoding it as a weight.**
