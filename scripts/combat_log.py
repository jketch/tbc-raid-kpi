"""Raw WoWCombatLog.txt parser — the combat-log half of the pipeline, separate from the
WCL API half. parse_combat_log reads a 180 MB+ log and returns the per-fight overlay dict
(engineering, drums, interrupts, avoidable mechanics, MC, friendly fire, consumable USE,
death-recap HP%). Pure of the WCL fetch layer: depends only on game_constants. The overlay
onto the WCL dict (merge_log_into_wcl) stays in wcl_auto_dashboard — it reaches the crit
model. wcl_auto_dashboard re-exports parse_combat_log so call sites are unchanged.
"""
import re
from collections import defaultdict

from game_constants import *   # noqa: F401,F403 — the curated name-sets parse uses

def _parse_ts(ts_str):
    m = re.match(r'\d+/\d+/\d+ (\d+):(\d+):(\d+)\.(\d+)', ts_str)
    if m:
        h,mi,s,ms = m.groups()
        return int(h)*3600 + int(mi)*60 + int(s) + int(ms)/1000
    return 0

# Consumable usage — surfaced as an informational call-out (who's actually popping their
# cooldowns), NOT a compliance threshold. Detected from SPELL_CAST_SUCCESS by name.
def _consumable_category(spell: str) -> str:
    if spell.startswith("Create "):           return ""        # warlock making stones, not using
    if "Healthstone" in spell:                 return "healthstone"
    if spell in ("Dark Rune", "Demonic Rune"): return "rune"
    if "Flame Cap" in spell:                   return "flamecap"
    if "Nightmare Seed" in spell:              return "nightmare_seed"
    if "Potion" in spell:                      return "potion"
    if spell.startswith("Scroll of"):          return "scroll"
    return ""

