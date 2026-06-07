# WoW Ranged DPS Log Analyzer — Skill

## What this skill does

Given a raw WoW `CombatLog.txt` file (or path), this skill:

1. **Parses** every ENCOUNTER_START/END block and extracts all damage, spell cast, and death events for the player.
2. **Computes per-pull KPIs**: DPS, total damage, damage-share %, cooldown count and timing, damage taken by spell, deaths.
3. **Trends across pulls**: DPS improvement pull-over-pull, cooldown usage consistency.
4. **Compares to Warcraftlogs.com** (optional): if WCL_CLIENT_ID / WCL_CLIENT_SECRET env vars are set, or --wcl-char / --wcl-realm are passed, it pulls percentile rankings.
5. **Outputs an Excel dashboard** (`.xlsx`) with color-coded sheets.

---

## When to invoke

Invoke whenever the user:
- Pastes a path to `CombatLog.txt` and asks for analysis, DPS, performance, cooldown review, etc.
- Says things like "analyze my log", "how did I do on [boss]", "show my cooldown usage", "compare my DPS to WCL"
- Provides a `sample_log.txt` path for testing

---

## How to run

### Step 1 — Confirm the log path
Ask the user: "What's the path to your CombatLog.txt?" (usually at `C:\Program Files (x86)\World of Warcraft\_retail_\Logs\CombatLog.txt`)

### Step 2 — Install dependency (once)
```bash
pip install openpyxl --break-system-packages
```

### Step 3 — Run the analyzer
```bash
# Basic (auto-detects first player in log)
python "C:\Users\Jon\Claude\Projects\Gaming\wow-analyzer\analyze.py" "<log_path>"

# Specify your character name explicitly
python "C:\Users\Jon\Claude\Projects\Gaming\wow-analyzer\analyze.py" "<log_path>" --player "YourName"

# With WCL comparison (requires free API key from https://www.warcraftlogs.com/api/clients/)
python "C:\Users\Jon\Claude\Projects\Gaming\wow-analyzer\analyze.py" "<log_path>" \
  --player "YourName" \
  --wcl-char "YourName" --wcl-realm "Area 52" --wcl-region US

# Custom output path
python "C:\Users\Jon\Claude\Projects\Gaming\wow-analyzer\analyze.py" "<log_path>" \
  --out "C:\Users\Jon\Claude\Projects\Gaming\my_raid_dashboard.xlsx"
```

### Step 4 — Present the output file
Use `mcp__cowork__present_files` to share the generated `.xlsx` with the user.

---

## Output sheets

| Sheet | Contents |
|---|---|
| 📊 Summary | One row per pull: DPS, total damage, damage share, damage taken, deaths, CDs used, WCL %ile |
| 📈 DPS Trend | Line chart of DPS across all pulls with rolling average |
| ⏱ Cooldowns | Every major CD, how many times used, first cast timing, all cast timestamps |
| 🛡 Damage Taken | Top spells that hit the player — identify avoidable damage |
| 🐉 [Boss Name] | Per-boss spell breakdown: hits, total, DPS contribution, % of player damage (+ bar chart) |

---

## Interpreting results / coaching tips

After generating the dashboard, analyze the output and provide targeted coaching:

### DPS / Throughput
- Compare DPS across pulls — is it trending up?
- If kill DPS is significantly lower than wipe DPS, uptime or movement is likely the issue
- Damage share % should be consistent; big drops indicate downtime or death

### Cooldown usage
- Each major CD should appear 2–3× per 3-minute fight (varies by CD duration)
- If `First Use` > 0:30, the opener needs work — most CDs should be used at 0:00–0:05
- Inconsistent CD counts across pulls = usage isn't on a predictable schedule

### Damage taken
- Repeated hits from the same mechanic spell = not dodging it
- Highest-damage-taken spells that aren't tank-busters are avoidable — look up the spell name on wowhead.com
- Deaths show as `death_time_offset` in the raw data (also visible in Summary sheet)

### WCL comparison
- < 25th percentile: Focus on rotation fundamentals and CD usage
- 25–75th: Look for cooldown timing improvements and uptime
- 75–95th: Fine-tune opener, pre-pot, and fight-specific optimizations
- 95th+: Spec-specific min/maxing and log-scrubbing for missed procs

---

## Adding more cooldowns

Edit the `MAJOR_COOLDOWNS` dict in `analyze.py` — add `spell_id: "Display Name"`.
Find spell IDs on https://www.wowhead.com (the ID is in the URL, e.g., `/spell=190319/combustion`).

## WCL API setup

1. Go to https://www.warcraftlogs.com/api/clients/
2. Create a client (free) — get Client ID and Client Secret
3. Set environment variables:
   ```
   set WCL_CLIENT_ID=your_id_here
   set WCL_CLIENT_SECRET=your_secret_here
   ```
4. Re-run the analyzer with `--wcl-char` and `--wcl-realm`

---

## Testing with the sample log

```bash
python "C:\Users\Jon\Claude\Projects\Gaming\wow-analyzer\analyze.py" \
  "C:\Users\Jon\Claude\Projects\Gaming\wow-analyzer\sample_log.txt" \
  --player "Jonketcher" \
  --out "C:\Users\Jon\Claude\Projects\Gaming\test_dashboard.xlsx"
```

Expected output: 2 pulls of "Stix Bunkjunker" (1 wipe, 1 kill), Fire Mage spells, Combustion + Meteor CDs.
