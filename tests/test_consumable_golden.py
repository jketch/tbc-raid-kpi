"""Captured-data golden for the consumable pipeline — the test that would have caught this session's
bugs (Anniversary buff renames, bare-name flasks/elixirs, first-pull sampling).

Unlike the synthetic fixtures elsewhere (which encode our assumptions, so can't falsify them), this
runs the REAL recognition + best-of-night merge over a captured live COMBATANT_INFO payload
(tests/fixtures/combatant_info_golden.json) and asserts the HUMAN-VERIFIED expected output. Hermetic —
no network. Re-bless the fixture with scripts/tools/capture_consumable_fixture.py.

Run:  python -m unittest discover -s tests
"""
import sys, json, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from wcl_fetchers import (classify_pull_auras, merge_pull_consumables, unrecognized_self_buffs,
                          group_buffs_provided)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "combatant_info_golden.json"
DATA = json.loads(FIXTURE.read_text(encoding="utf-8"))


def _best_of_night(pulls):
    """Reproduce the production derivation: classify each pull's auras, attach weapon_oil, merge."""
    per_pull = []
    for pull in pulls:
        c = classify_pull_auras(pull["auras"])
        c["weapon_oil"] = pull["weapon_oil"]
        c["ranged_scope"] = pull.get("ranged_scope", False)
        per_pull.append(c)
    return merge_pull_consumables(per_pull)


class TestConsumableGolden(unittest.TestCase):
    def test_each_player_matches_verified_golden(self):
        # Recognition + best-of-night merge over REAL auras must equal the verified expected output.
        # Catches: renamed/bare-name flask & elixir misses, and first-pull-only sampling (the expected
        # values come from raiders whose first pull differs from their best pull).
        for name, p in DATA["players"].items():
            with self.subTest(player=name):
                self.assertEqual(_best_of_night(p["pulls"]), p["expected"])

    def test_discriminating_negative_present(self):
        # Guard that the fixture keeps at least one genuine FAIL (no flask, <2 elixirs) — so a future
        # change that makes everything "pass" can't slip by. Philliam: 1 elixir, no flask.
        def passes(e):
            return bool(e["flask"]) or len(e["elixirs"]) >= 2
        verdicts = {n: passes(p["expected"]) for n, p in DATA["players"].items()}
        self.assertIn(False, verdicts.values(), "fixture lost its negative case — recognition may over-credit")
        self.assertIn(True, verdicts.values())

    def test_group_buff_gear_detected_for_provider(self):
        # Alldorin clicks Eye of the Night (JC +34 SP party neck) → detected as PROVIDED from his
        # self-sourced pull auras. Real-data guard for the Utility-pillar credit.
        provided = {}
        for pull in DATA["players"]["Alldorin"]["pulls"]:
            provided.update(group_buffs_provided(pull["auras"], pull["sourceID"]))
        self.assertIn("Eye of the Night", provided)

    def test_canary_quiet_on_real_data(self):
        # The recognition lists + SELF_BUFF_IGNORE together must cover ALL self-applied auras in real
        # data → the pipeline canary stays silent. A new name here means recognition regressed OR a real
        # consumable/self-buff appeared needing triage (add to game_constants ID maps / SELF_BUFF_IGNORE).
        unknown = set()
        for p in DATA["players"].values():
            for pull in p["pulls"]:
                unknown |= set(unrecognized_self_buffs(pull["auras"], pull["sourceID"]))
        self.assertEqual(unknown, set(), f"unrecognized self-applied pull auras: {sorted(unknown)}")


if __name__ == "__main__":
    unittest.main()
