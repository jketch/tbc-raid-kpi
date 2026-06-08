"""
db_writer.py — Persist WEEK_DATA into a local SQLite database for historical analysis.

DB location: Gaming/cache/raid_history.db

Schema (one row per player per week, keyed by report_code):
  weeks         — one row per weekly run (meta)
  roster        — player class/spec/role per week
  luck_kpi      — actual vs expected crit, luck delta
  avoidable_dmg — avoidable damage taken
  deaths        — total deaths
  consumables   — consumable score
  drums         — drums of battle casts/score
  engineering   — engineering damage + ability breakdown
  interrupts    — interrupt counts
  crit          — crit % by role type (caster/physical/healer/tank)
  boss_times    — kill times per boss per week

Usage:
  from db_writer import write_week
  write_week(week_data)   # week_data = output of map_to_week_data()
"""

import sqlite3
import json
from pathlib import Path

DB_PATH      = Path(__file__).parent.parent / "cache" / "raid_history.db"
DB_PATH_TEST = Path(__file__).parent.parent / "cache" / "raid_history_test.db"


# ── Schema ────────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS weeks (
    report_code  TEXT PRIMARY KEY,
    date         TEXT,
    zone         TEXT,
    kills        INTEGER,
    start_ms     INTEGER          -- report start epoch (ms); CHRONOLOGICAL sort key.
);

CREATE TABLE IF NOT EXISTS roster (
    report_code  TEXT,
    player       TEXT,
    class        TEXT,
    spec         TEXT,
    role         TEXT,
    PRIMARY KEY (report_code, player)
);

CREATE TABLE IF NOT EXISTS luck_kpi (
    report_code  TEXT,
    player       TEXT,
    role         TEXT,
    actual       REAL,
    expected     REAL,
    luck         REAL,
    PRIMARY KEY (report_code, player)
);

CREATE TABLE IF NOT EXISTS avoidable_dmg (
    report_code  TEXT,
    player       TEXT,
    role         TEXT,
    dmg          INTEGER,
    PRIMARY KEY (report_code, player)
);

CREATE TABLE IF NOT EXISTS deaths (
    report_code  TEXT,
    player       TEXT,
    role         TEXT,
    total        INTEGER,
    trash        INTEGER,
    PRIMARY KEY (report_code, player)
);

CREATE TABLE IF NOT EXISTS avoidable_sources (
    report_code  TEXT,
    player       TEXT,
    mechanic     TEXT,
    boss         TEXT,
    dmg          INTEGER,
    PRIMARY KEY (report_code, player, mechanic)
);

CREATE TABLE IF NOT EXISTS healing (
    report_code   TEXT,
    player        TEXT,
    role          TEXT,
    eff_hps       INTEGER,
    eff_heal      INTEGER,
    overheal_pct  REAL,
    activity_pct  REAL,
    tank_pct      REAL,
    mana_eff      REAL,     -- effective healing per mana spent (estimate)
    vs_replacement REAL,    -- HPS vs same-spec cohort median (1.15 = 15% above)
    top_spell     TEXT,
    PRIMARY KEY (report_code, player)
);

CREATE TABLE IF NOT EXISTS healing_spells (
    report_code   TEXT,
    player        TEXT,
    spell         TEXT,
    casts         INTEGER,
    eff           INTEGER,
    per_cast      INTEGER,   -- effective healing per cast
    overheal_pct  REAL,
    crit_pct      REAL,
    PRIMARY KEY (report_code, player, spell)
);

CREATE TABLE IF NOT EXISTS friendly_fire (
    report_code  TEXT,
    player       TEXT,
    role         TEXT,
    dmg          INTEGER,
    incidents    INTEGER,
    category     TEXT,      -- dominant cause: mc | engineering | mechanic
    mc_dmg       INTEGER,   -- damage dealt while Mind Controlled (not their fault)
    eng_dmg      INTEGER,   -- sapper splash
    mech_dmg     INTEGER,   -- clumping / positioning
    mc_count     INTEGER,   -- times this player was MC'd
    PRIMARY KEY (report_code, player)
);

CREATE TABLE IF NOT EXISTS consumables (
    report_code  TEXT,
    player       TEXT,
    role         TEXT,
    score        REAL,
    suboptimal   TEXT,
    badges       TEXT,
    flask        INTEGER,-- compliance: flask OR both elixir slots (0/1)
    food         INTEGER,
    weapon       INTEGER,
    combat_pot   TEXT,   -- combat-pot item name, or NULL
    alt_pot      TEXT,   -- alt-pot item name (Dark Rune / Flame Cap / Nightmare Seed), or NULL
    PRIMARY KEY (report_code, player)
);

