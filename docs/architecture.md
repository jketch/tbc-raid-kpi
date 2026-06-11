# Architecture

The weekly raid-analytics pipeline for TBC Anniversary 25-man content (SSC + TK). One
maintainer runs `run_weekly.bat`, enters a WCL report code, and the pipeline pulls fight
data, parses the combat log, computes KPIs, injects them into the dashboard HTML, and
deploys to Netlify.

The design collapses three formerly-drifted producers (weekly run, backfill, offline
reprocess) onto **one producer spine** (`week_build.py`), validated against **one contract**
(`week_schema.py`), guarded by **one offline gate** (`check.py`). Everything else is either a
**leaf** (imports nothing from the pipeline) or a thin orchestrator/sink.

---

## 1. Module map

How the modules layer by import direction and role. Leaves at the bottom depend on nothing in
the pipeline; arrows point from importer to imported (calls flow the other way).

```mermaid
flowchart TD
    %% ── Orchestrators (entry points) ────────────────────
    subgraph ORCH["Entry points — thin orchestrators"]
        MAIN["main()<br/>weekly run"]
        REPRO["reprocess.py<br/>offline, zero WCL"]
        BACK["backfill_snapshots.py<br/>DB read-only"]
    end

    %% ── Spine ───────────────────────────────────────────
    SPINE["week_build.py — the spine<br/>from_wcl / from_snapshot<br/>→ finalize_week → commit_week"]

    %% ── Core ────────────────────────────────────────────
    CORE["wcl_auto_dashboard.py — core<br/>WCL fetch + crit model + merge_log_into_wcl<br/>map_to_week_data · enrich_with_trends · inject_into_html<br/>(re-exports the leaves)"]

    %% ── Sinks / deploy ──────────────────────────────────
    subgraph SINK["Sinks & deploy"]
        DBW["db_writer.py<br/>write_week (downgrade-guard)"]
        PUB["publish.py / build_site.py<br/>stage .deploy/ → Netlify"]
    end

    %% ── Gate ────────────────────────────────────────────
    GATE["check.py — the gate<br/>unit suite + characterization<br/>(pre-commit hook)"]

    %% ── Leaves ──────────────────────────────────────────
    subgraph LEAF["Leaves — import nothing from the pipeline"]
        WCLC["wcl_client.py<br/>get_token · gql"]
        GC["game_constants.py<br/>curated name-sets<br/>(TIER-STABLE + T5 seam)"]
        CL["combat_log.py<br/>parse_combat_log"]
        SCHEMA["week_schema.py<br/>validate · regression<br/>populated_sections"]
    end

    MAIN --> SPINE
    REPRO --> SPINE
    BACK --> SPINE

    SPINE -.->|lazy import<br/>breaks the cycle| CORE
    CORE -.->|imports for main| SPINE
    SPINE --> SCHEMA
    SPINE --> DBW
    MAIN --> PUB

    CORE -->|re-exports| WCLC
    CORE -->|re-exports| GC
    CORE -->|re-exports| CL
    CL --> GC

    GATE --> SCHEMA

    classDef spine fill:#EEEDFE,stroke:#534AB7,color:#26215C;
    classDef leaf fill:#E1F5EE,stroke:#0F6E56,color:#04342C;
    classDef gate fill:#FBEAF0,stroke:#993556,color:#4B1528;
    classDef plain fill:#F1EFE8,stroke:#888780,color:#2C2C2A;

    class SPINE spine;
    class WCLC,GC,CL,SCHEMA leaf;
    class GATE gate;
    class MAIN,REPRO,BACK,CORE,DBW,PUB plain;
```

**Legend** — purple: spine · green: leaves · pink: gate · grey: orchestrators/core/sinks.
The core and the spine import each other; the spine's `import wcl_auto_dashboard` is **lazy
(inside functions)** to break the cycle, since the core imports the spine for `main()`.

---

## 2. Data flow — one week

