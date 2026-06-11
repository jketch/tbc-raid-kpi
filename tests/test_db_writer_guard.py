"""Tests for the write_week downgrade-guard + schema_version stamp (scripts/db_writer.py).

The _db_dropped_sections checks are hermetic (in-memory DB, manual rows). The end-to-end skip test
uses a real snapshot if present (it exercises the full write path), and skips cleanly otherwise.

Run:  python -m unittest discover -s tests
"""
import sys, json, sqlite3, io, contextlib, tempfile, shutil, unittest
from pathlib import Path

# Match the pipeline: it reconfigures stdout to UTF-8 so ✓/⚠ prints don't crash on the cp1252
# Windows console. unittest doesn't import the pipeline's main(), so do it here.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import db_writer


@contextlib.contextmanager
def temp_db():
    """A temp DB path with Windows-tolerant cleanup (sqlite WAL can briefly hold the file)."""
    d = tempfile.mkdtemp()
    try:
        yield Path(d) / "t.db"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _fresh_db():
    con = sqlite3.connect(":memory:")
    con.executescript(db_writer.SCHEMA)
    return con


class TestDroppedSections(unittest.TestCase):
    def test_detects_sections_the_incoming_would_drop(self):
        con = _fresh_db()
        con.execute("INSERT INTO avoidable_dmg (report_code, player) VALUES ('R1','A')")
        con.execute("INSERT INTO loot (report_code, player, item_id, boss) VALUES ('R1','A','1','B')")
        thin = {"meta": {"kills": 10}, "roster": {"A": {}}}      # lacks avoidableDmg + loot
        dropped = db_writer._db_dropped_sections(con, "R1", thin)
        self.assertIn("avoidableDmg", dropped)
        self.assertIn("loot", dropped)

    def test_no_drop_when_incoming_is_at_least_as_rich(self):
        con = _fresh_db()
        con.execute("INSERT INTO avoidable_dmg (report_code, player) VALUES ('R1','A')")
        con.execute("INSERT INTO loot (report_code, player, item_id, boss) VALUES ('R1','A','1','B')")
        rich = {"meta": {"kills": 10},
                "avoidableDmg": [{"name": "A"}],
                "loot": {"players": [{"name": "A"}], "total": 1}}
        self.assertEqual(db_writer._db_dropped_sections(con, "R1", rich), set())

    def test_empty_db_is_never_a_downgrade(self):
        con = _fresh_db()
        self.assertEqual(db_writer._db_dropped_sections(con, "NEW", {"meta": {"kills": 10}}), set())


class TestWriteWeekGuardEndToEnd(unittest.TestCase):
    """Full write path against a temp DB, using a real snapshot as the 'rich' week."""

    def setUp(self):
        snaps = sorted((ROOT / "cache" / "week_data").glob("*.json"))
        if not snaps:
            self.skipTest("no cache/week_data snapshot to use as a rich fixture")
        # pick a log-complete one (has avoidableDmg) so the thin copy is a real downgrade
        self.rich = None
        for s in snaps:
            d = json.loads(s.read_text(encoding="utf-8"))
            if d.get("avoidableDmg") and d.get("loot", {}).get("players"):
                self.rich = d
                break
        if self.rich is None:
            self.skipTest("no log-complete + loot snapshot available")

    def test_thin_write_preserves_dropped_sections_and_writes_the_rest(self):
        with temp_db() as dbp:
            db_writer.write_week(self.rich, dbp)            # seed the rich row
            con = sqlite3.connect(dbp)
            rc = self.rich["meta"]["report_code"]
            n_avoid = con.execute("SELECT COUNT(*) FROM avoidable_dmg WHERE report_code=?", (rc,)).fetchone()[0]
            n_loot  = con.execute("SELECT COUNT(*) FROM loot WHERE report_code=?", (rc,)).fetchone()[0]
            sv      = con.execute("SELECT schema_version FROM weeks WHERE report_code=?", (rc,)).fetchone()[0]
            con.close()
            self.assertGreater(n_avoid, 0)
            self.assertGreater(n_loot, 0)
            self.assertEqual(sv, db_writer.SCHEMA_VERSION)   # schema_version stamped

            # now a THIN version (loses avoidable + loot). The section-granular guard must PRESERVE
            # those richer rows (not wipe them) while still writing every other section — the old
            # behavior skipped the WHOLE write, discarding fresh data to save one section.
            thin = dict(self.rich)
            thin["avoidableDmg"] = []
            thin["loot"] = {}
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                db_writer.write_week(thin, dbp)
            out = buf.getvalue()
            self.assertIn("preserving", out)                # informs, doesn't skip the whole write
            self.assertIn("DB written", out)                # the write still happened
            con = sqlite3.connect(dbp)
            # the dropped sections' richer rows survive untouched …
            self.assertEqual(con.execute("SELECT COUNT(*) FROM avoidable_dmg WHERE report_code=?", (rc,)).fetchone()[0], n_avoid)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM loot WHERE report_code=?", (rc,)).fetchone()[0], n_loot)
            # … and the week row is (re)written at the current schema version
            self.assertEqual(con.execute("SELECT schema_version FROM weeks WHERE report_code=?", (rc,)).fetchone()[0],
                             db_writer.SCHEMA_VERSION)
            con.close()

    def test_allow_downgrade_forces_the_write(self):
        with temp_db() as dbp:
            db_writer.write_week(self.rich, dbp)
            thin = dict(self.rich); thin["avoidableDmg"] = []
            db_writer.write_week(thin, dbp, allow_downgrade=True)   # forced — should not skip/raise
            con = sqlite3.connect(dbp)
            # forcing doesn't DELETE the avoidable rows (INSERT OR REPLACE doesn't clear), but the call
            # completes without the guard tripping — assert the week row still exists.
            self.assertIsNotNone(con.execute("SELECT 1 FROM weeks WHERE report_code=?",
                                             (self.rich["meta"]["report_code"],)).fetchone())
            con.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
