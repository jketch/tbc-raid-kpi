"""Value tests for roles.py — the pure spec→role classification leaf that buckets every raider
across all KPIs. Covers _effective_role's <50%
off-role reclassification (tank/healer who ran DPS), the _nontank_role/_nonheal_role off-role maps,
and per-fight _fight_role (Prot→Tank, Feral-by-bucket, blank-spec→bucket fallback)."""
import sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from roles import _effective_role, _fight_role, _nonheal_role, _nontank_role


class TestEffectiveRole(unittest.TestCase):
    def test_tank_who_mostly_dps_is_reclassified(self):
        # Prot warrior tanked 1 of 4 fights → ran DPS most of the night → off-role (Physical).
        self.assertEqual(_effective_role("Tank", "Protection", 1, 0, 4), "Physical")

    def test_tank_who_mostly_tanked_keeps_tank(self):
        self.assertEqual(_effective_role("Tank", "Protection", 3, 0, 4), "Tank")
        self.assertEqual(_effective_role("Tank", "Protection", 2, 0, 4), "Tank")   # exactly 50% stays

    def test_healer_who_mostly_dps_is_reclassified(self):
        # Resto shaman/druid healed 1 of 4 → DPS most of the night → Caster.
        self.assertEqual(_effective_role("Healer", "Restoration", 0, 1, 4), "Caster")

    def test_healer_who_mostly_healed_keeps_healer(self):
        self.assertEqual(_effective_role("Healer", "Holy", 0, 4, 4), "Healer")

    def test_non_tank_healer_roles_never_reclassified(self):
        self.assertEqual(_effective_role("Physical", "Fury", 0, 0, 10), "Physical")
        self.assertEqual(_effective_role("Caster", "Shadow", 0, 0, 10), "Caster")

    def test_zero_fights_total_keeps_role_no_div_by_zero(self):
        self.assertEqual(_effective_role("Tank", "Protection", 0, 0, 0), "Tank")
        self.assertEqual(_effective_role("Healer", "Holy", 0, 0, 0), "Healer")


class TestOffRoleMaps(unittest.TestCase):
    def test_nontank_role(self):
        self.assertEqual(_nontank_role("Holy"), "Healer")        # healer spec
        self.assertEqual(_nontank_role("Shadow"), "Caster")      # caster spec
        self.assertEqual(_nontank_role("Fury"), "Physical")      # physical spec
        self.assertEqual(_nontank_role("Feral Combat"), "Physical")  # tank spec → melee when not tanking
        self.assertEqual(_nontank_role("Protection"), "Physical")
        self.assertEqual(_nontank_role(""), "Physical")          # unknown → physical fallback

    def test_nonheal_role(self):
        self.assertEqual(_nonheal_role("Fury"), "Physical")      # physical off-spec
        self.assertEqual(_nonheal_role("Shadow"), "Caster")      # caster off-spec
        self.assertEqual(_nonheal_role("Protection"), "Physical")  # tank spec → physical
        self.assertEqual(_nonheal_role("Restoration"), "Caster")   # resto shaman/druid → caster DPS
        self.assertEqual(_nonheal_role("Discipline"), "Caster")    # Holy/Disc → caster fallback


class TestFightRole(unittest.TestCase):
    def test_healer_specs_are_healer_any_bucket(self):
        for spec in ("Holy", "Discipline", "Restoration"):
            self.assertEqual(_fight_role(spec, "dps"), "Healer")

    def test_protection_is_tank_regardless_of_bucket(self):
        self.assertEqual(_fight_role("Protection", "dps"), "Tank")   # WCL mis-buckets prot into dps
        self.assertEqual(_fight_role("Protection", "tanks"), "Tank")

    def test_feral_combat_resolved_by_bucket(self):
        self.assertEqual(_fight_role("Feral Combat", "tanks"), "Tank")
        self.assertEqual(_fight_role("Feral Combat", "dps"), "dps")
        self.assertEqual(_fight_role("Feral Combat", "healers"), "dps")  # only the tanks bucket tanks

    def test_blank_spec_falls_back_to_bucket(self):
        self.assertEqual(_fight_role("", "tanks"), "Tank")
        self.assertEqual(_fight_role("", "healers"), "Healer")
        self.assertEqual(_fight_role("", "dps"), "dps")

    def test_caster_and_physical_dps_specs_are_dps(self):
        for spec in ("Arcane", "Fire", "Shadow", "Fury", "Retribution", "Marksmanship"):
            self.assertEqual(_fight_role(spec, "tanks"), "dps")  # spec wins over a wrong bucket


if __name__ == "__main__":
    unittest.main()
