#!/usr/bin/env python3
"""
wcl_auto_dashboard.py — WarcraftLogs API → Raid KPI Dashboard auto-updater
TBC Anniversary edition

THE FACADE + ORCHESTRATOR. The pipeline's implementation lives in leaf modules; this module
re-exports their public surface — `import wcl_auto_dashboard as W` resolves every W.<name>,
which is how week_build / reprocess / backfill_snapshots / build_site / preview / the tests
all consume it (guarded by tests/test_facade_surface.py) — and keeps the orchestration:
build_week_data() (the WCL fetch recipe), the snapshot dumps, reingest_loot(), and main().

WHAT LIVES WHERE:
    wcl_client.py      OAuth + gql() transport, .env autoload (pure leaf)
    paths.py           every filesystem path constant (pure leaf)
    roles.py           spec → role classification / effective-role logic (pure leaf)
    crit_model.py      TBC expected-crit model + item-crit cache
    game_constants.py  curated WoW name-sets (consumables, CC/MC/AoE, FF, eng, drums)
    combat_log.py      raw WoWCombatLog.txt parser
    wcl_fetchers.py    every WCL v2 GraphQL fetcher: Q_* strings, fetch_master_data,
                       damage/crit, roles/healers, tanks, class toolkit, saves/interrupts/
                       dispels/deaths, debuff coverage, mana returns, sunder, gear audit
    week_map.py        map_to_week_data + merge_log_into_wcl (pure transforms; the offline
                       reprocess --from-raw tier re-runs these with zero WCL)
    week_schema.py     THE CONTRACT — SECTIONS + WeekData TypedDict + validate()
    week_build.py      THE SPINE — from_wcl → finalize_week → commit_week; calls back
                       through W.<name> lazily (the ONLY module allowed to import this one)
    trends.py          enrich_with_trends (delta_* vs the prior week's DB rows)
    render_html.py     inject_into_html (brace-depth WEEK_DATA injector — never regex)
    db_writer.py       SQLite persistence

USAGE (unchanged):
    python wcl_auto_dashboard.py <REPORT_CODE> [--log WoWCombatLog.txt] [--out path.html]
                                 [--dry-run | --test-db]
    WCL report code = the code in the URL: fresh.warcraftlogs.com/reports/<CODE>
    Credentials: WCL_CLIENT_ID / WCL_CLIENT_SECRET via .env (auto-loaded by wcl_client) or flags.
"""

from __future__ import annotations

import os, sys, json, time, argparse
from pathlib import Path
from collections import defaultdict

# Force UTF-8 console output — Windows defaults to cp1252, which can't encode the
# ✓/✅/▲ glyphs this script prints and crashes with UnicodeEncodeError.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

# ══════════════════════════════════════════════════════════════════════════════
# Public surface — re-exports. DO NOT REMOVE entries: external consumers resolve
# these as W.<name> (tests/test_facade_surface.py freezes the load-bearing set).
# Importing wcl_client also bootstraps `requests` and auto-loads .env.
# ══════════════════════════════════════════════════════════════════════════════

from wcl_client import WCL_TOKEN_URL, WCL_API_URL, get_token, gql                     # noqa: F401

from paths import (ROOT_DIR, CACHE_FILE, LOGS_DIR, LOG_ARCHIVE_DIR, LOOT_DIR, DASH_FILE,  # noqa: F401
                   TEMPLATE_FILE, DEFAULT_TITLE, WEEK_DATA_CACHE, WCL_CACHE, ITEM_META_CACHE)  # noqa: F401

from roles import (TANK_SPECS, HEALER_SPECS, CASTER_SPECS, PHYSICAL_SPECS,      # noqa: F401
                   _nontank_role, _nonheal_role, _effective_role, _fight_role)  # noqa: F401

from crit_model import (CRIT_RATING_PER_PCT, LUCK_MIN_WEEKS, CLASS_BASE_CRIT,        # noqa: F401
                        SPEC_TALENT_CRIT, GEM_CRIT_RATING, ENCHANT_CRIT_RATING,      # noqa: F401
                        AGI_PER_CRIT, INT_PER_CRIT, STAT_CRIT_RATING,                # noqa: F401
                        STAT_SPELL_CRIT, Q_ITEM_STATS, load_cache, save_cache,       # noqa: F401
                        fetch_item_crit, gear_crit_rating, expected_crit)            # noqa: F401

from game_constants import *   # noqa: F401,F403 — curated name-sets, re-exported
from combat_log import parse_combat_log, _parse_ts, _consumable_category   # noqa: F401

from wcl_fetchers import (                                                            # noqa: F401
    # core + damage/crit + gear audit
    _loads_alias, MAX_EVENT_PAGES, Q_REPORT, Q_PLAYER_DETAILS, Q_DAMAGE_TABLE,
    Q_BUFFS_TABLE, Q_COMBATANT_INFO, Q_DAMAGE_EVENTS, parse_damage_table,
    fetch_gear_from_events, fetch_actual_crit, merge_actor_names,
    fetch_damage_by_selection, content_excluded_fight_ids, fetch_master_data, _GEAR_SLOT, _ENCHANTABLE_SLOTS,
    _ILVL_SKIP_SLOTS, _item_sockets, fetch_gear_audit,
    # per-fight roles + healers
    fetch_fight_roles, build_fight_roles_from_log, harden_tank_fights,
    fetch_healing_by_fight, fetch_damage_by_fight, damage_uptime_by_fight,
    healer_uptime_by_fight, compute_healing_metrics, fetch_healing_spells,
    fetch_healer_mana, fetch_role_spell_usage,
    # tanks + class toolkit
    TANK_CD_IDS, CD_NAMES, HITTYPE_CRUSH, HITTYPE_CRIT, build_tank_scorecard_extended,
    _median, compute_death_hp_timelines, compute_reaction_times, TOOLKIT_ABILITIES,
    _toolkit_metric, _buff_uptime_batch, build_class_toolkit, fetch_engineering_casts,
    fetch_utility_actions, fetch_bloodlust_windows, _tank_survival_grade,
    # parse % (the dashboard's Performance metric — WCL rankPercent vs the FULL logged
    # population; see fetch_parse_percentiles) + utility + buffs/debuffs
    fetch_parse_percentiles, fetch_saves, fetch_interrupts, fetch_dispels,
    fetch_ability_icons, fetch_deaths_split, _build_death_timeline, DEBUFF_SLOTS,
    _merge_bands, _totem_uptime, fetch_debuff_coverage, fetch_debuff_ramp_speed,
    MANA_SOURCES, INNERVATE_ICON,
    fetch_mana_returns, fetch_sunder_armor, fetch_expose_armor, fetch_mechanic_compliance,
    MAINTAIN_UPTIME_ABILITIES, fetch_maintain_uptime,
)

