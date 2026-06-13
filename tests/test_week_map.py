"""Value-level tests for scripts/week_map.py — map_to_week_data, the biggest pure transform.

Until now the mapper was only covered indirectly (section-presence characterization + the
manual replay byte-diff). These fixture tests pin actual VALUES: role splits, sort orders,
the avoidable WCL fallback, the interrupt headline-vs-log preference, compliance scoring,
and the 0-vs-None parse-percentile distinction. Hermetic — no WCL, no DB, no files.

Run:  python -m unittest discover -s tests
"""
import sys, json, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from week_map import map_to_week_data, build_consumable_compliance


def _player(name, role, spec, cls, **kw):
    base = {"name": name, "role": role, "spec": spec, "class": cls,
            "actual_crit": 0.0, "expected_crit": 0.0}
    base.update(kw)
    return base


def _fixture():
    """A 4-player raid: a warlock DPS (log-rich), a holy priest, a prot warrior tank,
    and a hunter whose interrupt data exists ONLY in the combat log (no WCL row)."""
    return {
        "meta": {"date": "Jun 08, 2026", "start_ms": 1780000000000, "zone": "Serpentshrine Cavern",
                 "kills": 2, "report_code": "TESTCODE", "log_missing": []},
        "players": [
            _player("Wlock", "Caster", "Destruction", "Warlock",
                    actual_crit=25.0, expected_crit=20.0, total_dmg=5_000_000,
                    deaths=1, deaths_trash=1,
                    avoidable_dmg=12000,
                    avoidable_sources={"Scalding Water": 9000, "Whirlwind": 3000},
                    avoidable_hits={"Scalding Water": [{"t": "21:02:11", "amt": 4500}]}),
            _player("Healz", "Healer", "Holy", "Priest"),
            _player("Tanky", "Tank", "Protection", "Warrior",
                    actual_crit=12.0, expected_crit=10.0,
                    fights_tanked=2, fights_total=2),
            _player("Huntz", "Physical", "Beast Mastery", "Hunter",
                    actual_crit=18.0, total_dmg=4_000_000,
                    interrupt_count=3, interrupt_list=["Heal", "Heal", "Frostbolt"]),
        ],
        "boss_times": {"Hydross the Unstable": 300.0, "The Lurker Below": 360.0},
        "interrupts_wcl": {"Wlock": {"count": 2, "spells": {"Greater Heal": 2}}},
        "ci_consumables": {
            "Wlock": {"flask": "Flask of Pure Death", "food": True, "elixirs": [],
                      "scrolls": [], "weapon_oil": True},
            # no flask, but battle + guardian elixir = the 2-elixir path; no food
            "Healz": {"flask": "", "food": False,
                      "elixirs": ["Adept's Elixir", "Elixir of Draenic Wisdom"],
                      "scrolls": [], "weapon_oil": False},
        },
        "consum_use": {"Wlock": {"healthstone": 2, "potion": 1}},
        "consum_label": {"Wlock": {"combat_pots": ["Haste Potion"]}},
        "healing_metrics": {
            "Healz": {"eff_heal": 3_000_000, "eff_hps": 4500, "overheal_pct": 22.0,
                      "activity_pct": 88.0, "tank_pct": 40.0, "top_spell": "Greater Heal",
                      "fights_healed": 2},
        },
        "healer_mana": {"Healz": 150_000},
        "healer_war": {},                       # not ranked → vs_replacement None
        "dps_war": {"Wlock": 78.0, "Huntz": 0.0},   # 0.0 = ranked dead-last, NOT unranked
        "tank_war": {"Tanky": 51.0},
        "tank_metrics": {
            "Tanky": {"dtps": 800, "taken": 500_000, "hps_recv": 900, "fights_tanked": 2,
                      "phys_pct": 70.0, "magic_pct": 30.0, "crush_count": 0, "crit_count": 0,
                      "avoid_pct": 38.0, "biggest_hit": {"amount": 9000, "ability": "Melee",
                                                         "boss": "Hydross the Unstable"},
                      "cooldowns": {"Shield Wall": 1}, "per_boss": []},
        },
        "damage_by_sel": {
            "durations": {"all": 900, "boss": 660, "trash": 240},
            "players": {
                "Wlock": {"all": {"total": 5_000_000, "active": 800_000},
                          "boss": {"total": 4_000_000, "active": 600_000},
                          "trash": {"total": 1_000_000, "active": 200_000}},
                "Huntz": {"all": {"total": 4_000_000, "active": 700_000},
                          "boss": {"total": 3_200_000, "active": 500_000},
                          "trash": {"total": 800_000, "active": 200_000}},
                # the tank dealt damage but must NOT appear in the DPS table
                "Tanky": {"all": {"total": 900_000, "active": 400_000},
                          "boss": {"total": 700_000, "active": 300_000},
                          "trash": {"total": 200_000, "active": 100_000}},
            },
        },
        "expose_armor": {"players": [{"name": "Huntz", "uptime": 67.8, "applications": 52}]},
    }


