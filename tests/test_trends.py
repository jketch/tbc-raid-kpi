"""Value-level tests for scripts/trends.py — enrich_with_trends, the week-over-week delta_*
enricher.

These tests seed a TEMP-FILE SQLite history DB via db_writer.write_week() (which creates the
schema and the prior-week rows), then call enrich_with_trends(current_week, tmp_db) and pin the
exact delta_* values it attaches. Hermetic — no WCL, no prod DB, no HTML.

Key behaviors covered:
  * Prior week is the most recent week STRICTLY BEFORE this one by CHRONOLOGICAL start_ms
    (NOT insert order, NOT the global-latest report) — the c6fbda1 ordering bug.
  * The DPS parse-% delta uses `is not None` on BOTH sides, so a real 0th-percentile baseline
    still deltas (the falsy-zero regression guard).
  * No prior week / missing DB / missing table all degrade silently (never raises).
  * A NULL-start_ms legacy row is a LOWER-priority fallback (anchored priors win).
  * Per-section delta math (dps / healing / sunderArmor / dispels) against hand values.

Run:  python -m unittest discover -s tests -p "test_trends.py" -v
"""
import sys, json, sqlite3, tempfile, shutil, contextlib, io, unittest
from pathlib import Path

# Match the pipeline: reconfigure stdout to UTF-8 so the ✓/⚠ glyphs db_writer/trends print
# don't crash the cp1252 Windows console under unittest (which doesn't import main()).
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import db_writer
from trends import enrich_with_trends


@contextlib.contextmanager
def temp_db():
    """A temp DB path with Windows-tolerant cleanup (sqlite WAL can briefly hold the file)."""
    d = tempfile.mkdtemp()
    try:
        yield Path(d) / "trends.db"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _seed(dbp, week):
    """Write one week into the temp DB, swallowing the ✓ print."""
    with contextlib.redirect_stdout(io.StringIO()):
        db_writer.write_week(week, dbp)


# ── fixture builders ────────────────────────────────────────────────────────────
# Each builder produces a MAPPED-shaped week_data dict whose sections land in exactly the
# DB columns the delta we want to test reads from. We keep them minimal but shaped so
# write_week persists the columns: damageBySelection -> dps (dps/total/uptime + war),
# healing -> healing.eff_hps, sunderArmor -> sunder_armor, dispels -> dispels.

def _dsel(players_spec, durs=None):
    """damageBySelection block. players_spec: {name: {boss_total, boss_active, war}}.
    durations default so boss-DPS math is clean (boss=100s)."""
    durs = durs or {"all": 100, "boss": 100, "trash": 100}
    players = []
    for nm, s in players_spec.items():
        bt = s.get("boss_total", 0)
        ba = s.get("boss_active", 0)
        players.append({
            "name": nm, "role": s.get("role", "Caster"),
            "vs_replacement": s.get("war"),
            "all":   {"total": bt, "active": ba},
            "boss":  {"total": bt, "active": ba},
            "trash": {"total": 0, "active": 0},
        })
    return {"durations": durs, "players": players}


def _week(report, start_ms, *, dsel=None, healing=None, sunder=None, dispels=None,
          deaths=None, avoid=None, drums=None,
          date=None, kills=1, zone="Serpentshrine Cavern"):
    wd = {
        "meta": {"report_code": report, "start_ms": start_ms,
                 "date": date or f"d{start_ms}", "kills": kills, "zone": zone,
                 "log_missing": []},
        "roster": {},
    }
    if dsel is not None:
        wd["damageBySelection"] = dsel
    if healing is not None:
        wd["healing"] = healing
    if sunder is not None:
        wd["sunderArmor"] = {"players": sunder}
    if dispels is not None:
        wd["dispels"] = dispels
    if deaths is not None:
        wd["deaths"] = deaths
    if avoid is not None:
        wd["avoidableDmg"] = avoid
    if drums is not None:
        wd["drums"] = drums
    return wd


# ── tests ────────────────────────────────────────────────────────────────────────

