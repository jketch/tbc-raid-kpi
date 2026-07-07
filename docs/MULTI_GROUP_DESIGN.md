# Multi-group / multi-tenant orchestration — design

## 1. Goal & context

One operator runs **multiple 25-man groups** under one WCL guild (today *Marvels*; next *Pills*).
Each group needs its **own dashboard URL** (separate Netlify site → separate audience/access) built
from its **own** weekly data, **own** trend chain, and **own** loot. The pipeline already does all of
this correctly — for exactly one tenant. This doc adds a `--group` axis without forking.

**Why one codebase, not clones.** The tool changes weekly (new KPIs, schema bumps, template edits).
Forked clones would drift: a fix to `wcl_fetchers` or `template.html` would have to be hand-ported to
every clone, and the schema/contract guards (`week_schema.SECTIONS`, `check.py`) can't span repos.
Forking only fits a *different operator* who shouldn't see the repo — not this case. So:

> **Code and template stay SHARED; STATE and DEPLOY TARGET become group-scoped.** A `--group <name>`
> flag threads through `paths.py` so every cache/DB/loot/drops path is per-group; a `groups.json`
> config maps each group to its Netlify site + Dropbox subfolder; the orphan `data` branch carries
> per-group subtrees. `--group marvels` (the default) resolves to **today's exact layout** — an
> unflagged run behaves identically to now.

The explicit `report_code` argument **stays**. Under one WCL guild each group uploads its kills
separately, so "latest report" is ambiguous — the code names the report, `--group` names the tenant.

**Honest scope note.** The review of the v1 draft proved the work is wider than it first reads:
`paths.py` is consumed as **imported constants**, not via a function, so adding `get_paths()` changes
**nothing** until every constant-consumer is rewritten and the group is *threaded through the call
chain* (§3, §4.2). Four write/read surfaces the v1 draft missed — the snapshot dumps, the render
target, the REPL helpers, and the local `logs/` fallback — must also be scoped before the "physically
incapable of seeing another group" claim in §4.3 is true. Net effort is **L**, not M–L (§7).

## 2. Non-goals

- **Not** a multi-operator / multi-repo product. No auth, no per-user RBAC, no hosted control plane.
  groups.json is operator-edited config, not a UI.
- **Not** a single shared DB with a `group` discriminator column. We isolate by **separate per-group
  DB files** (see §4.3 — this is the load-bearing decision). A `group` column is explicitly rejected
  as error-prone given how many queries would need the filter.
- **Not** a cross-group rollup / combined view. Each site shows one group. A "all my groups" landing
  page is a possible future, out of scope here.
- **Not** changing the WCL fetch layer, `map_to_week_data`, the KPI math, or the template render JS.
  Those are tenant-agnostic and stay byte-for-byte shared. The dashboard is differentiated only by the
  data injected into it (optionally a `display_name` in the header — injected **post-map** in
  `finalize_week`, NOT inside `map_to_week_data`, so this non-goal stays literally true; §4.8).
- **Not** concurrent in-process multi-group runs. One `--group` per invocation; parallelism is at the
  CI-job / separate-process level (per-group concurrency lock, §4.6).

## 3. Current single-tenant assumptions

Every row below is a hard-coded singleton today, **verified against the code**. "How to scope" is the
agreed fix. The critical realization: nothing here reads a *function* — every consumer imports a
module-level **constant** (`from paths import WEEK_DATA_CACHE, …`; `from db_writer import DB_PATH`),
so scoping means rewriting each call site to call a resolver **and** thread `group` to it.

