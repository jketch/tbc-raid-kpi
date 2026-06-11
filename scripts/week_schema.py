"""The WEEK_DATA contract — the single declarative definition of "a complete week".

Every top-level key that `map_to_week_data()` emits is registered here with its **source tier**
and a **populated?** predicate. This is the one place that knows what a finished week looks like, so
producers (main / backfill / reprocess), the finalize funnel, and the deploy guard all validate
against the SAME definition instead of drifting (the bug class that dropped `loot` and blanked trends).

Tiers encode the project's WCL-Durability Principle:
  • WCL      — sourced from WCL; MUST be present on any week with kills (empty ⇒ probable pipeline bug).
               `required=False` marks a WCL section that's legitimately empty when not applicable
               (no warriors → no sunders; a flawless week → no deaths; healer gear-crit is unavailable).
  • LOG      — combat-log only; empty is normal when no log was transferred. Never a hard warning.
  • EXTERNAL — the manual ThatsBIS loot CSV; empty on a dry night or when no CSV is present.
  • META     — structural (`meta`); never validated.

This module is a LEAF: it imports nothing from the pipeline, so anything may import it without cycles.
Tier-agnostic — these KPI sections are stable across content tiers (T5 SSC/TK → T6 Hyjal/BT); only the
*content within* a section (bosses, mechanics) changes, and that lives elsewhere.
"""

WCL      = "wcl"
LOG      = "log"
EXTERNAL = "external"
META     = "meta"


# ── populated? predicates for the sections whose "emptiness" is ambiguous ──────────────
# Most sections are a list or dict where `bool(v)` ([]/{} falsy) is the right test. These few
# are always-present containers whose meaningful emptiness lives one level down.
def _has_players(v):    return isinstance(v, dict) and bool(v.get("players"))
def _has_batteries(v):  return isinstance(v, dict) and (
    bool(v.get("batteries")) or bool((v.get("innervate") or {}).get("casters")))
def _hs_nonempty(v):    return isinstance(v, dict) and (
    (v.get("total_used") or 0) > 0 or (v.get("died_total") or 0) > 0)


# ── the registry: key → Section(tier, required, predicate, description) ─────────────────
# Order mirrors the map_to_week_data() return dict for easy cross-referencing.
class Section:
    __slots__ = ("tier", "required", "predicate", "desc")
    def __init__(self, tier, required=True, predicate=None, desc=""):
        self.tier, self.required, self.predicate, self.desc = tier, required, predicate, desc


