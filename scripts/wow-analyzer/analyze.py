#!/usr/bin/env python3
"""
WoW Ranged DPS Log Analyzer
Usage: python analyze.py <CombatLog.txt> [--player "PlayerName"] [--out dashboard.xlsx]

Produces an Excel dashboard with:
  - Per-encounter KPIs (DPS, damage breakdown, cooldown usage, deaths)
  - Trend sheets across all pulls
  - Warcraftlogs.com percentile comparison (via wcl_compare.py)
"""

import sys
import csv
import io
import re
import argparse
import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)
from dataclasses import dataclass, field
from datetime import datetime
from collections import defaultdict
from typing import Optional

# ──────────────────────────────────────────────────────────────────────────────
# MAJOR COOLDOWNS  (spell_id -> display name)
# TBC Classic — Destruction Warlock, all spell ranks
# Add wowhead.com spell IDs here for any on-use trinkets you carry.
# ──────────────────────────────────────────────────────────────────────────────
MAJOR_COOLDOWNS: dict[int, str] = {
    # ── Conflagrate (10s CD, consumes Immolate) ──
    # ranks 1-5; rank 5 (27266) is the T5-phase cap
    17962: "Conflagrate", 18930: "Conflagrate", 18931: "Conflagrate",
    18932: "Conflagrate", 27266: "Conflagrate",
    # ── Shadowburn (15s CD, execute <20% HP) ──
    # ranks 1-8; rank 8 (29341) used at 70
    17877: "Shadowburn", 17878: "Shadowburn", 17879: "Shadowburn",
    17880: "Shadowburn", 27263: "Shadowburn", 29341: "Shadowburn",
    # ── Death Coil (2 min CD, Warlock version) ──
    6789:  "Death Coil", 17925: "Death Coil",
    17926: "Death Coil", 27223: "Death Coil",
    # ── Curse of Doom (1 min CD, big delayed tick) ──
    603:   "Curse of Doom", 30910: "Curse of Doom",
    # ── Racials ──
    26297: "Berserking",          # Troll
    20572: "Blood Fury",          # Orc (spell power version in TBC)
    33702: "Blood Fury",          # Orc alternate ID
    # ── Common TBC T5-phase on-use trinkets ──
    # Uncomment / add the spell IDs for trinkets you actually equip.
    # Find them on wowhead.com — spell ID is in the URL.
    # 34430: "Skull of Gul'dan",  # example
}

# ──────────────────────────────────────────────────────────────────────────────
# DATA CLASSES
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class CastEvent:
    time_offset: float   # seconds from encounter start
    spell_id: int
    spell_name: str


@dataclass
class EncounterData:
    encounter_id: int
    encounter_name: str
    start_time: float
    difficulty: str = ""
    end_time: float = 0.0
    success: bool = False
    pull_number: int = 1

    player_guid: str = ""
    player_name: str = ""

    # spell_id -> {name, hits, total}
    damage_by_spell: dict = field(default_factory=lambda: defaultdict(lambda: {"name": "", "hits": 0, "total": 0}))
    # spell_id -> {name, hits, total}
    damage_taken: dict = field(default_factory=lambda: defaultdict(lambda: {"name": "", "hits": 0, "total": 0}))
    # Major cooldown casts
    cd_casts: list = field(default_factory=list)
    # All casts (for detecting auto-detected CDs)
    all_casts: list = field(default_factory=list)

    player_died: bool = False
    death_time_offset: float = 0.0

    # Raid-wide total damage (for damage share %)
    raid_total_damage: int = 0

    @property
    def duration(self) -> float:
        return max(self.end_time - self.start_time, 1.0)

    @property
    def player_total_damage(self) -> int:
        return sum(v["total"] for v in self.damage_by_spell.values())

    @property
    def dps(self) -> float:
        return self.player_total_damage / self.duration

    @property
    def damage_share_pct(self) -> float:
        if not self.raid_total_damage:
            return 0.0
        return (self.player_total_damage / self.raid_total_damage) * 100

    @property
    def total_damage_taken(self) -> int:
        return sum(v["total"] for v in self.damage_taken.values())

    @property
    def duration_str(self) -> str:
        m, s = divmod(int(self.duration), 60)
        return f"{m}:{s:02d}"


