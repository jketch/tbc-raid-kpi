# Raid KPI Dashboard — Claude Code Context

## What This Project Is

A weekly raid analytics pipeline for **TBC Anniversary** 25-man content (SSC + TK).
It is a **raid-wide accountability/insight tool** — it looks across all 25 raiders to surface
contribution and preparation.

**Tone — clean, principal-level.** The audience is raiders who know the game. Copy should only
explain *how a metric is calculated* or *how to read it* — cut anything that moralizes or states the
obvious. Positive individual call-outs (top DPS/healer, "tryhard" prep) are fine; **no
naming-and-shaming**, no shame-red on people. The tool *started* as a "Hall of Shame / call-outs with
humor" angle and has **deliberately moved away** from it — don't reintroduce that framing or preachy copy.

Each week the maintainer runs `run_weekly.bat`, enters a WCL report code, and the pipeline:
1. Authenticates against `fresh.warcraftlogs.com` OAuth
2. Pulls fight data via WCL v2 GraphQL API (`www.warcraftlogs.com/api/v2/client`)
3. Parses `WoWCombatLog.txt` for data WCL doesn't expose directly (engineering, drums, interrupts,
   avoidable mechanics, MC, friendly fire, consumable *use*, death-recap HP)
4. Calculates KPIs, injects them into `dashboard/raid_kpi_dashboard.html` as a `WEEK_DATA` JS object
5. Opens the dashboard, then `publish.py` deploys it to Netlify

