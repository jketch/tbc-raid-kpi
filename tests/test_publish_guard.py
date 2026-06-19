"""Tests for the publish layer: the deploy-time section-loss guard + the Netlify REST
deploy plumbing (config resolution, zip staging, upload/poll loop, skip-when-unconfigured).

Hermetic: builds fixtures in-process, fakes the HTTP layer — no Netlify, no network.

Run:  python -m unittest discover -s tests
"""
import io, json, os, sys, tempfile, unittest, zipfile
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import publish
import week_schema as ws


def complete_week(rc="W1", kills=10):
    """A WEEK_DATA with every contract section populated, on a killed week."""
    wk = {"meta": {"report_code": rc, "kills": kills, "start_ms": 1, "date": "d"}}
    for k, s in ws.SECTIONS.items():
        if s.tier == ws.META:
            continue
        if s.predicate is ws._has_players:
            wk[k] = {"players": [{"name": "X"}]}
        elif s.predicate is ws._has_batteries:
            wk[k] = {"batteries": [{"l": 1}]}
        elif s.predicate is ws._hs_nonempty:
            wk[k] = {"total_used": 1, "died_total": 1}
        elif k in ("roster", "boss_times", "boss_meta", "roleSpells", "playerSpells",
                   "avoidableMechanics", "healReaction", "debuffCoverage"):
            wk[k] = {"_": 1}
        else:
            wk[k] = [{"name": "X"}]
    return wk


class TestSectionLossGuard(unittest.TestCase):
    def test_complete_redeploy_has_no_reasons(self):
        self.assertEqual(publish._section_loss_guard(complete_week("A"), complete_week("A")), [])

    def test_same_week_redeploy_losing_loot_blocks(self):
        prev, new = complete_week("W1"), complete_week("W1")
        new["loot"] = {}                                    # the exact incident: loot dropped on re-deploy
        reasons = publish._section_loss_guard(prev, new)
        self.assertTrue(any("loot" in r for r in reasons), reasons)

    def test_same_week_redeploy_blanking_parse_pct_blocks(self):
        # field-coverage collapse INSIDE a still-populated section: parse % went all-null on a
        # re-deploy of the same report (the reprocess-over-fresh-WCL incident). The section stays
        # populated, so section-level regression() can't see it — only the coverage check can.
        prev, new = complete_week("W1"), complete_week("W1")
        prev["damageBySelection"]["players"] = [{"name": "X", "vs_replacement": 74}]
        prev["tankScorecard"] = [{"name": "T", "vs_replacement": 71}]
        new["damageBySelection"]["players"] = [{"name": "X"}]   # same players, parse value gone
        new["tankScorecard"] = [{"name": "T"}]
        reasons = publish._section_loss_guard(prev, new)
        self.assertTrue(any("parse" in r for r in reasons), reasons)

    def test_new_week_with_different_optional_sections_does_not_false_positive(self):
        # weekly deploys advance the latest week; a new week may legitimately have empty optional
        # sections (flawless = no deaths, dry night = no loot). Cross-week must NOT block.
        prev, new = complete_week("W1"), complete_week("W2")
        new["deaths"] = []      # optional WCL
        new["loot"] = {}        # external
        self.assertEqual(publish._section_loss_guard(prev, new), [])

    def test_broken_pipeline_blanks_a_required_wcl_section_blocks(self):
        new = complete_week("W3")
        new["damageBySelection"] = {"durations": {}, "players": []}   # the live DPS source, blank
        reasons = publish._section_loss_guard(None, new)
        self.assertTrue(any("damageBySelection" in r for r in reasons), reasons)

    def test_no_prev_and_complete_new_is_safe(self):
        self.assertEqual(publish._section_loss_guard(None, complete_week("W4")), [])


class TestFreshnessGuard(unittest.TestCase):
    """The freshness guard: refuse to deploy a latest week OLDER than the canonical current latest
    (origin/data + last deploy). Pure helper — no git, no network."""

    def test_older_latest_blocks(self):
        # the exact incident: staging Jun 08 (smaller start_ms) over a live/canonical Jun 15
        r = publish._stale_latest_reason(1780965731359, 1781570000000)
        self.assertIsNotNone(r)
        self.assertIn("OLDER", r)
        self.assertIn("sync_state.py pull", r)

    def test_same_latest_is_allowed(self):
        # re-deploying a BETTER version of the same week (equal start_ms) must NOT block
        self.assertIsNone(publish._stale_latest_reason(1780965731359, 1780965731359))

    def test_newer_latest_is_allowed(self):
        # the normal weekly advance (and the cloud run itself) — newer week, never blocked
        self.assertIsNone(publish._stale_latest_reason(1781570000000, 1780965731359))

    def test_unknown_baseline_skips(self):
        # offline dev / first-ever deploy: no canonical baseline → check skipped (fail-open)
        self.assertIsNone(publish._stale_latest_reason(1780965731359, None))
        self.assertIsNone(publish._stale_latest_reason(None, 1780965731359))


