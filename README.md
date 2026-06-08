# Raid KPI Dashboard — Setup Guide

Self-contained weekly analytics pipeline for TBC Anniversary 25-man raids.
Pulls data from WarcraftLogs v2 API + your local `WoWCombatLog.txt`, generates a
browser-viewable HTML dashboard, and deploys it to Netlify.

---

## Prerequisites

| Tool | Version | Notes |
|------|---------|-------|
| Python | 3.9+ | stdlib only — no `pip install` needed for the core pipeline |
| Node.js + npm | any | Only needed for the Netlify CLI |
| Netlify CLI | latest | `npm install -g netlify-cli` |
| Git | any | For cloning and pulling updates |

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

### 4. Set up Netlify (optional — for public sharing)

```
netlify login
netlify sites:create
```

Follow the prompts to name your site. This writes `.netlify/state.json` (gitignored).
After this, every `run_weekly.bat` run auto-deploys to your site URL.

Skip this step if you only want local use — the dashboard opens in your browser without Netlify.

### 5. Create the `logs` directory placeholder (if missing)

```
mkdir logs
```

---

## Weekly usage

1. Copy your `WoWCombatLog.txt` into the `logs\` folder (the newest `.txt` is auto-selected).
2. Double-click `run_weekly.bat` (or run it in cmd/PowerShell).
3. Enter the WCL report code when prompted (from `fresh.warcraftlogs.com/reports/XXXXXX`).
4. The dashboard opens automatically when done.

The pipeline:
- Authenticates against WarcraftLogs OAuth
- Pulls fight data via the WCL v2 GraphQL API
- Parses your `WoWCombatLog.txt` for supplemental data
- Writes `dashboard/raid_kpi_dashboard.html` (gitignored — real data stays local)
- Deploys to Netlify (if configured)

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

## File layout

```
tbc-raid-kpi/
├── run_weekly.bat              ← entry point
├── .env                        ← your WCL credentials (gitignored)
├── SETUP.md                    ← this file
├── scripts/
│   ├── wcl_auto_dashboard.py   ← main pipeline
│   ├── db_writer.py            ← SQLite history
│   └── publish.py              ← Netlify deploy
├── dashboard/
│   ├── template.html           ← markup/CSS/render engine (tracked in git)
│   └── raid_kpi_dashboard.html ← generated output (gitignored)
├── logs/                       ← drop WoWCombatLog.txt here
└── cache/
    └── raid_history.db         ← weekly KPI history (gitignored)
```
