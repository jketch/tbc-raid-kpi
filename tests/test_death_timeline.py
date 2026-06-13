"""Regression test for the 2026-06-13 review #1: Deaths-table recap events embed an
`ability` OBJECT ({name, guid, abilityIcon}) — they do NOT carry abilityGameID like regular
events. The old builder read only abilityGameID, so every recap hit rendered "Melee" and the
killing-blow headline contradicted its own timeline. Pins the embedded-object resolution,
the legacy abilityGameID fallback, and the Melee default."""
import sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from wcl_fetchers import _build_death_timeline


def _ev(ts, kind, amt, ability=None, gid=None, src=5, **kw):
    e = {"type": kind, "timestamp": ts, "amount": amt, "sourceID": src, **kw}
    if ability is not None:
        e["ability"] = ability
    if gid is not None:
        e["abilityGameID"] = gid
    return e


ACTORS = {5: "Greyheart Skulker", 7: "Healz"}


class TestDeathTimeline(unittest.TestCase):
    def test_embedded_ability_object_resolves_names(self):
        evs = [
            _ev(1000, "damage", 3000, ability={"name": "Whirlwind", "guid": 15578}),
            _ev(1500, "heal",   2000, ability={"name": "Flash Heal", "guid": 2061},
                src=7, overheal=100),
            _ev(2000, "damage", 9000, ability={"name": "Melee", "guid": 1}, overkill=1500),
        ]
        tl = _build_death_timeline(evs, {}, ACTORS)
        self.assertEqual([p["ability"] for p in tl], ["Whirlwind", "Flash Heal", "Melee"])
        self.assertTrue(tl[-1]["killing"])
        self.assertEqual(tl[-1]["over"], 1500)
        self.assertEqual(tl[0]["t"], -1.0)          # relative to the killing blow
        self.assertEqual(tl[1]["src"], "Healz")

    def test_legacy_abilityGameID_still_falls_back_to_masterdata(self):
        evs = [_ev(1000, "damage", 5000, gid=37500),
               _ev(2000, "damage", 9000, gid=1)]
        tl = _build_death_timeline(evs, {37500: "Whirlwind"}, ACTORS)
        self.assertEqual([p["ability"] for p in tl], ["Whirlwind", "Melee"])

    def test_no_ability_info_defaults_to_melee(self):
        tl = _build_death_timeline([_ev(1000, "damage", 9000)], {}, ACTORS)
        self.assertEqual(tl[0]["ability"], "Melee")


if __name__ == "__main__":
    unittest.main()
