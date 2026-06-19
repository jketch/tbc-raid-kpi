# DPS Spec Performance Framework — per-spec input model

> **STATUS (2026-06-17): v2 BUILT + live-validated, NOT deployed.** The MAINTAIN class (§1) is now
> scored from real uptime via `fetch_maintain_uptime` (WCL Debuffs/Buffs table) → `PERF_MAINTAIN` in
> `template.html`, folded into the overlay. The spec-baseline UTILITY credits below — **Affliction →
> Curse of the Elements, Balance → Improved Faerie Fire, Survival → Expose Weakness** (each scored as
> the debuff/buff UPTIME%) — are wired as PRIMARY facets in `utilFacetsFor`. The archetype tiers were
> RE-DERIVED on the **cost-of-utility** principle (passive/rotational utility → judge on Performance;
> a spec that exists FOR its raid buff → weight Utility): Ret & Shadow → tier 1, Affliction → tier 1,
> Balance → tier 2 (the lone buff-bot). The §7 per-spec `backboneWeight` is the one piece NOT adopted
> (the overlay still blends at the fixed 0.7/0.3 to avoid re-tuning every audited score — a v2.1 item).
> Targets are calibrated to live night-averaged uptime (~65-85% for DoTs, not the idealized 90-95%
> below) — `scripts/tools/probe_maintain_uptime.py`. See `docs/PERF_PILLAR_HANDOFF.md` for the full log.


**Purpose.** The design context for the **input-based** DPS performance metric (replacing parse % as the
Performance-pillar backbone — see the [perf-pillar-input-based-redesign] memory). Instead of grading the
*output* (parse %, which is buff/tempo/boss-only contaminated), we grade the **inputs a raider controls**:
*did you keep your maintenance effects up, fire your cooldowns on time, and stay on-GCD?* This file is the
per-spec catalog of **what to measure** and **what "good" looks like**, for the 13 DPS specs the roster
actually runs.

**Companion docs / code:**
- `docs/TBC_RAID_MECHANICS.md` §2 (why each spec is invited) + §4 (utility ownership) — the *utility* half.
- `dashboard/template.html` → `PERF_ARCHETYPE` / `perfArchetype()` / `PERF_UTIL_TIER1/2` — the archetype
  weighting this metric feeds. **Keep the tiers below in sync with `perfArchetype`.**
- The `tbc-wow-reference` + `wcl-api` skills — game truth and WCL-sourcing truth, respectively.

> **Verification discipline (carried from `TBC_RAID_MECHANICS.md`).** Abilities are referred to by **name**,
> not spell ID — the framework reads `playerSpells` by name and the IDs aren't load-bearing here. Every
> **cadence target is v1 / illustrative** — calibrate against live `playerSpells` (the best performer on a
> clean patchwerk-style kill is a better target than a number typed from memory). Don't encode a cooldown
> second-count as fact until it's confirmed against live data.

---

## 1. The measurement model — three ability classes

Every DPS rotation decomposes into three kinds of action, each measured differently:

| Class | What it is | How to measure | Example |
|---|---|---|---|
| **Maintain (uptime)** | a DoT, debuff, or self-buff that must stay up | **% uptime** vs a target band; refreshes past 100% don't add | Shadow Word: Pain, Slice and Dice, Serpent Sting, Moonfire |
| **On-cooldown (rate)** | a hard-hitting ability gated by its own cooldown | **casts/min** vs theoretical max (cooldown-derived) — i.e. "did you press it every time it was up" | Mind Blast, Bloodthirst, Stormstrike, Judgement, Kill Command |
| **Filler (activity)** | the GCD-filling spam that uses leftover time/resource | folded into the **GCD-uptime floor** (`active_pct`) — a high filler count at high uptime = no dead time | Shadow Bolt, Lightning Bolt, Steady Shot, Sinister Strike, Wrath |

**The backbone = GCD uptime.** `damageBySelection[].active_pct` is the universal floor every spec shares:
were you *doing something* on the GCD. On top of that floor, each spec gets an **overlay** of its maintain
+ on-cooldown abilities (the table in §3). A pure-filler spec (Destro lock) is *almost all backbone*; a
maintenance-heavy spec (Affliction, Shadow) is *mostly overlay*. The metric weights the overlay by how much
of the rotation it represents.

