"""Value tests for loot_parser.parse_loot — the ThatsBIS received-loot CSV → WEEK_DATA.loot transform.

External, fragile input: CSV column drift + a raid-night date filter that matches awards to the WCL
pull's LOCAL date, with a one-day adjacent fallback for ThatsBIS/runner timezone skew. Covers the
loot_parser cases — exact-night parse, adjacent-night exclusion, the
timezone fallback, the off-spec flag, item_id coercion, the missing-column guard, and the empty → {}
(card hides) contract."""
import contextlib, csv, io, os, sys, tempfile, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from loot_parser import parse_loot

FIELDS = ["received_at", "character_name", "character_class", "item_id",
          "item_name", "source_name", "instance_name", "is_offspec"]


def _parse(path, date):
    """parse_loot with stdout captured — its ⚠ warnings crash the cp1252 Windows console
    (the pipeline reconfigures stdout to UTF-8 via check.py:30; a bare unittest run doesn't)."""
    with contextlib.redirect_stdout(io.StringIO()):
        return parse_loot(path, date)


def _row(name, item_id, when, *, cls="Warrior", item_name="Item", boss="Boss",
         instance="SSC", os_flag="0"):
    return {"received_at": when, "character_name": name, "character_class": cls,
            "item_id": str(item_id), "item_name": item_name, "source_name": boss,
            "instance_name": instance, "is_offspec": os_flag}


class TestParseLoot(unittest.TestCase):
    def _csv(self, rows, fields=FIELDS):
        fd, path = tempfile.mkstemp(suffix=".csv")
        with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in fields})
        self.addCleanup(os.unlink, path)
        return path

    def test_exact_night_parsed_other_nights_excluded(self):
        path = self._csv([
            _row("Marvels", 30001, "2026-06-08 00:00:00"),
            _row("Marvels", 30002, "2026-06-08 00:00:00"),
            _row("Healz",   30003, "2026-06-08 00:00:00"),
            _row("Olddrop", 29999, "2026-05-25 00:00:00"),   # a different raid night
        ])
        out = _parse(path, "2026-06-08")
        self.assertEqual(out["total"], 3)              # the May 25 award is excluded
        self.assertEqual(out["offspec"], 0)
        self.assertEqual(out["date"], "2026-06-08")
        names = [p["name"] for p in out["players"]]
        self.assertEqual(names, ["Marvels", "Healz"])  # 2 items sorts ahead of 1
        # items keep CSV order within a player
        self.assertEqual([i["item_id"] for i in out["players"][0]["items"]], [30001, 30002])
        self.assertNotIn("Olddrop", names)

    def test_offspec_flag_counted(self):
        path = self._csv([
            _row("Marvels", 30001, "2026-06-08 00:00:00", os_flag="1"),
            _row("Marvels", 30002, "2026-06-08 00:00:00", os_flag="0"),
        ])
        out = _parse(path, "2026-06-08")
        self.assertEqual(out["offspec"], 1)
        self.assertEqual([i["offspec"] for i in out["players"][0]["items"]], [True, False])

    def test_adjacent_date_fallback_on_timezone_skew(self):
        # No rows on the raid_date; the night's loot is all stamped one day earlier (guild tz / midnight).
        path = self._csv([
            _row("Marvels", 30001, "2026-06-07 00:00:00"),
            _row("Healz",   30002, "2026-06-07 00:00:00"),
        ])
        out = _parse(path, "2026-06-08")
        self.assertEqual(out["total"], 2)              # fell back to 2026-06-07
        self.assertEqual(out["date"], "2026-06-08")    # reported date stays the raid_date

    def test_exact_date_wins_over_adjacent(self):
        # Exact match present → adjacent rows must NOT merge in (won't fuse two raid nights).
        path = self._csv([
            _row("Marvels", 30001, "2026-06-08 00:00:00"),  # exact
            _row("Healz",   30002, "2026-06-07 00:00:00"),  # adjacent
        ])
        out = _parse(path, "2026-06-08")
        self.assertEqual(out["total"], 1)
        self.assertEqual(out["players"][0]["name"], "Marvels")

    def test_item_id_coercion(self):
        path = self._csv([
            _row("Marvels", 30001,    "2026-06-08 00:00:00"),
            _row("Marvels", "TOKEN-X", "2026-06-08 00:00:00"),  # non-numeric kept as a string
        ])
        out = _parse(path, "2026-06-08")
        self.assertEqual([i["item_id"] for i in out["players"][0]["items"]], [30001, "TOKEN-X"])

    def test_missing_file_returns_empty(self):
        self.assertEqual(_parse(os.path.join(tempfile.gettempdir(), "nope-loot.csv"),
                                    "2026-06-08"), {})

    def test_no_matching_rows_returns_empty(self):
        path = self._csv([_row("Marvels", 30001, "2026-01-01 00:00:00")])  # far from any adjacent date
        self.assertEqual(_parse(path, "2026-06-08"), {})

    def test_missing_column_header_returns_empty(self):
        # Drop a required column — every row becomes a silent no-op; the guard must bail, not pretend dry.
        fields = [f for f in FIELDS if f != "item_id"]
        path = self._csv([_row("Marvels", 30001, "2026-06-08 00:00:00")], fields=fields)
        self.assertEqual(_parse(path, "2026-06-08"), {})

    def test_blank_character_name_skipped(self):
        path = self._csv([
            _row("",        30001, "2026-06-08 00:00:00"),
            _row("Marvels", 30002, "2026-06-08 00:00:00"),
        ])
        out = _parse(path, "2026-06-08")
        self.assertEqual(out["total"], 1)
        self.assertEqual(out["players"][0]["name"], "Marvels")

    def test_deterministic(self):
        path = self._csv([_row("Marvels", 30001, "2026-06-08 00:00:00"),
                          _row("Healz",   30002, "2026-06-08 00:00:00")])
        self.assertEqual(_parse(path, "2026-06-08"), _parse(path, "2026-06-08"))


if __name__ == "__main__":
    unittest.main()
