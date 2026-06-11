"""Tests for publish._section_loss_guard — the deploy-time section-loss guard.

Hermetic: builds complete/degraded WEEK_DATA fixtures in-process; no Netlify, no network.

Run:  python -m unittest discover -s tests
"""
import sys, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import publish
import week_schema as ws


def complete_week(rc="W1", kills=10):
    """A WEEK_DATA with every contract section populated, on a killed week."""
    wk = {"meta": {"report_code": rc, "kills": kills, "start_ms": 1, "date": "d"}}
    for k, s in ws.SECTIONS.items():
        if s.tier == ws.META:
            continue
        if s.predicate is ws._has_players:
            wk[k] = {"players": [{"name": "X"}]}
        elif s.predicate is ws._has_batteries:
            wk[k] = {"batteries": [{"l": 1}]}
        elif s.predicate is ws._hs_nonempty:
            wk[k] = {"total_used": 1, "died_total": 1}
        elif k in ("roster", "boss_times", "boss_meta", "roleSpells", "playerSpells",
                   "avoidableMechanics", "healReaction", "debuffCoverage"):
            wk[k] = {"_": 1}
        else:
            wk[k] = [{"name": "X"}]
    return wk


class TestSectionLossGuard(unittest.TestCase):
    def test_complete_redeploy_has_no_reasons(self):
        self.assertEqual(publish._section_loss_guard(complete_week("A"), complete_week("A")), [])

    def test_same_week_redeploy_losing_loot_blocks(self):
        prev, new = complete_week("W1"), complete_week("W1")
        new["loot"] = {}                                    # the exact incident: loot dropped on re-deploy
        reasons = publish._section_loss_guard(prev, new)
        self.assertTrue(any("loot" in r for r in reasons), reasons)

    def test_same_week_redeploy_blanking_parse_pct_blocks(self):
        # field-coverage collapse INSIDE a still-populated section: parse % went all-null on a
        # re-deploy of the same report (the reprocess-over-fresh-WCL incident). The section stays
        # populated, so section-level regression() can't see it — only the coverage check can.
        prev, new = complete_week("W1"), complete_week("W1")
        prev["damageBySelection"]["players"] = [{"name": "X", "vs_replacement": 74}]
        prev["tankScorecard"] = [{"name": "T", "vs_replacement": 71}]
        new["damageBySelection"]["players"] = [{"name": "X"}]   # same players, parse value gone
        new["tankScorecard"] = [{"name": "T"}]
        reasons = publish._section_loss_guard(prev, new)
        self.assertTrue(any("parse" in r for r in reasons), reasons)

    def test_new_week_with_different_optional_sections_does_not_false_positive(self):
        # weekly deploys advance the latest week; a new week may legitimately have empty optional
        # sections (flawless = no deaths, dry night = no loot). Cross-week must NOT block.
        prev, new = complete_week("W1"), complete_week("W2")
        new["deaths"] = []      # optional WCL
        new["loot"] = {}        # external
        self.assertEqual(publish._section_loss_guard(prev, new), [])

    def test_broken_pipeline_blanks_a_required_wcl_section_blocks(self):
        new = complete_week("W3")
        new["damageBySelection"] = {"durations": {}, "players": []}   # the live DPS source, blank
        reasons = publish._section_loss_guard(None, new)
        self.assertTrue(any("damageBySelection" in r for r in reasons), reasons)

    def test_no_prev_and_complete_new_is_safe(self):
        self.assertEqual(publish._section_loss_guard(None, complete_week("W4")), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
