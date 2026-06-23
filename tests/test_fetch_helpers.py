"""Unit tests for the PURE helpers in scripts/wcl_fetchers.py — no network, no DB, no HTML.

These helpers are the load-bearing plumbing under the WCL fetch layer:
  _report        — null-safe gql() payload descent (the "one bad alias can't kill the run" guard)
  _loads_alias   — JSON-string-or-dict normalizer for batched alias blobs
  _merge_bands   — overlap-merging interval coverage (ms)
  _totem_uptime  — cadence model for totem uptime (band-merge + pre-pull lead-in)
  _tank_survival_grade — absolute 0-100 tank survivability grade + flags
  parse_damage_table   — per-actor crit stats from a DamageDone table blob
  merge_actor_names    — sourceID counts -> name-keyed dict
  _median        — list median (0.0 on empty)

importing wcl_fetchers is fine — it imports `requests`/`gql` transitively but none of the
helpers tested here touch the network.
"""
import sys, json, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from wcl_fetchers import (
    _report,
    _loads_alias,
    _merge_bands,
    _totem_uptime,
    _tank_survival_grade,
    parse_damage_table,
    merge_actor_names,
    _median,
    content_excluded_fight_ids,
)


# ── content_excluded_fight_ids: drop off-content (T4 warmup) kills + their adjacent trash ──
class TestContentExcluded(unittest.TestCase):
    EXCL = {"High King Maulgar", "Gruul the Dragonkiller"}

    def _f(self, fid, name, kill, s, e, enc=None):
        return {"id": fid, "name": name, "kill": kill, "startTime": s, "endTime": e, "encounterID": enc}

    def test_no_excluded_encounters_returns_empty(self):
        fights = [self._f(1, "Lady Vashj", True, 0, 100),
                  self._f(2, None, False, 100, 120)]   # trash
        self.assertEqual(content_excluded_fight_ids(fights, self.EXCL), set())

    def test_drops_excluded_kills_and_their_trash(self):
        # SSC/TK block (0–1000), then a Gruul's-Lair block (2000–3000) with its own trash.
        fights = [
            self._f(1, "Lady Vashj", True, 0, 900),
            self._f(2, None, False, 950, 990),          # SSC trash — nearest kill is Vashj → KEEP
            self._f(3, "High King Maulgar", True, 2000, 2100),
            self._f(4, None, False, 2150, 2300),        # Gruul's-Lair trash → nearest is HKM/Gruul → DROP
            self._f(5, "Gruul the Dragonkiller", True, 2400, 2600),
        ]
        excl = content_excluded_fight_ids(fights, self.EXCL)
        self.assertEqual(excl, {3, 4, 5})

    def test_non_contiguous_warmup_does_not_swallow_real_content(self):
        # HKM at the START, SSC in the middle, Gruul at the END — a naive [first..last] window
        # would swallow all of SSC. Nearest-kill assignment must not.
        fights = [
            self._f(10, "High King Maulgar", True, 0, 100),
            self._f(11, None, False, 120, 140),         # near HKM → DROP
            self._f(12, "Lady Vashj", True, 500, 900),
            self._f(13, None, False, 905, 950),          # near Vashj → KEEP
            self._f(14, "Gruul the Dragonkiller", True, 1500, 1700),
            self._f(15, None, False, 1710, 1750),        # near Gruul → DROP
        ]
        excl = content_excluded_fight_ids(fights, self.EXCL)
        self.assertEqual(excl, {10, 11, 14, 15})
        self.assertNotIn(12, excl)   # real content survives
        self.assertNotIn(13, excl)


# ── _report: the durability guard ─────────────────────────────────────────────
class TestReport(unittest.TestCase):
    def test_full_payload_descends(self):
        data = {"reportData": {"report": {"events": {"data": [1]}}}}
        self.assertEqual(_report(data, "events", "data"), [1])

    def test_single_field_returns_subtree(self):
        data = {"reportData": {"report": {"events": {"data": [1]}}}}
        self.assertEqual(_report(data, "events"), {"data": [1]})

    def test_empty_payload_yields_default_not_crash(self):
        # PRIORITY: a totally empty gql() payload must NOT raise — thins ONE KPI, never kills the run.
        self.assertIsNone(_report({}, "events", "data"))

    def test_report_is_None_yields_default(self):
        # gql() returned reportData but report itself errored out to null.
        self.assertIsNone(_report({"reportData": {"report": None}}, "events"))

    def test_report_missing_field_yields_default(self):
        # report present but the requested alias/field never materialized.
        self.assertIsNone(_report({"reportData": {"report": {}}}, "events"))

    def test_partial_payload_passes_through_explicit_default(self):
        # When a default is provided, a missing field returns THAT (e.g. [] so callers can iterate).
        self.assertEqual(_report({}, "events", "data", default=[]), [])
        self.assertEqual(
            _report({"reportData": {"report": None}}, "rankings", default={}), {}
        )

    def test_none_input_yields_default(self):
        self.assertIsNone(_report(None, "events"))


