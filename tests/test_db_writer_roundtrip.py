"""Write -> read -> assert round-trip for scripts/db_writer.py::write_week.

Complements tests/test_db_writer_guard.py (which covers the downgrade-guard + schema_version).
This file proves the section -> table -> column mapping actually lands the input VALUES in the
right columns, exercises INSERT OR REPLACE idempotency, and that JSON columns survive a
dumps->loads round-trip.

IMPORTANT: db_writer.query() always opens the module-level prod DB_PATH and ignores any path —
so this file queries the TEMP DB DIRECTLY with sqlite3, never via db_writer.query().

Run:  python -m unittest discover -s tests -p "test_db_writer_roundtrip.py" -v
"""
import sys, json, sqlite3, io, contextlib, tempfile, shutil, unittest
from pathlib import Path

# write_week prints ✓/⚠ glyphs; match the pipeline and force UTF-8 so the cp1252 console
# doesn't crash mid-test.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import db_writer


@contextlib.contextmanager
def temp_db():
    """A temp DB path with Windows-tolerant cleanup (sqlite WAL can briefly hold the file)."""
    d = tempfile.mkdtemp()
    try:
        yield Path(d) / "t.db"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _week_data():
    """A representative WEEK_DATA dict — rich enough to populate weeks, roster, healing, dps
    (via damageBySelection), tank_scorecard, dispels, saves, consumables. Keys/shapes mirror
    exactly what write_week reads (see scripts/db_writer.py)."""
    return {
        "meta": {
            "date": "Jun 08, 2026",
            "zone": "Serpentshrine Cavern",
            "kills": 3,
            "start_ms": 1780000000000,
            "report_code": "ROUNDTRIP1",
        },
        "healthstoneStats": {"total_used": 14, "died_no_stone": 2},
        "roster": {
            "Marvels": {"class": "Warlock", "spec": "Destruction", "role": "Caster"},
            "Healz": {"class": "Priest", "spec": "Holy", "role": "Healer"},
            "Tanky": {"class": "Warrior", "spec": "Protection", "role": "Tank"},
        },
        # ── healing: the documented column gotcha — eff_hps, not hps ──
        "healing": [
            {"name": "Healz", "role": "Healer", "eff_hps": 4321, "eff_heal": 3_000_000,
             "overheal_pct": 22.5, "activity_pct": 88.0, "tank_pct": 40.0, "mana_eff": 1.8,
             "vs_replacement": 1.15, "top_spell": "Greater Heal",
             "spells": [{"spell": "Greater Heal", "casts": 120, "eff": 2_000_000,
                         "per_cast": 16666, "overheal_pct": 18.0, "crit_pct": 30.0}]},
        ],
        # ── dps comes from damageBySelection (the boss selection denominator) ──
        "damageBySelection": {
            "durations": {"all": 1000.0, "boss": 600.0, "trash": 400.0},
            "players": [
                {"name": "Marvels", "role": "Caster", "vs_replacement": 87.0,
                 "toolkit": {"label": "Curse of Elements uptime", "num": 95.0},
                 "boss": {"total": 6_000_000, "active": 540_000},
                 "all": {"total": 9_000_000, "active": 900_000},
                 "trash": {"total": 3_000_000, "active": 360_000}},
                {"name": "Healz", "role": "Healer", "vs_replacement": None,
                 "toolkit": {"label": None, "num": None},
                 "boss": {"total": 100_000, "active": 60_000},
                 "all": {"total": 150_000, "active": 100_000},
                 "trash": {"total": 50_000, "active": 40_000}},
            ],
        },
        # ── tank scorecard: cooldowns JSON, biggest_hit nested, survival nested ──
        "tankScorecard": [
            {"name": "Tanky", "dtps": 1234.5, "taken": 5_000_000, "hps_recv": 3000.0,
             "deaths": 0, "phys_pct": 70.0, "magic_pct": 30.0, "crush_count": 2,
             "crit_count": 0, "avoid_pct": 35.5, "vs_replacement": 42.0,
             "biggest_hit": {"amount": 9001, "ability": "Crushing Blow", "boss": "Hydross"},
             "cooldowns": {"Shield Wall": 3, "Last Stand": 1},
             "survival": {"score": 88, "flags": ["uncrittable"]},
             "per_boss": [{"boss": "Hydross the Unstable", "dtps": 1500.0,
                           "taken": 4_000_000, "seconds": 300.0}]},
        ],
        # ── dispels: removed list -> {aura: n} JSON ──
        "dispels": [
            {"name": "Healz", "role": "Healer", "class": "Priest", "cleanse": 7, "purge": 0,
             "total": 7, "removed": [{"aura": "Poison", "n": 4}, {"aura": "Curse", "n": 3}],
             "targets": {"Tanky": 5}},
        ],
        # ── saves: targets is a nested dict -> JSON ──
        "saves": [
            {"name": "Tanky", "role": "Tank", "class": "Warrior", "save": 2, "utility": 1,
             "total": 3, "targets": {"Hand of Protection": {"Marvels": 2}}},
        ],
        # ── consumables: badges JSON + compliance columns ──
        "consumables": [
            {"name": "Marvels", "role": "Caster", "score": 9.0, "badges": ["tryhard", "flasked"],
             "suboptimal": [], "flask": True, "food": True, "weapon": True,
             "combat_pot": "Haste Potion", "alt_pot": "Dark Rune"},
        ],
        # a couple of small sections so deaths/luck/boss_times also round-trip
        "deaths": [{"name": "Marvels", "role": "Caster", "total": 1, "trash": 0}],
        "luckKPI": [{"name": "Marvels", "role": "Caster", "actual": 25.0,
                     "expected": 20.0, "luck": 5.0}],
        "boss_times": {"Hydross the Unstable": 300.0, "The Lurker Below": 360.0},
        "boss_meta": {"Hydross the Unstable": {"encounter_id": 100623}},
    }


