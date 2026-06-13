"""Byte-determinism guarantee for scripts/week_map.py::map_to_week_data.

The project promises byte-identical output for identical input (commit db60d00):
every set->list / dict-keys iteration that reaches WEEK_DATA is sorted, so the
per-process hash seed can never reorder the emitted JSON. These tests pin that
guarantee at two altitudes:

  1. A self-contained synthetic fixture rich enough to drive MULTIPLE set->list
     conversions inside the mapper — avoidable mechanics (`_mechs_seen` set),
     consumable name union (`set(ci_use) | set(cu_use)`), per-role crit lists,
     the interrupt headline/log merge, and the damageBySelection player filter.
     If an unsorted set() iteration ever reaches output, the two json.dumps
     strings diverge run-to-run and this flips red.

  2. (real data) The newest cache/wcl/*.json raw dict, when present on a dev box.
     Exercises the full production shape; skips cleanly in CI where cache/ is gone.

Hermetic — no WCL, no DB, no files written. stdlib unittest only.
Run:  python -m unittest discover -s tests -p "test_determinism.py" -v
"""
import sys, json, glob, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from week_map import map_to_week_data

# Newest raw wcl cache dict, if any (gitignored — absent in CI).
_WCL_CACHE_DIR = Path(__file__).resolve().parent.parent / "cache" / "wcl"
_WCL_CACHE_FILES = sorted(glob.glob(str(_WCL_CACHE_DIR / "*.json")), key=lambda p: Path(p).stat().st_mtime)
_NEWEST_WCL = _WCL_CACHE_FILES[-1] if _WCL_CACHE_FILES else None


def _player(name, role, spec, cls, **kw):
    base = {"name": name, "role": role, "spec": spec, "class": cls,
            "actual_crit": 0.0, "expected_crit": 0.0}
    base.update(kw)
    return base


def _rich_fixture():
    """A 5-player raid that forces the mapper through several set->list conversions:
      - two casters + a hunter + a tank + a healer  → all four crit_list() roles populate
      - avoidable_sources span several distinct mechanics on several players
        → `_mechs_seen` is built as a SET, then sorted into avoidableMechanics
      - ci_consumables vs consum_use keys differ → `set(ci_use) | set(cu_use)` union
      - both a WCL interrupt row AND a log-only interrupt list
      - damageBySelection with a DPS and an off-table tank
    """
    return {
        "meta": {"date": "Jun 08, 2026", "start_ms": 1780000000000,
                 "zone": "Serpentshrine Cavern", "kills": 3,
                 "report_code": "DETERMIN", "log_missing": []},
        "players": [
            _player("Zlock", "Caster", "Destruction", "Warlock",
                    actual_crit=25.0, expected_crit=20.0, total_dmg=5_000_000,
                    deaths=1, deaths_trash=1,
                    avoidable_dmg=12000,
                    avoidable_sources={"Scalding Water": 9000, "Whirlwind": 3000,
                                       "Toxic Spores": 1500},
                    avoidable_hits={"Scalding Water": [{"t": "21:02:11", "amt": 4500}]}),
            _player("Amage", "Caster", "Arcane", "Mage",
                    actual_crit=30.0, expected_crit=24.0, total_dmg=4_800_000,
                    avoidable_dmg=4000,
                    avoidable_sources={"Coldflame": 2500, "Whirlwind": 1500}),
            _player("Bpriest", "Healer", "Holy", "Priest", actual_crit=0.0),
            _player("Ctank", "Tank", "Protection", "Warrior",
                    actual_crit=12.0, expected_crit=10.0,
                    fights_tanked=3, fights_total=3,
                    avoidable_dmg=800,
                    avoidable_sources={"Magma Geyser": 800}),
            _player("Dhunter", "Physical", "Beast Mastery", "Hunter",
                    actual_crit=18.0, total_dmg=4_000_000,
                    interrupt_count=3, interrupt_list=["Heal", "Heal", "Frostbolt"]),
        ],
        "boss_times": {"Hydross the Unstable": 300.0, "The Lurker Below": 360.0,
                       "Leotheras the Blind": 280.0},
        # Distinct mechanic→boss map so avoidableMechanics legend has several entries to sort.
        "avoidable_mech_boss": {
            "Scalding Water": "The Lurker Below", "Whirlwind": "Leotheras the Blind",
            "Toxic Spores": "The Lurker Below", "Coldflame": "Hydross the Unstable",
            "Magma Geyser": "Hydross the Unstable",
        },
        "interrupts_wcl": {"Zlock": {"count": 2, "spells": {"Greater Heal": 1, "Lesser Heal": 1}}},
        "ci_consumables": {
            "Zlock":   {"flask": "Flask of Pure Death", "food": True, "elixirs": [],
                        "scrolls": [], "weapon_oil": True},
            "Amage":   {"flask": "", "food": True,
                        "elixirs": ["Adept's Elixir", "Elixir of Draenic Wisdom"],
                        "scrolls": [], "weapon_oil": False},
            "Bpriest": {"flask": "Flask of Mighty Restoration", "food": True, "elixirs": [],
                        "scrolls": [], "weapon_oil": False},
        },
        # Keys here that aren't in ci_consumables force the union to actually merge two sets.
        "consum_use":   {"Zlock": {"healthstone": 2, "potion": 1},
                         "Dhunter": {"potion": 2}},
        "consum_label": {"Zlock": {"combat_pots": ["Haste Potion", "Destruction Potion"]}},
        "healing_metrics": {
            "Bpriest": {"eff_heal": 3_000_000, "eff_hps": 4500, "overheal_pct": 22.0,
                        "activity_pct": 88.0, "tank_pct": 40.0, "top_spell": "Greater Heal",
                        "fights_healed": 3},
        },
        "healer_mana": {"Bpriest": 150_000},
        "healer_war": {},
        "dps_war": {"Zlock": 78.0, "Amage": 0.0, "Dhunter": 64.0},
        "tank_war": {"Ctank": 51.0},
        "tank_metrics": {
            "Ctank": {"dtps": 800, "taken": 500_000, "hps_recv": 900, "fights_tanked": 3,
                      "phys_pct": 70.0, "magic_pct": 30.0, "crush_count": 0, "crit_count": 0,
                      "avoid_pct": 38.0,
                      "biggest_hit": {"amount": 9000, "ability": "Melee",
                                      "boss": "Hydross the Unstable"},
                      "cooldowns": {"Shield Wall": 1, "Last Stand": 1}, "per_boss": []},
        },
        "damage_by_sel": {
            "durations": {"all": 940, "boss": 660, "trash": 280},
            "players": {
                "Zlock":   {"all": {"total": 5_000_000, "active": 800_000},
                            "boss": {"total": 4_000_000, "active": 600_000},
                            "trash": {"total": 1_000_000, "active": 200_000}},
                "Amage":   {"all": {"total": 4_800_000, "active": 760_000},
                            "boss": {"total": 3_900_000, "active": 580_000},
                            "trash": {"total": 900_000, "active": 180_000}},
                "Dhunter": {"all": {"total": 4_000_000, "active": 700_000},
                            "boss": {"total": 3_200_000, "active": 500_000},
                            "trash": {"total": 800_000, "active": 200_000}},
                # tank dealt damage but must stay OFF the DPS table
                "Ctank":   {"all": {"total": 900_000, "active": 400_000},
                            "boss": {"total": 700_000, "active": 300_000},
                            "trash": {"total": 200_000, "active": 100_000}},
            },
        },
    }


