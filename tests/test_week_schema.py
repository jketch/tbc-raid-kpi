"""Contract tests for scripts/week_schema.py — hermetic (no real snapshots, no WCL, no DB).

Run:  python -m unittest discover -s tests      (or: python tests/test_week_schema.py)
pytest auto-discovers these unittest.TestCase classes too, if it's ever installed.
"""
import sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import week_schema as ws


def rich_week():
    """A minimal but complete mapped WEEK_DATA: every contract section populated, on a killed week."""
    wk = {"meta": {"date": "Jun 08, 2026", "kills": 10, "report_code": "TEST", "start_ms": 1}}
    for key, s in ws.SECTIONS.items():
        if s.tier == ws.META:
            continue
        if s.predicate is ws._has_players:
            wk[key] = {"players": [{"name": "X"}]}
        elif s.predicate is ws._has_batteries:
            wk[key] = {"batteries": [{"label": "VT"}], "innervate": {"casters": []}}
        elif s.predicate is ws._hs_nonempty:
            wk[key] = {"total_used": 3, "died_total": 1, "died_no_stone": 0}
        elif key in ("roster", "boss_times", "boss_meta", "roleSpells", "playerSpells",
                     "avoidableMechanics", "healReaction", "debuffCoverage"):
            wk[key] = {"_": 1}            # dict-shaped sections
        else:
            wk[key] = [{"name": "X"}]     # list-shaped sections
    return wk


class TestPredicates(unittest.TestCase):
    def test_damage_by_selection_checks_players_not_dict_truthiness(self):
        # always a dict with durations — emptiness lives in .players
        self.assertFalse(ws.is_populated({"damageBySelection": {"durations": {"all": 9}, "players": []}},
                                         "damageBySelection"))
        self.assertTrue(ws.is_populated({"damageBySelection": {"players": [{"name": "A"}]}},
                                        "damageBySelection"))

    def test_healthstone_nonempty_on_deaths_even_with_zero_used(self):
        self.assertTrue(ws.is_populated({"healthstoneStats": {"total_used": 0, "died_total": 2}},
                                        "healthstoneStats"))
        self.assertFalse(ws.is_populated({"healthstoneStats": {"total_used": 0, "died_total": 0}},
                                         "healthstoneStats"))

    def test_loot_and_sunder_use_players(self):
        self.assertFalse(ws.is_populated({"loot": {}}, "loot"))
        self.assertTrue(ws.is_populated({"loot": {"players": [{"name": "A"}], "total": 1}}, "loot"))
        self.assertFalse(ws.is_populated({"sunderArmor": {"players": []}}, "sunderArmor"))

    def test_list_sections_use_truthiness(self):
        self.assertFalse(ws.is_populated({"healing": []}, "healing"))
        self.assertTrue(ws.is_populated({"healing": [{"name": "A"}]}, "healing"))


class TestValidate(unittest.TestCase):
    def test_rich_week_has_no_warnings(self):
        rep = ws.validate(rich_week(), has_log=True, has_loot=True)
        self.assertEqual(rep["warn"], [], f"unexpected warnings: {rep['warn']}")

    def test_emptied_wcl_section_warns_on_killed_week(self):
        wk = rich_week()
        wk["damageBySelection"] = {"durations": {}, "players": []}   # the live DPS source, blanked
        rep = ws.validate(wk, has_log=True)
        self.assertTrue(any("damageBySelection" in w for w in rep["warn"]), rep)

    def test_log_section_empty_is_info_not_warn(self):
        wk = rich_week()
        wk["drums"] = []
        rep = ws.validate(wk, has_log=False)
        self.assertFalse(any("drums" in w for w in rep["warn"]))
        self.assertTrue(any("drums" in i for i in rep["info"]))

    def test_optional_wcl_empties_are_not_warnings(self):
        # deaths (flawless week), healerCrit (no gear crit), sunderArmor (no warriors) → INFO only
        wk = rich_week()
        wk["deaths"] = []
        wk["healerCrit"] = []
        wk["sunderArmor"] = {"players": []}
        rep = ws.validate(wk, has_log=True)
        self.assertEqual(rep["warn"], [], rep["warn"])

    def test_loot_empty_is_info_not_warn(self):
        wk = rich_week()
        wk["loot"] = {}
        rep = ws.validate(wk, has_loot=False)
        self.assertFalse(any("loot" in w for w in rep["warn"]))

    def test_zero_kill_week_never_warns(self):
        wk = {"meta": {"kills": 0}}   # nothing populated, but no kills → no WCL expectations
        rep = ws.validate(wk)
        self.assertEqual(rep["warn"], [])

    def test_never_raises_on_garbage(self):
        for junk in (None, {}, {"meta": None}, {"meta": {"kills": "x"}}):
            ws.validate(junk)   # must not raise


