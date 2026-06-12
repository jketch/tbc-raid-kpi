"""Facade-surface freeze for scripts/wcl_auto_dashboard.py.

The monolith is being decomposed into leaf modules (paths/roles/crit_model/wcl_fetchers/
week_map/trends/render_html), with wcl_auto_dashboard remaining the permanent facade that
re-exports the public surface. External consumers (week_build, reprocess, backfill_snapshots,
build_site, preview, tests) all import through `wcl_auto_dashboard as W` — this test asserts
every name they rely on stays bound on the facade, so a dropped re-export fails check.py
instead of a Thursday-night pipeline run.

Hermetic: imports the module only; no WCL, no DB, no file writes.

Run:  python -m unittest discover -s tests
"""
import sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import wcl_auto_dashboard as W

# name → external consumer(s) that import it via the facade (grep-frozen 2026-06-12)
PUBLIC_FUNCTIONS = {
    "get_token":            "backfill_snapshots",
    "gql":                  "week_build, backfill_snapshots",
    "parse_combat_log":     "week_build",
    "build_week_data":      "week_build",
    "merge_log_into_wcl":   "week_build",
    "map_to_week_data":     "week_build, reprocess",
    "reingest_loot":        "week_build (+ monkeypatched in test_week_build)",
    "dump_week_data_cache": "week_build, backfill_snapshots (+ monkeypatched)",
    "dump_wcl_raw_cache":   "backfill_snapshots",
    "enrich_with_trends":   "week_build, build_site (+ monkeypatched)",
    "inject_into_html":     "week_build, build_site, preview (+ monkeypatched)",
    "print_summary":        "run_weekly console output (main)",
}

PUBLIC_CONSTANTS = {
    "Q_REPORT":        "week_build",
    "ROOT_DIR":        "reprocess, build_site",
    "DASH_FILE":       "week_build, reprocess, build_site, preview",
    "WEEK_DATA_CACHE": "reprocess, build_site, backfill_snapshots",
    "WCL_CACHE":       "reprocess",
    "WCL_API_URL":     "re-export contract (wcl_client)",
    "WCL_TOKEN_URL":   "re-export contract (wcl_client)",
}

# Internal seams shared across the future module split — the facade keeps these bound so
# in-facade callers (build_week_data, main) and any retained snippet/REPL usage survive.
SHARED_INTERNALS = {
    "expected_crit", "gear_crit_rating", "LUCK_MIN_WEEKS",
    "_effective_role", "_toolkit_metric", "_tank_survival_grade",
    "build_consumable_compliance", "fetch_master_data",
    "TOOLKIT_ABILITIES", "DEBUFF_SLOTS", "MANA_SOURCES",
    "CACHE_FILE", "LOGS_DIR", "LOOT_DIR", "TEMPLATE_FILE", "DEFAULT_TITLE",
    "ITEM_META_CACHE",
}


class TestFacadeSurface(unittest.TestCase):
    def test_public_functions_present_and_callable(self):
        for name, consumer in PUBLIC_FUNCTIONS.items():
            self.assertTrue(hasattr(W, name), f"facade lost W.{name} (used by {consumer})")
            self.assertTrue(callable(getattr(W, name)), f"W.{name} is not callable")

    def test_public_constants_present(self):
        for name, consumer in PUBLIC_CONSTANTS.items():
            self.assertTrue(hasattr(W, name), f"facade lost W.{name} (used by {consumer})")

    def test_shared_internal_seams_present(self):
        for name in sorted(SHARED_INTERNALS):
            self.assertTrue(hasattr(W, name), f"facade lost W.{name} (cross-module seam)")

    def test_game_constants_star_reexport_alive(self):
        # spot-check the `from game_constants import *` re-export contract
        for name in ("ELIXIR_BUFFS", "POTION_BUFFS", "CC_ABILITIES", "AOE_ABILITIES"):
            self.assertTrue(hasattr(W, name), f"facade lost game_constants re-export W.{name}")


if __name__ == "__main__":
    unittest.main()
