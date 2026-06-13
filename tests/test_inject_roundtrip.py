"""Round-trip tests for the inject -> extract pair (the WEEK_DATA brace-walk seam).

scripts/render_html.py::inject_into_html (writer) and scripts/week_build.py::extract_week_data
(reader) share a string/escape-aware brace-depth walk. The reader's own unit coverage lives in
tests/test_week_build.py (TestExtractWeekData); this file's value-add is the *paired* behavior:

  1. inject(raw fixture) -> extract == map_to_week_data(raw fixture)   (full identity round-trip)
  2. brace/escape-laden string values survive the splice  (the E3 bug a naive regex would corrupt)
  3. the mapped= path is injected VERBATIM (delta_* trend fields survive; no re-mapping)
  4. a second inject fully replaces the first block (idempotent, no trailing corruption)

Hermetic: the injector reads the REAL dashboard/template.html (by design — it always reads
TEMPLATE_FILE so the output is never the source for the next run), but it WRITES only to a
tempfile we hand it. Zero WCL, zero DB, zero touch of the real raid_kpi_dashboard.html.

Run:  python -m unittest discover -s tests -p "test_inject_roundtrip.py" -v
"""
import sys, tempfile, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

try:                                  # the injector prints a ✅ glyph; keep the cp1252 console quiet
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import paths
from render_html import inject_into_html
from week_build import extract_week_data
from week_map import map_to_week_data


def _raw_fixture():
    """A small raw wcl dict (the shape map_to_week_data consumes), trimmed from
    tests/test_week_map.py. Enough to exercise roster/crit/deaths/damage/healing so the
    mapped output is non-trivial — the point is a full inject->extract->map identity."""
    return {
        "meta": {"date": "Jun 08, 2026", "start_ms": 1780000000000,
                 "zone": "Serpentshrine Cavern", "kills": 2,
                 "report_code": "TESTCODE", "log_missing": []},
        "players": [
            {"name": "Wlock", "role": "Caster", "spec": "Destruction", "class": "Warlock",
             "actual_crit": 25.0, "expected_crit": 20.0, "total_dmg": 5_000_000,
             "deaths": 1, "deaths_trash": 1},
            {"name": "Healz", "role": "Healer", "spec": "Holy", "class": "Priest",
             "actual_crit": 0.0, "expected_crit": 0.0},
            {"name": "Tanky", "role": "Tank", "spec": "Protection", "class": "Warrior",
             "actual_crit": 12.0, "expected_crit": 10.0,
             "fights_tanked": 2, "fights_total": 2},
            {"name": "Huntz", "role": "Physical", "spec": "Beast Mastery", "class": "Hunter",
             "actual_crit": 18.0, "expected_crit": 0.0, "total_dmg": 4_000_000},
        ],
        "boss_times": {"Hydross the Unstable": 300.0, "The Lurker Below": 360.0},
        "healing_metrics": {
            "Healz": {"eff_heal": 3_000_000, "eff_hps": 4500, "overheal_pct": 22.0,
                      "activity_pct": 88.0, "tank_pct": 40.0, "top_spell": "Greater Heal",
                      "fights_healed": 2},
        },
        "healer_mana": {"Healz": 150_000},
        "tank_metrics": {
            "Tanky": {"dtps": 800, "taken": 500_000, "hps_recv": 900, "fights_tanked": 2,
                      "phys_pct": 70.0, "magic_pct": 30.0, "crush_count": 0, "crit_count": 0,
                      "avoid_pct": 38.0,
                      "biggest_hit": {"amount": 9000, "ability": "Melee",
                                      "boss": "Hydross the Unstable"},
                      "cooldowns": {"Shield Wall": 1}, "per_boss": []},
        },
        "damage_by_sel": {
            "durations": {"all": 900, "boss": 660, "trash": 240},
            "players": {
                "Wlock": {"all": {"total": 5_000_000, "active": 800_000},
                          "boss": {"total": 4_000_000, "active": 600_000},
                          "trash": {"total": 1_000_000, "active": 200_000}},
                "Huntz": {"all": {"total": 4_000_000, "active": 700_000},
                          "boss": {"total": 3_200_000, "active": 500_000},
                          "trash": {"total": 800_000, "active": 200_000}},
            },
        },
    }