# ── _loads_alias ──────────────────────────────────────────────────────────────
class TestLoadsAlias(unittest.TestCase):
    def test_json_string_parses(self):
        self.assertEqual(_loads_alias('{"a":1}'), {"a": 1})

    def test_dict_passthrough(self):
        d = {"a": 1}
        out = _loads_alias(d)
        self.assertEqual(out, {"a": 1})
        self.assertIs(out, d)  # passthrough, not a copy

    def test_malformed_string_yields_empty(self):
        self.assertEqual(_loads_alias("{bad"), {})

    def test_none_yields_empty(self):
        self.assertEqual(_loads_alias(None), {})

    def test_empty_string_yields_empty(self):
        # empty string is not valid JSON -> {} (the "one bad alias can't abort the batch" guard)
        self.assertEqual(_loads_alias(""), {})


# ── _merge_bands ──────────────────────────────────────────────────────────────
class TestMergeBands(unittest.TestCase):
    def test_overlapping_intervals_merge(self):
        # [0,10] and [5,15] overlap -> covered = 15 (not 20)
        bands = [{"startTime": 0, "endTime": 10}, {"startTime": 5, "endTime": 15}]
        self.assertEqual(_merge_bands(bands), 15.0)

    def test_disjoint_intervals_sum(self):
        bands = [{"startTime": 0, "endTime": 10}, {"startTime": 20, "endTime": 30}]
        self.assertEqual(_merge_bands(bands), 20.0)

    def test_adjacent_intervals_merge_contiguously(self):
        # touching at the boundary (s <= ce) -> one contiguous span of 30
        bands = [{"startTime": 0, "endTime": 10}, {"startTime": 10, "endTime": 30}]
        self.assertEqual(_merge_bands(bands), 30.0)

    def test_unsorted_input_is_sorted_first(self):
        bands = [{"startTime": 20, "endTime": 30}, {"startTime": 0, "endTime": 10}]
        self.assertEqual(_merge_bands(bands), 20.0)

    def test_empty_yields_zero(self):
        self.assertEqual(_merge_bands([]), 0.0)


# ── _totem_uptime ─────────────────────────────────────────────────────────────
class TestTotemUptime(unittest.TestCase):
    def test_full_coverage_with_recasts(self):
        # window [0,100], dur 50; casts at 0 (pre-pull) and 50 -> bands cover [0,100] = 100%
        kills = [{"startTime": 0, "endTime": 100}]
        self.assertEqual(_totem_uptime([0, 50], kills, 50), 100.0)

    def test_pre_pull_lead_in_single_cast(self):
        # single cast at pull, dur 50 covers [0,50] of a [0,100] window -> 50%
        kills = [{"startTime": 0, "endTime": 100}]
        self.assertEqual(_totem_uptime([0], kills, 50), 50.0)

    def test_gap_reduces_below_100(self):
        # dur 20, single cast at pull covers [0,20] of [0,100] -> 20% (a long uncovered gap)
        kills = [{"startTime": 0, "endTime": 100}]
        self.assertEqual(_totem_uptime([0], kills, 20), 20.0)

    def test_no_cast_in_window_yields_zero(self):
        # a cast outside the fight window contributes nothing (still 0% coverage, total>0)
        kills = [{"startTime": 0, "endTime": 100}]
        self.assertEqual(_totem_uptime([500], kills, 50), 0.0)

    def test_late_cast_no_pre_pull_extension(self):
        # cast at t=60 (>dur from pull) -> no lead-in; covers [60, min(100,90)=90] = 30 -> 30%
        kills = [{"startTime": 0, "endTime": 100}]
        self.assertEqual(_totem_uptime([60], kills, 30), 30.0)

    def test_band_clamped_to_fight_end(self):
        # cast at pull, dur 200 > window 100 -> band clamped to [0,100] = 100%
        kills = [{"startTime": 0, "endTime": 100}]
        self.assertEqual(_totem_uptime([0], kills, 200), 100.0)

    def test_no_kills_yields_zero(self):
        self.assertEqual(_totem_uptime([0, 50], [], 50), 0)