from week_map import (_tally_spells, _interrupt_row, _ice_player_spells,              # noqa: F401
                      build_consumable_compliance, map_to_week_data,                  # noqa: F401
                      merge_log_into_wcl)                                             # noqa: F401

from trends import enrich_with_trends            # noqa: F401
from render_html import inject_into_html         # noqa: F401


# ══════════════════════════════════════════════════════════════════════════════
# Build WEEK_DATA — the one orchestrator: calls every fetcher in recipe order
# ══════════════════════════════════════════════════════════════════════════════

def build_week_data(report_code: str, token: str,
                    log_data: dict | None = None, report: dict | None = None, history: dict | None = None) -> dict:
    cache = load_cache()
    print(f"\n[1/5] Fetching report metadata: {report_code}")
    if report is None:   # may be pre-fetched by main() to avoid a duplicate call
        report = gql(token, Q_REPORT, {"code": report_code})["reportData"]["report"]

    zone      = (report.get("zone") or {}).get("name", "Unknown")  # WCL returns zone:null
    start_ms  = report["startTime"]                                 # until it classifies a report
    start_dt  = time.strftime("%b %d, %Y %H:%M", time.localtime(start_ms / 1000))
    # Drop off-content kills (a T4 Gruul's-Lair warmup bundled into the SSC/TK report) — they
    # pollute the roster (alt-swaps), boss tiles, tank scorecard and every denominator. Q_REPORT
    # returns kills only, so this just removes the warmup bosses; their trash is scrubbed inside
    # fetch_damage_by_selection (which queries all fights) via the same helper.
    _excl = content_excluded_fight_ids(report["fights"], EXCLUDED_ENCOUNTERS)
    if _excl:
        _dropped = sorted({f["name"] for f in report["fights"] if f["id"] in _excl})
        print(f"   ⊘ excluding off-content (T4 warmup): {', '.join(_dropped)}")
    kills     = [f for f in report["fights"] if f.get("kill") and f["id"] not in _excl]
    fight_ids = [f["id"] for f in kills]

    # Group kill times by zone for the header pills. `boss_times` stays a flat
    # {name: seconds} (the HTML reads it as a number); `boss_meta` is the additive parallel
    # key carrying portrait/encounter id, per-boss deaths and raid DPS for the new tiles.
    boss_times: dict[str, int] = {}
    boss_meta:  dict[str, dict] = {}
    for f in kills:
        duration_s = (f["endTime"] - f["startTime"]) // 1000
        boss_times[f["name"]] = duration_s
        boss_meta[f["name"]] = {"seconds": duration_s,
                                "encounter_id": f.get("encounterID"),
                                "deaths": 0}
    print(f"   Zone: {zone}  |  {len(kills)} kills  |  Fights: {fight_ids}")

    # ── Player details + gear ──────────────────────────────────────────────
    # ONE masterData fetch, shared by every consumer below (crit attribution, tank biggest-hit,
    # toolkit, mana returns, sunder, death recaps, ability icons) — see fetch_master_data. The
    # crit/damage attribution path uses the Player-filtered actor list (md["players"]) exactly as
    # before (pets must NOT be in the crit name map); pet-aware consumers use the full md["actors"].
    md        = fetch_master_data(token, report_code)
    actors    = md["players"]                       # == old actors(type:"Player")
    _abils    = md["abilities"]
    sb_ids    = {a.get("gameID") for a in _abils if (a.get("name") or "") == "Shadow Bolt"}

    print(f"\n[2/5] Fetching player details & gear (fight_ids={fight_ids[:3]}...)")
    pd_data = gql(token, Q_PLAYER_DETAILS, {"code": report_code, "fightIDs": fight_ids})
    raw_pd  = pd_data["reportData"]["report"]["playerDetails"]
    # playerDetails is a raw JSON blob: { "data": { "playerDetails": { "dps": [...], "tanks": [...], "healers": [...] } } }
    if isinstance(raw_pd, str):
        raw_pd = json.loads(raw_pd)
    pd = raw_pd.get("data", {}).get("playerDetails", raw_pd.get("data", raw_pd))

    # Flatten all players into a unified list
    players = []
    role_map = {"tanks": "Tank", "healers": "Healer", "dps": "Physical"}
    for role_key, role_label in role_map.items():
        for p in (pd.get(role_key) or []):
            # spec is in specs[0]["spec"]
            specs = p.get("specs") or []
            spec  = specs[0]["spec"] if specs else ""
            # gear lives inside combatantInfo[0]["gear"] when the logger captured COMBATANT_INFO
            ci    = p.get("combatantInfo") or []
            gear  = ci[0].get("gear", []) if ci else []
            players.append({
                "name": p["name"],
                "type": p.get("type", ""),
                "spec": spec,
                "role": role_label,
                "gear": gear,
                "potion_use":      p.get("potionUse", 0),
                "healthstone_use": p.get("healthstoneUse", 0),
            })
    # Deduplicate — WCL sometimes lists the same player in multiple role buckets.
    # Keep the entry whose role bucket matches the spec (DPS spec in tanks group = wrong).
    seen = {}
    for p in players:
        name = p["name"]
        if name not in seen:
            seen[name] = p
        else:
            # Prefer whichever has a more specific spec set, or the later one
            # (later entries tend to be the correct DPS assignment)
            existing = seen[name]
            # If existing is Tank/Healer but spec looks like DPS, replace it
            bad_tank_specs = {"Fury","Arms","Retribution","Enhancement","Survival",
                              "Marksmanship","Beast Mastery","Combat","Assassination",
                              "Subtlety","Arcane","Fire","Frost","Shadow","Elemental",
                              "Demonology","Affliction","Destruction","Balance"}
            if existing["role"] == "Tank" and existing["spec"] in bad_tank_specs:
                seen[name] = p
    players = list(seen.values())
    print(f"  Found {len(players)} unique players")

    # ── Gear from COMBATANT_INFO events (more reliable than playerDetails.combatantInfo) ──
    gear_by_name = fetch_gear_from_events(token, report_code, kills, actors)
    ci_consumables = gear_by_name.pop("__consumables__", {})   # pull-time flask/food/elixir
    group_buffs = gear_by_name.pop("__group_buffs__", {})       # gear group buffs PROVIDED (JC necks)
    for p in players:
        if not p["gear"] and p["name"] in gear_by_name:
            p["gear"] = gear_by_name[p["name"]]

    # Fix role classification — WCL sometimes puts DPS specs in "tanks" or "healers".
    # (spec sets are module-level constants, shared with fetch_fight_roles)
    for p in players:
        spec = p.get("spec", "")
        # Re-classify based on spec — more reliable than WCL's role bucket
        if spec in CASTER_SPECS:
            p["role"] = "Caster"
        elif spec in PHYSICAL_SPECS:
            p["role"] = "Physical"
        elif spec in HEALER_SPECS:
            p["role"] = "Healer"
        elif spec in TANK_SPECS and p["role"] in ("Tank", "Physical"):
            p["role"] = "Tank"  # only keep as Tank if WCL already said so

    # ── Calculate expected crit from gear ─────────────────────────────────
    print(f"\n[3/5] Calculating gear crit for {len(players)} players (may take ~30s, items are cached)...")
    for p in players:
        name = p["name"]
        if name in gear_by_name:
            # Prefer combatantinfo crit stats — critMelee/critSpell direct from WCL, no item lookup
            ci = gear_by_name[name][0]
            if p["role"] in ("Caster", "Healer"):
                crit_r = ci.get("_crit_spell", 0)
            elif p["role"] in ("Physical", "Tank"):
                crit_r = ci.get("_crit_melee", 0)
            else:
                crit_r = max(ci.get("_crit_melee", 0), ci.get("_crit_spell", 0))
        else:
            crit_r = gear_crit_rating(token, p["gear"], cache)
        ci = gear_by_name.get(name, [{}])[0]
        agi, intel = ci.get("_agility", 0), ci.get("_intellect", 0)
        p["agility"], p["intellect"] = agi, intel
        p["gear_crit_rating"] = crit_r
        p["expected_crit_pct"] = expected_crit(p["type"], p["spec"], crit_r, agi, intel)
        print(f"   {p['name']:18s}  gear_crit={crit_r:4d}  agi={agi:4d}  expected={p['expected_crit_pct']:.1f}%")
    save_cache(cache)

    # ── Actual crit from damage events + deaths table ─────────────────────
    print("\n[4/5] Fetching damage events & death table...")
    crit_counts_by_id = fetch_actual_crit(token, report_code, kills, crit_track_ids=sb_ids)
    crit_by_name      = merge_actor_names(crit_counts_by_id, actors)

    # Curated deaths (no Feign Death) from WCL, split boss vs trash by fight, + killing blows.
    death_boss, death_trash, death_recaps, deaths_by_boss = fetch_deaths_split(token, report_code, md)
    for b, n in deaths_by_boss.items():
        if b in boss_meta:
            boss_meta[b]["deaths"] = n

    # Overlay real HP% (from the combat log) onto each death recap, and build the healer
    # reaction-time heatmap — both from parse_combat_log's hp_samples / log_deaths (zero API).
    heal_reaction = {}
    if log_data:
        hp_tl = compute_death_hp_timelines(log_data)
        for nm, recaps in death_recaps.items():
            buckets = defaultdict(list)
            for lt in hp_tl.get(nm, []):
                buckets[lt["boss"]].append(lt["timeline"])
            for r in recaps:
                q = buckets.get(r.get("boss"))
                if q:
                    r["hp_timeline"] = q.pop(0)
        heal_reaction = compute_reaction_times(log_data)

    # Also pull damage totals from table (still useful even without crit breakdown)
    dmg_data     = gql(token, Q_DAMAGE_TABLE, {"code": report_code, "fightIDs": fight_ids})
    actual_stats = parse_damage_table(dmg_data["reportData"]["report"]["table"])

    # Attach actual crit (from events) + damage totals + deaths to players
    for p in players:
        ev_stats  = crit_by_name.get(p["name"], {})
        hits, crits = ev_stats.get("hits", 0), ev_stats.get("crits", 0)
        denom = hits + crits
        actual_pct = round(crits / denom * 100, 1) if denom > 0 else 0.0

        dmg_stats = actual_stats.get(p["name"], {})
        p["actual_crit_pct"]  = actual_pct
        p["total_dmg"]        = dmg_stats.get("total_dmg", 0)
        p["active_time_ms"]   = dmg_stats.get("active_time_ms", 0)
        p["deaths"]           = death_boss.get(p["name"], 0)
        p["deaths_trash"]     = death_trash.get(p["name"], 0)
        p["luck_delta"]       = round(p["actual_crit_pct"] - p["expected_crit_pct"], 2)

        # Gold-standard luck: this week's crit vs the player's OWN rolling baseline. A
        # sustained step above baseline reads as a gear upgrade; a one-week spike as luck —
        # the weekly `series` (sparkline) tells them apart. Needs MIN_WEEKS of history.
        h = (history or {}).get(p["name"])
        p["crit_baseline"] = h["mean"] if h else None
        p["crit_weeks"]    = h["n"] if h else 0
        p["crit_std"]      = h["std"] if h else None
        p["crit_series"]   = (h["series"] + [actual_pct]) if h else [actual_pct]
        p["crit_prev"]     = h["prev"] if h else None
        p["luck_hist"]     = (round(actual_pct - h["mean"], 1)
                              if h and h["n"] >= LUCK_MIN_WEEKS else None)

    # ── Total fight duration (for active time %) ───────────────────────────
    total_fight_ms = sum(f["endTime"] - f["startTime"] for f in kills)
    for p in players:
        p["active_time_pct"] = round(p["active_time_ms"] / total_fight_ms * 100, 1) if total_fight_ms else 0.0

    # ── Per-fight roles (spec-swap aware) — heal early/DPS late, prot/ret splits ──
    # Prefer combat-log ground truth (free, boss-melee tank signal beats WCL spec labels);
    # fall back to the per-fight playerDetails API only if no log or names don't line up.
    fight_roles, fight_durs = None, None
    if log_data and log_data.get("fight_roles_log"):
        fr, fd, cov = build_fight_roles_from_log(log_data["fight_roles_log"], kills)
        if cov >= 0.5:
            fight_roles, fight_durs = fr, fd
            print(f"   Per-fight roles from combat log ({cov*100:.0f}% boss-name match) — no API cost")
    if fight_roles is None:
        print("   Per-fight roles from WCL playerDetails (API)")
        fight_roles, fight_durs = fetch_fight_roles(token, report_code, kills)
        # WCL's per-fight spec labels drop ferals ('Warden') and undercount tanks, so harden
        # per-fight tank attribution from the boss-melee signal — restricted to known roster
        # tanks, additive only (see harden_tank_fights). Keeps log-less weeks from blanking a
        # tank, per the WCL-Durability Principle. (The log role path already has this signal.)
        roster_tanks = {p["name"] for p in players if p.get("role") == "Tank"}
        harden_tank_fights(token, report_code, kills, fight_roles, roster_tanks)
    # Report bosses with NO combat-log coverage (e.g. logging started mid-session) — these
    # are missing from the log-sourced KPIs, so surface them rather than silently dropping.
    logged_bosses = set((log_data or {}).get("fight_roles_log", {}).keys())
    log_missing = [f["name"] for f in kills if f["name"] not in logged_bosses] if log_data else []
    if log_missing:
        print(f"   ⚠ no combat-log coverage for: {', '.join(log_missing)}")
    # Only Warriors, Druids, and Paladins can tank a TBC raid boss. The combat-log Tank signal
    # (build_fight_roles_from_log) is the boss-melee-taken share — spec/class-blind, so a caster
    # who pulled aggro and ate a pull's worth of boss melee (a threat slip) gets tagged Tank for
    # that fight and lands on the tank scorecard (Marvels-the-Warlock as a "tank"). Gate the Tank
    # role to the three tank-capable classes — durable game truth that preserves feral/prot
    # detection. (The API path is already restricted via harden_tank_fights's roster_tanks set.)
    TANK_CAPABLE = {"Warrior", "Druid", "Paladin"}
    name_class = {p["name"]: p.get("type", "") for p in players}
    for nm, fr in fight_roles.items():
        if fr.get("Tank") and name_class.get(nm) not in TANK_CAPABLE:
            fr["dps"] = sorted(set(fr.get("dps", [])) | set(fr["Tank"]))   # they DPS'd that fight
            fr["Tank"] = []
    n_kills = len(kills)
    for p in players:
        fr = fight_roles.get(p["name"], {})
        p["fights_healed"] = len(fr.get("Healer", []))
        p["fights_tanked"] = len(fr.get("Tank", []))
        p["fights_dps"]    = len(fr.get("dps", []))
        p["fights_total"]  = n_kills
    # Anyone who tanked ≥1 fight counts as a tank target for healers' tank% split.
    tank_names = {n for n, fr in fight_roles.items() if fr.get("Tank")}

    # Healer throughput/efficiency metrics — SCOPED to each healer's heal-fights.
    heal_by_fight   = fetch_healing_by_fight(token, report_code, kills)
    healing_metrics = compute_healing_metrics(heal_by_fight, fight_roles, fight_durs, tank_names)
    healing_spells  = fetch_healing_spells(token, report_code, kills, actors, md)
    healer_mana     = fetch_healer_mana(token, report_code, fight_ids)
    # Performance metric — native WCL PARSE % (rankPercent vs the FULL logged population, not the
    # old top-100 cohort ratio). ONE fetch (report.rankings) covers all roles: the dps parse feeds
    # the DPS table AND tank threat; the hps parse feeds healers. Held in the same dps_war/tank_war/
    # healer_war keys the downstream emit/DB/render already read (values are now 0–100, not ratios).
    parse_pct  = fetch_parse_percentiles(token, report_code, kills)
    dps_war    = {nm: p["dps"] for nm, p in parse_pct.items() if p.get("dps") is not None}
    tank_war   = dps_war                                   # tank threat = their own dps parse %
    healer_war = {nm: p["hps"] for nm, p in parse_pct.items() if p.get("hps") is not None}
    # Tank scorecard — WCL is the durable source of record, run EVERY week (per-boss DTPS,
    # mitigation, school split, cooldowns, biggest hit, raid DPS for the boss tiles). The
    # combat log, when present, only adds lowest-HP%-survived; a missing log never blanks it.
    tank_metrics, boss_raid_dps = build_tank_scorecard_extended(
        token, report_code, kills, fight_roles, fight_durs, heal_by_fight, actors, md)
    for b, d in boss_raid_dps.items():
        if b in boss_meta:
            boss_meta[b]["raid_dps"] = d
    if log_data:
        # enrichment: lowest HP% each tank dropped to per boss (from the HP%-sample timeline)
        hp_samples = log_data.get("hp_samples", {})
        for nm, tm in tank_metrics.items():
            lows = {b: min(s[1] for s in samples)
                    for b, samples in hp_samples.get(nm, {}).items() if samples}
            if lows:
                tm["lowest_hp"] = lows
        # cooldown completeness: the WCL Casts query is kill-scoped (misses CDs popped on wipes) and
        # keyed by a fixed ID set; the combat log catches every defensive CD by NAME across the whole
        # night. Max-merge so neither source loses a cast (belt-and-suspenders).
        cd_casts = log_data.get("cd_casts", {})
        for nm, tm in tank_metrics.items():
            for cd, c in cd_casts.get(nm, {}).items():
                tm["cooldowns"][cd] = max(tm["cooldowns"].get(cd, 0), c)
        # active-mitigation EXECUTION signals (log-only): cast-rate / Lacerate-uptime per the tank's
        # boss-melee-taken time. Powers the tank Execution pillar (see _perfRows).
        _bms = log_data.get("boss_melee_sec", {})
        _mc  = log_data.get("mitig_cast", {})
        _lac = log_data.get("lacerate_pct", {})
        for nm, tm in tank_metrics.items():
            tm["boss_melee_sec"] = _bms.get(nm, 0)
            tm["mitig_casts"]    = _mc.get(nm, 0)
            tm["lacerate_pct"]   = _lac.get(nm, 0)   # Lacerate uptime % of active-melee time (bear)
    role_spells     = fetch_role_spell_usage(token, report_code, fight_ids, players)
    # Per-fight DamageDone — one paginated fetch feeds the uptime heatmap.
    dmg_by_fight    = fetch_damage_by_fight(token, report_code, kills)
    uptime_by_fight = damage_uptime_by_fight(dmg_by_fight, kills)        # {name: {boss: uptime%}}
    for p in players:
        p["uptime_by_fight"] = [{"boss": b, "uptime": u}
                                for b, u in uptime_by_fight.get(p["name"], {}).items()]
    # Healer casting-uptime per fight — reuses heal_by_fight (already fetched above), no extra API.
    healer_uptime = healer_uptime_by_fight(heal_by_fight, kills)
    for p in players:
        p["healer_uptime_by_fight"] = [{"boss": b, "uptime": u}
                                       for b, u in healer_uptime.get(p["name"], {}).items()]
    # Raid debuff coverage — pure WCL, per boss (CoE/Misery/Shadow Weaving/ISB + armor + judgements)
    debuff_coverage = fetch_debuff_coverage(token, report_code, kills)
    # Debuff RAMP SPEED — time-to-establish each debuff per boss (stacking → time-to-max; pure WCL)
    debuff_ramp = fetch_debuff_ramp_speed(token, report_code, kills, md)
    # Mechanic compliance — per-boss "who ate the mechanic" by verified ability-ID (pure WCL;
    # the durable headline behind avoidable damage when no combat log was transferred)
    mech_compliance = fetch_mechanic_compliance(token, report_code, kills, md)
    # Gear readiness audit — item level + enchant + gem/empty-socket compliance (WCL gear + wowhead)
    gear_audit      = fetch_gear_audit(token, report_code, kills)
    # Class toolkit — each DPS's signature class-relative utility (cast-based, pure WCL)
    mana_returns  = fetch_mana_returns(token, report_code, kills, md)
    sunder_armor  = fetch_sunder_armor(token, report_code, kills, md)
    expose_armor  = fetch_expose_armor(token, report_code, kills, md)   # rogue armor-debuff (fills the Sunder slot)
    maintain_uptime = fetch_maintain_uptime(token, report_code, kills, md)  # per-player DoT/self-buff uptime (perf v2 + §C utility)
    saves         = fetch_saves(token, report_code, kills, md)   # protective/external casts on allies
    interrupts_wcl = fetch_interrupts(token, report_code, kills, md)  # WCL-durable interrupt headline
    engineering_wcl = fetch_engineering_casts(token, report_code, kills, md)  # WCL-durable eng headline (no log)
    utility_actions = fetch_utility_actions(token, report_code, kills, md)   # HOJ + Grounding casts → util facets
    bloodlust_windows = fetch_bloodlust_windows(token, report_code, kills, md)  # per-lust-window DPS uplift + ≈HP
    dispels       = fetch_dispels(token, report_code, kills, md)  # who-dispelled-what (cleanses + purges)
    damage_by_sel = fetch_damage_by_selection(token, report_code)
    class_toolkit = build_class_toolkit(token, report_code, kills, md)
    # Fold the biggest Shadow Bolt crit (tracked free during the crit pass) into the toolkit
    # counts so the warlock metric resolver reads it like any other value.
    for nm, cd in crit_by_name.items():
        if cd.get("sb_crit"):
            class_toolkit.setdefault(nm, {})["sb_crit"] = cd["sb_crit"]
    # Fold the shadow priest's VT mana from the mana-returns batteries (the VT source already
    # paged Resources energize events there) so the toolkit doesn't run a SECOND full Resources
    # pass. The VT provider total == the old toolkit vt_mana sum (same energize events, same
    # source — VT credits the priest directly, no pet mapping). See build_class_toolkit.
    for _bat in (mana_returns or {}).get("batteries", []):
        if _bat.get("label") == MANA_SOURCES[0]["label"]:    # "Vampiric Touch"
            for _prov in _bat.get("providers", []):
                class_toolkit.setdefault(_prov["name"], {})["vt_mana"] = _prov["mana"]
    print(f"  ✓ class toolkit: {len(class_toolkit)} players with utility casts")

    # ── Assemble WEEK_DATA ─────────────────────────────────────────────────
    print("\n[5/5] Assembling WEEK_DATA...")

    # Crit lists by role
    def crit_list(role):
        return [
            {"name": p["name"], "actual": p["actual_crit_pct"], "expected": p["expected_crit_pct"], "luck": p["luck_delta"]}
            for p in sorted(players, key=lambda x: -x["actual_crit_pct"])
            if p["role"] == role and p["actual_crit_pct"] > 0
        ]

    # Deaths list
    deaths_list = [
        {"name": p["name"], "role": p["role"], "deaths": p["deaths"]}
        for p in sorted(players, key=lambda x: -x["deaths"])
        if p["deaths"] > 0
    ]

    # Damage list
    dmg_list = [
        {"name": p["name"], "role": p["role"], "total_dmg": p["total_dmg"], "active_pct": p["active_time_pct"]}
        for p in sorted(players, key=lambda x: -x["total_dmg"])
        if p["total_dmg"] > 0
    ]

    week_data = {
        "meta": {
            "report_code": report_code,
            "date": start_dt,
            "start_ms": start_ms,         # chronological sort key for the history DB
            "zone": zone,
            "kills": len(kills),
            "fight_ids": fight_ids,
            "log_missing": log_missing,   # report bosses absent from the combat log
        },
        "boss_times": boss_times,
        "boss_meta":  boss_meta,   # additive: portrait/encounter id + per-boss deaths/raid DPS
        "crit_casters":  crit_list("Caster"),
        "crit_physical": crit_list("Physical"),
        "crit_tanks":    crit_list("Tank"),
        "crit_healers":  crit_list("Healer"),
        "deaths": deaths_list,
        "damage": dmg_list,
        "players": [
            {
                "name": p["name"], "role": p["role"], "class": p["type"], "spec": p["spec"],
                "expected_crit": p["expected_crit_pct"],
                "actual_crit":   p["actual_crit_pct"],
                "luck_delta":    p["luck_delta"],
                "gear_crit_rating": p["gear_crit_rating"],
                "total_dmg":     p["total_dmg"],
                "active_pct":    p["active_time_pct"],
                "deaths":        p["deaths"],
                "deaths_trash":  p.get("deaths_trash", 0),
                "fights_total":  p.get("fights_total", 0),
                "fights_healed": p.get("fights_healed", 0),
                "fights_tanked": p.get("fights_tanked", 0),
                "fights_dps":    p.get("fights_dps", 0),
                "uptime_by_fight": p.get("uptime_by_fight", []),
                "healer_uptime_by_fight": p.get("healer_uptime_by_fight", []),
                "crit_baseline": p.get("crit_baseline"),
                "crit_weeks":    p.get("crit_weeks", 0),
                "crit_std":      p.get("crit_std"),
                "crit_series":   p.get("crit_series", []),
                "crit_prev":     p.get("crit_prev"),
                "luck_hist":     p.get("luck_hist"),
            }
            for p in players
        ],
        # ability name → real WCL icon slug, for the Hall of Shame legend
        "ability_icons": fetch_ability_icons(token, report_code, fight_ids, md),
        # accurate pull-time consumables (from combatantinfo auras)
        "ci_consumables": ci_consumables,
        # gear-provided GROUP buffs PROVIDED per player (JC necks) → Utility credit
        "group_buffs": group_buffs,
        # per-player healing throughput/efficiency + per-spell breakdown + mana
        "healing_metrics": healing_metrics,
        "healing_spells":  healing_spells,
        "healer_mana":     healer_mana,
        "healer_war":      healer_war,
        # Performance metric — WCL parse % (rankPercent, 0–100) per player; folded onto
        # damageBySelection / tankScorecard rows in map_to_week_data (kept in the legacy
        # dps_war/tank_war/healer_war keys + vs_replacement field, which now hold the percentile).
        "dps_war":         dps_war,
        "tank_war":        tank_war,
        # per-fight roles (spec-swap aware) + per-fight durations for tank scorecard
        "fight_roles":     fight_roles,
        "fight_durs":      fight_durs,
        "tank_metrics":    tank_metrics,
        # per-player killing blows for the death drill-down
        "death_recaps":    death_recaps,
        # healer reaction-time heatmap (median s to first heal after dropping <50% HP)
        "heal_reaction":   heal_reaction,
        # top abilities cast per role + per player
        "role_spells":     role_spells.get("roles", {}),
        "player_spells":   role_spells.get("players", {}),
        # per-boss uptime of key DPS-amplifying raid debuffs (pure WCL)
        "debuff_coverage": debuff_coverage,
        # per-debuff ramp speed — time-to-establish (stacking → time-to-max) + uptime-at-full (pure WCL)
        "debuff_ramp": debuff_ramp,
        # per-boss per-mechanic "who ate it" by verified ability-ID (pure WCL)
        "mech_compliance": mech_compliance,
        # gear readiness audit — item level / enchant / gem compliance (WCL gear + wowhead sockets)
        "gear_audit":      gear_audit,
        # per-player signature class-utility cast counts (pure WCL Casts)
        "class_toolkit":   class_toolkit,
        # mana returned to the raid, per provider (mana-battery leaderboard)
        "mana_returns":    mana_returns,
        # per-player Sunder Armor quality (effective/refreshed/wasted, pure WCL)
        "sunder_armor":    sunder_armor,
        # per-rogue Expose Armor uptime — same armor slot as Sunder, mutually exclusive (pure WCL)
        "expose_armor":    expose_armor,
        # per-player MAINTAIN uptime% (DoTs/self-buffs) + spec-baseline utility debuffs (CoE/IFF/
        # Expose Weakness) — the input-based Performance v2 overlay + §C utility credits (pure WCL)
        "maintain_uptime": maintain_uptime,
        # protective/external casts on allies (paladin Hands, battle-res, reactive utility) — "saving others"
        "saves":           saves,
        # WCL-durable interrupt headline (events) — name → {count, spells}; map prefers this, log fallback
        "interrupts_wcl":  interrupts_wcl,
        # WCL-durable engineering headline (Casts events) — name → {ability_name: count}; log overlay
        # (real sapper/bomb damage) stays primary when present, this backfills counts when no log
        "engineering_wcl": engineering_wcl,
        "utility_actions": utility_actions,
        # per-lust-window raid-DPS uplift vs baseline + ≈boss-HP at cast (pull-burn vs execute-save)
        "bloodlust_windows": bloodlust_windows,
        # who-dispelled-what — cleanses off allies + offensive purges on enemies (WCL Dispels events)
        "dispels":         dispels,
        # per-player damage + active time split All/Bosses/Trash (WCL-style DPS denominator)
        "damage_by_sel":   damage_by_sel,
    }

    return week_data