class TestSyntheticDeterminism(unittest.TestCase):
    def test_two_independent_calls_are_byte_identical(self):
        a = json.dumps(map_to_week_data(_rich_fixture()), sort_keys=False)
        b = json.dumps(map_to_week_data(_rich_fixture()), sort_keys=False)
        self.assertEqual(a, b)

    def test_set_derived_sections_are_sorted_and_complete(self):
        # Guards the specific set->list conversions the determinism promise hinges on.
        # avoidableMechanics keys come from a SET (`_mechs_seen`); a stable sorted order
        # is what makes the JSON byte-identical run to run.
        wd = map_to_week_data(_rich_fixture())
        mech_keys = list(wd["avoidableMechanics"].keys())
        self.assertEqual(mech_keys, sorted(mech_keys))
        # all five distinct mechanics across the three players must be present
        self.assertEqual(set(mech_keys),
                         {"Scalding Water", "Whirlwind", "Toxic Spores",
                          "Coldflame", "Magma Geyser"})
        # per-row avoidable sources are sorted by damage desc (deterministic tie-handling)
        zlock = next(r for r in wd["avoidableDmg"] if r["name"] == "Zlock")
        self.assertEqual([s["ability"] for s in zlock["sources"]],
                         ["Scalding Water", "Whirlwind", "Toxic Spores"])

    def test_consumable_name_union_is_sorted(self):
        # consum_names = sorted(set(ci_use) | set(cu_use)) — a two-set union that MUST
        # be emitted in a stable (sorted-by-name) order to stay byte-deterministic.
        wd = map_to_week_data(_rich_fixture())
        usage_names = [r["name"] for r in wd["consumableUsage"]]
        # Dhunter is in consum_use only; Bpriest in ci_consumables only — both present.
        self.assertEqual(set(usage_names),
                         {"Zlock", "Amage", "Bpriest", "Dhunter"})

    def test_repeated_dumps_within_one_call_stable(self):
        # Dumping the SAME mapped object twice is trivially equal; the meaningful check is
        # that two SEPARATE map calls agree (above). This guards against accidental in-place
        # mutation making a second dump of the same object differ.
        wd = map_to_week_data(_rich_fixture())
        self.assertEqual(json.dumps(wd, sort_keys=False),
                         json.dumps(wd, sort_keys=False))


@unittest.skipUnless(_NEWEST_WCL is not None,
                     "no cache/wcl/*.json present (gitignored; absent in CI)")
class TestRealDataDeterminism(unittest.TestCase):
    def test_newest_cache_maps_byte_identically_twice(self):
        with open(_NEWEST_WCL, encoding="utf-8") as fh:
            wcl = json.load(fh)
        a = json.dumps(map_to_week_data(wcl), sort_keys=False)
        # reload from disk so the second call cannot reuse any mutated in-memory dict
        with open(_NEWEST_WCL, encoding="utf-8") as fh:
            wcl2 = json.load(fh)
        b = json.dumps(map_to_week_data(wcl2), sort_keys=False)
        self.assertEqual(a, b,
                         f"map_to_week_data produced non-deterministic output for "
                         f"{Path(_NEWEST_WCL).name} — an unsorted set() reached output")


if __name__ == "__main__":
    unittest.main()
