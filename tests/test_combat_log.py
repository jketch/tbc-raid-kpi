"""Hermetic unit tests for scripts/combat_log.py — the raw WoWCombatLog.txt parser.

This is the biggest coverage gap and the most fragile input in the pipeline (180 MB+
hand-formatted text). Every case here is a tiny synthetic encounter (~5-15 lines) that
pins a REAL behavior so a regression flips it red:

  1. Hydross result=0 → kill promotion (6ff1b00 bug fix)
  2. a normal ENCOUNTER_END result=1 kill needs no boss UNIT_DIED
  3. a wipe (result=0, no boss death) stays out of the fights list
  4. allowed_bosses excludes an off-report boss window entirely
  5. a pet (Felhunter) interrupt credits the OWNER via SPELL_SUMMON; an unmapped pet is dropped
  6. _parse_ts: full-date parse, midnight rollover monotonicity, 2-digit-year == 4-digit
  7. MC save dedup: one save per caster per MC episode, re-armed when the MC aura re-applies
  8. robustness: a no-double-space line and a too-short event line don't raise
  9. _open_log: .txt / .gz / .zip all yield identical fights

LOG LINE FORMAT: "<timestamp><TWO spaces><comma-data>". The parser splits with
re.split(r"\\s{2}", line, maxsplit=1) then data.split(","). fields[0] is the event name.
TIMESTAMP must be FULL "M/D/YYYY H:MM:SS.mmm" (year + .mmm both required, else _parse_ts→0).

Field indices used below were verified empirically against parse_combat_log itself:
  SPELL_DAMAGE amount = fields[30], crit flag = fields[-4]
  SWING_DAMAGE amount = fields[27], crit flag = fields[-3]
  spellId fields[9], spellName fields[10]
  SPELL_INTERRUPT interrupted-spell = fields[13]
  aura events: srcGUID[1] srcName[2] dstGUID[5] dstName[6] spellName[10]
  UNIT_DIED destGUID = fields[5], destName = fields[6]

Run only this file:
  python -m unittest discover -s tests -p "test_combat_log.py" -v
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import gzip
import io
import tempfile
import unittest
import zipfile

from combat_log import parse_combat_log, _parse_ts


# ── timestamp helpers (full date is mandatory) ────────────────────────────────
TS = "6/11/2026 21:00:00.000"
TSE = "6/11/2026 21:05:00.000"      # +5 min — closes the first encounter
TS2 = "6/11/2026 21:10:00.000"
TS2E = "6/11/2026 21:15:00.000"


# ── line builders (exactly TWO spaces between stamp and comma-data) ───────────
def _line(ts, data):
    return ts + "  " + data


def _spell_dmg(src, srcname, dst, dstname, spellid, spellname, amount, crit=False):
    """Player→creature SPELL_DAMAGE. amount lands at fields[30]; the crit flag at
    fields[-4] (one trailing pad keeps -4 pointed at the slot right after amount)."""
    f = ["SPELL_DAMAGE", src, srcname, "0x511", "0x0", dst, dstname, "0xa48", "0x0",
         str(spellid), spellname, "0x20"]
    while len(f) < 30:
        f.append("0")
    f.append(str(amount))                       # idx 30
    f += [("1" if crit else "0"), "0", "0", "0"]  # crit at idx 31 == fields[-4]
    return ",".join(f)


def _avoid(src, srcname, dst, dstname, spellname, amount):
    """Creature→player SPELL_DAMAGE (an avoidable mechanic). amount at fields[30]."""
    f = ["SPELL_DAMAGE", src, srcname, "0xa48", "0x0", dst, dstname, "0x511", "0x0",
         "22222", spellname, "0x8"]
    while len(f) < 30:
        f.append("0")
    f.append(str(amount))
    f += ["0", "0", "0", "0"]
    return ",".join(f)


def _interrupt(src, srcname, dst, dstname, intspell):
    """SPELL_INTERRUPT. The interrupted spell name is fields[13] (extraSpellName)."""
    f = ["SPELL_INTERRUPT", src, srcname, "0x511", "0x0", dst, dstname, "0xa48", "0x0",
         "33333", '"Spell Lock"', "0x20", "44444", intspell]
    return ",".join(f)


def _summon(owner, ownername, pet):
    """SPELL_SUMMON: fields[1]=owner GUID, fields[2]=owner name, fields[5]=summoned GUID."""
    f = ["SPELL_SUMMON", owner, ownername, "0x511", "0x0", pet, '"Felhunter"', "0x1111",
         "0x0", "55555", '"Summon Felhunter"', "0x20"]
    return ",".join(f)


def _aura(ev, src, srcname, dst, dstname, spellname):
    """SPELL_AURA_APPLIED / SPELL_AURA_REMOVED. spellName is fields[10]; the trailing
    'BUFF' token is harmless (it's read only for the drum-buff branch)."""
    f = [ev, src, srcname, "0x511", "0x0", dst, dstname, "0xa48", "0x0", "77777",
         spellname, "0x20", "BUFF"]
    return ",".join(f)


def _unit_died(guid, name):
    """UNIT_DIED: destGUID=fields[5], destName=fields[6]."""
    f = ["UNIT_DIED", "0000000000000000", "nil", "0x80000000", "0x80000000",
         guid, name, "0xa48", "0x0"]
    return ",".join(f)


def _enc_start(name):
    return f'ENCOUNTER_START,623,"{name}",2,25'


def _enc_end(name, result):
    return f'ENCOUNTER_END,623,"{name}",2,25,{result}'


def _log(*lines, suffix=".txt"):
    """Write the joined lines to a NamedTemporaryFile (delete=False) and return its Path.
    Hermetic: a tempfile, never cache/ or dashboard/. Caller need not clean up (OS temp)."""
    tf = tempfile.NamedTemporaryFile("w", delete=False, suffix=suffix, encoding="utf-8")
    tf.write("\n".join(lines))
    tf.close()
    return Path(tf.name)


def _names(fights):
    return [f["name"] for f in fights]


class TestKillDetection(unittest.TestCase):
    def test_hydross_result0_promoted_to_kill(self):
        """6ff1b00: Hydross fires ENCOUNTER_END result=0 even on a kill. A Creature
        UNIT_DIED on the boss inside [start, end+1.5] promotes it to a fight."""
        p = _log(
            _line(TS, _enc_start("Hydross the Unstable")),
            _line(TS, _spell_dmg("Player-1-A", '"Wlock-R"', "Creature-0-1-HY",
                                 '"Hydross the Unstable"', 11111, '"Shadow Bolt"', 9999)),
            _line("6/11/2026 21:04:59.500",
                  _unit_died("Creature-0-1-HY", '"Hydross the Unstable"')),
            _line(TSE, _enc_end("Hydross the Unstable", 0)),
        )
        r = parse_combat_log(str(p))
        self.assertIn("Hydross the Unstable", _names(r["fights"]))
        self.assertEqual(len(r["fights"]), 1)
        self.assertEqual(r["fights"][0]["result"], 0)   # raw result preserved; promotion is by death

    def test_normal_kill_result1_no_death_needed(self):
        """A clean result=1 ENCOUNTER_END is a kill with no boss UNIT_DIED required."""
        p = _log(
            _line(TS, _enc_start("Void Reaver")),
            _line(TS, _spell_dmg("Player-1-A", '"Wlock-R"', "Creature-0-1-VR",
                                 '"Void Reaver"', 11111, '"Shadow Bolt"', 5000)),
            _line(TSE, _enc_end("Void Reaver", 1)),
        )
        r = parse_combat_log(str(p))
        self.assertEqual(_names(r["fights"]), ["Void Reaver"])

    def test_wipe_stays_a_wipe(self):
        """result=0 AND no boss UNIT_DIED in the window → NOT a fight."""
        p = _log(
            _line(TS, _enc_start("Lady Vashj")),
            _line(TS, _spell_dmg("Player-1-A", '"Wlock-R"', "Creature-0-1-V",
                                 '"Lady Vashj"', 11111, '"Shadow Bolt"', 9999)),
            _line(TSE, _enc_end("Lady Vashj", 0)),
        )
        r = parse_combat_log(str(p))
        self.assertEqual(r["fights"], [])

    def test_allowed_bosses_excludes_off_report_window(self):
        """Two kills; allowed_bosses={Hydross}. An avoidable hit landing INSIDE the
        excluded Gruul window must not reach that player — they're absent from result,
        their avoidable_dmg never accrues, while the in-report boss is still a fight."""
        p = _log(
            _line(TS, _enc_start("Hydross the Unstable")),
            _line(TS, _spell_dmg("Player-1-A", '"Wlock-R"', "Creature-0-1-HY",
                                 '"Hydross the Unstable"', 11111, '"Shadow Bolt"', 9999)),
            _line(TSE, _enc_end("Hydross the Unstable", 1)),
            _line(TS2, _enc_start("Gruul the Dragonkiller")),
            _line(TS2, _avoid("Creature-0-1-GR", '"Gruul the Dragonkiller"',
                              "Player-1-B", '"Eater-R"', '"Cave In"', 8000)),
            _line(TS2E, _enc_end("Gruul the Dragonkiller", 1)),
        )
        r = parse_combat_log(str(p), allowed_bosses={"Hydross the Unstable"})
        self.assertEqual(_names(r["fights"]), ["Hydross the Unstable"])
        # The off-report victim never reaches the output dict at all.
        self.assertNotIn("Eater", r["players"])
        self.assertIn("Wlock", r["players"])


class TestInterruptCredit(unittest.TestCase):
    def test_pet_interrupt_credits_owner_unmapped_pet_dropped(self):
        """A Felhunter summoned pre-pull (SPELL_SUMMON) → its in-window SPELL_INTERRUPT
        credits the owner (who also dealt boss damage so they appear in result). A SECOND
        interrupt from an UNSUMMONED pet GUID is dropped — owner stays at 1, stray absent."""
        PET = "Creature-0-1-PET"
        STRAY = "Creature-0-1-STRAY"
        p = _log(
            _line(TS, _summon("Player-1-A", '"Wlock-R"', PET)),   # pre-pull, outside any kill window
            _line(TS, _enc_start("Hydross the Unstable")),
            _line(TS, _spell_dmg("Player-1-A", '"Wlock-R"', "Creature-0-1-B",
                                 '"Hydross the Unstable"', 11111, '"Shadow Bolt"', 9999)),
            _line(TS, _interrupt(PET, '"Felhunter"', "Creature-0-1-B",
                                 '"Hydross the Unstable"', '"Greater Heal"')),
            _line(TS, _interrupt(STRAY, '"StrayPet"', "Creature-0-1-B",
                                 '"Hydross the Unstable"', '"Flash Heal"')),
            _line(TSE, _enc_end("Hydross the Unstable", 1)),
        )
        r = parse_combat_log(str(p))
        self.assertEqual(r["players"]["Wlock"]["interrupt_count"], 1)
        self.assertEqual(r["players"]["Wlock"]["interrupt_list"], ["Greater Heal"])
        # The stray (unmapped) pet is credited to nobody — it never spawns a result row.
        self.assertNotIn("StrayPet", r["players"])
        self.assertEqual(sorted(r["players"].keys()), ["Wlock"])


class TestParseTs(unittest.TestCase):
    def test_full_date_and_ordering(self):
        early = _parse_ts("6/11/2026 21:00:00.000")
        late = _parse_ts("6/11/2026 21:00:01.000")
        self.assertGreater(early, 0)
        self.assertEqual(late - early, 1.0)
        self.assertLess(early, late)

    def test_missing_year_returns_zero(self):
        # The plan prose's "6/11 21:00:00.000" is a trap — no year ⇒ regex miss ⇒ 0.
        self.assertEqual(_parse_ts("6/11 21:00:00.000"), 0)

    def test_midnight_rollover_is_monotonic(self):
        """A raid crossing 00:00 must keep advancing — the date is folded in, so the
        next-day 00:00:01 is strictly greater than the prior 23:59:59 (else fight
        windows straddling midnight would collapse)."""
        before = _parse_ts("6/11/2026 23:59:59.000")
        after = _parse_ts("6/12/2026 00:00:01.000")
        self.assertGreater(after, before)
        self.assertEqual(after - before, 2.0)

    def test_two_digit_year_equals_four_digit(self):
        # y<100 ⇒ +2000, so "6/11/26 …" must equal "6/11/2026 …".
        self.assertEqual(_parse_ts("6/11/26 21:00:00.000"),
                         _parse_ts("6/11/2026 21:00:00.000"))


class TestMCSaveDedup(unittest.TestCase):
    def test_one_save_per_episode_rearmed_on_remc(self):
        """A charmed ally: the boss MCs the victim, a caster lands Cyclone TWICE in one
        episode (deduped → count 1). The MC aura is REMOVED (episode ends) and re-APPLIED,
        and a third Cyclone now counts → total 2."""
        BOSS = "Creature-0-1-KAEL"
        VIC = "Player-1-VIC"
        CASTER = "Player-1-CC"
        p = _log(
            _line(TS, _enc_start("Kael'thas Sunstrider")),
            _line(TS, _aura("SPELL_AURA_APPLIED", BOSS, '"Kael"', VIC, '"Victim-R"',
                            '"Mind Control"')),
            _line(TS, _aura("SPELL_AURA_APPLIED", CASTER, '"Cycler-R"', VIC, '"Victim-R"',
                            '"Cyclone"')),
            _line(TS, _aura("SPELL_AURA_APPLIED", CASTER, '"Cycler-R"', VIC, '"Victim-R"',
                            '"Cyclone"')),                              # same episode → deduped
            _line(TS, _aura("SPELL_AURA_REMOVED", BOSS, '"Kael"', VIC, '"Victim-R"',
                            '"Mind Control"')),                         # episode ends
            _line(TS, _aura("SPELL_AURA_APPLIED", BOSS, '"Kael"', VIC, '"Victim-R"',
                            '"Mind Control"')),                         # re-MC
            _line(TS, _aura("SPELL_AURA_APPLIED", CASTER, '"Cycler-R"', VIC, '"Victim-R"',
                            '"Cyclone"')),                              # new episode → counts
            _line(TSE, _enc_end("Kael'thas Sunstrider", 1)),
        )
        r = parse_combat_log(str(p))
        savers = {s["name"]: s for s in r["mc_saves"]}
        self.assertIn("Cycler", savers)
        self.assertEqual(savers["Cycler"]["count"], 2)
        self.assertEqual(savers["Cycler"]["spells"], {"Cyclone": 2})


class TestRobustness(unittest.TestCase):
    def test_malformed_lines_do_not_raise(self):
        """A line with no double-space (splits to 1 part) and a too-short event line
        (<6 fields) are both skipped; the valid lines around them still parse."""
        p = _log(
            _line(TS, _enc_start("Hydross the Unstable")),
            "this is a malformed line with no double space",   # 1 part after split → skipped
            _line(TS, "SHORT,a,b"),                             # < 6 fields → skipped in pass 2
            _line(TS, _spell_dmg("Player-1-A", '"Wlock-R"', "Creature-0-1-HY",
                                 '"Hydross the Unstable"', 11111, '"Shadow Bolt"', 9999)),
            _line(TSE, _enc_end("Hydross the Unstable", 1)),
        )
        r = parse_combat_log(str(p))   # must NOT raise
        self.assertEqual(_names(r["fights"]), ["Hydross the Unstable"])
        self.assertEqual(r["players"]["Wlock"]["total_dmg"], 9999)


class TestOpenLog(unittest.TestCase):
    """_open_log transparently reads .txt / .gz / .zip — all three must agree."""
    BODY_LINES = (
        _line(TS, _enc_start("Hydross the Unstable")),
        _line(TS, _spell_dmg("Player-1-A", '"Wlock-R"', "Creature-0-1-HY",
                             '"Hydross the Unstable"', 11111, '"Shadow Bolt"', 9999)),
        _line(TSE, _enc_end("Hydross the Unstable", 1)),
    )

    def _body(self):
        return "\n".join(self.BODY_LINES)

    def test_txt_gz_zip_identical(self):
        body = self._body()

        # .txt
        txt = _log(*self.BODY_LINES)

        # .gz
        gz = tempfile.NamedTemporaryFile(delete=False, suffix=".gz")
        gz.close()
        with gzip.open(gz.name, "wt", encoding="utf-8") as g:
            g.write(body)

        # .zip (single member)
        zp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
        zp.close()
        with zipfile.ZipFile(zp.name, "w") as z:
            z.writestr("WoWCombatLog.txt", body)

        r_txt = parse_combat_log(str(txt))
        r_gz = parse_combat_log(gz.name)
        r_zip = parse_combat_log(zp.name)

        self.assertEqual(_names(r_txt["fights"]), ["Hydross the Unstable"])
        self.assertEqual(_names(r_gz["fights"]), _names(r_txt["fights"]))
        self.assertEqual(_names(r_zip["fights"]), _names(r_txt["fights"]))
        self.assertEqual(r_gz["players"], r_txt["players"])
        self.assertEqual(r_zip["players"], r_txt["players"])

    def test_open_log_zip_picks_first_sorted_member(self):
        """A self-check on _open_log's deterministic member pick (sorted namelist[0])."""
        from combat_log import _open_log
        body = self._body()
        zp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
        zp.close()
        with zipfile.ZipFile(zp.name, "w") as z:
            z.writestr("b_second.txt", "junk that should be ignored")
            z.writestr("a_first.txt", body)
        with _open_log(zp.name) as fh:
            self.assertIsInstance(fh, io.TextIOWrapper)
            first = fh.readline()
        self.assertIn("ENCOUNTER_START", first)   # a_first.txt sorts first


class TestDeterminism(unittest.TestCase):
    def test_same_input_same_output(self):
        """The project guarantees byte-identical output for identical input. Parse the
        same log twice (fresh tempfiles, same bytes) and assert the dicts are equal."""
        def build():
            return _log(
                _line(TS, _enc_start("Hydross the Unstable")),
                _line(TS, _spell_dmg("Player-1-A", '"Wlock-R"', "Creature-0-1-HY",
                                     '"Hydross the Unstable"', 11111, '"Shadow Bolt"',
                                     9999, crit=True)),
                _line(TS, _spell_dmg("Player-1-A", '"Wlock-R"', "Creature-0-1-HY",
                                     '"Hydross the Unstable"', 11111, '"Shadow Bolt"',
                                     5000, crit=False)),
                _line(TS, _avoid("Creature-0-1-HY", '"Hydross the Unstable"',
                                 "Player-1-A", '"Wlock-R"', '"Scalding Water"', 4500)),
                _line(TSE, _enc_end("Hydross the Unstable", 1)),
            )

        r1 = parse_combat_log(str(build()))
        r2 = parse_combat_log(str(build()))
        self.assertEqual(r1["players"], r2["players"])
        self.assertEqual(r1["fights"], r2["fights"])
        # crit (1) + hit (1) on the same spell → 50.0% actual_crit; avoidable accrued.
        self.assertEqual(r1["players"]["Wlock"]["actual_crit"], 50.0)
        self.assertEqual(r1["players"]["Wlock"]["total_dmg"], 14999)
        self.assertEqual(r1["players"]["Wlock"]["avoidable_dmg"], 4500)
        self.assertEqual(r1["players"]["Wlock"]["avoidable_sources"],
                         {"Scalding Water": 4500})


if __name__ == "__main__":
    unittest.main()