CREATE TABLE IF NOT EXISTS drums (
    report_code    TEXT,
    player         TEXT,
    casts          INTEGER,
    total          INTEGER,
    buffs          INTEGER,
    buffs_per_drum REAL,
    score          REAL,
    PRIMARY KEY (report_code, player)
);

CREATE TABLE IF NOT EXISTS engineering (
    report_code  TEXT,
    player       TEXT,
    role         TEXT,
    dmg          INTEGER,
    abilities    TEXT,   -- JSON dict of {abilityName: count}
    PRIMARY KEY (report_code, player)
);

CREATE TABLE IF NOT EXISTS interrupts (
    report_code  TEXT,
    player       TEXT,
    count        INTEGER,
    PRIMARY KEY (report_code, player)
);

CREATE TABLE IF NOT EXISTS crit (
    report_code  TEXT,
    player       TEXT,
    crit_type    TEXT,   -- 'caster' | 'physical' | 'healer' | 'tank'
    crit_pct     REAL,
    PRIMARY KEY (report_code, player, crit_type)
);

CREATE TABLE IF NOT EXISTS boss_times (
    report_code  TEXT,
    boss         TEXT,
    seconds      REAL,
    PRIMARY KEY (report_code, boss)
);

CREATE TABLE IF NOT EXISTS dps (
    report_code  TEXT,
    player       TEXT,
    role         TEXT,
    dps          REAL,      -- total_dmg / total kill seconds (normalizes raid length)
    total        INTEGER,   -- total damage done
    pct_raid     REAL,      -- share of raid damage (%)
    uptime       REAL,      -- active-time % (from week_data 'active_pct')
    PRIMARY KEY (report_code, player)
);

CREATE TABLE IF NOT EXISTS tank_scorecard (
    report_code  TEXT,
    player       TEXT,
    dtps         REAL,      -- damage taken/sec on tank fights (the trended headline)
    taken        INTEGER,
    hps_recv     REAL,
    deaths       INTEGER,
    phys_pct     REAL,
    magic_pct    REAL,
    crush_count  INTEGER,   -- crushing blows taken
    crit_count   INTEGER,   -- crits taken (~0 = defense-capped)
    avoid_pct    REAL,      -- melee swings avoided (miss/dodge/parry/full block)
    biggest_hit  INTEGER,
    cooldowns    TEXT,      -- JSON dict {cd_name: count}
    PRIMARY KEY (report_code, player)
);