| Subsystem | What's hard-coded (file:loc, verified) | How to scope |
|---|---|---|
| Path constants | `paths.py:10-31` — `CACHE_FILE` (item_crit), `LOGS_DIR`, `LOG_ARCHIVE_DIR`, `LOOT_DIR`, `DASH_FILE`, `WEEK_DATA_CACHE` (cache/week_data), `WCL_CACHE` (cache/wcl), `ITEM_META_CACHE`. **Imported as constants** by `wcl_auto_dashboard:60-61`, `build_site.py:28`, `reprocess.py:78,106,126`, `wcl_fetchers.py:18`, `crit_model.py:15` | `get_paths(group)` → group-scoped namespace; **rewrite every constant-consumer** to call it (enumerated in §4.2) |
| Snapshot dumps | `wcl_auto_dashboard.dump_week_data_cache:567` writes module-global `WEEK_DATA_CACHE`; `dump_wcl_raw_cache:589` writes module-global `WCL_CACHE`; `commit_week` (`week_build.py:124`) calls the former with **no group** | thread `group`/paths into `commit_week → dump_week_data_cache/dump_wcl_raw_cache` (the WRITE that feeds the glob — §4.3 finding) |
| Render target | `DASH_FILE` is a **single shared file**; `commit_week(render=True)` → `week_build.py:133`; `main()` injects into `Path(args.out)` default `DASH_FILE` (`:744`); `build_site.py:41,44,70` reads `DASH_FILE` as the embedded latest; `run_weekly.bat:67` opens the literal path | **per-group `DASH_FILE`** (`dashboard/raid_kpi_dashboard_<group>.html` for non-marvels); thread `--out`/group into `build_site`+`publish`; update the `.bat` open-path + the single `.gitignore` literal |
| History DB | `db_writer.py:34-35` — `DB_PATH`, `DB_PATH_TEST` | `db_path(group, test=False)` → `cache/<group>/raid_history[_test].db` |
| REPL helpers | `db_writer.query:718` (`sqlite3.connect(DB_PATH)`, unconditional); `crit_history:735`, `trend`, `shame_board` default to `DB_PATH` when `db_path` is None | add a `group=` param (or document loudly as **marvels-only**) — §4.3, not "no helper to forget" |
| Trend lookup | `trends.py:35-63` `enrich_with_trends(week_data, db_path)` — prior week by `start_ms < ?`, global over the DB | **Already takes `db_path`.** Isolation = pass the per-group DB; no `group` filter needed once DBs are separate |
| Rolling site | `build_site.py` globs `WEEK_DATA_CACHE/*.json` (newest 6), reads `DASH_FILE`, enriches vs `DB_PATH` | `build(group)` → glob `cache/<group>/week_data/`, read per-group `DASH_FILE`, enrich vs `db_path(group)`, stage `.deploy/<group>/` |
| Deploy | `publish.py:36-40` `DASH`, `DEPLOY`=.deploy, `LAST_DEPLOY`=cache/last_deploy.json; `_netlify_config()` one `(token, site_id)` | `--group`: `.deploy/<group>/`, `cache/<group>/last_deploy.json`, site_id from groups.json |
| Loot | `loot_parser.py` + `reingest_loot:607` — newest `LOOT_DIR/*.csv` by mtime | per-group `loot/<group>/*.csv`; explicit `--loot` still overrides |
| Item caches | `crit_model.py:15,114-120` reads/writes `CACHE_FILE`; `wcl_fetchers.py:18,577,608` reads/writes `ITEM_META_CACHE` | **reconsidered — share, seed-on-cold** (id-keyed reference data; §4.3 + open question §6) |
| Local logs | `wcl_auto_dashboard.py:697` falls back to newest `LOGS_DIR/WoWCombatLog*.txt`; `archive_log` → `LOG_ARCHIVE_DIR` (`:751`) when `WOW_LOG_DIR` unset | scope `logs/<group>/` + `logs/<group>/archive/`, **or** require explicit `--log`/`WOW_LOG_DIR` for non-marvels and refuse the shared fallback (§4.6) |
| Inputs (cloud) | `dropbox_drops.py:23-29` `DROPS_DIR`/`LOOT_DIR`/`PROCESSED="/processed"` rooted at ROOT; `weekly.yml:91` `WOW_LOG_DIR=…/drops` | per-group `drops/<group>/`, `loot/<group>/`, `WOW_LOG_DIR=…/drops/<group>`; Dropbox subfolder from groups.json; **archive sweeps the group subfolder** (§4.6) |
| State sync (local) | `sync_state.py:21-26` `CACHE`, `MARKER`=.data_branch_sync, `ALLOW_DIRS`/`ALLOW_FILES`; branch `data` hard-coded | `--group`: sync `cache/<group>/…`, marker `cache/<group>/.data_branch_sync`, into the `<group>/` subtree of `data` |
| State sync (cloud) | `weekly.yml:74-134` restores via **raw `cp -a .state/cache/. cache/`** and persists via inline `rm -rf .state/cache/week_data … ; cp -r … ; git add -A` — **does NOT call `sync_state.py`** | the workflow steps must independently learn the `<group>/cache/` subtree, scope `git add` to `.state/<group>`, and **stop `rm -rf`-ing the whole tree** (§4.5/§4.7) |
| Backfill / reprocess | `backfill_snapshots.py:33,39` imports global `DB_PATH`; `reprocess.py:152` resolves global `DB_PATH`/`DB_PATH_TEST` and globs global `WEEK_DATA_CACHE`/`WCL_CACHE` | `--group` plumbing into **DB and both cache dirs** — Phase 1/2 deliverable, not Phase 4 polish (§4.2, §5) |
| Workflow input | `weekly.yml:29-36` — `report_code` (plain string), `test_mode` (boolean); **no enum**; global `NETLIFY_SITE_ID`; `concurrency.group: weekly-state` | add `group` as `type: choice` with a literal `options:` list; resolve site_id from groups.json; `concurrency.group: weekly-state-<group>` |
| Orchestrator CLI | `wcl_auto_dashboard.main()` argparse — no `--group`; `run_weekly.bat` no prompt | add `--group` (default marvels) end-to-end; bat prompts for group |
| Facade surface | `tests/test_facade_surface.py` freezes the `W.<name>` re-export surface | re-bless when `get_paths`/`db_path`/`groups` re-exports land (§5 Phase 1) |