class TestRegression(unittest.TestCase):
    def test_detects_dropped_section(self):
        old = rich_week()
        new = rich_week()
        new["loot"] = {}                                  # loot dropped (the real incident)
        new["tankScorecard"] = []
        self.assertEqual(ws.regression(old, new), ["loot", "tankScorecard"])

    def test_no_regression_when_equal(self):
        self.assertEqual(ws.regression(rich_week(), rich_week()), [])

    def test_added_section_is_not_a_regression(self):
        old = rich_week()
        new = rich_week()
        old["sunderArmor"] = {"players": []}              # old lacked it, new has it
        self.assertEqual(ws.regression(old, new), [])


class TestFieldCoverage(unittest.TestCase):
    """coverage() / coverage_regression() — the field-level guard for a key going all-null inside a
    still-populated section (WCL parse % blanked by a reprocess from pre-ranking snapshots)."""

    @staticmethod
    def _wk(dps, tank, heal):
        """A week with `dps`/`tank`/`heal` items each carrying a non-null vs_replacement."""
        mk = lambda n: [{"name": f"P{i}", "vs_replacement": 70} for i in range(n)]
        return {"meta": {"report_code": "R"},
                "damageBySelection": {"players": mk(dps)},
                "tankScorecard": mk(tank),
                "healing": mk(heal)}

    def test_coverage_counts_non_null(self):
        c = ws.coverage(self._wk(3, 2, 5))
        self.assertEqual((c["dps parse %"], c["tank parse %"], c["healer parse %"]), (3, 2, 5))

    def test_total_collapse_is_flagged(self):
        # the real incident: dps + tank parse blanked to all-null, healer survives
        old, new = self._wk(19, 3, 5), self._wk(0, 0, 5)
        self.assertEqual(ws.coverage_regression(old, new), ["dps parse %", "tank parse %"])

    def test_partial_drop_does_not_trip(self):
        # roster churn (19 → 17 parsed) is NOT a collapse — only old>0 → new==0 fires
        self.assertEqual(ws.coverage_regression(self._wk(19, 3, 5), self._wk(17, 3, 5)), [])

    def test_first_deploy_with_nothing_ranked_is_not_a_regression(self):
        self.assertEqual(ws.coverage_regression(self._wk(0, 0, 0), self._wk(0, 0, 0)), [])

    def test_gaining_coverage_is_not_a_regression(self):
        self.assertEqual(ws.coverage_regression(self._wk(0, 0, 5), self._wk(19, 3, 5)), [])

    def test_never_raises_on_garbage(self):
        for junk in (None, {}, {"damageBySelection": None}, {"healing": "x"}):
            ws.coverage(junk)
            ws.coverage_regression(junk, junk)


class TestManifestCoverage(unittest.TestCase):
    def test_every_section_has_a_tier_and_desc(self):
        for key, s in ws.SECTIONS.items():
            self.assertIn(s.tier, (ws.WCL, ws.LOG, ws.EXTERNAL, ws.META), key)
            self.assertTrue(s.desc, f"{key} missing description")

    def test_populated_sections_excludes_meta(self):
        self.assertNotIn("meta", ws.populated_sections(rich_week()))


class TestTypedContract(unittest.TestCase):
    """WeekData (the TypedDict) must stay locked to the SECTIONS registry — identical top-level keys.
    A failure means a section was added/renamed in ONE but not the other; fix BOTH in week_schema.py."""

    def test_weekdata_keys_match_sections(self):
        self.assertEqual(
            set(ws.WeekData.__annotations__),
            set(ws.SECTIONS),
            "WeekData TypedDict and the SECTIONS registry have drifted — add/rename the key in BOTH.",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
