#!/usr/bin/env python
"""One command that makes the spine's invariants SELF-DEFENDING.

The 2026-06-10 refactor (see week_build.py / week_schema.py) made producer-drift
loud at runtime — but nothing *ran* the checks. This does, in one call:

  1. UNIT TESTS  — the hermetic tests/ suite (stdlib unittest; no pytest dep).
  2. CONTRACT CHARACTERIZATION — for every prod snapshot in cache/week_data/*.json,
     fingerprint which contract sections are populated (per week_schema) and diff
     against a recorded golden (tests/characterization.json). A producer/mapper
     change that silently drops a section (the loot/trend incidents) fails HERE,
     before commit — instead of on the live dashboard.

Zero WCL, zero DB writes, read-only on prod data. Deterministic and fast.

Usage:
    python scripts/check.py            # run both; exit 1 on any failure (CI / hook)
    python scripts/check.py --bless    # re-record the characterization golden, then run
    python scripts/check.py --tests    # unit tests only
    python scripts/check.py --char     # characterization only

When the golden legitimately changes (you ADDED a section, or a week genuinely has
new data), re-run with --bless and commit tests/characterization.json alongside.
"""
import sys, json, io, unittest, contextlib
from pathlib import Path

# Windows cp1252 console can't encode the ✓/⚠/✗ glyphs we print — force UTF-8.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import week_schema as ws
import week_build as wb

GOLDEN = ROOT / "tests" / "characterization.json"
CACHE  = ROOT / "cache" / "week_data"


def _fingerprint() -> dict:
    """{report_code: {"populated": [...], "keys": [...]}} over every prod snapshot.

    `populated` = contract sections with meaningful data; `keys` = every contract section the
    producer actually EMITTED (present as a key, even when empty). Tracking BOTH closes a gap:
    a section whose data was empty when blessed isn't in `populated`, so a mapper that stops
    emitting it entirely would leave the populated fingerprint unchanged — but the key set drops,
    so the silent loss is caught. Pure read of what the PRODUCERS wrote."""
    fp = {}
    for f in sorted(CACHE.glob("*.json")):
        try:
            wd = wb.from_snapshot(f)
        except Exception as e:
            print(f"  ⚠ skip {f.name}: {e}")
            continue
        code = (wd.get("meta") or {}).get("report_code") or f.stem
        fp[code] = {
            "populated": sorted(ws.populated_sections(wd)),
            "keys":      sorted(k for k, s in ws.SECTIONS.items() if s.tier != ws.META and k in wd),
        }
    return fp


def _norm(entry):
    """Normalize a golden/current entry to (populated:set, keys:set|None). Tolerates the
    legacy list format (populated-only) so an un-reblessed golden still runs the populated diff."""
    if isinstance(entry, dict):
        keys = entry.get("keys")
        return set(entry.get("populated", [])), (set(keys) if keys is not None else None)
    return set(entry), None


def run_tests() -> bool:
    print("── unit tests ──")
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=str(ROOT / "tests"), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    ok = result.wasSuccessful()
    print(f"  {'✓' if ok else '✗'} {result.testsRun} tests, "
          f"{len(result.failures)} failed, {len(result.errors)} errored")
    return ok


def bless() -> None:
    fp = _fingerprint()
    GOLDEN.write_text(json.dumps(fp, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"  ✓ blessed {len(fp)} week(s) → {GOLDEN.relative_to(ROOT)}")


def run_char() -> bool:
    print("── contract characterization ──")
    if not GOLDEN.exists():
        print(f"  ⚠ no golden at {GOLDEN.relative_to(ROOT)} — run `check.py --bless` once to record it")
        return False
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    current = _fingerprint()
    ok = True

    missing = set(golden) - set(current)
    added   = set(current) - set(golden)
    if missing:
        ok = False
        for c in sorted(missing):
            print(f"  ✗ week {c} present in golden but GONE from cache/week_data")
    if added:
        # a new week is normal (weekly run) — informational, not a failure, but nudge to bless.
        for c in sorted(added):
            print(f"  · new week {c} not in golden (run --bless after verifying it)")

    legacy_golden = False
    for code in sorted(set(golden) & set(current)):
        was, now = _norm(golden[code])
        now_pop, now_keys = _norm(current[code])
        dropped, gained = was - now_pop, now_pop - was
        if dropped:
            ok = False
            print(f"  ✗ {code}: section(s) DROPPED → {', '.join(sorted(dropped))}")
        if gained:
            ok = False  # a section appearing is also drift vs the recorded contract — bless to accept
            print(f"  ✗ {code}: section(s) APPEARED → {', '.join(sorted(gained))}  (--bless if intended)")
        # key-set invariant: a contract section the producer USED to emit but no longer does is a
        # silent drop even if its data was empty when blessed (populated diff can't see it).
        was_keys = _norm(golden[code])[1]
        if was_keys is None:
            legacy_golden = True
        else:
            key_dropped = was_keys - (now_keys or set())
            if key_dropped:
                ok = False
                print(f"  ✗ {code}: section KEY(S) no longer EMITTED → {', '.join(sorted(key_dropped))}  "
                      f"(silent drop — the mapper stopped producing this key)")
    if legacy_golden:
        print("  · golden predates the key-set check — run `check.py --bless` to enable it")

    if ok and not added:
        print(f"  ✓ {len(current)} week(s) match the recorded contract")
    elif ok:
        print(f"  ✓ existing weeks match; {len(added)} new week(s) to bless")
    return ok


def main():
    args = sys.argv[1:]
    if "--bless" in args:
        bless()
        args = [a for a in args if a != "--bless"]

    do_tests = "--char" not in args
    do_char  = "--tests" not in args

    results = []
    if do_tests:
        results.append(run_tests())
    if do_char:
        results.append(run_char())

    ok = all(results)
    print(f"\n{'✓ ALL GREEN' if ok else '✗ FAILURES ABOVE'} — "
          f"{'safe to commit/deploy' if ok else 'fix before committing'}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