CREATE TABLE IF NOT EXISTS tank_boss_dtps (
    report_code  TEXT,
    player       TEXT,
    boss         TEXT,
    dtps         REAL,
    taken        INTEGER,
    seconds      REAL,
    PRIMARY KEY (report_code, player, boss)
);
"""


# ── Writer ────────────────────────────────────────────────────────────────────

def write_week(week_data: dict, db_path: Path = None) -> None:
    """
    Upsert all KPI tables from a WEEK_DATA dict (output of map_to_week_data()).
    Safe to call multiple times for the same report_code — will overwrite.

    db_path: override the DB file (e.g. pass DB_PATH_TEST to avoid polluting prod).
    """
    target = Path(db_path) if db_path else DB_PATH
    target.parent.mkdir(exist_ok=True)
    con = sqlite3.connect(target)
    con.execute("PRAGMA journal_mode=WAL")  # safe for concurrent reads

    try:
        con.executescript(SCHEMA)
        # migrate older DBs that predate the start_ms sort key
        try:
            con.execute("ALTER TABLE weeks ADD COLUMN start_ms INTEGER")
        except sqlite3.OperationalError:
            pass   # column already exists
        # migrate older DBs that predate the Raid-Prep badges column
        try:
            con.execute("ALTER TABLE consumables ADD COLUMN badges TEXT")
        except sqlite3.OperationalError:
            pass   # column already exists
        # migrate older DBs to the compliance-grid columns
        for _col, _type in (("flask", "INTEGER"), ("food", "INTEGER"), ("weapon", "INTEGER"),
                            ("combat_pot", "TEXT"), ("alt_pot", "TEXT")):
            try:
                con.execute(f"ALTER TABLE consumables ADD COLUMN {_col} {_type}")
            except sqlite3.OperationalError:
                pass   # column already exists
        # migrate older DBs that predate the boss portrait/encounter id
        try:
            con.execute("ALTER TABLE boss_times ADD COLUMN encounter_id INTEGER")
        except sqlite3.OperationalError:
            pass   # column already exists

        meta = week_data.get("meta", {})
        rc   = meta.get("report_code") or week_data.get("reportCode", "unknown")

        # ── weeks ──────────────────────────────────────────────────────────────
        con.execute("""
            INSERT OR REPLACE INTO weeks (report_code, date, zone, kills, start_ms)
            VALUES (?, ?, ?, ?, ?)
        """, (rc, meta.get("date"), meta.get("zone"), meta.get("kills"), meta.get("start_ms")))

        # ── roster ─────────────────────────────────────────────────────────────
        for name, info in (week_data.get("roster") or {}).items():
            con.execute("""
                INSERT OR REPLACE INTO roster (report_code, player, class, spec, role)
                VALUES (?, ?, ?, ?, ?)
            """, (rc, name, info.get("class"), info.get("spec"), info.get("role")))

        # ── luck_kpi ───────────────────────────────────────────────────────────
        for p in (week_data.get("luckKPI") or []):
            con.execute("""
                INSERT OR REPLACE INTO luck_kpi
                    (report_code, player, role, actual, expected, luck)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (rc, p["name"], p.get("role"), p.get("actual"), p.get("expected"), p.get("luck")))

        # ── avoidable_dmg ──────────────────────────────────────────────────────
        for p in (week_data.get("avoidableDmg") or []):
            con.execute("""
                INSERT OR REPLACE INTO avoidable_dmg (report_code, player, role, dmg)
                VALUES (?, ?, ?, ?)
            """, (rc, p["name"], p.get("role"), p.get("dmg", 0)))

        # ── deaths ─────────────────────────────────────────────────────────────
        for p in (week_data.get("deaths") or []):
            con.execute("""
                INSERT OR REPLACE INTO deaths (report_code, player, role, total, trash)
                VALUES (?, ?, ?, ?, ?)
            """, (rc, p["name"], p.get("role"), p.get("total", 0), p.get("trash", 0)))

        # ── healing (per healer throughput + efficiency) ───────────────────────
        for p in (week_data.get("healing") or []):
            con.execute("""
                INSERT OR REPLACE INTO healing
                    (report_code, player, role, eff_hps, eff_heal, overheal_pct, activity_pct, tank_pct, mana_eff, vs_replacement, top_spell)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (rc, p["name"], p.get("role"), p.get("eff_hps", 0), p.get("eff_heal", 0),
                  p.get("overheal_pct", 0), p.get("activity_pct", 0), p.get("tank_pct", 0),
                  p.get("mana_eff", 0), p.get("vs_replacement", 0), p.get("top_spell", "")))

        # ── healing_spells (per healer × spell) ────────────────────────────────
        con.execute("DELETE FROM healing_spells WHERE report_code = ?", (rc,))
        for h in (week_data.get("healing") or []):
            for s in (h.get("spells") or []):
                con.execute("""
                    INSERT OR REPLACE INTO healing_spells
                        (report_code, player, spell, casts, eff, per_cast, overheal_pct, crit_pct)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (rc, h["name"], s.get("spell"), s.get("casts", 0), s.get("eff", 0),
                      s.get("per_cast", 0), s.get("overheal_pct", 0), s.get("crit_pct", 0)))

        # ── avoidable_sources (per player × mechanic, boss-attributed) ──────────
        # Grain varies per run, so clear this report's rows first to avoid staleness.
        mech_boss = {m: info.get("boss", "")
                     for m, info in (week_data.get("avoidableMechanics") or {}).items()}
        con.execute("DELETE FROM avoidable_sources WHERE report_code = ?", (rc,))
        for p in (week_data.get("avoidableDmg") or []):
            for s in (p.get("sources") or []):
                con.execute("""
                    INSERT OR REPLACE INTO avoidable_sources
                        (report_code, player, mechanic, boss, dmg)
                    VALUES (?, ?, ?, ?, ?)
                """, (rc, p["name"], s.get("ability"),
                      mech_boss.get(s.get("ability"), ""), s.get("dmg", 0)))

        # ── friendly_fire (source side) ────────────────────────────────────────
        con.execute("DELETE FROM friendly_fire WHERE report_code = ?", (rc,))
        for p in (week_data.get("friendlyFire") or []):
            cats = p.get("cats") or {}
            con.execute("""
                INSERT OR REPLACE INTO friendly_fire
                    (report_code, player, role, dmg, incidents, category, mc_dmg, eng_dmg, mech_dmg, mc_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (rc, p["name"], p.get("role"), p.get("dmg", 0), p.get("incidents", 0),
                  p.get("category", ""), cats.get("mc", 0), cats.get("engineering", 0),
                  cats.get("mechanic", 0), p.get("mc_count", 0)))

        # ── consumables ────────────────────────────────────────────────────────
        for p in (week_data.get("consumables") or []):
            con.execute("""
                INSERT OR REPLACE INTO consumables
                (report_code, player, role, score, suboptimal, badges, flask, food, weapon, combat_pot, alt_pot)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (rc, p["name"], p.get("role"), p.get("score"),
                  json.dumps(p.get("suboptimal") or []),
                  json.dumps(p.get("badges") or []),
                  int(bool(p.get("flask"))), int(bool(p.get("food"))), int(bool(p.get("weapon"))),
                  p.get("combat_pot"), p.get("alt_pot")))

        # ── drums ──────────────────────────────────────────────────────────────
        for p in (week_data.get("drums") or []):
            con.execute("""
                INSERT OR REPLACE INTO drums
                    (report_code, player, casts, total, buffs, buffs_per_drum, score)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (rc, p["name"], p.get("casts"), p.get("total"),
                  p.get("buffs"), p.get("buffs_per_drum"), p.get("score")))

        # ── engineering ────────────────────────────────────────────────────────
        for p in (week_data.get("engineering") or []):
            con.execute("""
                INSERT OR REPLACE INTO engineering (report_code, player, role, dmg, abilities)
                VALUES (?, ?, ?, ?, ?)
            """, (rc, p["name"], p.get("role"), p.get("dmg", 0),
                  json.dumps(p.get("eng") or {})))

        # ── interrupts ─────────────────────────────────────────────────────────
        for p in (week_data.get("interrupts") or []):
            con.execute("""
                INSERT OR REPLACE INTO interrupts (report_code, player, count)
                VALUES (?, ?, ?)
            """, (rc, p["name"], p.get("count", 0)))

        # ── crit (four lists → one table with crit_type column) ───────────────
        crit_map = {
            "caster":   week_data.get("casterCrit")   or [],
            "physical": week_data.get("physicalCrit") or [],
            "healer":   week_data.get("healerCrit")   or [],
            "tank":     week_data.get("tankCrit")     or [],
        }
        for crit_type, rows in crit_map.items():
            for p in rows:
                con.execute("""
                    INSERT OR REPLACE INTO crit (report_code, player, crit_type, crit_pct)
                    VALUES (?, ?, ?, ?)
                """, (rc, p["name"], crit_type, p.get("crit")))

        # ── boss_times (+ encounter_id for portraits) ──────────────────────────
        _bmeta = week_data.get("boss_meta") or {}
        for boss, seconds in (week_data.get("boss_times") or {}).items():
            con.execute("""
                INSERT OR REPLACE INTO boss_times (report_code, boss, seconds, encounter_id)
                VALUES (?, ?, ?, ?)
            """, (rc, boss, seconds, _bmeta.get(boss, {}).get("encounter_id")))

        # ── dps ────────────────────────────────────────────────────────────────
        # Persist per-player damage so it can be trended week-over-week. Store DPS
        # (total_dmg / total kill seconds) so the comparison normalizes raid length;
        # `dur` mirrors the live HTML's `sum(boss_times)`. Falls back to total when
        # boss_times is missing (no division → dps stored as 0).
        dmg_rows  = week_data.get("damage") or []
        dur       = sum((week_data.get("boss_times") or {}).values()) or 0
        raid_tot  = sum((p.get("total_dmg") or 0) for p in dmg_rows) or 0
        for p in dmg_rows:
            total = p.get("total_dmg") or 0
            con.execute("""
                INSERT OR REPLACE INTO dps (report_code, player, role, dps, total, pct_raid, uptime)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (rc, p["name"], p.get("role"),
                  round(total / dur, 2) if dur else 0,
                  total,
                  round(total / raid_tot * 100, 2) if raid_tot else 0,
                  p.get("active_pct")))

        # ── tank scorecard v2 (summary + per-boss) ─────────────────────────────
        for t in (week_data.get("tankScorecard") or []):
            bh = t.get("biggest_hit") or {}
            con.execute("""
                INSERT OR REPLACE INTO tank_scorecard
                  (report_code, player, dtps, taken, hps_recv, deaths, phys_pct, magic_pct,
                   crush_count, crit_count, avoid_pct, biggest_hit, cooldowns)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (rc, t["name"], t.get("dtps"), t.get("taken"), t.get("hps_recv"),
                  t.get("deaths"), t.get("phys_pct"), t.get("magic_pct"),
                  t.get("crush_count"), t.get("crit_count"), t.get("avoid_pct"),
                  bh.get("amount"), json.dumps(t.get("cooldowns") or {})))
            for pb in (t.get("per_boss") or []):
                con.execute("""
                    INSERT OR REPLACE INTO tank_boss_dtps
                      (report_code, player, boss, dtps, taken, seconds)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (rc, t["name"], pb.get("boss"), pb.get("dtps"),
                      pb.get("taken"), pb.get("seconds")))

        con.commit()
        print(f"  ✓ DB written → {target.name}  (report: {rc})")

    except Exception as e:
        con.rollback()
        print(f"  ⚠ DB write failed (dashboard still updated): {e}")

    finally:
        con.close()


