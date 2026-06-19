// Phase 2 — JS unit tests for the Performance/Raider-Score scoring core.
//
// The scoring lives inline in dashboard/template.html (the single-file deploy model keeps it
// there). Rather than duplicate it, this test READS the template, extracts the pure DOM-free
// region between the // __PERF_SCORING_PURE_BEGIN__ / __PERF_SCORING_PURE_END__ markers, and
// evaluates it in a vm sandbox with a synthetic WEEK_DATA — so the tests run the REAL shipped
// code with zero drift. No DOM, no network, no build step.
//
// Run:  node --test tests/perf_scoring.test.mjs      (Node 20+ — stable `node --test`)
// CI:   node --test tests/*.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(join(__dirname, '..', 'dashboard', 'template.html'), 'utf8');

// Pull every marked pure region and concatenate (the scoring core is two non-contiguous blocks:
// the PERF_* consts + perfArchetype/utilFacetsFor, and _perfRows — the DOM/drawer code between
// them is deliberately excluded).
const RE = /\/\/ __PERF_SCORING_PURE_BEGIN__[^\n]*\n([\s\S]*?)\/\/ __PERF_SCORING_PURE_END__/g;
let CODE = '', m, regions = 0;
while ((m = RE.exec(html)) !== null) { CODE += m[1] + '\n'; regions++; }

assert.equal(regions, 2, 'expected exactly 2 pure scoring regions (markers intact in template.html)');
for (const sym of ['function perfArchetype', 'function utilFacetsFor', 'function _perfRows']) {
  assert.ok(CODE.includes(sym), `extracted region is missing ${sym} — markers may have drifted`);
}

// Evaluate the extracted code in a fresh sandbox per call, returning the value of a trailing
// expression. WEEK_DATA is the only external global the core reads (at call time).
function run(wd, expr) {
  const ctx = vm.createContext({ WEEK_DATA: wd, console });
  return vm.runInContext(CODE + '\n;(' + expr + ');', ctx);
}
const rowsFor = wd => run(wd, '_perfRows()');
const rowByName = (wd, name) => rowsFor(wd).find(r => r.name === name);
const archOf = (c, s, r) => run({}, `perfArchetype(${JSON.stringify(c)},${JSON.stringify(s)},${JSON.stringify(r)})`);
// JSON round-trip so the returned array is a HOST array (vm-context arrays have a different
// Array.prototype, which deepStrictEqual rejects as not reference-equal).
const facetsOf = (c, s, r) => JSON.parse(JSON.stringify(
  run({}, `utilFacetsFor(${JSON.stringify(c)},${JSON.stringify(s)},${JSON.stringify(r)})`)));

// ── archetype resolution (per-spec weighting identity) ──────────────────────────────────────
test('perfArchetype resolves role/spec to the right weighting bucket', () => {
  assert.equal(archOf('Warrior', 'Protection', 'Tank'), 'tank');     // role wins (spec-swap aware)
  assert.equal(archOf('Priest', 'Holy', 'Healer'), 'healer');
  // ★ cost-of-utility re-derivation (§C): rotational (no-cost) utility → judged on Performance.
  // Ret went all the way to tier 0 — its seal-twist is now SCORED as Performance (PERF_ONCD Seal of
  // Command), leaving only a positive-only dispel. Shadow stays tier 1 (VT/Misery still in Utility).
  assert.equal(archOf('Paladin', 'Retribution', 'Physical'), 'dps0'); // Ret — seal-twist now Performance
  assert.equal(archOf('Priest', 'Shadow', 'Caster'), 'dps1');         // Shadow — rotational utility (VT/Misery)
  assert.equal(archOf('Druid', 'Balance', 'Caster'), 'dps2');         // boomkin — utility-FIRST (the aura is the point)
  assert.equal(archOf('Warlock', 'Affliction', 'Caster'), 'dps1');    // affli — CoE-credited utility-flavored
  assert.equal(archOf('Shaman', 'Elemental', 'Caster'), 'dps1');      // ToW buff — utility-flavored
  assert.equal(archOf('Warlock', 'Destruction', 'Caster'), 'dps0');   // pure parse
  assert.equal(archOf('Rogue', '', 'Physical'), 'dps0');              // blank spec → pure-DPS fallback
});

