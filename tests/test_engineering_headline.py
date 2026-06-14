"""Engineering WCL-durability headline — _engineering_rows in week_map.

Engineering used to be combat-log ONLY, so the section blanked entirely on any week whose
180MB log didn't get passed around. fetch_engineering_casts now provides a WCL Casts-events
headline (counts only); _engineering_rows merges it under the log overlay so the section is
WCL-durable: log wins when present (it carries real sapper/bomb damage), WCL backfills the
count when the log is absent. These tests pin that preference + the byte-deterministic sort.
Hermetic — no WCL, no DB, no files.

Run:  python -m unittest discover -s tests
"""
import sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from week_map import _engineering_rows


def _p(name, role="Physical", **kw):
    base = {"name": name, "role": role}
    base.update(kw)
    return base


class TestEngineeringRows(unittest.TestCase):
    def test_log_present_wins_and_carries_damage(self):
        players = [_p("Boomy", eng={"Super Sapper": 2}, eng_dmg=18000)]
        # WCL also saw casts, but the log overlay is authoritative when present.
        rows = _engineering_rows(players, {"Boomy": {"Super Sapper": 5}})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["eng"], {"Super Sapper": 2})   # log count, not WCL's 5
        self.assertEqual(rows[0]["dmg"], 18000)

    def test_no_log_backfills_from_wcl_with_zero_damage(self):
        players = [_p("Boomy")]                                  # no p["eng"] — log absent
        rows = _engineering_rows(players, {"Boomy": {"Goblin Sapper": 3}})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["eng"], {"Goblin Sapper": 3})  # WCL count
        self.assertEqual(rows[0]["dmg"], 0)                     # no log ⇒ no damage

    def test_neither_source_excludes_player(self):
        players = [_p("Healz", role="Healer")]
        self.assertEqual(_engineering_rows(players, {}), [])

    def test_log_ranks_above_wcl_only_by_damage(self):
        players = [_p("Wcl1"), _p("Logged", eng={"Fel Iron Bomb": 1}, eng_dmg=9000),
                   _p("Wcl2")]
        rows = _engineering_rows(players, {"Wcl1": {"Super Sapper": 1},
                                           "Wcl2": {"Super Sapper": 1}})
        self.assertEqual([r["name"] for r in rows], ["Logged", "Wcl1", "Wcl2"])  # dmg first, name tiebreak

    def test_wcl_only_week_is_byte_deterministic_by_name(self):
        # All dmg=0 → ties broken by name regardless of player iteration order.
        a = _engineering_rows([_p("Zeb"), _p("Abe")],
                              {"Zeb": {"Super Sapper": 1}, "Abe": {"Super Sapper": 1}})
        b = _engineering_rows([_p("Abe"), _p("Zeb")],
                              {"Abe": {"Super Sapper": 1}, "Zeb": {"Super Sapper": 1}})
        self.assertEqual([r["name"] for r in a], ["Abe", "Zeb"])
        self.assertEqual(a, b)

    def test_empty_eng_wcl_is_safe(self):
        rows = _engineering_rows([_p("Boomy", eng={"Super Sapper": 1}, eng_dmg=5000)], None)
        self.assertEqual(rows[0]["name"], "Boomy")


if __name__ == "__main__":
    unittest.main()
