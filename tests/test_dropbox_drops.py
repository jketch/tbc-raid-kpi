"""Tests for tools/dropbox_drops.py — the cloud input-drop fetch/archive plumbing.

Hermetic: the HTTP layer is faked through the `_post` seam (style of TestNetlifyDeployPoll);
no Dropbox, no network. Routing targets (DROPS_DIR/LOOT_DIR) are repointed into tempdirs.

Run:  python -m unittest discover -s tests
"""
import json, os, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "tools"))
import dropbox_drops as dd


class _Resp:
    def __init__(self, payload=None, status=200, content=b""):
        self._payload = payload if payload is not None else {}
        self.status_code, self.content = status, content
        self.text = json.dumps(self._payload)
    def json(self):
        return self._payload


def _entry(name, modified="2026-06-12T01:00:00Z"):
    return {".tag": "file", "name": name, "path_lower": f"/{name.lower()}",
            "server_modified": modified}


class _FakeDropbox:
    """Routes _post calls by URL; records download/move order."""
    def __init__(self, entries, pages=None, fail_download=None, folder_conflict=False):
        self.entries, self.pages = entries, pages or []
        self.fail_download, self.folder_conflict = fail_download, folder_conflict
        self.downloads, self.moves, self.calls = [], [], []

    def __call__(self, url, headers=None, data=None, auth=None, timeout=None):
        self.calls.append(url)
        if url.endswith("/oauth2/token"):
            return _Resp({"access_token": "AT"})
        if url.endswith("/files/list_folder"):
            if self.pages:
                return _Resp({"entries": self.entries, "has_more": True, "cursor": "c1"})
            return _Resp({"entries": self.entries, "has_more": False})
        if url.endswith("/files/list_folder/continue"):
            page = self.pages.pop(0)
            return _Resp({"entries": page, "has_more": bool(self.pages), "cursor": "c2"})
        if url.endswith("/files/download"):
            name = json.loads(headers["Dropbox-API-Arg"])["path"]
            if self.fail_download and self.fail_download in name:
                return _Resp({"error": "boom"}, status=500)
            self.downloads.append(name)
            return _Resp(content=f"data:{name}".encode())
        if url.endswith("/files/create_folder_v2"):
            return _Resp({"error": "conflict"}, status=409) if self.folder_conflict else _Resp({})
        if url.endswith("/files/move_v2"):
            self.moves.append(json.loads(data)["from_path"])
            return _Resp({})
        raise AssertionError(f"unexpected url {url}")


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._dirs = dd.DROPS_DIR, dd.LOOT_DIR
        dd.DROPS_DIR = Path(self.tmp.name) / "drops"
        dd.LOOT_DIR  = Path(self.tmp.name) / "loot"
        self._env = mock.patch.dict(os.environ, {"DROPBOX_APP_KEY": "k",
                                                 "DROPBOX_APP_SECRET": "s",
                                                 "DROPBOX_REFRESH_TOKEN": "r"})
        self._env.start()

    def tearDown(self):
        dd.DROPS_DIR, dd.LOOT_DIR = self._dirs
        self._env.stop()
        self.tmp.cleanup()


class TestFetch(_Base):
    def test_unconfigured_is_clean_noop(self):
        for k in ("DROPBOX_APP_KEY", "DROPBOX_APP_SECRET", "DROPBOX_REFRESH_TOKEN"):
            os.environ.pop(k, None)
        boom = lambda *a, **k: self.fail("must not touch the network when unconfigured")  # noqa: E731
        self.assertEqual(dd.fetch(_post=boom), 0)

    def test_routing_by_extension(self):
        fake = _FakeDropbox([_entry("WoWCombatLog-0608.zip"), _entry("loot.csv"),
                             _entry("screenshot.png")])
        self.assertEqual(dd.fetch(_post=fake), 0)
        self.assertTrue((dd.DROPS_DIR / "WoWCombatLog-0608.zip").exists())
        self.assertTrue((dd.LOOT_DIR / "loot.csv").exists())
        self.assertEqual(len(fake.downloads), 2)   # the .png was ignored, not downloaded

    def test_paging_follows_cursor(self):
        fake = _FakeDropbox([_entry("a.zip")], pages=[[_entry("b.csv")]])
        self.assertEqual(dd.fetch(_post=fake), 0)
        self.assertTrue((dd.DROPS_DIR / "a.zip").exists())
        self.assertTrue((dd.LOOT_DIR / "b.csv").exists())
        self.assertIn(f"{dd.API}/files/list_folder/continue", fake.calls)

    def test_downloads_oldest_first_so_newest_wins_mtime(self):
        fake = _FakeDropbox([_entry("new.csv", "2026-06-12T02:00:00Z"),
                             _entry("old.csv", "2026-06-01T02:00:00Z")])
        self.assertEqual(dd.fetch(_post=fake), 0)
        self.assertEqual(fake.downloads, ["/old.csv", "/new.csv"])

    def test_failed_download_aborts_with_exit_1(self):
        fake = _FakeDropbox([_entry("a.zip"), _entry("b.csv")], fail_download="a.zip")
        self.assertEqual(dd.fetch(_post=fake), 1)

    def test_token_failure_is_exit_1(self):
        post = lambda url, **k: _Resp({"error": "bad"}, status=400)  # noqa: E731
        self.assertEqual(dd.fetch(_post=post), 1)

    def test_empty_folder_is_fine(self):
        self.assertEqual(dd.fetch(_post=_FakeDropbox([])), 0)


class TestArchive(_Base):
    def test_moves_everything_and_tolerates_folder_conflict(self):
        fake = _FakeDropbox([_entry("a.zip"), _entry("b.csv")], folder_conflict=True)
        self.assertEqual(dd.archive(_post=fake), 0)
        self.assertEqual(sorted(fake.moves), ["/a.zip", "/b.csv"])

    def test_empty_folder_archives_nothing(self):
        fake = _FakeDropbox([])
        self.assertEqual(dd.archive(_post=fake), 0)
        self.assertEqual(fake.moves, [])

    def test_unconfigured_is_clean_noop(self):
        for k in ("DROPBOX_APP_KEY", "DROPBOX_APP_SECRET", "DROPBOX_REFRESH_TOKEN"):
            os.environ.pop(k, None)
        boom = lambda *a, **k: self.fail("must not touch the network when unconfigured")  # noqa: E731
        self.assertEqual(dd.archive(_post=boom), 0)

    def test_token_failure_never_fails_the_job(self):
        post = lambda url, **k: _Resp({"error": "bad"}, status=400)  # noqa: E731
        self.assertEqual(dd.archive(_post=post), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
