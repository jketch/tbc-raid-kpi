"""Re-derive a week's dashboard HTML + trend deltas from an ALREADY-INJECTED WEEK_DATA blob,
with **zero WCL calls**.

Why: every schema / trend / render change otherwise means re-running the full pipeline per week
(expensive WCL fetches, rate-limited). But the mapped WEEK_DATA is the only thing those stages
need — it's already embedded in each week's dashboard HTML. This re-runs just the cheap local
stages on it:

    load WEEK_DATA (from a cached injected HTML)
      → write_week()          persist this week's rows (so later weeks can trend against it)
      → enrich_with_trends()  recompute delta_* vs the prior week in the DB
      → inject_into_html()    re-render from the CURRENT template (picks up render changes too)

Covers anything derivable from already-fetched data: new DB columns/tables, new trends, render/
template changes. NOT covered: a brand-new RAW metric that needs a new WCL query (e.g. adding
mana_returns the first time needed the Resources events) — that one needs a single real run; after
which this script keeps it current for free.

Usage:
    python scripts/reprocess.py [path/to/injected.html]   # one week from an injected HTML
    python scripts/reprocess.py --test-db [path]          # write the test DB instead of prod
    python scripts/reprocess.py --all                     # rebuild the WHOLE DB + trends from
                                                          #   cache/week_data/*.json (zero WCL)
    python scripts/reprocess.py --all --test-db           # same, into the safe test DB

Single-file mode also SEEDS cache/week_data/<report>.json from the loaded HTML, so reprocessing a
retained dashboard backfills the offline cache. Each future prod run drops its own snapshot there
(see wcl_auto_dashboard.dump_week_data_cache), and `--all` then rebuilds everything offline.

★ RUN `--all` (oldest-first, which it does) AFTER ANY SCHEMA/TREND CHANGE — it is not optional.
  A week-over-week delta reads the PRIOR week's DB row. If that row predates a new column/table
  (e.g. a backfilled week written before all_*/trash_* DPS, class_toolkit, or mana_returns existed),
  the prior value is NULL and the delta SILENTLY BLANKS — the metric shows no trend with no error.
  Symptom seen 2026-06-10: after fixing enrich_with_trends to compare each week against its true
  chronological predecessor (c6fbda1) instead of the global-latest week, past-week trends went blank
  because the May rows were thin. Fix was `reprocess.py --all` to rewrite every week's row with the
  current schema from its (corrected) snapshot — zero WCL, chronological upsert. So: snapshot rebuilds
  via backfill_snapshots.py are READ-ONLY on the DB; to refresh the DB itself, run `--all` here.
  Caveat: `--all` writes each week's row FROM ITS SNAPSHOT, so a log-thin snapshot yields a log-thin
  row — rebuild a week's snapshot WITH its log first (backfill_snapshots.py --log) if you still have it.
"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import wcl_auto_dashboard as W
import db_writer
import week_build as wb


def load_week_data(html_path: Path) -> dict:
    """Load the WEEK_DATA object out of an injected dashboard HTML (string-aware brace match).
    Thin wrapper over week_build.from_snapshot — kept because build_site.py imports this name."""
    return wb.from_snapshot(html_path)


def reprocess_one(wd: dict, db_path: Path, *, render: bool = True, is_test: bool = False,
                 dump: bool = False) -> None:
    """Run the cheap local stages on one mapped WEEK_DATA via the shared spine: finalize (loot +
    contract validation) → commit (dump? → enrich → write → render). has_log=None: a snapshot can't
    know whether the original week had a log, so log-tier emptiness stays INFO (no false warnings)."""
    wb.finalize_week(wd, has_log=None)
    wb.commit_week(wd, db_path, is_test=is_test, dump=dump, enrich=True, write_db=True, render=render)


def reprocess_all(db_path: Path, *, is_test: bool = False) -> None:
    """Rebuild the whole DB + trend chain from cache/week_data/*.json, chronologically (zero WCL).
    Non-destructive upsert (INSERT OR REPLACE): older backfilled weeks not in the cache are kept and
    still serve as trend baselines. Re-renders the dashboard for the most recent cached week.
    Does not re-dump snapshots (it reads them) — `dump=False`."""
    files = sorted(W.WEEK_DATA_CACHE.glob("*.json"))
    weeks = []
    for f in files:
        try:
            weeks.append(wb.from_snapshot(f))
        except Exception as e:
            print(f"  ⚠ skip {f.name}: {e}")
    weeks = [w for w in weeks if (w.get("meta") or {}).get("start_ms") is not None]
    weeks.sort(key=lambda w: w["meta"]["start_ms"])     # chronological — NOT the display date string
    if not weeks:
        raise SystemExit(f"no cached weeks in {W.WEEK_DATA_CACHE} — run a prod pipeline (or seed one "
                         f"with `python scripts/reprocess.py <injected.html>`) first")
    print(f"reprocess --all: {len(weeks)} cached week(s) → {db_path.name}  ·  NO WCL calls")
    for i, wd in enumerate(weeks):
        meta = wd.get("meta", {})
        n = len(wd.get("roster") or {})
        print(f"  [{i+1}/{len(weeks)}] {meta.get('report_code')} ({meta.get('date')}) → {n} players")
        reprocess_one(wd, db_path, render=(i == len(weeks) - 1), is_test=is_test, dump=False)
    print(f"  ✓ rebuilt {len(weeks)} weeks; regenerated {W.DASH_FILE.name} for the latest")


def main():
    args = [a for a in sys.argv[1:]]
    test = "--test-db" in args
    do_all = "--all" in args
    args = [a for a in args if a not in ("--test-db", "--all")]
    db_path = db_writer.DB_PATH_TEST if test else db_writer.DB_PATH

    if do_all:
        reprocess_all(db_path, is_test=test)
        return

    src = Path(args[0]) if args else (W.ROOT_DIR / ".deploy" / "index.html")
    wd = wb.from_snapshot(src)
    meta = wd.get("meta", {})
    print(f"reprocess {meta.get('report_code')} ({meta.get('date')}) from {src}")
    print(f"  DB: {db_path.name}  ·  NO WCL calls")

    # Single-file mode SEEDS the offline cache from this HTML (dump=True, test-gated by commit_week).
    reprocess_one(wd, db_path, render=True, is_test=test, dump=True)
    print(f"  ✓ regenerated {W.DASH_FILE.name}")


if __name__ == "__main__":
    main()
