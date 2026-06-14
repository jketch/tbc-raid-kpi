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
  assert.equal(archOf('Paladin', 'Retribution', 'Physical'), 'dps2'); // Ret — utility-first
  assert.equal(archOf('Priest', 'Shadow', 'Caster'), 'dps2');         // Shadow — utility-first
  assert.equal(archOf('Shaman', 'Elemental', 'Caster'), 'dps1');      // ToW buff — utility-flavored
  assert.equal(archOf('Warlock', 'Destruction', 'Caster'), 'dps0');   // pure parse
  assert.equal(archOf('Rogue', '', 'Physical'), 'dps0');              // blank spec → pure-DPS fallback
});

// ── 1. Ret seal-twist: the dps2 weighting + the twist facet actively lift the score ──────────
test('Ret seal-twist scores via the twist facet, weighted dps2 (util x2)', () => {
  const wd = {
    roster: { Ret: { class: 'Paladin', spec: 'Retribution', role: 'Physical' } },
    damageBySelection: { players: [{ name: 'Ret', vs_replacement: 55, toolkit: { num: 5 }, all: { total: 100 } }] },
    // no consumables (prep null), no log (exec null) — isolates perf/surv/util
  };
  const r = rowByName(wd, 'Ret');
  assert.equal(r.arch, 'dps2');
  assert.equal(r.w[4], 2, 'util pillar weighted x2 for a utility-first DPS');
  assert.equal(r.util, 100, 'twist num 5 vs target 5 → 100');
  assert.match(r.why.util, /Seal twists/);   // FACET_LABEL.twist
  // composite renormalizes over the 3 present pillars: perf 55 (x1) + surv 100 (x1) + util 100 (x2)
  assert.ok(Math.abs(r.composite - (55 + 100 + 200) / 4) < 1e-9, `composite ${r.composite} != 88.75`);
});

// ── 2. Paladin utility split is by ROLE: Ret=twist, Prot/Holy=pala (JoW+blessings) ───────────
test('paladin utility facets split by role (twist vs pala)', () => {
  assert.deepEqual(facetsOf('Paladin', 'Retribution', 'Physical'), ['twist', 'disp']);
  assert.deepEqual(facetsOf('Paladin', 'Protection', 'Tank'), ['pala', 'disp']);
  assert.deepEqual(facetsOf('Paladin', 'Holy', 'Healer'), ['pala', 'disp']);
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
    damageBySelection: { players: [{ name: 'D', vs_replacement: 80, all: { total: 100 } }] },  // perf 80
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
