#!/usr/bin/env python3
"""
wcl_auto_dashboard.py — WarcraftLogs API → Raid KPI Dashboard auto-updater
TBC Anniversary edition

USAGE:
    python wcl_auto_dashboard.py <REPORT_CODE>
    python wcl_auto_dashboard.py <REPORT_CODE> --log WoWCombatLog.txt
    python wcl_auto_dashboard.py <REPORT_CODE> --out "C:/path/to/raid_kpi_dashboard.html"
    python wcl_auto_dashboard.py <REPORT_CODE> --dry-run   # print JSON only

    WCL report code = the code in the URL: fresh.warcraftlogs.com/reports/<CODE>
    --log  = optional path to WoWCombatLog.txt; fills in actual crit %, avoidable damage,
             interrupts, engineering, and drums that WCL API doesn't expose directly.

SETUP (one time):
    Set two environment variables from https://www.warcraftlogs.com/api/clients/
        WCL_CLIENT_ID=your_client_id
        WCL_CLIENT_SECRET=your_client_secret
    Or pass them as --client-id / --client-secret flags.

WHAT IT DOES:
    1. Pulls fight list + player gear from WCL v2 GraphQL
    2. Looks up crit rating on each item/gem/enchant (cached locally)
    3. Calculates EXPECTED crit % per player (TBC formula: rating / 22.08 + class base)
    4. Pulls hit/crit counts from damage table → ACTUAL crit %
    5. Luck KPI = actual − expected  (green = ran hot, orange = ran cold)
    6. Pulls deaths, damage totals, avoidable dmg, consumable uptimes
    7. Injects updated WEEK_DATA into the HTML dashboard
"""

from __future__ import annotations

import os, sys, json, time, argparse
from pathlib import Path
from collections import defaultdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:                       # resolved only by a type checker — no runtime import / cycle
    from week_schema import WeekData

# Force UTF-8 console output — Windows defaults to cp1252, which can't encode the
# ✓/✅/▲ glyphs this script prints and crashes with UnicodeEncodeError.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

# Auto-install requests if missing
try:
    import requests as _req
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests", "--quiet"])
    import requests as _req  # noqa: F401 — bootstrap import for its install side effect

# Auto-load .env from Gaming root (parent of scripts/)
_env_file = Path(__file__).parent.parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ[_k.strip()] = _v.strip()  # .env always wins over system env vars

# ── WCL endpoints + transport ─────────────────────────────────────────────────
# OAuth + the GraphQL wrapper live in wcl_client (a pure transport leaf). Re-exported here
# so every existing W.gql / W.get_token / W.WCL_API_URL reference keeps resolving.
from wcl_client import WCL_TOKEN_URL, WCL_API_URL, get_token, gql  # noqa: F401 — re-exported (W.WCL_API_URL etc.)

# ── TBC crit model (extracted module) ─────────────────────────────────────────
# The expected-crit model (class base / talent / gem / enchant / primary-stat tables +
# expected_crit), the item-crit cache I/O, and its WCL item lookup live in crit_model.py.
# Re-exported here so existing bare-name and W.<name> references resolve unchanged.
from crit_model import (CRIT_RATING_PER_PCT, LUCK_MIN_WEEKS, CLASS_BASE_CRIT,        # noqa: F401
                        SPEC_TALENT_CRIT, GEM_CRIT_RATING, ENCHANT_CRIT_RATING,      # noqa: F401
                        AGI_PER_CRIT, INT_PER_CRIT, STAT_CRIT_RATING,                # noqa: F401
                        STAT_SPELL_CRIT, Q_ITEM_STATS, load_cache, save_cache,       # noqa: F401
                        fetch_item_crit, gear_crit_rating, expected_crit)            # noqa: F401

# ── Curated game-data constants + combat-log parser (extracted modules) ──────────
# game_constants: all curated WoW name-sets (consumables, CC/MC/AoE, friendly-fire,
#   engineering, drums) + the T5 content seam. combat_log: the raw WoWCombatLog parser.
# Both are re-exported so every existing W.<name> reference keeps resolving.
from game_constants import *   # noqa: F401,F403 — curated name-sets, re-exported
from combat_log import parse_combat_log, _parse_ts, _consumable_category   # noqa: F401









# ── Filesystem paths (extracted module) ──────────────────────────────────────
# All path constants live in paths.py (a pure-constants leaf). Re-exported here so every
# existing W.DASH_FILE / W.WEEK_DATA_CACHE / W.ROOT_DIR reference keeps resolving.
from paths import (ROOT_DIR, CACHE_FILE, LOGS_DIR, LOOT_DIR, DASH_FILE, TEMPLATE_FILE,  # noqa: F401
                   DEFAULT_TITLE, WEEK_DATA_CACHE, WCL_CACHE, ITEM_META_CACHE)          # noqa: F401

# Performance metric = the native WCL PARSE % (rankPercent from report.rankings) — vs the FULL
# logged population, NOT the old top-100 cohort ratio (which made solid raiders read "below
# replacement"). See fetch_parse_percentiles. No cohort cache / baseline / TTL needed — WCL scores
# it server-side; we just read it. (The old healer_baseline.json + dps/tank_baseline.json caches are
# now orphaned and can be deleted from cache/.)

# ── WCL fetchers (extracted module) ───────────────────────────────────────────
# The WCL v2 GraphQL fetch layer lives in wcl_fetchers.py (moving cluster-by-cluster).
# Re-exported here so existing bare-name and W.<name> references resolve unchanged.
from wcl_fetchers import (_loads_alias, MAX_EVENT_PAGES, Q_REPORT, Q_PLAYER_DETAILS,         # noqa: F401
                          Q_DAMAGE_TABLE, Q_BUFFS_TABLE, Q_COMBATANT_INFO, Q_DAMAGE_EVENTS,   # noqa: F401
                          parse_damage_table, fetch_gear_from_events, fetch_actual_crit,      # noqa: F401
                          merge_actor_names, fetch_damage_by_selection, fetch_master_data,    # noqa: F401
                          _GEAR_SLOT, _ENCHANTABLE_SLOTS, _ILVL_SKIP_SLOTS, _item_sockets,    # noqa: F401
                          fetch_gear_audit)                                                   # noqa: F401


# ── Spec → role classification (extracted module) ─────────────────────────────
# Lives in roles.py (a pure leaf). Re-exported here so existing bare-name references
# (build_week_data's reclassifier, per-fight roles, map_to_week_data) resolve unchanged.
from roles import (TANK_SPECS, HEALER_SPECS, CASTER_SPECS, PHYSICAL_SPECS,      # noqa: F401
                   _nontank_role, _nonheal_role, _effective_role, _fight_role)  # noqa: F401


from wcl_fetchers import (fetch_fight_roles, build_fight_roles_from_log,             # noqa: F401
                          harden_tank_fights, fetch_healing_by_fight,                 # noqa: F401
                          fetch_damage_by_fight, damage_uptime_by_fight,              # noqa: F401
                          healer_uptime_by_fight, compute_healing_metrics,            # noqa: F401
                          fetch_healing_spells, fetch_healer_mana,                    # noqa: F401
                          fetch_role_spell_usage)                                     # noqa: F401


