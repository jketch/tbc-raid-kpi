"""Tank defensive-CD coverage value — _cd_cover in wcl_fetchers.

The "reverse bloodlust": for each CD's aura window, the UNMITIGATED damage the tank faced inside the
window ÷ window-seconds, over the tank's baseline unmitigated DTPS. >1× = the CD covered a spike
(well-timed); <1× = it was popped in a lull. _cd_cover is the pure core (windows + damage timeline +
baseline → ratio). These tests pin the window summation, the boundary inclusion, and the guards.
Hermetic — no WCL.

Run:  python -m unittest discover -s tests
"""
import sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from wcl_fetchers import _cd_cover, _cd_windows


class TestCdCover(unittest.TestCase):
    def test_spike_window_reads_above_one(self):
        # baseline 100 unmit/s; a 10s window faced 2000 → 200/s → 2.0× (covered heavy incoming)
        ev = [(500, 500), (2000, 1000), (5000, 1000), (12000, 500)]   # 500 & 12000 are outside the window
        self.assertEqual(_cd_cover([(1000, 11000)], ev, 100), 2.0)

    def test_lull_window_reads_below_one(self):
        ev = [(2000, 100), (5000, 100)]                               # only 200 over a 10s window → 20/s
        self.assertEqual(_cd_cover([(1000, 11000)], ev, 100), 0.2)

    def test_window_boundaries_are_inclusive(self):
        ev = [(1000, 100), (11000, 100)]                              # events exactly at start and end count
        self.assertEqual(_cd_cover([(1000, 11000)], ev, 10), 2.0)     # 200/10s = 20/s ÷ 10 = 2.0

    def test_multiple_windows_pooled(self):
        ev = [(5000, 1000), (25000, 3000)]
        # pooled: 4000 unmit over 20s of window = 200/s; baseline 100 → 2.0
        self.assertEqual(_cd_cover([(0, 10000), (20000, 30000)], ev, 100), 2.0)

    def test_zero_baseline_is_none(self):
        self.assertIsNone(_cd_cover([(0, 10000)], [(5000, 1000)], 0))

    def test_no_windows_is_none(self):
        self.assertIsNone(_cd_cover([], [(5000, 1000)], 100))

    def test_degenerate_window_skipped(self):
        # a zero/negative-length window contributes no seconds → None (nothing computable)
        self.assertIsNone(_cd_cover([(5000, 5000)], [(5000, 1000)], 100))


class TestCdWindows(unittest.TestCase):
    """Window reconstruction must be robust to the kill-scoped query dropping a removebuff that expired
    in a trash gap — otherwise an applybuff on boss A pairs with a removebuff on boss B (a giant phantom
    window that craters the coverage value). Same-fight pairing + a max-CD cap fix it."""
    FE = {"f1": 50000, "f2": 600000}              # fight end timestamps (ms)

    def test_normal_same_fight_window_is_exact(self):
        evs = [("applybuff", 1000, "f1"), ("removebuff", 13000, "f1")]   # 12s Barkskin, under the cap
        self.assertEqual(_cd_windows(evs, self.FE), [(1000, 13000)])

    def test_cross_fight_removebuff_is_capped_not_a_phantom(self):
        # the live bug: applybuff on f1, only removebuff is on f2 (~499s later) → must NOT make a 499s window
        evs = [("applybuff", 1000, "f1"), ("removebuff", 500000, "f2")]
        self.assertEqual(_cd_windows(evs, self.FE), [(1000, 26000)])     # capped to +25s, f2 removebuff ignored

    def test_missing_removebuff_orphan_capped(self):
        self.assertEqual(_cd_windows([("applybuff", 1000, "f1")], self.FE), [(1000, 26000)])

    def test_cap_clamped_to_short_fight_end(self):
        # fight ends before the cap would reach → window stops at the fight end
        self.assertEqual(_cd_windows([("applybuff", 1000, "f1")], {"f1": 9000}), [(1000, 9000)])

    def test_two_applybuffs_each_become_a_window(self):
        evs = [("applybuff", 1000, "f1"), ("applybuff", 30000, "f1"), ("removebuff", 40000, "f1")]
        self.assertEqual(_cd_windows(evs, self.FE), [(1000, 26000), (30000, 40000)])

    def test_empty(self):
        self.assertEqual(_cd_windows([], self.FE), [])


if __name__ == "__main__":
    unittest.main()