**Scoring shape (mirror the Utility pillar's philosophy — `PERF_FACET_TARGET`):** score each tracked
ability `min(100, value / target)` against an **absolute target**, not a cohort curve. A cohort curve forces
someone to the bottom every week and auto-100s a lone provider. Use `bestF` only as an applicability gate
(an ability nobody could do on a given fight → null, never a damaging 0). Same pattern already proven in
`utilFacetsFor`.

---

## 2. The data already in the pipeline

You do **not** need new fetchers for the v1 backbone — the inputs exist:

| Need | Source key | Notes |
|---|---|---|
| Casts per ability per player | `WEEK_DATA.playerSpells[role][].abilities[]` `{ability, casts}` | from `fetch_role_spell_usage` (WCL Casts events) |
| Role spell usage rollup | `WEEK_DATA.roleSpells[role][]` `{ability, casts, players}` | the cohort view |
| GCD / activity uptime | `WEEK_DATA.damageBySelection[].active_pct` + `durations{all,boss,trash}` | the casts/min **denominator** is here — divide casts by `durations` minutes |
| Per-fight uptime | `damageBySelection[].all/boss/trash.active` | per-selection activity |

**What's missing for a full build (new fetchers, if v2 wants them):**
- **Maintenance *uptime* per DoT/debuff** — `playerSpells` gives cast *count*, not aura uptime. For true
  uptime you need a per-player **Debuffs/Buffs events** band-reconstruction (same shape as `fetch_dispels`
  / Sunder). Casts-per-min is a usable v1 proxy (a DoT refreshed ~once/15s ≈ kept up) but it over-credits
  clipping and can't see a lapsed DoT. **Decide per-spec whether cast-rate is good enough or uptime is
  worth the fetch.** (Self-buffs like Slice and Dice especially want real uptime — clipping SnD is the
  classic rogue mistake a cast-count misses.)
- **casts/min theoretical max** — needs each on-CD ability's cooldown. Derive empirically (best performer)
  or verify the CD live before encoding; don't hardcode from memory.

---

## 3. Per-spec catalog

Grouped by role as the roster runs them. **Tier** = the `perfArchetype` DPS utility tier (dps0 pure-parse /
dps1 utility-flavored / dps2 utility-first) — it sets how much Performance vs Utility weighs, so a tier-2
spec's *input* score matters less to its composite than a tier-0's. **Util** column = the signature utility
that lives in the **Utility** pillar (`utilFacetsFor`), NOT the Performance input — listed so you don't
double-count a debuff as both.

> "Maintain" = uptime-scored. "On-CD" = rate-scored (press on cooldown). "Filler" = folds into the GCD floor.
> Cadence numbers are **v1 — calibrate against live `playerSpells`.**

### Casters

**Mage — Arcane** · roster: Zetla · **tier 0 (pure parse)**
- **On-CD / core:** Arcane Blast (the stacking nuke — the rotation IS managing AB stacks vs mana).
- **Filler:** Frostbolt / Arcane Missiles (the mana-neutral filler between AB sequences); Evocation as a
  mana cooldown (track usage, not a DPS loss if timed).
- **Maintain:** none (no DoT) — this spec is **almost pure backbone**: GCD uptime + AB cadence is ~the whole
  signal. Watch for dead time around mana breaks.
- **Util (separate pillar):** Arcane Brilliance, conjured food/water, Counterspell (interrupt), decurse.
- **Measurable now?** ✅ AB/Frostbolt/AM casts in `playerSpells`. Mana-break dead time shows as low `active_pct`.

**Warlock — Destruction** · roster: Marvels, Seedemup · **tier 0**
- **Filler / core:** Shadow Bolt spam — this is a **near-pure backbone** spec. Cadence of Shadow Bolt at
  high `active_pct` ≈ the whole input score.
- **Maintain:** Immolate (only if running a fire-destro variant — confirm live; many SM/Destro builds are
  pure Shadow Bolt). Curse upkeep is **utility/assigned**, not personal input.
- **Util (separate pillar):** Curse of the Elements / Recklessness (assigned — excluded from individual
  ranking), Improved Shadow Bolt debuff (ISB), healthstones/soulstones.
- **Measurable now?** ✅ Shadow Bolt count. Low overlay → lean hardest on the GCD-uptime backbone here.

**Warlock — Affliction** · roster: Alldorin · **tier 0** (utility rides in the Utility pillar)
- **Maintain (the whole game):** Corruption, Unstable Affliction, Siphon Life, Immolate, Curse of Agony
  (if running it as a personal DPS curse) — **multi-DoT uptime is the metric.** This is the most
  uptime-heavy DPS spec on the roster.
- **Filler:** Shadow Bolt / Drain Soul (execute).
- **Util (separate pillar):** assigned curse (CoE), stones.
- **Measurable now?** ⚠️ cast-rate proxy works but **this spec most wants real DoT uptime** (clipping vs
  lapsing matters). Strong candidate for the Debuffs-events uptime fetch (v2).

**Priest — Shadow** · roster: Shovelpriest · **tier 2 (utility-first)**
- **Maintain:** Vampiric Touch (also the mana battery — utility), Shadow Word: Pain, Devouring Plague,
  Vampiric Embrace (raid heal — utility).
- **On-CD:** Mind Blast (press on cooldown — the priority nuke).
- **Filler:** Mind Flay (channel that fills between Mind Blasts).
- **Util (separate pillar — already wired):** VT mana battery (`mana`), Misery / Shadow Weaving (raid
  shadow amp), dispels. **Tier 2 → personal input is de-weighted vs Utility.**
- **Measurable now?** ✅ rich signal: VT/SWP/DP refresh cadence + Mind Blast rate + Mind Flay filler. Note
  VT/Misery are **also** utility — score them once, in the pillar that's invited-for (Utility here).

**Shaman — Elemental** · roster: Feelsbaldman · **tier 1 (utility-flavored)**
- **Filler / core:** Lightning Bolt spam (the backbone).
- **On-CD:** Chain Lightning (on cooldown / for cleave).
- **Maintain:** Flame Shock (DoT — optional in some builds; confirm live before scoring).
- **Util (separate pillar — already wired):** Totem of Wrath uptime (`tow`, the *reason* you bring an ele).
- **Measurable now?** ✅ LB/CL casts. Tier-1 + the ToW signature mean Utility carries a lot; input is the
  LB-cadence/GCD floor.

**Druid — Balance** · roster: Bahguul · **tier 1**
- **Maintain:** Moonfire, Insect Swarm (two DoTs to keep up).
- **Filler / core:** Wrath / Starfire (the nuke filler — build-dependent).
- **Util (separate pillar):** Improved Faerie Fire (+spell hit — utility), Innervate, Rebirth.
- **Measurable now?** ✅ MF/IS refresh cadence + Wrath/Starfire filler. Like Affliction, the two DoTs are
  the strongest case for real uptime over cast-count if v2 adds it.

### Melee

**Rogue — Combat** · roster: Skeptxo, Cindybrooke · **tier 0**
- **Maintain (self-buff — critical):** Slice and Dice — **clipping or dropping SnD is THE combat-rogue
  mistake**, and a cast-count can't see it. Flag this spec for real **uptime** (Buffs events) over cast-rate.
- **On-CD:** Blade Flurry, Adrenaline Rush (cooldowns — track usage/timing).
- **Builder / filler:** Sinister Strike (the combo-point engine — the bulk of casts) → Eviscerate / Rupture
  finishers. Energy-pooling quality shows as `active_pct`.
- **Util (separate pillar — already wired):** Kick (`intr`), Expose Armor uptime (`expose`, positive-only —
  only the assigned rogue).
- **Measurable now?** ⚠️ SS/Evis/Rupture counts ✅, but **SnD uptime needs the Buffs-events fetch** to be
  honest. v1 proxy: SnD recast cadence.

**Warrior — Fury** · roster: Areso · **tier 0**
- **On-CD (the core):** Bloodthirst + Whirlwind (press both every cooldown — the heart of fury input).
- **Filler / rage dump:** Heroic Strike (on-next-swing rage dump — high count at high rage = good), Slam
  (build-dependent), Execute (sub-20% phase).
- **Util (separate pillar):** Battle Shout uptime (`bshout`), Sunder upkeep (`sunder`).
- **Measurable now?** ✅ BT/WW cast rate vs cooldown is a clean input signal; HS count as the rage-dump proxy.

**Warrior — Arms** · roster: Mmtoast · **tier 0**
- **On-CD (the core):** Mortal Strike + Whirlwind on cooldown.
- **Maintain:** Rend (bleed — also the **Blood Frenzy** carrier; note BF's aura is *untrackable*, see
  `TBC_RAID_MECHANICS.md` §1 — don't try to score a Blood Frenzy uptime).
- **Filler / execute:** Slam, Execute.
- **Util (separate pillar):** Battle Shout, Sunder. (Blood Frenzy is real utility but **unmeasurable** — do
  not invent a slot.)
- **Measurable now?** ✅ MS/WW rate + Rend recast. Don't double-count Rend as both Performance and a
  (nonexistent) BF utility.

**Shaman — Enhancement** · roster: Juricc, Boogiez · **tier 1**
- **On-CD (the core):** Stormstrike on cooldown.
- **Maintain:** Flame Shock / Earth Shock weaving (shock-on-cooldown), Lightning Shield refresh.
- **Filler:** white-swing weaving (shows as `active_pct`); Windfury procs are passive-from-totem.
- **Util (separate pillar — already wired):** Windfury Totem drops (`wf`, the signature) + Unleashed Rage.
- **Measurable now?** ✅ Stormstrike + shock cadence. Tier-1 + WF signature → Utility carries weight.

**Paladin — Retribution** · roster: Philliam · **tier 2 (utility-first)**
- **On-CD / signature:** **Seal twisting** — the `twist` facet already exists (`PERF_FACET_TARGET.twist:5`
  Seal of Command casts/min ≈ actively twisting vs ~0.3 set-and-forget). Judgement on cooldown, Crusader
  Strike on cooldown.
- **Filler:** Consecration (mana permitting), white swings (`active_pct`).
- **Util (separate pillar — already wired):** seal-twist cadence is currently scored as **utility** (`twist`
  in `utilFacetsFor` for `role==="Physical"` paladin). **Decision needed:** is seal-twisting a *utility*
  facet or a *performance* input? It's really execution/rotation quality. Consider moving `twist` into the
  Performance input here and leaving JoW/blessings as the Utility — but Ret is tier-2 so its personal parse
  is already de-weighted; keep the pillars from double-counting it.
- **Measurable now?** ✅ Seal of Command / Judgement / Crusader Strike counts. **Watch the twist double-count.**

### Ranged

**Hunter — Beast Mastery** · roster: Zyph, Mucoid · **tier 0**
- **On-CD:** Kill Command (press when up — the BM signature nuke), Bestial Wrath + Rapid Fire (cooldowns).
- **Core / filler (the "shot rotation"):** Steady Shot woven with auto-shot — **shot-rotation quality is
  the input**, and it shows as `active_pct` + Steady Shot cadence (clipping autos = lost DPS, reads as
  lower effective cadence).
- **Maintain:** Serpent Sting (DoT — keep it up).
- **Util (separate pillar — already wired):** Misdirection / Tranq Shot (`md`).
- **Measurable now?** ✅ Steady Shot + Kill Command + Serpent Sting in `playerSpells`. The shot-weave
  finesse is partly invisible to cast-count — `active_pct` is the best proxy.

**Hunter — Survival** · roster: Jadyx · **tier 1**
- Same shot mechanics as BM: Steady Shot core, Serpent Sting maintain, Kill Command, Multi-Shot (if used).
- **Util (separate pillar):** **Expose Weakness** (on-crit +AP for all physical — the survival signature,
  utility), Misdirection / Tranq.
- **Measurable now?** ✅ as BM. Tier-1 because Expose Weakness amps the physical group — Utility carries more.

---

## 4. Caveats — don't build a metric that punishes good play

1. **Steady-state ≠ real fights.** A "casts/min" target derived from a simulator (wowsims) or a clean
   patchwerk kill is a **ceiling**. Real fights have movement (Vashj static charge, Lurker spout),
   target swaps (council fights, adds), and phases (submerge) that *legitimately* drop cadence. A metric
   that demands sim cadence will mislabel correct situational play as failure. **Use absolute target
   *bands* and the applicability gate**, not exact equality — exactly the `PERF_FACET_TARGET` philosophy.
2. **Per-fight, not per-night, where it matters.** Averaging cadence across a movement-heavy fight and a
   patchwerk fight hides both. The data supports per-selection (`all/boss/trash`) — prefer boss-only and,
   ideally, per-boss for cadence so a single chaotic fight doesn't tank the week.
3. **Cast-count over-credits clipping.** Refreshing a DoT/SnD early inflates the count while *losing* DPS.
   v1 cast-rate is a usable proxy; the specs where this lies most (Affliction, Balance, Combat's SnD) are
   the ones to graduate to real **uptime** (Debuffs/Buffs events) first.
4. **Score each effect in exactly one pillar.** Many signature abilities are *both* a personal input and a
   raid utility (Shadow's VT, ele's nothing-personal totem, Arms' Rend≈BF). The Utility pillar already
   owns the assigned/raid-amp ones via `utilFacetsFor`. **Performance input should cover the
   personal-DPS rotation only** — don't let VT/Misery/Windfury/twist count twice.
5. **GCD uptime is the honest floor.** When in doubt about a spec's overlay, the backbone (`active_pct`)
   is the one signal that's fair across all 13 specs and hard to game. Build the backbone first, ship it,
   then add per-spec overlays where they add signal.

---

## 5. Cross-checking the priority lists (wowsims)

The per-spec **priority/maintain/on-CD/filler** split above is the load-bearing design input, and the
community TBC simulator **`wowsims/tbc-new`** (Go engine, `sim/` per-spec rotations + a serialized APL
preset system) is the best executable cross-check for it — it encodes each spec's optimal priority ordering
and which abilities are maintain vs filler. It is **not** a data source for the dashboard (we read real WCL
casts, not simulated ones), and its cadence is steady-state-on-a-dummy (caveat #1) — use it to **confirm the
ability priority list**, not to set live cadence targets.

> **Next step when building a spec:** pull that spec's APL preset from wowsims, reconcile it against the
> "Maintain / On-CD / Filler" rows above, then map each ability to a `playerSpells` name and an absolute
> target calibrated from the best live performer. Keep this file and `perfArchetype` in sync as you go.

### 5a. How to read a wowsims APL (so you can do the other 11)

Each spec's preset lives at `ui/<class>/<role>/apls/<build>.apl.json` (e.g.
`ui/warlock/dps/apls/affliction.apl.json`, `ui/priest/dps/apls/default.apl.json`). Fetch raw with:

```
gh api repos/wowsims/tbc-new/contents/<path> -H "Accept: application/vnd.github.raw"
```

The `priorityList` is **top-down priority** — the sim casts the first action whose `condition` passes.
Decode the shape:

| APL shape | Means | Our class |
|---|---|---|
| `condition: not dotIsActive(X)` → cast X | keep this DoT up | **maintain** (uptime-scored) |
| `condition: dotRemainingTime < castTime` → cast X | refresh **just before** it falls | **maintain** (the "near-100% but not clipped" tell) |
| bare `castSpell(X)` near the top, no condition | press on cooldown | **on-CD** (rate-scored) |
| bare `castSpell(X)` at the **bottom** | the fallback when nothing else is up | **filler** (GCD floor) |
| `condition: remainingTimePercent <= 5%` | boss-HP gate | **execute** (gate to fights that reach it) |
| `condition: currentManaPercent <= N%` | resource gate | **resource** (Life Tap / Evocation — neutral) |
| `autocastOtherCooldowns` / `castWarlockAssignedCurse` | trinkets / assigned debuff | **utility** (score in the Utility pillar, not here) |
| `channelSpell(X, interruptIf: ...Clip)` | a channel with clip management | **filler**, but the clip logic is a *finesse* signal (invisible to cast-count) |

Ability **names** (not the IDs) are what you wire in — `playerSpells` keys on names. The IDs in the JSON
are wowsims-sourced and corroborated by the per-ability files under `sim/<class>/` (one `.go` per ability),
so the decode below is safe; re-confirm any single ID against Wowhead only if it becomes load-bearing.

---

## 6. Worked configs — Affliction + Shadow (detailed walkthrough)

> **The full machine-ready config for ALL DPS specs is §7** — this section explains the two
> maintenance-heavy examples in depth; §7 is the consolidated table you wire in.

Decoded from the live APLs above. **`kind`** drives the measurement (§1): `maintain` = uptime-scored,
`oncd` = casts/min vs theoretical max, `filler` = folds into the `active_pct` backbone, `execute` = same as
oncd but applicability-gated to fights that reach the HP threshold, `resource` = neutral (mana management),
`utility` = **scored in the Utility pillar, excluded from this Performance input** (don't double-count).
**Targets are v1 — calibrate against live `playerSpells` / the best clean-kill performer.**

```js
// Per-spec rotation config for the input-based Performance metric.
// name = WCL/playerSpells ability name. prio = wowsims APL order (lower = higher priority;
// for `maintain` the order only matters to a future clipping/efficiency metric, not to uptime).
const SPEC_ROTATION = {
  Affliction: {                          // tier dps0 — utility (curse/stones) lives in the Utility pillar
    backboneWeight: 0.35,                // mostly DoT overlay, little pure filler → backbone counts less
    abilities: [
      { name: "Immolate",            kind: "maintain", prio: 1, target: { uptimePct: 90 } },
      { name: "Unstable Affliction", kind: "maintain", prio: 2, target: { uptimePct: 95 } },
      { name: "Corruption",          kind: "maintain", prio: 3, target: { uptimePct: 95 } },
      { name: "Siphon Life",         kind: "maintain", prio: 4, target: { uptimePct: 90 } },
      { name: "Shadow Bolt",         kind: "filler" },
      { name: "Death Coil",          kind: "execute" },   // <5% boss HP
      { name: "Shadowburn",          kind: "execute" },   // <5% boss HP
      { name: "Life Tap",            kind: "resource" },  // <15% mana — neutral
      // Curse of the Elements / assigned curse → utilFacetsFor (excluded), NOT a Performance input
    ],
    notes: "4-DoT uptime IS the score. Strongest case for the Debuffs-events uptime fetch — cast-count " +
           "over-credits clipping and can't see a lapsed DoT.",
  },
  Shadow: {                              // tier dps2 — utility-FIRST; VT/Misery/SW are scored in Utility
    backboneWeight: 0.30,                // DoT + Mind Blast overlay dominates; Mind Flay is the only filler
    abilities: [
      { name: "Shadow Word: Pain",   kind: "maintain", prio: 1, target: { uptimePct: 95 } },
      { name: "Vampiric Touch",      kind: "maintain", prio: 2, target: { uptimePct: 95 },
        note: "ALSO the mana battery — score the UPTIME here, the mana-returned VALUE in Utility (`mana`)." },
      { name: "Mind Blast",          kind: "oncd",     prio: 3, target: { castsPerMin: null /*calibrate*/ } },
      { name: "Shadow Word: Death",  kind: "oncd",     prio: 4, target: { castsPerMin: null } },
      { name: "Devouring Plague",    kind: "oncd",     prio: 5, target: { castsPerMin: null },
        note: "Undead racial — applicability-gate to undead shadow priests, else null (never a 0)." },
      { name: "Mind Flay",           kind: "filler",
        note: "Clip management (interrupt after 2 ticks if a priority spell is up) is a finesse signal " +
              "invisible to cast-count; `active_pct` is the best proxy until a tick-level fetch exists." },
      { name: "Shadowfiend",         kind: "cooldown", note: "mana CD — track usage/timing, not DPS loss." },
      // Misery / Shadow Weaving (raid shadow amp) → Utility pillar. Vampiric Embrace (raid heal) → off-role.
    ],
    notes: "tier-2: personal input is de-weighted vs Utility, so this overlay matters less to the composite " +
           "than a tier-0 spec's. SW:P + VT uptime + Mind Blast on-CD rate are the clean signals.",
  },
};
```

**Two things the APLs confirmed for the framework design:**
- **Uptime ≠ cast-count for VT** — the APL refreshes Vampiric Touch only when `remainingTime < castTime`
  (just-in-time). A good shadow priest shows ~100% VT *uptime* with *few* recasts; a cast-count would rank
  them **below** someone clipping it early. This is the concrete proof that maintenance effects want the
  Debuffs-events uptime fetch, not the v1 cast-rate proxy — promote VT/SW:P (and Affliction's 4 DoTs) first.
- **DoT refresh *priority* is real but orthogonal to uptime** — Affliction refreshes in a fixed order
  (Immolate→UA→Corruption→Siphon Life), which a *clipping/efficiency* metric would use, but a plain uptime
  metric ignores order. Ship uptime first; an efficiency overlay is a later refinement.

---

## 7. Full `SPEC_ROTATION` — every DPS spec (wowsims-derived)

Decoded from each spec's live `*.apl.json` priority list and corroborated against the per-ability
`sim/<class>/*.go` files. **This is the single source of truth for the config** — §6's Affliction/Shadow
are the same data explained. `kind`: `maintain` (uptime-scored) · `oncd` (casts/min vs theoretical max) ·
`filler` (folds into the `active_pct` backbone) · `execute` (oncd, gated to fights reaching the HP%) ·
`cooldown` (usage/timing) · `resource` (mana/energy mgmt — neutral) · `utility` (**scored in the Utility
pillar, NOT this input — listed so you don't double-count**). `backboneWeight` = how much of the score is
the GCD floor vs the spec overlay (backbone-dominant specs like Arcane/Destruction → high; overlay-dominant
like Affliction/FeralCat → low). **All targets v1 — calibrate from live `playerSpells` / the best clean kill.**

```js
const SPEC_ROTATION = {
  // ───────── CASTERS ─────────
  Affliction: { archetype:"dps0", backboneWeight:0.35, abilities:[
    {name:"Immolate",            kind:"maintain", target:{uptimePct:90}},
    {name:"Unstable Affliction", kind:"maintain", target:{uptimePct:95}},
    {name:"Corruption",          kind:"maintain", target:{uptimePct:95}},
    {name:"Siphon Life",         kind:"maintain", target:{uptimePct:90}},
    {name:"Shadow Bolt",         kind:"filler"},
    {name:"Death Coil",          kind:"execute"}, {name:"Shadowburn", kind:"execute"},
    {name:"Life Tap",            kind:"resource"},
    // assigned curse (CoE) → Utility, excluded
  ], notes:"4-DoT uptime IS the score. Best candidate for the Debuffs-events uptime fetch." },

  Destruction: { archetype:"dps0", backboneWeight:0.60, abilities:[
    {name:"Immolate",    kind:"maintain", target:{uptimePct:90}},
    {name:"Shadow Bolt", kind:"filler"},
    {name:"Shadowburn",  kind:"execute"}, {name:"Death Coil", kind:"execute"},
    {name:"Life Tap",    kind:"resource"},
    // assigned curse / Improved Shadow Bolt (ISB) / stones → Utility
  ], notes:"Near-pure backbone: Shadow Bolt spam + one DoT. Destro-fire variant swaps filler→Incinerate." },

  Shadow: { archetype:"dps2", backboneWeight:0.30, abilities:[
    {name:"Shadow Word: Pain",  kind:"maintain", target:{uptimePct:95}},
    {name:"Vampiric Touch",     kind:"maintain", target:{uptimePct:95}, note:"uptime here; mana VALUE → Utility (`mana`)"},
    {name:"Mind Blast",         kind:"oncd", target:{castsPerMin:null}},
    {name:"Shadow Word: Death", kind:"oncd", target:{castsPerMin:null}},
    {name:"Devouring Plague",   kind:"oncd", target:{castsPerMin:null}, note:"undead racial — gate to undead, else null"},
    {name:"Mind Flay",          kind:"filler", note:"clip-after-2-ticks is a finesse signal"},
    {name:"Shadowfiend",        kind:"cooldown"},
  ], notes:"tier-2: input de-weighted vs Utility (VT/Misery/Shadow Weaving live there)." },

  Elemental: { archetype:"dps1", backboneWeight:0.55, abilities:[
    {name:"Lightning Bolt",  kind:"filler"},
    {name:"Chain Lightning", kind:"oncd", target:{castsPerMin:null}},
    {name:"Flame Shock",     kind:"maintain", target:{uptimePct:80}, note:"build-dependent — gate if not run"},
    {name:"Totem of Wrath",  kind:"utility", note:"`tow` uptime — Utility signature"},
  ], notes:"LB cadence backbone + CL on CD. ToW is Utility, not input." },

  Balance: { archetype:"dps1", backboneWeight:0.50, abilities:[
    {name:"Moonfire",        kind:"maintain", target:{uptimePct:90}},
    {name:"Insect Swarm",    kind:"maintain", target:{uptimePct:85}, note:"toggle — gate to specs that run it"},
    {name:"Starfire",        kind:"filler"},
    {name:"Force of Nature", kind:"cooldown", note:"Treants — on CD"},
    {name:"Faerie Fire",     kind:"utility"}, {name:"Innervate", kind:"utility"},
  ], notes:"Starfire backbone + 1–2 DoTs. IFF/Innervate → Utility." },

  Arcane: { archetype:"dps0", backboneWeight:0.70, abilities:[
    {name:"Arcane Blast",     kind:"filler", note:"burn-phase core nuke"},
    {name:"Frostbolt",        kind:"filler", note:"conserve-phase filler (this is an arcane/frost build)"},
    {name:"Fire Blast",       kind:"filler", note:"instant last-GCD filler"},
    {name:"Arcane Power",     kind:"cooldown"}, {name:"Presence of Mind", kind:"cooldown"}, {name:"Icy Veins", kind:"cooldown"},
    {name:"Evocation",        kind:"resource"}, {name:"Mana Gem", kind:"resource"},
  ], notes:"No DoT → almost pure backbone. Burn/conserve; dead time at mana breaks reads as low active_pct." },

  // ───────── MELEE ─────────
  Combat: { archetype:"dps0", backboneWeight:0.50, abilities:[
    {name:"Slice and Dice",  kind:"maintain", target:{uptimePct:95}, note:"self-buff — clipping is THE mistake; wants Buffs-events uptime"},
    {name:"Rupture",         kind:"maintain", target:{uptimePct:80}, note:"optional finisher DoT — gate"},
    {name:"Sinister Strike", kind:"filler", note:"the builder — bulk of casts"},
    {name:"Eviscerate",      kind:"filler", note:"finisher"},
    {name:"Blade Flurry",    kind:"cooldown"}, {name:"Adrenaline Rush", kind:"cooldown"},
    {name:"Expose Armor",    kind:"utility", note:"`expose` — assigned rogue only (positive-only)"},
  ], notes:"SnD uptime headline; SS/Evis cadence backbone." },

  Fury: { archetype:"dps0", backboneWeight:0.45, abilities:[
    {name:"Bloodthirst",  kind:"oncd", target:{castsPerMin:null}},
    {name:"Whirlwind",    kind:"oncd", target:{castsPerMin:null}},
    {name:"Heroic Strike",kind:"filler", note:"on-next-swing rage dump — high count at high rage = good"},
    {name:"Execute",      kind:"execute"},
    {name:"Death Wish",   kind:"cooldown"}, {name:"Recklessness", kind:"cooldown"},
    {name:"Battle Shout", kind:"utility", note:"`bshout` uptime"}, {name:"Sunder Armor", kind:"utility"},
    {name:"Bloodrage",    kind:"resource"},
  ], notes:"BT+WW on CD is the heart; HS as the rage-dump proxy." },

  Arms: { archetype:"dps0", backboneWeight:0.45, abilities:[
    {name:"Mortal Strike", kind:"oncd", target:{castsPerMin:null}},
    {name:"Whirlwind",     kind:"oncd", target:{castsPerMin:null}},
    {name:"Slam",          kind:"filler", note:"2H slam-on-swing-reset rhythm"},
    {name:"Overpower",     kind:"filler", note:"dodge-proc weave (stance dance) — finesse"},
    {name:"Execute",       kind:"execute"},
    {name:"Death Wish",    kind:"cooldown"}, {name:"Recklessness", kind:"cooldown"},
    {name:"Battle Shout",  kind:"utility"}, {name:"Sunder Armor", kind:"utility"}, {name:"Bloodrage", kind:"resource"},
  ], notes:"MS+WW on CD + Slam cadence. The sim APL does NOT maintain Rend; Blood Frenzy's aura is " +
           "UNTRACKABLE (TBC_RAID_MECHANICS §1) — do not score a Rend/BF uptime." },

  Enhancement: { archetype:"dps1", backboneWeight:0.45, abilities:[
    {name:"Stormstrike",     kind:"oncd", target:{castsPerMin:null}},
    {name:"Flame Shock",     kind:"maintain", target:{uptimePct:80}, note:"twist — gate if not run"},
    {name:"Earth Shock",     kind:"filler", note:"filler shock (or Frost Shock)"},
    {name:"Lightning Shield",kind:"maintain", target:{uptimePct:95}, note:"self-buff upkeep"},
    {name:"Windfury Totem",  kind:"utility", note:"`wf` — the signature, Utility"},
    {name:"Shamanistic Rage",kind:"resource"},
  ], notes:"Stormstrike on CD + shock cadence + white-swing weave (active_pct). WF totem twist is Utility." },

  Retribution: { archetype:"dps2", backboneWeight:0.40, abilities:[
    {name:"Crusader Strike", kind:"oncd", target:{castsPerMin:null}},
    {name:"Judgement",       kind:"oncd", target:{castsPerMin:null}},
    {name:"Seal of Blood",   kind:"maintain", target:{uptimePct:95}, note:"primary seal up"},
    {name:"Seal of Command", kind:"oncd", target:{castsPerMin:5}, note:"SEAL-TWIST cadence — see decision in §3"},
    {name:"Consecration",    kind:"filler", note:"mana permitting"}, {name:"Exorcism", kind:"filler", note:"vs undead/demon"},
  ], notes:"tier-2. Seal-twist (`twist`) is currently a UTILITY facet — decide if it belongs in this input (don't double-count)." },

  // ───────── RANGED (one shared hunter APL — sim splits by variable) ─────────
  Hunter: { archetype:"dps0/dps1", backboneWeight:0.60, abilities:[
    {name:"Steady Shot",   kind:"filler", note:"the shot-rotation core — weaving WITHOUT clipping autos IS the skill"},
    {name:"Kill Command",  kind:"oncd", target:{castsPerMin:null}, note:"BM signature — press when up"},
    {name:"Multi-Shot",    kind:"oncd", target:{castsPerMin:null}, note:"if specced/used"},
    {name:"Arcane Shot",   kind:"filler"},
    {name:"Serpent Sting", kind:"maintain", target:{uptimePct:85}, note:"conditional — BM often SKIPS, MM/SV maintain; gate per spec"},
    {name:"Raptor Strike", kind:"filler", note:"melee-weave finesse"},
    {name:"Aspect (Hawk↔Viper)", kind:"resource", note:"mana-management swap"},
  ], notes:"BM=dps0 (Kill Command), Survival=dps1 (Expose Weakness → Utility), MM adds Trueshot Aura. " +
           "Shot-rotation finesse shows mostly as active_pct." },

  // ───────── DPS specs NOT on the current roster (included for completeness) ─────────
  FeralCat: { archetype:"dps1", backboneWeight:0.45, abilities:[
    {name:"Mangle (Cat)",   kind:"maintain", target:{uptimePct:90}, note:"bleed-amp debuff — ALSO raid physical amp (score uptime ONCE)"},
    {name:"Rip",            kind:"maintain", target:{uptimePct:85}, note:"finisher bleed"},
    {name:"Rake",           kind:"maintain", target:{uptimePct:80}, note:"bleed — gate if not run"},
    {name:"Shred",          kind:"filler", note:"the builder — bulk of casts (positional)"},
    {name:"Ferocious Bite", kind:"filler", note:"finisher (CP/energy dump)"},
    {name:"Faerie Fire (Feral)", kind:"utility"},
  ], notes:"Builder(Shred)→finisher(Rip/FB) like a rogue. Mangle uptime is both input AND raid-amp Utility." },

  // Demonology: structurally Destruction-with-2-DoTs — Immolate + Corruption (maintain), Shadow Bolt (filler),
  // Death Coil/Shadowburn (execute), Life Tap (resource). Reuse the Destruction shape + add Corruption.
};
```

### 7a. Primary signal per spec (glanceable)

| Spec | Backbone-vs-overlay | The one thing the metric is really watching |
|---|---|---|
| Arcane | backbone | Arcane Blast cadence + no mana-break dead time |
| Destruction | backbone | Shadow Bolt cadence + Immolate uptime |
| Hunter (BM/MM/SV) | backbone | Steady-Shot weave (active_pct) + Kill Command on CD |
| Elemental | backbone | Lightning Bolt cadence + Chain Lightning on CD |
| Combat | mixed | **Slice and Dice uptime** + Sinister Strike cadence |
| Fury | mixed | Bloodthirst + Whirlwind on CD + HS rage dump |
| Arms | mixed | Mortal Strike + Whirlwind on CD + Slam rhythm |
| Enhancement | mixed | Stormstrike on CD + shock cadence |
| Retribution | mixed | Crusader Strike + Judgement on CD + seal-twist |
| Balance | overlay | Moonfire (+IS) uptime + Starfire filler |
| Shadow | overlay | SW:P + VT uptime + Mind Blast on CD |
| Affliction | overlay | 4-DoT uptime |
| FeralCat | overlay | Mangle/Rip/Rake uptime + Shred cadence |

### 7b. Cross-spec findings from the full decode

- **Two `kind`s reliably lie to a cast-count** and want real uptime first: **self-buffs** (Combat's Slice
  and Dice, Enh's Lightning Shield) and **multi-DoT maintenance** (Affliction ×4, Balance, FeralCat,
  Shadow's VT/SW:P). Promote those to the Debuffs/Buffs-events uptime fetch before the rest.
- **Backbone-dominant specs barely need an overlay.** Arcane, Destruction, Hunter, Elemental are ~60–70%
  GCD-floor — ship them on `active_pct` + one or two on-CD/maintain checks and stop. Don't over-engineer.
- **Don't invent untrackable signals.** Arms' Rend/Blood Frenzy is the canonical trap — the sim doesn't
  maintain it and the BF aura is unlogged. Same caution for any "passive proc" amp.
- **Hunter is one APL for three specs.** Key the config on `Hunter` + branch `archetype`/Serpent-Sting by
  spec (`BeastMastery`/`Marksmanship`/`Survival`); don't build three near-identical entries.
- **Utility overlaps are everywhere** — Mangle (FeralCat), ToW (Ele), Windfury (Enh), Battle Shout
  (warriors), VT (Shadow), Expose (Combat), seal-twist (Ret) are *also* utility facets. The `utility`
  tag in the config marks every one so the Performance input and `utilFacetsFor` can't double-count.