SECTIONS = {
    "meta":                Section(META, False, desc="date / zone / kills / report_code"),
    "roster":              Section(WCL,  True,  desc="name → class/spec/role (sentinel: empty ⇒ nothing loaded)"),
    "consumables":         Section(WCL,  True,  desc="Raid Prep 0–10 (COMBATANT_INFO pull auras + log bonus)"),
    "consumableUsage":     Section(WCL,  True,  desc="per-player flask/food/elixir/oil/pot audit (pull auras)"),
    "drums":               Section(LOG,  desc="Drums of Battle buff counts"),
    "avoidableDmg":        Section(LOG,  desc="avoidable damage taken (combat-log spell-name whitelist)"),
    "avoidableMechanics":  Section(LOG,  desc="per-mechanic avoidable breakdown"),
    "friendlyFire":        Section(LOG,  desc="clumping splash (mostly Vashj Static Charge)"),
    "mcSaves":             Section(LOG,  desc="CC'd a charmed ally (deduped per MC episode)"),
    "mcLiable":            Section(LOG,  desc="AoE'd into a charmed ally"),
    "healthstoneStats":    Section(LOG,  predicate=_hs_nonempty,
                                   desc="stones used + died-without-stone (use=log, deaths=WCL)"),
    "healing":             Section(WCL,  True,  desc="healer scorecard (eff_hps / overheal / activity / WAR)"),
    "tankScorecard":       Section(WCL,  True,  desc="tank v2 — DTPS, phys/magic, crush/crit/avoid, CDs, per-boss"),
    "roleSpells":          Section(WCL,  True,  desc="role-based spell usage"),
    "playerSpells":        Section(WCL,  True,  desc="per-player ability tallies (iconized)"),
    "damage":              Section(WCL,  True,  desc="top-25 DPS leaderboard (legacy; publish summary reads it)"),
    "uptimeByFight":       Section(WCL,  True,  desc="every attacker's per-boss uptime (heatmap)"),
    "healerUptimeByFight": Section(WCL,  True,  desc="per-healer casting uptime by fight"),
    "deaths":              Section(WCL,  False, desc="deaths + recap (empty ⇒ a flawless week, not a bug)"),
    "casterCrit":          Section(WCL,  True,  desc="caster spell-crit (WCL-durable)"),
    "physicalCrit":        Section(WCL,  True,  desc="physical melee-crit (WCL-durable)"),
    "tankCrit":            Section(WCL,  True,  desc="tank melee-crit (WCL-durable)"),
    "healerCrit":          Section(WCL,  False, desc="healer crit (gear-crit unavailable; from heal events — may be empty)"),
    "luckKPI":             Section(WCL,  True,  desc="actual − expected crit (historical baseline)"),
    "engineering":         Section(LOG,  desc="sappers/bombs by ability (combat log)"),
    "interrupts":          Section(LOG,  desc="interrupt counts + spells stopped (combat log)"),
    "boss_times":          Section(WCL,  True,  desc="kill time per boss (seconds)"),
    "boss_meta":           Section(WCL,  True,  desc="boss tiles — portrait/delta/deaths/raid-DPS"),
    "healReaction":        Section(LOG,  desc="per-raider/boss reaction-time medians (HP%-timeline)"),
    "debuffCoverage":      Section(WCL,  False, desc="CoE/Misery/SW/ISB/FF/Sunder/Reck/Expose/judgements uptime (empty if none cast)"),
    "gearAudit":           Section(WCL,  False, predicate=_has_players,
                                   desc="per-raider item level / enchant / gem compliance (WCL gear + wowhead sockets)"),
    "sunderArmor":         Section(WCL,  False, predicate=_has_players,
                                   desc="per-warrior Sunder quality (empty: no warriors / no sunders)"),
    "manaReturns":         Section(WCL,  False, predicate=_has_batteries,
                                   desc="mana returned to raid by source (empty if comp returns none)"),
    "saves":               Section(WCL,  False,
                                   desc="protective/external casts on allies (paladin Hands, dispels, battle-res)"),
    "loot":                Section(EXTERNAL, predicate=_has_players,
                                   desc="this-week loot from the ThatsBIS CSV (empty: dry night / no CSV)"),
    "damageBySelection":   Section(WCL,  True,  predicate=_has_players,
                                   desc="DPS table All/Bosses/Trash (the live DPS data source)"),
    "perfSummaries":       Section(EXTERNAL, predicate=lambda v: isinstance(v, dict) and bool(v),
                                   desc="AI-written 1-liners per raider (officer drilldown; empty: no API key / generation off)"),
}

# Top-level keys that legitimately appear AFTER mapping (enrich / loot mutate in place) and must
# NOT be reported as "unknown" by validate().
POST_MAP_KEYS = {"prev"}


def is_populated(mapped: dict, key: str) -> bool:
    """Is section `key` meaningfully present in `mapped`? Uses the section's predicate, or truthiness."""
    spec = SECTIONS.get(key)
    val = (mapped or {}).get(key)
    if spec and spec.predicate:
        return bool(spec.predicate(val))
    return bool(val)


def populated_sections(mapped: dict) -> set:
    """The set of non-empty content sections in `mapped` (excludes `meta`)."""
    return {k for k, s in SECTIONS.items()
            if s.tier != META and is_populated(mapped, k)}


