"""paths.py — every filesystem path constant the pipeline reads/writes, in one leaf.

Pure-constants module (no logic, no imports beyond pathlib) so any module can import it
without side effects. wcl_auto_dashboard re-exports all of these, so every existing
W.DASH_FILE / W.WEEK_DATA_CACHE reference keeps resolving.
"""
from pathlib import Path

ROOT_DIR   = Path(__file__).parent.parent  # Gaming/
CACHE_FILE = ROOT_DIR / "cache" / "item_crit_cache.json"
LOGS_DIR   = ROOT_DIR / "logs"
LOOT_DIR   = ROOT_DIR / "loot"   # ThatsBIS "received" CSV exports — newest *.csv auto-picked
DASH_FILE      = ROOT_DIR / "dashboard" / "raid_kpi_dashboard.html"
TEMPLATE_FILE  = ROOT_DIR / "dashboard" / "template.html"
DEFAULT_TITLE  = "Raid KPI Dashboard — TBC Anniversary"

# Per-week mapped WEEK_DATA snapshots — the source of record for OFFLINE reprocessing
# (reprocess.py --all) so a schema/trend/render change never needs a WCL re-run. One JSON
# per report, written every prod run after map_to_week_data() (gitignored, ~MB each).
WEEK_DATA_CACHE = ROOT_DIR / "cache" / "week_data"

# Per-week PRE-map merged `wcl` dict snapshots — the richer offline-reprocess source (reprocess.py
# --from-raw). Where WEEK_DATA_CACHE freezes the MAPPED output, this freezes the input to
# map_to_week_data(), so re-running map offline can repopulate a NEW map-derived metric (the mapped
# snapshot can't — it predates the field). One JSON per report, written every prod run after
# merge_log_into_wcl() (gitignored, ~MB each). Only a brand-new WCL *query* still needs a live run.
WCL_CACHE = ROOT_DIR / "cache" / "wcl"

# Wowhead item socket-count cache for the gear-readiness audit (fetched once per item, then free).
ITEM_META_CACHE = ROOT_DIR / "cache" / "item_meta_cache.json"
