"""Value-level tests for scripts/week_map.py::merge_log_into_wcl.

merge_log_into_wcl overlays the combat-log dict onto the wcl dict IN PLACE and reaches
crit_model.expected_crit to backfill tank/healer gear crit (WCL emits 0 for those roles).
These tests pin the two load-bearing behaviors:

  1. Backfill GAP-FILL guard — a Tank with gear_crit_rating==0 gets the log's _crit_melee
     value, expected_crit recomputed via the real model, and luck_delta set; a DPS player
     who ALREADY had a nonzero gear_crit_rating is left exactly untouched (no overwrite).
  2. WCL-Durability — an empty/missing log THINS (no overlay) but never BLANKS: the wcl
     players keep their original WCL crit values.

The expected backfilled value is computed by calling the real expected_crit(), so the
assertion tracks the model rather than hardcoding a number.

Hermetic — no WCL, no DB, no files.

Run:  python -m unittest discover -s tests -p "test_merge_log.py" -v
"""
import sys, json, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from week_map import merge_log_into_wcl
from crit_model import expected_crit


def _wcl_player(name, role, spec, cls, **kw):
    """A minimal wcl-side player dict carrying the fields merge_log_into_wcl reads."""
    base = {"name": name, "role": role, "spec": spec, "class": cls,
            "gear_crit_rating": 0, "actual_crit": 0.0, "expected_crit": 0.0,
            "intellect": 0}
    base.update(kw)
    return base


class TestMergeBackfill(unittest.TestCase):
    """Case 1: tank gap-fill fires; an already-rated DPS player is untouched."""

    def _build(self):
        wcl = {"players": [
            # Tank: WCL gave gear_crit_rating==0 (the role WCL zeroes) → eligible for backfill.
            _wcl_player("Tanky", "Tank", "Protection", "Warrior",
                        actual_crit=14.0, expected_crit=0.0),
            # DPS: WCL already supplied a nonzero gear crit → must NOT be overwritten,
            # even though the log carries a (different) _crit_melee value.
            _wcl_player("Rogan", "Physical", "Combat", "Rogue",
                        gear_crit_rating=250, actual_crit=30.0, expected_crit=27.5),
        ]}
        log = {"players": {
            "Tanky": {"_crit_melee": 132, "_crit_spell": 0, "_agility": 480},
            "Rogan": {"_crit_melee": 999, "_crit_spell": 0, "_agility": 700},
        }}
        return wcl, log

    def test_tank_gear_crit_backfilled_from_log_melee(self):
        wcl, log = self._build()
        merge_log_into_wcl(wcl, log)
        tank = next(p for p in wcl["players"] if p["name"] == "Tanky")
        # Tank role pulls the MELEE crit school from the log.
        self.assertEqual(tank["gear_crit_rating"], 132)

    def test_tank_expected_crit_matches_real_model(self):
        wcl, log = self._build()
        merge_log_into_wcl(wcl, log)
        tank = next(p for p in wcl["players"] if p["name"] == "Tanky")
        # Recompute via the real model: Protection spec → agility branch, intellect ignored.
        want = expected_crit("Warrior", "Protection", 132, 480, 0)
        self.assertEqual(tank["expected_crit"], want)

    def test_tank_luck_delta_is_actual_minus_expected(self):
        wcl, log = self._build()
        merge_log_into_wcl(wcl, log)
        tank = next(p for p in wcl["players"] if p["name"] == "Tanky")
        want = round(14.0 - tank["expected_crit"], 2)
        self.assertEqual(tank["luck_delta"], want)

    def test_dps_with_nonzero_gear_crit_is_unchanged(self):
        """The gap-fill GUARD: a DPS player WCL already rated keeps its exact originals.
        Breaking the guard (overwriting nonzero gear crit) flips these assertions red."""
        wcl, log = self._build()
        merge_log_into_wcl(wcl, log)
        rogue = next(p for p in wcl["players"] if p["name"] == "Rogan")
        # gear_crit_rating untouched (NOT replaced by the log's 999), expected_crit untouched.
        self.assertEqual(rogue["gear_crit_rating"], 250)
        self.assertEqual(rogue["expected_crit"], 27.5)
        # No luck_delta written for the untouched player (the backfill branch never ran).
        self.assertNotIn("luck_delta", rogue)

    def test_returns_same_dict_object_mutated_in_place(self):
        wcl, log = self._build()
        out = merge_log_into_wcl(wcl, log)
        self.assertIs(out, wcl)

    def test_backfill_is_deterministic(self):
        """Same input → identical output bytes (project byte-determinism guarantee)."""
        wcl_a, log_a = self._build()
        wcl_b, log_b = self._build()
        merge_log_into_wcl(wcl_a, log_a)
        merge_log_into_wcl(wcl_b, log_b)
        self.assertEqual(json.dumps(wcl_a, sort_keys=True, default=str),
                         json.dumps(wcl_b, sort_keys=True, default=str))