from wcl_fetchers import (TANK_CD_IDS, CD_NAMES, HITTYPE_CRUSH, HITTYPE_CRIT,        # noqa: F401
                          build_tank_scorecard_extended, _median,                     # noqa: F401
                          compute_death_hp_timelines, compute_reaction_times,         # noqa: F401
                          TOOLKIT_ABILITIES, _toolkit_metric, _buff_uptime_batch,     # noqa: F401
                          build_class_toolkit, _tank_survival_grade)                  # noqa: F401






from wcl_fetchers import (fetch_parse_percentiles, fetch_saves, fetch_interrupts,    # noqa: F401
                          fetch_dispels, fetch_ability_icons, fetch_deaths_split,     # noqa: F401
                          _build_death_timeline, DEBUFF_SLOTS, fetch_debuff_coverage, # noqa: F401
                          MANA_SOURCES, INNERVATE_ICON, fetch_mana_returns,           # noqa: F401
                          fetch_sunder_armor)                                         # noqa: F401




# ══════════════════════════════════════════════════════════════════════════════
# Build WEEK_DATA
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
    kills     = [f for f in report["fights"] if f.get("kill")]
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
    healing_spells  = fetch_healing_spells(token, report_code, kills, actors)
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
    # Gear readiness audit — item level + enchant + gem/empty-socket compliance (WCL gear + wowhead)
    gear_audit      = fetch_gear_audit(token, report_code, kills)
    # Class toolkit — each DPS's signature class-relative utility (cast-based, pure WCL)
    mana_returns  = fetch_mana_returns(token, report_code, kills, md)
    sunder_armor  = fetch_sunder_armor(token, report_code, kills, md)
    saves         = fetch_saves(token, report_code, kills, md)   # protective/external casts on allies
    interrupts_wcl = fetch_interrupts(token, report_code, kills, md)  # WCL-durable interrupt headline
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
        # gear readiness audit — item level / enchant / gem compliance (WCL gear + wowhead sockets)
        "gear_audit":      gear_audit,
        # per-player signature class-utility cast counts (pure WCL Casts)
        "class_toolkit":   class_toolkit,
        # mana returned to the raid, per provider (mana-battery leaderboard)
        "mana_returns":    mana_returns,
        # per-player Sunder Armor quality (effective/refreshed/wasted, pure WCL)
        "sunder_armor":    sunder_armor,
        # protective/external casts on allies (paladin Hands, battle-res, reactive utility) — "saving others"
        "saves":           saves,
        # WCL-durable interrupt headline (events) — name → {count, spells}; map prefers this, log fallback
        "interrupts_wcl":  interrupts_wcl,
        # who-dispelled-what — cleanses off allies + offensive purges on enemies (WCL Dispels events)
        "dispels":         dispels,
        # per-player damage + active time split All/Bosses/Trash (WCL-style DPS denominator)
        "damage_by_sel":   damage_by_sel,
    }

    return week_data


# ══════════════════════════════════════════════════════════════════════════════
# Map WCL data → WEEK_DATA format expected by the HTML dashboard
# ══════════════════════════════════════════════════════════════════════════════

def _tally_spells(names, icons=None):
    """Tally a list of interrupted spell NAMES into [{spell, n, icon}], most-kicked first.
    `icons` is the ability-name → WCL icon-slug map, so each cast carries its real game icon."""
    icons = icons or {}
    agg = {}
    for s in (names or []):
        agg[s] = agg.get(s, 0) + 1
    return sorted(({"spell": s, "n": n, "icon": icons.get(s, "")} for s, n in agg.items()),
                  key=lambda x: -x["n"])


def _interrupt_row(p, intr_wcl, icons):
    """One interrupts-KPI row for player `p`, preferring the WCL headline (events) and falling back
    to the combat log only when WCL has no row for them. Returns None if neither source has kicks.
    WCL `spells` is {interruptedSpellName: count}; the log path tallies its raw interrupt_list."""
    w = (intr_wcl or {}).get(p["name"])
    if w and w.get("count", 0) > 0:
        spells = sorted(({"spell": s, "n": n, "icon": (icons or {}).get(s, "")}
                         for s, n in (w.get("spells") or {}).items()), key=lambda x: -x["n"])
        return {"name": p["name"], "role": p["role"], "count": w["count"], "spells": spells}
    log_n = p.get("interrupt_count", 0)
    if log_n > 0:
        return {"name": p["name"], "role": p["role"], "count": log_n,
                "spells": _tally_spells(p.get("interrupt_list"), icons)}
    return None


def _ice_player_spells(player_spells, icons):
    """Attach a WCL icon slug to every ability in the per-player spell-usage map, so the
    'Spell Usage by Player' card can render the same icon pills as the interrupt list."""
    icons = icons or {}
    return {
        role: [{**pl, "abilities": [{**a, "icon": icons.get(a.get("ability"), "")}
                                     for a in (pl.get("abilities") or [])]}
               for pl in plist]
        for role, plist in (player_spells or {}).items()
    }


def build_consumable_compliance(consumable_usage):
    """Reshape consumableUsage into the role-split compliance grid shape — a pure transform,
    no new queries. Each row → {name, role, flask, food, weapon, combat_pots, alt_pot}.
    Flask/Elixirs passes on a flask OR both elixir slots (battle AND guardian)."""
    out = []
    for e in consumable_usage:
        elixirs = e.get("elixirs") or []
        has_guardian = any(x in GUARDIAN_ELIXIRS for x in elixirs)
        has_battle   = any(x not in GUARDIAN_ELIXIRS for x in elixirs)
        flask_ok = bool(e.get("flask")) or (has_battle and has_guardian)
        # Alt-pot slot = the situational mana-sustain / utility consumable. Priority (show the item
        # name): Nightmare Seed > Flame Cap > Dark/Demonic Rune > Mana Gem. A mage's Mana Gem is its
        # SEPARATE-cooldown mana sustain (a mage can use a gem AND a potion), so it fills this slot
        # when no rune/seed/cap was used — crediting an Arcane mage who lives on gems (was uncredited).
        if   e.get("nightmare_seed"): alt_pot = "Nightmare Seed"
        elif e.get("flamecap"):       alt_pot = "Flame Cap"
        elif e.get("rune_name"):      alt_pot = e["rune_name"]
        elif e.get("rune"):           alt_pot = "Mana Rune"   # used but specific name not captured
        elif e.get("mana_gem"):       alt_pot = "Mana Gem"    # mage conjured gem (Replenish Mana)
        else:                         alt_pot = None
        out.append({
            "name":  e["name"],
            "role":  e.get("role", ""),
            "flask": flask_ok,
            # show-our-work: the SPECIFIC items behind each ✓, so the grid is auditable
            "flask_name": e.get("flask") or None,      # the flask, when one is present
            "elixirs":    elixirs,                     # the 2-elixir path (battle + guardian)
            "food":  bool(e.get("food")),
            "weapon": bool(e.get("weapon_oil")),
            "combat_pots": e.get("combat_pots", []),   # all combat pots popped (may be empty)
            "alt_pot": alt_pot,
            # fight-distribution context — used by JS to bucket hybrid players correctly
            "effective_role": e.get("effective_role", e.get("role", "")),
            "fights_tanked":  e.get("fights_tanked", 0),
            "fights_healed":  e.get("fights_healed", 0),
            "fights_total":   e.get("fights_total", 0),
        })
    return out