# ──────────────────────────────────────────────────────────────────────────────
# PARSER
# ──────────────────────────────────────────────────────────────────────────────

DIFFICULTY_MAP = {
    # TBC Classic
    "1": "5-man Normal", "2": "5-man Heroic",
    "3": "10-man",       "4": "25-man",
    # Retail (kept for compatibility)
    "14": "Normal", "15": "Heroic", "16": "Mythic",
    "17": "LFR",    "9":  "Mythic", "8":  "Mythic+",
}


def _ts_to_seconds(ts: str) -> Optional[float]:
    """Parse WoW timestamp to float seconds-since-midnight.
    Handles formats:
      M/D/YYYY HH:MM:SS.mmm        (retail)
      M/D/YYYY HH:MM:SS.mmm-TZ     (Anniversary/Classic with timezone offset)
      M/D HH:MM:SS.mmm             (older retail logs)
    """
    ts = ts.strip()
    # Strip trailing timezone offset: ".891-6" or ".123+5" → ".891" / ".123"
    ts = re.sub(r'(\.\d+)[+-]\d+$', r'\1', ts)
    # Inject year if missing (older log format "M/D HH:MM...")
    if re.match(r'^\d{1,2}/\d{1,2} ', ts):
        ts = re.sub(r'^(\d{1,2}/\d{1,2}) ', r'\g<1>/2026 ', ts)
    try:
        dt = datetime.strptime(ts, "%m/%d/%Y %H:%M:%S.%f")
        return dt.hour * 3600 + dt.minute * 60 + dt.second + dt.microsecond / 1_000_000
    except ValueError:
        return None


def _csv_row(s: str) -> list[str]:
    return next(csv.reader(io.StringIO(s)), [])


def _is_player(guid: str) -> bool:
    return guid.startswith("Player-")


def _name_matches(src_name: str, target: str) -> bool:
    """Match player names allowing realm suffix.
    'Marvels' matches 'Marvels-Nightslayer-US'.
    """
    if not target:
        return False
    sl, tl = src_name.lower(), target.lower()
    return sl == tl or sl.startswith(tl + "-")