// ── 1. Ret seal-twist is now a PERFORMANCE input (PERF_ONCD Seal of Command); Ret is dps0 ─────
test('Ret seal-twist scores in Performance (Seal of Command on-CD), weighted dps0', () => {
  const wd = {
    roster: { Ret: { class: 'Paladin', spec: 'Retribution', role: 'Physical' } },
    // 1-minute selection: cpm == casts. On-CD: Crusader Strike 3 (≥3), Judgment 3 (≥2.5), Seal of
    // Command 5 (≥5) → all at/above target → on-CD overlay 100; activity 100 → perf 100.
    damageBySelection: { durations: { all: 60, boss: 60 },
      players: [{ name: 'Ret', vs_replacement: 55, all: { total: 100, active: 60000 }, boss: { active: 60000 } }] },
    playerSpells: { Physical: [{ name: 'Ret', abilities: [
      { ability: 'Crusader Strike', casts: 3 }, { ability: 'Judgment', casts: 3 }, { ability: 'Seal of Command', casts: 5 }] }] },
    // no consumables (prep null), no log (exec null), no dispels → util null (disp is positive-only)
  };
  const r = rowByName(wd, 'Ret');
  assert.equal(r.arch, 'dps0', 'Ret is pure-DPS now that seal-twist is scored as Performance');
  assert.equal(r.perf, 100, 'activity 100 + on-CD (CS/Judgment/Seal of Command all at target) → 100');
  assert.match(r.why.perf, /Seal of Command/, 'seal-twist appears in the Performance breakdown, not Utility');
  assert.equal(r.util, null, 'Ret utility is dispels-only (positive-only) → null with no dispels');
});

// ── 2. Paladin utility split is by ROLE: Ret=disp-only (seal-twist→Perf), Prot/Holy=pala ─────
test('paladin utility facets split by role (ret disp-only vs pala)', () => {
  assert.deepEqual(facetsOf('Paladin', 'Retribution', 'Physical'), ['disp']);   // twist moved to Performance
  assert.deepEqual(facetsOf('Paladin', 'Protection', 'Tank'), ['pala', 'disp']);
  assert.deepEqual(facetsOf('Paladin', 'Holy', 'Healer'), ['pala', 'disp']);
});

// ── 2b. Enhancement has NO utility facet — Windfury/totem-twisting is EXECUTION, moving to Performance
// (PR #2, scored as twist cadence). The interim must NOT crater a GoA-heavy twister on a WF-uptime target.
test('Enhancement utility is empty (Windfury moved out of Utility → Performance)', () => {
  assert.deepEqual(facetsOf('Shaman', 'Enhancement', 'Physical'), []);
  const wd = {
    roster: { Enh: { class: 'Shaman', spec: 'Enhancement', role: 'Physical' } },
    // low WF uptime (a twister) must NOT show up as a cratered utility score anymore.
    damageBySelection: { durations: { all: 100 }, players: [{ name: 'Enh', toolkit: { num: 12 }, all: { total: 1, active: 0 } }] },
  };
  const r = rowByName(wd, 'Enh');
  assert.equal(r.util, null, 'no utility facet → util null (not a cratered 13 from low WF)');
});

// ── 3. Rogue Expose is positive-only: it LIFTS the assigned rogue, never drags a pure-DPS one ─
test('rogue Expose Armor is positive-only (lifts, never drags)', () => {
  const wd = {
    roster: {
      RogA: { class: 'Rogue', spec: 'Combat', role: 'Physical' },
      RogB: { class: 'Rogue', spec: 'Combat', role: 'Physical' },
    },
    interrupts: [{ name: 'RogA', count: 2 }, { name: 'RogB', count: 2 }],   // both: intr 2 vs target 4 → 50
    exposeArmor: { players: [{ name: 'RogA', uptime: 80 }] },               // only A holds the armor slot
  };
  const a = rowByName(wd, 'RogA'), b = rowByName(wd, 'RogB');
  assert.equal(b.util, 50, 'RogB (no Expose) sits at its primary (interrupts) — not dragged below');
  assert.equal(a.util, 75, 'RogA lifted by Expose: max(avg[50], avg[50,100]) = 75');
  assert.ok(a.util > b.util);
});

