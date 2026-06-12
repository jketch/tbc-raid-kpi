# Raid KPI Dashboard — Setup Guide

Self-contained weekly analytics pipeline for TBC Anniversary 25-man raids.
Pulls data from WarcraftLogs v2 API + your local `WoWCombatLog.txt`, generates a
browser-viewable HTML dashboard, and deploys it to Netlify.

---

## Prerequisites

| Tool | Version | Notes |
|------|---------|-------|
| Python | 3.9+ | stdlib only — no `pip install` needed for the core pipeline |
| Git | any | For cloning and pulling updates |
| Playwright + pypdf | latest | `pip install playwright pypdf` + `playwright install chromium` — only for screenshot/PDF export |

(Netlify hosting needs no extra tooling — deploys go straight to the Netlify REST API.)

---

## One-time setup

### 1. Clone the repo

```
git clone <repo-url> tbc-raid-kpi
cd tbc-raid-kpi
```

### 2. Create your WCL API credentials

1. Go to `warcraftlogs.com/api/clients/` and log in.
2. Click **Create Client** — name it anything (e.g. "Raid Dashboard").
3. Copy the **Client ID** and **Client Secret**.

> Same-guild second raid: you may reuse an existing app's credentials since
> `client_credentials` can read any public report.
> Different guild: register a separate client.

### 3. Create `.env`

Copy the example and fill in your credentials:

```
copy .env.example .env
```

Edit `.env`:

```
WCL_CLIENT_ID=your_client_id_here
WCL_CLIENT_SECRET=your_client_secret_here
```

Optional — override the dashboard title displayed in the browser:

```
DASHBOARD_TITLE=My Guild — Raid KPI Dashboard
```

Optional — enable the officer-only **AI performance summaries** (one-line notes per raider on the
Performance tab). Pay-as-you-go key from `console.anthropic.com`; pennies per week. Leave unset to
keep it off — the dashboard is unaffected either way.

```
ANTHROPIC_API_KEY=sk-ant-...
```

### 4. Choose a sharing method (optional)

The dashboard is a self-contained HTML file — `dashboard/raid_kpi_dashboard.html` — that works
locally without any hosting. Two options if you want to share it with your raid:

**Option A — Netlify (live URL, always up-to-date; no Node/CLI needed)**

1. Create a site once at `app.netlify.com` (**Add new project → Deploy manually** — drag any
   placeholder file; the weekly run replaces it). Pick a site name your raid will bookmark.
2. Copy the **Site ID** from *Site configuration → Site details*.
3. Create a personal access token: *User settings → Applications → New access token*.
4. Add both to `.env`:

```
NETLIFY_AUTH_TOKEN=your_token_here
NETLIFY_SITE_ID=your_site_id_here
```

(A site previously linked by the old netlify-cli flow keeps working — its id is read from
`.netlify/state.json`, so only the token line is new.)
After this, every `run_weekly.bat` run auto-deploys and your raid gets a stable URL to bookmark.

**Option B — Screenshot / PDF export (no hosting needed)**

Install Playwright once:

```
pip install playwright pypdf
playwright install chromium
```

After each weekly run, export the dashboard to per-tab PNGs or a single merged PDF:

```
python scripts/tools/screenshot_dashboard.py                  # one PNG per tab → screenshots\
python scripts/tools/screenshot_dashboard.py --pdf            # merged PDF → screenshots\dashboard.pdf
python scripts/tools/screenshot_dashboard.py --tabs overview utility  # specific tabs only
```

Post the images or PDF directly to your raid Discord channel.

### 5. Create the `logs` directory placeholder (if missing)

```
mkdir logs
```

---

## Weekly usage