def parse_combat_log(log_path: str, allowed_bosses=None) -> dict:
    """
    Parse WoWCombatLog.txt and return a dict with per-player stats
    to be merged with WCL API data.
    Returns: { player_name: { actual_crit, deaths, interrupts, eng, drums, avoidable_dmg } }

    `allowed_bosses`: if given (the WCL report's kill boss names), any encounter NOT in it
    (e.g. off-report DST clears like Gruul / High King Maulgar that share the night's log)
    is treated as an EXCLUSION window — every event inside it is skipped, so the dashboard
    stays scoped to the report code you entered.
    """
    print(f"\n[LOG] Parsing combat log: {log_path}")
    from collections import defaultdict

    # Pass 1: identify all successful-kill encounter windows, split into in-report (kept)
    # vs off-report (excluded).
    # TBC Classic bug: Hydross the Unstable (and possibly others) fires ENCOUNTER_END with
    # result=0 even on a kill.  WCL uses UNIT_DIED for kill detection; we do the same:
    # collect all boss UNIT_DIED events and promote any result=0 encounter to a kill when
    # its boss dies within 1.5s of ENCOUNTER_END.
    all_encounters = []   # {name, start, end, result}
    boss_deaths    = []   # {name, ts}
    with open(log_path, encoding="utf-8", errors="replace") as f:
        current = None
        for line in f:
            parts = re.split(r'\s{2}', line.strip(), maxsplit=1)
            if len(parts) != 2: continue
            ts_str, data = parts
            fields = data.split(",")
            ev = fields[0]
            if ev == "ENCOUNTER_START":
                current = {"name": fields[2].strip('"'), "start": _parse_ts(ts_str)}
            elif ev == "ENCOUNTER_END" and current:
                current["end"]    = _parse_ts(ts_str)
                current["result"] = int(fields[5].strip()) if len(fields) > 5 else 0
                all_encounters.append(current)
                current = None
            elif ev == "UNIT_DIED" and len(fields) > 6:
                dst_guid = fields[5]
                dst_name = fields[6].strip('"')
                if dst_guid.startswith("Creature"):
                    boss_deaths.append({"name": dst_name, "ts": _parse_ts(ts_str)})

    all_kills = []
    for enc in all_encounters:
        if enc["result"] == 1:
            all_kills.append(enc)
        else:
            # Fallback: treat as kill if the boss has a UNIT_DIED within 1.5s of ENCOUNTER_END
            for death in boss_deaths:
                if death["name"] == enc["name"] and abs(death["ts"] - enc["end"]) <= 1.5:
                    all_kills.append(enc)
                    break

    if allowed_bosses is not None:
        kills    = [k for k in all_kills if k["name"] in allowed_bosses]
        excluded = [k for k in all_kills if k["name"] not in allowed_bosses]
    else:
        kills, excluded = all_kills, []
    excl_windows = [(k["start"], k["end"]) for k in excluded]
    if excluded:
        print(f"  [LOG] ignoring {len(excluded)} off-report fight(s): "
              f"{', '.join(k['name'] for k in excluded)}")

    def in_excluded(ts):
        return any(s <= ts <= e for s, e in excl_windows)

    kill_windows = [(k["start"], k["end"], k["name"]) for k in kills]
    def in_kill(ts):
        return any(s <= ts <= e for s,e,_ in kill_windows)
    def which_boss(ts):
        for s,e,nm in kill_windows:
            if s <= ts <= e:
                return nm
        return None

    # Pass 2: collect stats
    player_names = {}        # guid → first name (strip realm)
    gear_crit_by_guid = {}   # guid → {_crit_melee, _crit_ranged, _crit_spell} from COMBATANT_INFO
    gear_by_name = {}        # name → {_crit_melee, _crit_ranged, _crit_spell} (resolved after parse)
    swing_crits  = defaultdict(int)
    swing_hits   = defaultdict(int)
    spell_crits  = defaultdict(int)
    spell_hits   = defaultdict(int)
    dmg_totals   = defaultdict(int)
    interrupts   = defaultdict(list)
    # Per-fight role signals (by boss name) — ground truth for spec-swap-aware roles,
    # immune to WCL's tank spec-label quirks (Warden/Justicar). See classify below.
    fight_melee_taken  = defaultdict(lambda: defaultdict(int))  # [boss][player] = boss MELEE dmg taken (TANK signal)
    fight_healing_done = defaultdict(lambda: defaultdict(int))  # [boss][player] = effective healing (HEALER signal)
    fight_damage_done  = defaultdict(lambda: defaultdict(int))  # [boss][player] = damage to creatures (DPS signal)
    fight_dmg_taken    = defaultdict(lambda: defaultdict(int))  # [boss][player] = ALL dmg taken (tank DTPS)
    fight_heal_recv    = defaultdict(lambda: defaultdict(int))  # [boss][player] = healing received (tank soak)
    eng_usage    = defaultdict(lambda: defaultdict(int))
    eng_dmg      = defaultdict(int)   # real engineering damage to enemies
    drums_cast   = defaultdict(int)
    drums_buffs  = defaultdict(int)
    avoidable    = defaultdict(lambda: defaultdict(int))   # [player][mechanic] = dmg
    avoid_hits   = defaultdict(lambda: defaultdict(list))  # [player][mechanic] = [{t,amt}] drill-down
    mech_boss    = defaultdict(lambda: defaultdict(int))   # [mechanic][boss]   = dmg (attribution)
    friendly_fire = defaultdict(lambda: {"dmg": 0, "incidents": 0, "hits": [],
                                         "cats": defaultdict(int), "mechanics": defaultdict(int),
                                         "victims": set(), "bosses": set()})
    mc_now    = set()              # player GUIDs currently Mind Controlled
    mc_save_credit = defaultdict(set)  # MC'd GUID → casters already credited a save this episode
    mc_count  = defaultdict(int)   # times each player was MC'd (by name)
    mc_source = {}                 # player name → who controlled them (last seen)
    consum_use = defaultdict(lambda: defaultdict(int))  # [player][category] = use count
    consum_label = defaultdict(dict)  # [player][category] = specific item name (first seen)
    melee_swings = defaultdict(int)   # [player] = auto-attack swings in boss windows
    # MC accountability — blame flips onto the raid when a teammate is controlled.
    mc_saves  = defaultdict(lambda: {"count": 0, "spells": defaultdict(int),
                                     "targets": defaultdict(int), "hits": []})  # by caster
    mc_liable = defaultdict(lambda: {"dmg": 0, "hits": 0, "kills": 0,
                                     "spells": defaultdict(int), "victims": defaultdict(int),
                                     "events": []})  # by aggressor
    last_aoe_on = {}               # MC'd victim GUID → (aggressor, spell, amt, ts) for killing-blow
    consumes  = defaultdict(lambda: {"flask": False, "food": False, "elixirs": set()})
    # HP% timeline: players log HP as a PERCENT (maxHP field == "100") in the advanced
    # block. Sampled on damage taken / heals received / own melee → dense per-player track.
    hp_samples = defaultdict(lambda: defaultdict(list))  # [player][boss] = [(ts, hp_pct, kind)]
    log_deaths = defaultdict(list)                       # [player] = [(ts, boss)] real UNIT_DIED in a boss window

    with open(log_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            parts = re.split(r'\s{2}', line.strip(), maxsplit=1)
            if len(parts) != 2: continue
            ts_str, data = parts
            fields = data.split(",")
            ev = fields[0]
            if len(fields) < 6: continue
            ts = _parse_ts(ts_str)
            if in_excluded(ts): continue   # skip off-report fights (Gruul / HKM DST clears)

            # Track player name from any event. COMBATANT_INFO's field[2] is the
            # faction (a number), not a name — skip it so we don't poison the map.
            if "Player-" in fields[1] and len(fields) > 2 and ev != "COMBATANT_INFO":
                guid = fields[1]
                if guid not in player_names:
                    player_names[guid] = fields[2].strip('"').split("-")[0]

            # Parse COMBATANT_INFO for per-school gear crit rating. Key by GUID and
            # resolve to a name after the loop — a player's name may not be known
            # yet when their COMBATANT_INFO line appears at the pull.
            # TBC 2.5.x CLEU stat order after GUID,faction:
            #   [3]str [4]agi [5]sta [6]int [7]spi [8]dodge [9]parry [10]block
            #   [11]critMelee [12]critRanged [13]critSpell
            if ev == "COMBATANT_INFO" and "Player-" in fields[1]:
                try:
                    gear_crit_by_guid[fields[1]] = {
                        "_crit_melee":  int(fields[11]),
                        "_crit_ranged": int(fields[12]),
                        "_crit_spell":  int(fields[13]),
                        "_agility":     int(fields[4]),    # [4]=agi, for stat-derived crit backfill
                    }
                except: pass

            # Mind Control windows — a controlled player's damage to the raid is the
            # boss's fault, not theirs. Track who's currently MC'd (boss OR trash) by
            # aura name so it generalizes (Kael'thas, Fel Reaver room, etc.).
            if ev == "SPELL_AURA_APPLIED" and len(fields) > 10 and "Player-" in fields[5] \
                    and fields[10].strip('"') in MC_AURAS:
                if fields[5] not in mc_now:        # fresh control episode → reset save credits
                    mc_save_credit.pop(fields[5], None)
                mc_now.add(fields[5])
                dn = player_names.get(fields[5], fields[5])
                mc_count[dn] += 1
                mc_source[dn] = fields[2].strip('"')
            elif ev == "SPELL_AURA_REMOVED" and len(fields) > 10 and "Player-" in fields[5] \
                    and fields[10].strip('"') in MC_AURAS:
                mc_now.discard(fields[5])
                mc_save_credit.pop(fields[5], None)

            # MC SAVE — a raider lands CC on a currently-controlled teammate, parking
            # them harmlessly (Cyclone et al.). Credit the caster. Cyclone is the marquee.
            if ev == "SPELL_AURA_APPLIED" and len(fields) > 10 \
                    and "Player-" in fields[1] and "Player-" in fields[5] \
                    and fields[1] != fields[5] and fields[5] in mc_now \
                    and fields[10].strip('"') in CC_ABILITIES:
                caster = player_names.get(fields[1], fields[1])
                tgt    = player_names.get(fields[5], fields[5])
                spell  = fields[10].strip('"')
                # Dedupe: one save per caster per controlled ally per MC episode — re-casting
                # CC to keep them parked isn't a second save.
                if caster not in mc_save_credit[fields[5]]:
                    mc_save_credit[fields[5]].add(caster)
                    rec = mc_saves[caster]
                    rec["count"] += 1; rec["spells"][spell] += 1; rec["targets"][tgt] += 1
                    if len(rec["hits"]) < 40:
                        rec["hits"].append({"t": ts_str.split()[1][:8] if " " in ts_str else "",
                                            "spell": spell, "target": tgt})

            # Consumables — flask / food / elixirs from buff auras. Any apply/refresh/
            # remove means the player had it (flasks persist through death; food/elixirs
            # get re-applied, so these events catch them).
            if ev in ("SPELL_AURA_APPLIED", "SPELL_AURA_REFRESH", "SPELL_AURA_REMOVED") \
                    and len(fields) > 10 and "Player-" in fields[5]:
                bn = fields[10].strip('"')
                if bn.startswith("Flask of") or bn == FOOD_BUFF \
                        or bn in ELIXIR_BUFFS or bn.startswith("Elixir of"):
                    cn = player_names.get(fields[5], fields[5])
                    if bn.startswith("Flask of"):                       consumes[cn]["flask"] = True
                    elif bn == FOOD_BUFF:                               consumes[cn]["food"]  = True
                    else:                                               consumes[cn]["elixirs"].add(bn)

            # Consumable USAGE (informational call-out): healthstones, runes, pots, scrolls.
            # One SPELL_CAST_SUCCESS per activation = a clean use count.
            if ev == "SPELL_CAST_SUCCESS" and "Player-" in fields[1] and len(fields) > 10:
                _spell = fields[10].strip('"')
                _cat = _consumable_category(_spell)
                if _cat:
                    _pn = player_names.get(fields[1], fields[1])
                    consum_use[_pn][_cat] += 1
                    consum_label[_pn].setdefault(_cat, _spell)   # specific name: Dark Rune / Flame Cap / Nightmare Seed

            # Combat potions (Destruction, Insane Strength, Haste, Free Action, …) log only their
            # effect BUFF, not a "… Potion" cast — count each APPLIED as one potion use. Unambiguous
            # buff names match by name; ambiguous ones (Haste / Free Action collide with trinket
            # procs) match by the potion's SPELL ID so proc spam doesn't inflate the count.
            if ev == "SPELL_AURA_APPLIED" and len(fields) > 10 and "Player-" in fields[5]:
                _eff = fields[10].strip('"')
                _potname = (POTION_NAME.get(_eff, _eff + " Potion") if _eff in POTION_BUFFS
                            else POTION_BUFF_IDS.get(fields[9]))
                if not _potname and _eff in PROTECTION_BUFFS and fields[9] not in PROTECTION_EXCLUDE_IDS:
                    _potname = _eff + " Potion"   # e.g. "Nature Protection" → "Nature Protection Potion"
                if _potname:
                    _pn = player_names.get(fields[5], fields[5])
                    consum_use[_pn]["potion"] += 1
                    # all DISTINCT combat pots they popped across the night (shared CD → a
                    # player legitimately swaps e.g. Haste on most pulls, Free Action on Vashj)
                    _cp = consum_label[_pn].setdefault("combat_pots", [])
                    if _potname not in _cp:
                        _cp.append(_potname)

            # Player→player damage. Two distinct accountability paths:
            #   (1) TARGET is Mind Controlled → a raider AoE'd the controlled ally.
            #       LIABLE — but only for intentional AoE (AOE_ABILITIES); single-target
            #       overlap doesn't count. Killing blow tracked via last_aoe_on + UNIT_DIED.
            #   (2) neither MC'd → clumping/positioning friendly fire (FF_MECHANICS).
            # Sapper self-damage and a controlled player's own swings are NOT shamed.
            if ev in ("SPELL_DAMAGE", "SPELL_PERIODIC_DAMAGE", "RANGE_DAMAGE") \
                    and "Player-" in fields[1] and len(fields) > 5 and "Player-" in fields[5] \
                    and fields[1] != fields[5]:
                ff_spell = fields[10].strip('"') if len(fields) > 10 else ""
                try:    amt = int(fields[30]) if len(fields) > 30 else 0
                except: amt = 0
                if amt > 0 and ff_spell not in FF_REFLECT:
                    if fields[5] in mc_now:                 # (1) aggressor hit a controlled ally
                        if ff_spell in AOE_ABILITIES and ff_spell not in MC_LIABLE_EXCLUDE:
                            agg = player_names.get(fields[1], fields[1])
                            vic = player_names.get(fields[5], fields[5])
                            rec = mc_liable[agg]
                            rec["dmg"] += amt; rec["hits"] += 1
                            rec["spells"][ff_spell] += amt; rec["victims"][vic] += amt
                            last_aoe_on[fields[5]] = (agg, ff_spell, amt,
                                ts_str.split()[1][:8] if " " in ts_str else "", vic)
                            if len(rec["events"]) < 40:
                                rec["events"].append({"t": ts_str.split()[1][:8] if " " in ts_str else "",
                                                      "amt": amt, "spell": ff_spell, "victim": vic, "kill": False})
                    elif fields[1] not in mc_now and ff_spell in FF_MECHANICS:  # (2) clumping
                        ff = friendly_fire[player_names.get(fields[1], fields[1])]
                        ff["dmg"] += amt; ff["incidents"] += 1
                        ff["cats"]["mechanic"] += amt
                        ff["mechanics"][ff_spell] += amt
                        ff["victims"].add(player_names.get(fields[5], fields[5]))
                        _fb = which_boss(ts)
                        if _fb: ff["bosses"].add(_fb)
                        if len(ff["hits"]) < 40:
                            ff["hits"].append({"t": ts_str.split()[1][:8] if " " in ts_str else "",
                                               "amt": amt, "mech": ff_spell, "cat": "mechanic",
                                               "boss": which_boss(ts) or ""})

            # Killing blow on a controlled teammate — credit the marquee shame to whoever
            # landed the last intentional-AoE hit on the MC'd victim before they died.
            if ev == "UNIT_DIED" and len(fields) > 5 and "Player-" in fields[5] \
                    and fields[5] in mc_now and fields[5] in last_aoe_on:
                agg, spell, amt, t, vic = last_aoe_on.pop(fields[5])
                rec = mc_liable[agg]
                rec["kills"] += 1
                for e in rec["events"]:
                    if e["spell"] == spell and e["victim"] == vic and not e["kill"]:
                        e["kill"] = True
                        break

            # Player death in a boss window — for the HP-recap timeline + reaction censoring.
            # (Real-death filter via HP→0 happens later; Feign Death keeps HP up and is dropped.)
            if ev == "UNIT_DIED" and len(fields) > 5 and fields[5].startswith("Player-"):
                _db = which_boss(ts)
                if _db:
                    log_deaths[player_names.get(fields[5], fields[5])].append((ts, _db))

            if not in_kill(ts): continue

            src_guid = fields[1]
            dst_guid = fields[5] if len(fields) > 5 else ""
            src_name = player_names.get(src_guid, src_guid)
            dst_name = player_names.get(dst_guid, dst_guid)
            src_is_player = "Player-" in src_guid
            dst_is_player = "Player-" in dst_guid
            dst_is_boss   = "Creature-" in dst_guid
            src_is_creature = "Creature-" in src_guid

            # ── Per-fight role signals + tank survivability (by boss name) ──────
            _boss = which_boss(ts)
            if _boss:
                # Incoming damage to a player → tank DTPS; SWING share = the tank signal
                if dst_is_player and src_is_creature and ev in \
                        ("SWING_DAMAGE", "SPELL_DAMAGE", "SPELL_PERIODIC_DAMAGE", "RANGE_DAMAGE"):
                    idx = 27 if ev == "SWING_DAMAGE" else 30
                    try:
                        amt = int(fields[idx])
                        fight_dmg_taken[_boss][dst_name] += amt
                        if ev == "SWING_DAMAGE":
                            fight_melee_taken[_boss][dst_name] += amt   # in the boss's face = TANK
                    except: pass
                # Healing: done by source (HEALER signal) + received by dest (tank soak).
                # Advanced-log SPELL_HEAL layout (this client): amount=[31], overheal=[32].
                elif ev in ("SPELL_HEAL", "SPELL_PERIODIC_HEAL"):
                    try:
                        eff = max(int(fields[31]) - int(fields[32]), 0)
                        if src_is_player: fight_healing_done[_boss][src_name] += eff
                        if dst_is_player: fight_heal_recv[_boss][dst_name]   += eff
                    except: pass
                # DPS: player damage to creatures (any school)
                elif ev in ("SWING_DAMAGE", "SPELL_DAMAGE", "SPELL_PERIODIC_DAMAGE", "RANGE_DAMAGE") \
                        and src_is_player and dst_is_boss:
                    idx = 27 if ev == "SWING_DAMAGE" else 30
                    try:    fight_damage_done[_boss][src_name] += int(fields[idx])
                    except: pass
                    # melee auto-attack swings (the Casts table omits these) — feeds the
                    # "Melee" row the spell-usage breakdown is otherwise missing
                    if ev == "SWING_DAMAGE":
                        melee_swings[src_name] += 1

                # HP% sample — the advanced block carries the relevant unit's HP as a percent
                # (maxHP field == "100" for players). SWING → source unit; SPELL/RANGE/HEAL →
                # target unit. So this captures damage TAKEN, heals RECEIVED, and own melee.
                if ev == "SWING_DAMAGE":               gi, hi, mi, _kind = 9, 11, 12, "dmg"
                elif ev in ("SPELL_DAMAGE", "SPELL_PERIODIC_DAMAGE", "RANGE_DAMAGE"):
                                                        gi, hi, mi, _kind = 12, 14, 15, "dmg"
                elif ev == "SPELL_HEAL":               gi, hi, mi, _kind = 12, 14, 15, "heal"   # direct heal
                elif ev == "SPELL_PERIODIC_HEAL":      gi, hi, mi, _kind = 12, 14, 15, "hot"    # HoT tick
                else:                                   gi = None
                if gi is not None and len(fields) > mi and fields[mi] == "100" \
                        and fields[gi].startswith("Player-"):
                    try:
                        _hpct = int(fields[hi])
                        if 0 <= _hpct <= 100:
                            _nm = src_name if fields[gi] == src_guid else dst_name
                            hp_samples[_nm][_boss].append((ts, _hpct, _kind))
                    except: pass

            # Swing damage → crit
            if ev == "SWING_DAMAGE" and src_is_player and dst_is_boss:
                try:
                    amt = int(fields[27])
                    dmg_totals[src_name] += amt
                    if fields[-3].strip() == "1": swing_crits[src_name] += 1
                    else: swing_hits[src_name] += 1
                except: pass

            # Spell damage → crit
            if ev == "SPELL_DAMAGE" and src_is_player and dst_is_boss:
                try:
                    amt = int(fields[30])
                    dmg_totals[src_name] += amt
                    if fields[-4].strip() == "1": spell_crits[src_name] += 1
                    else: spell_hits[src_name] += 1
                    # real engineering damage (sappers/bombs), tracked separately
                    if (fields[10].strip('"') if len(fields) > 10 else "") in ENG_DMG_NAMES:
                        eng_dmg[src_name] += amt
                except: pass

            # Interrupts: PLAYER (src) interrupts CREATURE (dst)
            if ev == "SPELL_INTERRUPT" and src_is_player and not dst_is_player:
                interrupted = fields[13].strip('"') if len(fields) > 13 else "?"
                interrupts[src_name].append(interrupted)

            # Engineering
            if ev == "SPELL_CAST_SUCCESS" and src_is_player:
                try:
                    sid = int(fields[9])
                    if sid in ENG_SPELLS:
                        eng_usage[src_name][ENG_SPELLS[sid]] += 1
                except: pass

            # Drums cast count
            if ev == "SPELL_CAST_SUCCESS" and src_is_player:
                try:
                    sid = int(fields[9])
                    if sid in DRUM_SPELLS:
                        drums_cast[src_name] += 1
                except: pass

            # Drum buffs applied
            if ev == "SPELL_AURA_APPLIED" and "BUFF" in data:
                try:
                    sid = int(fields[9])
                    if sid in DRUM_SPELLS:
                        drums_buffs[src_name] += 1
                except: pass

            # Avoidable damage taken — matched by spell name (reliable across patches).
            # Tracked per mechanic and attributed to the boss whose kill window it lands in.
            if ev in ("SPELL_DAMAGE","SPELL_PERIODIC_DAMAGE") and dst_is_player and not src_is_player:
                spell_name = fields[10].strip('"') if len(fields) > 10 else ""
                if spell_name in AVOIDABLE_SPELL_NAMES:
                    try:
                        amt = int(fields[30]) if len(fields) > 30 else 0
                        avoidable[dst_name][spell_name] += amt
                        hh = avoid_hits[dst_name][spell_name]
                        if len(hh) < 40:   # cap per (player, mechanic) to bound HTML size
                            hh.append({"t": ts_str.split()[1][:8] if " " in ts_str else "", "amt": amt})
                        boss = which_boss(ts)
                        if boss:
                            mech_boss[spell_name][boss] += amt
                    except: pass

    # Resolve COMBATANT_INFO crit (keyed by GUID) to player names now that the
    # full GUID→name map is known.
    for guid, crits in gear_crit_by_guid.items():
        name = player_names.get(guid)
        if name:
            gear_by_name[name] = crits

    # Build per-player output. Include players who only appear in COMBATANT_INFO
    # (e.g. healers who deal no boss damage) so their gear crit can be backfilled.
    all_names = (set(swing_crits) | set(spell_crits) | set(dmg_totals)
                 | set(gear_by_name) | set(avoidable))
    result = {}
    for name in all_names:
        total_hits  = swing_hits[name] + spell_hits[name]
        total_crits = swing_crits[name] + spell_crits[name]
        denom = total_hits + total_crits
        crits = gear_by_name.get(name, {})
        result[name] = {
            "actual_crit":     round(total_crits / denom * 100, 1) if denom > 0 else 0.0,
            "total_dmg":       dmg_totals[name],
            "interrupt_count": len(interrupts.get(name, [])),
            "interrupt_list":  interrupts.get(name, []),
            "eng":             dict(eng_usage.get(name, {})),
            "eng_dmg":         eng_dmg.get(name, 0),
            "avoidable_dmg":   sum(avoidable.get(name, {}).values()),
            "avoidable_sources": dict(avoidable.get(name, {})),
            "avoidable_hits":  {m: list(hs) for m, hs in avoid_hits.get(name, {}).items()},
            "_crit_melee":     crits.get("_crit_melee", 0),
            "_crit_ranged":    crits.get("_crit_ranged", 0),
            "_crit_spell":     crits.get("_crit_spell", 0),
            "_agility":        crits.get("_agility", 0),
        }

    drums_out = []
    for name, casts in sorted(drums_cast.items(), key=lambda x: -x[1]):
        buffs = drums_buffs.get(name, 0)
        drums_out.append({
            "name": name,
            "casts": casts,
            "total": casts,   # alias the dashboard reads as the per-drummer "Total"
            "buffs": buffs,
            "buffs_per_drum": round(buffs / casts, 2) if casts > 0 else 0,
            "score": buffs,
        })

    # Friendly-fire offenders (source side): who damaged teammates, how much, how often
    ff_out = []
    for name, d in sorted(friendly_fire.items(), key=lambda x: -x[1]["dmg"]):
        cats = dict(d["cats"])
        ff_out.append({
            "name": name,
            "dmg": d["dmg"],
            "incidents": d["incidents"],
            "cats": cats,                                   # {mc, engineering, mechanic}
            "category": max(cats, key=cats.get) if cats else "",   # dominant cause
            "mechanics": dict(d["mechanics"]),
            "hits": list(d.get("hits", [])),                # drill-down detail
            "victims": sorted(d.get("victims", set())),     # who got splashed (context)
            "bosses": sorted(d.get("bosses", set())),       # which fights (context)
            "mc_count": mc_count.get(name, 0),
            "mc_by": mc_source.get(name, ""),
        })

    # MC accountability — Saves (CC'd a controlled ally) and Liabilities (AoE'd one).
    mc_saves_out = []
    for name, d in sorted(mc_saves.items(), key=lambda x: -x[1]["count"]):
        mc_saves_out.append({
            "name": name, "count": d["count"],
            "spells": dict(d["spells"]), "targets": dict(d["targets"]),
            "hits": list(d.get("hits", [])),
        })
    mc_liable_out = []
    for name, d in sorted(mc_liable.items(), key=lambda x: (-x[1]["kills"], -x[1]["dmg"])):
        mc_liable_out.append({
            "name": name, "dmg": d["dmg"], "hits": d["hits"], "kills": d["kills"],
            "spells": dict(d["spells"]), "victims": dict(d["victims"]),
            "events": list(d.get("events", [])),
        })

    # Dominant boss per avoidable mechanic, for the legend's "what fight" label
    mech_boss_out = {m: max(bosses.items(), key=lambda x: x[1])[0]
                     for m, bosses in mech_boss.items() if bosses}

    consumes_out = {n: {"flask": c["flask"], "food": c["food"], "elixirs": sorted(c["elixirs"])}
                    for n, c in consumes.items()}

    # ── Per-fight roles (by boss name) from combat-log ground truth ──────────────
    # Tank  = took a real share of the boss's MELEE (immune to WCL spec-label quirks).
    # Healer= effective healing is a meaningful share of the raid's AND exceeds own damage.
    # DPS   = dealt damage and is neither. This replaces ~10 per-fight playerDetails calls.
    fight_roles_log = {}
    _bosses = set(fight_melee_taken) | set(fight_healing_done) | set(fight_damage_done)
    for boss in _bosses:
        melee = fight_melee_taken.get(boss, {})
        heald = fight_healing_done.get(boss, {})
        dmgd  = fight_damage_done.get(boss, {})
        max_melee  = max(melee.values()) if melee else 0
        total_heal = sum(heald.values()) or 1
        roles = {"Tank": [], "Healer": [], "dps": []}
        for p in (set(melee) | set(heald) | set(dmgd)):
            mt, hd, dd = melee.get(p, 0), heald.get(p, 0), dmgd.get(p, 0)
            if max_melee and mt >= max(max_melee * 0.25, 20000):
                roles["Tank"].append(p)
            elif hd >= total_heal * 0.04 and hd > dd:
                roles["Healer"].append(p)
            elif dd > 0:
                roles["dps"].append(p)
        fight_roles_log[boss] = roles

    print(f"  [LOG] {len(result)} players · {len(kills)} kill fights parsed · "
          f"{len(ff_out)} clumping FF · {len(mc_liable_out)} MC-liable · {len(mc_saves_out)} MC-savers")
    return {"players": result, "drums": drums_out, "fights": kills,
            "friendly_fire": ff_out, "avoidable_mech_boss": mech_boss_out,
            "consumables": consumes_out,
            "mc_saves": mc_saves_out, "mc_liable": mc_liable_out,
            "fight_roles_log": fight_roles_log,
            "fight_dmg_taken": {b: dict(v) for b, v in fight_dmg_taken.items()},
            "fight_heal_recv": {b: dict(v) for b, v in fight_heal_recv.items()},
            "consum_use": {n: dict(v) for n, v in consum_use.items()},
            "consum_label": {n: dict(v) for n, v in consum_label.items()},
            "melee_swings": dict(melee_swings),
            "hp_samples": {n: {b: list(s) for b, s in bs.items()} for n, bs in hp_samples.items()},
            "log_deaths": {n: list(d) for n, d in log_deaths.items()}}
