"""test_log_window_guard.py — the combat-log <-> report identity guard.

Regression cover for the Jun-1-log-on-May-weeks contamination (2026-06-14): a combat log whose
time span does NOT cover the report window must be REJECTED, not silently merged. TBC bosses repeat
weekly, so parse_combat_log's boss-NAME scope can't catch a wrong-week log — only the time window
can. Exercises week_build._log_covers_report directly (the from_wcl chokepoint guard) plus the
date-anchored overlap math the fix relies on. Hermetic: temp logs only, zero WCL/DB.
"""
import io, sys, unittest, tempfile, contextlib
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import week_build as wb
import log_discovery as ld


def _epoch_ms(y, mo, d, h, mi):
    # Local-tz epoch ms, matching how to_log_scale interprets a WCL report's startTime/endTime.
    return int(datetime(y, mo, d, h, mi).timestamp() * 1000)


def _write_log(dir_, name, y, mo, d, h0, h1):
    """A tiny WoWCombatLog .txt whose first/last stamps span (y/mo/d h0:00)..(y/mo/d h1:00), in the
    client's 'M/D/YYYY H:MM:SS.mmm  EVENT' format that combat_log._parse_ts reads."""
    p = Path(dir_) / name
    lines = [
        f"{mo}/{d}/{y} {h0}:00:00.000  ENCOUNTER_START,x",
        f"{mo}/{d}/{y} {(h0 + h1) // 2}:30:00.000  SPELL_DAMAGE,x",
        f"{mo}/{d}/{y} {h1}:00:00.000  ENCOUNTER_END,x",
    ]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(p)


class LogWindowGuard(unittest.TestCase):
    def setUp(self):
        # A May-18 report window: 19:00–23:00, local (the scale logs are stamped on).
        self.report = {
            "startTime": _epoch_ms(2026, 5, 18, 19, 0),
            "endTime":   _epoch_ms(2026, 5, 18, 23, 0),
            "fights": [{"name": "Lady Vashj", "kill": True}],
        }

    def _guard(self, log_path):
        with contextlib.redirect_stdout(io.StringIO()):   # swallow the ⚠/glyph prints (cp1252-safe)
            return wb._log_covers_report(log_path, self.report)

    def test_same_week_log_accepted(self):
        with tempfile.TemporaryDirectory() as d:
            lp = _write_log(d, "WoWCombatLog-good.txt", 2026, 5, 18, 19, 23)
            self.assertTrue(self._guard(lp))

    def test_off_week_june_log_rejected(self):
        # The actual bug: a Jun-1 log handed to the May-18 report. Same boss, two weeks later.
        with tempfile.TemporaryDirectory() as d:
            lp = _write_log(d, "WoWCombatLog-june.txt", 2026, 6, 1, 19, 23)
            self.assertFalse(self._guard(lp))

    def test_unreadable_log_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "WoWCombatLog-empty.txt"
            p.write_text("", encoding="utf-8")
            self.assertFalse(self._guard(str(p)))

    def test_missing_window_does_not_block(self):
        # A report dict lacking startTime/endTime (defensive) must not crash or false-reject.
        with tempfile.TemporaryDirectory() as d:
            lp = _write_log(d, "WoWCombatLog-x.txt", 2026, 6, 1, 19, 23)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertTrue(wb._log_covers_report(lp, {"fights": []}))

    def test_overlap_math_is_date_anchored(self):
        # Freeze the property the fix leans on: a cross-week range scores 0 (below floor); the
        # in-window range scores >= floor. Guards against a future scale/TZ refactor breaking it.
        S = ld.to_log_scale(self.report["startTime"])
        E = ld.to_log_scale(self.report["endTime"])
        win = max(E - S, 1.0)

        def score(rng):
            return max(0.0, min(E, rng[1]) - max(S, rng[0])) / win

        june = (ld.to_log_scale(_epoch_ms(2026, 6, 1, 19, 0)),
                ld.to_log_scale(_epoch_ms(2026, 6, 1, 23, 0)))
        self.assertEqual(score(june), 0.0)
        self.assertGreaterEqual(score((S, E)), ld.MATCH_FLOOR)


if __name__ == "__main__":
    unittest.main()
