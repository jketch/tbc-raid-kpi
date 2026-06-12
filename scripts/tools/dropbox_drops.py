"""dropbox_drops.py — weekly inputs from a Dropbox app folder, for full-fat CLOUD runs.

  python scripts/tools/dropbox_drops.py fetch     # app folder → drops/ (logs) + loot/ (CSVs)
  python scripts/tools/dropbox_drops.py archive   # sweep the app folder root → /processed/

The weekly workflow runs `fetch` before the pipeline: a zipped combat log lands in drops/
(which the workflow points WOW_LOG_DIR at, so log_discovery window-matches it exactly like a
local run) and a ThatsBIS loot CSV lands in loot/ (newest-CSV auto-pick + raid-date filter).
After a successful non-test run, `archive` MOVES everything in the app-folder root to
/processed/ (never deletes — recoverable in Dropbox). Unset secrets ⇒ both modes no-op
cleanly, so the workflow degrades to the log-less week it produced before this existed.

Auth: a Scoped-access "App folder" Dropbox app; per run the refresh token is exchanged for a
short-lived access token. Env (repo secrets in CI): DROPBOX_APP_KEY, DROPBOX_APP_SECRET,
DROPBOX_REFRESH_TOKEN. One-time setup steps are in README.md (Cloud run section).
"""
import os, sys, json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

DROPS_DIR = ROOT / "drops"     # zipped/raw combat logs → WOW_LOG_DIR target in CI
LOOT_DIR  = ROOT / "loot"      # ThatsBIS CSVs → main()'s newest-CSV auto-pick
API       = "https://api.dropboxapi.com/2"
CONTENT   = "https://content.dropboxapi.com/2"
TOKEN_URL = "https://api.dropboxapi.com/oauth2/token"
LOG_EXTS  = (".zip", ".gz", ".txt")
PROCESSED = "/processed"


def _cfg():
    """(app_key, app_secret, refresh_token) or None when the feature is unconfigured."""
    k, s, r = (os.environ.get(x) for x in
               ("DROPBOX_APP_KEY", "DROPBOX_APP_SECRET", "DROPBOX_REFRESH_TOKEN"))
    return (k, s, r) if (k and s and r) else None


def _real_post():
    import wcl_client  # noqa: F401 — auto-installs requests if missing (the blessed bootstrap)
    import requests
    return requests.post


def _access_token(cfg, _post):
    """Refresh-token → short-lived access token (per run)."""
    k, s, r = cfg
    resp = _post(TOKEN_URL, data={"grant_type": "refresh_token", "refresh_token": r},
                 auth=(k, s), timeout=30)
    if resp.status_code >= 300:
        print(f"  ⚠ Dropbox token exchange failed (HTTP {resp.status_code}): {resp.text[-300:]}")
        return None
    return resp.json().get("access_token")


def _hdrs(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _list_root(token, _post):
    """Every FILE in the app-folder root (paged; /processed and other folders excluded)."""
    entries, endpoint, body = [], "files/list_folder", {"path": ""}
    while True:
        r = _post(f"{API}/{endpoint}", headers=_hdrs(token), data=json.dumps(body), timeout=30)
        if r.status_code >= 300:
            raise RuntimeError(f"list_folder failed (HTTP {r.status_code}): {r.text[-300:]}")
        j = r.json()
        entries.extend(e for e in j.get("entries", []) if e.get(".tag") == "file")
        if not j.get("has_more"):
            return entries
        endpoint, body = "files/list_folder/continue", {"cursor": j.get("cursor")}


def fetch(_post=None) -> int:
    """Download the drop folder's inputs: log files → drops/, CSVs → loot/. Exit 1 only on a
    mid-fetch failure (a half-fetched week must not run as if it were complete)."""
    cfg = _cfg()
    if not cfg:
        print("  Dropbox drops not configured — skipping "
              "(set DROPBOX_APP_KEY / DROPBOX_APP_SECRET / DROPBOX_REFRESH_TOKEN)")
        return 0
    if _post is None:
        _post = _real_post()
    token = _access_token(cfg, _post)
    if not token:
        return 1
    try:
        files = _list_root(token, _post)
    except Exception as e:
        print(f"  ⚠ Dropbox list failed: {e}")
        return 1
    # oldest-first by server time, so the NEWEST server file gets the newest local mtime —
    # main()'s newest-CSV auto-pick then chooses the most recently dropped loot file.
    files.sort(key=lambda e: (e.get("server_modified") or "", e.get("name") or ""))
    n = 0
    for e in files:
        name = e.get("name") or ""
        ext = Path(name).suffix.lower()
        if ext in LOG_EXTS:
            dst_dir = DROPS_DIR
        elif ext == ".csv":
            dst_dir = LOOT_DIR
        else:
            print(f"  · ignoring {name} (not a log/CSV)")
            continue
        r = _post(f"{CONTENT}/files/download",
                  headers={"Authorization": f"Bearer {token}",
                           "Dropbox-API-Arg": json.dumps({"path": e.get("path_lower") or f"/{name}"})},
                  timeout=600)
        if r.status_code >= 300:
            print(f"  ⚠ download failed for {name} (HTTP {r.status_code}) — aborting fetch")
            return 1
        dst_dir.mkdir(parents=True, exist_ok=True)
        (dst_dir / name).write_bytes(r.content)
        print(f"  ✓ fetched {name} → {dst_dir.name}/ ({len(r.content):,} bytes)")
        n += 1
    if n == 0:
        print("  · drop folder empty — running without dropped inputs")
    return 0


def archive(_post=None) -> int:
    """Sweep every file in the app-folder root to /processed/ (move + autorename, never
    delete). Best-effort: the run already succeeded — cleanup failures only warn (exit 0)."""
    cfg = _cfg()
    if not cfg:
        print("  Dropbox drops not configured — nothing to archive")
        return 0
    if _post is None:
        _post = _real_post()
    token = _access_token(cfg, _post)
    if not token:
        return 0
    try:
        files = _list_root(token, _post)
    except Exception as e:
        print(f"  ⚠ Dropbox list failed: {e} — drops left in place")
        return 0
    if not files:
        print("  · drop folder already empty")
        return 0
    # ensure /processed exists; 409 (already exists) is the normal steady state
    r = _post(f"{API}/files/create_folder_v2", headers=_hdrs(token),
              data=json.dumps({"path": PROCESSED}), timeout=30)
    if r.status_code >= 300 and r.status_code != 409:
        print(f"  ⚠ could not ensure {PROCESSED}/ (HTTP {r.status_code}) — drops left in place")
        return 0
    moved = 0
    for e in sorted(files, key=lambda x: x.get("name") or ""):
        name = e.get("name") or ""
        r = _post(f"{API}/files/move_v2", headers=_hdrs(token),
                  data=json.dumps({"from_path": e.get("path_lower") or f"/{name}",
                                   "to_path": f"{PROCESSED}/{name}", "autorename": True}),
                  timeout=30)
        if r.status_code >= 300:
            print(f"  ⚠ could not archive {name} (HTTP {r.status_code})")
        else:
            moved += 1
    print(f"  ✓ archived {moved}/{len(files)} drop(s) → {PROCESSED.strip('/')}/")
    return 0


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "fetch":
        sys.exit(fetch())
    if mode == "archive":
        sys.exit(archive())
    sys.exit(__doc__.strip())


if __name__ == "__main__":
    main()