def _fixture_no_log():
    """A log-less week: no per-player avoidable overlay, but mechanic compliance (pure WCL)
    is present — the avoidable card must be REBUILT from it, not blanked."""
    wcl = _fixture()
    for p in wcl["players"]:
        p.pop("avoidable_dmg", None)
        p.pop("avoidable_sources", None)
        p.pop("avoidable_hits", None)
    wcl["mech_compliance"] = {
        "The Lurker Below": {
            "encounter_id": 100624,
            "mechanics": {
                "Scalding Water": {"total": 15000, "events": 5,
                                   "players": {"Wlock": {"hits": 3, "dmg": 9000},
                                               "Huntz": {"hits": 2, "dmg": 6000}}},
            },
        },
    }
    return wcl


class TestRosterAndMeta(unittest.TestCase):
    def test_meta_passthrough(self):
        wd = map_to_week_data(_fixture())
        self.assertEqual(wd["meta"]["report_code"], "TESTCODE")
        self.assertEqual(wd["meta"]["kills"], 2)
        self.assertEqual(wd["meta"]["start_ms"], 1780000000000)

    def test_roster_identity(self):
        wd = map_to_week_data(_fixture())
        self.assertEqual(set(wd["roster"]), {"Wlock", "Healz", "Tanky", "Huntz"})
        self.assertEqual(wd["roster"]["Wlock"],
                         {"class": "Warlock", "spec": "Destruction", "role": "Caster"})


class TestCritAndDeaths(unittest.TestCase):
    def test_crit_lists_split_by_role_and_exclude_zero(self):
        wd = map_to_week_data(_fixture())
        self.assertEqual([c["name"] for c in wd["casterCrit"]], ["Wlock"])
        self.assertEqual([c["name"] for c in wd["physicalCrit"]], ["Huntz"])
        self.assertEqual([c["name"] for c in wd["tankCrit"]], ["Tanky"])
        self.assertEqual(wd["healerCrit"], [])   # Healz crit 0 → excluded, not 0-row

    def test_deaths_total_includes_trash_and_excludes_clean_players(self):
        wd = map_to_week_data(_fixture())
        self.assertEqual(len(wd["deaths"]), 1)
        d = wd["deaths"][0]
        self.assertEqual((d["name"], d["total"], d["trash"]), ("Wlock", 2, 1))


class TestAvoidable(unittest.TestCase):
    def test_log_path_sorted_with_per_hit_drilldown(self):
        wd = map_to_week_data(_fixture())
        self.assertEqual(len(wd["avoidableDmg"]), 1)
        row = wd["avoidableDmg"][0]
        self.assertEqual(row["name"], "Wlock")
        self.assertEqual(row["dmg"], 12000)
        # sources sorted by damage desc; the log's per-hit timestamps survive
        self.assertEqual([s["ability"] for s in row["sources"]],
                         ["Scalding Water", "Whirlwind"])
        self.assertEqual(len(row["sources"][0]["hits"]), 1)

    def test_wcl_fallback_rebuilds_card_when_log_missing(self):
        wd = map_to_week_data(_fixture_no_log())
        # WCL-Durability: the card is rebuilt from mechanicCompliance, not blanked
        self.assertEqual([(r["name"], r["dmg"]) for r in wd["avoidableDmg"]],
                         [("Wlock", 9000), ("Huntz", 6000)])
        # role/class joined from the roster; per-hit drilldown legitimately empty
        self.assertEqual(wd["avoidableDmg"][0]["role"], "Caster")
        self.assertEqual(wd["avoidableDmg"][0]["sources"][0]["hits"], [])
        self.assertEqual(wd["avoidableMechanics"]["Scalding Water"]["boss"],
                         "The Lurker Below")

    def test_mechanic_compliance_players_sorted_by_dmg(self):
        wd = map_to_week_data(_fixture_no_log())
        bosses = wd["mechanicCompliance"]["bosses"]
        self.assertEqual(len(bosses), 1)
        mech = bosses[0]["mechanics"][0]
        self.assertEqual(mech["players_hit"], 2)
        self.assertEqual([p["name"] for p in mech["players"]], ["Wlock", "Huntz"])