```mermaid
flowchart TD
    %% ── Sources ─────────────────────────────────────────
    subgraph SRC["Sources"]
        OAUTH["fresh.warcraftlogs.com<br/>OAuth token"]
        WCL["www.warcraftlogs.com<br/>v2 GraphQL API"]
        LOG["logs/WoWCombatLog.txt<br/>raw combat log"]
        CSV["loot/*.csv<br/>ThatsBIS received-loot"]
        SNAP_IN[("cache/week_data/*.json<br/>snapshot (reprocess only)")]
    end

    %% ── Build ───────────────────────────────────────────
    subgraph BUILD["Build (week_build spine)"]
        FROMWCL["from_wcl()<br/>fetch + parse + merge + map"]
        FROMSNAP["from_snapshot()<br/>load cached WEEK_DATA"]
        MAPPED["mapped WEEK_DATA"]
    end

    %% ── Finalize ────────────────────────────────────────
    subgraph FIN["finalize_week() — the single funnel"]
        LOOT["reingest_loot()<br/>loot_parser.py"]
        VALIDATE["week_schema.validate()<br/>against the contract"]
    end

    %% ── Commit (ordered tail) ───────────────────────────
    subgraph COMMIT["commit_week() — ordered persistence tail"]
        DUMP["dump snapshot<br/>(clean, pre-enrich, test-gated)"]
        ENRICH["enrich_with_trends()<br/>reads prior week → delta_*"]
        WRITE["db_writer.write_week()"]
        INJECT["inject_into_html()<br/>template.html → output HTML"]
    end

    %% ── Sinks ───────────────────────────────────────────
    subgraph SINKS["Sinks"]
        DB[("cache/raid_history.db<br/>SQLite KPI history")]
        SNAP_OUT[("cache/week_data/*.json")]
        HTML["raid_kpi_dashboard.html"]
        DEPLOY["build_site → .deploy/<br/>→ publish → Netlify"]
    end

    OAUTH -.-> FROMWCL
    WCL --> FROMWCL
    LOG -.->|degrades silently if absent| FROMWCL
    SNAP_IN --> FROMSNAP

    FROMWCL --> MAPPED
    FROMSNAP --> MAPPED
    MAPPED --> LOOT
    CSV -.->|optional| LOOT
    LOOT --> VALIDATE

    VALIDATE --> DUMP
    DUMP --> ENRICH
    ENRICH --> WRITE
    WRITE --> INJECT

    DUMP --> SNAP_OUT
    ENRICH -.->|reads prior row| DB
    WRITE --> DB
    INJECT --> HTML
    HTML --> DEPLOY
    SNAP_OUT --> DEPLOY

    classDef src fill:#E6F1FB,stroke:#185FA5,color:#0b2a45;
    classDef data fill:#E1F5EE,stroke:#0F6E56,color:#04342C;
    classDef plain fill:#F1EFE8,stroke:#888780,color:#2C2C2A;

    class OAUTH,WCL,LOG,CSV src;
    class DB,SNAP_IN,SNAP_OUT data;
    class FROMWCL,FROMSNAP,MAPPED,LOOT,VALIDATE,DUMP,ENRICH,WRITE,INJECT,HTML,DEPLOY plain;
```

**Legend** — blue: external sources · green: persisted data stores · grey: pipeline steps.
Solid arrows are the always-run path; dashed arrows are additive/optional inputs that degrade
silently when absent (OAuth token bootstrap, combat log, loot CSV, the prior-row read).

---

## Load-bearing invariants

- **WCL is the source of record; the combat log is additive enrichment.** Every KPI has a
  WCL-sourced headline that runs every week. A missing log thins a section (drill-downs, HP
  timelines, attribution) but never blanks it — every log read is guarded, which is why
  `LOG -.-> from_wcl` is dashed.
- **One finalize funnel.** `from_wcl` (live) and `from_snapshot` (offline) both hand a mapped
  `WEEK_DATA` to `finalize_week`, which owns the single loot-ingestion point (`reingest_loot`)
  and validates against `week_schema`. Producers can't drift on loot or skip validation — the
  structural fix for the loot-drop / role-drift / order-skew bug class.
- **`commit_week` encodes the persistence order, and it is not reorderable:** dump the clean
  snapshot (pre-enrich) → `enrich_with_trends` (reads the *prior* week's DB row for `delta_*`) →
  `write_week` (whose `INSERT OR REPLACE` would otherwise clobber that prior row) → render.
- **Backfill is DB-read-only.** It calls `finalize_week` + the snapshot dump but *not*
  `commit_week`, so it never overwrites log-complete rows with thinner WCL-only data.
- **The snapshot dump is test-gated.** `--test-db` never pollutes the canonical
  `cache/week_data/` cache.
- **The gate is offline.** `check.py` runs the unit suite plus a characterization test — does
  any prod snapshot silently drop a populated `WEEK_DATA` section vs the recorded golden? — with
  zero WCL/DB access, and the pre-commit hook blocks commits on failure.

## Multi-week viewing

The pipeline embeds only the **latest** week in `const WEEK_DATA`. `build_site.py` stages
`.deploy/index.html` (latest, verbatim) plus `.deploy/weeks/<report>.json` for earlier weeks
(each enriched with its own `delta_*` vs the week before it) and a `WEEKS_INDEX` manifest. The
header date pill becomes a dropdown; `loadWeek(report)` fetches a past week's JSON and mutates
`WEEK_DATA` in place, then re-renders — so the dashboard shows each week *as it was*.