// ── 4. Composite renormalizes over PRESENT pillars only (a null pillar is excluded) ──────────
test('composite renormalizes over present pillars when one is null', () => {
  const wd = {
    roster: { D: { class: 'Mage', spec: 'Fire', role: 'Caster' } },
    consumables: [{ name: 'D', flask: true, food: true, weapon: true }],   // prep 40+25+20 = 85
    damageBySelection: { durations: { all: 100 }, players: [{ name: 'D', vs_replacement: 80, all: { total: 100, active: 80000 } }] },  // activity 80 → perf 80
    // no log → exec null; Mage's only facet is intr, nobody interrupted → util null
  };
  const r = rowByName(wd, 'D');
  assert.equal(r.exec, null);
  assert.equal(r.util, null);
  assert.equal(r.present, 3);
  // dps0 weights: prep 1, perf 2.25, surv 1 (exec/util absent). Renormalized den = 4.25, NOT 5.75.
  const expected = (1 * 85 + 2.25 * 80 + 1 * 100) / (1 + 2.25 + 1);
  assert.ok(Math.abs(r.composite - expected) < 1e-9, `composite ${r.composite} != renormalized ${expected}`);
  assert.ok(r.composite > 80, 'sanity: a 63 here would mean it wrongly divided by all 5 weights');
});

// ── 5. Killing a charmed teammate floors Execution to 0 ──────────────────────────────────────
test('MC kill floors execution to 0', () => {
  const wd = {
    roster: { K: { class: 'Warrior', spec: 'Fury', role: 'Physical' } },
    avoidableDmg: [{ name: 'K', dmg: 0, sources: [] }],   // a log exists → exec is graded (not null)
    mcLiable: [{ name: 'K', kills: 1 }],                  // landed a killing blow on a charmed ally
  };
  const r = rowByName(wd, 'K');
  assert.equal(r.exec, 0);
  assert.match(r.why.exec, /killed a Mind-Controlled teammate/);
});

// ── 6b. Group-buff GEAR (JC neck) lifts utility positive-only — any provider, never drags ─────
test('group-buff gear lifts the provider, never drags a non-provider', () => {
  const wd = {
    roster: {
      Mg:  { class: 'Mage', spec: 'Fire', role: 'Caster' },
      Mg2: { class: 'Mage', spec: 'Fire', role: 'Caster' },
    },
    interrupts: [{ name: 'Mg', count: 2 }, { name: 'Mg2', count: 2 }],   // both: intr 2 vs target 4 → 50
    groupBuffGear: [{ name: 'Mg', buffs: [{ item: 'Eye of the Night', label: '+34 spell power (party)' }] }],
  };
  const a = rowByName(wd, 'Mg'), b = rowByName(wd, 'Mg2');
  assert.equal(b.util, 50, 'non-provider sits at its primary (interrupts) — not dragged');
  assert.equal(a.util, 75, 'provider lifted by the neck: max(avg[50], avg[50,100]) = 75');
  assert.match(a.why.util, /group buff: Eye of the Night/);
});

// ── 6c. Hunter weapon-oil slot is N/A → Prep renormalizes so the n/a doesn't cap them ─────────
// Compared WITHIN the phys archetype (both flask+food, no oil): the hunter's oil is N/A (renormalized
// OUT of the denominator), a melee rogue's oil is a real UNFILLED slot (counts against them).
test('hunter n/a weapon-oil slot renormalizes Prep (not a flat cap)', () => {
  const wd = {
    roster: { H: { class: 'Hunter', spec: 'Marksmanship', role: 'Physical' },
              R: { class: 'Rogue',  spec: 'Combat',        role: 'Physical' } },
    consumables: [ { name: 'H', flask: true, food: true },     // hunter: flask+food, oil N/A
                   { name: 'R', flask: true, food: true } ],    // rogue: same, oil a real unfilled slot
  };
  // phys set: FLASK 40, FOOD 15, OIL 25, POT 30, ALT 5 → setMax 115. core = flask+food = 55.
  const h = rowByName(wd, 'H'), r = rowByName(wd, 'R');
  assert.equal(h.prep, Math.round(55 * 100 / (115 - 25)), 'hunter core 55 renormalized over 90 (oil N/A) → 61');
  assert.equal(r.prep, Math.round(55 * 100 / 115), 'rogue core 55 over the full 115 (oil unfilled) → 48');
  assert.ok(h.prep > r.prep, 'hunter not capped for a slot that does not apply to them');
  assert.match(h.why.prep, /weapon oil n\/a/);
});

