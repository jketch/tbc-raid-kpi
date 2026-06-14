"""Bloodlust window dedup — _dedup_lust_windows in wcl_fetchers.

A lust WINDOW is 30s. Two shamans lusting together (same moment) are ONE window; a separate later
cast (split lust — pull + execute) is its own. _dedup_lust_windows collapses raw casts into windows;
these tests pin that grouping (the boundary, co-caster merge, and ordering). Hermetic — no WCL.

Run:  python -m unittest discover -s tests
"""
import sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from wcl_fetchers import _dedup_lust_windows


class TestDedupLustWindows(unittest.TestCase):
    def test_single_cast_is_one_window(self):
        self.assertEqual(_dedup_lust_windows([(5000, "Shammy")]), [[5000, ["Shammy"]]])

    def test_simultaneous_casters_merge_into_one_window(self):
        # two shamans lust ~together (within 30s) → one window, both credited
        out = _dedup_lust_windows([(5000, "Shammy"), (7000, "Bloodlusty")])
        self.assertEqual(out, [[5000, ["Shammy", "Bloodlusty"]]])

    def test_split_lust_is_two_windows(self):
        # pull lust + execute lust 5 minutes later → two distinct windows
        out = _dedup_lust_windows([(5000, "Shammy"), (305000, "Shammy")])
        self.assertEqual(out, [[5000, ["Shammy"]], [305000, ["Shammy"]]])

    def test_window_boundary_is_exclusive_at_30s(self):
        # exactly 30s apart → NOT merged (>= gap opens a new window)
        out = _dedup_lust_windows([(0, "A"), (30000, "B")])
        self.assertEqual(out, [[0, ["A"]], [30000, ["B"]]])
        # just under 30s → merged
        out2 = _dedup_lust_windows([(0, "A"), (29999, "B")])
        self.assertEqual(out2, [[0, ["A", "B"]]])

    def test_same_caster_not_double_credited_in_window(self):
        out = _dedup_lust_windows([(1000, "Shammy"), (2000, "Shammy")])
        self.assertEqual(out, [[1000, ["Shammy"]]])

    def test_unordered_input_is_sorted(self):
        out = _dedup_lust_windows([(305000, "Shammy"), (5000, "Shammy")])
        self.assertEqual(out, [[5000, ["Shammy"]], [305000, ["Shammy"]]])

    def test_empty(self):
        self.assertEqual(_dedup_lust_windows([]), [])


if __name__ == "__main__":
    unittest.main()
