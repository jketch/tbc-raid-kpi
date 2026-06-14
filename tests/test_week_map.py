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
from wcl_fetchers import (classify_pull_auras, merge_pull_consumables, unrecognized_self_buffs,
                          group_buffs_provided)


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
            # hunter: NO temp oil (doesn't oil a bow) but HAS a ranged scope → counts as the weapon enhancer
            "Huntz": {"flask": "", "food": True, "elixirs": [], "scrolls": [],
                      "weapon_oil": False, "ranged_scope": True},
        },
        "consum_use": {"Wlock": {"healthstone": 2, "potion": 1}},
        "group_buffs": {"Wlock": {"Eye of the Night": "+34 spell power (party)"}},
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
        self.assertFalse(rows[0]["flask"])    # battle-only — one slot → not flask-equivalent

    # ── classify_pull_auras: ID-anchored consumable recognition (the recognition list) ──
    def _aura(self, name, ability=None):
        return {"name": name, "ability": ability}

    def test_recognize_flask_by_bare_effect_name(self):
        # Flask of Supreme Power logs as bare "Supreme Power" (no "Flask of" prefix) — caught by
        # ID (17628) and by the effect-name net. Previously recognized as NOTHING.
        by_id   = classify_pull_auras([self._aura("Supreme Power", 17628)])
        by_name = classify_pull_auras([self._aura("Supreme Power", 999999)])  # unknown id, name net
        self.assertEqual(by_id["flask"], "Flask of Supreme Power")
        self.assertEqual(by_name["flask"], "Flask of Supreme Power")

    def test_recognize_renamed_elixir_by_id(self):
        # "Major Shadow Power" (28503) is a battle elixir that was unrecognized by name.
        out = classify_pull_auras([self._aura("Major Shadow Power", 28503)])
        self.assertEqual(out["elixirs"], ["Major Shadow Power"])

    def test_recognize_chromatic_wonder_flask(self):
        # The T5/T6 resist flask logs as bare "Chromatic Wonder" (id 42735).
        self.assertEqual(classify_pull_auras([self._aura("Chromatic Wonder", 42735)])["flask"],
                         "Flask of Chromatic Wonder")

    def test_recognize_shattrath_flask_family(self):
        # Marks-of-Illidari "Shattrath Flask of X" → buff "<Effect> of Shattrath", distinct id+name.
        out = classify_pull_auras([self._aura("Pure Death of Shattrath", 46837)])
        self.assertEqual(out["flask"], "Flask of Pure Death (Shattrath)")
        # robust to an unknown id (the suffix-match carries it):
        out2 = classify_pull_auras([self._aura("Relentless Assault of Shattrath", 123456)])
        self.assertEqual(out2["flask"], "Flask of Relentless Assault (Shattrath)")

    def test_recognize_zanza_buffs_as_elixirs(self):
        # Zanza potion buffs log as "<Effect> of Zanza" (Spirit/Swiftness/Sheen) — stat consumables,
        # counted as an elixir slot via the suffix (covers all three, incl. ones we never ID).
        out = classify_pull_auras([
            self._aura("Spirit of Zanza", 24382),
            self._aura("Swiftness of Zanza", 24383),
        ])
        self.assertEqual(out["elixirs"], ["Spirit of Zanza", "Swiftness of Zanza"])
        # now recognized → the canary must NOT flag it as a missed consumable:
        zanza = [{"name": "Spirit of Zanza", "ability": 24382, "source": 7}]
        self.assertEqual(unrecognized_self_buffs(zanza, 7), [])

    def test_recognize_scroll_rank_iv_protection(self):
        # "Armor" (12175 = Scroll of Protection rank IV) was unmapped; rank V (33079) already was.
        self.assertEqual(classify_pull_auras([self._aura("Armor", 12175)])["scrolls"], ["Scroll of Protection"])

    def test_recognize_scrolls_by_id(self):
        # Scrolls log under a BARE stat name (no "Scroll of " prefix) — recognized by ID only.
        # 33080 logs as "Versatility" on Anniversary but is Scroll of Spirit.
        out = classify_pull_auras([
            self._aura("Agility", 33077),
            self._aura("Versatility", 33080),   # Anniversary-renamed Scroll of Spirit
        ])
        self.assertEqual(out["scrolls"], ["Scroll of Agility", "Scroll of Spirit"])
        self.assertEqual(out["flask"], "")          # a scroll is not a flask/elixir
        self.assertEqual(out["elixirs"], [])

    def test_merge_best_of_night_credits_late_flask_and_food(self):
        # Regression (Blunderdin): first pull caught him with just an elixir, no flask/food; he
        # flasked + ate after. Best-of-night must credit the flask + food from the later pulls.
        out = merge_pull_consumables([
            {"flask": "", "food": False, "elixirs": ["Mighty Agility"], "scrolls": [], "weapon_oil": False},
            {"flask": "Flask of Relentless Assault", "food": True, "elixirs": ["Mighty Agility"],
             "scrolls": ["Scroll of Agility"], "weapon_oil": True},
        ])
        self.assertEqual(out["flask"], "Flask of Relentless Assault")
        self.assertTrue(out["food"])
        self.assertTrue(out["weapon_oil"])
        self.assertEqual(out["scrolls"], ["Scroll of Agility"])

    def test_merge_elixir_slot_uses_best_single_pull_not_union(self):
        # Two DIFFERENT single battle elixirs across pulls must NOT union into a fake battle+guardian
        # pair — the elixir slot takes the largest single-pull set (here still 1).
        out = merge_pull_consumables([
            {"flask": "", "food": True, "elixirs": ["Mighty Agility"], "scrolls": []},
            {"flask": "", "food": True, "elixirs": ["Major Strength"], "scrolls": []},
        ])
        self.assertEqual(len(out["elixirs"]), 1)
        # but a genuine 2-elixir pull is kept whole:
        out2 = merge_pull_consumables([
            {"flask": "", "food": True, "elixirs": ["Mighty Agility"], "scrolls": []},
            {"flask": "", "food": True, "elixirs": ["Greater Versatility", "Mighty Agility"], "scrolls": []},
        ])
        self.assertEqual(sorted(out2["elixirs"]), ["Greater Versatility", "Mighty Agility"])

    def test_hunter_weapon_slot_uses_ranged_scope_not_oil(self):
        # A hunter's weapon enhancer is the ranged SCOPE (permanent), not a temp oil/stone. Huntz has
        # ranged_scope=True but weapon_oil(temp)=False → the Weapon slot should still read ✓.
        wd = map_to_week_data(_fixture())
        grid = {r["name"]: r for r in wd["consumables"]}
        self.assertTrue(grid["Huntz"]["weapon"])        # scope counts
        # a non-hunter (Wlock) still keys off the temp oil:
        self.assertTrue(grid["Wlock"]["weapon"])        # weapon_oil=True in the fixture

    def test_group_buff_gear_surfaced_in_week_data(self):
        wd = map_to_week_data(_fixture())
        gbg = {r["name"]: r for r in wd["groupBuffGear"]}
        self.assertIn("Wlock", gbg)
        self.assertEqual(gbg["Wlock"]["buffs"], [{"item": "Eye of the Night", "label": "+34 spell power (party)"}])
        self.assertEqual(gbg["Wlock"]["class"], "Warlock")

    def test_group_buff_gear_credits_provider_not_recipients(self):
        ME, OTHER = 7, 3
        provider = [{"ability": 31033, "name": "Eye of the Night", "source": ME}]      # I clicked it
        recipient = [{"ability": 31033, "name": "Eye of the Night", "source": OTHER}]  # cast on me
        self.assertEqual(group_buffs_provided(provider, ME), {"Eye of the Night": "+34 spell power (party)"})
        self.assertEqual(group_buffs_provided(recipient, ME), {})   # recipient gets no credit
        # and the canary must NOT flag a recognized group-buff neck as a missed consumable:
        self.assertEqual(unrecognized_self_buffs(provider, ME), [])

    def test_canary_flags_self_applied_unknown_only(self):
        ME = 7
        auras = [
            {"ability": 99999, "name": "Mystery Brew", "source": ME},          # self + unknown → FLAG
            {"ability": 17628, "name": "Supreme Power", "source": ME},         # self but recognized → no
            {"ability": 25898, "name": "Greater Blessing of Kings", "source": 3},  # other-sourced → no
            {"ability": 2458,  "name": "Berserker Stance", "source": ME},      # self but known buff → no
        ]
        self.assertEqual(unrecognized_self_buffs(auras, ME), ["Mystery Brew"])

    def test_scrolls_flow_into_compliance_grid(self):
        # The compliance grid (Prep data source) must carry scrolls so the Raider Score can credit them.
        rows = build_consumable_compliance([
            {"name": "Rg", "role": "Physical", "flask": "Flask of Relentless Assault",
             "elixirs": [], "food": True, "scrolls": ["Scroll of Agility"]}])
        self.assertEqual(rows[0]["scrolls"], ["Scroll of Agility"])

    def test_full_anniversary_loadout_and_noise_ignored(self):
        # A realistic Moojerked-style pull: 2 renamed elixirs + food, plus raid buffs that must
        # NOT be mistaken for consumables.
        out = classify_pull_auras([
            self._aura("Greater Versatility", 28509),   # Mageblood guardian (renamed)
            self._aura("Spellpower Elixir", 33721),     # Adept's battle (renamed)
            self._aura("Well Fed", 33263),
            self._aura("Greater Blessing of Kings", 25898),
            self._aura("Arcane Brilliance", 27127),
            self._aura("Prayer of Shadow Protection", 39374),
        ])
        self.assertEqual(out["elixirs"], ["Greater Versatility", "Spellpower Elixir"])
        self.assertTrue(out["food"])
        self.assertEqual(out["flask"], "")            # no flask; the 2-elixir path is what passes

    def test_anniversary_renamed_two_elixirs_pass(self):
        # Regression (Moojerked et al.): on Anniversary the Mageblood guardian buff logs as
        # "Greater Versatility" — not in the old guardian allowlist, so a real battle+guardian pair
        # was wrongly failed. Two distinct elixir auras = both slots filled, by the TBC game rule.
        rows = {r["name"]: r for r in build_consumable_compliance([
            {"name": "Moojerked", "role": "Healer", "flask": "",
             "elixirs": ["Greater Versatility", "Spellpower Elixir"], "food": True},
            {"name": "Zyph", "role": "Physical", "flask": "",
             "elixirs": ["Greater Versatility", "Mighty Agility"], "food": True}])}
        self.assertTrue(rows["Moojerked"]["flask"])
        self.assertTrue(rows["Zyph"]["flask"])

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
