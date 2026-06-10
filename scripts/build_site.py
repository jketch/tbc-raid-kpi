"""Stage .deploy/ for the rolling multi-week dashboard (zero WCL calls). Run by publish.py.

Output:
  .deploy/index.html            ← the LATEST week's full dashboard, taken verbatim from
                                   dashboard/raid_kpi_dashboard.html (embedded WEEK_DATA = enriched + loot),
                                   with the WEEKS_INDEX manifest injected for the week dropdown.
  .deploy/weeks/<report>.json   ← each EARLIER week's WEEK_DATA, enriched (delta_* vs its prior week),
                                   fetched on demand by the dashboard when you pick that week.

Sources: cache/week_data/*.json (mapped snapshots) + cache/raid_history.db (prior rows, READ-only for
trends). The latest week comes from the HTML so its loot + enrichment match exactly what the pipeline
produced; earlier weeks are enriched here.
"""
import sys, json, shutil
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import wcl_auto_dashboard as W
import reprocess
from db_writer import DB_PATH

DEPLOY_DIR = W.ROOT_DIR / ".deploy"
WEEKS_DIR  = DEPLOY_DIR / "weeks"
WINDOW = 6   # rolling number of weeks to publish (newest N)


def _load_snapshots():
    weeks = []
    for f in W.WEEK_DATA_CACHE.glob("*.json"):
        try:
            wd = json.loads(f.read_text(encoding="utf-8"))
            if (wd.get("meta") or {}).get("start_ms") is not None:
                weeks.append(wd)
        except Exception as e:
            print(f"  ⚠ skip {f.name}: {e}")
    weeks.sort(key=lambda w: w["meta"]["start_ms"], reverse=True)   # newest first (dropdown order)
    return weeks[:WINDOW]


def build():
    DEPLOY_DIR.mkdir(exist_ok=True)
    if not W.DASH_FILE.exists():
        raise SystemExit(f"no dashboard HTML at {W.DASH_FILE} — run the pipeline first")

    embedded = reprocess.load_week_data(W.DASH_FILE)               # latest week (enriched + loot)
    latest_code = (embedded.get("meta") or {}).get("report_code")

    weeks = _load_snapshots()
    if not weeks:
        raise SystemExit("no week_data snapshots in cache/week_data — run the pipeline or backfill first")

    if WEEKS_DIR.exists():
        shutil.rmtree(WEEKS_DIR)
    WEEKS_DIR.mkdir(parents=True)

    manifest = []
    for wd in weeks:
        code = wd["meta"]["report_code"]
        if code == latest_code:
            data = embedded                          # verbatim from HTML (loot + enrichment intact)
        else:
            W.enrich_with_trends(wd, DB_PATH)        # delta_* vs the prior week (read-only DB)
            data = wd
        (WEEKS_DIR / f"{code}.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        manifest.append({"report": code, "date": wd["meta"].get("date"), "start_ms": wd["meta"]["start_ms"]})

    # stage index.html = latest dashboard with the WEEKS_INDEX manifest injected for the dropdown
    html = W.DASH_FILE.read_text(encoding="utf-8")
    inj = "const WEEKS_INDEX = " + json.dumps(manifest, ensure_ascii=False) + ";"
    if "const WEEKS_INDEX = [];" in html:
        html = html.replace("const WEEKS_INDEX = [];", inj, 1)
    else:
        print("  ⚠ WEEKS_INDEX placeholder not found — index will render single-week")
    (DEPLOY_DIR / "index.html").write_text(html, encoding="utf-8")

    print(f"  ✓ staged .deploy: index.html + {len(manifest)} week(s) in weeks/")
    for m in manifest:
        tag = "  (latest · embedded)" if m["report"] == latest_code else ""
        print(f"     · {m['date']}  {m['report']}{tag}")


if __name__ == "__main__":
    build()
