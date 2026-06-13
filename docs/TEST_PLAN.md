# Test Build-Out Plan (handoff)

Goal: close the coverage gaps in the suite. Today the tests prove **structure** (sections present,
contract drift, facade surface) very well, but barely test **values**, and don't test the **JS**
scoring layer at all. This plan adds value-level tests for the pure transforms, safety-net tests that
make refactors safe, and a path to testing the Performance scoring.

**Status legend:** ☐ not started · ◐ in progress · ☑ done. Update inline as you go.

---

## Conventions (read first)

- **Framework:** stdlib `unittest`. **No pytest.** Files live in `tests/`, named `test_*.py`.
- **Run:** `python scripts/check.py --tests` (unit suite only) or `python scripts/check.py` (suite +
  characterization). CI runs the same gate; the pre-commit hook runs `check.py`.
- **Hermetic — non-negotiable:** zero WCL calls, zero writes to the prod DB (`cache/raid_history.db`),
  zero writes to `dashboard/raid_kpi_dashboard.html`. Use in-memory SQLite (`:memory:`) or a
  `tempfile` DB, and write any HTML/JSON to `tempfile` paths.
- **Import shim every file needs** (matches the existing tests):
  ```python
  import sys
  from pathlib import Path
  sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
  ```
- **Exemplars to copy from:**
  - `tests/test_week_map.py` — fixture dict → `map_to_week_data` → assert values. The pattern for all
    pure-transform tests.
  - `tests/test_death_timeline.py` — testing a single pure function with hand-built events.
  - `tests/test_week_build.py` — in-memory + monkeypatch for orchestration seams.
  - `tests/test_db_writer_guard.py` — SQLite against a temp DB.
- **Determinism rule:** the project guarantees byte-identical output for identical input. Any new test
  that builds output should, where cheap, assert determinism (dump twice, compare).

---

## Phase 0 — safety nets (do FIRST; they protect every later refactor)

These don't just add coverage — once green, no change can silently alter dashboard output without a red
test. Land these before touching `template.html`.

### ☑ 0a. `tests/test_inject_roundtrip.py` — the brace-depth injector
- **Targets:** `render_html.inject_into_html` (writer) ↔ `week_build.extract_week_data` (reader). They
  share the string/escape-aware brace walk that CLAUDE.md warns "a regex would corrupt."
- **Cases:**
  1. **Round-trip identity:** inject a WEEK_DATA dict into a temp HTML (uses the real
     `dashboard/template.html`), extract it back, assert deep-equal.
  2. **Brace inside a string value survives:** a boss/item name containing `{` / `}` / `};` / escaped
     `"` round-trips intact (the exact E3 bug — see `test_week_build.py::TestExtractWeekData`).
  3. **`mapped=` path:** when `inject_into_html(wd, path, mapped=enriched)` is given an already-mapped
     dict, the injected block equals `enriched` verbatim (preserves `delta_*`/`prev`), not a re-map.
  4. **Idempotent:** inject A then inject B into the same file → extract returns B (the old block is
     fully replaced, no trailing corruption).
- **Infra:** write to a `tempfile` HTML path; read `dashboard/template.html` (it exists in-repo).
- **DoD:** all 4 cases pass; deliberately swapping the brace walk for a naive `re.sub(r'\{.*?\};')`
  makes case 2 fail (proves the test has teeth).

### ☑ 0b. `tests/test_determinism.py` — byte-stable output in CI
- **Target:** the set→list-sorted determinism guarantee, currently only checked by hand via
  `replay_render.py` + `fc /b`.
- **Cases:**
  1. `map_to_week_data(fixture)` dumped twice (`json.dumps`, `sort_keys=False`) is byte-identical.
     (A minimal version already exists in `test_week_map.py`; promote/expand it here.)
  2. **Stronger:** load a cached raw dict from `cache/wcl/<code>.json` *if present* and assert
     `map_to_week_data` is byte-stable across two calls. Guard with `unittest.skipUnless(path.exists())`
     so CI (no cache) skips cleanly but a dev box exercises real data.
- **DoD:** green in CI; a deliberately-unsorted `set()` iteration reaching output flips it red.

---

## Phase 1 — independent Python value tests (no template dependency)

