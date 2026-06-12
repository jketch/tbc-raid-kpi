"""Mechanic Compliance (backlog #2) + the avoidable WCL fallback (#7) — hermetic map tests.

Feeds map_to_week_data a skeletal wcl dict (no WCL, no log) and asserts:
  1. mech_compliance reshapes into the WEEK_DATA.mechanicCompliance grid (sorted, iconized);
  2. with NO combat-log overlay, avoidableDmg/avoidableMechanics fall back to the WCL
     mechanic-compliance data (the durability principle: a missing log thins, never blanks);
  3. with a log overlay present, the log stays authoritative (fallback does NOT fire).

Run:  python -m unittest discover -s tests
"""
import sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import week_map


MECH = {
    "The Lurker Below": {
        "encounter_id": 100624,
        "mechanics": {
            "Whirl":  {"total": 9000, "events": 3,
                       "players": {"Aaa": {"hits": 2, "dmg": 6000}, "Bbb": {"hits": 1, "dmg": 3000}}},
            "Geyser": {"total": 4000, "events": 1,
                       "players": {"Aaa": {"hits": 1, "dmg": 4000}}},
        },
    },
}


def _wcl(players=None, mech=None):
    """Minimal pre-map dict: only the keys this test exercises; map defaults the rest."""
    return {"players": players or [], "mech_compliance": mech or {},
            "ability_icons": {"Whirl": "trade_engineering", "Geyser": "spell_frost_summonwaterelemental"}}


class TestMechanicComplianceEmit(unittest.TestCase):
    def test_grid_shape_sorted_and_iconized(self):
        wd = week_map.map_to_week_data(_wcl(mech=MECH))
        mc = wd["mechanicCompliance"]
        self.assertEqual(len(mc["bosses"]), 1)
        b = mc["bosses"][0]
        self.assertEqual(b["boss"], "The Lurker Below")
        self.assertEqual(b["encounter_id"], 100624)
        # mechanics sorted by total damage desc
        self.assertEqual([m["name"] for m in b["mechanics"]], ["Whirl", "Geyser"])
        whirl = b["mechanics"][0]
        self.assertEqual(whirl["players_hit"], 2)
        self.assertEqual([p["name"] for p in whirl["players"]], ["Aaa", "Bbb"])  # dmg desc
        # ICON_OVERRIDES beats the (misleading) WCL slug for Whirl
        self.assertEqual(whirl["icon"], "spell_nature_cyclone")

    def test_empty_without_data(self):
        self.assertEqual(week_map.map_to_week_data(_wcl())["mechanicCompliance"], {})


class TestAvoidableWclFallback(unittest.TestCase):
    def test_no_log_falls_back_to_wcl(self):
        players = [{"name": "Aaa", "role": "Caster", "class": "Mage", "spec": "Frost",
                    "actual_crit": 0, "expected_crit": 0}]
        wd = week_map.map_to_week_data(_wcl(players=players, mech=MECH))
        av = wd["avoidableDmg"]
        self.assertEqual([r["name"] for r in av], ["Aaa", "Bbb"])     # 10000 > 3000
        aaa = av[0]
        self.assertEqual(aaa["dmg"], 10000)
        self.assertEqual(aaa["role"], "Caster")                        # joined via roster
        self.assertEqual([s["ability"] for s in aaa["sources"]], ["Whirl", "Geyser"])  # dmg desc
        self.assertEqual(aaa["sources"][0], {"ability": "Whirl", "dmg": 6000, "hits": []})
        # legend rebuilt from the WCL map
        self.assertEqual(wd["avoidableMechanics"]["Whirl"]["boss"], "The Lurker Below")

    def test_log_overlay_stays_authoritative(self):
        players = [{"name": "Aaa", "role": "Caster", "class": "Mage", "spec": "Frost",
                    "actual_crit": 0, "expected_crit": 0,
                    "avoidable_dmg": 1234, "avoidable_sources": {"Spout": 1234}}]
        wd = week_map.map_to_week_data(_wcl(players=players, mech=MECH))
        self.assertEqual(len(wd["avoidableDmg"]), 1)
        self.assertEqual(wd["avoidableDmg"][0]["dmg"], 1234)           # log value, not 10000
        self.assertIn("Spout", wd["avoidableMechanics"])               # log legend, not WCL


if __name__ == "__main__":
    unittest.main()