def map_to_week_data(wcl: dict) -> WeekData:
    """Convert WCL API output to the WEEK_DATA structure the HTML renders from."""
    players = wcl.get("players", [])

    def _eff(p):
        """Effective (played) role for a player dict from the `players` whitelist."""
        return _effective_role(p.get("role", ""), p.get("spec", ""),
                               p.get("fights_tanked", 0), p.get("fights_healed", 0),
                               p.get("fights_total", 0))

    _toolkit_counts = wcl.get("class_toolkit", {})
    _tk_icons       = wcl.get("ability_icons", {})
    _kill_min       = sum((wcl.get("boss_times") or {}).values()) / 60.0
    _dps_war        = wcl.get("dps_war", {})    # name → WCL dps parse % (DPS table cell)
    _tank_war       = wcl.get("tank_war", {})   # name → WCL dps parse % (tank threat cell)

    def _toolkit_cell(p):
        """Signature class-utility cell for a damage row, or None. Resolves the per-class
        metric from cast counts + attaches a live icon and a ⚔ off-role annotation for
        hybrids (a tank/healer who mostly DPS'd), mirroring the consumables report."""
        m = _toolkit_metric(p.get("class", ""), p.get("spec", ""),
                            _toolkit_counts.get(p["name"], {}), _kill_min)
        if not m:
            return None
        icon = _tk_icons.get(m["icon_ability"]) or m["fallback"]
        cell = {"label": m["label"], "value": m["value"], "title": m["title"], "icon": icon}
        if m.get("num") is not None:
            cell["num"] = m["num"]          # raw numeric, for week-over-week trending
        if m.get("tag"):
            cell["tag"] = m["tag"]
        # hybrid tag: this metric belongs to someone whose roster role isn't what they played
        if _eff(p) != p.get("role") and p.get("fights_dps", 0) > 0:
            cell["off"] = f"{p.get('fights_dps', 0)}/{p.get('fights_total', 0)}"
        return cell

    def crit_list(role):
        return [
            {"name": p["name"], "crit": p["actual_crit"]}
            for p in sorted(players, key=lambda x: -x["actual_crit"])
            if p["role"] == role and p["actual_crit"] > 0
        ]

    # Deaths — boss + trash, from the combat-log encounter-window split
    _recaps = wcl.get("death_recaps") or {}
    death_list = [
        {"name": p["name"], "role": p["role"],
         "total": p.get("deaths", 0) + p.get("deaths_trash", 0),
         "trash": p.get("deaths_trash", 0),
         "recap": _recaps.get(p["name"], [])}
        for p in sorted(players, key=lambda x: -(x.get("deaths", 0) + x.get("deaths_trash", 0)))
        if (p.get("deaths", 0) + p.get("deaths_trash", 0)) > 0
    ]

    # Luck KPI tiles — luck is now vs the player's OWN multi-week baseline (gold standard).
    # `expected`/`luck_delta` (gear-modeled) are kept as a stored reference only.
    luck_tiles = [
        {
            "name": p["name"], "role": p["role"],
            "actual": p["actual_crit"], "expected": p["expected_crit"],
            "baseline": p.get("crit_baseline"), "weeks": p.get("crit_weeks", 0),
            "std": p.get("crit_std"), "series": p.get("crit_series", []),
            "prev": p.get("crit_prev"),
            "luck": p.get("luck_hist"),   # actual − baseline; None until LUCK_MIN_WEEKS
        }
        for p in sorted(players, key=lambda x: -(x.get("luck_hist") if x.get("luck_hist") is not None else -999))
        if p["actual_crit"] > 0
    ]

    # Consumables → role-split COMPLIANCE grid (binary ✓/✗ per role). A pure reshape of
    # consumableUsage via build_consumable_compliance — consum_list is assigned just after
    # consum_usage is built (below).

    # ── Hall of Shame: avoidable damage (per-mechanic) + legend + friendly fire ──
    roster_idx = {p["name"]: p for p in players}
    def _srcs(d):
        return sorted([{"ability": a, "dmg": v} for a, v in (d or {}).items()],
                      key=lambda x: -x["dmg"])
    def _avsrc(p):
        hits = p.get("avoidable_hits") or {}
        return sorted(
            [{"ability": a, "dmg": v, "hits": hits.get(a, [])}
             for a, v in (p.get("avoidable_sources") or {}).items()],
            key=lambda x: -x["dmg"])
    avoidable_list = [
        {"name": p["name"], "role": p["role"], "class": p.get("class", ""),
         "dmg": p.get("avoidable_dmg", 0), "sources": _avsrc(p)}
        for p in sorted(players, key=lambda x: -x.get("avoidable_dmg", 0))
        if p.get("avoidable_dmg", 0) > 0
    ]
    # mechanic → {boss, icon} for the legend (icon: real WCL slug, with overrides)
    _icons     = wcl.get("ability_icons", {})
    _intr_wcl  = wcl.get("interrupts_wcl", {})   # WCL interrupt headline; log is the fallback
    _mech_boss = wcl.get("avoidable_mech_boss", {})
    _mechs_seen = {a for p in players for a in (p.get("avoidable_sources") or {})}
    avoidable_mechanics = {
        m: {"boss": _mech_boss.get(m, ""),
            "icon": ICON_OVERRIDES.get(m) or _icons.get(m, "")}
        for m in _mechs_seen
    }
    # friendly fire (source side) — join with roster for class/role coloring
    friendly_fire = [
        {"name": f["name"],
         "role":  roster_idx.get(f["name"], {}).get("role", ""),
         "class": roster_idx.get(f["name"], {}).get("class", ""),
         "dmg": f.get("dmg", 0), "incidents": f.get("incidents", 0),
         "category": "mechanic", "cats": f.get("cats", {}),
         "hits": f.get("hits", []),
         "mechanics": f.get("mechanics", {}),
         "bosses": f.get("bosses", []),
         "victims": f.get("victims", []),
         "sources": _srcs(f.get("mechanics"))}
        for f in wcl.get("friendly_fire", [])
        if f.get("dmg", 0) >= FF_MIN_DMG
    ]

    # MC accountability — Saves (CC'd a controlled ally) and Liabilities (AoE'd one).
    def _mc_row(f):
        return {**f,
                "role":  roster_idx.get(f["name"], {}).get("role", ""),
                "class": roster_idx.get(f["name"], {}).get("class", "")}
    mc_saves  = [_mc_row(f) for f in wcl.get("mc_saves", [])]
    mc_liable = [_mc_row(f) for f in wcl.get("mc_liable", [])]

    # Consumable AUDIT — the full raid-buff spread. Persistent buffs (flask/elixirs/food/
    # scrolls) come from the pull-time COMBATANT_INFO snapshot (catches pre-applied); active
    # items (pots/runes/healthstones) come from cast counts. One row per raider.
    ci_use = wcl.get("ci_consumables", {})
    cu_use = wcl.get("consum_use", {})
    lbl_use = wcl.get("consum_label", {})
    consum_names = set(ci_use) | set(cu_use)
    consum_usage = []
    for n in consum_names:
        c = ci_use.get(n, {})
        u = cu_use.get(n, {})
        lb = lbl_use.get(n, {})
        flask = c.get("flask", "")
        elixirs = c.get("elixirs", [])
        food = bool(c.get("food"))
        p_info        = roster_idx.get(n, {})
        role          = p_info.get("role", "")
        spec          = p_info.get("spec", "")
        fights_tanked = p_info.get("fights_tanked", 0)
        fights_healed = p_info.get("fights_healed", 0)
        fights_total  = p_info.get("fights_total", 0)
        # Hybrid/spec-swap: a tank who mostly DPS'd (or a healer who mostly DPS'd) is
        # evaluated against their off-role's threshold, not their roster slot's.
        effective_role = _effective_role(role, spec, fights_tanked, fights_healed, fights_total)
        consum_usage.append({
            "name": n,
            "role":  role,
            "class": p_info.get("class", p_info.get("type", "")),
            "flask": flask, "elixirs": elixirs, "food": food,
            "scrolls": c.get("scrolls", []), "weapon_oil": bool(c.get("weapon_oil")),
            "potion": u.get("potion", 0), "rune": u.get("rune", 0),
            "mana_gem": u.get("mana_gem", 0),         # mage Mana Emerald/Ruby on-use (Replenish Mana)
            "healthstone": u.get("healthstone", 0),
            "stones_made": u.get("stones_made", 0),   # warlock raid provision (utility): Create/Ritual of Souls
            "fear_ward":   u.get("fear_ward", 0),      # holy/disc priest anti-fear utility (cast count)
            "blessing":    u.get("blessing", 0),       # paladin Greater Blessing casts (raid buff provision)
            "judge_util":  u.get("judge_util", 0),     # paladin JoW/JoL upkeep (mana/healing to the raid)
            # specific item names for the compliance grid (None if unused)
            "combat_pots": lb.get("combat_pots", []),   # all combat pots popped (Haste/Destruction/Free Action…)
            "rune_name": lb.get("rune"),
            "flamecap": bool(u.get("flamecap")),
            "nightmare_seed": bool(u.get("nightmare_seed")),
            # prepared = has a flask (or 2 elixirs) AND food — the BiS baseline
            "prepared": bool(flask or len(elixirs) >= 2) and food,
            # fight-distribution context for compliance edge cases
            "effective_role": effective_role,
            "fights_tanked": fights_tanked,
            "fights_healed": fights_healed,
            "fights_total": fights_total,
        })
    # least-prepared first (missing flask/food bubbles up — that's the accountability angle)
    consum_usage.sort(key=lambda x: (x["prepared"], bool(x["flask"] or x["elixirs"]), x["food"],
                                     x["name"]))
    consum_list = build_consumable_compliance(consum_usage)

    # Healer scorecard — actual healers only (role=Healer), ranked by effective HPS.
    # Throughput + overheal% (efficiency) + activity% differentiate them; crit luck doesn't.
    heal_metrics = wcl.get("healing_metrics", {})
    heal_spells  = wcl.get("healing_spells", {})
    heal_mana    = wcl.get("healer_mana", {})
    heal_war     = wcl.get("healer_war", {})
    # Driven by who actually HEALED (>=1 heal fight), not by primary role — so a
    # spec-swapper like a heal-early/DPS-late player still shows, scoped to heal fights.
    healing = sorted(
        ({"name": nm,
          "role":  roster_idx.get(nm, {}).get("role", "Healer"),
          "class": roster_idx.get(nm, {}).get("class", ""),
          "eff_hps": m.get("eff_hps", 0), "eff_heal": m.get("eff_heal", 0),
          "overheal_pct": m.get("overheal_pct", 0), "activity_pct": m.get("activity_pct", 0),
          "tank_pct": m.get("tank_pct", 0), "top_spell": m.get("top_spell", ""),
          "fights_healed": m.get("fights_healed", 0),
          "fights_total":  roster_idx.get(nm, {}).get("fights_total", 0),
          "mana_eff": round(m.get("eff_heal", 0) / heal_mana[nm], 1) if heal_mana.get(nm) else 0,
          "vs_replacement": heal_war.get(nm),   # WCL hps parse % (0–100); None ⇒ not ranked yet
          "spells": heal_spells.get(nm, [])[:8]}
         for nm, m in heal_metrics.items() if m.get("eff_heal", 0) > 0),
        key=lambda x: -x["eff_hps"])

    # Tank scorecard — scoped to each player's TANK fights (prot/ret-swap aware), ranked
    # by damage taken/sec. Lower DTPS = better avoidance/mitigation for the same content.
    tank_metrics = wcl.get("tank_metrics", {})
    tank_scorecard = sorted(
        ({"name": nm, "role": "Tank",
          "class": roster_idx.get(nm, {}).get("class", ""),
          "dtps": tm.get("dtps", 0), "taken": tm.get("taken", 0),
          "hps_recv": tm.get("hps_recv", 0),
          "fights_tanked": tm.get("fights_tanked", 0),
          "fights_total":  roster_idx.get(nm, {}).get("fights_total", 0),
          "deaths": roster_idx.get(nm, {}).get("deaths", 0),
          # v2 — survivability depth (all WCL-durable except lowest_hp, which is log enrichment)
          "phys_pct": tm.get("phys_pct", 0), "magic_pct": tm.get("magic_pct", 0),
          "crush_count": tm.get("crush_count", 0), "crit_count": tm.get("crit_count", 0),
          "avoid_pct": tm.get("avoid_pct", 0),
          "biggest_hit": tm.get("biggest_hit"),
          "cooldowns": tm.get("cooldowns", {}),
          # tank Execution inputs (log-only): active-mitigation cast-rate / Lacerate uptime per
          # boss-melee-taken time (the normalization validated in discovery).
          "mitig_casts":    tm.get("mitig_casts", 0),
          "boss_melee_sec": tm.get("boss_melee_sec", 0),
          "lacerate_pct":   tm.get("lacerate_pct", 0),
          "per_boss": tm.get("per_boss", []),
          "lowest_hp": tm.get("lowest_hp", {}),
          # Performance pillar (two parts): threat = the tank's WCL dps PARSE % (WCL ranks tanks by
          # dps); survivability = an ABSOLUTE grade from the mitigation signals above (parse % can't
          # measure mitigation). vs_replacement holds the parse % (0–100); None/0 ⇒ not ranked yet.
          "vs_replacement": _tank_war.get(nm),
          "survival": _tank_survival_grade(tm, roster_idx.get(nm, {}).get("deaths", 0),
                                           roster_idx.get(nm, {}).get("class", ""))}
         for nm, tm in tank_metrics.items()),
        key=lambda x: -x["dtps"])

    meta = wcl.get("meta", {})

    return {
        "meta": {
            "date":        meta.get("date", ""),
            "start_ms":    meta.get("start_ms"),
            "zone":        meta.get("zone", ""),
            "kills":       meta.get("kills", 0),
            "report_code": meta.get("report_code", ""),
            "log_missing": meta.get("log_missing", []),
        },
        # name → identity, so the UI can color names/bars by WoW class
        "roster": {
            p["name"]: {"class": p.get("class", ""), "spec": p.get("spec", ""), "role": p.get("role", "")}
            for p in players
        },
        "consumables":  consum_list,
        "drums":        wcl.get("drums_log", []),
        "avoidableDmg":        avoidable_list,
        "avoidableMechanics":  avoidable_mechanics,
        "friendlyFire":        friendly_fire,
        "mcSaves":             mc_saves,
        "mcLiable":            mc_liable,
        "consumableUsage":     consum_usage,
        # Healthstone accountability (trended): raid-wide stones used + how many of the
        # raiders who died never popped one. {total_used, died_total, died_no_stone}.
        "healthstoneStats":    (lambda hs, dd: {
            "total_used":    sum(hs.values()),
            "died_total":    len(dd),
            "died_no_stone": sum(1 for p in dd if hs.get(p["name"], 0) == 0),
        })({p["name"]: p.get("healthstone", 0) for p in consum_usage},
           [p for p in death_list if p.get("total", 0) > 0]),
        "healing":             healing,
        "tankScorecard":       tank_scorecard,
        "roleSpells":          wcl.get("role_spells", {}),
        "playerSpells":        _ice_player_spells(wcl.get("player_spells", {}), _icons),
        # DPS report = actual damage-dealers only (effective_role Physical/Caster) — tanks and
        # healers who happened to deal damage are filtered out (they have their own surfaces).
        # A hybrid who mostly DPS'd (e.g. prot pally who twisted) stays via effective_role.
        # Expanded past the old top-10 so utility-DPS classes (Balance, etc.) appear with their
        # Class Toolkit cell; the DPS table has a scroll-y cap so length is handled.
        "damage": sorted(
            ({"name": p["name"], "role": p["role"], "effective_role": _eff(p),
              "total_dmg": p.get("total_dmg", 0),
              "active_pct": p.get("active_pct", 0),
              "toolkit": _toolkit_cell(p),
              "uptime_by_fight": p.get("uptime_by_fight", {})}
             for p in players
             if p.get("total_dmg", 0) > 0 and _eff(p) in ("Physical", "Caster")),
            key=lambda x: -x["total_dmg"])[:25],
        # Full roster of damage-dealers (NOT sliced to top 10) — powers the Uptime-by-Fight
        # heatmap so every attacker's per-boss uptime shows, while `damage` above stays a
        # top-10 DPS leaderboard. Same shape as `damage` minus active_pct. effective_role
        # lets the heatmap filter to who actually DPS'd (e.g. a prot pally who twisted).
        "uptimeByFight": sorted(
            ({"name": p["name"], "role": p["role"], "effective_role": _eff(p),
              "total_dmg": p.get("total_dmg", 0),
              "uptime_by_fight": p.get("uptime_by_fight", {})}
             for p in players if p.get("total_dmg", 0) > 0),
            key=lambda x: -x["total_dmg"]),
        # Healer casting-uptime per fight — the Healers & Tanks tab analog of uptimeByFight.
        # Filtered by effective_role so a resto player who mostly DPS'd drops out of the
        # healer chart (and into the DPS one).
        "healerUptimeByFight": sorted(
            ({"name": p["name"], "role": p["role"], "effective_role": _eff(p),
              "uptime_by_fight": p.get("healer_uptime_by_fight", [])}
             for p in players if _eff(p) == "Healer"),
            key=lambda x: x["name"]),
        "deaths":       death_list,
        "casterCrit":   crit_list("Caster"),
        "physicalCrit": crit_list("Physical"),
        "tankCrit":     crit_list("Tank"),
        "healerCrit":   crit_list("Healer"),
        "luckKPI":      luck_tiles,
        "engineering":  sorted(
            [{"name": p["name"], "role": p["role"], "eng": p.get("eng", {}),
              "dmg": p.get("eng_dmg", 0)}   # real sapper/bomb damage from the log
             for p in players if p.get("eng")],
            key=lambda x: -x["dmg"]
        ),
        # Interrupts — WCL headline (events) with the combat log as a silent fallback. WCL gives
        # the count AND which enemy casts were stopped (extraAbilityGameID); the log only fills in
        # when WCL has no row for a player (log present, WCL thin) — so a missing log thins, never
        # blanks, the KPI (WCL-Durability Principle).
        "interrupts":   sorted(
            filter(None, (_interrupt_row(p, _intr_wcl, _icons) for p in players)),
            key=lambda x: -x["count"]
        ),
        "boss_times":   wcl.get("boss_times", {}),
        "boss_meta":    wcl.get("boss_meta", {}),
        "healReaction": wcl.get("heal_reaction", {}),
        "debuffCoverage": wcl.get("debuff_coverage", {}),
        "gearAudit":      wcl.get("gear_audit", {}),
        "sunderArmor":    wcl.get("sunder_armor", {}),
        # "saving others" — protective/external casts on allies, ranked by saves then total.
        # Dispels are filtered off here (they own the dispels card below); this is protective
        # saves + reactive utility only. Positive call-out surface; empty list ⇒ a quiet week.
        "saves": (lambda sv: sorted(
            ({"name": nm, "role": roster_idx.get(nm, {}).get("role", ""),
              "class": roster_idx.get(nm, {}).get("class", ""),
              "save": d.get("save", 0),
              "utility": d.get("utility", 0), "total": d.get("total", 0),
              "targets": d.get("targets", {})}
             for nm, d in sv.items() if d.get("total", 0) > 0),
            key=lambda x: (-x["save"], -x["total"], x["name"])))(wcl.get("saves", {}) or {}),
        # who-dispelled-what — cleanses off allies + offensive purges on enemies (WCL Dispels events).
        # `removed` = the auras stripped (with live icons), the "what"; targets = cleanse recipients.
        # Ranked by total then cleanses. Empty list ⇒ no dispels this week (not a bug).
        "dispels": (lambda dp: sorted(
            ({"name": nm, "role": roster_idx.get(nm, {}).get("role", ""),
              "class": roster_idx.get(nm, {}).get("class", ""),
              "cleanse": d.get("cleanse", 0), "purge": d.get("purge", 0),
              "total": d.get("total", 0),
              "removed": sorted(({"aura": a, "n": n, "icon": _icons.get(a, "")}
                                 for a, n in (d.get("removed") or {}).items()),
                                key=lambda x: -x["n"]),
              "targets": d.get("targets", {})}
             for nm, d in dp.items() if d.get("total", 0) > 0),
            key=lambda x: (-x["total"], -x["cleanse"], x["name"])))(wcl.get("dispels", {}) or {}),
        "manaReturns":    wcl.get("mana_returns", {}),
        "loot":           wcl.get("loot_data", {}),   # this-week loot, external ThatsBIS CSV (degrades to {})
        # DPS table data split All/Bosses/Trash — each with its own WCL-style denominator.
        # DPS-only (effective_role Physical/Caster); toolkit cell carried per player.
        "damageBySelection": (lambda ds: {
            "durations": ds.get("durations", {}),
            "players": [
                {"name": p["name"], "role": p["role"], "effective_role": _eff(p),
                 "toolkit": _toolkit_cell(p),
                 # Performance pillar: the player's WCL dps PARSE % (rankPercent, 0–100 vs all logged
                 # parses). None ⇒ report not ranked yet; the cell renders "—".
                 "vs_replacement": _dps_war.get(p["name"]),
                 "all":   ds.get("players", {}).get(p["name"], {}).get("all",   {"total": 0, "active": 0}),
                 "boss":  ds.get("players", {}).get(p["name"], {}).get("boss",  {"total": 0, "active": 0}),
                 "trash": ds.get("players", {}).get(p["name"], {}).get("trash", {"total": 0, "active": 0})}
                for p in players
                if _eff(p) in ("Physical", "Caster")
                and ds.get("players", {}).get(p["name"], {}).get("all", {}).get("total", 0) > 0
            ],
        })(wcl.get("damage_by_sel", {}) or {}),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Inject WEEK_DATA into HTML dashboard
# ══════════════════════════════════════════════════════════════════════════════

def enrich_with_trends(week_data: dict, db_path) -> dict:
    """Attach week-over-week delta_* fields to the per-player items of a MAPPED week_data
    dict, by reading the PREVIOUS week's values from the history DB.

    Must run AFTER build_week_data()/map_to_week_data() and BEFORE write_week() — otherwise
    this week's row overwrites last week's before we can read it. Never throws: any DB error
    (missing file, missing table on an older DB, no prior week) just yields no deltas, and the
    HTML degrades silently (the fmtDelta() helper renders nothing for undefined values).

    db_path: the SAME DB the run will write to (DB_PATH or DB_PATH_TEST), so --test-db reads
    and writes the test DB consistently.
    """
    import sqlite3
    try:
        path = Path(db_path)
        if not path.exists():
            return week_data                      # first ever run — nothing to compare to
        con = sqlite3.connect(path)
        try:
            current = (week_data.get("meta") or {}).get("report_code")
            # Prior week = the most recent week STRICTLY BEFORE this one by CHRONOLOGICAL
            # start_ms (epoch ms). Must be `start_ms < this week's start_ms`, NOT merely "any
            # other report" — build_site.py reuses this to enrich EARLIER weeks for the rolling
            # multi-week view, where "most recent other report" would be a FUTURE week and trend
            # each past week against the latest (e.g. June 1 vs June 8). Each week trends against
            # its OWN predecessor. (start_ms is the canonical sort key; the date string misorders.)
            cur_ms = (week_data.get("meta") or {}).get("start_ms")
            if cur_ms is None:
                r0 = con.execute("SELECT start_ms FROM weeks WHERE report_code=?", (current,)).fetchone()
                cur_ms = r0[0] if r0 else None
            if cur_ms is None:
                # No chronological anchor — fall back to the global-latest other report.
                row = con.execute(
                    "SELECT report_code, date, kills, start_ms FROM weeks WHERE report_code != ? "
                    "ORDER BY start_ms DESC, date DESC LIMIT 1",
                    (current,)
                ).fetchone()
            else:
                # Prefer an anchored prior (start_ms < this week). A legacy row with NULL
                # start_ms (predates the migration) would be silently excluded by a bare
                # `start_ms < ?` — so include it as a LOWER-priority fallback so we trend
                # against it rather than returning nothing. `ORDER BY (start_ms IS NULL)`
                # keeps anchored priors first; the NULL row is chosen only if none exist.
                row = con.execute(
                    "SELECT report_code, date, kills, start_ms FROM weeks "
                    "WHERE report_code != ? AND (start_ms < ? OR start_ms IS NULL) "
                    "ORDER BY (start_ms IS NULL), start_ms DESC LIMIT 1",
                    (current, cur_ms)
                ).fetchone()
            if not row:
                return week_data                  # only one week of history
            prev, prev_date, prev_kills, prev_ms = row[0], row[1], row[2], row[3]
            if prev_ms is None:
                print(f"  ⚠ trends: prior week {prev} has no start_ms (predates migration) — "
                      f"baseline ordering is best-effort; run `reprocess.py --all` to anchor it")

            # Stale-prior-week guard: a delta reads the prior week's row, so if that row predates a
            # schema bump (missing new columns) the delta SILENTLY blanks. Warn loudly instead — the
            # fix is `reprocess.py --all`, which re-stamps every week to the current schema_version.
            try:
                import db_writer as _dbw
                _sv = con.execute("SELECT schema_version FROM weeks WHERE report_code=?", (prev,)).fetchone()
                _sv = _sv[0] if _sv else None
                if _sv is None or _sv < _dbw.SCHEMA_VERSION:
                    print(f"  ⚠ trends: prior week {prev} predates schema v{_dbw.SCHEMA_VERSION} "
                          f"(row is v{_sv}) — some delta_* may be blank; run `reprocess.py --all`")
            except sqlite3.OperationalError:
                pass   # older DB without the schema_version column

            def pv(table, player, col):
                """Previous-week value, or None if absent (also tolerates a missing
                table/column on an older DB that predates this metric)."""
                try:
                    r = con.execute(
                        f"SELECT {col} FROM {table} WHERE report_code=? AND player=?",
                        (prev, player)
                    ).fetchone()
                    return r[0] if r and r[0] is not None else None
                except sqlite3.OperationalError:
                    return None

            # ── damage: trend BOSS DPS (boss damage / boss time), matching the live DPS
            #    table's WCL-style denominator. Compare against the `dps` table's dps (also
            #    boss DPS now). Delta lands on each damageBySelection player (shown in the
            #    Bosses view + the header pill).
            dsel = week_data.get("damageBySelection") or {}
            durs = dsel.get("durations") or {}
            # delta DPS / uptime / total for EACH selection (All/Bosses/Trash), comparing the
            # matching prior-week columns (boss = base dps/total/uptime; all_*/trash_* added).
            SEL_COLS = {"boss":  ("dps", "total", "uptime"),
                        "all":   ("all_dps", "all_total", "all_uptime"),
                        "trash": ("trash_dps", "trash_total", "trash_uptime")}
            for it in (dsel.get("players") or []):
                nm = it.get("name")
                for sel, (c_dps, c_total, c_up) in SEL_COLS.items():
                    d = durs.get(sel) or 0
                    sd = it.get(sel)
                    if not d or sd is None:
                        continue
                    pd, pu, pt = pv("dps", nm, c_dps), pv("dps", nm, c_up), pv("dps", nm, c_total)
                    if pd is not None:
                        sd["delta_dps"] = round(sd.get("total", 0) / d - pd, 1)
                    if pu is not None:
                        sd["delta_uptime"] = round(sd.get("active", 0) / 1000 / d * 100 - pu, 1)
                    if pt is not None:
                        sd["delta_total"] = round(sd.get("total", 0) - pt, 1)

            # ── DPS parse %: delta the percentile vs last week (top-level on each dsel player).
            for it in (dsel.get("players") or []):
                pw = pv("dps", it.get("name"), "war")
                if pw is not None and it.get("vs_replacement"):
                    it["delta_vs_replacement"] = round(it["vs_replacement"] - pw, 2)

            # ── class toolkit: delta the signature metric, but ONLY when the metric KIND
            #    (label) matches last week — comparing Windfury casts to ToW uptime is nonsense.
            for it in (dsel.get("players") or []):
                tk = it.get("toolkit")
                if not tk or tk.get("num") is None:
                    continue
                try:
                    r = con.execute("SELECT label, num FROM class_toolkit WHERE report_code=? AND player=?",
                                    (prev, it.get("name"))).fetchone()
                except sqlite3.OperationalError:
                    r = None
                if r and r[0] == tk.get("label") and r[1] is not None:
                    tk["delta"] = round(tk["num"] - r[1], 1)

            # ── mana returns: delta per provider, per source (matched on player+source).
            for b in (week_data.get("manaReturns") or {}).get("batteries", []):
                for prov in b.get("providers", []):
                    try:
                        r = con.execute(
                            "SELECT mana FROM mana_returns WHERE report_code=? AND player=? AND source=?",
                            (prev, prov["name"], b.get("label"))).fetchone()
                    except sqlite3.OperationalError:
                        r = None
                    if r and r[0] is not None:
                        prov["delta_mana"] = prov.get("mana", 0) - r[0]

            # ── sunder armor: delta total + effective stacks built, per warrior.
            for p in (week_data.get("sunderArmor") or {}).get("players", []):
                try:
                    r = con.execute("SELECT total, effective FROM sunder_armor WHERE report_code=? AND player=?",
                                    (prev, p.get("name"))).fetchone()
                except sqlite3.OperationalError:
                    r = None
                if r:
                    if r[0] is not None:
                        p["delta_total"] = p.get("total", 0) - r[0]
                    if r[1] is not None:
                        p["delta_effective"] = p.get("effective", 0) - r[1]

            # ── healthstones: raid-level deltas (total used + # who died never popping).
            hs = week_data.get("healthstoneStats")
            if hs:
                try:
                    r = con.execute("SELECT hs_used, hs_died_no_stone FROM weeks WHERE report_code=?",
                                    (prev,)).fetchone()
                except sqlite3.OperationalError:
                    r = None
                if r:
                    if r[0] is not None and hs.get("total_used") is not None:
                        hs["delta_used"] = hs["total_used"] - r[0]
                    if r[1] is not None and hs.get("died_no_stone") is not None:
                        hs["delta_no_stone"] = hs["died_no_stone"] - r[1]

            # ── healing: four trended columns (HPS, overheal, activity, mana-efficiency).
            for it in (week_data.get("healing") or []):
                nm = it.get("name")
                for cur_field, db_col, delta_field in (
                    ("eff_hps",      "eff_hps",      "delta_hps"),
                    ("overheal_pct", "overheal_pct", "delta_overheal"),
                    ("activity_pct", "activity_pct", "delta_activity"),
                    ("mana_eff",     "mana_eff",     "delta_hp_per_mana"),
                ):
                    p = pv("healing", nm, db_col)
                    if p is not None:
                        it[delta_field] = round((it.get(cur_field) or 0) - p, 1)

            # ── single-field KPIs: (week key, table, current field, db col, delta field)
            #    (luck is intentionally NOT trended: the `luck_kpi.luck` column is NULL by
            #    design — luck is derived at render time from the multi-week crit baseline,
            #    not persisted — so a week-over-week DB delta is meaningless.)
            for wk_key, table, cur_field, db_col, delta_field in (
                ("avoidableDmg",  "avoidable_dmg",  "dmg",   "dmg",   "delta_dmg"),
                ("deaths",        "deaths",         "total", "total", "delta_deaths"),
                ("drums",         "drums",          "score", "score", "delta_drums"),
                ("saves",         "saves",          "save",  "save",  "delta_save"),
                ("dispels",       "dispels",        "total", "total", "delta_total"),
                # tank DTPS (lower better → HTML renders ▼ green via fmtDelta(...,false)).
                # Absent until 2 weeks of the new tank_scorecard table exist (no backfill).
                ("tankScorecard", "tank_scorecard", "dtps",  "dtps",  "delta_dtps"),
            ):
                for it in (week_data.get(wk_key) or []):
                    p = pv(table, it.get("name"), db_col)
                    if p is not None:
                        it[delta_field] = round((it.get(cur_field) or 0) - p, 1)

            # ── tank threat parse % + survivability grade — dedicated (survival is nested under
            #    .score, and the parse-% delta wants its own pass).
            for it in (week_data.get("tankScorecard") or []):
                nm = it.get("name")
                pw = pv("tank_scorecard", nm, "war")
                if pw is not None and it.get("vs_replacement"):
                    it["delta_vs_replacement"] = round(it["vs_replacement"] - pw, 2)
                ps, sv = pv("tank_scorecard", nm, "survival"), it.get("survival")
                if ps is not None and isinstance(sv, dict) and sv.get("score") is not None:
                    sv["delta_score"] = round(sv["score"] - ps, 1)

            # ── prior-week header context (powers the "vs last week" header pills).
            #    Kill-time delta is summed over the bosses cleared in BOTH weeks so it's
            #    fight-count agnostic — comparing raw totals would unfairly reward a week
            #    that simply downed fewer bosses. Negative delta = faster this week.
            prev_bt = {r[0]: r[1] for r in con.execute(
                "SELECT boss, seconds FROM boss_times WHERE report_code=?", (prev,)).fetchall()}
            cur_bt  = week_data.get("boss_times") or {}
            common  = [b for b in cur_bt if b in prev_bt]
            # Per-boss kill-time delta for the Overview tiles (negative = faster this week).
            # Folded in here — same start_ms-ordered prior week as the header pill — rather
            # than a separate ORDER BY date pass (which misorders; see memory).
            for b, meta in (week_data.get("boss_meta") or {}).items():
                if b in prev_bt and meta.get("seconds") is not None:
                    meta["delta_seconds"] = round(meta["seconds"] - prev_bt[b], 1)
            prev_ctx = {"report_code": prev, "date": prev_date, "kills": prev_kills}
            if common:
                prev_ctx["delta_kill_secs"] = round(
                    sum(cur_bt[b] for b in common) - sum(prev_bt[b] for b in common), 1)
                prev_ctx["common_bosses"] = len(common)
            week_data["prev"] = prev_ctx
        finally:
            con.close()
    except Exception as e:
        print(f"  [trends] warning: {e}")
    return week_data


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


def inject_into_html(week_data: dict, html_path: Path, mapped: WeekData | None = None):
    """Read template.html, inject WEEK_DATA, write to html_path (the gitignored output).

    Always reads from TEMPLATE_FILE so the output is never the source for the next run.
    Substitutes {{DASHBOARD_TITLE}} from the DASHBOARD_TITLE env var (default fallback).
    mapped: an already-mapped (and possibly trend-enriched) WEEK_DATA dict. When given,
    it is injected verbatim instead of re-mapping `week_data` — this preserves delta_*
    fields added by enrich_with_trends(). Omit it and the old behavior is unchanged."""
    src = TEMPLATE_FILE if TEMPLATE_FILE.exists() else html_path
    html = src.read_text(encoding="utf-8")

    # Substitute the dashboard title placeholder
    title = os.environ.get("DASHBOARD_TITLE") or DEFAULT_TITLE
    html = html.replace("{{DASHBOARD_TITLE}}", title)

    if mapped is None:
        mapped = map_to_week_data(week_data)
    new_json = json.dumps(mapped, indent=2, ensure_ascii=False)

    # Find the start of const WEEK_DATA = {
    marker = "const WEEK_DATA ="
    start  = html.find(marker)
    if start == -1:
        print("  ERROR: could not find 'const WEEK_DATA =' in HTML — skipping injection")
        return

    # Walk forward from { counting brackets to find the matching }
    brace_start = html.index("{", start)
    depth, i = 0, brace_start
    in_str, escape = False, False
    while i < len(html):
        ch = html[i]
        if escape:
            escape = False
        elif ch == "\\" and in_str:
            escape = True
        elif ch == '"' and not escape:
            in_str = not in_str
        elif not in_str:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    break
        i += 1

    # Replace from marker through the closing }; (plus optional semicolon)
    end = i + 1
    if html[end:end+1] == ";":
        end += 1

    updated = html[:start] + f"const WEEK_DATA = {new_json};" + html[end:]
    html_path.write_text(updated, encoding="utf-8")
    print(f"\n✅ Dashboard updated: {html_path}")









def merge_log_into_wcl(wcl_data: dict, log_data: dict) -> dict:
    """Overlay combat log stats onto WCL API data.

    The WCL API only populates combatantinfo crit fields for DPS roles, so tanks
    and healers come back with gear_crit_rating=0. The local combat log's
    COMBATANT_INFO carries per-school crit for everyone, so we use it to backfill
    any player whose WCL gear crit is still 0 — without disturbing the DPS values
    WCL already got right.
    """
    log_players = log_data.get("players", {})
    for p in wcl_data.get("players", []):
        log = log_players.get(p["name"], {})
        if not log:
            continue

        # Pick the role-appropriate crit school from the log — same logic as the
        # WCL combatantinfo path (spell for casters/healers, melee otherwise).
        role = p.get("role", "")
        if role in ("Caster", "Healer"):
            log_gear_crit = log.get("_crit_spell", 0)
        elif role in ("Physical", "Tank"):
            log_gear_crit = log.get("_crit_melee", 0)
        else:
            log_gear_crit = max(log.get("_crit_melee", 0), log.get("_crit_spell", 0))

        # Backfill expected crit only where WCL didn't supply gear crit (gap-fill).
        if log_gear_crit > 0 and p.get("gear_crit_rating", 0) == 0:
            p["gear_crit_rating"] = log_gear_crit
            p["expected_crit"]    = expected_crit(
                p.get("class", ""), p.get("spec", ""), log_gear_crit,
                log.get("_agility", 0), p.get("intellect", 0))
            p["luck_delta"]       = round(
                p.get("actual_crit", 0) - p["expected_crit"], 2)

        # Other log-sourced fields (log is authoritative for these).
        # NB: deaths come from the WCL table now (feign-free) — do NOT overwrite from the log.
        p["interrupt_count"] = log.get("interrupt_count", 0)
        p["interrupt_list"]  = log.get("interrupt_list", [])
        p["eng"]             = log.get("eng", {})
        p["eng_dmg"]         = log.get("eng_dmg", 0)
        p["avoidable_dmg"]     = log.get("avoidable_dmg", 0)
        p["avoidable_sources"] = log.get("avoidable_sources", {})
        p["avoidable_hits"]    = log.get("avoidable_hits", {})

    # Overwrite drums with log data (more reliable than WCL API for this)
    if log_data.get("drums"):
        wcl_data["drums_log"] = log_data["drums"]

    # Hall-of-Shame extras from the log (top-level, not per-player)
    wcl_data["friendly_fire"]       = log_data.get("friendly_fire", [])
    wcl_data["avoidable_mech_boss"] = log_data.get("avoidable_mech_boss", {})
    wcl_data["log_consumables"]     = log_data.get("consumables", {})
    wcl_data["mc_saves"]            = log_data.get("mc_saves", [])
    wcl_data["mc_liable"]           = log_data.get("mc_liable", [])
    wcl_data["consum_use"]          = log_data.get("consum_use", {})
    wcl_data["consum_label"]        = log_data.get("consum_label", {})

    # Melee auto-attack swings → into the spell-usage breakdown. The WCL Casts table omits
    # auto-attacks, so melee classes were missing their single biggest "action". Combat-log
    # only (degrades silently without a log). Inject a "Melee" row per player + per role.
    swings = log_data.get("melee_swings", {})
    if swings:
        role_of = {p["name"]: p.get("role", "") for p in wcl_data.get("players", [])}
        # force the conventional white-melee icon (the WCL layer maps "Melee" to a specific
        # weapon icon, which reads oddly as a generic auto-attack row)
        wcl_data.setdefault("ability_icons", {})["Melee"] = "ability_meleedamage"
        pspells = wcl_data.get("player_spells", {})
        rspells = wcl_data.get("role_spells", {})
        role_melee = defaultdict(lambda: {"casts": 0, "players": set()})
        for nm, sw in swings.items():
            if sw <= 0:
                continue
            role = role_of.get(nm)
            if role not in ("Physical", "Tank", "Caster", "Healer"):
                continue
            plist = pspells.setdefault(role, [])
            entry = next((p for p in plist if p["name"] == nm), None)
            if entry is None:
                entry = {"name": nm, "role": role, "total": 0, "abilities": []}
                plist.append(entry)
            abils = [a for a in entry.get("abilities", []) if a.get("ability") != "Melee"]
            abils.append({"ability": "Melee", "casts": sw})
            entry["abilities"] = sorted(abils, key=lambda x: -x["casts"])[:20]
            entry["total"] = entry.get("total", 0) + sw
            role_melee[role]["casts"]  += sw
            role_melee[role]["players"].add(nm)
        for role, mm in role_melee.items():
            rlist = [a for a in rspells.get(role, []) if a.get("ability") != "Melee"]
            rlist.append({"ability": "Melee", "casts": mm["casts"], "players": len(mm["players"])})
            rspells[role] = sorted(rlist, key=lambda x: -x["casts"])[:12]
        for role in pspells:
            pspells[role].sort(key=lambda x: -x.get("total", 0))
        wcl_data["player_spells"] = pspells
        wcl_data["role_spells"]   = rspells

    return wcl_data


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
    # Auto-find most recent .txt in logs/ if not specified
    _auto_log = None
    _log_files = sorted(LOGS_DIR.glob("WoWCombatLog*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
    if _log_files:
        _auto_log = str(_log_files[0])
    parser.add_argument("--log", default=_auto_log,
                        help="Path to WoWCombatLog.txt (default: newest WoWCombatLog*.txt in logs/)")
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
        args.report_code, token, log_path=args.log, report=rep0,
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
        print("\nRun next time with:")
        print(f"  python wcl_auto_dashboard.py {args.report_code} --out \"{args.out}\"")


if __name__ == "__main__":
    main()