# ── _tank_survival_grade ──────────────────────────────────────────────────────
class TestTankSurvivalGrade(unittest.TestCase):
    def test_clean_tank_is_100_no_flags(self):
        tm = {"crit_count": 0, "crush_count": 0}
        out = _tank_survival_grade(tm, deaths=0)
        self.assertEqual(out["score"], 100)
        self.assertEqual(out["flags"], [])

    def test_one_crit_taken_penalized_70_with_flag(self):
        # crit penalty = min(40, 20 + 1*10) = 30 -> 100 - 30 = 70
        tm = {"crit_count": 1, "crush_count": 0}
        out = _tank_survival_grade(tm, deaths=0)
        self.assertEqual(out["score"], 70)
        self.assertEqual(out["flags"], ["1 crit taken — not crit-immune"])

    def test_crit_penalty_caps_at_40(self):
        # 3 crits -> 20 + 30 = 50, capped at 40 -> 100 - 40 = 60
        tm = {"crit_count": 3, "crush_count": 0}
        out = _tank_survival_grade(tm, deaths=0)
        self.assertEqual(out["score"], 60)
        self.assertIn("3 crits taken — not crit-immune", out["flags"])

    def test_warrior_penalized_for_crushes(self):
        # crush penalty = min(25, crush) = 5 -> 100 - 5 = 95
        tm = {"crit_count": 0, "crush_count": 5}
        out = _tank_survival_grade(tm, deaths=0, cls="Warrior")
        self.assertEqual(out["score"], 95)
        self.assertEqual(out["flags"], ["5 crushing blows"])

    def test_druid_not_penalized_for_crushes(self):
        # bears cannot block -> crushing is unavoidable, no penalty, no flag
        tm = {"crit_count": 0, "crush_count": 5}
        out = _tank_survival_grade(tm, deaths=0, cls="Druid")
        self.assertEqual(out["score"], 100)
        self.assertEqual(out["flags"], [])

    def test_death_penalty_and_singular_flag(self):
        # one death -> min(30, 15) = 15 -> 100 - 15 = 85, flag "died once"
        tm = {"crit_count": 0, "crush_count": 0}
        out = _tank_survival_grade(tm, deaths=1)
        self.assertEqual(out["score"], 85)
        self.assertEqual(out["flags"], ["died once"])

    def test_combined_penalties_stack(self):
        # crit min(40,20+50)=40 + crush min(25,10)=10 + deaths min(30,75)=30 = 80 -> 100-80 = 20
        tm = {"crit_count": 5, "crush_count": 10}
        out = _tank_survival_grade(tm, deaths=5, cls="Warrior")
        self.assertEqual(out["score"], 20)
        # plural death flag
        self.assertIn("died 5×", out["flags"])

    def test_capped_penalties_floor_the_warrior_score_at_five(self):
        # The three penalty caps (crit 40, crush 25, deaths 30) sum to a MAX of 95, so a graded
        # warrior tank can never score below 5 — max(0, score) is a belt-and-suspenders floor that
        # is unreachable under the capped model. Pin the true minimum so a cap retune flips this red.
        tm = {"crit_count": 99, "crush_count": 99}
        out = _tank_survival_grade(tm, deaths=99, cls="Warrior")
        self.assertEqual(out["score"], 5)
        # a Druid (no crush penalty) tops out at 40+30=70 penalty -> minimum score 30
        druid = _tank_survival_grade(tm, deaths=99, cls="Druid")
        self.assertEqual(druid["score"], 30)