1. Get the combat log in place — two options:
   - **Hands-off (recommended):** set `WOW_LOG_DIR` in `.env` to your WoW client's `Logs`
     folder once. Each run auto-picks the file whose timestamps cover the report's raid
     window (so an alt session's log is never grabbed by mistake) and, after a successful
     run, zips it into `logs\archive\` — kept for later log re-enrichment, out of next
     week's way.
   - **Manual:** copy `WoWCombatLog.txt` into the `logs\` folder (newest `.txt` auto-selected).
2. Double-click `run_weekly.bat` (or run it in cmd/PowerShell).
3. Enter the WCL report code when prompted (from `fresh.warcraftlogs.com/reports/XXXXXX`).
4. The dashboard opens automatically when done.

> **Officer view:** append `?officer=1` to the dashboard URL for the private Performance /
> Raider-Score tab. (Obscurity, not security — the underlying numbers also appear on the public tabs.)

The pipeline:
- Authenticates against WarcraftLogs OAuth
- Pulls fight data via the WCL v2 GraphQL API
- Parses your `WoWCombatLog.txt` for supplemental data
- Writes `dashboard/raid_kpi_dashboard.html` (gitignored — real data stays local)
- Deploys to Netlify (if configured), or export to screenshots/PDF with `screenshot_dashboard.py`

---

## Cloud run (GitHub Actions) — the zero-install fallback

When the usual operator can't run locally, anyone with repo access can run the week from the
GitHub **Actions** tab: *Weekly pipeline → Run workflow → paste the report code*. The run pulls
the week from WCL, deploys to Netlify, and prints the paste-ready raid-channel summary in the
job log. No machine setup at all.

- **One-time:** add four repo secrets (*Settings → Secrets and variables → Actions*):
  `WCL_CLIENT_ID`, `WCL_CLIENT_SECRET`, `NETLIFY_AUTH_TOKEN`, `NETLIFY_SITE_ID`.
- **Cloud runs are log-less by design** — every KPI keeps its WCL headline; the combat-log
  extras stay thin until re-enriched locally (`scripts/tools/backfill_snapshots.py <code>
  --log <archived log>`). The Loot tab (manual CSV) hides.
- **State lives on the `data` branch** (history DB + per-week snapshots — the one place this
  repo carries real raid data). Local and cloud runs share it via:

```
python scripts/tools/sync_state.py pull   # after a cloud-run week, before your next local run
python scripts/tools/sync_state.py push   # after local runs, so the cloud is never behind
```

- A `test_mode` checkbox on the dispatch form does a `--test-db` proof: no deploy, no state push.

---

## Pulling updates

```
git pull
```

Per-instance state (`.env`, `.netlify/`, `cache/*.db`, `logs/*.txt`,
`dashboard/raid_kpi_dashboard.html`) is gitignored — pulls never overwrite it.

---

## CLI flags

```
python scripts\wcl_auto_dashboard.py REPORTCODE            # normal run
python scripts\wcl_auto_dashboard.py REPORTCODE --test-db  # writes test DB only (safe proof)
python scripts\wcl_auto_dashboard.py REPORTCODE --dry-run  # prints week_data JSON, no HTML/DB
```

---

## File layout — what you touch weekly vs. never

```
tbc-raid-kpi/
│  ── you touch these ─────────────────────────────────────────────────────────
├── run_weekly.bat              ← THE entry point: double-click, paste the report code
├── logs/                       ← WEEKLY: drop WoWCombatLog.txt here (newest auto-selected)
├── loot/                       ← WEEKLY, optional: drop the ThatsBIS received-loot CSV here
├── .env                        ← one-time: your WCL credentials (gitignored; copy .env.example)
│  ── you read these ──────────────────────────────────────────────────────────
├── README.md                   ← this file (setup guide)
├── docs/                       ← architecture + TBC/WCL reference docs
│  ── the machinery (never touched in weekly use) ─────────────────────────────
├── scripts/                    ← the pipeline modules (facade: wcl_auto_dashboard.py)
│   ├── hooks/                  ←   tracked git-hook source (the pre-commit test gate)
│   └── tools/                  ←   on-demand tools: backfill_snapshots (rebuild past weeks),
│                                    probe_mechanic_ids (WCL recon), screenshot_dashboard
│                                    (PNG/PDF export), install_hooks (once per clone)
├── dashboard/
│   ├── template.html           ←   markup/CSS/render engine (tracked — the only UI source)
│   └── raid_kpi_dashboard.html ←   generated output (gitignored; rewritten every run)
├── tests/                      ← unit suite + golden (run via python scripts/check.py)
└── cache/                      ← machine-managed state (gitignored): SQLite history DB,
                                   per-week snapshots, item caches. Never edit by hand.
```
(Also gitignored at root: `.deploy/` + `.netlify/` (Netlify staging/state), `screenshots/`,
`prompts/` — all machine-managed or scratch.)