# ══════════════════════════════════════════════════════════════════════════════
# Pipeline tail — snapshot dumps + loot re-ingest (stay here: they ARE the facade's
# orchestration glue; week_build calls them through W.* at call time, preserving the
# test_week_build monkeypatch seam. Never import them into week_build directly.)
# ══════════════════════════════════════════════════════════════════════════════

def dump_week_data_cache(mapped: dict) -> None:
    """Persist a mapped WEEK_DATA dict to cache/week_data/<report>.json — the offline-reprocess
    source of record (reprocess.py --all rebuilds the whole DB + trends from these, zero WCL).

    Cache the CLEAN mapped data (call BEFORE enrich_with_trends) so a from-scratch reprocess
    recomputes deltas fresh instead of inheriting stale ones. The mapped dict is already
    json-serializable (inject_into_html json.dumps's it). Never raises — a cache miss must not
    break the run (same discipline as the db_writer write_week warning)."""
    try:
        code = (mapped.get("meta") or {}).get("report_code")
        if not code:
            print("  ⚠ week_data cache: no report_code in meta — skipping")
            return
        WEEK_DATA_CACHE.mkdir(parents=True, exist_ok=True)
        out = WEEK_DATA_CACHE / f"{code}.json"
        out.write_text(json.dumps(mapped, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"  ✓ cached WEEK_DATA → week_data/{out.name}")
    except Exception as e:
        print(f"  ⚠ week_data cache write failed: {e}")


def dump_wcl_raw_cache(raw: dict) -> None:
    """Persist the PRE-map merged `wcl` dict to cache/wcl/<report>.json — the source for
    `reprocess.py --from-raw`, which re-runs map_to_week_data() on it (zero WCL) so a NEW
    map-derived metric repopulates across past weeks offline.

    The merged wcl dict is already json-serializable: build_week_data + combat_log convert every
    set→sorted list and defaultdict→plain dict at construction (so json.dumps needs no custom
    encoder). Sibling of dump_week_data_cache and gated the same way by callers (skip --test-db, so a
    proof never pollutes the canonical cache). Never raises — a cache miss must not break the run."""
    try:
        code = (raw.get("meta") or {}).get("report_code")
        if not code:
            print("  ⚠ wcl raw cache: no report_code in meta — skipping")
            return
        WCL_CACHE.mkdir(parents=True, exist_ok=True)
        out = WCL_CACHE / f"{code}.json"
        out.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"  ✓ cached raw wcl → wcl/{out.name}")
    except Exception as e:
        print(f"  ⚠ wcl raw cache write failed: {e}")


