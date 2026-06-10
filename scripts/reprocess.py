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
    python scripts/reprocess.py [path/to/injected.html]   # default: .deploy/index.html
    python scripts/reprocess.py --test-db [path]          # write the test DB instead of prod
"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import wcl_auto_dashboard as W
import db_writer


def load_week_data(html_path: Path) -> dict:
    """Brace-match the WEEK_DATA object out of an injected dashboard HTML."""
    html = html_path.read_text(encoding="utf-8")
    i = html.find("const WEEK_DATA =")
    if i < 0:
        raise SystemExit(f"no WEEK_DATA in {html_path}")
    s = html.find("{", i)
    depth = 0
    for j in range(s, len(html)):
        c = html[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                break
    return json.loads(html[s:j + 1])


def main():
    args = [a for a in sys.argv[1:]]
    test = "--test-db" in args
    args = [a for a in args if a != "--test-db"]
    src = Path(args[0]) if args else (W.ROOT_DIR / ".deploy" / "index.html")
    db_path = db_writer.DB_PATH_TEST if test else db_writer.DB_PATH

    wd = load_week_data(src)
    meta = wd.get("meta", {})
    print(f"reprocess {meta.get('report_code')} ({meta.get('date')}) from {src}")
    print(f"  DB: {db_path.name}  ·  NO WCL calls")

    # 1) recompute trend deltas vs the prior week (reads DB; must precede this week's write)
    W.enrich_with_trends(wd, db_path)
    # 2) persist this week's rows so subsequent weeks can trend against the new schema
    try:
        db_writer.write_week(wd, db_path)
    except Exception as e:
        print(f"  db write warning: {e}")
    # 3) re-render from the current template
    W.inject_into_html(wd, W.DASH_FILE, mapped=wd)
    print(f"  ✓ regenerated {W.DASH_FILE.name}")


if __name__ == "__main__":
    main()