def validate(mapped: dict, *, has_log=None, has_loot=None) -> dict:
    """Check `mapped` against the contract. Returns {"warn": [...], "info": [...]} — NEVER raises.

    A WARN is a probable pipeline bug: a `required` WCL section empty on a week that has kills.
    Everything else is INFO: log-tier emptiness (normal without a log), external loot emptiness,
    optional WCL sections (no warriors, a flawless week), or a missing key. `has_log`/`has_loot`
    (True / False / None=unknown) only sharpen the INFO wording — they never upgrade INFO to WARN,
    so reprocess (which works from a snapshot and can't know the original sources) passes None and
    stays quiet. Use format_report() to render.
    """
    warn, info = [], []
    kills = ((mapped or {}).get("meta") or {}).get("kills", 0) or 0
    for key, s in SECTIONS.items():
        if s.tier == META:
            continue
        if key not in (mapped or {}):
            info.append(f"{key}: missing from week_data")
            continue
        if is_populated(mapped, key):
            continue
        if s.tier == WCL and s.required and kills > 0:
            warn.append(f"{key}: EMPTY — WCL-durable section blank on a {kills}-kill week (likely a pipeline drop)")
        elif s.tier == WCL:
            info.append(f"{key}: empty (WCL, not applicable this week)")
        elif s.tier == LOG:
            why = "no combat log" if has_log is False else "clean week or no log"
            info.append(f"{key}: empty ({why})")
        elif s.tier == EXTERNAL:
            why = "no awards / no CSV" if has_loot is False else "dry night or no CSV"
            info.append(f"{key}: empty ({why})")
    return {"warn": warn, "info": info}


def regression(old: dict, new: dict) -> list:
    """Sections that were populated in `old` but are empty in `new` — probable drops/regressions.

    This is the deploy-guard signal: a section the previous deploy had that the new one lost (the
    loot-drop symptom). Tier-agnostic — catches WCL, LOG, and EXTERNAL drops alike."""
    return sorted(populated_sections(old) - populated_sections(new))


# ── field-coverage (inside a populated section) ─────────────────────────────────────────
# A section can stay "populated" while a key field across its items collapses to all-null — e.g.
# WCL parse % (vs_replacement) blanked by a reprocess from pre-ranking snapshots: damageBySelection
# still has players, so section-level regression() can't see it. These entries track per-field
# coverage so the deploy guard can. key → (extractor(week_data) -> list[item dict], field name).
COVERAGE_FIELDS = {
    "dps parse %":    (lambda wd: ((wd.get("damageBySelection") or {}).get("players")) or [], "vs_replacement"),
    "tank parse %":   (lambda wd: wd.get("tankScorecard") or [], "vs_replacement"),
    "healer parse %": (lambda wd: wd.get("healing") or [], "vs_replacement"),
}


def coverage(mapped: dict) -> dict:
    """{label: count of items carrying a non-null value} for each COVERAGE_FIELDS entry."""
    out = {}
    for label, (extract, field) in COVERAGE_FIELDS.items():
        try:
            items = extract(mapped or {})
            out[label] = sum(1 for it in items if isinstance(it, dict) and it.get(field) is not None)
        except Exception:
            out[label] = 0
    return out


def coverage_regression(old: dict, new: dict) -> list:
    """Labels whose field-coverage COLLAPSED — present in `old`, entirely null in `new`.

    Targets the exact 'a populated section's key field went all-null' failure that section-level
    regression() can't see (WCL parse % blanked by a reprocess from pre-ranking snapshots). Only
    fires on a TOTAL collapse (old>0 → new==0), so ordinary per-player roster churn never trips it."""
    co, cn = coverage(old), coverage(new)
    return sorted(k for k in COVERAGE_FIELDS if co.get(k, 0) > 0 and cn.get(k, 0) == 0)


def format_report(report: dict, *, label: str = "week") -> str:
    """Render a validate() result as a compact, human-readable block (for finalize_week's stdout)."""
    lines = []
    for w in report.get("warn", []):
        lines.append(f"  ⚠ {w}")
    for i in report.get("info", []):
        lines.append(f"  · {i}")
    if not lines:
        return f"  ✓ {label}: all contract sections present"
    head = f"  contract check ({label}): {len(report.get('warn', []))} warning(s), {len(report.get('info', []))} note(s)"
    return "\n".join([head, *lines])
