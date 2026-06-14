"""Tests for build_site.build — the rolling multi-week .deploy staging (zero WCL).

Hermetic: monkeypatch enrich_with_trends + finalize_week (like test_week_build), point the snapshot
cache / dashboard HTML / .deploy at tempdirs, and capture the ✓/· prints (cp1252-unsafe). Covers the
build_site cases — newest-WINDOW selection + start_ms ordering, the latest
week embedded VERBATIM from the HTML while earlier weeks are enriched+finalized, the WEEKS_INDEX
manifest shape/injection, the stale weeks/ cleanup, and _load_snapshots skipping bad snapshots."""
import contextlib, io, json, sys, tempfile, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import build_site as bs
import wcl_auto_dashboard as W
import week_build as wb


def _run_build():
    with contextlib.redirect_stdout(io.StringIO()):   # build() prints ✓/· — crashes cp1252
        bs.build()


class TestBuildSite(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.cache = root / "week_data"; self.cache.mkdir()
        self.deploy = root / ".deploy"
        self.weeks = self.deploy / "weeks"
        self.dash = root / "dash.html"

        # 8 weeks in the cache; the newest (8000) is the LATEST and also lives embedded in the HTML.
        for s in (8000, 7000, 6000, 5000, 4000, 3000, 2000, 1000):
            code = "LATEST" if s == 8000 else f"W{s}"
            self._snap({"report_code": code, "start_ms": s, "date": f"d{s}"}, extra={"_from": "cache"})
        # The HTML embeds the LATEST week with a DIFFERENT marker so we can prove "embedded verbatim".
        embedded = {"meta": {"report_code": "LATEST", "start_ms": 8000, "date": "d8000"},
                    "_from": "html"}
        self.dash.write_text("<html>const WEEK_DATA = " + json.dumps(embedded)
                             + ";\nconst WEEKS_INDEX = [];</html>", encoding="utf-8")

        # Patch the module/global seams build() reaches; record enrich/finalize without touching DB/loot.
        self.enriched, self.finalized = [], []
        self._orig = {"DEPLOY_DIR": bs.DEPLOY_DIR, "WEEKS_DIR": bs.WEEKS_DIR,
                      "CACHE": W.WEEK_DATA_CACHE, "DASH": W.DASH_FILE,
                      "enrich": W.enrich_with_trends, "finalize": wb.finalize_week}
        bs.DEPLOY_DIR, bs.WEEKS_DIR = self.deploy, self.weeks
        W.WEEK_DATA_CACHE, W.DASH_FILE = self.cache, self.dash
        W.enrich_with_trends = lambda wd, p: (self.enriched.append(wd["meta"]["report_code"]), wd)[1]
        wb.finalize_week = lambda wd, *a, **k: (self.finalized.append(wd["meta"]["report_code"]), wd)[1]

    def tearDown(self):
        bs.DEPLOY_DIR, bs.WEEKS_DIR = self._orig["DEPLOY_DIR"], self._orig["WEEKS_DIR"]
        W.WEEK_DATA_CACHE, W.DASH_FILE = self._orig["CACHE"], self._orig["DASH"]
        W.enrich_with_trends, wb.finalize_week = self._orig["enrich"], self._orig["finalize"]

    def _snap(self, meta, extra=None):
        wd = {"meta": meta, **(extra or {})}
        (self.cache / f"{meta['report_code']}.json").write_text(json.dumps(wd), encoding="utf-8")

    def _manifest(self):
        html = (self.deploy / "index.html").read_text(encoding="utf-8")
        i = html.index("const WEEKS_INDEX = ")
        return json.loads(html[i + len("const WEEKS_INDEX = "):html.index(";", i)])

    # ---- cases ----

    def test_window_cap_and_ordering(self):
        _run_build()
        manifest = self._manifest()
        self.assertEqual([m["start_ms"] for m in manifest], [8000, 7000, 6000, 5000, 4000, 3000])
        self.assertEqual([m["report"] for m in manifest],
                         ["LATEST", "W7000", "W6000", "W5000", "W4000", "W3000"])
        # the two oldest weeks are dropped from BOTH the manifest and weeks/ (WINDOW=6)
        self.assertFalse((self.weeks / "W2000.json").exists())
        self.assertFalse((self.weeks / "W1000.json").exists())
        self.assertEqual(len(list(self.weeks.glob("*.json"))), 6)

    def test_latest_embedded_verbatim_earlier_enriched(self):
        _run_build()
        latest = json.loads((self.weeks / "LATEST.json").read_text(encoding="utf-8"))
        self.assertEqual(latest["_from"], "html")        # taken from the HTML, NOT the cache snapshot
        # the latest is NOT re-enriched/finalized; the 5 earlier published weeks ARE
        self.assertNotIn("LATEST", self.enriched)
        self.assertNotIn("LATEST", self.finalized)
        self.assertEqual(set(self.enriched), {"W7000", "W6000", "W5000", "W4000", "W3000"})
        self.assertEqual(set(self.finalized), {"W7000", "W6000", "W5000", "W4000", "W3000"})

    def test_weeks_index_manifest_shape_and_injection(self):
        _run_build()
        html = (self.deploy / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("const WEEKS_INDEX = [];", html)   # placeholder replaced
        manifest = self._manifest()
        self.assertEqual(len(manifest), 6)
        for m in manifest:
            self.assertEqual(set(m), {"report", "date", "start_ms"})

    def test_stale_weeks_dir_is_cleaned(self):
        self.weeks.mkdir(parents=True)
        (self.weeks / "STALE.json").write_text("{}", encoding="utf-8")
        _run_build()
        self.assertFalse((self.weeks / "STALE.json").exists())   # fresh rmtree + mkdir each build

    def test_load_snapshots_skips_no_start_ms_and_malformed(self):
        self._snap({"report_code": "NOSTART", "date": "x"})      # start_ms missing → excluded
        (self.cache / "broken.json").write_text("{not json", encoding="utf-8")  # unreadable → skipped
        with contextlib.redirect_stdout(io.StringIO()):
            weeks = bs._load_snapshots()
        codes = {w["meta"]["report_code"] for w in weeks}
        self.assertNotIn("NOSTART", codes)
        self.assertEqual(codes, {"LATEST", "W7000", "W6000", "W5000", "W4000", "W3000"})  # good top-6


if __name__ == "__main__":
    unittest.main(verbosity=2)
