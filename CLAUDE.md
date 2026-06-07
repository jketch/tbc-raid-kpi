# Raid KPI Dashboard — Claude Code Context

## What This Project Is

A weekly raid analytics pipeline for **TBC Anniversary** 25-man content (SSC + TK).
It is a **raid-wide accountability/insight tool** — it looks across all 25 raiders to
surface who's pulling weight and who needs a nudge. Tone leans toward *call-outs with
humor* (a "Hall of Shame" angle), not a personal/parse dashboard.

Each week the maintainer runs `run_weekly.bat`, enters a WCL report code, and the pipeline:
1. Authenticates against `fresh.warcraftlogs.com` OAuth
2. Pulls fight data via WCL v2 GraphQL API (`www.warcraftlogs.com/api/v2/client`)
3. Parses `WoWCombatLog.txt` for data WCL doesn't expose directly (engineering, drums, interrupts, avoidable mechanics)
4. Calculates KPIs, injects them into `dashboard/raid_kpi_dashboard.html` as a `WEEK_DATA` JS object
5. Opens the dashboard in-browser

**Not personal:** the tool is for the whole roster. (Maintainer's character is *Marvels* — a
Warlock — but that's just one of the 25; there is no Marvels-specific view.)

---

## File Map

```
Gaming/
├── run_weekly.bat                  ← entry point, prompts for WCL report code
├── .env                            ← WCL_CLIENT_ID, WCL_CLIENT_SECRET (never commit)
├── CLAUDE.md                       ← this file
├── scripts/
│   ├── wcl_auto_dashboard.py       ← main pipeline (~1290 lines)
│   └── db_writer.py                ← SQLite persistence layer (called at end of main())
├── dashboard/
│   └── raid_kpi_dashboard.html     ← self-contained HTML dashboard (Chart.js 4.4.1 via CDN)
├── logs/                           ← drop WoWCombatLog.txt here (newest .txt auto-selected)
└── cache/
    ├── <item_id>.json              ← item crit cache, persisted across runs
    ├── raid_history.db             ← SQLite: all KPIs, one row per player per week
    └── raid_history_test.db        ← SQLite: safe test target (--test-db flag), delete freely
```

---

## Core Architecture

### `wcl_auto_dashboard.py`

Pipeline functions (in execution order):
- `get_token()` — OAuth2 client_credentials against fresh.warcraftlogs.com
- `gql(token, query, variables)` — GraphQL wrapper with retry/backoff
- `build_week_data(report_code, token)` — main orchestrator; returns the `wcl` dict
- `parse_combat_log(log_path)` — parses raw combat log; returns `log_data`
- `merge_log_into_wcl(wcl_data, log_data)` — overlays combat-log results onto the wcl dict
- `map_to_week_data(wcl)` — transforms the merged dict into the `WEEK_DATA` shape the HTML renders
- `inject_into_html(week_data, html_path)` — replaces `const WEEK_DATA = {...}` in the HTML
- `db_writer.write_week(week_data, db_path=None)` — upserts all KPI tables into SQLite; called at end of `main()` after `inject_into_html()`

> **Note:** the script forces UTF-8 stdout at startup (`sys.stdout.reconfigure`) because the
> Windows cp1252 console crashes on the ✓/✅/▲ glyphs it prints.

### `WEEK_DATA` schema — **as actually emitted by `map_to_week_data()`**

```js
{
  meta:   { date, zone, kills, report_code },     // NOT flat reportCode/raidName
  roster: { [name]: { class, spec, role } },      // drives class colors in the UI

  consumables:  [{ name, role, score, suboptimal }],   // ⚠ currently a broken proxy (see below)
  drums:        [{ name, casts, total, buffs, buffs_per_drum, score }],
  avoidableDmg: [{ name, role, dmg }],                  // source: combat-log spell-NAME whitelist
  deaths:       [{ name, role, total, trash }],         // trash always 0 (kill fights only)
  luckKPI:      [{ name, role, actual, expected, luck }],   // key is `luck`, not `delta`
  engineering:  [{ name, role, eng: { [abilityName]: count }, dmg }],   // eng is a DICT
  interrupts:   [{ name, count }],
  casterCrit:   [{ name, crit }],
  physicalCrit: [{ name, crit }],
  healerCrit:   [{ name, crit }],
  tankCrit:     [{ name, crit }],
  tankMit:      { bear:{}, pally:{} },
  trinkets:     [],   // ⚠ STUB — hardcoded empty, not implemented
  gearFlags:    [],   // ⚠ STUB — hardcoded empty, not implemented
  boss_times:   { [bossName]: seconds },
}
```

Also note a **dead `WCL_AUTO_DATA` block** lower in the HTML (~line 1400) that nothing reads —
only `WEEK_DATA` is consumed. Safe to delete; don't be fooled by its stale per-player values.

### WCL v2 GraphQL patterns

OAuth token comes from `fresh.warcraftlogs.com/oauth/token`; **all queries** hit
`www.warcraftlogs.com/api/v2/client`. Pagination: if `nextPageTimestamp` is non-null, re-query
with `startTime: nextPageTimestamp`. (See the `Q_*` query constants in the script for the exact
shapes used: report/fights, playerDetails, DamageDone/Deaths/Healing tables, combatantinfo events,
DamageDone/Healing events, item stats, and `worldData.encounter(...).characterRankings` for the
healer-WAR cohort.)

---

## Current KPI Status (honest)

| KPI | Source | Status |
|-----|--------|--------|
| Luck / Relative Crit (actual − expected) | combatantinfo crit + damage events | ✅ Works for DPS; tanks via combat-log backfill; **healers have no gear-crit** (see gotcha) |
| Avoidable damage taken | **combat log** (`AVOIDABLE_SPELL_NAMES`, by spell name) | ✅ Works (severity-ranked, class-colored) |
| Deaths | WCL Deaths table (counted per event) | ✅ Fixed — was reading a non-existent `total` field |
| Drums of Battle | combat log | ✅ Fixed — `total` field added |
| Engineering (sappers/bombs) | combat log | ✅ Table reads the `eng` dict by ability name |
| Interrupts | combat log | ✅ Works |
| Consumable score | `potionUse`/`healthstoneUse` **proxy** | ⚠ Broken — list is empty unless someone used a pot; **not** the Buffs table. Needs real flask/elixir/food uptime. |
| Trinket usage | — | ⚠ Stub (`[]`) |
| Gear flags | — | ⚠ Stub (`[]`) |

---

## Backlog (merged plan)

Keep the "feels fine" keepers — **Luck/Relative Crit** and **Avoidable Damage** — and build
outward. Priority order:

1. **Cleanup** — this doc; drums/deaths/eng fixes (done); decide trinkets/gearFlags (build or drop); **real consumable scoring** from WCL Buffs table (flask/elixir/food uptime).
2. **Avoidable-damage visual** — current severity bars are hard to read; redesign (use the `frontend-design` skill).
3. **Hall of Shame** — raid-wide call-out cards with humor (worst avoidable, most deaths, lowest consumables, etc.). The accountability hook.
4. **Debuff Coverage** — CoE/CoS/Faerie Fire/Sunder/ISB/Blood Frenzy uptime on boss (pure WCL). Share its spell-ID map with any future curse/ISB logic.
5. **Mechanic Compliance** — per-boss "who ate Spout/Pounding/Shock Blast." **Unify with Avoidable Damage** on one ID-based spell map; don't keep name-based + ID-based both.
6. **Healer WAR** — relative crit + HPS vs a **cohort median** ("replacement level"), same-spec, same boss, duration ±15s. Baseline is **cached** (`cache/healer_baseline.json`, keyed by encounter+class+spec, holding raw cohort samples), refreshed ~monthly with a `--refresh-baseline` flag and lazy per-(boss,spec) population. Weekly runs read the cache → zero extra API cost; ±15s match happens at scoring time.
7. **Bloodlust Optimization** — per-fight BL timing / boss HP / raid mana (pure WCL).
8. **Mana Economy** — pots/innervates/OOM. Use WCL **`Resources` events**, not combat-log `UNIT_POWER_UPDATE` (unreliable in TBC 2.5 logs).
9. **Progression Velocity** — week-over-week, **appends** into the HTML (read existing `WEEK_DATA` via the bracket-counter, push, write back).

---

## Dashboard HTML Conventions

- Each KPI = a `<div class="card">`; render functions live in the inline `<script>` and run on load.
- **Class colors:** `CLASS_COLORS` (TBC 9-class palette; Priest uses web-tuned `#F0EBE0`).
  `nameColor(name, role)` resolves class via `WEEK_DATA.roster`, falling back to `roleColor`.
  Wrap class-colored names in `<span class="cname" ...>` (adds a dark text-shadow for legibility).
- Throughput bars (engineering, interrupts) and crit charts are class-colored; "judgment" bars
  (consumables green/yellow/red, avoidable severity-red) keep semantic colors and class-color the *name*.
- Structural accent stays gold (`--accent: #c89b3c`); it contrasts cleanly with every class color.
- CSS vars: `--green/--red/--orange/--blue/--muted/--bg/--accent`.

---

## SQLite History DB (`scripts/db_writer.py`)

Every weekly run writes to `cache/raid_history.db` via `write_week(week_data)`. Failure is caught and printed as a warning — the HTML dashboard still updates even if the DB write fails.

### CLI flags
```
python scripts\wcl_auto_dashboard.py REPORTCODE            # prod DB
python scripts\wcl_auto_dashboard.py REPORTCODE --test-db  # writes to raid_history_test.db only
python scripts\wcl_auto_dashboard.py REPORTCODE --dry-run  # no HTML, no DB — prints JSON only
```

### Schema (all tables keyed on `report_code`)
| Table | Grain | Key columns |
|-------|-------|-------------|
| `weeks` | 1 row/week | `report_code`, `date`, `zone`, `kills` |
| `roster` | 1 row/player/week | `player`, `class`, `spec`, `role` |
| `luck_kpi` | 1 row/player/week | `actual`, `expected`, `luck` |
| `avoidable_dmg` | 1 row/player/week | `dmg` |
| `deaths` | 1 row/player/week | `total`, `trash` |
| `consumables` | 1 row/player/week | `score`, `suboptimal` (JSON list) |
| `drums` | 1 row/player/week | `casts`, `total`, `buffs`, `score` |
| `engineering` | 1 row/player/week | `dmg`, `abilities` (JSON dict) |
| `interrupts` | 1 row/player/week | `count` |
| `crit` | 1 row/player/type/week | `crit_type` ∈ {caster,physical,healer,tank}, `crit_pct` |
| `boss_times` | 1 row/boss/week | `seconds` |

All inserts use `INSERT OR REPLACE` — re-running the same report code is safe/idempotent.

### Helper functions in db_writer.py
```python
from scripts.db_writer import query, trend, shame_board

query("SELECT player, AVG(luck) FROM luck_kpi GROUP BY player ORDER BY 2 DESC")
trend('Marvels', 'luck')      # week-over-week luck for one player
shame_board()                  # latest week: avoidable dmg + deaths + consumable score
shame_board('2026-05-22')      # specific week
```

---

## Constraints & Gotchas

- **`inject_into_html()` uses brace-depth counting, NOT a regex.** It walks `{`/`}` (string/escape
  aware) to find the matching close. A `re.sub(r'... \{.*?\};', re.DOTALL)` approach would truncate
  at the first `};` and corrupt the data — don't "simplify" it to that. New top-level keys just work.
- **Healer gear crit is unavailable.** WoW Classic 2.5.x emits `0` for healers' spell-crit in
  `COMBATANT_INFO` — confirmed in both the WCL API and the raw log (a real caster DPS reports crit
  fine). So healer "expected crit" can't come from gear; measure crit from **healing events** instead
  (hitType 2 = crit) and use a cohort/relative baseline. Tanks' melee crit IS present (log field `[11]`).
- Combat-log `COMBATANT_INFO` crit indices (TBC 2.5): `[11]` critMelee, `[12]` critRanged, `[13]` critSpell.
- WCL rate limit ~300 points/min; complex queries cost more. Reuse `gql()` retry logic.
- `fresh.warcraftlogs.com` is a separate OAuth host but the **same** GraphQL API URL.
- Item/gem cache in `cache/` is keyed by item ID — don't break its structure.
- Combat-log timestamps are wall-clock strings (`M/D H:MM:SS.mmm`); `_parse_ts()` converts them.
- Submerge phases (Lurker, Vashj) make the boss actor inactive — filter to the active window + boss `targetID`.
- TBC debuff limit is effectively capped — a 0% uptime on a debuff that was cast may mean it got bumped.