def _tmp_html_path(stack):
    """A throwaway html path inside a TemporaryDirectory whose lifetime is tied to `stack`."""
    d = tempfile.TemporaryDirectory()
    stack.append(d)
    return Path(d.name) / "out.html"


class TestInjectExtractIdentity(unittest.TestCase):
    """Case 1: inject a raw fixture, extract it back, and assert it equals map_to_week_data()."""

    def setUp(self):
        self._tmpdirs = []

    def tearDown(self):
        for d in self._tmpdirs:
            d.cleanup()

    def test_template_actually_present(self):
        # Guard: the injector silently no-ops if it can't find the marker. If the tracked
        # template ever loses `const WEEK_DATA =`, this test (and the round-trip) must fail loudly.
        self.assertTrue(paths.TEMPLATE_FILE.exists(),
                        "dashboard/template.html missing — the injector needs it")
        self.assertIn("const WEEK_DATA =",
                      paths.TEMPLATE_FILE.read_text(encoding="utf-8"))

    def test_roundtrip_equals_mapper_output(self):
        tmp_html = _tmp_html_path(self._tmpdirs)
        raw = _raw_fixture()
        inject_into_html(raw, tmp_html)                  # mapped=None -> injector re-maps internally
        extracted = extract_week_data(tmp_html.read_text(encoding="utf-8"))
        self.assertEqual(extracted, map_to_week_data(raw))

    def test_roundtrip_preserves_known_values(self):
        # Spot-pin a few real values so a future template/injector change that silently corrupts
        # a subtree (but still parses as JSON) is caught.
        tmp_html = _tmp_html_path(self._tmpdirs)
        inject_into_html(_raw_fixture(), tmp_html)
        wd = extract_week_data(tmp_html.read_text(encoding="utf-8"))
        self.assertEqual(wd["meta"]["report_code"], "TESTCODE")
        self.assertEqual(set(wd["roster"]), {"Wlock", "Healz", "Tanky", "Huntz"})
        self.assertEqual(wd["roster"]["Wlock"]["class"], "Warlock")
        self.assertEqual(wd["boss_times"]["The Lurker Below"], 360.0)

    def test_mapped_arg_matches_none_path(self):
        # Passing mapped=map_to_week_data(raw) must produce the identical extracted dict as the
        # mapped=None path (which maps internally) — proves the two entry points agree.
        tmp_a = _tmp_html_path(self._tmpdirs)
        tmp_b = _tmp_html_path(self._tmpdirs)
        raw = _raw_fixture()
        inject_into_html(raw, tmp_a)                                  # internal map
        inject_into_html(raw, tmp_b, mapped=map_to_week_data(raw))    # explicit map, verbatim
        self.assertEqual(extract_week_data(tmp_a.read_text(encoding="utf-8")),
                         extract_week_data(tmp_b.read_text(encoding="utf-8")))