## 4. Proposed design

### 4.1 groups.json config model

A single `groups.json` at repo root, **committed** (Netlify site IDs and Dropbox subfolder names are
not secrets — the actual secrets stay in `.env` / GitHub Secrets). One loader module reads it.

```jsonc
{
  "groups": {
    "marvels": {
      "netlify_site_id":  "clinquant-taffy-c345c2",
      "dropbox_subfolder": "",            // "" = app-folder root (back-compat for the existing setup)
      "display_name":     "Marvels"
    },
    "pills": {
      "netlify_site_id":  "<pills-site-id>",
      "dropbox_subfolder": "pills",       // /pills/<files> inside the same Dropbox app folder
      "display_name":     "Pills"
    }
  }
}
```

- New leaf module `scripts/groups.py`: `load_groups()`, `group_config(name)` (raises on unknown
  name — **fail loud**, never silently fall through to a default tenant), `default_group()` →
  `"marvels"`. Pure stdlib `json`, no deps. Imported by `publish.py`, `build_site.py`,
  `dropbox_drops.py`, and the workflow (via a tiny `python -c` step).
- `display_name` is the only field the front-end may read; everything else is operator-side.
- **Enum-parity is a YAML diff, not a dispatch enum.** `workflow_dispatch` inputs cannot derive an
  enum from `groups.json` at definition time — the `options:` of a `type: choice` input is literal
  YAML. So: the workflow's `group` input is `type: choice` with a **hand-maintained `options:` list**,
  and `python scripts/groups.py check` **parses `weekly.yml`** and asserts
  `set(options) == set(groups.json keys)`. Wired into CI, a typo in either place fails the build, not
  a deploy. (The v1 "the enum must match groups.json" wording conflated config-validation with a
  dispatch enum that doesn't exist — this is the concrete mechanism.)

### 4.2 The `--group` flag + paths.py scoping (default = `marvels`)

`paths.py` today is **pure constants consumed by import** — `from paths import WEEK_DATA_CACHE, …`,
then code reads the module global directly. Adding `get_paths()` alongside the constants therefore
**redirects nothing on its own**; every consumer below must be rewritten to call the resolver *and*
the enclosing function must accept and thread a `group`. We keep the constants as the
`group="marvels"` values (for back-compat and REPL convenience) and add:

```python
# paths.py
DEFAULT_GROUP = "marvels"

def get_paths(group: str = DEFAULT_GROUP) -> "PathSet":
    """Group-scoped path namespace. get_paths('marvels') returns TODAY's exact layout
    (cache/week_data, cache/wcl, cache/raid_history.db, loot/, dashboard/raid_kpi_dashboard.html…)
    so an unflagged run is byte-identical to now. Any other group nests under cache/<group>/,
    loot/<group>/, drops/<group>/, and dashboard/raid_kpi_dashboard_<group>.html."""
```

**Back-compat rule (non-negotiable):** for `group == "marvels"`, every path equals the current
constant. For any other group, the same names resolve under a `<group>/` segment (and `DASH_FILE` gets
a `_<group>` filename suffix, since it shares one `dashboard/` dir). Zero migration for existing data;
an unflagged `run_weekly.bat` is unchanged.

**Required edits — the full constant-consumer list (this is the L, not an M):**

- `wcl_auto_dashboard`: `main()` gains `--group` (default `marvels`); resolve `paths = get_paths(group)`
  and `db_path = db_writer.db_path(group, test=args.test_db)` once.
- **Thread `group`/paths into the snapshot WRITERS** — `commit_week → dump_week_data_cache(mapped,
  paths)` and `dump_wcl_raw_cache(raw, paths)` (today both write the module global; under `--group
  pills` they'd land in Marvels' dirs — see §4.3). Also thread it into `reingest_loot` (globs
  `LOOT_DIR`) and the gear-audit's `ITEM_META_CACHE` reader and `crit_model.load_cache/save_cache`
  (`CACHE_FILE`).
- `week_build.from_wcl/from_snapshot → finalize_week → commit_week` carry `group`/`db_path` through.
- `inject_into_html` writes the per-group `DASH_FILE` (via `--out`); `build_site.build(group)` reads it.
- `db_writer`: `query`, `crit_history`, `trend`, `shame_board` gain `group=` (or are documented
  marvels-only — §4.3).
- `reprocess.py` and `backfill_snapshots.py` each gain `--group` plumbed into **both** the DB resolve
  **and** the `WEEK_DATA_CACHE`/`WCL_CACHE` globs (Phase 1/2, not polish).
- **Audit for direct path literals.** Any `ROOT / "cache" / "week_data"` style construction outside
  `paths.py` is a back-compat landmine (silently reads Marvels under `--group pills`). Grep
  `cache /`, `"loot"`, `"drops"`, `.deploy`, `raid_history` across `scripts/` and route every hit
  through `get_paths`/`db_path`.

### 4.3 STATE ISOLATION — the load-bearing decision

**This is the one part that must be perfect.** Trends and the rolling site are *historical* reads:

- `trends.enrich_with_trends` (trends.py:35-63) finds the prior week as **the most recent row with
  `start_ms < this week's start_ms`** — a chronological lookup, *global over the DB*, no notion of
  tenant. Share a DB and have Pills raid the night after Marvels, and **Pills' week deltas against
  Marvels' week**. Every `delta_*` field silently corrupts.
- `build_site._load_snapshots` takes the **newest 6** snapshots from one directory and re-enriches
  each. Mixed-tenant snapshots interleave by time — the rolling dropdown becomes a cross-group
  history, each past week trending against whatever week (any group) preceded it.
- `publish.py`'s section-loss guard compares the incoming week to one shared `last_deploy.json`
  baseline → cross-group false positives, or a real section drop slipping through because the baseline
  was the other group's.

**Decision: separate per-group state, full stop.** `cache/<group>/raid_history.db`,
`cache/<group>/week_data/`, `cache/<group>/wcl/`, `cache/<group>/last_deploy.json`, plus the per-group
`DASH_FILE`. The prior-week query, the snapshot glob, and the deploy baseline become *structurally*
isolated — they open a different file/dir, so cross-tenant reads are impossible. No `WHERE group = ?`
to forget, no column to backfill. A shared DB with a discriminator column is **rejected**: it would
need the filter in `enrich_with_trends`, `crit_history`, `build_site`, the downgrade guard, and every
REPL helper — one omission re-opens the contamination.

**The claim "physically incapable of seeing another group" is only true once the WRITE side and the
side doors are also scoped.** Four surfaces the v1 draft missed:

1. **Snapshot dumps** (§4.2) — `commit_week → dump_week_data_cache/dump_wcl_raw_cache` are unthreaded
   today; under `--group pills` they write Pills snapshots into `cache/week_data/`/`cache/wcl/`
   (Marvels' dirs), re-introducing the exact mixing this section forbids. They are the *write that
   feeds the glob*; thread paths through them. **Isolation test:** a `--group pills` run writes **zero
   bytes** under Marvels' `cache/week_data/` (assert the dir is untouched).
2. **Render target** — `DASH_FILE` is one shared file (§3); without a per-group filename a Pills run
   clobbers the Marvels HTML and `build_site` embeds the wrong group's latest. Per-group `DASH_FILE`.
3. **REPL helpers** — `query`/`shame_board`/`crit_history`'s default branch open `DB_PATH`
   unconditionally; an officer running `query(...)` for Pills silently hits Marvels' DB. Add `group=`
   or document marvels-only.
4. **Local `logs/` fallback** (§4.6) — `main():697` picks the newest `logs/WoWCombatLog*.txt` when
   `WOW_LOG_DIR` is unset, so a local Pills run could merge a Marvels log.

Corollary: `--test-db` becomes `cache/<group>/raid_history_test.db`, so `--test-db --group pills`
and `--test-db --group marvels` no longer collide. **Invariant:** `crit_history` must always receive
the *resolved* per-group `db_path` — `main()` already does (`:705-706`), but `backfill_snapshots.py`
and `reprocess.py` (which use the global today) must too, or `--test-db --group pills` baselines luck
against the wrong file.

**Item caches — reconsidered: share, don't isolate.** `item_crit_cache.json` (`crit_model`) and
`item_meta_cache.json` (`wcl_fetchers`) are **item-id-keyed reference data** — identical across groups
(an item's crit/sockets don't depend on tenant). The v1 draft isolated them "to avoid concurrent-run
races," but the real cost there is a **cold Pills cache re-fetching every item from Wowhead** (slower
first run, N× API hits) for data that already exists in Marvels' copy. id→stats never conflicts, so
last-writer-wins is safe without a lock. **Decision: keep the two item caches SHARED** (at the repo
root, unscoped); at minimum seed Pills from Marvels' copy. Isolate only the genuinely tenant-specific
state (DB, snapshots, last_deploy, DASH_FILE). This is the cheaper-and-correct call; the lock concern
was overstated for append-only identical-value writes.

### 4.4 Deploy & per-group Netlify URL

Per-group site = per-group URL = per-group audience/access (the whole point — Pills shouldn't see
Marvels' page and vice-versa).

- `publish.py` gains `--group`. `_netlify_config(group)` returns `(NETLIFY_AUTH_TOKEN,
  groups.json[group].netlify_site_id)` — **one shared auth token** (the operator owns all the sites),
  **per-group site id from config**. `NETLIFY_SITE_ID` in `.env`/secrets is retired in favor of
  groups.json (kept as the marvels fallback during migration).
- `build_site.build(group)` stages into `.deploy/<group>/` (its own `index.html` + `weeks/*.json` +
  `WEEKS_INDEX`), reads the embedded latest from the **per-group `DASH_FILE`**, enriches earlier weeks
  vs `db_path(group)`. `deploy_netlify(group)` zips `.deploy/<group>/` and POSTs to that group's site.
- `cache/<group>/last_deploy.json` is the per-group section-loss baseline.
- Each group's site keeps its **own** `WEEKS_INDEX` manifest and 6-week dropdown — naturally, because
  staging reads only that group's snapshot dir + DASH_FILE.

### 4.5 The `data`-branch per-group layout

The orphan `data` branch is canonical CI/local state (history DB + snapshots). Today its tree is
`cache/week_data/`, `cache/wcl/`, `cache/raid_history.db`, … at the root.

**Decision: one `data` branch, per-group SUBTREES** (not `data-marvels` / `data-pills` branches).
Subtrees keep a single branch to fetch/push and avoid branch proliferation; the cost is a bit more
path logic, which is contained. Layout on the branch:

```
data (orphan branch)
├── marvels/cache/{raid_history.db, week_data/, wcl/, last_deploy.json}
└── pills/cache/{raid_history.db, week_data/, wcl/, last_deploy.json}
```

(Item caches are NOT under the per-group subtree — they're shared reference data, §4.3; carry them at
the branch root or skip them from sync entirely, since they're cheap to rebuild.)

`sync_state.py` (the LOCAL path) changes:
- `pull <group>` / `push <group>` / `check <group>` (default `marvels`). The temp-worktree mechanism
  is unchanged; only the **source/dest subtree** moves: copy `<group>/cache/` on the branch ↔ local
  `cache/<group>/` (= `get_paths(group)` cache root).
- `ALLOW_DIRS`/`ALLOW_FILES` keep the same *names* (`week_data`, `wcl`, `raid_history.db`,
  `last_deploy.json`) but are joined under the group prefix on the branch side. `item_*.json` drops
  from the allowlist (now shared, not per-group).
- Per-group sync marker: `cache/<group>/.data_branch_sync`. `check <group>` compares that marker vs
  `origin/data` tip — but both groups share the branch tip SHA, so a push of group A advances the tip
  and group B's `check` then reports "behind" even though B's subtree is untouched. **Acceptable**:
  `check` is a conservative "pull before you run" guard; a redundant pull only overwrites B's own
  subtree with identical bytes. If noisy, refine `check` to diff only the `<group>/` subtree path.
- A push touches **only** the pushing group's subtree (`git add <group>/`), so A's push never clobbers
  B's state.

**The cloud path is raw shell, not `sync_state.py` — fix it independently.** `weekly.yml:74-134`
restores with `cp -a .state/cache/. cache/` and persists with `rm -rf .state/cache/week_data … ; cp
-r … ; git add -A` — it never calls `sync_state.py`. Under per-group subtrees that inline shell must:
restore from `.state/<group>/cache/` (not `.state/cache/`); persist **only** `.state/<group>/cache/`;
**stop `rm -rf`-ing the whole `.state/cache`** (at the subtree root that would delete the *other*
group's snapshots); and scope `git add` to `.state/<group>`. Cleanest: **route both steps through
`sync_state.py pull --group / push --group`** so the local and cloud paths share one subtree-aware
implementation instead of two copies of the layout logic.

**The one-time `git mv` is a migration HAZARD that must be atomic with the code deploy.**
`sync_state.py` records the synced SHA in `cache/.data_branch_sync` and `check()` compares it to
`origin/data` tip. The moment a `git mv` (root tree → `marvels/` subtree) advances the tip, **every
local clone's `check` reports "behind,"** and a clone still running the *old* `sync_state.py`/`weekly.yml`
would `cp -a .state/cache/.` and find **nothing** (cache now lives under `marvels/cache/`) → start from
empty state → trend chain breaks, the downgrade-guard sees an empty DB. So the migration is **one
atomic change**: deploy subtree-aware `sync_state.py` + `weekly.yml` *together with* the `git mv`, then
force a marker reset (`pull --group marvels`) on every clone before its next run. Sequence it; do not
treat the `git mv` as a casual "one-time, contained" step.

### 4.6 Inputs — Dropbox subfolder & combat logs

The cloud input drop (`dropbox_drops.py fetch|archive`) and combat-log discovery
(`log_discovery.discover_log`) become group-aware:

- `dropbox_drops.fetch(group)`: download into local `drops/<group>/` and `loot/<group>/`; list from
  the group's Dropbox subfolder (`groups.json[group].dropbox_subfolder` — `""` = app-folder root, the
  existing Marvels behavior).
- `dropbox_drops.archive(group)`: **sweep the GROUP's subfolder, not the root.** Today `archive`
  moves the app-folder *root* to `/processed` (`PROCESSED="/processed"`, root-level sweep). If Pills'
  files live in `/pills`, a root sweep never archives them (re-ingested next week) and could move
  Marvels' just-fetched root files mid-run if two groups overlap. So `archive(group)` sweeps
  `/<subfolder>` → `/<subfolder>/processed` (Marvels' empty-subfolder case stays `/` → `/processed`).
  **One Dropbox app, prefixed subfolders** — not a separate app per group (one refresh-token trio
  stays in secrets).
- `log_discovery` is already group-agnostic (it globs the directory it's handed). The cloud change is
  the **caller**: `WOW_LOG_DIR` points at `drops/<group>/`. Add a **loud warning if `WOW_LOG_DIR`
  doesn't exist** so a misconfig doesn't silently fall back to the manual `logs/` directory.
- **The local `logs/` fallback is a third, unscoped log source** (`main():697` picks newest
  `logs/WoWCombatLog*.txt` when `WOW_LOG_DIR` is unset; `archive_log` writes `LOG_ARCHIVE_DIR`). The
  cloud warning above doesn't help a *local* Pills run, which could pick up a Marvels log sitting in
  `logs/` and merge it into Pills' data. Fix: either scope `logs/<group>/` + `logs/<group>/archive/`,
  **or** make a non-marvels local run *require* an explicit `--log`/`WOW_LOG_DIR` and refuse the
  shared `logs/` fallback (preferred — simpler, and a wrong log is worse than a thin one).
- Loot newest-CSV auto-pick scopes to `loot/<group>/*.csv`. Explicit `--loot <path>`
  (`finalize_week(..., loot_csv=…)`) still overrides for a manual run.

### 4.7 Cloud workflow + secrets

`.github/workflows/weekly.yml`:
- Add a `group` `workflow_dispatch` input as **`type: choice`** with a literal `options:` list (e.g.
  `[marvels, pills]`), default `marvels`. `groups.py check` (in CI) parses this YAML and asserts the
  options set-equals the groups.json keys (§4.1).
- **Secrets stay shared** (`WCL_CLIENT_ID/SECRET`, `NETLIFY_AUTH_TOKEN`, `DROPBOX_APP_KEY/APP_SECRET/
  REFRESH_TOKEN`). GitHub Actions **cannot dynamically name secrets**, so per-group differentiation
  lives in **groups.json** (Netlify site id, Dropbox subfolder), read in a small `groups.py` step that
  exports `SITE_ID` and `DROPBOX_SUBFOLDER` into `$GITHUB_ENV`.
- Thread `--group ${{ inputs.group }}` into `dropbox_drops.py fetch`, the pipeline run, `publish.py`,
  and the state steps.
- `WOW_LOG_DIR` becomes `${{ github.workspace }}/drops/${{ inputs.group }}`.
- **State restore/persist** target the `<group>/` subtree (§4.5) — rewrite the inline shell (restore
  from `.state/<group>/cache/`, persist only `.state/<group>/cache/`, subtree-scoped `git add`, no
  whole-tree `rm -rf`), or route through `sync_state.py pull/push --group`.
- **Per-group concurrency lock:** `concurrency.group: weekly-state-${{ inputs.group }}` so Marvels and
  Pills runs proceed in parallel without racing each other's state push (today's global
  `weekly-state` serializes them; a slow run blocks the other). Note: this removes the *cross-group*
  serialization, so the per-group subtree `git add` scoping (§4.5) and the per-group Dropbox
  `archive` subfolder (§4.6) are what keep two parallel runs from clobbering shared surfaces.

### 4.8 The dashboard — separate site, minimal/no template change

Because each group deploys to its **own** Netlify site with **only its own** `WEEK_DATA` /
`WEEKS_INDEX` embedded, the template is intrinsically group-correct — no in-page group switcher, no
client-side filtering. `template.html` needs **no functional change**.

Optional polish (one line): `renderHeader()` reads `meta.display_name` and prepends it to the header
sub-line, so a glance distinguishes the Pills site from the Marvels site. To keep §2's "mapper
untouched" promise literally true, inject `display_name` into `meta` in **`finalize_week`/`commit_week`**
(post-map, where `group` is already in scope) — **not** inside the tenant-agnostic `map_to_week_data`.
Strictly cosmetic; ship it only if the URLs alone aren't enough.

## 5. Migration & phased rollout

### Phase 0 — pragmatic interim (this week, ZERO code change)

Get a **Pills URL live now** without waiting on the refactor: a throwaway clone of the repo with its
own `.env` (new `NETLIFY_SITE_ID`, new Dropbox folder, its own `data` branch). It runs the existing
single-tenant pipeline verbatim against Pills' report codes and deploys to the Pills site. This proves
the "separate site, separate state" model end-to-end with no risk to Marvels. **Retire the clone**
once Phase 2 lands; don't let it drift into a fork.

### Phase 1 — paths + DB scoping (the spine)

- Add `groups.py` + `groups.json` (marvels only at first).
- `paths.get_paths(group)` + `db_writer.db_path(group, test)`, both `group="marvels"`-default = today.
- Rewrite **every constant-consumer** (§4.2 list), including the snapshot WRITERS
  (`dump_week_data_cache`/`dump_wcl_raw_cache` via `commit_week`), the per-group `DASH_FILE`/`--out`,
  `reingest_loot`, and the REPL helpers. Thread `--group` through `wcl_auto_dashboard.main` →
  `week_build` → `commit_week` → `enrich_with_trends` (per-group `db_path`) and `build_site`/`publish`.
- Add `--group` to `backfill_snapshots.py` and `reprocess.py` (DB **and** both cache globs) — they're
  on the schema-bump path, so deferring them is a foot-gun.
- **Re-bless `tests/test_facade_surface.py`** for the new `get_paths`/`db_path`/`groups` re-exports.
- **Two separate proofs (don't conflate them):**
  - *(a) back-compat (marvels-only):* with no `--group`, run `replay_render.py` and `fc /b` the output
    against `cache/replay_baseline.html` — byte-identical. (`replay_render.py:31,44` reads the global
    `WCL_CACHE`/`DB_PATH`, so it can only prove the default group — which is exactly what back-compat
    needs.) Run `check.py` (gate green).
  - *(b) isolation (new test):* the two-group contamination guard below — a *different* test from the
    byte-diff, and the load-bearing one.

### Phase 2 — add Pills as a real `--group`

- Populate `groups.json` Pills entry. Create `cache/pills/`, `loot/pills/`, `drops/pills/`.
- First Pills run (`--group pills --test-db`) builds its own DB + snapshots from scratch (no prior
  week → no deltas, expected). Backfill earlier Pills weeks with `backfill_snapshots.py --group pills`
  (now `--group`-aware) if WCL has them (READ-only on the prod DB, as today).
- Deploy Pills to its Netlify site; verify the dropdown, deep-links, and that Marvels' site is
  untouched.

### Phase 3 — cloud + state branch (the atomic migration)

- `weekly.yml` `group` input (`type: choice`) + per-group secret-via-config resolution + per-group
  concurrency.
- **Atomic:** deploy subtree-aware `sync_state.py` + the rewritten `weekly.yml` state steps *together
  with* the one-time `git mv` of the `data` root tree into a `marvels/` subtree, then force a marker
  reset (`pull --group marvels`) on every clone before its next run (§4.5). Do NOT split the `git mv`
  from the code.
- Retire the Phase 0 clone.

### Phase 4 — polish

- Optional `display_name` in the header (injected post-map, §4.8).
- `groups.py check` in CI (options ↔ config parity).
- Docs: `run_weekly.bat` group prompt; README migration note; update `CLAUDE.md` File Map + the
  *Multi-week viewing* / *Publish workflow* sections + `docs/architecture.md` *Operator runs* to
  mention the group axis.

## 6. Risks & open questions

- **CRITICAL — trend cross-contamination** if state is ever shared. Mitigated structurally by
  per-group DB/snapshot dirs + per-group DASH_FILE (§4.3). *Guard (new isolation test):* run two
  groups through `commit_week` into separate DBs, assert each `delta_*` references only its own
  group's prior week **and** that a `--group pills` run writes zero bytes under Marvels'
  `cache/week_data/`. Never introduce a shared DB "interim" with `--group` flags — that's the exact
  failure mode.
- **The four side doors** (snapshot dumps, render target, REPL helpers, local `logs/` fallback) are
  the surfaces that make "physically incapable of seeing another group" *not yet true* until scoped
  (§4.3). The isolation test must exercise the dump path specifically — it's the write that feeds the
  glob.
- **Hidden path literals.** Any un-routed `ROOT / "cache" / …` silently reads Marvels under
  `--group pills`. Mitigation: grep audit (§4.2) + the back-compat byte-diff in Phase 1.
- **`data`-branch atomic migration.** The `git mv` + marker reset must ship with the subtree-aware
  `sync_state.py`/`weekly.yml` in one change (§4.5), or every clone starts from empty state. This is
  the single highest-risk step.
- **Cloud state shell vs `sync_state.py`.** The two code paths (inline `weekly.yml` shell + local
  `sync_state.py`) must both learn the subtree layout. Open question: route the workflow through
  `sync_state.py pull/push --group` to collapse to one implementation, vs. keep the inline shell
  (faster, but a second copy of the layout logic to keep in sync). Routing through `sync_state.py` is
  preferred.
- **Item-cache strategy — RESOLVED to share** (§4.3). Two id-keyed reference caches, shared/unscoped,
  seeded on cold start. Re-fetch cost (not race risk) was the real driver; last-writer-wins on
  identical values needs no lock. Revisit only if a future cache stores tenant-specific values.
- **Test-DB isolation.** `--test-db` resolves per-group (§4.3). Confirm no test fixture hard-codes
  `raid_history_test.db`; thread `group="marvels"` through fixtures. Invariant: `crit_history` always
  gets the resolved per-group `db_path` (true in `main`; enforce for backfill/reprocess too).
- **Schema drift across groups.** Each group's DB carries its own `schema_version`; `reprocess.py
  --all` operates on **one group's** DB, so the schema-bump dance must run **per group** (document it).
  The per-report downgrade guard is correct *within* a group and never spans groups (separate DBs).
- **groups.json ↔ workflow options parity.** A `choice` option with no groups.json key (or vice-versa)
  would fail in the cloud. Mitigation: `groups.py check` parses `weekly.yml` and diffs the sets in CI.
- **Report-code collision.** Two groups can't share a WCL report code in practice (distinct kills).
  Per-group DBs make even a hypothetical collision harmless (different files). No composite key needed.
- **Open: cross-group landing page.** Out of scope now; if wanted later, a static index linking the
  per-group sites is the cheapest option (no shared data).

## 7. Effort estimate

Per-subsystem (S ≤ ~½ day, M ≈ 1–2 days, L ≈ 3+ days). The heavy lifting is **constant-consumer
rewrites + state isolation**; the KPI/fetch/render core is untouched. Re-rated up from the v1 draft
after the review proved `get_paths()` is not a drop-in (every consumer reads imported constants) and
the two highest-risk surfaces are the inline-shell `data`-branch steps + the atomic `git mv`.

| Subsystem | Effort | Notes |
|---|---|---|
| `groups.py` + `groups.json` loader + `check` (YAML options diff) | **S** | new leaf, pure stdlib |
| `paths.py` `get_paths(group)` + **rewrite every constant-consumer** (incl. snapshot dumps, per-group DASH_FILE, reingest_loot, REPL helpers, crit/item cache readers) + back-compat byte-diff | **L** | the wide one — every call site, not a drop-in |
| `db_writer.py` `db_path(group, test)` + REPL helper `group=` | **S** | resolve at call time; constants stay marvels defaults |
| Thread `--group` through orchestrators (`wcl_auto_dashboard`, `week_build`, `reprocess`, `backfill_snapshots`) — DB **and** both cache globs | **M** | mechanical but wide; backfill/reprocess are Phase 1/2, not polish |
| `build_site.py` + `publish.py` per-group deploy + per-group DASH_FILE/last_deploy baseline | **M** | `.deploy/<group>/`, per-group site id |
| `trends.py` | **S** | already takes `db_path`; isolation = pass the per-group DB |
| Inputs (`dropbox_drops.py` fetch **and** archive-subfolder, `WOW_LOG_DIR` caller, local `logs/` refusal, loot scoping) | **M** | subfolder routing both ways + the local-log guard |
| `sync_state.py` per-group subtree + **rewrite the cloud `weekly.yml` state shell** + atomic `git mv` migration | **L** | two code paths; the migration must be atomic with the deploy |
| `weekly.yml` (`type: choice` group input, config-driven secrets, subtree state, per-group concurrency, per-group `WOW_LOG_DIR`) | **M** | YAML + `groups.py` resolution step |
| Tests (two-group isolation guard incl. dump-path zero-bytes, back-compat diff, fixture group threading, re-bless facade surface) | **M** | the contamination guard is the important one |
| Template `display_name` (optional, injected post-map) | **S** | one line in `renderHeader()` + `finalize_week` |
| Docs (`run_weekly.bat`, README, CLAUDE.md, architecture.md) | **S** | |

**Rolled up: L (~1–1.5 weeks of focused work** for Phases 1–3), front-loaded on the `paths.py`
constant-consumer rewrite and the `sync_state.py`/`weekly.yml` subtree migration. **Phase 0 (throwaway
clone) is ~S and unblocks Pills' URL this week** independent of all the above.
