"""Debuff ramp-speed stack reconstruction — _stack_ramp in wcl_fetchers.

_stack_ramp turns one (debuff, enemy-target) Debuffs-event stream into (peak, ramp_ts,
uptime_at_threshold_ms). The "established" threshold is the declared max only when the debuff
actually stacked (peak >= 2) — otherwise time-to-first-up — so a single-application debuff (or a
stacker that flatlined at 1, e.g. Expose holding the Sunder slot) is measured honestly. These tests
pin that threshold rule, the applydebuffstack stack-count handling, and the uptime integration
(including the still-up-at-fight-end close). Pure, hermetic — no WCL.

Run:  python -m unittest discover -s tests
"""
import sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from wcl_fetchers import _stack_ramp


class TestStackRamp(unittest.TestCase):
    def test_stacking_to_max_ramp_and_uptime(self):
        # Shadow-Weaving-like build to 5; stays up to fight end. f_start=0, f_end=100_000 (100s).
        evs = [(5000, "applydebuff", None), (6000, "applydebuffstack", 2),
               (7000, "applydebuffstack", 3), (8000, "applydebuffstack", 4),
               (9000, "applydebuffstack", 5)]
        peak, ramp_ts, up_ms = _stack_ramp(evs, 0, 100_000, 5)
        self.assertEqual(peak, 5)
        self.assertEqual(ramp_ts, 9000)            # first reached 5 at t=9000
        self.assertEqual(up_ms, 91_000)            # 5-stack held from 9000 to fight end

    def test_single_application_threshold_one(self):
        evs = [(3000, "applydebuff", None)]
        peak, ramp_ts, up_ms = _stack_ramp(evs, 0, 100_000, 1)
        self.assertEqual(peak, 1)
        self.assertEqual(ramp_ts, 3000)
        self.assertEqual(up_ms, 97_000)

    def test_declared_stacker_that_flatlines_falls_to_first_up(self):
        # Declared max 5 but the debuff never went past 1 (Expose holding the Sunder slot) →
        # threshold collapses to 1, so it's measured as time-to-first-up, NOT "never reached 5".
        evs = [(5000, "applydebuff", None)]
        peak, ramp_ts, up_ms = _stack_ramp(evs, 0, 100_000, 5)
        self.assertEqual(peak, 1)
        self.assertEqual(ramp_ts, 5000)           # established at the (single) application
        self.assertEqual(up_ms, 95_000)

    def test_uptime_integrates_across_drop_and_reapply(self):
        evs = [(1000, "applydebuff", None), (3000, "removedebuff", None),
               (5000, "applydebuff", None)]
        peak, ramp_ts, up_ms = _stack_ramp(evs, 0, 10_000, 1)
        self.assertEqual(peak, 1)
        self.assertEqual(ramp_ts, 1000)
        self.assertEqual(up_ms, 2000 + 5000)      # [1000,3000] + [5000,fight end 10000]

    def test_refresh_does_not_change_stack(self):
        evs = [(1000, "applydebuff", None), (2000, "applydebuffstack", 5),
               (3000, "refreshdebuff", None)]
        peak, ramp_ts, up_ms = _stack_ramp(evs, 0, 10_000, 5)
        self.assertEqual(peak, 5)
        self.assertEqual(ramp_ts, 2000)
        self.assertEqual(up_ms, 8000)             # 5-stack from 2000 to fight end, refresh inert

    def test_never_reaches_declared_max_has_no_ramp(self):
        # Genuine stacker (peak 3) that never hit the declared 5 → threshold stays 5, never reached.
        evs = [(1000, "applydebuff", None), (2000, "applydebuffstack", 2),
               (3000, "applydebuffstack", 3)]
        peak, ramp_ts, up_ms = _stack_ramp(evs, 0, 10_000, 5)
        self.assertEqual(peak, 3)
        self.assertIsNone(ramp_ts)                # never reached 5
        self.assertEqual(up_ms, 0)


if __name__ == "__main__":
    unittest.main()