class TestBraceLadenStringsSurvive(unittest.TestCase):
    """Case 2: the E3 bug — braces / `};` / escaped quotes inside STRING VALUES must round-trip.

    Constructed so a naive `re.sub(r'\\{.*?\\};', ..., re.DOTALL)` would truncate at the first
    `};` that appears *inside a string value* and corrupt the data. The string-aware brace walk
    must ignore every brace between quotes."""

    def setUp(self):
        self._tmpdirs = []

    def tearDown(self):
        for d in self._tmpdirs:
            d.cleanup()

    def _payload(self):
        # Each value below carries a brace, a `};`, or an escaped quote. A regex-truncating
        # extractor (cut at first `};`) would lose everything after "tail_with_close".
        return {
            "meta": {"report_code": "T", "kills": 1},
            "boss": "Lurker {Below}",
            "open_brace": "value with a { lonely open brace",
            "close_brace": "value with a } lonely close brace",
            "tail_with_close": "this string literally contains a }; sequence — keep reading",
            "escaped_quote": 'a\\"b',         # JSON-encodes to  a\"b  -> the escape must be honored
            "tail": "}",
            "nested": {"deep": "}{};{", "list": ["{", "}", "};", "x\\\"y"]},
            "last_key": "MUST_SURVIVE",       # anything after a `};`-in-string proves no truncation
        }

    def test_brace_and_escape_values_roundtrip(self):
        tmp_html = _tmp_html_path(self._tmpdirs)
        payload = self._payload()
        inject_into_html({}, tmp_html, mapped=payload)   # verbatim — no re-mapping of `{}`
        got = extract_week_data(tmp_html.read_text(encoding="utf-8"))
        self.assertEqual(got, payload)                   # whole object survives byte-for-byte
        # Explicitly pin the brace/escape-laden leaves so a partial corruption is unambiguous:
        self.assertEqual(got["boss"], "Lurker {Below}")
        self.assertEqual(got["tail_with_close"],
                         "this string literally contains a }; sequence — keep reading")
        self.assertEqual(got["escaped_quote"], 'a\\"b')
        self.assertEqual(got["tail"], "}")
        self.assertEqual(got["nested"]["deep"], "}{};{")
        self.assertEqual(got["nested"]["list"], ["{", "}", "};", "x\\\"y"])
        self.assertEqual(got["last_key"], "MUST_SURVIVE")

    def test_string_with_close_semicolon_does_not_truncate(self):
        # The targeted teeth: a `};` inside a string before the real end of the object. A regex
        # extractor would stop at this `};` and drop `after_the_trap`.
        tmp_html = _tmp_html_path(self._tmpdirs)
        payload = {"meta": {"report_code": "T"},
                   "trap": "premature }; end-marker inside a value",
                   "after_the_trap": [1, 2, 3]}
        inject_into_html({}, tmp_html, mapped=payload)
        got = extract_week_data(tmp_html.read_text(encoding="utf-8"))
        self.assertEqual(got["after_the_trap"], [1, 2, 3])
        self.assertEqual(got["trap"], "premature }; end-marker inside a value")


class TestMappedVerbatim(unittest.TestCase):
    """Case 3: mapped=<dict> is injected verbatim — re-mapping would DROP trend (delta_*) fields."""

    def setUp(self):
        self._tmpdirs = []

    def tearDown(self):
        for d in self._tmpdirs:
            d.cleanup()

    def test_delta_fields_survive_only_via_mapped_path(self):
        # A dict that looks like an already-mapped + trend-enriched WEEK_DATA. delta_deaths /
        # delta_total etc. are added by enrich_with_trends AFTER map_to_week_data, so a re-map
        # of this dict would not reproduce them — the only way they reach the HTML is verbatim.
        enriched = {
            "meta": {"report_code": "T", "kills": 1, "date": "Jun 08, 2026"},
            "deaths": [{"name": "X", "role": "Caster", "total": 1, "trash": 0, "delta_deaths": -2}],
            "sunderArmor": {"players": [{"name": "Warr", "total": 40, "effective": 30,
                                         "refreshed": 10, "delta_total": 5}]},
            "healthstoneStats": {"total_used": 3, "died_total": 1, "died_no_stone": 0,
                                 "delta_used": 1, "delta_no_stone": -1},
        }
        tmp_html = _tmp_html_path(self._tmpdirs)
        inject_into_html({}, tmp_html, mapped=enriched)
        got = extract_week_data(tmp_html.read_text(encoding="utf-8"))
        self.assertEqual(got, enriched)                          # exact, verbatim
        self.assertEqual(got["deaths"][0]["delta_deaths"], -2)   # trend field survived
        self.assertEqual(got["sunderArmor"]["players"][0]["delta_total"], 5)
        self.assertEqual(got["healthstoneStats"]["delta_used"], 1)
        self.assertEqual(got["healthstoneStats"]["delta_no_stone"], -1)

    def test_mapped_path_does_not_reconstruct_from_week_data(self):
        # Hand a non-empty `week_data` that, if (wrongly) re-mapped, would produce a totally
        # different roster than `mapped`. The mapped dict must win — week_data is ignored.
        misleading_week_data = _raw_fixture()      # would map to a 4-player roster
        mapped = {"meta": {"report_code": "VERBATIM"}, "roster": {"OnlyMe": {"class": "Mage"}}}
        tmp_html = _tmp_html_path(self._tmpdirs)
        inject_into_html(misleading_week_data, tmp_html, mapped=mapped)
        got = extract_week_data(tmp_html.read_text(encoding="utf-8"))
        self.assertEqual(got, mapped)
        self.assertEqual(set(got["roster"]), {"OnlyMe"})   # NOT the 4-player re-map


