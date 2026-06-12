"""sync_state.py — sync local cache/ pipeline state with the weekly workflow's `data` branch.

  python scripts/tools/sync_state.py pull   # data branch → local cache/  (after a cloud-run week)
  python scripts/tools/sync_state.py push   # local cache/ → data branch  (after local runs)
  python scripts/tools/sync_state.py check  # exit 1 if the data branch has state not pulled yet
                                            #   (run_weekly.bat runs this so a forgotten pull
                                            #    can't silently corrupt the trend chain)

The cloud workflow (.github/workflows/weekly.yml) and local runs share ONE canonical state:
the SQLite history DB + per-week snapshots, carried on the orphan `data` branch (main's
.gitignore blocks cache/, the orphan branch has no .gitignore). Discipline: after any week
that ran in the cloud, `pull` once before the next local run; optionally `push` after local
runs so the cloud is never behind. Pull OVERWRITES the local copies of the synced set; push
replaces the branch tip's cache/. Same allowlist as the workflow — the test DB and scratch
files never ship. pull/push record the synced data-branch commit in cache/.data_branch_sync;
`check` compares that marker against origin/data.
"""
import sys, shutil, subprocess, tempfile
from pathlib import Path

ROOT  = Path(__file__).resolve().parent.parent.parent
CACHE = ROOT / "cache"
MARKER = CACHE / ".data_branch_sync"   # origin/data commit the local cache/ last synced with
ALLOW_DIRS  = ("week_data", "wcl")
ALLOW_FILES = ("raid_history.db", "last_deploy.json",
               "item_crit_cache.json", "item_meta_cache.json")


def _git(*args, cwd=ROOT, check=True):
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if check and r.returncode != 0:
        sys.exit(f"git {' '.join(args)} failed:\n{(r.stderr or r.stdout).strip()}")
    return r


def _copy_state(src_cache: Path, dst_cache: Path):
    """Copy the allowlisted state set; returns a human list of what moved."""
    moved = []
    dst_cache.mkdir(parents=True, exist_ok=True)
    for d in sorted(ALLOW_DIRS):
        s = src_cache / d
        if s.is_dir():
            dst = dst_cache / d
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(s, dst)
            moved.append(f"{d}/  ({len(sorted(dst.iterdir()))} file(s))")
    for f in sorted(ALLOW_FILES):
        s = src_cache / f
        if s.is_file():
            shutil.copyfile(s, dst_cache / f)
            moved.append(f)
    return moved


def _mark_synced(sha: str):
    try:
        CACHE.mkdir(parents=True, exist_ok=True)
        MARKER.write_text(sha + "\n", encoding="utf-8")
    except Exception as e:
        print(f"  · could not record the sync marker: {e}")


def check() -> int:
    """0 = local state is current with origin/data (or there's nothing/no way to verify);
    1 = the data branch has commits the local cache/ hasn't pulled — a cloud run happened.
    Never blocks on network problems: an offline raid night must still run."""
    if _git("fetch", "origin", "data", check=False).returncode != 0:
        print("sync check: no data branch on origin (or fetch failed) — nothing to verify")
        return 0
    remote = _git("rev-parse", "origin/data").stdout.strip()
    local = MARKER.read_text(encoding="utf-8").strip() if MARKER.exists() else None
    if local == remote:
        print("sync check: local state is current with the data branch")
        return 0
    print("")
    print("  *** The data branch has state your local cache/ has NOT pulled. ***")
    print("  A cloud run happened since your last sync - running locally now would compute")
    print("  trends against a stale prior week and stage the rolling site without it.")
    print("  Fix:   python scripts/tools/sync_state.py pull")
    return 1


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode not in ("pull", "push", "check"):
        sys.exit(__doc__.strip())
    if mode == "check":
        sys.exit(check())
    has_remote = _git("fetch", "origin", "data", check=False).returncode == 0
    if mode == "pull" and not has_remote:
        sys.exit("no `data` branch on origin yet — nothing to pull")
    td = tempfile.mkdtemp(prefix="sync_state_")
    wt = Path(td) / "state"
    try:
        if has_remote:
            _git("worktree", "add", "--detach", str(wt), "origin/data")
        else:
            _git("worktree", "add", "--detach", str(wt))
        if mode == "pull":
            moved = _copy_state(wt / "cache", CACHE)
            _mark_synced(_git("rev-parse", "origin/data").stdout.strip())
            print("pulled from the data branch:\n  " + "\n  ".join(moved or ["(nothing)"]))
        else:
            if has_remote:
                _git("checkout", "-B", "data", "origin/data", cwd=wt)
            else:
                _git("checkout", "--orphan", "data", cwd=wt)
                _git("rm", "-rfq", ".", cwd=wt, check=False)
            state_cache = wt / "cache"
            if state_cache.exists():
                shutil.rmtree(state_cache)
            moved = _copy_state(CACHE, state_cache)
            (wt / "README.md").write_text(
                "Pipeline state for the weekly workflow (cache/ DB + snapshots).\n"
                "Real raid data — accepted exception; see .github/workflows/weekly.yml.\n",
                encoding="utf-8")
            _git("add", "-A", cwd=wt)
            c = _git("commit", "-m", "state: local sync", cwd=wt, check=False)
            if c.returncode != 0 and "nothing to commit" not in (c.stdout + c.stderr):
                sys.exit(f"commit failed:\n{(c.stderr or c.stdout).strip()}")
            _git("push", "origin", "data", cwd=wt)
            _mark_synced(_git("rev-parse", "HEAD", cwd=wt).stdout.strip())
            print("pushed to the data branch:\n  " + "\n  ".join(moved or ["(nothing)"]))
    finally:
        _git("worktree", "remove", "--force", str(wt), check=False)
        shutil.rmtree(td, ignore_errors=True)


if __name__ == "__main__":
    main()
