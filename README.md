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
| Node.js + npm | any | Only needed for Netlify hosting (optional) |
| Netlify CLI | latest | `npm install -g netlify-cli` — only for Netlify hosting |
| Playwright + pypdf | latest | `pip install playwright pypdf` + `playwright install chromium` — only for screenshot/PDF export |

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

### 4. Choose a sharing method (optional)

The dashboard is a self-contained HTML file — `dashboard/raid_kpi_dashboard.html` — that works
locally without any hosting. Two options if you want to share it with your raid:

**Option A — Netlify (live URL, always up-to-date)**

```
netlify login
netlify sites:create
```

Follow the prompts to name your site. This writes `.netlify/state.json` (gitignored).
After this, every `run_weekly.bat` run auto-deploys and your raid gets a stable URL to bookmark.

**Option B — Screenshot / PDF export (no hosting needed)**

Install Playwright once:

```
pip install playwright pypdf
playwright install chromium
```

After each weekly run, export the dashboard to per-tab PNGs or a single merged PDF:

```
python scripts\screenshot_dashboard.py                  # one PNG per tab → screenshots\
python scripts\screenshot_dashboard.py --pdf            # merged PDF → screenshots\dashboard.pdf
python scripts\screenshot_dashboard.py --tabs overview utility  # specific tabs only
```

Post the images or PDF directly to your raid Discord channel.

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
- Deploys to Netlify (if configured), or export to screenshots/PDF with `screenshot_dashboard.py`

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
│   ├── publish.py              ← Netlify deploy
│   └── screenshot_dashboard.py ← export dashboard to PNG/PDF (requires Playwright)
├── dashboard/
│   ├── template.html           ← markup/CSS/render engine (tracked in git)
│   └── raid_kpi_dashboard.html ← generated output (gitignored)
├── logs/                       ← drop WoWCombatLog.txt here
└── cache/
    └── raid_history.db         ← weekly KPI history (gitignored)
```