# ── parse_damage_table ────────────────────────────────────────────────────────
class TestParseDamageTable(unittest.TestCase):
    def test_minimal_table_per_actor_dict(self):
        raw = {"data": {"entries": [
            {"name": "Marvels", "total": 1000, "hitCount": 75, "critCount": 25,
             "activeTime": 60000},
        ]}}
        out = parse_damage_table(raw)
        self.assertEqual(set(out), {"Marvels"})
        row = out["Marvels"]
        # crit / (hit + crit) * 100 = 25 / 100 * 100 = 25.0
        self.assertEqual(row["actual_crit_pct"], 25.0)
        self.assertEqual(row["total_dmg"], 1000)
        self.assertEqual(row["active_time_ms"], 60000)
        self.assertEqual(row["hit_count"], 75)
        self.assertEqual(row["crit_count"], 25)

    def test_field_name_variants_resolved(self):
        # uses the legacy 'hits'/'crits'/'activeTimeReduced' aliases
        raw = {"data": {"entries": [
            {"name": "Healz", "total": 50, "hits": 90, "crits": 10,
             "activeTimeReduced": 30000},
        ]}}
        out = parse_damage_table(raw)
        self.assertEqual(out["Healz"]["actual_crit_pct"], 10.0)
        self.assertEqual(out["Healz"]["active_time_ms"], 30000)

    def test_zero_denominator_yields_zero_pct(self):
        raw = {"data": {"entries": [{"name": "Idle", "total": 0}]}}
        out = parse_damage_table(raw)
        self.assertEqual(out["Idle"]["actual_crit_pct"], 0.0)
        self.assertEqual(out["Idle"]["hit_count"], 0)
        self.assertEqual(out["Idle"]["crit_count"], 0)

    def test_json_string_input_parsed(self):
        raw = json.dumps({"data": {"entries": [
            {"name": "Marvels", "total": 1, "hitCount": 1, "critCount": 1}]}})
        out = parse_damage_table(raw)
        self.assertEqual(out["Marvels"]["actual_crit_pct"], 50.0)

    def test_malformed_blob_yields_empty_not_crash(self):
        # bad blob is swallowed (warns) -> {} rather than raising
        self.assertEqual(parse_damage_table("not json"), {})
        self.assertEqual(parse_damage_table(None), {})


# ── merge_actor_names ─────────────────────────────────────────────────────────
class TestMergeActorNames(unittest.TestCase):
    def test_id_keyed_counts_mapped_to_names(self):
        counts = {5: {"name": "5", "hits": 100, "crits": 20},
                  7: {"name": "7", "hits": 80, "crits": 40}}
        actors = [{"id": 5, "name": "Marvels", "type": "Player"},
                  {"id": 7, "name": "Healz", "type": "Player"},
                  {"id": 9, "name": "Boss", "type": "NPC"}]
        out = merge_actor_names(counts, actors)
        self.assertEqual(set(out), {"Marvels", "Healz"})
        self.assertEqual(out["Marvels"]["crits"], 20)
        self.assertEqual(out["Marvels"]["name"], "Marvels")  # name field overwritten to the resolved name
        self.assertEqual(out["Healz"]["hits"], 80)

    def test_unknown_id_falls_back_to_string_id(self):
        # an id not in the actors list keys (and names) by str(sid)
        counts = {42: {"name": "42", "hits": 1, "crits": 0}}
        out = merge_actor_names(counts, [])
        self.assertEqual(set(out), {"42"})
        self.assertEqual(out["42"]["name"], "42")


# ── _median ───────────────────────────────────────────────────────────────────
class TestMedian(unittest.TestCase):
    def test_empty_yields_zero_float(self):
        self.assertEqual(_median([]), 0.0)
        self.assertIsInstance(_median([]), float)

    def test_odd_length_middle_value(self):
        self.assertEqual(_median([3, 1, 2]), 2.0)
        self.assertIsInstance(_median([3, 1, 2]), float)

    def test_even_length_average_of_middle_two(self):
        # sorted [1,2,3,4] -> (2+3)/2 = 2.5
        self.assertEqual(_median([4, 1, 3, 2]), 2.5)

    def test_single_element(self):
        self.assertEqual(_median([7]), 7.0)


# ── determinism: identical input -> byte-identical serialization ──────────────
class TestDeterminism(unittest.TestCase):
    def test_parse_damage_table_is_deterministic(self):
        raw = {"data": {"entries": [
            {"name": "B", "total": 2, "hitCount": 1, "critCount": 1},
            {"name": "A", "total": 1, "hitCount": 3, "critCount": 1},
        ]}}
        a = json.dumps(parse_damage_table(raw), sort_keys=True)
        b = json.dumps(parse_damage_table(raw), sort_keys=True)
        self.assertEqual(a, b)

    def test_totem_uptime_is_deterministic(self):
        kills = [{"startTime": 0, "endTime": 100}]
        self.assertEqual(_totem_uptime([0, 40, 80], kills, 50),
                         _totem_uptime([0, 40, 80], kills, 50))


if __name__ == "__main__":
    unittest.main()