def parse_log(filepath: str, target_player: str = "") -> tuple[list[EncounterData], str]:
    """
    Parse a WoW CombatLog.txt.
    Returns (list of EncounterData, detected_player_name).
    If target_player is given, only track that player; otherwise auto-detects
    the first player GUID seen doing damage.
    """
    encounters: list[EncounterData] = []
    current: Optional[EncounterData] = None
    pull_counts: dict[str, int] = defaultdict(int)
    detected: str = target_player

    with open(filepath, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.rstrip("\n")
            if not raw:
                continue

            # Split "timestamp  EVENT,params..." on double-space
            head, _, tail = raw.partition("  ")
            if not tail:
                continue

            ts = _ts_to_seconds(head)
            if ts is None:
                continue

            comma = tail.find(",")
            event = tail[:comma].strip() if comma != -1 else tail.strip()
            param_str = tail[comma + 1:] if comma != -1 else ""
            p = _csv_row(param_str)

            # ── ENCOUNTER_START ──────────────────────────────────────────────
            if event == "ENCOUNTER_START":
                if len(p) < 3:
                    continue
                enc_id = int(p[0]) if p[0].isdigit() else 0
                enc_name = p[1].strip('"')
                diff = DIFFICULTY_MAP.get(p[2], p[2])
                pull_counts[enc_name] += 1
                current = EncounterData(
                    encounter_id=enc_id,
                    encounter_name=enc_name,
                    difficulty=diff,
                    start_time=ts,
                    pull_number=pull_counts[enc_name],
                    player_name=detected,
                )
                continue

            # ── ENCOUNTER_END ────────────────────────────────────────────────
            if event == "ENCOUNTER_END" and current is not None:
                current.end_time = ts
                if len(p) >= 5:
                    current.success = (p[4] == "1")
                encounters.append(current)
                current = None
                continue

            if current is None:
                continue

            # All subsequent events need at least 8 base params
            if len(p) < 8:
                continue

            src_guid = p[0].strip('"')
            src_name = p[1].strip('"')
            dst_guid = p[4].strip('"')
            extra = p[8:]  # spell_id, spell_name, school, [amounts...]

            # ── Auto-detect player ───────────────────────────────────────────
            if not current.player_guid and _is_player(src_guid) and not _is_player(dst_guid):
                if not detected or _name_matches(src_name, detected):
                    current.player_guid = src_guid
                    current.player_name = src_name
                    if not detected:
                        detected = src_name

            tguid = current.player_guid

            # ── SPELL_DAMAGE / SPELL_PERIODIC_DAMAGE ─────────────────────────
            # TBC Classic advanced log format (ADVANCED_LOG_ENABLED=1):
            #   extra[0]  = spellId
            #   extra[1]  = spellName
            #   extra[2]  = spellSchool
            #   extra[3..20] = 18 advanced params (unitGUID, ownerGUID, HP,
            #                  maxHP, AP, SP, armor, powerType, power, maxPower,
            #                  powerCost, 3×unknown, posX, posY, mapID, facing, level)
            #   extra[21] = amount (damage dealt)
            #   extra[22] = overkill
            #   extra[27] = critical (1=crit, 0=normal)
            if event in ("SPELL_DAMAGE", "SPELL_PERIODIC_DAMAGE"):
                if len(extra) < 22:
                    continue
                try:
                    sid   = int(extra[0])
                    sname = extra[1].strip('"')
                    amount = int(extra[21])
                    is_crit = extra[27] == "1" if len(extra) > 27 else False
                except (ValueError, IndexError):
                    continue

                if _is_player(src_guid) and not _is_player(dst_guid):
                    current.raid_total_damage += amount

                if src_guid == tguid:
                    e = current.damage_by_spell[sid]
                    e["name"] = sname
                    e["hits"] += 1
                    e["total"] += amount
                    if "crits" not in e:
                        e["crits"] = 0
                    if is_crit:
                        e["crits"] += 1

                # Damage TAKEN by player
                if dst_guid == tguid and not _is_player(src_guid):
                    e = current.damage_taken[sid]
                    e["name"] = sname
                    e["hits"] += 1
                    e["total"] += amount

            # ── SWING_DAMAGE ─────────────────────────────────────────────────
            # No spell prefix — advanced block starts at extra[0], amount at extra[18]
            elif event == "SWING_DAMAGE":
                if len(extra) < 19:
                    continue
                try:
                    amount = int(extra[18])
                except (ValueError, IndexError):
                    continue
                if _is_player(src_guid) and not _is_player(dst_guid):
                    current.raid_total_damage += amount
                if src_guid == tguid:
                    e = current.damage_by_spell[0]
                    e["name"] = "Auto Attack"
                    e["hits"] += 1
                    e["total"] += amount

            # ── SPELL_CAST_SUCCESS ───────────────────────────────────────────
            elif event == "SPELL_CAST_SUCCESS":
                if src_guid == tguid and len(extra) >= 2:
                    try:
                        sid   = int(extra[0])
                        sname = extra[1].strip('"')
                    except (ValueError, IndexError):
                        continue
                    offset = ts - current.start_time
                    cast = CastEvent(time_offset=offset, spell_id=sid, spell_name=sname)
                    current.all_casts.append(cast)
                    if sid in MAJOR_COOLDOWNS:
                        current.cd_casts.append(cast)

            # ── UNIT_DIED ────────────────────────────────────────────────────
            elif event == "UNIT_DIED":
                if dst_guid == tguid:
                    current.player_died = True
                    current.death_time_offset = ts - current.start_time

    return encounters, detected


# ──────────────────────────────────────────────────────────────────────────────
# KPI HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def _merge_by_name(spell_dict: dict) -> dict[str, dict]:
    """
    TBC Classic logs a unique spell ID per rank (e.g. Shadow Bolt r10 ≠ r11).
    Merge all entries that share the same spell name so the dashboard shows
    one row per spell, not one per rank.
    Returns {spell_name: {hits, total}}.
    """
    merged: dict[str, dict] = {}
    for sid, v in spell_dict.items():
        name = v["name"] or str(sid)
        if name not in merged:
            merged[name] = {"hits": 0, "total": 0}
        merged[name]["hits"]  += v["hits"]
        merged[name]["total"] += v["total"]
    return merged


def top_spells(enc: EncounterData, n: int = 8) -> list[dict]:
    """Return top-N spells by total damage, with ranks merged by name."""
    by_name = _merge_by_name(enc.damage_by_spell)
    rows = [
        {"spell": name, "hits": v["hits"], "total": v["total"],
         "pct": v["total"] / max(enc.player_total_damage, 1) * 100}
        for name, v in by_name.items()
    ]
    return sorted(rows, key=lambda r: r["total"], reverse=True)[:n]


def cooldown_summary(enc: EncounterData) -> list[dict]:
    """Aggregate CD usage: name, count, first_cast_offset."""
    agg: dict[int, dict] = {}
    for cast in enc.cd_casts:
        if cast.spell_id not in agg:
            agg[cast.spell_id] = {"name": cast.spell_name, "count": 0, "first": cast.time_offset, "times": []}
        agg[cast.spell_id]["count"] += 1
        agg[cast.spell_id]["times"].append(cast.time_offset)
    return sorted(agg.values(), key=lambda x: x["first"])


def fmt_offset(secs: float) -> str:
    """Float seconds → 'M:SS'."""
    m, s = divmod(int(secs), 60)
    return f"{m}:{s:02d}"


# ──────────────────────────────────────────────────────────────────────────────
# EXCEL DASHBOARD BUILDER
# ──────────────────────────────────────────────────────────────────────────────

def build_dashboard(encounters: list[EncounterData], out_path: str, wcl_data: dict = None):
    try:
        from openpyxl import Workbook
        from openpyxl.styles import (Font, PatternFill, Alignment,
                                      Border, Side, numbers)
        from openpyxl.utils import get_column_letter
        from openpyxl.chart import BarChart, LineChart, Reference
        from openpyxl.chart.series import DataPoint
    except ImportError:
        print("ERROR: openpyxl not installed. Run: pip install openpyxl")
        sys.exit(1)

    wb = Workbook()
    wb.remove(wb.active)  # remove default sheet

    # ── Color palette ────────────────────────────────────────────────────────
    C_HEADER_BG  = "1E3A5F"   # dark navy
    C_HEADER_FG  = "FFFFFF"
    C_ACCENT1    = "F5A623"   # gold
    C_ACCENT2    = "4A90D9"   # blue
    C_KILL_BG    = "D4EDDA"   # light green
    C_WIPE_BG    = "FDECEA"   # light red
    C_SECTION_BG = "EAF0FB"   # section header light blue
    C_ALT_ROW    = "F7F9FC"

    def hdr_font(sz=11, bold=True, color=C_HEADER_FG):
        return Font(name="Arial", size=sz, bold=bold, color=color)

    def body_font(sz=10, bold=False, color="000000"):
        return Font(name="Arial", size=sz, bold=bold, color=color)

    def hdr_fill(color=C_HEADER_BG):
        return PatternFill("solid", fgColor=color)

    def cell_fill(color):
        return PatternFill("solid", fgColor=color)

    def thin_border():
        s = Side(style="thin", color="CCCCCC")
        return Border(left=s, right=s, top=s, bottom=s)

    def center():
        return Alignment(horizontal="center", vertical="center", wrap_text=True)

    def right_align():
        return Alignment(horizontal="right", vertical="center")

    def write_header_row(ws, row, cols, bg=C_HEADER_BG):
        for c, val in enumerate(cols, 1):
            cell = ws.cell(row=row, column=c, value=val)
            cell.font  = hdr_font(color=C_HEADER_FG if bg == C_HEADER_BG else "000000")
            cell.fill  = hdr_fill(bg)
            cell.alignment = center()
            cell.border = thin_border()

    def number_fmt(cell, fmt="#,##0"):
        cell.number_format = fmt

    # ════════════════════════════════════════════════════════════════════════
    # SHEET 1 — SUMMARY (one row per encounter / pull)
    # ════════════════════════════════════════════════════════════════════════
    ws_sum = wb.create_sheet("📊 Summary")
    ws_sum.freeze_panes = "A3"
    ws_sum.sheet_view.showGridLines = False

    # Title
    ws_sum.merge_cells("A1:L1")
    title_cell = ws_sum["A1"]
    title_cell.value = "⚔️  WoW Ranged DPS — Raid Performance Dashboard"
    title_cell.font = Font(name="Arial", size=14, bold=True, color=C_HEADER_BG)
    title_cell.alignment = center()
    ws_sum.row_dimensions[1].height = 28

    headers = [
        "Boss", "Pull #", "Difficulty", "Duration",
        "Result", "Player DPS", "Total Dmg",
        "Dmg Share %", "Dmg Taken", "Deaths",
        "CDs Used", "WCL %ile",
    ]
    write_header_row(ws_sum, 2, headers)
    ws_sum.row_dimensions[2].height = 22

    col_widths = [24, 8, 12, 10, 8, 14, 16, 12, 14, 8, 10, 10]
    for i, w in enumerate(col_widths, 1):
        ws_sum.column_dimensions[get_column_letter(i)].width = w

    data_start = 3
    for enc in encounters:
        row = data_start + encounters.index(enc)
        is_kill = enc.success
        bg = C_KILL_BG if is_kill else C_WIPE_BG

        wcl_pct = ""
        if wcl_data:
            key = (enc.encounter_name, enc.difficulty)
            wcl_pct = wcl_data.get(key, {}).get("percentile", "")

        vals = [
            enc.encounter_name,
            enc.pull_number,
            enc.difficulty,
            enc.duration_str,
            "✅ Kill" if is_kill else "💀 Wipe",
            round(enc.dps),
            enc.player_total_damage,
            round(enc.damage_share_pct, 1),
            enc.total_damage_taken,
            1 if enc.player_died else 0,
            len(enc.cd_casts),
            wcl_pct,
        ]
        for col, val in enumerate(vals, 1):
            cell = ws_sum.cell(row=row, column=col, value=val)
            cell.font = body_font()
            cell.fill = cell_fill(bg)
            cell.border = thin_border()
            if col in (6, 7, 9):
                cell.number_format = "#,##0"
                cell.alignment = right_align()
            elif col == 8:
                cell.number_format = "0.0"
                cell.alignment = right_align()
            else:
                cell.alignment = center()

    # ════════════════════════════════════════════════════════════════════════
    # SHEET 2 — DPS TREND (line chart across pulls)
    # ════════════════════════════════════════════════════════════════════════
    ws_trend = wb.create_sheet("📈 DPS Trend")
    ws_trend.sheet_view.showGridLines = False

    write_header_row(ws_trend, 1, ["Pull Label", "DPS", "Avg DPS (=AVERAGE)"])
    ws_trend.column_dimensions["A"].width = 28
    ws_trend.column_dimensions["B"].width = 14
    ws_trend.column_dimensions["C"].width = 18

    for i, enc in enumerate(encounters, 2):
        label = f"{enc.encounter_name} P{enc.pull_number} ({'K' if enc.success else 'W'})"
        ws_trend.cell(row=i, column=1, value=label).font = body_font()
        dps_cell = ws_trend.cell(row=i, column=2, value=round(enc.dps))
        dps_cell.number_format = "#,##0"
        dps_cell.font = body_font()

    if len(encounters) > 0:
        last_row = 1 + len(encounters)
        # Avg formula column
        for i in range(2, last_row + 1):
            ws_trend.cell(row=i, column=3,
                          value=f"=AVERAGE($B$2:$B${last_row})").number_format = "#,##0"

        # Line chart
        chart = LineChart()
        chart.title = "DPS Across Pulls"
        chart.style = 10
        chart.y_axis.title = "DPS"
        chart.x_axis.title = "Pull"
        chart.height = 14
        chart.width  = 28

        data_ref = Reference(ws_trend, min_col=2, max_col=3,
                             min_row=1, max_row=last_row)
        chart.add_data(data_ref, titles_from_data=True)

        labels = Reference(ws_trend, min_col=1, min_row=2, max_row=last_row)
        chart.set_categories(labels)
        chart.series[0].graphicalProperties.line.solidFill = C_ACCENT2
        chart.series[1].graphicalProperties.line.dashDot = "dash"
        chart.series[1].graphicalProperties.line.solidFill = C_ACCENT1
        ws_trend.add_chart(chart, "E1")

    # ════════════════════════════════════════════════════════════════════════
    # SHEET 3 — COOLDOWN ANALYSIS
    # ════════════════════════════════════════════════════════════════════════
    ws_cd = wb.create_sheet("⏱ Cooldowns")
    ws_cd.sheet_view.showGridLines = False
    ws_cd.freeze_panes = "A3"

    ws_cd.merge_cells("A1:G1")
    ws_cd["A1"].value = "Cooldown Usage by Encounter"
    ws_cd["A1"].font = Font(name="Arial", size=12, bold=True, color=C_HEADER_BG)

    write_header_row(ws_cd, 2,
                     ["Boss", "Pull", "Result", "Cooldown", "# Used", "First Use", "All Timings"])
    for i, w in enumerate([24, 7, 8, 28, 8, 12, 40], 1):
        ws_cd.column_dimensions[get_column_letter(i)].width = w

    row = 3
    for enc in encounters:
        cds = cooldown_summary(enc)
        if not cds:
            cell = ws_cd.cell(row=row, column=1, value=enc.encounter_name)
            cell.font = body_font()
            ws_cd.cell(row=row, column=2, value=enc.pull_number).font = body_font()
            ws_cd.cell(row=row, column=3,
                       value="Kill" if enc.success else "Wipe").font = body_font()
            ws_cd.cell(row=row, column=4, value="— no major CDs detected —").font = body_font(color="999999")
            row += 1
            continue
        for cd in cds:
            bg = C_KILL_BG if enc.success else C_WIPE_BG
            for col, val in enumerate([
                enc.encounter_name,
                enc.pull_number,
                "Kill" if enc.success else "Wipe",
                cd["name"],
                cd["count"],
                fmt_offset(cd["first"]),
                "  |  ".join(fmt_offset(t) for t in cd["times"]),
            ], 1):
                cell = ws_cd.cell(row=row, column=col, value=val)
                cell.font = body_font()
                cell.fill = cell_fill(bg)
                cell.border = thin_border()
                cell.alignment = Alignment(horizontal="center" if col in (2,5,6) else "left",
                                           vertical="center")
            row += 1

    # ════════════════════════════════════════════════════════════════════════
    # SHEET 4 — DAMAGE TAKEN (avoidable damage candidates)
    # ════════════════════════════════════════════════════════════════════════
    ws_dt = wb.create_sheet("🛡 Damage Taken")
    ws_dt.sheet_view.showGridLines = False
    ws_dt.freeze_panes = "A3"

    ws_dt.merge_cells("A1:H1")
    ws_dt["A1"].value = "Damage Taken by Spell (review high-hit spells for avoidable damage)"
    ws_dt["A1"].font = Font(name="Arial", size=12, bold=True, color=C_HEADER_BG)

    write_header_row(ws_dt, 2,
                     ["Boss", "Pull", "Result", "Spell", "Hits", "Total Taken", "Avg/Hit", "% of Total Taken"])
    for i, w in enumerate([24, 7, 8, 32, 8, 16, 12, 16], 1):
        ws_dt.column_dimensions[get_column_letter(i)].width = w

    row = 3
    for enc in encounters:
        spells = sorted(enc.damage_taken.items(), key=lambda x: x[1]["total"], reverse=True)
        for sid, v in spells[:10]:
            if v["total"] == 0:
                continue
            bg = C_ALT_ROW if row % 2 == 0 else "FFFFFF"
            avg_hit = v["total"] // max(v["hits"], 1)
            pct     = v["total"] / max(enc.total_damage_taken, 1) * 100
            for col, val in enumerate([
                enc.encounter_name,
                enc.pull_number,
                "Kill" if enc.success else "Wipe",
                v["name"] or str(sid),
                v["hits"],
                v["total"],
                avg_hit,
                round(pct, 1),
            ], 1):
                cell = ws_dt.cell(row=row, column=col, value=val)
                cell.font = body_font()
                cell.fill = cell_fill(bg)
                cell.border = thin_border()
                if col in (5, 6, 7):
                    cell.number_format = "#,##0"
                    cell.alignment = right_align()
                elif col == 8:
                    cell.number_format = "0.0"
                    cell.alignment = right_align()
                else:
                    cell.alignment = center() if col <= 3 else Alignment(horizontal="left", vertical="center")
            row += 1

    # ════════════════════════════════════════════════════════════════════════
    # SHEETS 5..N — Per-Boss Spell Breakdown
    # ════════════════════════════════════════════════════════════════════════
    seen_bosses: set[str] = set()
    for enc in encounters:
        boss_key = enc.encounter_name
        if boss_key in seen_bosses:
            continue
        seen_bosses.add(boss_key)

        # Aggregate all pulls of this boss (kills only if any, else all)
        boss_encs = [e for e in encounters if e.encounter_name == boss_key]
        kills = [e for e in boss_encs if e.success]
        agg_encs = kills if kills else boss_encs

        tab_name = enc.encounter_name[:28].replace("/", "-").replace("\\", "-").replace(":", "")
        ws_boss = wb.create_sheet(f"🐉 {tab_name}"[:31])
        ws_boss.sheet_view.showGridLines = False

        ws_boss.merge_cells("A1:F1")
        ws_boss["A1"].value = f"{enc.encounter_name} — Spell Breakdown ({'Kills' if kills else 'All Pulls'})"
        ws_boss["A1"].font = Font(name="Arial", size=12, bold=True, color=C_HEADER_BG)

        write_header_row(ws_boss, 2,
                         ["Spell", "Hits", "Total Damage", "DPS Contribution", "% of Player Dmg", "Avg/Hit"])
        for i, w in enumerate([32, 8, 16, 16, 16, 14], 1):
            ws_boss.column_dimensions[get_column_letter(i)].width = w

        # Aggregate spells across chosen pulls, merging by name (handles TBC ranked spells)
        agg_by_name: dict[str, dict] = {}
        agg_duration = sum(e.duration for e in agg_encs)
        for e in agg_encs:
            merged = _merge_by_name(e.damage_by_spell)
            for name, v in merged.items():
                if name not in agg_by_name:
                    agg_by_name[name] = {"hits": 0, "total": 0}
                agg_by_name[name]["hits"]  += v["hits"]
                agg_by_name[name]["total"] += v["total"]

        total_dmg = sum(v["total"] for v in agg_by_name.values())

        # Reformat as list of (key, value) tuples so existing loop still works
        sorted_spells = sorted(agg_by_name.items(), key=lambda x: x[1]["total"], reverse=True)
        for i, (spell_name, v) in enumerate(sorted_spells, 3):
            bg = C_ALT_ROW if i % 2 == 0 else "FFFFFF"
            dps_contrib = v["total"] / max(agg_duration, 1)
            avg_hit = v["total"] // max(v["hits"], 1)
            pct = v["total"] / max(total_dmg, 1) * 100
            for col, val in enumerate([
                spell_name,
                v["hits"],
                v["total"],
                round(dps_contrib),
                round(pct, 1),
                avg_hit,
            ], 1):
                cell = ws_boss.cell(row=i, column=col, value=val)
                cell.font = body_font()
                cell.fill = cell_fill(bg)
                cell.border = thin_border()
                if col in (2, 3, 4, 6):
                    cell.number_format = "#,##0"
                    cell.alignment = right_align()
                elif col == 5:
                    cell.number_format = "0.0"
                    cell.alignment = right_align()
                else:
                    cell.alignment = Alignment(horizontal="left", vertical="center")

        # Bar chart
        if len(sorted_spells) >= 2:
            chart = BarChart()
            chart.type  = "bar"
            chart.title = f"{enc.encounter_name} — Damage by Spell"
            chart.style = 10
            chart.y_axis.title = "Total Damage"
            chart.height = 14
            chart.width  = 28

            last = 2 + len(sorted_spells)
            data_ref = Reference(ws_boss, min_col=3, min_row=2, max_row=last)
            chart.add_data(data_ref, titles_from_data=True)
            labels = Reference(ws_boss, min_col=1, min_row=3, max_row=last)
            chart.set_categories(labels)
            ws_boss.add_chart(chart, "H2")

    wb.save(out_path)
    print(f"\n✅  Dashboard saved → {out_path}")


# ──────────────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="WoW Ranged DPS Log Analyzer")
    ap.add_argument("log",    help="Path to CombatLog.txt")
    ap.add_argument("--player", default="", help="Your character name (auto-detected if omitted)")
    ap.add_argument("--out",  default="wow_dashboard.xlsx", help="Output .xlsx path")
    ap.add_argument("--wcl-char",  default="", help="WarcraftLogs character name for percentile lookup")
    ap.add_argument("--wcl-realm", default="", help="Realm (e.g. 'Area 52')")
    ap.add_argument("--wcl-region", default="US", help="Region: US, EU, KR, TW")
    args = ap.parse_args()

    print(f"Parsing {args.log} …")
    encounters, detected = parse_log(args.log, args.player)

    if not encounters:
        print("No encounters found. Check that ENCOUNTER_START/END events are present.")
        sys.exit(1)

    print(f"Player detected: {detected or '(unknown)'}")
    print(f"Encounters parsed: {len(encounters)}")
    for enc in encounters:
        print(f"  {enc.encounter_name} Pull {enc.pull_number} "
              f"({'Kill' if enc.success else 'Wipe'}) — "
              f"{enc.duration_str}  {round(enc.dps):,} DPS")

    # Optional WCL comparison
    wcl_data = {}
    if args.wcl_char and args.wcl_realm:
        try:
            from wcl_compare import fetch_wcl_percentiles
            print(f"\nFetching WCL data for {args.wcl_char}-{args.wcl_realm} ({args.wcl_region}) …")
            wcl_data = fetch_wcl_percentiles(
                args.wcl_char, args.wcl_realm, args.wcl_region, encounters
            )
        except Exception as ex:
            print(f"  WCL lookup skipped: {ex}")

    build_dashboard(encounters, args.out, wcl_data)


if __name__ == "__main__":
    main()
