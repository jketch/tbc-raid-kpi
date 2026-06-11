# Setup — standing up a new instance

A weekly raid-analytics dashboard for **TBC Anniversary** 25-man content (SSC + TK). You enter a
WarcraftLogs report code each week; the pipeline pulls the data, computes KPIs, builds a static HTML
dashboard, and deploys it to Netlify. Each clone is its own independent instance — no other guild's
data comes with it.

---

## Prerequisites
- **Python 3.x** — the pipeline is essentially stdlib; the few external packages (`requests`, and
  `anthropic` only if you enable AI summaries) auto-install on first run.
- **Node.js + npm** — only for the Netlify CLI (the deploy step).
- **git**, and a **WarcraftLogs account** (to register an API client).
- Windows is the tested host (`run_weekly.bat`); the underlying `python scripts/...` commands work
  anywhere.

---

## One-time setup

### 1. Clone
```
git clone https://github.com/<you>/tbc-raid-kpi.git
cd tbc-raid-kpi
```

### 2. WCL API credentials (required)
1. Go to **https://www.warcraftlogs.com/api/clients/** and create a client (any name; redirect URL can
   be `https://localhost`). Copy the **Client ID** and **Client Secret**.
2. Copy the example env file and fill them in:
   ```
   copy .env.example .env        ::  (cp .env.example .env on mac/linux)
   ```
   Set `WCL_CLIENT_ID` and `WCL_CLIENT_SECRET` in `.env`. **`.env` is gitignored — never commit it.**

### 3. Install the git hooks (once per clone)
```
python scripts/install_hooks.py
```
This wires the pre-commit gate (`scripts/check.py` — unit suite + contract check) so a bad change
can't be committed. `.git/hooks` isn't tracked, so this is needed on every fresh clone.

### 4. Link a Netlify site (once)
```
npm install -g netlify-cli
netlify login
netlify sites:create        ::  pick/confirm a team; this links the site (writes .netlify/)
```
After this, the weekly run deploys automatically. If the Netlify CLI isn't present, the pipeline still
builds the dashboard locally and just skips the deploy.

### 5. (Optional) extras in `.env`
- `DASHBOARD_TITLE` — brand the header/browser-tab for your guild.
- `ANTHROPIC_API_KEY` (+ `PERF_SUMMARY_MODEL`) — enables the officer-only AI performance summaries.
  Pay-as-you-go key from https://console.anthropic.com; pennies/week. Leave blank to keep it off.
- `DISCORD_WEBHOOK_URL` — for an auto-posted weekly raid-channel summary (skipped if blank).

---

## Each week
1. Drop your **`WoWCombatLog.txt`** into `logs/` (the newest `.txt` is auto-selected). *Optional:* a
   ThatsBIS received-loot CSV in `loot/` for the Loot tab.
2. Run **`run_weekly.bat`** (double-click, or `run_weekly.bat` in a terminal).
3. Enter the **WCL report code** when prompted (the bit after `fresh.warcraftlogs.com/reports/…`).
4. The pipeline fetches → builds → **deploys to Netlify** → opens the dashboard. Full output is saved
   to `run.log`.

> No combat log this week? It still works — WCL is the source of record; the combat log only adds
> drill-downs (avoidable damage, drums, etc.), which degrade silently when absent.

---

## Good to know
- **Never edit `dashboard/raid_kpi_dashboard.html`** — it's the gitignored generated output, rebuilt
  every run. All markup/CSS/JS lives in `dashboard/template.html` (the tracked source).
- **Per-instance state is gitignored** — `.env`, `.netlify/`, `cache/*.db`, `logs/*.txt`, `loot/*.csv`.
  A fresh clone starts with an **empty history DB**, so week-over-week trends fill in as you run.
- **`git pull`** safely updates the tool (template, pipeline, docs) without touching your data or output.
- **Officer view:** append `?officer=1` to the dashboard URL for the private Performance / Raider-Score
  tab. (Obscurity, not security — the underlying numbers are on the public tabs.)
- WCL rate limit is ~300 points/min; the client retries/backs off automatically.
- Reference docs for the metrics + data sources live in `docs/` and `CLAUDE.md`.
