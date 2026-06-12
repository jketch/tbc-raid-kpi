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

---

# Part II — The definitive TBC raiding reference

> **Scope note (added 2026-06-12).** Part I (§§1–4 above) is the *authoritative utility/debuff economy* that drives the dashboard's Utility pillar — it stays the source of truth and must not be contradicted. Part II extends the doc into a full TBC raiding reference: combat-math caps, the consumable economy, and a per-tier encounter catalog (T4 → Sunwell) with avoidable-mechanic spell IDs for the Mechanic-Compliance backlog (#2). **Verification discipline carries over:** every spell ID below is tagged ✅ (read off a Wowhead `spell=` record or a Warcraft Wiki inline link) or **(ID unverified)** (named ability, no confirmed ID — key the tracker on the *name*, never a guessed number). The same failure that motivated Part I — a wrong ID reads 0% forever — is why nothing here is guessed into the verified column.

---

## 5. Stat caps & combat-math breakpoints

The numbers a raider must hit before gear/parse comparisons mean anything. All values are vs a **level-73 raid boss** (+3 levels on a level-70 player) unless noted. Rating→% conversions are level-70.

### 5a. Caster caps
| Cap | Value | Notes |
|---|---|---|
| **Spell hit cap** | **16% = 202 spell hit rating** (12.62 rating = 1%) | Base spell miss vs a +3 boss is 16%. Talent hit (Elemental Precision, Shadow Focus, etc.) and the **+3% from Misery / Improved Faerie Fire** subtract directly from the rating you need — a shadow-priest/boomkin in the group can drop a caster's effective cap to **13% ≈ 164 rating**. This is exactly why Misery/IFF coverage is a raid-DPS multiplier, not just a debuff. |
| **Spell crit** | no cap | Totem of Wrath (+3%) and Moonkin Aura (+5%) are additive party crit; ele/boomkin presence shifts the whole caster group's crit floor. |
| **Spell penetration** | situational | Caps useful vs resistance auras; never stack past the target's resist. |

### 5b. Melee / physical caps
| Cap | Value | Notes |
|---|---|---|
| **Special-attack ("yellow") hit cap** | **9% = 142 hit rating** (15.77 rating = 1%) | Removes misses on Sinister Strike, Mortal Strike, Mangle, etc. The first hard target for every melee DPS. |
| **Dodge cap (expertise)** | boss dodge **6.5%**, negated by **~26 expertise (≈103 expertise rating)** | Behind the boss there is no parry, so 26 expertise zeroes the boss's dodge entirely. Front parry (14%) needs **221 expertise rating** — only tanks care. |
| **Dual-wield white-hit cap** | **24%** (8% special + auto-attack penalty) ≈ 6% beyond yellow cap is still white-DPS gain | The 28% raw DW miss is why hit keeps scaling white damage well past 142 rating for rogues/fury/enhance. Yellow cap first, then crit, then more hit for white. |
| **Armor / crush** | see tank block | DPS ignore; tanks below. |

### 5c. Tank survival caps (the load-bearing ones)
| Cap | Value | Why |
|---|---|---|
| **Defense skill** | **490** (350 base + 140) | Reaches the **5.6% crit reduction** vs a +3 boss → **uncrittable**. Below 490 a boss crit can chain into a one-shot; this is the first non-negotiable tank gate. |
| **Uncrushable** | **102.4% combined avoidance** = miss + dodge + parry + **block** | Crushing blows (+50% damage) only come from mobs 3+ levels up. Warriors/paladins reach 102.4% with shield block → no crushes. **Druids cannot block, so a bear can never be uncrushable** — this is the documented reason the dashboard's survival grade *waives the crush penalty for bears* (see §"Tank survivability" in the KPI status table). |
| **Resistance (effective cap)** | **365** vs a level-73 boss | 365 ≈ the point of ~75% average partial mitigation; you can never fully resist (a floor chance of a full hit always remains). Drives the resist-tank gear sets: Hydross (frost **and** nature tanks), Leotheras (~365 fire raid-wide), Mother Shahraz (shadow-resist raid), Felmyst/KJ fire pressure. |

> **Crit/crush model tie-in.** The dashboard's tank `survival` grade (0–100) is built from exactly these signals — uncrittable (def≥490), uncrushable (102.4%), deaths, and defensive-CD usage — because a parse % can't measure mitigation. The caps here are the spec for that grade.