# ── Quick query helpers (for ad-hoc analysis in a Python REPL) ────────────────

def query(sql: str, params: tuple = ()) -> list[dict]:
    """Run any SELECT and get back a list of dicts."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def crit_history(db_path=None, exclude_report: str = None, window: int = 8) -> dict:
    """Per-player crit baseline from history: {player: {"mean", "n", "std"}} over the most
    recent `window` weeks of `luck_kpi.actual`, EXCLUDING `exclude_report` (so an idempotent
    re-run of the current week never baselines against itself). This is the gold-standard
    'luck' reference — a player's own multi-week average controls for gear/spec/talents/buffs,
    leaving only RNG. Returns {} if the DB doesn't exist yet."""
    import statistics
    from collections import defaultdict
    target = Path(db_path) if db_path else DB_PATH
    if not target.exists():
        return {}
    con = sqlite3.connect(target)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute("""
            SELECT l.player AS player, l.actual AS actual, w.date AS date
            FROM luck_kpi l JOIN weeks w USING (report_code)
            WHERE l.actual IS NOT NULL AND l.report_code != ?
            ORDER BY w.start_ms, w.date
        """, (exclude_report or "",)).fetchall()
    except sqlite3.OperationalError:
        return {}   # tables not created yet
    finally:
        con.close()
    by = defaultdict(list)
    for r in rows:
        by[r["player"]].append(r["actual"])
    out = {}
    for p, vals in by.items():
        recent = vals[-window:]                      # most-recent `window` weeks
        out[p] = {
            "mean":   round(statistics.mean(recent), 1),
            "n":      len(recent),
            "std":    round(statistics.pstdev(recent), 1) if len(recent) > 1 else 0.0,
            "series": [round(v, 1) for v in recent],  # weekly actuals → sparkline + luck/gear
            "prev":   round(recent[-1], 1) if recent else None,  # last week (spike vs step)
        }
    return out