class TestNetlifyConfig(unittest.TestCase):
    """_netlify_config resolution: .env > env var; site id from NETLIFY_SITE_ID > state.json.
    load_env is monkeypatched so the developer's real .env never leaks into the tests."""

    def setUp(self):
        self._load_env, self._state = publish.load_env, publish.NETLIFY_STATE
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        for k in ("NETLIFY_AUTH_TOKEN", "NETLIFY_SITE_ID"):
            os.environ.pop(k, None)
        self.tmp = tempfile.TemporaryDirectory()
        publish.NETLIFY_STATE = Path(self.tmp.name) / "state.json"   # no state by default

    def tearDown(self):
        publish.load_env, publish.NETLIFY_STATE = self._load_env, self._state
        self._env.stop()
        self.tmp.cleanup()

    def test_token_plus_state_json(self):
        publish.load_env = lambda: {"NETLIFY_AUTH_TOKEN": "tok"}
        publish.NETLIFY_STATE.write_text(json.dumps({"siteId": "site-1"}), encoding="utf-8")
        self.assertEqual(publish._netlify_config(), ("tok", "site-1"))

    def test_no_token_is_unconfigured(self):
        publish.load_env = lambda: {}
        publish.NETLIFY_STATE.write_text(json.dumps({"siteId": "site-1"}), encoding="utf-8")
        self.assertIsNone(publish._netlify_config())

    def test_no_site_anywhere_is_unconfigured(self):
        publish.load_env = lambda: {"NETLIFY_AUTH_TOKEN": "tok"}
        self.assertIsNone(publish._netlify_config())

    def test_env_var_site_id_works_without_state_json(self):
        publish.load_env = lambda: {}
        os.environ["NETLIFY_AUTH_TOKEN"] = "tok"
        os.environ["NETLIFY_SITE_ID"] = "site-ci"
        self.assertEqual(publish._netlify_config(), ("tok", "site-ci"))

    def test_dotenv_site_id_beats_state_json(self):
        publish.load_env = lambda: {"NETLIFY_AUTH_TOKEN": "tok", "NETLIFY_SITE_ID": "site-env"}
        publish.NETLIFY_STATE.write_text(json.dumps({"siteId": "site-state"}), encoding="utf-8")
        self.assertEqual(publish._netlify_config(), ("tok", "site-env"))


class TestZipDeployDir(unittest.TestCase):
    def test_relative_sorted_arcnames_and_content(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "weeks").mkdir()
            (d / "index.html").write_text("<html>latest</html>", encoding="utf-8")
            (d / "weeks" / "B.json").write_text('{"b": 1}', encoding="utf-8")
            (d / "weeks" / "A.json").write_text('{"a": 1}', encoding="utf-8")
            blob = publish._zip_deploy_dir(d)
            with zipfile.ZipFile(io.BytesIO(blob)) as zf:
                self.assertEqual(zf.namelist(), ["index.html", "weeks/A.json", "weeks/B.json"])
                self.assertEqual(zf.read("index.html").decode("utf-8"), "<html>latest</html>")
                self.assertEqual(zf.read("weeks/A.json").decode("utf-8"), '{"a": 1}')


class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code, self.text = payload, status, json.dumps(payload)
    def json(self):
        return self._payload


class TestNetlifyDeployPoll(unittest.TestCase):
    """The upload/poll loop, with the HTTP layer faked via the _post/_get seams."""

    def test_processing_then_ready_returns_ssl_url(self):
        polls = iter([_FakeResp({"id": "d1", "state": "processing"}),
                      _FakeResp({"id": "d1", "state": "ready", "ssl_url": "https://x.netlify.app"})])
        with mock.patch.object(publish.time, "sleep"):
            url = publish._netlify_deploy(
                "tok", "site", b"zip",
                _post=lambda *a, **k: _FakeResp({"id": "d1", "state": "uploading"}),
                _get=lambda *a, **k: next(polls))
        self.assertEqual(url, "https://x.netlify.app")

    def test_error_state_returns_none(self):
        with mock.patch.object(publish.time, "sleep"):
            url = publish._netlify_deploy(
                "tok", "site", b"zip",
                _post=lambda *a, **k: _FakeResp({"id": "d1", "state": "uploading"}),
                _get=lambda *a, **k: _FakeResp({"id": "d1", "state": "error",
                                                "error_message": "boom"}))
        self.assertIsNone(url)

    def test_http_failure_returns_none(self):
        url = publish._netlify_deploy("tok", "site", b"zip",
                                      _post=lambda *a, **k: _FakeResp({"msg": "nope"}, status=401),
                                      _get=lambda *a, **k: self.fail("must not poll after a failed upload"))
        self.assertIsNone(url)

    def test_deadline_expiry_returns_none(self):
        # timeout_s=0 ⇒ the first deadline check fires before any sleep/poll
        url = publish._netlify_deploy("tok", "site", b"zip", timeout_s=0,
                                      _post=lambda *a, **k: _FakeResp({"id": "d1", "state": "uploading"}),
                                      _get=lambda *a, **k: self.fail("must not poll past the deadline"))
        self.assertIsNone(url)


class TestDeploySkip(unittest.TestCase):
    def test_unconfigured_skips_before_staging(self):
        orig_cfg, orig_load = publish._netlify_config, publish._load_last_deploy
        touched = []
        publish._netlify_config = lambda: None
        publish._load_last_deploy = lambda: touched.append("staged")   # first call inside the try
        try:
            self.assertIsNone(publish.deploy_netlify())
            self.assertEqual(touched, [], "unconfigured deploy must return before staging")
        finally:
            publish._netlify_config, publish._load_last_deploy = orig_cfg, orig_load


if __name__ == "__main__":
    unittest.main(verbosity=2)
