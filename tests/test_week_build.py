"""Tests for scripts/week_build.py — the loader is hermetic; finalize/commit use monkeypatching
so they assert orchestration (loot ingestion, enrich-before-write order, test-gating) without
touching WCL, the DB, or the real loot CSV.

Run:  python -m unittest discover -s tests
"""
import sys, io, json, contextlib, tempfile, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import week_build as wb


class TestExtractWeekData(unittest.TestCase):
    def test_brace_inside_string_value_does_not_break_match(self):
        # The exact E3 bug the old reprocess matcher had: a `{`/`}` inside a string value.
        html = ('junk\nconst WEEK_DATA = '
                '{"boss": "Lurker {Below}", "n": [1, 2], "o": {"a": "}"}};\nmore html')
        d = wb.extract_week_data(html)
        self.assertEqual(d["boss"], "Lurker {Below}")
        self.assertEqual(d["o"]["a"], "}")
        self.assertEqual(d["n"], [1, 2])

    def test_roundtrip_with_escapes(self):
        obj = {"meta": {"date": "x"}, "list": [{"k": 'a"b\\c'}], "z": [1, {"q": 2}]}
        html = "const WEEK_DATA = " + json.dumps(obj) + ";"
        self.assertEqual(wb.extract_week_data(html), obj)

    def test_missing_marker_raises(self):
        with self.assertRaises(ValueError):
            wb.extract_week_data("no marker here")


class TestFromSnapshot(unittest.TestCase):
    def test_loads_json_snapshot(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "w.json"
            p.write_text(json.dumps({"meta": {"kills": 1}}), encoding="utf-8")
            self.assertEqual(wb.from_snapshot(p)["meta"]["kills"], 1)

    def test_loads_injected_html(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "i.html"
            p.write_text('x const WEEK_DATA = {"meta":{"kills":2}}; y', encoding="utf-8")
            self.assertEqual(wb.from_snapshot(p)["meta"]["kills"], 2)


class TestFinalizeAndCommit(unittest.TestCase):
    """Monkeypatch the pipeline hooks so we test week_build's orchestration in isolation."""

    def setUp(self):
        import wcl_auto_dashboard as W
        import db_writer
        self.W, self.db = W, db_writer
        self._orig = {
            "reingest": W.reingest_loot, "dump": W.dump_week_data_cache,
            "enrich": W.enrich_with_trends, "write": db_writer.write_week,
            "inject": W.inject_into_html,
        }

    def tearDown(self):
        W, db = self.W, self.db
        W.reingest_loot = self._orig["reingest"]
        W.dump_week_data_cache = self._orig["dump"]
        W.enrich_with_trends = self._orig["enrich"]
        db.write_week = self._orig["write"]
        W.inject_into_html = self._orig["inject"]

    def test_finalize_ingests_loot_and_validates(self):
        seen = []
        self.W.reingest_loot = lambda mapped, csv_path=None: (seen.append(csv_path), mapped)[1]
        mapped = {"meta": {"kills": 10, "report_code": "T"}}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            out = wb.finalize_week(mapped, has_log=True, loot_csv="/some/path.csv")
        self.assertIs(out, mapped)                       # mutates in place, returns same object
        self.assertEqual(seen, ["/some/path.csv"])       # loot ingested via the one funnel
        self.assertIn("contract", buf.getvalue())        # validation ran

    def test_finalize_can_skip_validation(self):
        self.W.reingest_loot = lambda mapped, csv_path=None: mapped
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            wb.finalize_week({"meta": {}}, do_validate=False)
        self.assertEqual(buf.getvalue(), "")

    def test_commit_order_is_dump_enrich_write_render(self):
        order = []
        self.W.dump_week_data_cache = lambda m: order.append("dump")
        self.W.enrich_with_trends = lambda m, p: order.append("enrich")
        self.db.write_week = lambda m, p: order.append("write")
        self.W.inject_into_html = lambda wd, path, mapped=None: order.append("render")
        wb.commit_week({"meta": {"report_code": "T"}}, "fake.db", is_test=False, render=True)
        self.assertEqual(order, ["dump", "enrich", "write", "render"])  # enrich strictly before write

    def test_commit_test_db_skips_snapshot_dump(self):
        order = []
        self.W.dump_week_data_cache = lambda m: order.append("dump")
        self.W.enrich_with_trends = lambda m, p: order.append("enrich")
        self.db.write_week = lambda m, p: order.append("write")
        self.W.inject_into_html = lambda wd, path, mapped=None: order.append("render")
        wb.commit_week({"meta": {}}, "test.db", is_test=True, render=False)
        self.assertEqual(order, ["enrich", "write"])     # R4: --test-db must not dump the prod cache

    def test_commit_backfill_style_dump_only_no_db(self):
        order = []
        self.W.dump_week_data_cache = lambda m: order.append("dump")
        self.W.enrich_with_trends = lambda m, p: order.append("enrich")
        self.db.write_week = lambda m, p: order.append("write")
        wb.commit_week({"meta": {}}, "x.db", enrich=False, write_db=False, render=False)
        self.assertEqual(order, ["dump"])                # stays DB-read-only


if __name__ == "__main__":
    unittest.main(verbosity=2)