// ── 6d. Per-archetype prep: 2 distinct elixirs ≈ a flask > 1 elixir; combat pot lifts phys a lot ──
test('per-archetype prep: 2 elixirs ≈ flask > 1 elixir; phys combat pot is heavily weighted', () => {
  const mk = c => ({ roster: { W: { class: 'Warrior', spec: 'Fury', role: 'Physical' } }, consumables: [{ name: 'W', ...c }] });
  const flask = rowByName(mk({ flask: true, food: true }), 'W').prep;
  const two   = rowByName(mk({ elixirs: ['Major Strength', 'Mongoose'], food: true }), 'W').prep;
  const one   = rowByName(mk({ elixirs: ['Major Strength'], food: true }), 'W').prep;
  assert.ok(two > one, '2 distinct elixirs beat 1 (the missing 1-vs-2 rule)');
  assert.ok(flask >= two && (flask - two) <= 5, '2 elixirs ≈ a flask (within a few points)');
  const noPot  = rowByName(mk({ flask: true, food: true }), 'W').prep;
  const withPot = rowByName(mk({ flask: true, food: true, combat_pots: ['Haste Potion'] }), 'W').prep;
  assert.ok(withPot - noPot >= 15, 'a phys combat pot is a big lift (chart: Haste Potion is the top gain)');
});