**Sources:** [Wowhead — TBC Stats Overview](https://www.wowhead.com/tbc/guide/classic-the-burning-crusade-stats-overview); [Uncrushable Helper / 490-defense, 102.4% avoidance (CurseForge)](https://www.curseforge.com/wow/addons/uncrushable-helper-tbc); [Spell hit cap 202 — Blizzard forums](https://us.forums.blizzard.com/en/wow/t/whats-the-tbc-spell-hit-rating-cap/2239148); [Rogue Hit & Expertise — Warcraft Tavern](https://www.warcrafttavern.com/tbc/guides/rogue-hit-expertise/).

---

## 6. The consumable economy (Raid-Prep ground truth)

What "tryhard prep" actually means, and the source-of-record for the Raid-Prep 0–10 score. Flasks persist through death (the cost-effective baseline); a flask occupies **both** elixir slots, so it competes with running a battle **and** a guardian elixir. Re-verify exact buff-aura IDs against live COMBATANT_INFO data before encoding any as a weight (Part I's rule).

### 6a. Flasks (both-slot, persist through death)
| Flask | Effect | Who |
|---|---|---|
| **Flask of Relentless Assault** | +120 attack power | Melee / physical DPS, hunters |
| **Flask of Pure Death** | +80 spell damage (shadow/fire/frost — all schools) | Caster DPS |
| **Flask of Blinding Light** | +80 healing / +spell dmg (holy/nature/arcane) | Some healers/casters |
| **Flask of Mighty Restoration** | +25 mp5 | Mana-constrained healers |
| **Flask of Fortification** | +500 HP, +10 defense | Tanks (progression/heavy-hit fights) |
| **Flask of Chromatic Wonder** | +18 all resist, +35 all stats | Resist fights (Leotheras, Hydross, Shahraz) — the "raid-wide resist" flask |

### 6b. Battle elixirs (offense slot)
- **Elixir of Major Agility** (+35 agi, +20 crit rating) — rogues/hunters/enhance/feral. *(Elixir of the Mongoose is a near-identical alternative.)*
- **Elixir of Major Strength** (+35 str) — warriors/ret.
- **Elixir of Major Firepower** / **Adept's Elixir** / **Elixir of Major Shadow Power** — caster schools.
- **Elixir of Healing Power** / **Elixir of Draenic Wisdom** — healers.

### 6c. Guardian elixirs (defensive slot — stacks with a battle elixir if not flasked)
- **Elixir of Major Fortitude** (+250 HP, +10 hp5), **Elixir of Major Defense** (+550 armor), **Elixir of Draenic Wisdom** (+30 int/spi), **Elixir of Major Mageblood** (+16 mp5), **Gift of Arthas** (resist fights).

### 6d. Food buffs (separate slot — stacks with flask + elixirs)
- **Spicy Hot Talbuk / Warp Burger** (+20 hit, +20 crit) — hit-hungry melee.
- **Grilled Mudfish / Blackened Trout** (+20 agi / +20 hit).
- **Crunchy Serpent / Blackened Basilisk** (+23 spell dmg).
- **Golden Fish Sticks / Blackened Sporefish** (+44 healing / +20 spi, +20 stam).
- **Roasted Clefthoof** (+20 str) — warriors/ret.

### 6e. Weapon enhancers (oils / stones / sharpening — own slot)
- **Superior Wizard Oil** (+42 spell dmg) vs **Brilliant Wizard Oil** (+36 spell dmg, +14 crit rating) — casters.
- **Superior Mana Oil** (+14 mp5, +heal) — healers.
- **Adamantite Weightstone / Sharpening Stone** (+12 dmg, +14 crit rating) — physical, on a non-enchanted/non-poisoned weapon.

### 6f. Combat / mid-fight items (the +bonus tier of the 0–10 score — needs the weekly combat log)
- **Combat potions:** Haste Potion (+400 haste 15s), Destruction Potion (+120 spell dmg +crit), Insane Strength, Fel Mana Potion, **Super/Major Mana & Healing Potions** (the standard mid-fight pot).
- **Dark Rune / Demonic Rune** (mana for HP) — casters/healers; counts toward prep bonus.
- **Flame Cap** (+fire dmg / on-use) — fire casters; prep bonus.
- **Drums of Battle** (+80 haste / ~2–5% party haste, 30s) — leatherworker group buff; the dashboard tracks **raw buff count** as `score`, not a %. (Drums of Restoration / Speed are situational variants.)
- **Healthstone / Soulstone** — warlock-supplied; tracked under Panic Button + Saving Others.

> **Mapping to the 0–10 Raid-Prep score:** flask **+4** (= both elixir slots) *or* battle-elixir **+2** + guardian-elixir **+2**; food **+2**; weapon oil **+1**; +1 each for Flame Cap / combat pot / mana rune. Base 7 = COMBATANT_INFO (always works); the +3 bonus needs the combat log. This is the canonical scoring; §6 above is the *catalog* it scores against.

**Sources:** [Wowhead — TBC Raid Consumables Guide](https://www.wowhead.com/tbc/guide/raid-consumables-flasks-elixirs-potions-wow-burning-crusade-classic); [Flask of Relentless Assault (item 22854)](https://www.wowhead.com/tbc/item=22854/flask-of-relentless-assault); [Warcraft Tavern — PvE Consumables](https://www.warcrafttavern.com/tbc/guides/rogue-pve-consumables/).

---

## 7. Encounter catalog — avoidable mechanics, enrage, dispels, interrupts, tanking

Per-boss, in the format the **Mechanic-Compliance** tracker consumes. **Avoidable** = the damaging ability a raider should not eat (the "who stood in it" signal); IDs feed a per-boss `{ability-ID: mechanic}` map (backlog #2). **Tranq** = a frenzy/enrage a hunter soothes (vs a hard berserk timer, which is a DPS check, not a Tranq target). Everything is T5-content patch behaviour (2.4.3 / 2.5.x).

### 7.1 — Tier 4: Karazhan, Gruul's Lair, Magtheridon

**Karazhan**
- **Attumen the Huntsman** — *avoidable:* **Shadow Cleave** ✅`29832` (frontal shadow AoE — face away). *dispel:* **Intangible Presence** ✅`30523` (Curse, −50% hit; decurse). *tank:* tank Midnight + Attumen separately, merge at 25%.
- **Moroes** — *avoidable:* **Garrote** (random bleed after Vanish; clear via immunity) *(ID unverified)*. *enrage:* hard enrage 30% (DPS check). *dispel:* **Blind** is poison-based. *interrupt:* dinner-guest adds (Baroness = shadow priest, kill/lock first). *tank:* 2 tanks — **Gouge** stuns MT and swaps to #2; **Vanish** drops threat. *comp:* ~2 CCs for the 4 adds.
- **Maiden of Virtue** — *avoidable:* **Holy Wrath** ✅`23979` (chains between players — **spread**, doesn't chain to pets); **Holy Ground** ✅`29512` (melee-range holy + silence); **Repentance** ✅`29511` (raid stun, broken by taking damage). *dispel:* **Holy Fire** ✅`29522` (fire Magic DoT). *enrage:* Berserk ✅`45078` @10min.
- **Opera Event** (random of 3): **Wizard of Oz** (kill all 5 → Crone; Strawman takes bonus fire dmg, fear-vulnerable), **Big Bad Wolf** (**Terrifying Howl** AoE fear; "Red Riding Hood" polymorph-kite), **Romulo & Julianne** (interrupt Julianne's **Eternal Affection** heal; both must die within ~10s). All sub-ability IDs *(unverified)*.
- **The Curator** — *avoidable:* **Astral Flares** cast **Arcing Sear** (chains ≤3 targets within 10yd — spread) *(IDs unverified)*. *enrage:* soft @15%, hard @10min. *tank:* **Hateful Bolt** hits #2 non-tank — assign a soaker; **Evocation** phase = +200%+ dmg burst window *(IDs unverified)*.
- **Terestian Illhoof** — *avoidable:* **Sacrifice** ✅`30115` (pulls+stuns a player, ~1500 shadow/s until **Demon Chains** die — burst them). *dispel:* **Amplify Flames** (+fire taken) *(ID unverified)*. *tank:* kill **Kil'rek** for **Broken Pact** (+25% dmg taken), re-kill on respawn. *comp:* warlocks on imp portals.
- **Shade of Aran** — *the interrupt fight.* *avoidable:* **Flame Wreath** ✅`29946` (**do NOT move**); **Circular Blizzard** ✅`29952` (rotating, move with it); **Magnetic Pull→Mass Slow→Charged Arcane Explosion** ✅`30035`/✅`37106` (run out). *dispel:* **Chains of Ice** ✅`29991` (Magic root — dispel fast). *interrupt:* **Arcane Missiles** ✅`31751` (priority) > **Frostbolt** ✅`29954` / **Fireball** ✅`29953`; beware **Area Counterspell** ✅`29961` (casters at max range). *tank:* **cannot be tanked** (tanks go DPS gear; raid needs ≥8k HP for Pyroblast).
- **Netherspite** — *avoidable:* beam-phase **Void Zone** (move out) *(ID unverified)*. *mechanic:* body-block the 3 beams — **Red** (tank soak/aggro), **Green** (healer soak, rotate), **Blue** (DPS soak, swap before ~30 stacks of **Nether Burn**); Banish phase = boss takes/deals double, burn/heal.
- **Chess Event** — no-damage RP fight; move pieces off Medivh's fire-hazard squares. No tanks/dispels/interrupts.
- **Prince Malchezaar** — *avoidable:* **Shadow Nova** ✅`30852` (AoE+knockback, run out); **Enfeeble** ✅`30843` (5 players → 1 HP, any dmg kills — dodge everything while enfeebled); **Infernal** Hellfire patches (move); **Amplify Damage** ✅`39095` (P3, +100% taken). *dispel:* **SW:P** ✅`30898`. *tank:* P2 axes → melee spike + **Sunder Armor** ✅`30901`; P3 axes fly to random raiders.
- **Nightbane** (summoned) — *avoidable:* **Charred Earth**, **Distracting Ash**, **Smoldering Breath**, **Cleave/Tail Sweep**; air phase **Rain of Bones** → kill skeletons *(all IDs unverified)*. *cc:* **Bellowing Roar** AoE fear.

**Gruul's Lair**
- **High King Maulgar** — *council, kill order Priest→Warlock→Mage→Shaman→Maulgar.* *interrupt:* **Blindeye the Seer**'s **Prayer of Healing** (full council heal — dedicate kicks/silences). *tank:* **5 tanks** — **a Mage tanks Krosh Firehand & Spellsteals his Spell Shield**; Kiggler (shaman) ranged-tanked; Maulgar OT handles the post-50% **Intimidating Roar** (tank-stun + fear). *comp:* mandatory Mage, heavy interrupts, dispels, fear tools *(member ability IDs unverified)*.
- **Gruul the Dragonkiller** — *avoidable:* **Cave In** ✅`36240` (telegraphed rock AoE); **Ground Slam** ✅`33525` → **Gronn Lord's Grasp** ✅`33572` (slow, stacks 5) → **Stoned**/**Shatter** ✅`33654` (**spread before Shatter** — dmg scales with proximity); **Reverberation** ✅`36297` (raid silence). *enrage:* **Growth** ✅`36300` (stacking — the DPS race; Drums/BL early). *tank:* **Hurtful Strike** ✅`33813` hits #2 in melee — OT solidly #2, melee stacked Patchwerk-style.

**Magtheridon's Lair**
- **Magtheridon** — *P1 interrupt:* channeler **Dark Mending** ✅`30528` (top kick priority) + **Shadow Bolt Volley** ✅`30510`; **Soul Transfer** ✅`30531` buffs survivors (last channeler dangerous); warlocks **Banish** Burning Abyssals. *P2 avoidable:* **Blast Nova** ✅`30616` (interrupted only by **5 players clicking Manticron Cubes** — rotate, **Mind Exhaustion** ✅`44032` locks repeat clickers; all-cubes = +300% dmg burst); **Quake** ✅`30576` (knockback + 7s cast-interrupt — pre-HoT); **Cleave** ✅`30619` (wall-face); **Conflagration** ✅`30757` (fire ring, move). *P3 (30%):* **Debris** ✅`36449` (raid dmg + stun, then ceiling collapse — keep moving). *enrage:* ✅`37023` @22min. *comp:* warlocks mandatory; 5+ cube-clickers w/ backups; Fear Ward/Tremor.

### 7.2 — Tier 5: Serpentshrine Cavern & Tempest Keep

**Serpentshrine Cavern (SSC)**
- **Hydross the Unstable** — *mechanic:* alternates **frost ↔ nature form** at the line, each switch summons 4 elementals + resets the stacking **Mark of Hydross / Mark of Corruption** (+dmg-taken per stack) → needs **a frost-resist tank AND a nature-resist tank** swapping every transition *(Mark IDs unverified)*. *avoidable:* **Water Tomb** (8yd stun+frost — spread) *(ID unverified)*. *dispel:* **Vile Sludge** (−50% healing/dmg done — isolate) *(ID unverified)*. *enrage:* hard @10min. *comp:* the defining dual-resist-tank fight; raid needs no resist.
- **The Lurker Below** — *avoidable:* **Spout** ✅`37433` (rotating jet + 100yd knockback — get in the water; pets immune); **Geyser** ✅`37478` (random, spread); **Water Bolt** ✅`37138` (only if no one in melee — keep a melee on him); **Whirl** (melee knockback after Spout) *(ID unverified)*. *mechanic:* submerge → CC-able adds (sheep/trap/fear); don't kill the last add during a Spout.
- **Leotheras the Blind** — *avoidable:* **Whirlwind** (humanoid; run away — 15s bleed) *(ID unverified)*; **Chaos Blast** (demon; +1675 fire-taken/stack — warlock-tanked at hitbox edge) *(ID unverified)*. *mechanic:* **Inner Demon** — kill your own demon or be permanently MC'd; alternates forms (threat wipe each). *enrage:* **Berserk @10min** (hard). *comp:* ~365 fire resist + ~18k HP, warlock tank.
- **Fathom-Lord Karathress** — *council.* *interrupt:* **Caribdis** (priest) **Healing Wave** (15s CD — mandatory kick rotation). *avoidable:* **Cataclysmic Bolt** (50% of target max HP — mana-users only, no paladin tanks); **Spitfire Totem** (Tidalvess — kill ASAP); **Tidal Surge** (Caribdis AoE stun) *(IDs unverified)*. *mechanic:* kill adds before pushing boss past 75% (**Blessing of the Tides** = +66% per living advisor). *comp:* Grounding Totem on Tidalvess's Frost Shock.
- **Morogrim Tidewalker** — *avoidable:* **Watery Grave** ✅`38028` (teleports 4 players under waterfalls, ~6k — clear the spots); **Tidal Wave** (frontal, −attack speed); **Earthquake** (raid dmg, triggers murloc waves); **Watery Globules** (≤25%, kite). *adds:* murloc packs after each Earthquake (AoE). *comp:* no resist needed.
- **Lady Vashj** — *the clumping/friendly-fire fight (Static Charge — the dashboard watches it).* *avoidable:* **Static Charge** (~2k/tick to victim + 5yd — **run from raid**); **Shock Blast** (~9k + stun — **Grounding Totem** absorbs); **Entangle** (root — **Blessing of Freedom**); **Forked Lightning** (frontal cone, not resistible) *(IDs unverified)*. *P2:* deactivate 4 generators with **Tainted Cores** from elementals; kill **Tainted Elementals** instantly; kite **Striders** (Panic Aura fear). *P3:* soft enrage via **Toxic Spore Bats**. *comp:* Grounding shaman, Freedom paladins, Strider kiter.

**Tempest Keep: The Eye (TK)**
- **Void Reaver** ("Loot Reaver") — *avoidable:* **Arcane Orb** (random ≥18yd, ~7k + silence, **lands where you stood — move**; numeric ID circulated but **unverified**); **Pounding** (18yd PBAoE channel — melee step out) *(ID unverified)*. *tank:* **Knock Away** drops threat → **3-tank rotation**. *enrage:* @10min (pure DPS check).
- **High Astromancer Solarian** — *avoidable:* **Wrath of the Astromancer** ✅`33045` (bomb +debuff ✅`33044` — **bomb target runs out**); **Blinding Light** ✅`33009` (+**Mark of Solarian** ✅`33023`); **Arcane Missiles** ✅`39414` (not interruptible). *interrupt:* Solarium Priest **Great Heal** ✅`33387`. *mechanic:* split/summon 12 Agents + 2 Priests; P3 Voidwalker (**Void Bolt** ✅`39329`, **Psychic Scream** ✅`34322`).
- **Al'ar** — *avoidable:* **Flame Quills** ✅`34229` (get off the platform); **Dive Bomb** ✅`35181` (P2, clear the impact point — spawns Embers); **Ember Blast** ✅`34341` (Ember death explosion — kill away from raid); **Rebirth** ✅`34342`/`35369`; **Charge** ✅`35412`. *tank:* **Melt Armor** ✅`35410` (−80% armor → tank swap); **Flame Buffet** ✅`34121` (stacks if no one in melee — always keep a tank in). *enrage:* **Berserk** ✅`61632` ~10min P2.
- **Kael'thas Sunstrider** — *the capstone interrupt/positioning fight.* *avoidable:* **Flamestrike** (~big AoE patch — move; ID **unverified**); **Pyroblast** ✅`36819` (P4, 45k+ — **interrupt after Shock Barrier drops**; ⚠ confirm raid vs Magisters' Terrace version); **Arcane Disruption** (raid disorient); **Gravity Lapse** (P5 float — avoid **Nether Beams**) *(IDs unverified)*. *advisors (P1/P3):* **Thaladred** (Gaze — kite), **Sanguinar** (fear), **Capernian** (Conflagration — ranged-only, FR/warlock eats it), **Telonicus** (bombs/Remote Toy). *comp:* fire-resist tank (~150 FR) for Conflagration + Phoenix; ranged-favored; interrupt rotation; the 7 Weapons (P2) are CC/snare-able.

### 7.3 — Tier 6: Mount Hyjal & Black Temple

**Mount Hyjal** (wave-defense → boss)
- **Rage Winterchill** — *avoidable:* **Death and Decay** (~15%/s patch — move); **Frost Nova** (root); **Icebolt** (freeze a target — pre-heal) *(IDs unverified)*. *tank:* spank; spread vs D&D/Nova.
- **Anetheron** — *avoidable:* **Carrion Swarm** (frontal cone, −75% healing taken — face away); **Inferno** (stun + summons Towering Infernal) *(IDs unverified)*. *cc:* **Sleep** (3 targets, **not** WotF/Tremor/trinket-removable — damage to wake). *tank:* **Vampiric Aura** (heals 300% of melee dealt — mitigation cuts his healing).
- **Kaz'rogal** — *soft enrage:* **Mark of Kaz'rogal** (drains mana → explodes at 0; CD shrinks each cast — mana classes burn it off early). *avoidable:* **War Stomp** (AoE stun), **Cripple** *(IDs unverified)*. *tank:* **Malevolent Cleave** (frontal split). *comp:* the classic "bring fewer mana users" fight.
- **Azgalor** — *avoidable:* **Rain of Fire** (move/spread); **Doom** (45s, kills target + spawns **Lesser Doomguard** — Soulstone + OT) *(IDs unverified)*. *raid silence:* **Howl of Azgalor** (resistable — Shadow Resistance helps). *tank:* **Cleave** (face away).
- **Archimonde** — *execution/positioning capstone.* *avoidable:* **Air Burst** (knock-up — **Tears of the Goddess** slow-fall buff before pull); **Doomfire** (wandering fire trail — kite); **Finger of Death** (kills ranged/healer if no one in melee — keep bodies on boss) *(IDs unverified)*. *dispel:* **Grip of the Legion** (Fire DoT — **decurse** immediately). *cc:* **Fear** (~40s — Tremor/Fear Ward; a feared player runs into Doomfire/off the cliff). *snowball:* **Soul Charge** — each death deals class-flavored raid dmg → minimize deaths.

**Black Temple**
- **High Warlord Naj'entus** — *avoidable:* **Needle Spine** ✅`39835` (random + frost splash to ≤6yd — **spread**; targeting helper ✅`39992`). *mechanic:* **Impaling Spine** ✅`39837` (stun+bleed — ally clicks dropped spine to free + collect it); **Tidal Shield** (immune/self-heal until a spine is hurled → raid-wide **Tidal Burst** ~8.5k — top off first). *enrage:* ~8min hard. *tank:* single, no swap — difficulty is spine/splash discipline.
- **Supremus** — *avoidable:* **Volcanic Geyser** ✅`42052` (eruption under players — move); **Molten Flame** fire lines (dodge; **note 39849 = "Throw Glaive", NOT this** — ID unverified). *P2:* **Gaze/Fixate** — boss chases a random raider (kite) *(ID unverified)*. *tank:* Hateful-style P1 threat; no tank in P2.
- **Shade of Akama** — *add/interrupt fight.* *interrupt:* **Ashtongue Spiritbinders/Elementalists** **Spirit Heal** (~8–10k — kick/stun) *(IDs unverified)*. *adds:* **Sorcerers** re-shackle the Shade (kill on timer); **Defenders** Shield-Bash casters (add-tank); stealthed **Rogues**. *comp:* heavy interrupt breadth (good interrupt-KPI fit).
- **Teron Gorefiend** — *signature:* **Shadow of Death** (kills a random raider → **Vengeful Spirit** ghost + 4 **Constructs**; ghost uses **Spirit Lance** ✅`40157`/Chains/Volley — failure wipes the raid). *avoidable:* **Doom Blossom** (AoE blossoms — kill/spread); **Incinerate** *(IDs unverified)*. *dispel:* **Crushing Shadows** (+shadow taken). *comp:* practiced ghost rotation.
- **Gurtogg Bloodboil** — *avoidable:* **Bloodboil** (hits the **furthest** players, stacking shadow DoT — rotate positioning); **Fel Acid Breath** ✅`40508` (frontal); **Arcing Smash** (frontal) *(ID unverified)*. *mechanic:* **Fel Rage** ✅`40604` (fixates a random non-tank, +250% HP — that player **becomes the de-facto tank**, heal-swap not taunt; **Insignificance** zeroes threat). *comp:* healers instantly pivot to the Fel Rage target.
- **Reliquary of Souls** — *3 Essences.* **Suffering (P1):** **Aura of Suffering** (no-heal phase — tank on stamina/CDs); **Frenzy** (**Tranq/soothe**); **Soul Drain** (dispel ~3). **Desire (P2):** **Spirit Shock** (~17k + confuse); **Deaden** (+100% dmg taken — heal through); damage **reflect** (throttle DPS). **Anger (P3):** **Aura of Anger** (ramping — soft enrage, burn fast); **Spite** ✅`41376` (immune→detonate — spread); **Seethe** (threat ramp). *(non-✅ IDs unverified)*.
- **Mother Shahraz** — *the shadow-resist raid fight.* *signature:* **Fatal Attraction** (teleports ~3 players together, beams deal escalating shadow to allies ≤15yd — run to 3 pre-assigned corners; **emulator-cited 41001 not confirmed → ID unverified**). *avoidable:* **Silencing Shriek** (≤18yd silence — resistable); **Sinful Beam** (arcs to a 2nd target) *(IDs unverified)*. *raid dmg:* **Prismatic Aura** cycles school vulnerabilities — constant magic dmg. *tank:* **Saber Lash** (frontal ~32k **split among 2–3 stacked tanks** — if one dies the survivor is one-shot). *comp:* **shadow-resist gear on the whole raid except tanks**.
- **The Illidari Council** — *4 bosses, shared HP, kill together.* *interrupt:* **Lady Malande** (priest) **Circle of Healing** (must interrupt) + **Reflective Shield** (reflects half absorbed — stop hitting); **High Nethermancer Zerevor** (mage — interrupt/LoS; **Blizzard/Flamestrike** patches, move). *tank:* **Gathios** (pally — dispel his **Hammer of Justice**/blessings); **Veras** (rogue — vanishes, **dispel Deadly Poison**). *comp:* interrupt rotation on Malande + Zerevor, poison/magic dispels, even DPS *(member IDs unverified)*.
- **Illidan Stormrage** — *5 phases.* *tank:* **Shear** (lethal tank hit — bears dodge-form / shield-block / CDs). *avoidable:* **Flame Crash** (fire patch under MT — step off); **Parasitic Shadowfiend** (spreads + summons adds — **spread**); **Agonizing Flames** (spread); **Eye Beam** (P2 sweep — move out of path); **Shadow Prison** (P4 demon — **do NOT move or take massive dmg**) *(IDs unverified)*. *P2:* **Flames of Azzinoth** — two adds **warlock-tanked / fire-resist**, leashed to their glaives (enrage if pulled too far). *comp:* Flame tanking (2 warlocks) is the headline; Shear CD rotation; freeze during Shadow Prison. *(Illidan himself has no Tranq frenzy — the Flames' enrage is a leash, not a soothe.)*

### 7.4 — Sunwell Plateau

- **Kalecgos (+ Sathrovarr)** — *two-realm.* *avoidable:* **Spectral Blast** (~5k arcane + ports to Spectral Realm — spread); **Arcane Buffet** (stacking +arcane-taken, cleared only by entering the realm/immunity) *(IDs unverified)*; **Tail Lash** (rear stun). *dispel:* **Curse of Boundless Agony** ✅`45032` (Curse, doubles every 5s, **jumps on dispel/expiry — time it onto a healthy target**). *enrage:* soft when **either** boss hits 10% → both must die ~together. *comp:* **portal rotation is the whole fight**.
- **Brutallus** — *pure DPS race, hard enrage.* *avoidable:* **Meteor Slash** ✅`45150` (frontal ~20k **split** + stacking fire-vuln — **two melee stacks alternate** as stacks decay); **Burn** ✅`46394` (spreads — burned player runs out). *tank:* **Stomp** ✅`45185` (−50% armor, also clears Burn). *enrage:* **Berserk** ✅`26662` @**6min** (~4.7M HP gear check).
- **Felmyst** — *avoidable (ground):* **Gas Nova** ✅`45855` (dispellable — move/dispel); **Encapsulate** ✅`45662` (lifts a player, ~3.5k/s to ≤20yd — **spread**); **Noxious Fumes** ✅`47002` (passive raid tax). *air phase:* **Demonic Vapor** ✅`45402` (gas line — run perpendicular); **Fog of Corruption** ✅`45717` (**MCs anyone caught for the fight — avoid the fog**). *tank:* **Corrosion** ✅`45866` (+100% physical taken → swap/mitigate). *enrage:* ✅`46587` (+500% @10min).
- **The Eredar Twins (Sacrolash & Alythess)** — *shared HP, kill Sacrolash first.* *avoidable:* **Conflagration** ✅`45342` (random, ~16k fire + confuse — run out, trinket); **Shadow Nova** ✅`45329` (intentionally eaten to clear **Flame Touched** stacks); **Flame Sear** ✅`46771` (3–5 players, fire DoT — spread). *dispel/steal:* **Pyrogenics** (+35% her fire — **purge/Spellsteal off her**) *(ID unverified)*. *tank:* **Confounding Blow** (confuses Sacrolash's tank — swap) *(ID unverified)*. *enrage:* **Berserk** ✅`26662`.
- **M'uru (+ Entropius)** — *the add/healing capstone (IDs largely unverified — confirm vs live WCL).* *avoidable:* **Negative Energy** (beam to 4–5 targets/s — raid tax); **Darkness** void zones spawning **Dark Fiends** (~5k explosion — **purge/dispel/AoE** them); **Singularity** void zones (Entropius — move). *adds:* **Shadowsword Berserkers/Fury Mages** (CC casters; **interrupt** Fury Mage); **Void Sentinel** → 8 **Void Spawns** (snare/fear). *setup:* prot-pally on humanoids + warlock group on Sentinels. *enrage:* shared ~10min. *comp:* shadow resist eases the raid tax.
- **Kil'jaeden** — *5 phases, **Dragon Orbs** to survive the ultimate.* *avoidable:* **Fire Bloom** ✅`45641` (5 players, fire to ≤10yd — spread); **Legion Lightning** ✅`45664` (chains 5, drains mana — spread); **Flame Dart** ✅`45737` (P3 raid-wide); **Armageddon** ✅`45909` (P4 meteor markers — move off; impact `45915`); **Shadow Spike** (orbs, −50% healing — dodge) *(ID unverified)*. *ultimate:* **Darkness of a Thousand Souls** ✅`46605` (raid-wide ~50k, **not resistible — countered only by an empowered Shield of the Blue** from a Dragon Orb). *tank:* **Soul Flay** ✅`45442` (channel on top threat — steady tax). *P3+:* **Sinister Reflection** (4 mirror images — not CC-able, **interruptible**, tank+kill); P5 **Sacrifice of Anveena** (+25% holy taken, final burn window).

---

## 8. Master mechanic-ID table (verified — for the Mechanic-Compliance map, backlog #2)

Every ✅ ID below was read off a Wowhead `spell=` record or a Warcraft Wiki inline link. These are safe to wire into a per-boss `{ability-ID: mechanic}` map *today*; the avoidable-damage `DamageTaken`-by-ID query keys on these. Abilities marked unverified in §7 are deliberately omitted — key those on the ability **name** until a live WCL `DamageTaken` probe confirms the ID.

> **⚡ = LIVE-verified** (scripts/tools/probe_mechanic_ids.py on report J4Ba1j6VAPDmqCFp, 2026-06-12) — read
> straight from DamageTaken events on real kills, the strongest verification tier. Note two aura-vs-splash
> splits the live probe exposed: Morogrim's damage event is **37852** (38028 is the grave aura) and
> Solarian's bomb damage is **42787** (33045 is the carried debuff). The wired subset lives in
> `game_constants.MECHANIC_IDS` (curated parity with `AVOIDABLE_SPELL_NAMES`).

| Tier | Boss | Avoidable / key ability | Spell ID |
|---|---|---|---|
| T4 | Attumen | Shadow Cleave | 29832 |
| T4 | Maiden | Holy Wrath (chain) | 23979 |
| T4 | Maiden | Holy Ground | 29512 |
| T4 | Maiden | Repentance | 29511 |
| T4 | Maiden | Holy Fire (dispel) | 29522 |
| T4 | Shade of Aran | Flame Wreath | 29946 |
| T4 | Shade of Aran | Circular Blizzard | 29952 |
| T4 | Shade of Aran | Charged Arcane Explosion | 37106 |
| T4 | Terestian | Sacrifice | 30115 |
| T4 | Prince | Shadow Nova | 30852 |
| T4 | Prince | Enfeeble | 30843 |
| T4 | Prince | Amplify Damage | 39095 |
| T4 | Gruul | Cave In | 36240 |
| T4 | Gruul | Ground Slam | 33525 |
| T4 | Gruul | Shatter | 33654 |
| T4 | Gruul | Hurtful Strike | 33813 |
| T4 | Gruul | Growth (soft enrage) | 36300 |
| T4 | Magtheridon | Blast Nova | 30616 |
| T4 | Magtheridon | Quake | 30576 |
| T4 | Magtheridon | Conflagration | 30757 |
| T4 | Magtheridon | Debris (P3) | 36449 |
| T4 | Magtheridon | Dark Mending (interrupt) | 30528 |
| T5 | Lurker Below | Spout | 37433 |
| T5 | Lurker Below | Geyser | 37478 |
| T5 | Lurker Below | Water Bolt | 37138 |
| T5 | Lurker Below | Whirl ⚡ | 37363 |
| T5 | Lurker Below | Scalding Water ⚡ | 37284 |
| T5 | Leotheras | Whirlwind ⚡ | 37641 |
| T5 | Leotheras | Chaos Blast ⚡ | 37675 |
| T5 | Morogrim | Watery Grave (aura) | 38028 |
| T5 | Morogrim | Watery Grave Explosion (the splash) ⚡ | 37852 |
| T5 | Karathress | Sear Nova (Caribdis) ⚡ | 38445 |
| T5 | Vashj | Entangle ⚡ | 38316 |
| T5 | Vashj | Static Charge (FF-tracked) ⚡ | 38281 |
| T5 | Void Reaver | Arcane Orb ⚡ | 34190 |
| T5 | Solarian | Wrath of the Astromancer (bomb DEBUFF) | 33045 |
| T5 | Solarian | Wrath of the Astromancer (the splash) ⚡ | 42787 |
| T5 | Solarian | Blinding Light | 33009 |
| T5 | Solarian | Arcane Missiles | 39414 |
| T5 | Al'ar | Flame Patch ⚡ | 35383 |
| T5 | Kael'thas | Nether Vapor ⚡ | 35859 |
| T5 | Kael'thas | Nether Beam ⚡ | 35873 |
| T5 | Kael'thas | Arcane Disruption ⚡ | 36834 |
| T5 | Kael'thas | Shock Barrier ⚡ | 36822 |
| T5 | Kael'thas | Conflagration (Capernian) ⚡ | 37018 |
| T5 | Kael'thas | Whirlwind (advisor phase) ⚡ | 36982 |
| T5 | Al'ar | Flame Quills | 34229 |
| T5 | Al'ar | Dive Bomb | 35181 |
| T5 | Al'ar | Ember Blast | 34341 |
| T5 | Al'ar | Melt Armor (tank swap) | 35410 |
| T5 | Al'ar | Flame Buffet | 34121 |
| T5 | Kael'thas | Pyroblast (⚠ confirm raid vs MgT) | 36819 |
| T6 | Naj'entus | Needle Spine | 39835 |
| T6 | Naj'entus | Impaling Spine | 39837 |
| T6 | Supremus | Volcanic Geyser | 42052 |
| T6 | Teron | Spirit Lance (ghost) | 40157 |
| T6 | Gurtogg | Fel Rage | 40604 |
| T6 | Gurtogg | Fel Acid Breath | 40508 |
| T6 | Reliquary | Spite (Anger) | 41376 |
| SW | Kalecgos | Curse of Boundless Agony (dispel) | 45032 |
| SW | Brutallus | Meteor Slash | 45150 |
| SW | Brutallus | Burn | 46394 |
| SW | Brutallus | Stomp | 45185 |
| SW | Felmyst | Encapsulate | 45662 |
| SW | Felmyst | Gas Nova | 45855 |
| SW | Felmyst | Corrosion (tank) | 45866 |
| SW | Felmyst | Fog of Corruption | 45717 |
| SW | Twins | Conflagration | 45342 |
| SW | Twins | Shadow Nova | 45329 |
| SW | Twins | Flame Sear | 46771 |
| SW | Kil'jaeden | Fire Bloom | 45641 |
| SW | Kil'jaeden | Legion Lightning | 45664 |
| SW | Kil'jaeden | Flame Dart | 45737 |
| SW | Kil'jaeden | Armageddon | 45909 |
| SW | Kil'jaeden | Darkness of a Thousand Souls | 46605 |
| SW | Kil'jaeden | Soul Flay (tank) | 45442 |

> **Build note (backlog #2).** This table is the seed for the shared ID-based spell map that unifies **Avoidable Damage** (currently name-based `AVOIDABLE_SPELL_NAMES`) with **Mechanic Compliance**. Add `fetch_mechanic_compliance` querying WCL `DamageTaken` per kill filtered to these IDs → per-player hits/dmg → `WEEK_DATA.mechanicCompliance` (clone `renderDebuffCoverage`). Then retire the name-based path (log = drill-down). For the **(ID unverified)** abilities, run a one-time WCL `DamageTaken`-by-ability probe on a kill of that boss to read the live ability ID, then promote it into this table — same discipline that built `DEBUFF_SLOTS`.

---

## Sources (Part II)

**Caps & combat math:** [Wowhead — TBC Stats Overview](https://www.wowhead.com/tbc/guide/classic-the-burning-crusade-stats-overview) · [Uncrushable Helper (CurseForge)](https://www.curseforge.com/wow/addons/uncrushable-helper-tbc) · [Spell-hit-cap 202 (Blizzard forums)](https://us.forums.blizzard.com/en/wow/t/whats-the-tbc-spell-hit-rating-cap/2239148) · [Rogue Hit & Expertise (Warcraft Tavern)](https://www.warcrafttavern.com/tbc/guides/rogue-hit-expertise/).

**Consumables:** [Wowhead — Raid Consumables Guide](https://www.wowhead.com/tbc/guide/raid-consumables-flasks-elixirs-potions-wow-burning-crusade-classic) · [Flask of Relentless Assault](https://www.wowhead.com/tbc/item=22854/flask-of-relentless-assault) · [Warcraft Tavern — PvE Consumables](https://www.warcrafttavern.com/tbc/guides/rogue-pve-consumables/).

**T4 encounters:** Wowhead TBC guides ([Maiden](https://www.wowhead.com/tbc/guide/maiden-virtue-karazhan-strategy-burning-crusade-classic), [Shade of Aran](https://www.wowhead.com/tbc/guide/shade-aran-karazhan-strategy-burning-crusade-classic), [Gruul](https://www.wowhead.com/tbc/guide/gruul-dragonkiller-gruuls-lair-strategy-burning-crusade-classic), [Magtheridon](https://www.wowhead.com/tbc/guide/magtheridon-magtheridons-lair-strategy-burning-crusade-classic), [Maulgar](https://www.wowhead.com/tbc/guide/high-king-maulgar-gruuls-lair-strategy-burning-crusade-classic)) · Warcraft Wiki ([Prince Malchezaar](https://warcraft.wiki.gg/wiki/Prince_Malchezaar), [The Curator](https://warcraft.wiki.gg/wiki/The_Curator), [Attumen](https://warcraft.wiki.gg/wiki/Attumen_the_Huntsman), [Terestian](https://warcraft.wiki.gg/wiki/Terestian_Illhoof), [Maulgar](https://warcraft.wiki.gg/wiki/High_King_Maulgar)).

**T5 encounters:** Warcraft Wiki ([Hydross](https://warcraft.wiki.gg/wiki/Hydross), [Lurker](https://warcraft.wiki.gg/wiki/The_Lurker_Below), [Leotheras](https://warcraft.wiki.gg/wiki/Leotheras_the_Blind), [Karathress](https://warcraft.wiki.gg/wiki/Fathom-Lord_Karathress), [Morogrim](https://warcraft.wiki.gg/wiki/Morogrim_Tidewalker), [Vashj](https://warcraft.wiki.gg/wiki/Lady_Vashj_(tactics)), [Void Reaver](https://warcraft.wiki.gg/wiki/Void_Reaver), [Solarian](https://warcraft.wiki.gg/wiki/High_Astromancer_Solarian), [Al'ar](https://warcraft.wiki.gg/wiki/Al'ar), [Kael'thas](https://warcraft.wiki.gg/wiki/Kael%27thas_Sunstrider_(tactics))).

**T6 encounters:** Warcraft Wiki / Icy Veins / Warcraft Tavern per boss ([Archimonde](https://www.icy-veins.com/tbc-classic/archimonde-guide-strategy-abilities-loot), [Naj'entus](https://www.icy-veins.com/tbc-classic/high-warlord-naj-entus-guide-strategy-abilities-loot), [Reliquary](https://warcraft.wiki.gg/wiki/Reliquary_of_Souls), [Mother Shahraz](https://warcraft.wiki.gg/wiki/Mother_Shahraz), [Illidari Council](https://warcraft.wiki.gg/wiki/Illidari_Council), [Illidan](https://www.wowhead.com/tbc/guide/illidan-stormrage-black-temple-bt-strategy-burning-crusade-classic)); verified spell IDs from Wowhead `spell=` pages (39835/39837/42052/40157/40604/40508/41376).

**Sunwell encounters:** Warcraft Wiki ([Kalecgos](https://warcraft.wiki.gg/wiki/Kalecgos_(tactics)), [Brutallus](https://warcraft.wiki.gg/wiki/Brutallus), [Felmyst](https://warcraft.wiki.gg/wiki/Felmyst), [Eredar Twins](https://warcraft.wiki.gg/wiki/Eredar_Twins), [M'uru](https://warcraft.wiki.gg/wiki/M%27uru_(tactics)), [Kil'jaeden](https://warcraft.wiki.gg/wiki/Kil%27jaeden_(tactics))); verified spell IDs from Wowhead `spell=` pages (45032/45150/46394/45185/45662/45855/45866/45717/45342/45329/46771/45641/45664/45737/45909/46605/45442).

> **Standing rule (unchanged from Part I):** these are aggregated-guide + wiki values for 2.4.3 content. Where a number becomes a load-bearing weight or an ID becomes a tracked slot, **re-verify against live WCL/Wowhead before encoding it** — the verified-ID discipline is the whole reason this doc is trustworthy.

---

# Part III — Per-spec stat weights, professions & gearing

> **Scope note (added 2026-06-12).** Part III adds the *per-spec optimization* layer the doc lacked:
> stat-priority hierarchies, profession picks, gemming/enchant priorities, and iconic trinkets — for
> all raiding specs. It is the gear-side companion to §5 (the caps these priorities chase) and Part I
> (the raid-utility each spec brings). **Not a BiS list** (item drops are patch/phase-volatile) — it's
> the *decision framework*: what to cap, what to stack, what to gem/enchant, and why. Values are
> standard 2.4.3 / Anniversary; **re-verify a number before encoding it as a weight.** Reductions from
> raid buffs (Misery/IFF/ToW spell-hit, Draenei +1%) shift gear targets — they're noted inline.

## 9. Universal gearing constants

These recur across every spec below (full derivations in §5):

- **Melee/ranged special ("yellow") hit cap:** 9% = **142 hit rating** (15.77 rating = 1%).
- **Dodge cap (from behind):** ~26 expertise = **~103 expertise rating** (3.94 rating/expertise).
- **Dual-wield white cap:** 24% (hard) — DW specs eat white misses; never itemized to it.
- **Spell hit cap:** 16% = **202 spell hit rating** (12.62 rating = 1%); talent/raid-debuff reductions below.
- **Tank survival:** Defense **490** = uncrittable; **102.4%** avoidance = uncrushable (block required → bears never uncrushable).
- **Meta gems:** physical → **Relentless Earthstorm Diamond** (3 STR/+1% crit dmg, 2R/2Y/2B); crit casters → **Chaotic Skyfire Diamond** (12 SP/+3% crit dmg, needs 2 blue); healers → **Insightful** (Int+mana proc) or **Bracing Earthstorm** (+26 heal); tanks → **Powerful Earthstorm** (Sta, early) → **Relentless** (threat, later).
- **Dominant colored gem by archetype:** STR melee → Bold Living Ruby (8 STR); AGI melee/hunter → Delicate Living Ruby (8 AGI); caster → Runed Living Ruby (SP; **Brilliant/Int for Arcane**); healer → Teardrop/Brilliant (+heal); tank → Solid Star of Elune (Sta). Hit gems (Veiled Nightseye caster / Glinting-Rigid physical) used *surgically* to land exactly on cap.

## 10. Melee & hunter physical DPS

> Across all: Hit→142, Expertise→~103, then the spec's scaling stat. Weapon enchant is **Mongoose**
> (1H; agi+crit+haste proc) for nearly everyone; legs **Nethercobra Leg Armor**; head **Glyph of
> Ferocity**; shoulder **Greater Inscription of Vengeance** (Aldor). Near-universal trinket pool:
> **Dragonspine Trophy** (BiS AP+haste proc), **Bloodlust Brooch**, **Hourglass of the Unraveller**,
> **Tsunami Talisman** (raw AP — best for warriors/feral). With a Boomkin (IFF) + Draenei, gear hit can
> drop ~3–4%.

| Spec | Stat priority | Notable specifics |
|---|---|---|
| **Fury Warrior** | Hit 142 → Exp 26 → **Crit** → STR/AP → ArP(late) → Haste | White-DW reliant so hit is very strong; crit drives Flurry. JC/Eng/LW professions. Bold Ruby gems. |
| **Arms Warrior** | Hit 142 → Exp 26 → **Crit** → STR/AP → **Armor Pen** | 2H, less white-dependent; ArP is its scaling angle. Weapon = **Savagery (+70 AP)** or Executioner. Tsunami Talisman strong. |
| **Combat Rogue** | Hit 142(yellow) → Exp 26 → **Agility** → Crit → Haste → AP | **White hit keeps scaling past 9%** (Combat Potency + poison apps) → often ~300+ hit. Dominant raid rogue. **Bandit's Insignia** top sustained trinket. Delicate Ruby. |
| **Mutilate Rogue** | Hit (Precision +5% → less gear hit) → Exp 26 → **Agility** → Crit | Daggers; niche vs Combat. Human dagger expertise cap lower. |
| **Enhance Shaman** | Hit 142 (mostly from talents) → Exp 26 → **STR/AP** → Agi/Crit → Haste | Brings Windfury/SoE/Bloodlust. **Eng+LW** standard; Enchanting (ring stats) strong. **Avoid Blacksmithing** (slow MH bad for OH). Bold Ruby. |
| **Ret Paladin** | **Exp first** (rare on ret gear) → Hit 142 (95 w/ Precision) → **STR** → Crit(~30% for Vengeance) → Haste | Seal of Blood top seal; mana via Spiritual Attunement. Bold Ruby. |
| **Feral Cat** | Hit 9% → Exp 26 → **Agility** → Crit/AP → **Armor Pen** | **LW signature** (instant Greater Drums while shifted). Often **Wolfshead Helm** (no meta). Weapon = **Adamantite Weightstone** kept up. **Tsunami Talisman** BiS. Delicate Ruby. |
| **BM Hunter** | Hit 142 → **Agility** → ArP → AP → Crit → Haste | Dominant raid hunter (pet share + Ferocious Inspiration). Eng-loving. Scope = Stabilized Eternium. |
| **MM Hunter** | Hit 142 → **Agility** → ArP → AP → Crit | Brings **Trueshot Aura**; itemization ≈ BM. |
| **Survival Hunter** | Hit (Surefooted +3% → ~6% gear) → **Agility above all** | **Expose Weakness** converts the hunter's agility into raid-wide AP → SV agility is the most valuable in the game; stack it relentlessly. |

## 11. Caster DPS

> **Spell hit cap 16% = 202 rating**, lowered by: **Misery / Improved Faerie Fire = +3% each** (don't
> stack with each other → effective gear cap ~13% ≈ **164**), **Totem of Wrath +3%**, **Draenei
> Presence +1%**, plus per-spec talents below. Meta = **Chaotic Skyfire Diamond** (2 blue). Dominant
> gem = **Runed Living Ruby** (SP); **Veiled Nightseye** to finish hit. Universal enchants: head
> **Arcanum of Power**, shoulder **Greater Inscription of the Orb** (Aldor), gloves **Major Spellpower**,
> legs **Runic Spellthread**, chest **Exceptional Stats**, rings **Spellpower** (enchanter), weapon
> **Soulfrost** (shadow/frost) or **Sunfire** (fire/arcane).

| Spec | Talented hit reduction → gear hit | Stat priority | Notable specifics |
|---|---|---|---|
| **Arcane Mage** | none → full **202** | Hit → **Intellect** (mana engine) → SP → Crit → Haste | Int gemmed (Brilliant Ruby) — unique among casters. **Spellfire** tailoring set. Glyph of Power head (+hit). |
| **Fire Mage** | none → **202** | Hit → SP → **Crit** (Ignite/Combustion) → Haste | Scales up into later tiers. **The Lightning Capacitor** / **Hex Shrunken Head** crit trinkets. Pure SP gems. |
| **Frost Mage** | none → 202 | Hit → SP → Crit/Haste | Off-meta; AoE/utility niches only. Weapon **Soulfrost**. |
| **Destro Warlock** | Suppression is **Affliction-only** → still **202** | Hit → SP → **Crit** (Ruin = 200% crit) → Haste | Chaotic meta + Ruin = **9% effective** crit dmg — huge. **Voidheart (T4)** 4-pc DoT extend. Soulfrost/Sunfire by damage type. |
| **Affliction Warlock** | Suppression −10% on DoTs → **~76 for DoTs** (full for Shadow Bolt filler) | Hit → **SP** (DoTs only scale with SP — no crit/haste) → Haste → Crit | Brings Shadow Embrace + CoE. Pure SP gems. Lightning Capacitor weak here (low crit). |
| **Shadow Priest** | Shadow Focus −10% → **~76 gear** | **SP** → Hit → Crit → Haste | Brings **Misery (+5% magic, +3% raid hit)** + **VT mana battery** — near-mandatory utility. **Avatar Regalia (T5)** = damage set. |
| **Elemental Shaman** | Elem Precision −3% + Nature's Guidance −3% (+ToW −3%) → **~3–4% gear** | Hit → SP → **Crit** (Elemental Fury 200% + Focus mana-refund) → Haste | Easiest hit of any caster; **provides ToW**. **The Lightning Capacitor** signature/often-BiS. |
| **Balance Druid** | Balance of Power −4% → **152** (→ ~7–8% w/ raid debuff) | Hit → SP → **Crit** (Vengeance) → Haste | **Provides IFF (+3% raid hit) + Moonkin Aura (+5% raid crit)**. Big raid-buff value. |

## 12. Healers

> **Healers do NOT stack spell hit** (heals can't miss). Universal enchants: weapon/gloves **Major
> Healing**, rings **Healing Power** (enchanter), legs **Golden Spellthread**, head **Arcanum of
> Renewal**, shoulder **Greater Inscription of Faith** (Aldor). Near-universal trinket pool: **Lower
> City Prayerbook** (on-cast +heal burst — top easy pickup), **Eye of the Dead** (heroic), **Essence of
> the Martyr** (badge), **Bangle of Endless Blessings** (early regen).

| Spec | Stat priority | Engine / dominant spell | Specifics |
|---|---|---|---|
| **Holy Priest** | +Heal → **Spirit** → Haste → Crit → mp5 → Int | Raid AoE — **Circle of Healing / PoH / PoM** | Spirit double-dips (Meditation regen + Spiritual Guidance throughput). Meta **Insightful**. |
| **Disc Priest** | +Heal → Haste → Spirit → mp5 → Crit | Tank single-target + **Power Infusion** + PW:Shield | Same template as Holy, more haste/throughput-leaning. |
| **Resto Shaman** | +Heal → **Haste** → mp5 → Crit → Int | **Chain Heal** (smart bounce) | Spirit near-worthless (no spirit-regen talents). Haste exceptional (long casts). Meta **Bracing Earthstorm** (+26 heal). |
| **Resto Druid** | +Heal → Haste → Spirit → mp5 → Int | **HoT blanket** — Lifebloom/Rejuv/Regrowth | **Druid heals CANNOT crit — skip crit entirely** (the big gotcha). Chest = **Major Spirit**. Never gem crit. |
| **Holy Paladin** | **Intellect** → +Heal → **Crit/mp5** → Haste | Mana-efficient **Holy Light** tank spam | Outlier: Int is #1 (mana pool + Holy Guidance), and **Crit is a regen stat via Illumination** (crits refund ~60% base mana). Meta **Insightful**. **Darkmoon Card: Blue Dragon** can fully sustain mana. |

## 13. Tanks — three distinct threat models

> All three cap **Defense 490** (uncrittable) first. **Warriors & Paladins reach uncrushable via block
> (102.4%); a Feral bear cannot block or parry → never uncrushable**, surviving crushes through massive
> armor + HP instead (Survival of the Fittest gives bears +3% crit reduction, so they hit uncrittable
> from only **~2.6% / ~154 def rating**). Resilience from PvP off-pieces is a budget route to crit
> immunity (~39.4 resi = 1% vs ~59 def rating). Meta **Powerful Earthstorm** (Sta) early → **Relentless**
> (threat) late; **Solid Star of Elune** (Sta) default gem, deviating only to *reach* a survival cap.
> Gloves **+2% Threat**, legs **Nethercleft Leg Armor**, boots **Boar's Speed**.

| | Crush immune? | Headline threat stat | Top threat ability | Weapon enchant | Professions |
|---|---|---|---|---|---|
| **Prot Warrior** | Yes — Shield Block on demand + 102.4% | **Hit → Expertise → Block Value** | **Shield Slam** (scales w/ Block Value) | Mongoose (P1) / Savagery (threat) | BS+Eng (or BS+JC) |
| **Prot Paladin** | Yes — passive 102.4% (Holy Shield block) | **Spell Damage / Spell Hit** → Sta | Consecration / Holy Shield / Judgement (scale w/ **spell power**) | **Superior Wizard Oil (+42 SP)** | **Eng+Enchanting** (ring spellpower) |
| **Feral Bear** | **No** — armor + HP instead | **Expertise → Agility → AP/Crit** | **Mangle** (scales w/ its damage) | **2H Major Agility** (Mongoose is 1H-only) | **LW+Eng** |

**Threat-model detail.** Warrior threat is mostly *flat innate values* that scale poorly with offense — hence Hit→Expertise→**Shield Block Value** (1 Block Value = +1 Shield Slam damage). Paladin threat scales with **spell damage** (Consecration/Holy Shield/Righteous-Fury seals) — the stat that separates a pally from the other tanks; they wear caster spell-power neck/rings/weapon-oil for threat. Bear threat **is** damage (every threat ability scales with the damage it deals), so AGI/AP/crit drive TPS and **hit/expertise capping is NOT a goal** (they're just AP-equivalent). Notable tank trinkets: **Dabiri's Enigma** (def+sta, guaranteed quest — premier early), **Adamantine Figurine** (on-use armor spike-soak), **Moroes' Lucky Pocket Watch** (dodge), **Mark of the Champion** (150 AP — bear threat), **Darkmoon Card: Vengeance** (warrior). An **off-tank Fury-prot threat set** (DPS plate, dual-wield, deep enough for Shield Slam) is deliberately *not* itemized to 490/uncrushable — used only on low-incoming-damage fights.

---

## Sources (Part III)

**Universal/caps & professions:** [Wowhead — Stats Overview](https://www.wowhead.com/tbc/guide/classic-the-burning-crusade-stats-overview) · [Professions Overview](https://www.wowhead.com/tbc/guide/professions-burning-crusade-classic) · [Jewelcrafting Overview](https://www.wowhead.com/tbc/guide/professions/jewelcrafting-overview).

**Melee/hunter:** Icy Veins TBC stat-priority guides ([Fury](https://www.icy-veins.com/tbc-classic/fury-warrior-dps-pve-stat-priority), [Arms](https://www.icy-veins.com/tbc-classic/arms-warrior-dps-pve-stat-priority), [Combat Rogue](https://www.icy-veins.com/tbc-classic/rogue-dps-pve-stat-priority), [Enhance](https://www.icy-veins.com/tbc-classic/enhancement-shaman-dps-pve-stat-priority), [Ret](https://www.icy-veins.com/tbc-classic/retribution-paladin-dps-pve-stat-priority), [Feral](https://www.icy-veins.com/tbc-classic/feral-druid-dps-pve-stat-priority), [BM](https://www.icy-veins.com/tbc-classic/beast-mastery-hunter-dps-pve-stat-priority)/[MM](https://www.icy-veins.com/tbc-classic/marksmanship-hunter-dps-pve-stat-priority)/[SV](https://www.icy-veins.com/tbc-classic/survival-hunter-dps-pve-stat-priority)) · [Warcraft Tavern — Rogue Hit & Expertise](https://www.warcrafttavern.com/tbc/guides/rogue-hit-expertise/) · [Dragonspine Trophy](https://www.wowhead.com/tbc/item=28830/dragonspine-trophy).

**Caster:** Wowhead class gems/stat pages ([Mage](https://www.wowhead.com/tbc/guide/classes/mage/dps-stat-priority-attributes-pve), [Warlock](https://www.wowhead.com/tbc/guide/classes/warlock/dps-stat-priority-attributes-pve), [Shadow Priest](https://www.wowhead.com/tbc/guide/classes/priest/shadow/dps-stat-priority-attributes-pve), [Ele Shaman](https://www.wowhead.com/tbc/guide/classes/shaman/elemental/dps-stat-priority-attributes-pve), [Balance](https://www.wowhead.com/tbc/guide/classes/druid/balance/dps-stat-priority-attributes-pve)) · [The Lightning Capacitor](https://www.wowhead.com/tbc/item=28785/the-lightning-capacitor) · [Improved Faerie Fire](https://www.wowhead.com/tbc/spell=33602/improved-faerie-fire).

**Healers:** Wowhead healer pages ([Priest](https://www.wowhead.com/tbc/guide/classes/priest/healer-stat-priority-attributes-pve), [Resto Shaman](https://www.wowhead.com/tbc/guide/classes/shaman/healer-stat-priority-attributes-pve), [Resto Druid](https://www.wowhead.com/tbc/guide/classes/druid/healer-enchants-gems-pve), [Holy Paladin](https://www.wowhead.com/tbc/guide/classes/paladin/holy/healer-stat-priority-attributes-pve)) · [Lower City Prayerbook](https://www.wowhead.com/tbc/item=30841/lower-city-prayerbook).

**Tanks:** Wowhead tank pages ([Prot Warrior](https://www.wowhead.com/tbc/guide/classes/warrior/protection/tank-stat-priority-attributes-pve), [Prot Paladin](https://www.wowhead.com/tbc/guide/classes/paladin/tank-stat-priority-attributes-pve), [Feral Bear](https://www.wowhead.com/tbc/guide/classes/druid/feral/tank-stat-priority-attributes-pve)) · [Warcraft Tavern — Prot Warrior caps](https://www.warcrafttavern.com/tbc/guides/pve-protection-warrior-tank-stat-priority/) · [Dabiri's Enigma](https://www.wowhead.com/tbc/item=30300/dabiris-enigma) · [Adamantine Figurine](https://www.wowhead.com/tbc/item=27891/adamantine-figurine).

> **Verify-flags carried from research:** exact gear-side hit for Ele/Boomkin shifts with which raid
> debuff (Misery vs IFF vs ToW) is present; the 490/485 heroic-defense split and a couple of tank
> enchant slots are cross-source consensus, not a single table; later-phase gems (Pyrestone/Shadowsong
> Amethyst) substitute for the Nightseye/Topaz equivalents quoted. As always — re-confirm any number
> before it becomes an encoded weight.
