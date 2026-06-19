# Performance Pillar — Handoff (resume in a fresh session)

**Purpose.** Continue the Raider-Score **Performance pillar** redesign. v1.5 is built + offline-verified
(not deployed). Remaining: a v2 uptime fetch + a tier/utility-credit redesign. This doc is the single
entry point — read it, then `docs/DPS_SPEC_PERFORMANCE_FRAMEWORK.md` (the per-spec build sheet) and the
`perf-pillar-input-based-redesign` memory.

**START HERE → open decisions / first tasks are at the bottom (§F).**

---

## Status snapshot (2026-06-17)
- **★ v2 BUILT + LIVE-VALIDATED, NOT deployed (2026-06-17).** The maintain-uptime fetch, the §C
  spec-baseline utility credits, and the tier re-derivation are all done and proven on live WCL.
  Gates green: `python scripts/check.py` (330 + characterization) + `node --test tests/perf_scoring.test.mjs`
  (14) + ruff. Live site still runs the **old parse-based** Performance pillar — **nothing below is deployed.**
  What landed this session:
  - **`fetch_maintain_uptime`** (`wcl_fetchers.py`) — per-player DoT/self-buff UPTIME% + the spec-baseline
    utility debuffs, pure WCL (Casts prepass → batched Debuffs/Buffs *table*). **Live gotcha LOCKED:** the
    Debuffs table returns **0** when `sourceID` + `hostilityType:Enemies` are combined — use `sourceID`
    ALONE (the source's tracked debuffs are enemy-only anyway). Wired through facade → `wcl[maintain_uptime]`
    → `week_schema.maintainUptime` (drift-guard) → `week_map` → `WEEK_DATA.maintainUptime` {name:{ability:pct}}.
  - **MAINTAIN overlay** (`PERF_MAINTAIN` in template.html) — DoT/self-buff uptime folds into the SAME
    overlay as `PERF_ONCD`, blended under activity at the existing `PERF_CORE_W` (0.3). Gated optionals drop
    out below ⅓·target (ele Flame Shock at 0.4% correctly drops). **Guarded:** applies ONLY when the player
    is in `maintainUptime`, so an old snapshot without the section stays exact-v1.5 (byte-safe).
  - **§C utility credits** — affli `coe` (CoE uptime), boomkin `iff` (Faerie Fire uptime), survival
    `exposew` (Expose Weakness uptime) as PRIMARY facets in `utilFacetsFor`/`PERF_FACET_TARGET`. Live: affli
    CoE 95.7% → util 100, boomkin Faerie Fire 88.8% → util 100.
  - **Tiers RE-DERIVED** on cost-of-utility: Ret & Shadow tier2→1, Affliction 0→1, Balance 1→2.
    `PERF_UTIL_TIER2={Balance}` (the lone buff-bot), dps2 shrank.
  - **VT double-count DECIDED** — conscious split: VT *uptime* → Performance maintain, VT *mana value* →
    Utility (`mana`). Two metrics of one ability (framework §6 intent).
  - **Targets calibrated** to 2 live weeks (`scripts/tools/probe_maintain_uptime.py` — kept, read-only recon).
    Night-averaged DoT uptime runs ~65-85% (movement fights), NOT the idealized 90-95% — targets reflect that. **v1, recalibrate as the roster shifts.**
- **OPEN CALLS made this session (maintainer can veto):** (a) **Shadow → dps1** — the handoff move-list
  named only Ret/Boomkin/Affli, but §C explicitly says the dps2 rationale dissolved for "Ret, **Shadow**";
  moved it by the same principle. (b) the overlay still blends at the FIXED 0.7/0.3 — per-spec
  `backboneWeight` (framework §7) was NOT adopted (it would re-tune every spec's audited v1.5 score);
  flagged as a v2.1 refinement.
- **Deploy = `reprocess.py dashboard/raid_kpi_dashboard.html` (re-skin) → `scripts/publish.py`** when ready.
  NOTE: re-skinning a CACHED snapshot blanks `maintainUptime` (snapshots predate the fetch) → those weeks
  read v1.5 (the guard handles it). Only a fresh `run_weekly` pull populates the v2 overlay for real.
- **Per-player audit COMPLETE** — every DPS spec walked with the maintainer; calibration baked into v1.5/v2.

---

## A. What v1.5 does (built)
**The model — DPS Performance =** `0.7·activity% + 0.3·on-CD cadence + engineering bonus`. Healers/tanks
unchanged (HPS parse / threat+survival).
- **Activity backbone** = WCL `activeTime` ÷ summed fight duration (boss+trash), per `damageBySelection`.
  Buff/tempo-independent. The honest floor every spec shares.
- **On-CD cadence overlay** (`PERF_ONCD` in template.html) — rate-scored `min(100, cpm/target)`, averaged
  over a spec's on-CD abilities, blended at 0.3. Current table + audit-set targets:
  - Fury: Bloodthirst@3 + Whirlwind@2 · Arms: Mortal Strike@2.5 + Whirlwind@2 (both rage-gated → lenient)
  - Enhancement: Stormstrike@5 · Shadow: Mind Blast@3 · Ret: Crusader Strike@3 + Judgment@2.5
  - Hunter (BM/SV/MM): Kill Command@3 · Elemental: Chain Lightning@2.5
- **Fillers fold into the backbone** (Shadow Bolt, Steady Shot, Lightning Bolt, Sinister Strike, Starfire,
  Arcane Blast) — NOT scored.
- **Maintain class deferred to v2** (DoT/self-buff uptime) — Affliction / Balance / Combat are
  **activity-only** now, flagged in the brief (`PERF_MAINTAIN_V2`).
- **Parse % → context only** (shown in the brief, not scored). **Engineering** (sapper/bomb) = positive-only
  bonus. **Fill-in DPS judged by `effective_role`** (Moojerked/Blunderdin → DPS, hybrid-annotated).
- **Hunter Steady:Auto ratio** = a shown diagnostic (not scored — it's haste-driven; rotationtools link).
- **No double-counting** (verified): every on-CD ability is distinct from every Utility facet. Seal-twist →
  Utility only; CS/Judgment → Performance only. The one v2 watch-item is **Vampiric Touch** (uptime→Perf,
  mana-value→Util — intentional per framework §6, but make it a conscious call in v2).

---

## B. v2 — the remaining build (needs LIVE WCL; can't be proofed offline)
**One new fetcher serves TWO consumers:** a WCL **Debuffs/Buffs-events band-reconstruction uptime fetch**
(same shape as `fetch_dispels` / Sunder). Build per `DPS_SPEC_PERFORMANCE_FRAMEWORK.md` §2 ("what's missing")
+ §7 (`SPEC_ROTATION`).
1. **Maintain class for Performance** — DoT/self-buff *uptime*: Affliction's 4 DoTs, Balance Moonfire/Insect
   Swarm, **Combat Slice and Dice** (clipping is THE rogue mistake a cast-count misses), Shadow SW:P + VT.
   Then apply per-spec `backboneWeight` (framework §7) — until maintain lands, those specs are activity-only.
2. **Spec-baseline utility credits** (see §C) — CoE/IFF/Expose Weakness/Moonkin-Aura *uptime* are the same
   Debuffs/Buffs events.
- **Blocker:** cached snapshots don't carry the raw event bands → needs a live `--dry-run`/pipeline run to
  populate + verify. Do it in a fresh session with WCL access.

---

## C. Tier + utility-credit redesign (NEW decisions — the heart of the handoff)

### The structural insight
The archetype tiers (`PERF_ARCHETYPE` / `perfArchetype`, dps0/dps1/dps2) were calibrated to compensate for
**PARSE's** blind spots — the code comment literally says dps2 = *"specs whose personal **parse** structurally
understates them (Ret, Shadow)."* **v1.5 made Performance activity-based, and activity does NOT understate
them** (a seal-twisting Ret has high activity). So that rationale dissolves → **the tiers need re-deriving**,
and the dps2 tier likely shrinks/flattens.

### The organizing principle (maintainer's, adopt it): "cost of utility"
- Utility that's **passive / part of the rotation** (Ret seal-twist, Shadow VT, Survival Expose Weakness) →
  costs no output → judge the spec MORE on Performance.
- Utility that requires **pressing non-damage buttons** (warrior Sunders, shaman totems, rogue EA, warlock
  curses) → costs output → weight Utility higher, Performance lower.
This replaces both "dps magnitude" and the old parse-understatement logic as the basis for the weights.

### Spec-baseline utility credit (NEW — the key decision)
A spec brought **primarily for a debuff/buff** gets that as a **baseline utility credit** (it's their invited
role), measured as the debuff/buff **uptime** (→ the v2 fetch). This reverses the old blanket "exclude shared
curses" stance for specs that are the *designated* provider:
- **Affliction → Curse of the Elements.** "Affli is ALWAYS responsible for CoE" → bake CoE in as the affli
  spec baseline. (Previously excluded as a shared curse; now credited because affli is the dedicated holder.)
- **Balance → Moonkin Aura (+5% spell crit) + Improved Faerie Fire (+3% hit).** Currently UNcredited
  (boomkin util facets are only `innervate`+`save`) — this is why a tier move alone would backfire.
- **Survival hunter → Expose Weakness (+AP).** Currently UNcredited (`md` only). "Typically only brought for
  those debuffs."
- General rule: identify each spec's "you were invited for this" debuff/buff and credit its uptime.

### Tier moves (apply AFTER the utility credits land, else they backfire)
- **Ret → up (1.0 → 1.75 / dps1):** justified by the activity-perf shift + rotational (no-cost) utility.
- **Boomkin → utility-first (1.75 → 1.0 / dps2):** correct ONCE Moonkin Aura + IFF are credited (else it'd be
  weighted on utility it doesn't get points for → tank its score).
- **Affliction → middle (2.25 → 1.75 / dps1):** justified ONCE CoE is credited (then affli > destro in
  utility; the dps gap itself is moot since perf is activity-based).
- Re-derive the rest on the cost-of-utility principle; don't move a tier before its utility is actually scored.

---

## D. Doc staleness to fix (decide: now, or at deploy)
- **`CLAUDE.md`** (gitignored, local-only) — KPI table ~line 409 still says **"Performance = WCL Parse %"**.
  Stale vs the code; still accurate for what's LIVE (not deployed). Rewrite to input-based v2 at deploy time. **(STILL TO DO — deferred to deploy.)**
- **`DPS_SPEC_PERFORMANCE_FRAMEWORK.md`** — ✅ DONE (2026-06-17): v2 status header added; §C credits +
  cost-of-utility principle folded in.
- **The two one-pagers** (`raider_score_onepager.html/pdf` leadership overview,
  `performance_composite_per_spec.html/pdf` per-spec logic) — ✅ UPDATED to v2 (2026-06-18): archetype
  tiers (boomkin = the lone utility-first; Ret/Shadow/Affli moves), the maintain-uptime overlay, the §C
  spec-baseline credits, and the VT "decided" split. PDFs regenerated 1-page via headless Chrome
  (`--headless=new --no-pdf-header-footer --print-to-pdf`; raider doc margin tightened to 0.42in to fit).

---

## E. Where things live (code + docs)
- **Scoring core:** `dashboard/template.html`, fenced `// __PERF_SCORING_PURE_BEGIN__ … _END__` —
  `PERF_ARCHETYPE`, `perfArchetype`, `PERF_ONCD`, `PERF_MAINTAIN_V2`, `PERF_TUNING`, `utilFacetsFor`,
  `CLASS_UTIL_FACETS`, `PERF_FACET_TARGET`, `_perfRows`. (DOM-free — keep it that way or the node test breaks.)
- **JS tests:** `tests/perf_scoring.test.mjs` (`node --test`, CI-only — run by hand when touching the fence).
- **Build sheet:** `docs/DPS_SPEC_PERFORMANCE_FRAMEWORK.md` (§7 SPEC_ROTATION = the v2 config; §2 = what's
  missing; §5 = wowsims APL decode method).
- **Memory:** `perf-pillar-input-based-redesign` (auto-recalled) + `raider-score-kpi-design` +
  `tbc-raid-mechanics-reference`. Verification skills: `wcl-api`, `tbc-wow-reference`.
- **Offline proof:** `python scripts/replay_render.py <out.html> <report>` (zero WCL/DB) → open with
  `?officer=1` to see the Performance tab; `_perfRows()` in console for raw rows.

## F. Open decisions / first tasks for the fresh session
1. **Build v2 uptime fetch** (live WCL) → Maintain class + the §C spec-baseline utility credits.
2. **Credit CoE→affli, Moonkin Aura+IFF→boomkin, Expose Weakness→survival** in `utilFacetsFor` /
   `PERF_FACET_TARGET` (these gate the tier moves).
3. **Re-derive the archetype tiers** on the cost-of-utility principle (Ret up; boomkin/affli per above;
   dps2 likely shrinks).
4. **Decide the VT double-count** (uptime vs mana-value) consciously when wiring Shadow's maintain.
5. **Deploy** v1.5 (and v2 when ready) — re-skin + `publish.py`; update `CLAUDE.md` Performance entry at deploy.
6. Keep all gates green; keep the fenced region DOM-free; calibrate every target against live `playerSpells`.