def reingest_loot(mapped: dict, csv_path: str | None = None) -> dict:
    """Re-attach a week's loot to a MAPPED week_data from a ThatsBIS 'received' CSV, keyed on the
    raid-night date (from meta.start_ms). Loot is normally ingested ONLY by the live pipeline's
    `--loot` step (main()), so the OFFLINE rebuild paths — backfill_snapshots.py (rebuilds a
    snapshot from WCL) and reprocess.py --all (re-renders/re-persists from snapshots) — would
    silently DROP loot from any week they touch unless they call this. Newest loot/*.csv is
    auto-picked. No-op (loot untouched) if there's no CSV or no awards dated that night, so it
    never clobbers existing loot with emptiness on a date the CSV doesn't cover. Never raises."""
    try:
        if csv_path is None:
            csvs = sorted(LOOT_DIR.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
            if not csvs:
                return mapped
            csv_path = str(csvs[0])
        sm = (mapped.get("meta") or {}).get("start_ms")
        if sm is None:
            return mapped
        raid_date = time.strftime("%Y-%m-%d", time.localtime(sm / 1000))
        import loot_parser
        ld = loot_parser.parse_loot(csv_path, raid_date)
        if ld:
            mapped["loot"] = ld
            print(f"  + loot re-ingested: {ld['total']} items to {len(ld['players'])} raiders on {raid_date}")
    except Exception as e:
        print(f"  loot re-ingest warning: {e}")
    return mapped


def print_summary(week_data: dict):
    print("\n" + "="*60)
    print(f"REPORT: {week_data['meta']['report_code']}  |  {week_data['meta']['date']}")
    print(f"Zone: {week_data['meta']['zone']}  |  Kills: {week_data['meta']['kills']}")
    print("\n─── Luck KPI (actual crit − expected crit) ───")
    for p in sorted(week_data["players"], key=lambda x: -x["luck_delta"]):
        bar = "▲" if p["luck_delta"] > 0 else ("▼" if p["luck_delta"] < 0 else "─")
        print(f"  {bar} {p['name']:18s}  expected={p['expected_crit']:5.1f}%  "
              f"actual={p['actual_crit']:5.1f}%  delta={p['luck_delta']:+.1f}%")
    print("\n─── Deaths ───")
    for d in week_data["deaths"]:
        print(f"  {d['name']:18s}  {d['deaths']} deaths")


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="WCL → Raid KPI Dashboard updater")
    parser.add_argument("report_code", help="WCL report code (from URL)")
    parser.add_argument("--client-id",     default=os.getenv("WCL_CLIENT_ID"),     help="WCL V2 Client ID")
    parser.add_argument("--client-secret", default=os.getenv("WCL_CLIENT_SECRET"), help="WCL V2 Client Secret")
    parser.add_argument("--out", default=str(DASH_FILE),
                        help="Path to HTML dashboard to update")
    parser.add_argument("--log", default=None,
                        help="Path to WoWCombatLog.txt/.zip/.gz (default: auto-match WOW_LOG_DIR "
                             "against the report's time window, else newest WoWCombatLog*.txt in logs/)")
    # Auto-find newest ThatsBIS loot CSV in loot/ — external/optional; absent = Loot card hides.
    _auto_loot = None
    _loot_files = sorted(LOOT_DIR.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True) \
        if LOOT_DIR.exists() else []
    if _loot_files:
        _auto_loot = str(_loot_files[0])
    parser.add_argument("--loot", default=_auto_loot,
                        help="Path to ThatsBIS received-loot CSV (default: newest *.csv in loot/)")
    parser.add_argument("--dry-run",  action="store_true", help="Print JSON only, don't write HTML")
    parser.add_argument("--test-db",  action="store_true", help="Write to raid_history_test.db instead of prod")
    args = parser.parse_args()

    if not args.client_id or not args.client_secret:
        print("ERROR: Set WCL_CLIENT_ID and WCL_CLIENT_SECRET env vars, or pass --client-id / --client-secret")
        sys.exit(1)

    print("Authenticating with WCL...")
    token = get_token(args.client_id, args.client_secret)
    print("  ✓ Token obtained")

    # Fetch the report's kill list once, up front — used to (a) scope the combat log to
    # THIS report's bosses (ignore off-report DST clears like Gruul/HKM) and (b) feed
    # build_week_data so it doesn't re-fetch.
    rep0 = gql(token, Q_REPORT, {"code": args.report_code})["reportData"]["report"]

    # Resolve the combat log. Precedence: explicit --log > a WOW_LOG_DIR file whose time range
    # covers THIS report's window (read straight from the game's Logs dir — no weekly copy, and
    # an alt session's file can't be picked by accident) > newest in logs/ (zero-config path).
    import log_discovery
    log_path = args.log
    if log_path is None and os.getenv("WOW_LOG_DIR"):
        log_path = log_discovery.discover_log(rep0["startTime"], rep0["endTime"],
                                              os.getenv("WOW_LOG_DIR"))
    if log_path is None:
        # Window-match the logs/ fallback too — never blind-newest. A leftover log from another raid
        # night must not be picked just because it's the most recent .txt (the Jun-1-log-on-May-weeks
        # contamination). Defense-in-depth: week_build.from_wcl ALSO rejects a non-covering log; this
        # stops the wrong file being SELECTED in the first place.
        _S = log_discovery.to_log_scale(rep0["startTime"]); _E = log_discovery.to_log_scale(rep0["endTime"])
        _win = max(_E - _S, 1.0)
        _cands = sorted(LOGS_DIR.glob("WoWCombatLog*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
        for _p in _cands:
            _rng = log_discovery.sniff_log_range(_p)
            if _rng and (max(0.0, min(_E, _rng[1]) - max(_S, _rng[0])) / _win) >= log_discovery.MATCH_FLOOR:
                log_path = str(_p)
                print(f"  [LOG] using window-matched in logs/: {_p.name}")
                break
        else:
            if _cands:
                print(f"  [LOG] {len(_cands)} log(s) in logs/ but none cover the report window — building WCL-only")

    # Per-player crit baseline from history (gold-standard 'luck' = this week vs your own
    # multi-week average). Read from the SAME db the run will write to, excluding this report.
    from db_writer import crit_history, DB_PATH, DB_PATH_TEST
    db_path = DB_PATH_TEST if args.test_db else DB_PATH
    crit_hist = crit_history(db_path, exclude_report=args.report_code)

    # Build → finalize → commit through the shared week_build spine — the SAME path backfill and
    # reprocess use, so the live run can't drift from them (the loot-drop / role / order bug class).
    # from_wcl parses the log (scoped to rep0's bosses) and feeds build_week_data → no double fetch.
    import week_build as wb
    mapped, week_data, log_data = wb.from_wcl(
        args.report_code, token, log_path=log_path, report=rep0,
        history=crit_hist)

    # finalize_week owns the ONE loot-ingestion point (replacing a raw-dict loot_data block) plus
    # the contract check. Pass the resolved --loot path so an explicit CSV isn't overridden by mtime.
    wb.finalize_week(mapped, has_log=log_data is not None, loot_csv=args.loot, label=args.report_code)

    print_summary(week_data)

    # Officer-only AI performance one-liners (optional, fully guarded — no key/SDK/error ⇒ no-op).
    # BEFORE commit_week so the summaries land in BOTH the snapshot dump and the injected HTML.
    # Skipped on --test-db / --dry-run so proofs never spend on the API.
    if not args.dry_run and not args.test_db:
        try:
            import perf_summaries
            perf_summaries.attach(mapped)
        except Exception as e:
            print(f"  perf summaries: skipped ({e.__class__.__name__}) — dashboard unaffected")

    if args.dry_run:
        print("\n─── WCL_AUTO_DATA JSON ───")
        print(json.dumps(week_data, indent=2, ensure_ascii=False))
    else:
        # Cache the PRE-map merged wcl dict for offline re-derivation (reprocess.py --from-raw) — a
        # sibling of the week_data snapshot that commit_week dumps below. Same test-gating: a --test-db
        # proof must never pollute the canonical cache. `week_data` here IS the raw merged dict.
        if not args.test_db:
            dump_wcl_raw_cache(week_data)
        # commit: dump (clean, test-gated) → enrich (strictly before write) → write_week. Then render
        # to --out explicitly (commit_week renders to DASH_FILE; main honors a custom --out path).
        wb.commit_week(mapped, db_path, is_test=args.test_db, dump=True, render=False)
        inject_into_html(week_data, Path(args.out), mapped=mapped)
        # Archive the consumed log (auto-picked .txt only — an explicit --log is operator-
        # managed and may itself be an archive). Zip→verify→remove keeps next week's discovery
        # list short while preserving the log for backfill_snapshots --log re-enrichment.
        if (not args.test_db and log_data is not None and args.log is None
                and log_path and log_path.lower().endswith(".txt")):
            log_discovery.archive_log(log_path, args.report_code, rep0["startTime"],
                                      LOG_ARCHIVE_DIR)
        print("\nRun next time with:")
        print(f"  python wcl_auto_dashboard.py {args.report_code} --out \"{args.out}\"")


if __name__ == "__main__":
    main()