class TestChronologicalPrior(unittest.TestCase):
    """Case 1: the prior week is chosen by start_ms < this week, NOT insert order, NOT the
    global-latest report (the c6fbda1 bug)."""

    def test_delta_anchors_to_immediate_predecessor_not_global_latest(self):
        with temp_db() as dbp:
            # W1 oldest (boss_total 100000 -> 1000 boss-dps), W3 newest (300000 -> 3000 dps).
            # Insert OUT of chronological order: W3 first, then W1, to prove the SQL sorts by
            # start_ms and does not lean on insert order.
            w1 = _week("W1", 1000, dsel=_dsel({"Marv": {"boss_total": 100000, "boss_active": 0,
                                                        "war": 40.0, "role": "Caster"}}))
            w3 = _week("W3", 3000, dsel=_dsel({"Marv": {"boss_total": 300000, "boss_active": 0,
                                                        "war": 90.0, "role": "Caster"}}))
            _seed(dbp, w3)
            _seed(dbp, w1)

            # Enrich W2 (start_ms 2000, between W1 and W3). boss_total 250000 -> 2500 boss-dps.
            w2 = _week("W2", 2000, dsel=_dsel({"Marv": {"boss_total": 250000, "boss_active": 0,
                                                        "war": 70.0, "role": "Caster"}}))
            out = enrich_with_trends(w2, dbp)

            row = out["damageBySelection"]["players"][0]
            # delta vs W1 (1000 dps) => 2500-1000 = +1500.0  (vs W3 it would be 2500-3000=-500)
            self.assertEqual(row["boss"]["delta_dps"], 1500.0)
            # parse-% delta vs W1's 40.0 => 70-40 = +30.0 (vs W3's 90 it would be -20)
            self.assertEqual(row["delta_vs_replacement"], 30.0)
            # prior-week header context points at W1, not the global-latest W3.
            self.assertEqual(out["prev"]["report_code"], "W1")


class TestZeroParseBaseline(unittest.TestCase):
    """Case 2: the `is not None` falsy-zero fix — a prior dps.war of exactly 0.0 still deltas."""

    def test_zero_percentile_baseline_is_not_skipped(self):
        with temp_db() as dbp:
            prior = _week("P", 1000, dsel=_dsel({"Z": {"boss_total": 50000, "boss_active": 0,
                                                       "war": 0.0, "role": "Caster"}}))
            _seed(dbp, prior)
            # Confirm the baseline really persisted as 0.0 (not NULL) — the precondition for the guard.
            con = sqlite3.connect(dbp)
            stored = con.execute("SELECT war FROM dps WHERE report_code='P' AND player='Z'").fetchone()[0]
            con.close()
            self.assertEqual(stored, 0.0)

            cur = _week("C", 2000, dsel=_dsel({"Z": {"boss_total": 50000, "boss_active": 0,
                                                     "war": 5.0, "role": "Caster"}}))
            out = enrich_with_trends(cur, dbp)
            row = out["damageBySelection"]["players"][0]
            # 5.0 - 0.0 = 5.0 — the real 0 baseline is NOT treated as "unranked"/skipped.
            # (Reverting `is not None` to a truthy `if pw and ...` would drop this key → KeyError.)
            self.assertEqual(row["delta_vs_replacement"], 5.0)


class TestNoPriorWeek(unittest.TestCase):
    """Case 3: a single week in the DB → no delta_* attached, no crash, prev not set."""

    def test_single_week_yields_no_deltas(self):
        with temp_db() as dbp:
            only = _week("ONLY", 5000, dsel=_dsel({"Solo": {"boss_total": 100000, "boss_active": 0,
                                                            "war": 50.0, "role": "Caster"}}),
                         healing=[{"name": "Solo", "role": "Healer", "eff_hps": 4000}])
            _seed(dbp, only)
            # Enrich the SAME week (its own report is excluded by `report_code != ?`).
            out = enrich_with_trends(only, dbp)
            row = out["damageBySelection"]["players"][0]
            self.assertNotIn("delta_dps", row.get("boss", {}))
            self.assertNotIn("delta_vs_replacement", row)
            self.assertNotIn("delta_hps", out["healing"][0])
            self.assertNotIn("prev", out)