// ── 7. DPS perf = activity backbone + core cadence (NOT parse); engineering lifts it positive-only ─
test('DPS perf = 0.7·activity + 0.3·core cadence (parse is context); engineering is positive-only', () => {
  const wd = {
    roster: { A: { class: 'Warrior', spec: 'Fury', role: 'Physical' },
              B: { class: 'Warrior', spec: 'Fury', role: 'Physical' } },
    damageBySelection: { durations: { all: 100, boss: 80 },
      players: [ { name: 'A', vs_replacement: 95, all: { total: 1, active: 70000 }, boss: { active: 60000 } },
                 { name: 'B', vs_replacement: 40, all: { total: 1, active: 70000 }, boss: { active: 60000 } } ] },
    playerSpells: { Physical: [ { name: 'A', abilities: [{ ability: 'Bloodthirst', casts: 4 }, { ability: 'Whirlwind', casts: 3 }] },
                                { name: 'B', abilities: [{ ability: 'Bloodthirst', casts: 4 }, { ability: 'Whirlwind', casts: 3 }] } ] },  // BT+WW both ≥ target → on-CD 100 (Fury has no rotation-share entry)
    engineering: [{ name: 'A', eng: { 'Super Sapper Charge': 6 } }],   // A actively uses engineering
  };
  const a = rowByName(wd, 'A'), b = rowByName(wd, 'B');
  // 70% activity (x0.7) + full overlay 100 (x0.3) = 79; parse (95 vs 40) is NOT in the score
  assert.equal(b.perf, 79, 'perf = 0.7·activity + 0.3·overlay — independent of parse (B parses 40 but scores 79)');
  assert.equal(a.perf, 85, 'engineering +6 lifts A from 79 → 85, positive-only');
  assert.ok(a.perf > b.perf, 'identical activity + cadence, but the engineer is lifted');
  assert.match(a.why.perf, /activity/);
  assert.match(a.why.perf, /parse 95 \(context/);   // parse demoted to context in the brief
});

// ── 7b. On-CD cadence moves the score (minority, can't invert); uses an on-CD spec ────────────
test('on-CD cadence blends in: same activity, but pressing your cooldown matters', () => {
  const wd = {
    roster: { Good: { class: 'Shaman', spec: 'Enhancement', role: 'Physical' },
              Lazy: { class: 'Shaman', spec: 'Enhancement', role: 'Physical' } },
    damageBySelection: { durations: { all: 60, boss: 60 },
      players: [ { name: 'Good', all: { total: 1, active: 60000 }, boss: { active: 60000 } },
                 { name: 'Lazy', all: { total: 1, active: 60000 }, boss: { active: 60000 } } ] },   // both 100% active
    playerSpells: { Physical: [ { name: 'Good', abilities: [{ ability: 'Stormstrike', casts: 5 }] },   // 5/min vs target 5 → on-CD 100
                                { name: 'Lazy', abilities: [{ ability: 'Stormstrike', casts: 1 }] } ] }, // 1/min → ~20
  };
  const g = rowByName(wd, 'Good'), l = rowByName(wd, 'Lazy');
  assert.equal(g.perf, 100, 'full activity + on-CD cadence at target → 100');
  assert.ok(l.perf < g.perf, 'identical activity, but a missed cooldown scores lower (minority drag)');
  assert.match(l.why.perf, /Stormstrike/);
});

// ── 7c. Fillers are NOT scored; WITHOUT maintainUptime, a maintain-heavy spec is activity-only ─
test('no maintainUptime → Affliction falls back to activity-only (v1.5 behavior preserved)', () => {
  const wd = {
    roster: { Lock: { class: 'Warlock', spec: 'Affliction', role: 'Caster' } },
    damageBySelection: { durations: { all: 60 }, players: [{ name: 'Lock', all: { total: 1, active: 48000 } }] }, // 80% activity
    playerSpells: { Caster: [{ name: 'Lock', abilities: [{ ability: 'Shadow Bolt', casts: 99 }] }] },  // filler — must NOT be scored
    // NO maintainUptime section (old snapshot) → the ungated Corruption maintain must NOT read 0 and tank them
  };
  const r = rowByName(wd, 'Lock');
  assert.equal(r.perf, 80, 'no maintain data → Shadow Bolt filler ignored, 80% activity → perf 80 (not dragged to 56)');
});

// ── 7d. MAINTAIN uptime overlay (v2): DoT uptime folds into the overlay, blended under activity ─
test('maintain uptime blends into the overlay (Affliction Corruption + gated Immolate)', () => {
  const wd = {
    roster: { Lock: { class: 'Warlock', spec: 'Affliction', role: 'Caster' } },
    damageBySelection: { durations: { all: 100 }, players: [{ name: 'Lock', all: { total: 1, active: 100000 } }] }, // 100% activity
    playerSpells: { Caster: [{ name: 'Lock', abilities: [{ ability: 'Shadow Bolt', casts: 99 }] }] },
    // Corruption ungated (target 85) → 85/85=100; Immolate gated (target 50) at 10% < 1/3·50≈16.7 → DROPS;
    // Siphon Life gated (target 40) at 38 ≥ 13.3 → 38/40=95; UA absent (0 < gate) → drops.
    maintainUptime: { Lock: { 'Corruption': 85, 'Immolate': 10, 'Siphon Life': 38 } },
  };
  const r = rowByName(wd, 'Lock');
  // overlay = avg(Corruption 100, Siphon Life 95) = 97.5 → round 98; perf = 0.7·100 + 0.3·98 = 99.4 → 99
  assert.equal(r.perf, 99, 'overlay = avg(Corruption 100, Siphon Life 95); Immolate gated out');
  assert.match(r.why.perf, /Corruption 85% up/);
  assert.doesNotMatch(r.why.perf, /Immolate/, 'a gated maintain below threshold drops out of the brief');
});

// ── 7e. §C spec-baseline utility credit: Affliction is credited Curse of the Elements UPTIME ────
test('Affliction is credited CoE uptime as a primary Utility facet (§C)', () => {
  assert.deepEqual(facetsOf('Warlock', 'Affliction', 'Caster'), ['stones', 'coe']);
  assert.deepEqual(facetsOf('Warlock', 'Destruction', 'Caster'), ['stones'], 'destro is stones-only (curse is assigned)');
  assert.deepEqual(facetsOf('Druid', 'Balance', 'Caster'), ['innervate', 'save', 'iff'], 'boomkin gets Faerie Fire');
  assert.deepEqual(facetsOf('Hunter', 'Survival', 'Physical'), ['md', 'exposew'], 'survival gets Expose Weakness');
  assert.deepEqual(facetsOf('Hunter', 'Marksmanship', 'Physical'), ['md'], 'MM is md-only');
  const wd = {
    roster: { Lock: { class: 'Warlock', spec: 'Affliction', role: 'Caster' } },
    consumableUsage: [{ name: 'Lock', stones_made: 50 }],   // stones primary at target 50 → 100
    maintainUptime: { Lock: { 'Curse of the Elements': 90 } },  // coe 90 vs target 90 → 100
  };
  const r = rowByName(wd, 'Lock');
  assert.equal(r.util, 100, 'stones 100 + CoE 100 (both primary) → 100');
  assert.match(r.why.util, /Curse of Elements % 90 vs 90 target/);
});

// ── 7f. ROTATION SHARE: the main nuke as a % of the rotational cast set (fills the activity blind spot) ─
test('rotation share scores Arcane Blast share; minority weight cannot invert activity', () => {
  const mk = (ab, fb) => ({
    roster: { Mage: { class: 'Mage', spec: 'Arcane', role: 'Caster' } },
    damageBySelection: { durations: { all: 100 }, players: [{ name: 'Mage', all: { total: 1, active: 100000 } }] }, // 100% activity
    playerSpells: { Caster: [{ name: 'Mage', abilities: [{ ability: 'Arcane Blast', casts: ab }, { ability: 'Frostbolt', casts: fb }] }] },
  });
  const good = rowByName(mk(55, 45), 'Mage');   // AB share 0.55 / target 0.55 → 100
  assert.equal(good.perf, 100, '0.7·activity 100 + 0.3·share 100 → 100');
  assert.match(good.why.perf, /Arcane Blast 55% of rotation/);
  const bad = rowByName(mk(30, 70), 'Mage');     // AB share 0.30 → ~55 → overlay drags
  assert.ok(bad.perf < good.perf, 'wrong-nuke mage scores lower at IDENTICAL activity (the blind spot fixed)');
  assert.ok(bad.perf >= 80, 'minority overlay nudges, never inverts the 100% activity backbone');
});

// ── 7g. The curated denominator EXCLUDES Auto Shot (the hunter problem) ───────────────────────
test('hunter Steady Shot share excludes Auto Shot from the denominator', () => {
  const wd = {
    roster: { Hunter: { class: 'Hunter', spec: 'Marksmanship', role: 'Physical' } },
    damageBySelection: { durations: { all: 60 }, players: [{ name: 'Hunter', all: { total: 1, active: 60000 } }] },
    // Steady 80 / Arcane 20 / Multi 0 → denom 100, share 0.80. Auto Shot 300 must be EXCLUDED (else 80/380=0.21).
    playerSpells: { Physical: [{ name: 'Hunter', abilities: [
      { ability: 'Steady Shot', casts: 80 }, { ability: 'Arcane Shot', casts: 20 },
      { ability: 'Auto Shot', casts: 300 }, { ability: 'Kill Command', casts: 3 }] }] },
  };
  const r = rowByName(wd, 'Hunter');
  assert.match(r.why.perf, /Steady Shot 80% of rotation/, 'share = 80/100 (Auto excluded), not 80/380 = 21%');
});

// ── 6. A facet nobody did this week drops out (null) — never a damaging 0 ─────────────────────
test('empty-cohort facet yields null utility, not a 0', () => {
  const wd = {
    roster: { W: { class: 'Warrior', spec: 'Arms', role: 'Physical' } },
    // Warrior facets are bshout + sunder; provide neither → both gated out by the bestF applicability check
  };
  const r = rowByName(wd, 'W');
  assert.equal(r.util, null, 'no measured utility → null (drops out), not a damaging 0');
  assert.match(r.why.util, /no measured utility/);
});