class TestInterrupts(unittest.TestCase):
    def test_wcl_headline_preferred_over_log(self):
        wd = map_to_week_data(_fixture())
        rows = {r["name"]: r for r in wd["interrupts"]}
        # Wlock has BOTH a WCL row (2) and would have had a log count — WCL wins
        self.assertEqual(rows["Wlock"]["count"], 2)
        self.assertEqual(rows["Wlock"]["spells"][0]["spell"], "Greater Heal")

    def test_log_fallback_when_wcl_has_no_row(self):
        wd = map_to_week_data(_fixture())
        rows = {r["name"]: r for r in wd["interrupts"]}
        self.assertEqual(rows["Huntz"]["count"], 3)
        # log list tallied: Heal ×2 first, then Frostbolt
        self.assertEqual([(s["spell"], s["n"]) for s in rows["Huntz"]["spells"]],
                         [("Heal", 2), ("Frostbolt", 1)])


class TestConsumables(unittest.TestCase):
    def test_flask_passes_and_two_elixirs_pass(self):
        wd = map_to_week_data(_fixture())
        grid = {r["name"]: r for r in wd["consumables"]}
        self.assertTrue(grid["Wlock"]["flask"])           # real flask
        self.assertTrue(grid["Healz"]["flask"])           # battle + guardian = equivalent
        self.assertFalse(grid["Healz"]["food"])

    def test_one_elixir_does_not_pass_the_flask_slot(self):
        rows = build_consumable_compliance([
            {"name": "X", "role": "Caster", "flask": "",
             "elixirs": ["Adept's Elixir"], "food": True}])
        self.assertFalse(rows[0]["flask"])    # battle-only — no guardian → not flask-equivalent

    def test_healthstone_stats(self):
        wd = map_to_week_data(_fixture())
        hs = wd["healthstoneStats"]
        self.assertEqual(hs["total_used"], 2)       # Wlock's two stones
        self.assertEqual(hs["died_total"], 1)       # only Wlock died
        self.assertEqual(hs["died_no_stone"], 0)    # ...and he did pop one


class TestDamageBySelection(unittest.TestCase):
    def test_filtered_to_effective_dps_only(self):
        wd = map_to_week_data(_fixture())
        names = [p["name"] for p in wd["damageBySelection"]["players"]]
        self.assertIn("Wlock", names)
        self.assertIn("Huntz", names)
        self.assertNotIn("Tanky", names)   # a tank who dealt damage stays off the DPS table

    def test_zero_parse_percentile_is_preserved_not_nulled(self):
        # 0.0 = ranked dead-last; None = unranked. The mapper must keep the distinction
        # (the `x or None` idiom would destroy it — the falsy-zero bug class).
        wd = map_to_week_data(_fixture())
        rows = {p["name"]: p for p in wd["damageBySelection"]["players"]}
        self.assertEqual(rows["Wlock"]["vs_replacement"], 78.0)
        self.assertEqual(rows["Huntz"]["vs_replacement"], 0.0)

    def test_tank_parse_and_survival(self):
        wd = map_to_week_data(_fixture())
        t = wd["tankScorecard"][0]
        self.assertEqual(t["name"], "Tanky")
        self.assertEqual(t["vs_replacement"], 51.0)
        # uncrittable, uncrushable, no deaths → a perfect survival grade
        self.assertEqual(t["survival"]["score"], 100)
        self.assertEqual(t["survival"]["flags"], [])


class TestHealing(unittest.TestCase):
    def test_mana_efficiency_and_unranked_parse(self):
        wd = map_to_week_data(_fixture())
        h = wd["healing"][0]
        self.assertEqual(h["name"], "Healz")
        self.assertEqual(h["mana_eff"], 20.0)            # 3,000,000 eff / 150,000 mana
        self.assertIsNone(h["vs_replacement"])           # not ranked → None, renders "—"


class TestExposeArmor(unittest.TestCase):
    def test_expose_armor_passthrough(self):
        # exposeArmor (rogue armor-debuff uptime — the Sunder slot's twin) maps straight through.
        wd = map_to_week_data(_fixture())
        self.assertEqual(wd["exposeArmor"], {"players": [{"name": "Huntz", "uptime": 67.8, "applications": 52}]})

    def test_expose_armor_empty_when_absent(self):
        wcl = _fixture()
        del wcl["expose_armor"]
        wd = map_to_week_data(wcl)
        self.assertEqual(wd["exposeArmor"], {})   # absent → {} (card/facet drops out, no crash)


class TestDeterminism(unittest.TestCase):
    def test_same_input_same_bytes(self):
        a = json.dumps(map_to_week_data(_fixture()), sort_keys=False)
        b = json.dumps(map_to_week_data(_fixture()), sort_keys=False)
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
