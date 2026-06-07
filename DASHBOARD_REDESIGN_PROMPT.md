# Dashboard Redesign — Claude Code Prompt

Paste everything below the line into Claude Code with the Gaming folder open.

---

Redesign `dashboard/raid_kpi_dashboard.html` across all tabs. This is a fun weekly guild recap tool — tone is celebratory and informative. Do not name individuals negatively (no "worst performer," no "hall of shame" framing). Cohort-level group insights are fine. Individual positive callouts are fine.

**Hard constraints — do not touch:**
- The `inject_into_html()` brace-depth injection logic
- The `const WEEK_DATA = {...}` block and anything below it that isn't a render function
- `CLASS_COLORS`, `nameColor()`, `.cname` span styling — class colors are load-bearing
- The `db_writer` import and `write_week()` call at the end of `main()`
- Any other tab's Python pipeline — HTML only

**CSS variables to use throughout:**
`--bg`, `--surface`, `--surface2`, `--text`, `--muted`, `--border`, `--accent` (#c89b3c gold), `--green`, `--red`, `--orange`, `--blue`

---

## GLOBAL CHANGES

1. **Tab icons:** Remove all emoji from tab button labels (📊 💚 ⚔️ 🛠 🐀). Replace with clean text only: "Overview", "Accountability", "Healers & Tanks", "DPS", "Utility". Keep `data-tab` attributes unchanged.

2. **Weekly Update bar:** The full-width instruction bar at the bottom of every tab is aimed at developers, not raiders. Collapse it to a small muted footnote in the page footer only: `Auto-generated from WarcraftLogs + WoWCombatLog.txt`. Remove it from inside each tab section.

3. **Section header treatment:** All section labels currently look identical regardless of importance. Introduce two levels:
   - **Primary section** (`<div class="section-label">`) — keep gold star + gold text, add `font-size: 15px`
   - **Sub-section** — muted text, `font-size: 12px`, no star

4. **Card baseline:** All `<div class="card">` elements should have a subtle left-border accent: `border-left: 3px solid var(--accent)`. Existing border/background rules remain.

---

## OVERVIEW TAB

**Remove entirely:** The "Highlights & Lowlights" card and "Tonight's Highlights" card.

**Stat bar — reorder and add one stat:**
Order: Bosses Killed → Total Kill Time → Total Deaths → Avoidable DMG → Spells Interrupted → Drums Cast

Add "Total Kill Time": sum all values in `WEEK_DATA.boss_times`, format as `M:SS`. Render it as the second stat tile.

**Boss Summary Tiles — replace the plain "Bosses Down" list:**

Two side-by-side zone sections with headers in gold accent:
- Left: **Serpentshrine Cavern**
- Right: **The Eye**

Canonical boss order (use this order regardless of kill order):

SSC: Hydross the Unstable, The Lurker Below, Morogrim Tidewalker, Leotheras the Blind, Fathom-Lord Karathress, Lady Vashj

TK: Void Reaver, High Astromancer Solarian, Al'ar, Kael'thas Sunstrider

Each boss = a tile: boss name bold, kill time in gold formatted M:SS. If the boss name is NOT in `WEEK_DATA.boss_times`, render it grayed out with text "—" (not killed). Tiles in a 3-column CSS grid per zone. No kill time color-coding yet (benchmarks will come from DB once more weeks are collected).

**Raid Cohort Scorecards — add below boss tiles:**

A row of 4 cards, computed in JS at render time from `WEEK_DATA`. No individual names — cohort aggregates only.

Card 1 — **Avoidable DMG Split:** Sum `avoidableDmg` by role using `WEEK_DATA.roster[name].role`. Show melee vs ranged as a two-segment bar. Label: "Melee · Ranged avoidable split" with percentages.

Card 2 — **Deaths by Role:** Sum `deaths[].total` grouped by `roster[name].role` (tank/healer/melee/ranged). Show as a small labeled bar chart or pill row.

Card 3 — **Interrupt Breadth:** Count players in `interrupts` with `count > 0`. Show as "N raiders contributed interrupts" with N large and prominent.

Card 4 — **Drums Efficiency:** Average `score` across all `drums[]` entries. Show as a large percentage. Color: ≥90% `--green`, 70–89% `--orange`, <70% `--red`.

---

## ACCOUNTABILITY TAB

**Remove:** Any element that labels a single player as "worst," "bottom," or "spotlight" loser. Remove the top "worst performer this week" callout card if present.

**Avoidable Damage bars:** Increase bar height from current size to `height: 20px`. Increase the class icon size to 22px. Add more vertical padding between rows (12px gap minimum).

**Death breakdown table:** The current per-boss death cause table is too granular. Replace with a simplified version:
- Keep columns: Player · Role · Total Deaths · Primary Cause
- "Primary Cause" = the single death cause with the highest count for that player, shown as a badge
- Remove all per-boss breakdown columns
- Sort by Total Deaths descending
- Limit to players with at least 1 death

**Section spacing:** Add `margin-bottom: 24px` between each major section (avoidable, deaths, any heatmaps).

---

## DPS TAB

**Spec column:** In the per-player stat cards at the bottom, add `spec` as a muted sub-label under the player name. Pull from `WEEK_DATA.roster[name].spec`. No other columns change.

**Crit bar chart height:** Increase the chart canvas or bar container height by 40% so bars are taller and easier to compare.

**Heatmap text contrast:** In any green-background heatmap cells, ensure text color is dark (e.g. `#1a2a1a` or similar dark green) rather than white or light gray so it's readable. Red cells keep light text.

---

## HEALERS & TANKS TAB

**Tank section:** The tank section currently gets ~20% of the tab. Give it at minimum 35% of vertical space. Add a visual divider (gold accent line) between the healer section and tank section.

**Tank damage type:** If `WEEK_DATA.tankMit` has bear or pally data, add a small inline label next to each tank's name showing their primary damage type received (magic vs physical) as a colored badge — blue for magic-heavy fights (Vashj, Leotheras), amber for physical. This can be derived from which boss they were tanking if the data is available; otherwise skip.

**Healer section label:** Make the "Healers & Percentages" section header larger — `font-size: 15px`, gold accent color.

---

## UTILITY TAB

**Spell usage section:** The "Spells Used by Player" section at the bottom is unreadable at normal screen sizes. Replace it with a simplified version:
- Show only the top 3 spells by cast count per player
- Each player gets a small card with their name (class-colored) and 3 pill badges showing spell name + count
- If the data isn't available or is empty, hide the section entirely with no empty-state message

**Engineering table:** Current column widths are too wide for the sparse data. Set `table-layout: fixed`, constrain to 4 columns: Player · Total DMG · Sappers · Bombs. Remove any columns with all-zero data.

**Drums section:** Keep exactly as-is — it's the best-designed element in the dashboard.

**Interrupt bars:** Currently only shows players with count > 0. Add a muted footnote below: "Players with 0 interrupts not shown." Keep the bars themselves unchanged.

---

## IMPLEMENTATION ORDER

Implement in this order to minimize risk of breaking the injection logic:

1. Global changes (tab labels, footer, card border, section headers)
2. Overview tab (stat bar → boss tiles → cohort cards)
3. Accountability tab (remove callout, fix bars, simplify death table)
4. Utility tab (spell section, engineering table)
5. DPS tab (spec label, chart height, heatmap contrast)
6. Healers & Tanks tab (tank section sizing, divider)

After each tab, verify `const WEEK_DATA` is still intact and the `inject_into_html` marker comment is untouched.