class TestSilentDegradation(unittest.TestCase):
    """Case 4: missing DB file (and a DB missing the tables) returns the input unchanged."""

    def test_missing_db_file_returns_input_unchanged(self):
        nonexistent = Path(tempfile.gettempdir()) / "definitely_not_a_db_xyz123.sqlite"
        if nonexistent.exists():
            nonexistent.unlink()
        cur = _week("C", 2000, dsel=_dsel({"Z": {"boss_total": 1, "boss_active": 0, "war": 5.0}}))
        before = json.dumps(cur, sort_keys=True)
        out = enrich_with_trends(cur, str(nonexistent))
        self.assertIs(out, cur)
        self.assertEqual(json.dumps(out, sort_keys=True), before)   # untouched

    def test_db_missing_weeks_table_does_not_raise(self):
        # An empty/garbage sqlite file (no `weeks` table) must degrade, not throw.
        with temp_db() as dbp:
            con = sqlite3.connect(dbp)
            con.execute("CREATE TABLE unrelated (x INTEGER)")
            con.commit()
            con.close()
            cur = _week("C", 2000, dsel=_dsel({"Z": {"boss_total": 1, "boss_active": 0, "war": 5.0}}))
            out = enrich_with_trends(cur, dbp)        # must not raise
            self.assertNotIn("prev", out)
            self.assertNotIn("delta_vs_replacement", out["damageBySelection"]["players"][0])


class TestNullStartMsFallback(unittest.TestCase):
    """Case 5: a legacy prior row with start_ms IS NULL is a LOWER-priority fallback —
    used when no anchored prior exists, but an anchored prior is PREFERRED when both exist."""

    def test_null_start_ms_used_only_as_fallback(self):
        with temp_db() as dbp:
            legacy = _week("LEG", 1000, dsel=_dsel({"M": {"boss_total": 100000, "boss_active": 0,
                                                          "war": 10.0, "role": "Caster"}}))
            _seed(dbp, legacy)
            # Null out the legacy row's start_ms after write (simulates a pre-migration row).
            con = sqlite3.connect(dbp)
            con.execute("UPDATE weeks SET start_ms=NULL WHERE report_code='LEG'")
            con.commit()
            con.close()

            # Current week has NO same-start anchored prior in the DB → the NULL row is used.
            cur = _week("CUR", 2000, dsel=_dsel({"M": {"boss_total": 300000, "boss_active": 0,
                                                       "war": 30.0, "role": "Caster"}}))
            out = enrich_with_trends(cur, dbp)
            self.assertEqual(out["prev"]["report_code"], "LEG")          # fallback chosen
            row = out["damageBySelection"]["players"][0]
            self.assertEqual(row["boss"]["delta_dps"], 2000.0)           # 3000 - 1000
            self.assertEqual(row["delta_vs_replacement"], 20.0)          # 30 - 10

    def test_anchored_prior_preferred_over_null_row(self):
        with temp_db() as dbp:
            legacy = _week("LEG", 1000, dsel=_dsel({"M": {"boss_total": 999999, "boss_active": 0,
                                                          "war": 99.0, "role": "Caster"}}))
            anchored = _week("ANCH", 1500, dsel=_dsel({"M": {"boss_total": 100000, "boss_active": 0,
                                                             "war": 10.0, "role": "Caster"}}))
            _seed(dbp, legacy)
            _seed(dbp, anchored)
            con = sqlite3.connect(dbp)
            con.execute("UPDATE weeks SET start_ms=NULL WHERE report_code='LEG'")
            con.commit()
            con.close()

            cur = _week("CUR", 2000, dsel=_dsel({"M": {"boss_total": 300000, "boss_active": 0,
                                                       "war": 30.0, "role": "Caster"}}))
            out = enrich_with_trends(cur, dbp)
            # ANCH (start_ms 1500 < 2000) is preferred over the NULL-start LEG row.
            self.assertEqual(out["prev"]["report_code"], "ANCH")
            row = out["damageBySelection"]["players"][0]
            self.assertEqual(row["boss"]["delta_dps"], 2000.0)           # 3000 - 1000 (vs ANCH)
            self.assertEqual(row["delta_vs_replacement"], 20.0)          # 30 - 10 (vs ANCH)


