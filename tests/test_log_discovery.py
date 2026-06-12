"""Tests for log_discovery (report-window matching + archival) and combat_log._open_log.

Hermetic: synthetic combat logs in tempdirs; no WCL, no real WoW directory. Timestamps are
built from the SAME local datetimes on both sides (epoch via .timestamp(), log lines via the
wall-clock format), so the tests pass in any timezone.

Run:  python -m unittest discover -s tests
"""
import gzip, sys, tempfile, unittest, zipfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import log_discovery as ld
import combat_log as cl


def _ts(dt):
    """One combat-log wall-clock stamp (M/D/YYYY H:MM:SS.mmm)."""
    return f"{dt.month}/{dt.day}/{dt.year} {dt.hour}:{dt.minute:02d}:{dt.second:02d}.000"


def _line(dt):
    return f"{_ts(dt)}  SPELL_DAMAGE,Player-1,\"Src\",0x511,Player-2,\"Dst\",0x511,1,2,3"


def write_log(path, start_dt, end_dt, step_s=60, prefix_lines=()):
    lines = list(prefix_lines)
    t = start_dt
    while t <= end_dt:
        lines.append(_line(t))
        t += timedelta(seconds=step_s)
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


# A fixed local raid window all tests share: 6/11/2026 19:00 → 23:00 local.
W_START = datetime(2026, 6, 11, 19, 0, 0)
W_END   = datetime(2026, 6, 11, 23, 0, 0)
START_MS = int(W_START.timestamp() * 1000)
END_MS   = int(W_END.timestamp() * 1000)


class TestToLogScale(unittest.TestCase):
    def test_round_trips_against_parse_ts(self):
        # the converter must land on the exact scale _parse_ts produces for the same wall clock
        self.assertAlmostEqual(ld.to_log_scale(START_MS),
                               cl._parse_ts(_ts(W_START) + "  X"), places=3)
        self.assertAlmostEqual(ld.to_log_scale(END_MS),
                               cl._parse_ts(_ts(W_END) + "  X"), places=3)