class TestIdempotentReplace(unittest.TestCase):
    """Case 4: injecting a second time fully replaces the first block — no trailing corruption."""

    def setUp(self):
        self._tmpdirs = []

    def tearDown(self):
        for d in self._tmpdirs:
            d.cleanup()

    def test_second_inject_replaces_first(self):
        tmp_html = _tmp_html_path(self._tmpdirs)
        a = {"meta": {"report_code": "AAA"}, "marker": "first", "rows": [1, 2, 3]}
        b = {"meta": {"report_code": "BBB"}, "marker": "second", "other": {"k": "}"}}
        inject_into_html({}, tmp_html, mapped=a)
        inject_into_html({}, tmp_html, mapped=b)         # re-inject into the SAME file
        got = extract_week_data(tmp_html.read_text(encoding="utf-8"))
        self.assertEqual(got, b)                          # B exactly — A fully gone
        # And there is exactly ONE WEEK_DATA block left (no leftover trailing object/`};`).
        html = tmp_html.read_text(encoding="utf-8")
        self.assertEqual(html.count("const WEEK_DATA ="), 1)
        # No remnant of the first payload. Pin on unique tokens that do NOT occur in the tracked
        # template ("AAA" report code; the serialized `"marker": "first"` line) — a bare "first"
        # would false-positive on template CSS (e.g. :first-child).
        self.assertNotIn("AAA", html)
        self.assertNotIn('"marker": "first"', html)

    def test_repeated_inject_is_stable(self):
        # Injecting the SAME dict twice yields a byte-identical file the second time (determinism).
        tmp_html = _tmp_html_path(self._tmpdirs)
        payload = {"meta": {"report_code": "T"}, "boss": "Lurker {Below}", "n": [1, 2]}
        inject_into_html({}, tmp_html, mapped=payload)
        once = tmp_html.read_text(encoding="utf-8")
        inject_into_html({}, tmp_html, mapped=payload)
        twice = tmp_html.read_text(encoding="utf-8")
        self.assertEqual(once, twice)                     # same input -> same bytes
        self.assertEqual(extract_week_data(twice), payload)


class TestDeterminism(unittest.TestCase):
    """The project guarantees byte-identical output for identical input."""

    def setUp(self):
        self._tmpdirs = []

    def tearDown(self):
        for d in self._tmpdirs:
            d.cleanup()

    def test_two_injections_into_separate_files_match(self):
        raw = _raw_fixture()
        tmp_a = _tmp_html_path(self._tmpdirs)
        tmp_b = _tmp_html_path(self._tmpdirs)
        inject_into_html(raw, tmp_a)
        inject_into_html(raw, tmp_b)
        self.assertEqual(tmp_a.read_text(encoding="utf-8"),
                         tmp_b.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