def trend(player: str, kpi: str = "luck") -> list[dict]:
    """
    Quick week-over-week trend for one player.
    kpi = 'luck' | 'actual' | 'expected'
    Example: trend('Marvels', 'luck')
    """
    return query(f"""
        SELECT w.date, l.{kpi}
        FROM luck_kpi l
        JOIN weeks w USING (report_code)
        WHERE l.player = ?
        ORDER BY w.start_ms, w.date
    """, (player,))


def shame_board(week: str = None) -> list[dict]:
    """
    Hall of Shame for a given week (or latest if omitted):
    worst avoidable damage, most deaths, lowest consumable score.
    """
    where = "WHERE w.date = ?" if week else "WHERE w.start_ms = (SELECT MAX(start_ms) FROM weeks)"
    params = (week,) if week else ()
    return query(f"""
        SELECT
            a.player,
            a.dmg          AS avoidable_dmg,
            d.total        AS deaths,
            c.score        AS consumable_score
        FROM avoidable_dmg a
        JOIN weeks w USING (report_code)
        LEFT JOIN deaths d ON d.report_code = a.report_code AND d.player = a.player
        LEFT JOIN consumables c ON c.report_code = a.report_code AND c.player = a.player
        {where}
        ORDER BY a.dmg DESC
    """, params)
