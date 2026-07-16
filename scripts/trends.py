"""trends.py — week-over-week trend enrichment (delta_* fields).

enrich_with_trends() reads the PREVIOUS week's rows from the history DB and attaches
delta_* fields to a MAPPED week_data dict. Pipeline-order contract (enforced by
week_build.commit_week and its tests): runs AFTER map_to_week_data and BEFORE
db_writer.write_week — otherwise this week's row overwrites last week's before it can be
read. Extracted verbatim from wcl_auto_dashboard, which re-exports it (week_build keeps
calling W.enrich_with_trends so the test monkeypatch seam stays intact).
"""
from __future__ import annotations

from pathlib import Path


def enrich_with_trends(week_data: dict, db_path) -> dict:
    """Attach week-over-week delta_* fields to the per-player items of a MAPPED week_data
    dict, by reading the PREVIOUS week's values from the history DB.

    Must run AFTER build_week_data()/map_to_week_data() and BEFORE write_week() — otherwise
    this week's row overwrites last week's before we can read it. Never throws: any DB error
    (missing file, missing table on an older DB, no prior week) just yields no deltas, and the
    HTML degrades silently (the fmtDelta() helper renders nothing for undefined values).

    db_path: the SAME DB the run will write to (DB_PATH or DB_PATH_TEST), so --test-db reads
    and writes the test DB consistently.
    """
    import sqlite3
    try:
        path = Path(db_path)
        if not path.exists():
            return week_data                      # first ever run — nothing to compare to
        con = sqlite3.connect(path)
        try:
            current = (week_data.get("meta") or {}).get("report_code")
            # Prior week = the most recent week STRICTLY BEFORE this one by CHRONOLOGICAL
            # start_ms (epoch ms). Must be `start_ms < this week's start_ms`, NOT merely "any
            # other report" — build_site.py reuses this to enrich EARLIER weeks for the rolling
            # multi-week view, where "most recent other report" would be a FUTURE week and trend
            # each past week against the latest (e.g. June 1 vs June 8). Each week trends against
            # its OWN predecessor. (start_ms is the canonical sort key; the date string misorders.)
            cur_ms = (week_data.get("meta") or {}).get("start_ms")
            if cur_ms is None:
                r0 = con.execute("SELECT start_ms FROM weeks WHERE report_code=?", (current,)).fetchone()
                cur_ms = r0[0] if r0 else None
            if cur_ms is None:
                # No chronological anchor — fall back to the global-latest other report.
                row = con.execute(
                    "SELECT report_code, date, kills, start_ms FROM weeks WHERE report_code != ? "
                    "ORDER BY start_ms DESC, date DESC LIMIT 1",
                    (current,)
                ).fetchone()
            else:
                # Prefer an anchored prior (start_ms < this week). A legacy row with NULL
                # start_ms (predates the migration) would be silently excluded by a bare
                # `start_ms < ?` — so include it as a LOWER-priority fallback so we trend
                # against it rather than returning nothing. `ORDER BY (start_ms IS NULL)`
                # keeps anchored priors first; the NULL row is chosen only if none exist.
                row = con.execute(
                    "SELECT report_code, date, kills, start_ms FROM weeks "
                    "WHERE report_code != ? AND (start_ms < ? OR start_ms IS NULL) "
                    "ORDER BY (start_ms IS NULL), start_ms DESC LIMIT 1",
                    (current, cur_ms)
                ).fetchone()
            if not row:
                return week_data                  # only one week of history
            prev, prev_date, prev_kills, prev_ms = row[0], row[1], row[2], row[3]
            if prev_ms is None:
                print(f"  ⚠ trends: prior week {prev} has no start_ms (predates migration) — "
                      f"baseline ordering is best-effort; run `reprocess.py --all` to anchor it")

            # Stale-prior-week guard: a delta reads the prior week's row, so if that row predates a
            # schema bump (missing new columns) the delta SILENTLY blanks. Warn loudly instead — the
            # fix is `reprocess.py --all`, which re-stamps every week to the current schema_version.
            try:
                import db_writer as _dbw
                _sv = con.execute("SELECT schema_version FROM weeks WHERE report_code=?", (prev,)).fetchone()
                _sv = _sv[0] if _sv else None
                if _sv is None or _sv < _dbw.SCHEMA_VERSION:
                    print(f"  ⚠ trends: prior week {prev} predates schema v{_dbw.SCHEMA_VERSION} "
                          f"(row is v{_sv}) — some delta_* may be blank; run `reprocess.py --all`")
            except sqlite3.OperationalError:
                pass   # older DB without the schema_version column

            def pv(table, player, col):
                """Previous-week value, or None if absent (also tolerates a missing
                table/column on an older DB that predates this metric)."""
                try:
                    r = con.execute(
                        f"SELECT {col} FROM {table} WHERE report_code=? AND player=?",
                        (prev, player)
                    ).fetchone()
                    return r[0] if r and r[0] is not None else None
                except sqlite3.OperationalError:
                    return None

            # ── damage: trend BOSS DPS (boss damage / boss time), matching the live DPS
            #    table's WCL-style denominator. Compare against the `dps` table's dps (also
            #    boss DPS now). Delta lands on each damageBySelection player (shown in the
            #    Bosses view + the header pill).
            dsel = week_data.get("damageBySelection") or {}
            durs = dsel.get("durations") or {}
            # delta DPS / uptime / total for EACH selection (All/Bosses/Trash), comparing the
            # matching prior-week columns (boss = base dps/total/uptime; all_*/trash_* added).
            SEL_COLS = {"boss":  ("dps", "total", "uptime"),
                        "all":   ("all_dps", "all_total", "all_uptime"),
                        "trash": ("trash_dps", "trash_total", "trash_uptime")}
            for it in (dsel.get("players") or []):
                nm = it.get("name")
                for sel, (c_dps, c_total, c_up) in SEL_COLS.items():
                    d = durs.get(sel) or 0
                    sd = it.get(sel)
                    if not d or sd is None:
                        continue
                    pd, pu, pt = pv("dps", nm, c_dps), pv("dps", nm, c_up), pv("dps", nm, c_total)
                    if pd is not None:
                        sd["delta_dps"] = round(sd.get("total", 0) / d - pd, 1)
                    if pu is not None:
                        sd["delta_uptime"] = round(sd.get("active", 0) / 1000 / d * 100 - pu, 1)
                    if pt is not None:
                        sd["delta_total"] = round(sd.get("total", 0) - pt, 1)

            # ── DPS parse %: delta the percentile vs last week (top-level on each dsel player).
            for it in (dsel.get("players") or []):
                pw = pv("dps", it.get("name"), "war")
                # `is not None` on BOTH sides — a legitimate 0th-percentile parse still deltas.
                if pw is not None and it.get("vs_replacement") is not None:
                    it["delta_vs_replacement"] = round(it["vs_replacement"] - pw, 2)

            # ── class toolkit: delta the signature metric, but ONLY when the metric KIND
            #    (label) matches last week — comparing Windfury casts to ToW uptime is nonsense.
            for it in (dsel.get("players") or []):
                tk = it.get("toolkit")
                if not tk or tk.get("num") is None:
                    continue
                try:
                    r = con.execute("SELECT label, num FROM class_toolkit WHERE report_code=? AND player=?",
                                    (prev, it.get("name"))).fetchone()
                except sqlite3.OperationalError:
                    r = None
                if r and r[0] == tk.get("label") and r[1] is not None:
                    tk["delta"] = round(tk["num"] - r[1], 1)

            # ── mana returns: delta per provider, per source (matched on player+source).
            for b in (week_data.get("manaReturns") or {}).get("batteries", []):
                for prov in b.get("providers", []):
                    try:
                        r = con.execute(
                            "SELECT mana FROM mana_returns WHERE report_code=? AND player=? AND source=?",
                            (prev, prov["name"], b.get("label"))).fetchone()
                    except sqlite3.OperationalError:
                        r = None
                    if r and r[0] is not None:
                        prov["delta_mana"] = prov.get("mana", 0) - r[0]

            # ── sunder armor: delta total + effective stacks built, per warrior.
            for p in (week_data.get("sunderArmor") or {}).get("players", []):
                try:
                    r = con.execute("SELECT total, effective FROM sunder_armor WHERE report_code=? AND player=?",
                                    (prev, p.get("name"))).fetchone()
                except sqlite3.OperationalError:
                    r = None
                if r:
                    if r[0] is not None:
                        p["delta_total"] = p.get("total", 0) - r[0]
                    if r[1] is not None:
                        p["delta_effective"] = p.get("effective", 0) - r[1]

            # ── healthstones: raid-level deltas (total used + # who died never popping).
            hs = week_data.get("healthstoneStats")
            if hs:
                try:
                    r = con.execute("SELECT hs_used, hs_died_no_stone FROM weeks WHERE report_code=?",
                                    (prev,)).fetchone()
                except sqlite3.OperationalError:
                    r = None
                if r:
                    if r[0] is not None and hs.get("total_used") is not None:
                        hs["delta_used"] = hs["total_used"] - r[0]
                    if r[1] is not None and hs.get("died_no_stone") is not None:
                        hs["delta_no_stone"] = hs["died_no_stone"] - r[1]

            # ── healing: four trended columns (HPS, overheal, activity, mana-efficiency).
            for it in (week_data.get("healing") or []):
                nm = it.get("name")
                for cur_field, db_col, delta_field in (
                    ("eff_hps",      "eff_hps",      "delta_hps"),
                    ("overheal_pct", "overheal_pct", "delta_overheal"),
                    ("activity_pct", "activity_pct", "delta_activity"),
                    ("mana_eff",     "mana_eff",     "delta_hp_per_mana"),
                ):
                    p = pv("healing", nm, db_col)
                    if p is not None:
                        it[delta_field] = round((it.get(cur_field) or 0) - p, 1)

            # ── single-field KPIs: (week key, table, current field, db col, delta field)
            #    (luck is intentionally NOT trended: the `luck_kpi.luck` column is NULL by
            #    design — luck is derived at render time from the multi-week crit baseline,
            #    not persisted — so a week-over-week DB delta is meaningless.)
            for wk_key, table, cur_field, db_col, delta_field in (
                ("avoidableDmg",  "avoidable_dmg",  "dmg",   "dmg",   "delta_dmg"),
                ("deaths",        "deaths",         "total", "total", "delta_deaths"),
                ("drums",         "drums",          "score", "score", "delta_drums"),
                ("saves",         "saves",          "save",  "save",  "delta_save"),
                ("dispels",       "dispels",        "total", "total", "delta_total"),
                # tank DTPS (lower better → HTML renders ▼ green via fmtDelta(...,false)).
                # Absent until 2 weeks of the new tank_scorecard table exist (no backfill).
                ("tankScorecard", "tank_scorecard", "dtps",  "dtps",  "delta_dtps"),
            ):
                for it in (week_data.get(wk_key) or []):
                    p = pv(table, it.get("name"), db_col)
                    if p is not None:
                        it[delta_field] = round((it.get(cur_field) or 0) - p, 1)

            # ── tank threat parse % + survivability grade — dedicated (survival is nested under
            #    .score, and the parse-% delta wants its own pass).
            for it in (week_data.get("tankScorecard") or []):
                nm = it.get("name")
                pw = pv("tank_scorecard", nm, "war")
                if pw is not None and it.get("vs_replacement") is not None:
                    it["delta_vs_replacement"] = round(it["vs_replacement"] - pw, 2)
                ps, sv = pv("tank_scorecard", nm, "survival"), it.get("survival")
                if ps is not None and isinstance(sv, dict) and sv.get("score") is not None:
                    sv["delta_score"] = round(sv["score"] - ps, 1)

            # ── prior-week header context (powers the "vs last week" header pills).
            #    Kill-time delta is summed over the bosses cleared in BOTH weeks so it's
            #    fight-count agnostic — comparing raw totals would unfairly reward a week
            #    that simply downed fewer bosses. Negative delta = faster this week.
            prev_bt = {r[0]: r[1] for r in con.execute(
                "SELECT boss, seconds FROM boss_times WHERE report_code=?", (prev,)).fetchall()}
            cur_bt  = week_data.get("boss_times") or {}
            common  = [b for b in cur_bt if b in prev_bt]
            # Per-boss kill-time delta for the Overview tiles (negative = faster this week).
            # Folded in here — same start_ms-ordered prior week as the header pill — rather
            # than a separate ORDER BY date pass (which misorders; see memory).
            for b, meta in (week_data.get("boss_meta") or {}).items():
                if b in prev_bt and meta.get("seconds") is not None:
                    meta["delta_seconds"] = round(meta["seconds"] - prev_bt[b], 1)
            prev_ctx = {"report_code": prev, "date": prev_date, "kills": prev_kills}
            # Raid-level prior totals — power the aggregate "vs last week" deltas on the stat
            # tiles / cohort cards as Σ(this week) − Σ(prev week). Summing the per-player
            # delta_* fields there instead silently drops churn: a raider with no prior-week
            # row carries no delta, and last week's dead who sat out this week aren't in this
            # week's array at all (the Jul-13 deaths tile read +65 on a true +90).
            def prev_sum(table, col):
                try:
                    r = con.execute(f"SELECT SUM({col}) FROM {table} WHERE report_code=?",
                                    (prev,)).fetchone()
                    return r[0] if r else None        # SUM over no rows → NULL → None
                except sqlite3.OperationalError:
                    return None                       # older DB without the table
            prev_ctx["totals"] = {k: v for k, v in (
                ("deaths",    prev_sum("deaths", "total")),
                ("avoidable", prev_sum("avoidable_dmg", "dmg")),
                ("drums",     prev_sum("drums", "total")),
            ) if v is not None}
            if common:
                prev_ctx["delta_kill_secs"] = round(
                    sum(cur_bt[b] for b in common) - sum(prev_bt[b] for b in common), 1)
                prev_ctx["common_bosses"] = len(common)
            week_data["prev"] = prev_ctx
        finally:
            con.close()
    except Exception as e:
        print(f"  [trends] warning: {e}")
    return week_data