class TestWriteWeekRoundTrip(unittest.TestCase):
    def _write(self, dbp, wd):
        """Write while swallowing the ✓/⚠ stdout so test output stays clean."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            db_writer.write_week(wd, dbp)
        return buf.getvalue()

    # ── CASE 1: write -> read -> assert each table's values match the input ──
    def test_roundtrip_values_match_input(self):
        wd = _week_data()
        rc = wd["meta"]["report_code"]
        with temp_db() as dbp:
            out = self._write(dbp, wd)
            self.assertIn("DB written", out)   # the write actually committed
            con = sqlite3.connect(dbp)
            con.row_factory = sqlite3.Row
            try:
                # weeks
                w = con.execute("SELECT * FROM weeks WHERE report_code=?", (rc,)).fetchone()
                self.assertEqual(w["date"], "Jun 08, 2026")
                self.assertEqual(w["zone"], "Serpentshrine Cavern")
                self.assertEqual(w["kills"], 3)
                self.assertEqual(w["start_ms"], 1780000000000)
                self.assertEqual(w["hs_used"], 14)
                self.assertEqual(w["hs_died_no_stone"], 2)
                self.assertEqual(w["schema_version"], db_writer.SCHEMA_VERSION)

                # roster
                r = con.execute("SELECT * FROM roster WHERE report_code=? AND player='Tanky'",
                                (rc,)).fetchone()
                self.assertEqual(r["class"], "Warrior")
                self.assertEqual(r["spec"], "Protection")
                self.assertEqual(r["role"], "Tank")

                # healing — CRITICAL: the column is eff_hps, NOT hps (documented gotcha).
                h = con.execute("SELECT eff_hps, eff_heal, overheal_pct, vs_replacement, top_spell "
                                "FROM healing WHERE report_code=? AND player='Healz'",
                                (rc,)).fetchone()
                self.assertEqual(h["eff_hps"], 4321)
                self.assertEqual(h["eff_heal"], 3_000_000)
                self.assertAlmostEqual(h["overheal_pct"], 22.5)
                self.assertAlmostEqual(h["vs_replacement"], 1.15)
                self.assertEqual(h["top_spell"], "Greater Heal")
                # the per-spell child row too
                hs = con.execute("SELECT casts, eff, per_cast FROM healing_spells "
                                 "WHERE report_code=? AND player='Healz' AND spell='Greater Heal'",
                                 (rc,)).fetchone()
                self.assertEqual(hs["casts"], 120)
                self.assertEqual(hs["eff"], 2_000_000)
                self.assertEqual(hs["per_cast"], 16666)

                # dps — boss selection: dps = boss.total / durations.boss, uptime = active/1000/dur*100
                d = con.execute("SELECT * FROM dps WHERE report_code=? AND player='Marvels'",
                                (rc,)).fetchone()
                self.assertEqual(d["role"], "Caster")
                self.assertEqual(d["total"], 6_000_000)            # boss total
                self.assertAlmostEqual(d["dps"], round(6_000_000 / 600.0, 2))   # 10000.0
                self.assertAlmostEqual(d["uptime"], round(540_000 / 1000 / 600.0 * 100, 1))  # 90.0
                self.assertAlmostEqual(d["all_dps"], round(9_000_000 / 1000.0, 2))   # 9000.0
                self.assertAlmostEqual(d["trash_dps"], round(3_000_000 / 400.0, 2))  # 7500.0
                self.assertEqual(d["war"], 87.0)                   # vs_replacement -> war
                # pct_raid = boss total / sum(all boss totals) * 100  (6M / 6.1M)
                self.assertAlmostEqual(d["pct_raid"], round(6_000_000 / 6_100_000 * 100, 2))
                # an unranked player stores war=NULL (None), not 0
                d2 = con.execute("SELECT war FROM dps WHERE report_code=? AND player='Healz'",
                                 (rc,)).fetchone()
                self.assertIsNone(d2["war"])

                # class_toolkit — only rows with a non-None num are written
                tk = con.execute("SELECT label, num FROM class_toolkit "
                                 "WHERE report_code=? AND player='Marvels'", (rc,)).fetchone()
                self.assertEqual(tk["label"], "Curse of Elements uptime")
                self.assertAlmostEqual(tk["num"], 95.0)
                # Healz had num=None -> no toolkit row
                self.assertIsNone(con.execute("SELECT 1 FROM class_toolkit "
                                              "WHERE report_code=? AND player='Healz'",
                                              (rc,)).fetchone())

                # tank_scorecard — biggest_hit flattened to amount, survival -> score
                t = con.execute("SELECT * FROM tank_scorecard WHERE report_code=? AND player='Tanky'",
                                (rc,)).fetchone()
                self.assertAlmostEqual(t["dtps"], 1234.5)
                self.assertEqual(t["taken"], 5_000_000)
                self.assertEqual(t["crush_count"], 2)
                self.assertEqual(t["crit_count"], 0)
                self.assertEqual(t["biggest_hit"], 9001)          # bh["amount"]
                self.assertEqual(t["war"], 42.0)                  # vs_replacement
                self.assertEqual(t["survival"], 88)               # survival["score"]
                # per-boss child row
                pb = con.execute("SELECT dtps, taken, seconds FROM tank_boss_dtps "
                                 "WHERE report_code=? AND player='Tanky'", (rc,)).fetchone()
                self.assertAlmostEqual(pb["dtps"], 1500.0)
                self.assertEqual(pb["taken"], 4_000_000)

                # dispels
                dp = con.execute("SELECT cleanse, purge, total FROM dispels "
                                 "WHERE report_code=? AND player='Healz'", (rc,)).fetchone()
                self.assertEqual(dp["cleanse"], 7)
                self.assertEqual(dp["purge"], 0)
                self.assertEqual(dp["total"], 7)

                # deaths / luck / boss_times spot-checks
                dt = con.execute("SELECT total, trash FROM deaths WHERE report_code=? AND player='Marvels'",
                                 (rc,)).fetchone()
                self.assertEqual((dt["total"], dt["trash"]), (1, 0))
                lk = con.execute("SELECT actual, expected, luck FROM luck_kpi "
                                 "WHERE report_code=? AND player='Marvels'", (rc,)).fetchone()
                self.assertEqual((lk["actual"], lk["expected"], lk["luck"]), (25.0, 20.0, 5.0))
                bt = con.execute("SELECT seconds, encounter_id FROM boss_times "
                                 "WHERE report_code=? AND boss='Hydross the Unstable'", (rc,)).fetchone()
                self.assertAlmostEqual(bt["seconds"], 300.0)
                self.assertEqual(bt["encounter_id"], 100623)
            finally:
                con.close()

    # ── CASE 2: idempotency — same report_code twice => row counts unchanged ──
    def test_idempotent_rewrite_keeps_row_counts(self):
        wd = _week_data()
        rc = wd["meta"]["report_code"]
        tables = ["weeks", "roster", "healing", "healing_spells", "dps",
                  "tank_scorecard", "tank_boss_dtps", "dispels", "saves",
                  "consumables", "deaths", "boss_times"]
        with temp_db() as dbp:
            self._write(dbp, wd)
            con = sqlite3.connect(dbp)
            try:
                first = {tbl: con.execute(
                    f"SELECT COUNT(*) FROM {tbl} WHERE report_code=?", (rc,)
                ).fetchone()[0] for tbl in tables}
            finally:
                con.close()

            self._write(dbp, wd)   # write the SAME report_code a second time
            con = sqlite3.connect(dbp)
            try:
                second = {tbl: con.execute(
                    f"SELECT COUNT(*) FROM {tbl} WHERE report_code=?", (rc,)
                ).fetchone()[0] for tbl in tables}
            finally:
                con.close()

        self.assertEqual(first, second)
        # sanity: the rows actually exist (not 0==0)
        self.assertEqual(second["weeks"], 1)
        self.assertEqual(second["roster"], 3)
        self.assertEqual(second["dps"], 2)
        self.assertEqual(second["tank_scorecard"], 1)

    # ── CASE 2b: a re-run that SHRINKS the row set leaves NO orphans (the T4-warmup-exclusion gap:
    # INSERT OR REPLACE overwrites matching keys but never deletes vanished ones) ──
    def test_rewrite_with_fewer_rows_flushes_orphans(self):
        wd = _week_data()
        rc = wd["meta"]["report_code"]
        wd["roster"]["Altswap"] = {"class": "Mage", "spec": "Fire", "role": "Caster"}  # an alt-swap player
        with temp_db() as dbp:
            self._write(dbp, wd)   # first write: 4 roster, 2 boss_times
            con = sqlite3.connect(dbp)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM roster WHERE report_code=?",
                                         (rc,)).fetchone()[0], 4)
            con.close()
            # the clean re-run drops the alt-swap player AND a (warmup) boss
            wd2 = _week_data()                                       # roster back to 3 (no Altswap)
            wd2["boss_times"] = {"Hydross the Unstable": 300.0}      # 2 -> 1 boss
            self._write(dbp, wd2)
            con = sqlite3.connect(dbp)
            try:
                roster = {p for (p,) in con.execute(
                    "SELECT player FROM roster WHERE report_code=?", (rc,))}
                bosses = {b for (b,) in con.execute(
                    "SELECT boss FROM boss_times WHERE report_code=?", (rc,))}
            finally:
                con.close()
        self.assertNotIn("Altswap", roster)                          # alt orphan flushed
        self.assertEqual(roster, {"Marvels", "Healz", "Tanky"})
        self.assertEqual(bosses, {"Hydross the Unstable"})           # dropped-boss orphan flushed

    # ── CASE 3: JSON columns survive dumps->loads back to the original Python object ──
    def test_json_columns_roundtrip(self):
        wd = _week_data()
        rc = wd["meta"]["report_code"]
        with temp_db() as dbp:
            self._write(dbp, wd)
            con = sqlite3.connect(dbp)
            try:
                # tank_scorecard.cooldowns
                cd_raw = con.execute("SELECT cooldowns FROM tank_scorecard "
                                     "WHERE report_code=? AND player='Tanky'", (rc,)).fetchone()[0]
                self.assertEqual(json.loads(cd_raw), {"Shield Wall": 3, "Last Stand": 1})

                # consumables.badges
                bd_raw = con.execute("SELECT badges FROM consumables "
                                     "WHERE report_code=? AND player='Marvels'", (rc,)).fetchone()[0]
                self.assertEqual(json.loads(bd_raw), ["tryhard", "flasked"])

                # saves.targets (nested dict)
                tg_raw = con.execute("SELECT targets FROM saves "
                                     "WHERE report_code=? AND player='Tanky'", (rc,)).fetchone()[0]
                self.assertEqual(json.loads(tg_raw),
                                 {"Hand of Protection": {"Marvels": 2}})

                # dispels.removed — list-of-{aura,n} is reshaped into a {aura: n} dict
                rm_raw = con.execute("SELECT removed FROM dispels "
                                     "WHERE report_code=? AND player='Healz'", (rc,)).fetchone()[0]
                self.assertEqual(json.loads(rm_raw), {"Poison": 4, "Curse": 3})
            finally:
                con.close()

    # ── determinism: identical input -> byte-identical row tuples on re-write ──
    def test_deterministic_output(self):
        wd = _week_data()
        rc = wd["meta"]["report_code"]

        def _dump(dbp):
            self._write(dbp, wd)
            con = sqlite3.connect(dbp)
            try:
                snap = {}
                for tbl in ("weeks", "roster", "healing", "dps", "tank_scorecard",
                            "dispels", "saves", "consumables"):
                    rows = con.execute(
                        f"SELECT * FROM {tbl} WHERE report_code=? ORDER BY 1,2", (rc,)
                    ).fetchall()
                    snap[tbl] = rows
                return snap
            finally:
                con.close()

        with temp_db() as a, temp_db() as b:
            self.assertEqual(_dump(a), _dump(b))


if __name__ == "__main__":
    unittest.main(verbosity=2)