class TestPerSectionDeltaMath(unittest.TestCase):
    """Case 6: spot-check one delta each for dps / healing / sunderArmor / dispels against
    hand-computed values."""

    def _seed_prior_and_enrich(self, current):
        """Helper: seed a fixed prior week, enrich `current`, return it."""
        dbp = self.dbp
        prior = _week(
            "PRIOR", 1000,
            dsel=_dsel({"Mage": {"boss_total": 200000, "boss_active": 50000, "war": 60.0,
                                 "role": "Caster"}}),
            healing=[{"name": "Pri", "role": "Healer", "eff_hps": 5000,
                      "overheal_pct": 20.0, "activity_pct": 80.0, "mana_eff": 12.0}],
            sunder=[{"name": "War", "total": 100, "effective": 60, "refreshed": 40}],
            dispels=[{"name": "Sham", "role": "Caster", "class": "Shaman",
                      "cleanse": 5, "purge": 2, "total": 7, "removed": []}],
        )
        _seed(dbp, prior)
        return enrich_with_trends(current, dbp)

    def setUp(self):
        self._ctx = temp_db()
        self.dbp = self._ctx.__enter__()

    def tearDown(self):
        self._ctx.__exit__(None, None, None)

    def test_dps_boss_delta(self):
        # prior boss-dps = 200000/100 = 2000. current boss_total 250000 -> 2500. delta +500.0.
        # prior uptime = 50000/1000/100*100 = 50.0%. current active 80000 -> 80.0%. delta +30.0.
        cur = _week("CUR", 2000, dsel=_dsel(
            {"Mage": {"boss_total": 250000, "boss_active": 80000, "war": 75.0, "role": "Caster"}}))
        out = self._seed_prior_and_enrich(cur)
        b = out["damageBySelection"]["players"][0]["boss"]
        self.assertEqual(b["delta_dps"], 500.0)
        self.assertEqual(b["delta_uptime"], 30.0)
        self.assertEqual(b["delta_total"], 50000.0)                     # 250000 - 200000
        self.assertEqual(out["damageBySelection"]["players"][0]["delta_vs_replacement"], 15.0)

    def test_healing_delta(self):
        # prior eff_hps 5000 -> current 6200 => delta_hps +1200.0. overheal 20->15 => -5.0.
        cur = _week("CUR", 2000, healing=[{"name": "Pri", "role": "Healer", "eff_hps": 6200,
                                           "overheal_pct": 15.0, "activity_pct": 88.0,
                                           "mana_eff": 14.5}])
        out = self._seed_prior_and_enrich(cur)
        h = out["healing"][0]
        self.assertEqual(h["delta_hps"], 1200.0)
        self.assertEqual(h["delta_overheal"], -5.0)
        self.assertEqual(h["delta_activity"], 8.0)                     # 88 - 80
        self.assertEqual(h["delta_hp_per_mana"], 2.5)                  # 14.5 - 12.0

    def test_sunder_delta(self):
        # prior total 100 / effective 60 -> current 130 / 70 => +30 / +10.
        cur = _week("CUR", 2000,
                    sunder=[{"name": "War", "total": 130, "effective": 70, "refreshed": 60}])
        out = self._seed_prior_and_enrich(cur)
        p = out["sunderArmor"]["players"][0]
        self.assertEqual(p["delta_total"], 30)
        self.assertEqual(p["delta_effective"], 10)

    def test_dispels_delta(self):
        # prior total 7 -> current 11 => delta_total +4.0 (single-field KPI loop, rounded).
        cur = _week("CUR", 2000,
                    dispels=[{"name": "Sham", "role": "Caster", "class": "Shaman",
                              "cleanse": 8, "purge": 3, "total": 11, "removed": []}])
        out = self._seed_prior_and_enrich(cur)
        self.assertEqual(out["dispels"][0]["delta_total"], 4.0)


