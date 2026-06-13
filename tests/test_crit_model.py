"""Characterization tests for crit_model.expected_crit — the PURE TBC expected-crit model.

expected_crit(player_type, spec, crit_rating, agility=0, intellect=0) returns
  round(CLASS_BASE_CRIT[type] + SPEC_TALENT_CRIT[spec] + crit_rating/CRIT_RATING_PER_PCT + stat, 2)
where `stat` is intellect/INT_PER_CRIT[type] for CASTER_SPECS|HEALER_SPECS specs, else
agility/AGI_PER_CRIT[type]. Every expected number below is HAND-COMPUTED from the constant
tables (CLASS_BASE_CRIT, SPEC_TALENT_CRIT, CRIT_RATING_PER_PCT, AGI_PER_CRIT, INT_PER_CRIT,
CASTER_SPECS, HEALER_SPECS) — so a typo in any one of those constants flips a case red.

Scope: the pure model ONLY. gear_crit_rating / fetch_item_crit / load_cache / save_cache touch
the item cache / WCL network and are deliberately untouched here (this stays hermetic).

crit_rating is passed as a clean multiple of CRIT_RATING_PER_PCT (22.08) so the gear term lands
on a whole number; the model divides plainly, so floats are fine and keep the arithmetic legible.
"""
import sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from crit_model import (
    expected_crit,
    CRIT_RATING_PER_PCT,
    CLASS_BASE_CRIT,
    SPEC_TALENT_CRIT,
    AGI_PER_CRIT,
    INT_PER_CRIT,
)
from roles import CASTER_SPECS, HEALER_SPECS


class TestExpectedCrit(unittest.TestCase):
    # ── sanity-check the constants the cases lean on (so a table typo is named, not mysterious)
    def test_constants_unchanged(self):
        self.assertEqual(CRIT_RATING_PER_PCT, 22.08)
        self.assertEqual(CLASS_BASE_CRIT["Rogue"], 5.0)
        self.assertEqual(CLASS_BASE_CRIT["Mage"], 3.29)
        self.assertEqual(CLASS_BASE_CRIT["Priest"], 3.29)
        self.assertEqual(CLASS_BASE_CRIT["Warrior"], 3.0)
        self.assertEqual(SPEC_TALENT_CRIT["Fire"], 6.0)
        self.assertEqual(SPEC_TALENT_CRIT["Holy"], 3.0)
        self.assertEqual(SPEC_TALENT_CRIT["Arms"], 5.0)
        self.assertEqual(AGI_PER_CRIT["Rogue"], 29.0)
        self.assertEqual(INT_PER_CRIT["Mage"], 59.5)
        self.assertEqual(INT_PER_CRIT["Priest"], 59.2)
        # Path routing the cases depend on.
        self.assertNotIn("Assassination", CASTER_SPECS)
        self.assertNotIn("Assassination", HEALER_SPECS)
        self.assertNotIn("Arms", CASTER_SPECS)
        self.assertNotIn("Arms", HEALER_SPECS)
        self.assertIn("Fire", CASTER_SPECS)
        self.assertIn("Holy", HEALER_SPECS)

    def test_physical_agility_path_rogue_assassination(self):
        # Rogue base 5.0; Assassination is NOT a key in SPEC_TALENT_CRIT -> talent default 3.0;
        # gear = 220.8 / 22.08 = 10.0; agility path (not caster/healer): 290 / 29.0 (Rogue) = 10.0.
        # 5.0 + 3.0 + 10.0 + 10.0 = 28.0
        self.assertNotIn("Assassination", SPEC_TALENT_CRIT)  # confirms the talent-default branch
        self.assertEqual(
            expected_crit("Rogue", "Assassination", 220.8, agility=290), 28.0
        )

    def test_caster_intellect_path_mage_fire(self):
        # Mage base 3.29; Fire talent 6.0; gear = 110.4 / 22.08 = 5.0;
        # Fire is in CASTER_SPECS -> intellect path: 595 / 59.5 (Mage) = 10.0.
        # 3.29 + 6.0 + 5.0 + 10.0 = 24.29
        self.assertEqual(
            expected_crit("Mage", "Fire", 110.4, intellect=595), 24.29
        )

    def test_healer_intellect_path_priest_holy(self):
        # Priest base 3.29; Holy talent 3.0; gear = 44.16 / 22.08 = 2.0;
        # Holy is in HEALER_SPECS -> intellect path: 296 / 59.2 (Priest) = 5.0.
        # 3.29 + 3.0 + 2.0 + 5.0 = 13.29
        self.assertEqual(
            expected_crit("Priest", "Holy", 44.16, intellect=296), 13.29
        )

    def test_pure_base_plus_talent_no_rating_no_stat(self):
        # crit_rating=0, agility=0, intellect=0 -> gear term and stat term both 0.
        # Warrior base 3.0 + Arms talent 5.0 = 8.0 exactly.
        self.assertEqual(expected_crit("Warrior", "Arms", 0), 8.0)

    def test_agility_path_ignores_intellect(self):
        # Same Rogue/Assassination physical spec: passing intellect must NOT contribute
        # (only the agility branch fires). With agility=0 the stat term is 0 regardless of int.
        # base 5.0 + talent 3.0 + gear (220.8/22.08=10.0) + 0 = 18.0
        self.assertEqual(
            expected_crit("Rogue", "Assassination", 220.8, intellect=9999), 18.0
        )

    def test_unknown_class_and_spec_fall_back_to_defaults(self):
        # Unknown type -> CLASS_BASE_CRIT default 3.0; unknown spec -> SPEC_TALENT_CRIT default 3.0;
        # unknown spec is in neither CASTER nor HEALER set -> agility path, agility=0 -> stat 0.
        # gear = 22.08 / 22.08 = 1.0. 3.0 + 3.0 + 1.0 + 0 = 7.0
        self.assertEqual(
            expected_crit("Nonexistent", "Nonexistent", 22.08), 7.0
        )

    def test_deterministic_same_input_same_output(self):
        # The project guarantees byte-identical output for identical input.
        a = expected_crit("Mage", "Fire", 110.4, intellect=595)
        b = expected_crit("Mage", "Fire", 110.4, intellect=595)
        self.assertEqual(a, b)
        self.assertIsInstance(a, float)


if __name__ == "__main__":
    unittest.main()