class TestMergeNoLog(unittest.TestCase):
    """Case 2: an empty log THINS, never BLANKS — WCL crit values survive intact."""

    def _wcl(self):
        return {"players": [
            _wcl_player("Wlock", "Caster", "Destruction", "Warlock",
                        gear_crit_rating=300, actual_crit=25.0, expected_crit=22.5,
                        intellect=400),
            _wcl_player("Tanky", "Tank", "Protection", "Warrior",
                        gear_crit_rating=0, actual_crit=14.0, expected_crit=11.0),
        ]}

    def test_empty_log_leaves_wcl_crit_values_intact(self):
        wcl = self._wcl()
        merge_log_into_wcl(wcl, {})
        wlock = next(p for p in wcl["players"] if p["name"] == "Wlock")
        tank = next(p for p in wcl["players"] if p["name"] == "Tanky")
        # No log player → the `if not log: continue` path → no overlay, originals preserved.
        self.assertEqual(wlock["gear_crit_rating"], 300)
        self.assertEqual(wlock["expected_crit"], 22.5)
        self.assertEqual(wlock["actual_crit"], 25.0)
        # The tank had gear_crit_rating==0 but with NO log there is nothing to backfill —
        # it must stay 0, not get invented.
        self.assertEqual(tank["gear_crit_rating"], 0)
        self.assertEqual(tank["expected_crit"], 11.0)

    def test_empty_log_writes_no_luck_delta(self):
        wcl = self._wcl()
        merge_log_into_wcl(wcl, {})
        for p in wcl["players"]:
            self.assertNotIn("luck_delta", p)

    def test_missing_players_key_in_log_is_safe(self):
        """log_data.get('players', {}) → a log dict with no 'players' key thins, never errors."""
        wcl = self._wcl()
        out = merge_log_into_wcl(wcl, {"drums": [], "friendly_fire": []})
        wlock = next(p for p in out["players"] if p["name"] == "Wlock")
        self.assertEqual(wlock["gear_crit_rating"], 300)

    def test_empty_log_sets_toplevel_defaults_not_blanks(self):
        """A missing log → top-level log keys default to empty containers (card hides),
        which is the acceptable 'thins' outcome — not a crash, not stale WCL data."""
        wcl = self._wcl()
        merge_log_into_wcl(wcl, {})
        self.assertEqual(wcl["friendly_fire"], [])
        self.assertEqual(wcl["mc_saves"], [])
        self.assertEqual(wcl["mc_liable"], [])
        self.assertEqual(wcl["consum_use"], {})


class TestMergeHealerSchool(unittest.TestCase):
    """Healer role pulls the SPELL crit school (mirrors the Caster branch)."""

    def test_healer_backfills_from_crit_spell_not_melee(self):
        wcl = {"players": [
            _wcl_player("Healz", "Healer", "Holy", "Priest",
                        gear_crit_rating=0, actual_crit=20.0, expected_crit=0.0,
                        intellect=500),
        ]}
        log = {"players": {
            # melee value is high but must be IGNORED for a healer; spell value is used.
            "Healz": {"_crit_melee": 800, "_crit_spell": 175, "_agility": 0},
        }}
        merge_log_into_wcl(wcl, log)
        healer = wcl["players"][0]
        self.assertEqual(healer["gear_crit_rating"], 175)
        # Holy is a HEALER_SPEC → intellect branch in expected_crit; agility ignored.
        want = expected_crit("Priest", "Holy", 175, 0, 500)
        self.assertEqual(healer["expected_crit"], want)


if __name__ == "__main__":
    unittest.main()
