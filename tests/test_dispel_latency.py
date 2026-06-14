"""Dispel responsiveness matcher — _latency_match in wcl_fetchers.

The Dispels event stream only carries the REMOVAL; _dispel_latency pairs each cleanse with the
harmful debuff's most-recent prior LAND (from a companion Debuffs-events query) so latency =
dispel − land. _latency_match is the pure pairing core: latest apply ≤ dispel on the same
(aura, target), skip when the debuff was pre-applied (no recorded land), and flag DANGEROUS_DISPELS
(Mind Control) as clutch. These tests pin that pairing. Hermetic — no WCL.

Run:  python -m unittest discover -s tests
"""
import sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from wcl_fetchers import _latency_match

GID = {99: "Mind Control", 50: "Poison"}   # 99 is dangerous, 50 is routine


class TestLatencyMatch(unittest.TestCase):
    def test_basic_latency_from_most_recent_land(self):
        # cleanse at t=5000 of aura 50 on target 7; landed at 3000 → latency 2000ms.
        cleanse = [("Healz", 7, 50, 5000)]
        applies = {(50, 7): [3000]}
        lat, clutch = _latency_match(cleanse, applies, GID)
        self.assertEqual(lat["Healz"], [2000])
        self.assertEqual(clutch, {})           # 50 is not dangerous

    def test_picks_latest_land_before_dispel(self):
        # two lands (re-applied); the cleanse reacts to the most recent one (4500), not the old 1000.
        cleanse = [("Healz", 7, 50, 5000)]
        applies = {(50, 7): [1000, 4500]}
        lat, _ = _latency_match(cleanse, applies, GID)
        self.assertEqual(lat["Healz"], [500])

    def test_clutch_mind_control_flagged_with_seconds(self):
        cleanse = [("Zyph", 3, 99, 12700)]
        applies = {(99, 3): [10000]}
        lat, clutch = _latency_match(cleanse, applies, GID)
        self.assertEqual(lat["Zyph"], [2700])
        self.assertEqual(clutch["Zyph"], [{"aura": "Mind Control", "sec": 2.7}])

    def test_preapplied_debuff_is_skipped(self):
        # No land recorded for this (aura,target) — debuff was up before the window. Not timeable.
        cleanse = [("Healz", 7, 50, 5000)]
        applies = {}
        lat, clutch = _latency_match(cleanse, applies, GID)
        self.assertEqual(lat, {})
        self.assertEqual(clutch, {})

    def test_land_after_dispel_is_ignored(self):
        # Only land is AFTER the dispel (a later re-application) → no valid prior land → skipped.
        cleanse = [("Healz", 7, 50, 5000)]
        applies = {(50, 7): [6000]}
        lat, _ = _latency_match(cleanse, applies, GID)
        self.assertEqual(lat, {})

    def test_target_must_match(self):
        # Land was on a different target → no match for this cleanse.
        cleanse = [("Healz", 7, 50, 5000)]
        applies = {(50, 9): [3000]}
        lat, _ = _latency_match(cleanse, applies, GID)
        self.assertEqual(lat, {})


if __name__ == "__main__":
    unittest.main()
