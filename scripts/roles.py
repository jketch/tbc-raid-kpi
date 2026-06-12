"""roles.py — spec → role classification, a pure leaf (no I/O, no WCL).

Single source of truth for hybrid/spec-swap placement: build_week_data's reclassifier,
the per-fight role fetchers, and map_to_week_data all route through these. Extracted from
wcl_auto_dashboard, which re-exports every name so existing references resolve unchanged.
"""

# Spec → role classification, shared by build_week_data's reclassifier and per-fight roles.
TANK_SPECS     = {"Protection", "Feral Combat"}    # Feral only counts as tank if WCL agrees
HEALER_SPECS   = {"Holy", "Discipline", "Restoration"}
CASTER_SPECS   = {"Arcane", "Fire", "Frost", "Shadow", "Elemental",
                  "Demonology", "Affliction", "Destruction", "Balance"}
PHYSICAL_SPECS = {"Fury", "Arms", "Retribution", "Enhancement",
                  "Survival", "Marksmanship", "Beast Mastery",
                  "Combat", "Assassination", "Subtlety"}


def _nontank_role(spec: str) -> str:
    """Role to use for compliance when a Tank spec tanks < 50 % of fights (runs DPS consumes)."""
    if spec in HEALER_SPECS:  return "Healer"
    if spec in CASTER_SPECS:  return "Caster"
    if spec in PHYSICAL_SPECS: return "Physical"
    return "Physical"   # Feral Combat, Protection → physical melee when not tanking


def _nonheal_role(spec: str) -> str:
    """Role to use when a Healer spec heals < 50 % of fights (ran DPS most of the night).
    Resto shaman/druid → Caster by default; an Enhancement/Survival-style off-spec maps by
    its physical spec. Mirrors _nontank_role. Handles the resto-shaman-who-DPS'd case."""
    if spec in PHYSICAL_SPECS: return "Physical"
    if spec in CASTER_SPECS:   return "Caster"
    if spec in TANK_SPECS:     return "Physical"
    return "Caster"   # Restoration (shaman/druid), Holy/Disc → caster DPS when not healing


def _effective_role(role: str, spec: str, fights_tanked: int, fights_healed: int,
                    fights_total: int) -> str:
    """A player's role for the night by what they ACTUALLY did, not their roster slot.
    A Tank who tanked < 50% of fights, or a Healer who healed < 50%, ran DPS most of the
    night and is reclassified to their off-role. Everyone else keeps their roster role.
    Single source of truth for hybrid/spec-swap placement across all KPIs."""
    if fights_total > 0:
        if role == "Tank"   and fights_tanked / fights_total < 0.5: return _nontank_role(spec)
        if role == "Healer" and fights_healed / fights_total < 0.5: return _nonheal_role(spec)
    return role


def _fight_role(spec: str, bucket: str) -> str:
    """Per-fight role from the player's spec THAT fight — reliable for prot/ret and
    heal/dps swaps. WCL's tanks/healers/dps bucket is only a tiebreaker (it mis-buckets
    Prot warriors into dps), so we classify by spec and fall back to the bucket."""
    if spec in HEALER_SPECS:
        return "Healer"
    if spec == "Protection":
        return "Tank"
    if spec == "Feral Combat":
        return "Tank" if bucket == "tanks" else "dps"
    if not spec:
        return {"tanks": "Tank", "healers": "Healer", "dps": "dps"}[bucket]
    return "dps"   # all caster/physical DPS specs