class TestPrevTotals(unittest.TestCase):
    """prev.totals — raid-level prior sums for the aggregate stat-tile deltas.

    Regression for the Jul-13 deaths tile (+65 shown on a true +90): the tile used to SUM the
    per-player delta_deaths fields, which drops churn — a raider who died this week but has no
    prior-week deaths row carries no delta, and last week's dead who sat out this week aren't
    in this week's array at all. trends.py now injects prev.totals so the tile can compute
    Σ(this week) − Σ(prev week) directly."""

    def test_totals_reflect_full_prior_sums_despite_churn(self):
        with temp_db() as dbp:
            # Prior week: A died 2, B died 6 (Σ=8) — B sits out the current week entirely.
            prior = _week("PRIOR", 1000,
                          deaths=[{"name": "A", "role": "Caster", "total": 2, "trash": 1},
                                  {"name": "B", "role": "Healer", "total": 6, "trash": 0}],
                          avoid=[{"name": "A", "role": "Caster", "dmg": 40000}],
                          drums=[{"name": "L", "casts": 5, "total": 10, "buffs": 30,
                                  "buffs_per_drum": 3.0, "score": 30}])
            _seed(dbp, prior)

            # Current week: A died 1, C (new — no prior row) died 3 (Σ=4). True delta = 4−8 = −4;
            # the per-player delta sum would be A's −1 only (C has no delta, B isn't here).
            cur = _week("CUR", 2000,
                        deaths=[{"name": "A", "role": "Caster", "total": 1, "trash": 0},
                                {"name": "C", "role": "Physical", "total": 3, "trash": 2}])
            out = enrich_with_trends(cur, dbp)

            self.assertEqual(out["prev"]["totals"]["deaths"], 8)
            self.assertEqual(out["prev"]["totals"]["avoidable"], 40000)
            self.assertEqual(out["prev"]["totals"]["drums"], 10)
            # Per-player deltas still only land on matched players (A), proving why the
            # aggregate must NOT be built from them.
            self.assertEqual(out["deaths"][0]["delta_deaths"], -1.0)     # A: 1 − 2
            self.assertNotIn("delta_deaths", out["deaths"][1])           # C: no prior row

    def test_sections_absent_last_week_are_omitted_from_totals(self):
        with temp_db() as dbp:
            # Prior week has deaths but NO avoidable/drums rows → SUM is NULL → key omitted
            # (can't distinguish "no data" from a true zero, so no delta is the safe degrade).
            _seed(dbp, _week("PRIOR", 1000,
                             deaths=[{"name": "A", "role": "Caster", "total": 5, "trash": 0}]))
            out = enrich_with_trends(_week("CUR", 2000, deaths=[]), dbp)
            self.assertEqual(out["prev"]["totals"], {"deaths": 5})


class TestNeverRaises(unittest.TestCase):
    """The contract: enrich_with_trends NEVER raises — any DB error returns the input."""

    def test_returns_same_object(self):
        with temp_db() as dbp:
            _seed(dbp, _week("A", 1000, dsel=_dsel({"X": {"boss_total": 10, "boss_active": 0,
                                                          "war": 1.0}})))
            cur = _week("B", 2000, dsel=_dsel({"X": {"boss_total": 20, "boss_active": 0,
                                                     "war": 2.0}}))
            out = enrich_with_trends(cur, dbp)
            self.assertIs(out, cur)            # mutates + returns the SAME dict


class TestDeterminism(unittest.TestCase):
    """Identical input + identical DB state → byte-identical enriched output."""

    def test_same_input_same_bytes(self):
        with temp_db() as dbp:
            _seed(dbp, _week("PRIOR", 1000,
                             dsel=_dsel({"M": {"boss_total": 200000, "boss_active": 40000,
                                               "war": 50.0, "role": "Caster"}}),
                             healing=[{"name": "H", "role": "Healer", "eff_hps": 4000,
                                       "overheal_pct": 10.0, "activity_pct": 70.0, "mana_eff": 9.0}]))

            def run():
                cur = _week("CUR", 2000,
                            dsel=_dsel({"M": {"boss_total": 250000, "boss_active": 60000,
                                              "war": 65.0, "role": "Caster"}}),
                            healing=[{"name": "H", "role": "Healer", "eff_hps": 4800,
                                      "overheal_pct": 8.0, "activity_pct": 75.0, "mana_eff": 10.0}])
                return json.dumps(enrich_with_trends(cur, dbp), sort_keys=True)

            self.assertEqual(run(), run())


if __name__ == "__main__":
    unittest.main(verbosity=2)