### ☑ 1a. `tests/test_combat_log.py` — the raw-log parser (BIGGEST gap)
`scripts/combat_log.py` (~666 lines) parses the most fragile input and has **zero** tests. Build a tiny
synthetic-log helper, then assert behavior. Log line format: `TIMESTAMP<2+ spaces>EVENT,field,field,…`;
timestamp is `M/D H:MM:SS.mmm`. Read the field indices straight from `parse_combat_log` (e.g.
`ENCOUNTER_START` name = `fields[2]`; `ENCOUNTER_END` result = `fields[5]`; `UNIT_DIED` destGUID/name =
`fields[5]`/`fields[6]`; `SPELL_SUMMON` src=`fields[1]`, summoned=`fields[5]`; `SPELL_INTERRUPT`
interrupted spell = `fields[13]`).

- **Build a fixture helper** `_log(*lines) -> path`: write lines to a `tempfile`, return the path
  (`parse_combat_log` takes a path).
- **Cases (one synthetic encounter each, ~5–15 lines):**
  1. **Kill detection — the Hydross bug (`6ff1b00`):** `ENCOUNTER_START` → `UNIT_DIED` on the boss
     Creature within 1.5s → `ENCOUNTER_END` with `result=0`. Assert the boss is treated as a **kill**
     (appears in `fights`). This is a documented fix with no guard today.
  2. **Normal kill:** `ENCOUNTER_END,…,1` (result=1) → kill, no UNIT_DIED needed.
  3. **Wipe stays a wipe:** `result=0` and no boss UNIT_DIED in-window → NOT a kill.
  4. **`allowed_bosses` exclusion:** a second encounter not in `allowed_bosses` → its events are
     skipped (e.g. an off-report boss's avoidable dmg doesn't reach a player).
  5. **Pet→owner interrupt (this-session fix):** `SPELL_SUMMON` (warlock summons Felhunter) then
     `SPELL_INTERRUPT` from the pet GUID on a Creature → the **owner** gets `interrupt_count == 1`;
     an unmapped pet's interrupt is dropped.
  6. **`_parse_ts`:** unit-test directly — a normal time, midnight rollover across two lines (later
     wall-clock parses as later), and the 2-digit-year bump (`y < 100 → +2000`).
  7. **MC-save dedup:** same caster CCs the same charmed ally twice in one MC episode → counts once;
     a new MC episode (aura re-applied) → counts again.
  8. **Robustness:** a malformed/short line (`len(parts) != 2`, or `fields` shorter than an index the
     handler reads) does not raise — the parser skips it.
  9. **Archive open (`_open_log`):** a `.gz` and a `.zip` of a valid mini-log parse identically to the
     plain `.txt` (this is also partly covered by `test_log_discovery.py` — cross-check, don't dup).
- **DoD:** the 9 cases pass; reverting the pet-interrupt change (case 5) or the Hydross promotion
  (case 1) turns the suite red.

### ☑ 1b. `tests/test_trends.py` — week-over-week enrichment
`scripts/trends.enrich_with_trends(week_data, db_path)` is subtle, load-bearing, and has had real bugs.
Test against a `tempfile` SQLite seeded via `db_writer.write_week(prior_week, db_path)`.

- **Cases:**
  1. **Chronological prior (`c6fbda1` bug):** seed weeks W1 (older `start_ms`), W3 (newer). Enrich W2
     (middle). Assert deltas compute vs **W1**, never the global-latest W3. Build them out of insertion
     order to prove it sorts by `start_ms`, not insert order or date string.
  2. **0th-percentile parse delta (this-session falsy-zero fix):** prior `dps.war = 0`, current
     `vs_replacement = 5` → `delta_vs_replacement == 5` (a real 0 baseline is NOT skipped). Regression
     guard for the `is not None` fix.
  3. **No prior week:** single week in DB → `enrich` returns it unchanged, no `delta_*`, no crash.
  4. **Missing DB file / missing table:** `enrich` swallows the error and returns the input unchanged
     (degrades silently — the contract).
  5. **NULL `start_ms` legacy row:** a prior row with `start_ms IS NULL` is used as the
     lower-priority fallback (not silently excluded), and an anchored prior is preferred when both
     exist.
  6. **Per-section deltas:** spot-check one each of dps / healing / sunder / dispels delta math against
     hand-computed values.
- **Infra:** `db_writer.write_week(wd, tmp_db)` to seed; then `enrich_with_trends(current, tmp_db)`.
- **DoD:** cases pass; reverting the `is not None` fix flips case 2 red.

### ☑ 1c. `tests/test_crit_model.py` — TBC crit math
`scripts/crit_model.expected_crit` is pure (class/talent/gem/enchant/primary-stat tables). Lock it.
- **Cases:** 3–5 known `(class, spec, gear-crit-rating, primary stat) → expected %` cases covering at
  least one melee (agi-scaled), one caster (int-scaled), and the talent/base contribution. Pull the
  expected numbers by running the function once on a known input and freezing them (characterization
  style) **after** sanity-checking against `docs/TBC_RAID_MECHANICS.md` crit math.
- **Skip** `gear_crit_rating`/`fetch_item_crit` (they touch the item cache / network) unless you can
  inject a fake cache — keep this file to the pure model.
- **DoD:** the frozen cases pass; a typo in a crit table flips them.

### ☑ 1d. `tests/test_merge_log.py` — the log overlay + graceful degradation
`week_map.merge_log_into_wcl(wcl, log_data)` overlays combat-log results onto the wcl dict and reaches
the crit model (backfills tank/healer gear-crit, which WCL emits as 0).
- **Cases:**
  1. **Backfill:** a tank with `gear_crit_rating==0` from WCL + a log carrying per-school crit → the
     merged dict has the tank's crit backfilled, while a DPS WCL already got right is untouched.
  2. **WCL-Durability:** `merge_log_into_wcl(wcl, {})` (empty/missing log) returns the wcl dict with its
     WCL values intact — a missing log THINS, never BLANKS. (Pairs with the avoidable-fallback test
     already in `test_week_map.py`.)
- **DoD:** both pass.

### ☑ 1e. `tests/test_db_writer_roundtrip.py` — write → read → assert
You have a downgrade-*guard* test; add a write-then-read round-trip (the `eff_hps`-vs-`hps` column
gotcha lives here).
- **Cases:**
  1. Write a representative `week_data` to a temp DB; query back `weeks`, `healing` (assert the column
     is `eff_hps`), `dps`, `tank_scorecard`, `dispels`; assert values match the input.
  2. **Idempotency:** write the same report twice → row counts unchanged (`INSERT OR REPLACE`).
  3. **JSON columns:** `cooldowns` / `badges` / `targets` round-trip through `json.dumps`→`loads`.
- **Infra:** `db_writer.write_week(wd, tmp_db)`; `db_writer.query(...)` against the temp DB (pass the
  path; `query()` defaults to prod — use `sqlite3` directly on the temp path or extend the helper).
- **DoD:** a renamed/typo'd column flips case 1.

### ☑ 1f. `tests/test_fetch_helpers.py` — the pure helpers in `wcl_fetchers.py`
The fetch layer is WCL-coupled, but its helpers are pure and free to test.
- **Targets & cases:**
  - `_report(payload, *path, default)` (this-session addition): a full payload descends correctly; a
    **partial** payload (missing `report`/field, as `gql()` can return) yields `default`, not a crash.
  - `_loads_alias`: JSON string → dict; already-parsed → passthrough; malformed → `{}`.
  - `_merge_bands`: overlapping/adjacent/disjoint `{startTime,endTime}` intervals → correct covered ms.
  - `_totem_uptime`: a cast at pull + recasts within duration → expected %; gaps reduce it.
  - `_tank_survival_grade`: a clean tank (no crits/crushes/deaths) → 100; crit taken → penalty + flag;
    bear crush is waived (cls=="Druid").
  - `parse_damage_table` / `merge_actor_names` / `_median`: one case each.
  - **Stretch:** factor `fetch_expose_armor`'s band reconstruction into a pure helper
    (`_expose_bands(events, total_ms) -> {name: uptime}`) and test it with synthetic apply/refresh/
    remove events (open band closed at window end; multi-target merge). Today it's only validated by
    the live re-run.
- **DoD:** each helper has ≥1 case; `_report` partial-payload case is the priority (it guards the
  whole "one bad alias can't kill the run" property).

---

## Phase 2 — the scoring extraction + JS tests (the high-churn, zero-coverage surface)

The Performance/Raider Score logic (`_perfRows`, `facetVal`, `utilFacetsFor`, `perfArchetype`, the
`PERF_*` constants, the pillar math) is your **highest churn × zero coverage** code — every change this
session (ret seal-twist, rogue Expose, the falsy-zero) was verified by eyeballing the browser preview.
This phase makes it testable. **Gate on Phase 0 being green** (0a/0b are the net for touching the
monolith).

### ☐ 2a. Extract the scoring into a DOM-free module
- Pull the scoring surface out of `dashboard/template.html` into `dashboard/perf_scoring.js` (a plain
  module with **no DOM access** — pure functions over a `WEEK_DATA`-shaped object): the `PERF_*`
  constants, `perfArchetype`, `utilFacetsFor`, `facetVal`, and `_perfRows` (or a pure core of it that
  takes `wd` and returns the rows). The template `<script>` then references it.
- **Constraint:** preserve byte-identical rendered output — Phase 0b is the proof. Render a week before
  and after the extraction and confirm identical bytes (`replay_render.py` + the new determinism test).
- This is a *narrow slice* of the backlogged full template decomposition — do only the scoring, not the
  render functions.

### ☐ 2b. `tests/perf_scoring.test.mjs` — JS unit tests under `node --test`
- Use Node's built-in runner (`node --test`) — no new npm deps. Add `node --test dashboard/*.test.mjs`
  as a CI step (and document the Node version floor).
- **Cases (the properties hand-checked this session):**
  1. **Ret seal-twist:** a Ret with toolkit `num≈5` → `twist` facet ≈ 98; util lifts; composite reflects
     the `dps2` weighting (util ×2).
  2. **Paladin split:** a Prot/Holy paladin uses `pala` (JoW+blessings vs 500); a Ret uses `twist` —
     the split is by role.
  3. **Rogue Expose (positive-only):** a rogue with `exposeArmor` uptime is **lifted**; a rogue with no
     Expose is **unchanged** (not dragged) — positive-only behavior.
  4. **Composite renormalization:** a raider with a null pillar (e.g. no log → exec null) is scored over
     the present pillars only, weights renormalized.
  5. **MC-kill floor:** a player who killed a charmed teammate → exec floored to 0.
  6. **Empty-cohort safety:** a facet nobody did this week drops out (null), never a damaging 0.
- **DoD:** the 6 properties pass; they would have caught the ret/rogue mis-scoring before a human asked.

---

## Phase 3 — deferred (not part of this handoff)

Full `template.html` decomposition (all render functions → modules). Big, risky, interacts with the
injector + publish/extract. Do it when render-side work forces it — by then Phase 0 is the safety net.
Tracking only; **do not start as part of the test build-out.**

---

## Suggested order & rough sizing

| Order | Task | Size | Why here |
|------|------|------|----------|
| 1 | 0a inject round-trip | S | safety net before any refactor |
| 2 | 0b determinism in CI | S | safety net; partly exists |
| 3 | 1a combat_log | **L** | biggest gap, most fragile input |
| 4 | 1b trends | M | real bug history, load-bearing |
| 5 | 1f fetch helpers | S | cheap, guards the `_report` property |
| 6 | 1c crit_model | S | pure, free |
| 7 | 1d merge_log | S | WCL-durability at the merge |
| 8 | 1e db round-trip | M | column-drift guard |
| 9 | 2a scoring extraction | M | unblocks 2b; narrow slice only |
| 10 | 2b JS scoring tests | M | highest churn × zero coverage |

Phase 0 + Phase 1 are all stdlib, hermetic, no live WCL — landable incrementally, each its own commit,
each green through `python scripts/check.py` before moving on. Phase 2 is the only one needing a new
runner (Node) and a template change.

## Definition of done (whole effort)
- `python scripts/check.py` green with every new file discovered.
- Each task's named cases present and asserting real values (not just "doesn't throw").
- CI runs the Python gate **and** `node --test` (after Phase 2).
- A deliberate regression in each fixed-this-session behavior (pet interrupts, falsy-zero, death-timeline
  ability, ret/rogue scoring) turns a test red — the suite has teeth, not just coverage.