class TestSniffLogRange(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_small_file(self):
        p = self.dir / "WoWCombatLog-a.txt"
        write_log(p, W_START, W_END)
        f, l = ld.sniff_log_range(p)
        self.assertAlmostEqual(f, ld.to_log_scale(START_MS), places=3)
        self.assertAlmostEqual(l, ld.to_log_scale(END_MS), places=3)

    def test_garbage_prefix_is_skipped(self):
        p = self.dir / "WoWCombatLog-b.txt"
        write_log(p, W_START, W_END, prefix_lines=["COMBAT_LOG_VERSION,9", "garbage line"])
        f, _l = ld.sniff_log_range(p)
        self.assertAlmostEqual(f, ld.to_log_scale(START_MS), places=3)

    def test_empty_and_garbage_only_return_none(self):
        e = self.dir / "WoWCombatLog-empty.txt"
        e.write_text("", encoding="utf-8")
        g = self.dir / "WoWCombatLog-garbage.txt"
        g.write_text("not a log\nstill not a log\n", encoding="utf-8")
        self.assertIsNone(ld.sniff_log_range(e))
        self.assertIsNone(ld.sniff_log_range(g))

    def test_file_larger_than_head_plus_tail(self):
        # enough 5s-step lines that the head and tail 64 KB windows don't overlap
        p = self.dir / "WoWCombatLog-big.txt"
        write_log(p, W_START, W_END, step_s=5)
        self.assertGreater(p.stat().st_size, ld.HEAD_BYTES + ld.TAIL_BYTES)
        f, l = ld.sniff_log_range(p)
        self.assertAlmostEqual(f, ld.to_log_scale(START_MS), places=3)
        self.assertAlmostEqual(l, ld.to_log_scale(END_MS), places=3)


class TestDiscoverLog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _discover(self):
        return ld.discover_log(START_MS, END_MS, self.dir)

    def test_covering_file_is_picked(self):
        p = self.dir / "WoWCombatLog-061126_185500.txt"
        write_log(p, W_START - timedelta(minutes=5), W_END + timedelta(minutes=10))
        self.assertEqual(self._discover(), str(p))

    def test_late_logger_still_matches(self):
        # zoned in 12 minutes after the report started → ~95% overlap, above the floor
        p = self.dir / "WoWCombatLog-061126_191200.txt"
        write_log(p, W_START + timedelta(minutes=12), W_END + timedelta(minutes=5))
        self.assertEqual(self._discover(), str(p))

    def test_disjoint_previous_raid_is_not_picked(self):
        write_log(self.dir / "WoWCombatLog-060926.txt",
                  W_START - timedelta(days=2), W_END - timedelta(days=2))
        self.assertIsNone(self._discover())

    def test_tie_breaks_to_smallest_span(self):
        mega = self.dir / "WoWCombatLog-mega.txt"
        write_log(mega, W_START - timedelta(hours=9), W_END + timedelta(minutes=30), step_s=300)
        tight = self.dir / "WoWCombatLog-tight.txt"
        write_log(tight, W_START - timedelta(minutes=2), W_END + timedelta(minutes=5))
        self.assertEqual(self._discover(), str(tight))   # both score 1.0; specific file wins

    def test_garbage_and_empty_candidates_are_skipped(self):
        (self.dir / "WoWCombatLog-empty.txt").write_text("", encoding="utf-8")
        (self.dir / "WoWCombatLog-junk.txt").write_text("nope\n", encoding="utf-8")
        good = self.dir / "WoWCombatLog-good.txt"
        write_log(good, W_START, W_END)
        self.assertEqual(self._discover(), str(good))

    def test_missing_dir_returns_none(self):
        self.assertIsNone(ld.discover_log(START_MS, END_MS, self.dir / "nope"))


class TestArchiveLog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.arch = self.dir / "archive"
        self.log = self.dir / "WoWCombatLog.txt"
        write_log(self.log, W_START, W_START + timedelta(minutes=10))
        self.orig_bytes = self.log.read_bytes()

    def tearDown(self):
        self.tmp.cleanup()

    def test_zip_verify_remove(self):
        out = ld.archive_log(self.log, "CODE1", START_MS, self.arch)
        self.assertEqual(out, self.arch / "WoWCombatLog-20260611-CODE1.zip")
        self.assertFalse(self.log.exists(), "original must be removed after a verified zip")
        with zipfile.ZipFile(out) as zf:
            self.assertEqual(zf.read("WoWCombatLog.txt"), self.orig_bytes)

    def test_failure_keeps_original(self):
        with mock.patch.object(ld.zipfile, "ZipFile", side_effect=OSError("disk full")):
            out = ld.archive_log(self.log, "CODE2", START_MS, self.arch)
        self.assertIsNone(out)
        self.assertTrue(self.log.exists(), "a failed archive must never lose the original")
        self.assertEqual(self.log.read_bytes(), self.orig_bytes)

    def test_existing_target_gets_suffix_not_overwritten(self):
        self.arch.mkdir(parents=True)
        first = self.arch / "WoWCombatLog-20260611-CODE3.zip"
        first.write_bytes(b"sentinel")
        out = ld.archive_log(self.log, "CODE3", START_MS, self.arch)
        self.assertEqual(out, self.arch / "WoWCombatLog-20260611-CODE3-2.zip")
        self.assertEqual(first.read_bytes(), b"sentinel")


class TestOpenLog(unittest.TestCase):
    def test_txt_zip_gz_yield_identical_lines(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            txt = d / "WoWCombatLog.txt"
            write_log(txt, W_START, W_START + timedelta(minutes=3))
            zp = d / "WoWCombatLog.zip"
            with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.write(txt, txt.name)
            gz = d / "WoWCombatLog.gz"
            gz.write_bytes(gzip.compress(txt.read_bytes()))
            outs = []
            for p in (txt, zp, gz):
                with cl._open_log(str(p)) as f:
                    outs.append(list(f))
            self.assertEqual(outs[0], outs[1])
            self.assertEqual(outs[0], outs[2])
            self.assertGreater(len(outs[0]), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