**Not personal:** the tool is for the whole roster. (Maintainer's character is *Marvels* — a
Warlock — but that's just one of the 25; there is no Marvels-specific view.)

---

## File Map

```
Gaming/
├── run_weekly.bat                  ← entry point; prompts for report code, runs pipeline + publish.py
├── .env                            ← WCL_CLIENT_ID, WCL_CLIENT_SECRET (never commit; gitignored)
├── CLAUDE.md                       ← this file
├── prompts/                        ← maintainer's scratch feature-prompts (GITIGNORED; ref by path)
├── scripts/
│   ├── wcl_auto_dashboard.py       ← main pipeline (~3155 lines)
│   ├── db_writer.py                ← SQLite persistence layer (called at end of main())
│   ├── publish.py                  ← Netlify deploy + paste-ready raid-channel summary
│   ├── backfill_dps.py             ← one-off: pull historical DPS rows from WCL into `dps` table
│   ├── backfill_tank.py            ← one-off: pull historical tank v2 rows from WCL into tank tables
│   └── screenshot_dashboard.py     ← Playwright export: per-tab PNG or merged PDF (requires pypdf)
├── dashboard/
│   └── raid_kpi_dashboard.html     ← self-contained HTML dashboard (Chart.js 4.4.1 via CDN)
├── logs/                           ← drop WoWCombatLog.txt here (newest .txt auto-selected)
├── .deploy/                        ← staged copy publish.py deploys (gitignored)
├── .netlify/                       ← Netlify site link/state (gitignored)
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
- `build_week_data(report_code, token, ...)` — main orchestrator; returns the `wcl` dict
  - `fetch_fight_roles(token, code, kills)` — per-fight player role resolution from WCL combatantinfo
  - `fetch_healing_by_fight(token, code, kills)` — batched per-fight healing tables
  - `fetch_uptime_by_fight(token, code, kills)` — per-fight DPS uptime
  - `build_tank_scorecard_extended(token, code, kills, ...)` — full tank v2: DTPS, phys/magic split,
    crush/crit/avoid, defensive CDs, biggest hit, per-boss breakdown — all from WCL DamageTaken
  - `fetch_deaths_split(token, code)` — deaths split boss/trash with per-death recaps
  - `fetch_healing_spells(token, code, fights, actors)` — per-healer spell breakdown
  - `fetch_healer_mana(token, code, fight_ids)` — healer mana from WCL Resources events
  - `fetch_role_spell_usage(token, code, fight_ids, players)` — role-based spell usage
  - `compute_healer_war(token, heal_by_fight, ...)` — WAR cohort comparison
  - `build_consumable_compliance(consumable_usage)` — pure reshape of consumableUsage into compliance grid
- `parse_combat_log(log_path)` — parses raw combat log; returns `log_data`
- `merge_log_into_wcl(wcl_data, log_data)` — overlays combat-log results onto the wcl dict
- `map_to_week_data(wcl)` — transforms the merged dict into the `WEEK_DATA` shape the HTML renders
- `enrich_with_trends(week_data, db_path)` — reads prev week from DB, injects `delta_*` fields;
  called AFTER `build_week_data()` and BEFORE `inject_into_html()`
- `inject_into_html(week_data, html_path)` — replaces `const WEEK_DATA = {...}` in the HTML
- `db_writer.write_week(week_data, db_path=None)` — upserts all KPI tables into SQLite; called at
  end of `main()` after `inject_into_html()`

### `publish.py`
- `deploy_netlify()` — `netlify deploy --prod --dir .deploy` (copies the dashboard → `.deploy/index.html`)
- `build_summary(week_data)` — the paste-ready raid-channel blurb (see Publish workflow below)
- `extract_week_data()` — brace-matches `WEEK_DATA` out of the HTML (same logic as the injector)

> **Note:** the pipeline forces UTF-8 stdout at startup (`sys.stdout.reconfigure`) because the
> Windows cp1252 console crashes on the ✓/✅/▲/emoji glyphs it prints. `publish.py` does the same.

### `WEEK_DATA` schema — **as actually emitted by `map_to_week_data()`** (return dict ~L1931)

```js
{
  meta:   { date, start_ms, zone, kills, report_code, log_missing:[] },
  roster: { [name]: { class, spec, role } },     // role ∈ Tank|Healer|Physical|Caster (capitalized)

  consumables:   [{ name, role, class, score, max_score, badges:[...] }],  // Raid Prep, 0–10 (see below)
  drums:         [{ name, casts, total, buffs, buffs_per_drum, score }],   // score = raw buff count, NOT %
  avoidableDmg:  [{ name, role, class, dmg, sources:[...] }],              // combat-log spell-NAME whitelist
  avoidableMechanics: {...},                                              // per-mechanic breakdown
  friendlyFire:  [{ name, role, dmg, incidents, ... }],                   // clumping splash
  mcSaves:       [{ name, role, spells:{}, targets:{}, hits:[] }],        // CC'd a charmed ally (deduped)
  mcLiable:      [{ name, role, dmg, hits, kills, spells:{}, events:[] }],// AoE'd into a charmed ally
  consumableUsage: [{ name, role, flask, elixirs:[], food, weapon_oil, potion, rune, ... }], // raw audit
  healing:       [{ name, eff_hps, overheal_pct, activity_pct, vs_replacement, spells:[], ... }],
  tankScorecard: [{ name, dtps, taken, hps_recv, fights_tanked, fights_total, deaths,    // v2 (all WCL except lowest_hp):
                    phys_pct, magic_pct, crush_count, crit_count, avoid_pct,
                    biggest_hit:{amount,ability,boss}, cooldowns:{}, per_boss:[{boss,dtps,taken,seconds}],
                    lowest_hp:{[boss]:pct}/*log enrichment*/, delta_dtps/*trend*/ }],
  roleSpells:    { [role]: [{ability, casts, players}] },
  playerSpells:  { [role]: [{name, role, total, abilities:[{ability,casts}]}] },
  damage:        [{ name, role, total_dmg, active_pct, uptime_by_fight }],  // top 10
  deaths:        [{ name, role, total, trash, recap:[...] }],               // recap powers HP-timeline drill
  casterCrit / physicalCrit / tankCrit / healerCrit: [{ name, crit }],
  luckKPI:       [{ name, role, actual, expected, luck, series, ... }],     // key is `luck`
  engineering:   [{ name, role, eng:{ [abilityName]: count }, dmg }],       // eng is a DICT
  interrupts:    [{ name, count }],
  boss_times:    { [bossName]: seconds },                                   // flat; HTML reads as a number
  boss_meta:     { [bossName]: { seconds, encounter_id, deaths, raid_dps?, delta_seconds? } }, // ADDITIVE — powers boss tiles (portrait/delta/chips)
  healReaction:  { ...per-raider/boss reaction medians... },
}
```

- **Cohort scorecards** and **boss-zone tiles** (Overview) are **computed in JS at render time** from
  the arrays above — they are NOT separate emitted keys.
- The old doc's `tankMit`, `trinkets`, `gearFlags` keys are **not emitted** anymore (trinkets/gear
  flags were never built). Don't reference them.
- A **dead `WCL_AUTO_DATA` block** lower in the HTML is read by nothing — only `WEEK_DATA` is consumed.

### Render functions (boot order in `DOMContentLoaded`, near end of HTML)
`renderHeader, renderStatTiles, renderBossTiles, renderCohortCards, renderOverview, renderDrums,
renderAvoidableShame, renderFriendlyFire, renderMC, renderDeaths, renderHealthstones, renderDamage,
renderUptimeHeatmap, renderLuckGrid, renderHealing, renderReactionHeatmap, renderTanks, renderEngTable,
renderInterruptBars, renderRoleSpells, renderPlayerSpells, renderRaidPrep`, then `showTab("overview")`.
Helper: **`gicon(slug)`** builds a CDN icon `<img>` (see HTML Conventions).

### WCL v2 GraphQL patterns
OAuth token from `fresh.warcraftlogs.com/oauth/token`; **all queries** hit
`www.warcraftlogs.com/api/v2/client`. Pagination: if `nextPageTimestamp` is non-null, re-query with
`startTime: nextPageTimestamp`. See the `Q_*` constants (report/fights, playerDetails,
DamageDone/Deaths/Healing tables, **combatantinfo events**, DamageDone/Healing events, item stats,
`worldData.encounter(...).characterRankings` for the healer-WAR cohort). `table(dataType: Casts/Buffs)`
returns an opaque JSON blob — Casts is **player-centric** (`data.entries[]`), Buffs is **aura-centric**
(`data.auras[]`; see Data Source Map).

---

## Data Source Map  ★ READ BEFORE WRITING A KPI PROMPT

Target the right source — most "use the WCL Buffs table" ideas for buffs/consumables **do not work**.

| Data | Real source (what to query/parse) |
|------|-----------------------------------|
| **Consumables present at pull** (flask, food, elixirs, weapon oil) | **COMBATANT_INFO event auras** → `ci_consumables` (per-player; catches pre-applied buffs) |
| **Consumables *used* mid-fight** (combat pot, Dark/Demonic Rune, Flame Cap) | **combat-log casts** → `consum_use` (per-player counts, via `_consumable_category` / `POTION_BUFFS`) |
| Avoidable dmg, drums, interrupts, engineering, MC saves/liabilities, friendly fire, death-recap HP | **combat log** (`parse_combat_log`) |
| DPS/HPS, totals, deaths, uptime, DPS/tank crit, healer-WAR cohort | **WCL v2 tables/events** |

### Why the obvious WCL-buff approach fails (proven via live probe — don't repeat it)
- **Buffs *table* is aura-centric** — `data.auras[]` lists *which* auras appeared, **not who had them**.
  No per-player attribution.
- **Buffs *events* miss pre-pull consumables** — flask/food/elixir are applied **before** the pull, so
  no `applybuff` fires inside the logged fight windows. A filtered Buffs-events query returns **0**.
- **Casts *table* has no potion/rune rows** in TBC 2.5 logs.
- **Prompt "spell IDs" for flasks are item/cast IDs, not buff-AURA IDs.** Real flask *aura* IDs seen
  live: `28520/28521/28540` (Relentless Assault / Blinding Light / Pure Death) — **not** the `28589/
  28591…` lists. Don't trust ID lists from memory; confirm against live data with `--dry-run`.
- **Reuse the curated name sets already in the code** (don't re-derive): `ELIXIR_BUFFS`,
  `GUARDIAN_ELIXIRS`, `POTION_BUFFS`, `FOOD_BUFF`, `CC_ABILITIES`, `MC_AURAS`, `AOE_ABILITIES`,
  `AVOIDABLE_SPELL_NAMES`.

**Rule of thumb:** *buff present at the pull → COMBATANT_INFO; item used mid-fight → combat-log casts.
The WCL Buffs/Casts tables are not a per-player consumable source.*

---

## WCL-Durability Principle  ★ APPLIES TO EVERY KPI

Combat logs are 180 MB+ and frequently **don't get transferred** between raiders, but **WCL always
has 1–2 loggers**. So the load-bearing rule for the whole dashboard:

> **Every KPI gets a WCL-sourced "headline" that runs every week. The combat log is *additive
> enrichment* (drill-downs, HP timelines, attribution) — never the sole source for a whole section.**
> A missing combat log may thin a KPI; it must never blank one.

Concretely: WCL is the **source of record**; combat-log overlays **degrade silently** when absent
(guard every log read, default to the WCL value). When adding/retrofitting a KPI, find its durable
WCL path first, then layer the log on top.

### Tiering (where each KPI stands)
- **WCL-durable today:** DPS/HPS, deaths (+ per-boss tile counts), crit/luck, healer WAR, boss
  times + portraits, consumables-at-pull, **tank scorecard v2** (DTPS, per-boss, phys/magic school
  split, crush/crit mitigation, avoidance, defensive-cooldown casts, biggest hit — all WCL).
- **Log-only today but with a known WCL path (ROADMAP — not yet built):** interrupts → WCL
  `Interrupts` table (high confidence); avoidable damage → WCL `DamageTaken` by ability-ID (unify
  with backlog #2 Mechanic Compliance); engineering → WCL `Casts`; drums → evaluate WCL
  `Casts`/`Buffs` (lower confidence — WCL is less reliable here). Combat log stays as the
  drill-down layer for each.
- **Genuinely log-only (accept graceful degradation):** MC saves/liabilities, consumables-used
  mid-fight (TBC `Casts` has no potion/rune rows), friendly-fire clumping, death-recap HP%-timeline
  + reaction heatmap, tank **lowest-HP%-survived**.

> **hitType enum (LOCKED via live probe — don't trust memory):** WCL `DamageTaken` events expose
> `hitType` — `1` hit · `2` crit · `4` blocked(partial) · `15` crushing; `0`/`7`/`8` = miss/dodge/parry
> (zero damage = avoided). Confirmed by a crit-immune bear showing only `{0,1,7,15}`. The `DamageTaken`
> *table* also gives a per-ability `type` = damage **school** (`1` = physical) → phys/magic split is
> WCL-durable, no log needed. Boss portrait CDN uses the **de-prefixed** encounter id
> (`assets.rpglogs.com/img/warcraft/bosses/{encounterID − 100000}-icon.jpg`).

---

## Current KPI Status (honest)

| KPI | Source | Status |
|-----|--------|--------|
| Luck / Relative Crit (actual − expected) | combatantinfo crit + damage events | ✅ DPS + tanks; **healers have no gear-crit** (gotcha) |
| Avoidable damage taken | combat log (`AVOIDABLE_SPELL_NAMES`) | ✅ severity-ranked, class-colored, drill-down |
| Deaths + death recap | WCL Deaths + combat-log HP timeline | ✅ click a raider → per-death HP curve + ledger |
| Drums of Battle | combat log | ✅ (`score` = raw buff count, not a %) |
| Engineering (sappers/bombs) | combat log | ✅ `eng` dict by ability name; fixed-layout table |
| Interrupts | combat log | ✅ |
| **Raid Prep (consumables)** | **COMBATANT_INFO pull auras + combat-log casts** | ✅ **0–10 tryhard score + badges** (was the broken proxy) |
| MC accountability | combat log | ✅ saves (deduped) + liabilities bar chart |
| Friendly Fire (clumping) | combat log | ✅ (mostly fires only on Vashj Static Charge) |
| Healer scorecard + WAR | WCL healing tables + cohort baseline | ✅ |
| Tank scorecard **v2** | **WCL `DamageTaken` table+events (primary)** + log lowest-HP | ✅ per-boss DTPS, phys/magic split, crush/crit + avoid%, defensive CDs, biggest hit |
| **Boss tiles** (Overview) | **WCL** fight objects + Deaths + DamageDone | ✅ portraits + kill-time delta pill + 💀 deaths / ⚔ raid-DPS chips |
| Uptime / Reaction heatmaps, Cohort cards, Spell usage | mixed | ✅ |
| Trinket usage / Gear flags | — | ❌ not built, not in `WEEK_DATA` |

**Raid Prep scoring (0–10):** flask **+4** (= both elixir slots) *else* battle-elixir **+2** / guardian-elixir **+2**;
food **+2**; weapon oil **+1**; bonus +1 each for Flame Cap, combat pot, mana rune. Base (7) is
COMBATANT_INFO (API-only, always works); the +3 bonus needs the weekly combat log.

---

## Backlog (open work)

1. **Debuff Coverage** — CoE/CoS/Faerie Fire/Sunder/ISB/Blood Frenzy uptime on boss (pure WCL). Share
   its spell-ID map with any curse/ISB logic.
2. **Mechanic Compliance** — per-boss "who ate Spout/Pounding/Shock Blast." **Unify with Avoidable
   Damage** on one ID-based spell map; don't keep name-based + ID-based both.
3. **Healer WAR refinements** — relative crit + HPS vs a cohort median ("replacement level"),
   same-spec/boss, duration ±15s. Baseline cached (`cache/healer_baseline.json`), refreshed ~monthly
   via `--refresh-baseline`; weekly runs read the cache (zero extra API cost).
4. **Bloodlust Optimization** — per-fight BL timing / boss HP / raid mana (pure WCL).
5. **Mana Economy** — pots/innervates/OOM. Use WCL **`Resources` events**, not combat-log
   `UNIT_POWER_UPDATE` (unreliable in TBC 2.5 logs).
6. **Trend / Progression reporting** ✅ **BUILT** — `enrich_with_trends()` live in pipeline; injects
   `delta_*` fields for DPS, HPS, avoidable, deaths, luck, drums, tank DTPS. `fmtDelta()` helper in
   HTML. `dps` and `healing` DB tables now populated. Backfill scripts cover historical gaps.
7. **Harden log-only KPIs onto WCL headlines** (per the WCL-Durability Principle above) — give each
   combat-log-only KPI a durable WCL source so a missing log thins but never blanks it: **interrupts →
   WCL `Interrupts` table** (high confidence), **avoidable → WCL `DamageTaken` by ability-ID** (unify
   with #2), **engineering → WCL `Casts`**, **drums → evaluate WCL `Casts`/`Buffs`**. Combat log stays
   the drill-down/enrichment layer.

---

## Dashboard HTML Conventions

- Each KPI = a `<div class="card">`; render functions live in the inline `<script>` and run on load.
- **Class colors:** `CLASS_COLORS` (TBC 9-class palette; Priest uses web-tuned `#F0EBE0`).
  `nameColor(name, role)` resolves class via `WEEK_DATA.roster`, falling back to `roleColor`. Wrap
  class-colored names in `<span class="cname">` (dark text-shadow for legibility).
- **WoW-icon CDN system (prefer real game icons over emoji):** `gicon(slug)` →
  `<img class="ticon…">`; base `https://wow.zamimg.com/images/wow/icons/large/<slug>.jpg`. Size
  classes: `.ticon` (14px) / `.ticon-sm` (13px) / `.ticon-lg` (17px) / `.ticon-hdr` (30px). Also
  `classIcon(name)` and `avMechIcon(ability)`. **Always verify a slug resolves before using it** —
  a valid slug returns HTTP 200 + real bytes; a bogus slug 404s (`curl` the URL to check).
- **Tabs are split across MULTIPLE `.tsec` blocks sharing the same `data-tab`** (e.g. drums,
  engineering, interrupts, spell-usage are four separate `data-tab="utility"` blocks). `showTab(name)`
  toggles `.hidden` on **all** matching blocks. Don't assume one tab = one container.
- **Section headers:** `.section-label` (15px gold) for primary; `.subsection-label` (12px muted) for
  sub. Cards carry a role-matched **left accent**; `.card.tank/.healer/.caster/.physical/.warn/.gold`
  set the top bar + left border.
- **CSS vars:** `--bg, --surface, --surface2, --border, --accent (#c89b3c gold), --accent2, --gold-soft,
  --amber, --dim, --text, --muted, --red, --orange, --yellow, --green, --blue, --purple, --cyan, --pink`
  and role colors `--tank (#3b82f6), --healer (#22c55e), --caster (#a855f7), --physical (#f97316),
  --drum`. Throughput bars are class-colored; judgment colors are semantic but **no shame-red on people**.

---

## Publish / Deploy workflow (`scripts/publish.py`)

- Deploys the dashboard to **Netlify**: live at `https://clinquant-taffy-c345c2.netlify.app`
  (admin: `app.netlify.com/projects/clinquant-taffy-c345c2`). Site was linked once via
  `netlify sites:create` (team *Marvels*); `.netlify/state.json` holds the link.
- The dashboard HTML is a **template**: `inject_into_html` replaces **only** the `WEEK_DATA` block, so
  edits to markup / render functions / CSS **persist across weekly regenerations** — you don't need to
  re-run the pipeline to see a design change (open the local file).
- `build_summary()` prints the paste-ready raid-channel blurb: kill time + positive leaders (top
  dmg/healer) + cohort stats (raid avoidable, interrupt breadth, drum coverage) — **no individual
  shaming**. Discord auto-post is disabled (guild webhooks locked).
- **Workflow rule:** regenerate locally with `--test-db` to **proof**; **deploy only when explicitly
  asked.** After a deploy, hard-refresh (Ctrl+Shift+R) — Netlify/browser cache.

---

## SQLite History DB (`scripts/db_writer.py`)

Every weekly run writes to `cache/raid_history.db` via `write_week(week_data)`. Failure is caught and
printed as a warning — the HTML still updates even if the DB write fails.

### CLI flags
```
python scripts\wcl_auto_dashboard.py REPORTCODE            # prod DB + writes HTML + deploys via run_weekly
python scripts\wcl_auto_dashboard.py REPORTCODE --test-db  # writes raid_history_test.db only (safe proof)
python scripts\wcl_auto_dashboard.py REPORTCODE --dry-run  # no HTML, no DB — prints week_data JSON only
```

### Schema (all tables keyed on `report_code`)
| Table | Grain | Key columns |
|-------|-------|-------------|
| `weeks` | 1 row/week | `report_code`, `date`, `zone`, `kills`, `start_ms` |
| `roster` | 1 row/player/week | `player`, `class`, `spec`, `role` |
| `luck_kpi` | 1 row/player/week | `role`, `actual`, `expected`, `luck` |
| `avoidable_dmg` | 1 row/player/week | `role`, `dmg` |
| `avoidable_sources` | 1 row/player/mechanic/boss/week | `mechanic`, `boss`, `dmg` — per-mechanic breakdown |
| `deaths` | 1 row/player/week | `role`, `total`, `trash` |
| `consumables` | 1 row/player/week | `role`, `score` (0–10), `badges` (JSON), `flask`, `food`, `weapon`, `combat_pot`, `alt_pot` (compliance columns) |
| `drums` | 1 row/player/week | `casts`, `total`, `buffs`, `score` |
| `engineering` | 1 row/player/week | `role`, `dmg`, `abilities` (JSON dict) |
| `interrupts` | 1 row/player/week | `count` |
| `crit` | 1 row/player/type/week | `crit_type` ∈ {caster,physical,healer,tank}, `crit_pct` |
| `boss_times` | 1 row/boss/week | `seconds`, `encounter_id` |
| `dps` | 1 row/player/week | `role`, `dps`, `total`, `pct_raid`, `uptime` |
| `healing` | 1 row/player/week | `role`, `eff_hps`, `eff_heal`, `overheal_pct`, `activity_pct`, `tank_pct`, `mana_eff`, `vs_replacement`, `top_spell` — **column is `eff_hps` not `hps`** |
| `healing_spells` | 1 row/player/spell/week | `spell`, `casts`, `eff`, `per_cast`, `overheal_pct`, `crit_pct` |
| `friendly_fire` | 1 row/player/week | `role`, `dmg`, `incidents`, `category`, `mc_dmg`, `eng_dmg`, `mech_dmg`, `mc_count` |
| `tank_scorecard` | 1 row/player/week | `dtps`, `taken`, `hps_recv`, `deaths`, `phys_pct`, `magic_pct`, `crush_count`, `crit_count`, `avoid_pct`, `biggest_hit`, `cooldowns` (JSON) |
| `tank_boss_dtps` | 1 row/player/boss/week | `dtps`, `taken`, `seconds` |

All inserts use `INSERT OR REPLACE` — re-running a report code is safe/idempotent. **Additive schema
changes use a guarded `try: ALTER TABLE … ADD COLUMN … except sqlite3.OperationalError: pass`** (so
existing DBs migrate; see the `badges` and `start_ms` migrations).

### Helper functions
```python
from scripts.db_writer import query, trend, shame_board
query("SELECT player, AVG(luck) FROM luck_kpi GROUP BY player ORDER BY 2 DESC")
trend('Marvels', 'luck')      # week-over-week luck for one player
shame_board('2026-05-22')     # a week's avoidable dmg + deaths + consumable score
```

---

## Constraints & Gotchas

- **`inject_into_html()` uses brace-depth counting, NOT a regex.** It walks `{`/`}` (string/escape
  aware) to find the matching close. A `re.sub(r'... \{.*?\};', re.DOTALL)` approach truncates at the
  first `};` and corrupts the data — don't "simplify" it. New top-level keys just work.
- **Consumables/buffs are NOT sourced from the WCL Buffs/Casts tables** — see the Data Source Map. The
  Buffs table is aura-centric, Buffs events miss pre-pull buffs, and prompt spell-IDs are item IDs. Use
  COMBATANT_INFO (`ci_consumables`) + combat-log casts (`consum_use`).
- **Healer gear crit is unavailable.** WoW Classic 2.5.x emits `0` for healers' spell-crit in
  `COMBATANT_INFO` (confirmed in API + raw log). Measure healer crit from **healing events**
  (hitType 2 = crit) + a cohort/relative baseline. Tanks' melee crit IS present (log field `[11]`).
- Combat-log `COMBATANT_INFO` crit indices (TBC 2.5): `[11]` critMelee, `[12]` critRanged, `[13]` critSpell.
- **MC saves are deduped** — one save per caster per controlled ally per **MC episode** (reset on the
  MC aura applying/removing). Re-casting CC to keep someone parked doesn't inflate the count.
- WCL rate limit ~300 points/min; complex queries cost more. Reuse `gql()` retry logic.
- `fresh.warcraftlogs.com` is a separate OAuth host but the **same** GraphQL API URL.
- Item/gem cache in `cache/` is keyed by item ID — don't break its structure.
- Combat-log timestamps are wall-clock strings (`M/D H:MM:SS.mmm`); `_parse_ts()` converts them.
- Submerge phases (Lurker, Vashj) make the boss actor inactive — filter to the active window + boss `targetID`.
- TBC debuff limit is effectively capped — a 0% uptime on a debuff that was cast may mean it got bumped.
- **Tabs:** content for one tab is spread across several `.tsec` blocks with the same `data-tab` (above).
- **Icons:** verify a Zamimg slug resolves (200 vs 404) before wiring it in.
- **Deploy discipline:** regenerate with `--test-db` to proof; deploy to Netlify only when explicitly
  asked; the live site is stale until you deploy.
- **`prompts/` is gitignored** — the `@` picker won't show it; reference scratch prompts by path
  (e.g. `prompts/TREND_DATA_PROMPT.md`).
- **`healing` DB column is `eff_hps`** (not `hps`). The DB stores the raw value from `WEEK_DATA.healing[].eff_hps`
  directly. Any code reading from the `healing` table must use `eff_hps`, not `hps`.
- **Backfill scripts** (`backfill_dps.py`, `backfill_tank.py`) are safe to re-run — all inserts use
  `INSERT OR REPLACE`. They gap-fill older weeks without touching log-sourced tables.
- **`screenshot_dashboard.py`** uses Playwright — requires `pip install playwright pypdf` and
  `playwright install chromium`. Outputs PNG-per-tab or a single merged PDF to `screenshots/`.
