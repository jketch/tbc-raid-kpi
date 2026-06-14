"""week_map.py — the pure transform layer: merged wcl dict → WEEK_DATA.

map_to_week_data() turns the raw `wcl` dict (build_week_data + merge_log_into_wcl output)
into the WEEK_DATA shape the HTML renders — no API calls, no I/O, so reprocess.py
--from-raw can re-run it offline on cached raw dicts. merge_log_into_wcl() overlays the
combat-log results onto the wcl dict first (it reaches the crit model, which is why the
two live together). Extracted verbatim from wcl_auto_dashboard, which re-exports both.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:                       # resolved only by a type checker — no runtime import / cycle
    from week_schema import WeekData

from collections import defaultdict

from roles import _effective_role
from crit_model import expected_crit
from game_constants import ICON_OVERRIDES, FF_MIN_DMG
from wcl_fetchers import _toolkit_metric, _tank_survival_grade

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
        # TBC allows exactly one Battle + one Guardian elixir at a time (a flask blocks both), so any
        # TWO distinct elixir auras present at the pull ARE a full battle+guardian pair = both slots
        # filled = flask-equivalent. This game-rule test is robust to Anniversary buff renames (e.g.
        # the Mageblood guardian buff logs as "Greater Versatility"); the old hardcoded guardian-name
        # allowlist silently FAILED real 2-elixir healers/hybrids whenever a rename wasn't in the list.
        flask_ok = bool(e.get("flask")) or len(elixirs) >= 2
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
            "scrolls": e.get("scrolls", []),           # stat scrolls (informational + small prep bonus)
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
        for m in sorted(_mechs_seen)   # sorted: set iteration is hash-seed-dependent
    }

    # ── Mechanic Compliance — per-boss "who ate it" (pure WCL, verified ability-ID map) ──
    # The WCL-durable twin of the log-based avoidable view (game_constants.MECHANIC_IDS).
    _mech_wcl = wcl.get("mech_compliance", {})
    mech_compliance = {"bosses": [
        {"boss": boss, "encounter_id": (bv or {}).get("encounter_id"),
         "mechanics": [
             {"name": mech, "icon": ICON_OVERRIDES.get(mech) or _icons.get(mech, ""),
              "total": mv["total"], "events": mv["events"], "players_hit": len(mv["players"]),
              "players": sorted(({"name": n, "hits": pv["hits"], "dmg": pv["dmg"]}
                                 for n, pv in mv["players"].items()),
                                key=lambda x: (-x["dmg"], x["name"]))}
             for mech, mv in sorted(bv["mechanics"].items(), key=lambda kv: -kv[1]["total"])
         ]}
        for boss, bv in _mech_wcl.items()    # insertion order = kill order (deterministic)
    ]} if _mech_wcl else {}

    # WCL-Durability fallback (#7): no combat log ⇒ no per-player avoidable overlay. Rebuild the
    # avoidable view from mechanic compliance (the SAME curated mechanics, ID-keyed) so a missing
    # log THINS the card (no per-hit timestamps) but never blanks it. Log present ⇒ untouched.
    if not avoidable_list and _mech_wcl:
        _by_player = {}
        for boss, bv in _mech_wcl.items():
            for mech, mv in bv["mechanics"].items():
                for n, pv in mv["players"].items():
                    d = _by_player.setdefault(n, {})
                    d[mech] = d.get(mech, 0) + pv["dmg"]
        avoidable_list = [
            {"name": n, "role": roster_idx.get(n, {}).get("role", ""),
             "class": roster_idx.get(n, {}).get("class", ""),
             "dmg": sum(d.values()),
             "sources": sorted(({"ability": a, "dmg": v, "hits": []} for a, v in d.items()),
                               key=lambda x: -x["dmg"])}
            for n, d in sorted(_by_player.items(), key=lambda kv: (-sum(kv[1].values()), kv[0]))
        ]
        _wcl_mech_boss = {mech: boss for boss, bv in _mech_wcl.items() for mech in bv["mechanics"]}
        avoidable_mechanics = {
            m: {"boss": _wcl_mech_boss.get(m, ""),
                "icon": ICON_OVERRIDES.get(m) or _icons.get(m, "")}
            for m in sorted(_wcl_mech_boss)
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
    consum_names = sorted(set(ci_use) | set(cu_use))   # sorted: deterministic output order
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

    # A HUNTER's weapon enhancer is the ranged SCOPE (a permanent enchant) — GEAR readiness, not a
    # weapon-oil consumable (oils only go on the melee weapon a hunter never fights with). So the oil
    # slot is N/A for hunters in the compliance grid (template), and the scope is folded into Gear
    # Readiness here: a scope-less hunter gets a 'Ranged' missing-enchant, and the melee 'Main Hand'
    # flag is dropped (their melee weapon isn't load-bearing). Uses the COMBATANT_INFO ranged_scope.
    gear_audit = wcl.get("gear_audit", {})
    if gear_audit.get("players"):
        new_players = []
        for ga in gear_audit["players"]:
            if roster_idx.get(ga.get("name"), {}).get("class") == "Hunter":
                ga = dict(ga)   # copy — non-mutating / idempotent
                miss = [s for s in (ga.get("missing_enchants") or []) if s != "Main Hand"]
                if not ci_use.get(ga.get("name"), {}).get("ranged_scope"):
                    miss.append("Ranged (scope)")
                ga["missing_enchants"] = sorted(miss)
                if ga.get("ench_total") is not None:
                    ga["ench_ok"] = ga["ench_total"] - len(miss)
            new_players.append(ga)
        gear_audit = {**gear_audit, "players": new_players}

    # Group-buff GEAR provided (JC necks: Eye of the Night +SP / Chain of the Twilight Owl +crit) —
    # real raid utility, credited positive-only in the Raider Score Utility pillar. Sorted for
    # deterministic output. buffs = [{item, label}] per provider.
    group_buff_gear = []
    for n, items in sorted((wcl.get("group_buffs") or {}).items()):
        if not items:
            continue
        pinfo = roster_idx.get(n, {})
        group_buff_gear.append({
            "name": n, "role": pinfo.get("role", ""), "class": pinfo.get("class", pinfo.get("type", "")),
            "buffs": [{"item": it, "label": lb} for it, lb in sorted(items.items())],
        })

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
        # per-boss per-mechanic "who ate it" grid (pure WCL, verified ability-ID map)
        "mechanicCompliance":  mech_compliance,
        "friendlyFire":        friendly_fire,
        "mcSaves":             mc_saves,
        "mcLiable":            mc_liable,
        "consumableUsage":     consum_usage,
        "groupBuffGear":       group_buff_gear,
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
        # Inclusion matches the healer SCORECARD (anyone who healed ≥1 fight as a healer, via
        # healing_metrics), not effective_role — a spec-swapper who healed part of the night
        # showed on the scorecard but vanished from this heatmap (2026-06-13 review #10).
        "healerUptimeByFight": sorted(
            ({"name": p["name"], "role": p["role"], "effective_role": _eff(p),
              "uptime_by_fight": p.get("healer_uptime_by_fight", [])}
             for p in players
             if _eff(p) == "Healer" or p["name"] in heal_metrics),
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
        "gearAudit":      gear_audit,
        "sunderArmor":    wcl.get("sunder_armor", {}),
        # per-rogue Expose Armor uptime — the rogue's share of the armor-debuff slot (Sunder's twin)
        "exposeArmor":    wcl.get("expose_armor", {}),
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
