"""log_discovery.py — find the right combat log for a report, then archive the consumed one.

The WoW client always appends WoWCombatLog*.txt files into its Logs\\ folder (an addon like
LoggerheadLite only toggles logging on, never the destination), so when WOW_LOG_DIR is set in
.env the weekly run can read the log straight from the game — no manual copy into logs/.
Several files can sit there (alt sessions, multiple raid nights), so selection is a MATCH
against the WCL report's time window, not a newest-file guess: sniff each candidate's
first/last timestamps cheaply and pick the file covering the window.

Pure leaf: stdlib + combat_log's _parse_ts/_open_log. Consumed by wcl_auto_dashboard.main().
"""
from __future__ import annotations

import time, zipfile
from collections import deque
from datetime import datetime
from pathlib import Path

from combat_log import _parse_ts, _open_log

HEAD_BYTES = 64 * 1024     # sniff window at each end of a .txt candidate
TAIL_BYTES = 64 * 1024
# A candidate must overlap at least this fraction of the report window. Strict covering would
# false-negative the real case of a logger who zoned in minutes after the WCL uploader's log
# started; an overlap fraction also keeps disjoint files (another night's raid) at score 0.
# A multi-raid mega-file scores 1.0 and is fine — parse_combat_log scopes to the report's bosses.
MATCH_FLOOR = 0.70
LOG_PATTERNS = ("WoWCombatLog*.txt", "WoWCombatLog*.zip", "WoWCombatLog*.gz")


def to_log_scale(epoch_ms) -> float:
    """Epoch ms → the scale combat_log._parse_ts returns (LOCAL-wall-clock seconds anchored on
    date.toordinal()). Log stamps are the player's local clock; WCL report times are epoch ms —
    this is the load-bearing conversion that lets the two be compared."""
    dt = datetime.fromtimestamp(epoch_ms / 1000)   # local tz, matching what the game writes
    return (dt.toordinal() * 86400 + dt.hour * 3600 + dt.minute * 60
            + dt.second + dt.microsecond / 1e6)


def sniff_log_range(path: Path):
    """(first_ts, last_ts) on the _parse_ts scale, or None for empty/unparsable files.
    .txt: read a head chunk + a tail chunk (two seeks — no full read of a 180 MB file).
    .zip/.gz: stream-decompress once, keeping the first lines + a rolling tail."""
    path = Path(path)
    try:
        if path.suffix.lower() == ".txt":
            size = path.stat().st_size
            if size == 0:
                return None
            with open(path, "rb") as f:
                head_lines = f.read(HEAD_BYTES).decode("utf-8", errors="replace").splitlines()
                f.seek(max(0, size - TAIL_BYTES))
                tail_lines = f.read().decode("utf-8", errors="replace").splitlines()
            if size > TAIL_BYTES and tail_lines:
                tail_lines = tail_lines[1:]        # first tail line is likely a partial
        else:
            head_lines, tail = [], deque(maxlen=200)
            with _open_log(str(path)) as f:
                for line in f:
                    if len(head_lines) < 200:
                        head_lines.append(line)
                    tail.append(line)
            tail_lines = list(tail)
    except Exception:
        return None
    first = next((ts for ln in head_lines if (ts := _parse_ts(ln)) > 0), None)
    last  = next((ts for ln in reversed(tail_lines) if (ts := _parse_ts(ln)) > 0), None)
    if first is None or last is None or last < first:
        return None
    return first, last


def discover_log(start_ms, end_ms, wow_dir):
    """Pick the WoWCombatLog file in `wow_dir` that covers the report window [start_ms, end_ms].
    Returns the path as a str, or None (caller falls back to logs/). Prints a decision trace
    so the operator sees what was matched and why."""
    wow_dir = Path(wow_dir)
    if not wow_dir.is_dir():
        print(f"  [LOG] WOW_LOG_DIR does not exist: {wow_dir}")
        return None
    S, E = to_log_scale(start_ms), to_log_scale(end_ms)
    window = max(E - S, 1.0)
    files = sorted(p for pat in LOG_PATTERNS for p in wow_dir.glob(pat))
    matches = []
    for p in files:
        rng = sniff_log_range(p)
        if not rng:
            continue
        f, l = rng
        score = max(0.0, min(E, l) - max(S, f)) / window
        if score >= MATCH_FLOOR:
            matches.append((score, l - f, p.name, p))
    if not matches:
        print(f"  [LOG] no file in {wow_dir} covers the report window "
              f"(scanned {len(files)} candidate(s))")
        return None
    # best score; ties → smallest time span (the most specific file), then name (determinism)
    matches.sort(key=lambda m: (-m[0], m[1], m[2]))
    score, _span, _name, best = matches[0]
    w0 = time.strftime("%m/%d %H:%M", time.localtime(start_ms / 1000))
    w1 = time.strftime("%H:%M", time.localtime(end_ms / 1000))
    print(f"  ✓ matched {best.name} — covers {w0}–{w1} ({round(score * 100)}% of the report window)")
    for m in matches[1:]:
        print(f"  · also matched (not picked): {m[3].name} ({round(m[0] * 100)}%)")
    return str(best)


def archive_log(log_path, report_code, start_ms, archive_dir):
    """Zip the consumed log into archive_dir (WoWCombatLog-YYYYMMDD-<code>.zip), verify the
    archive, and only then remove the original — never plain-deletes. Any failure (including
    WoW still holding the file open) keeps the original and cleans up the partial zip.
    The archives are real inputs later: backfill_snapshots --log replays them."""
    log_path, archive_dir = Path(log_path), Path(archive_dir)
    target = None
    try:
        size = log_path.stat().st_size
        datestr = time.strftime("%Y%m%d", time.localtime(start_ms / 1000))
        archive_dir.mkdir(parents=True, exist_ok=True)
        target = archive_dir / f"WoWCombatLog-{datestr}-{report_code}.zip"
        n = 2
        while target.exists():                      # re-run: keep both, never overwrite
            target = archive_dir / f"WoWCombatLog-{datestr}-{report_code}-{n}.zip"
            n += 1
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(log_path, log_path.name)
        with zipfile.ZipFile(target) as zf:
            if zf.testzip() is not None or zf.getinfo(log_path.name).file_size != size:
                raise ValueError("zip verification failed")
        log_path.unlink()
        print(f"  ✓ combat log archived → {target}")
        return target
    except Exception as e:
        print(f"  ⚠ log archive skipped ({e}) — original kept: {log_path}")
        if target is not None:
            try:
                target.unlink(missing_ok=True)      # don't leave a bad/orphaned zip behind
            except Exception:
                pass
        return None
