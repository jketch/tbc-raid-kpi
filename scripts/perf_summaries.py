"""Officer-only AI performance one-liners — optional, fully guarded.

Generates ONE constructive 1–2 sentence summary per raider from the week's HARD numbers, in a
SINGLE batched Claude call, and writes them to `WEEK_DATA.perfSummaries = {name: text}`. The
officer Performance drilldown renders the line above the 5-pillar `why` grid; the numbers stay the
source of truth, the prose is just a readable lead-in.

DESIGN GUARANTEES
  • Strictly grounded — the model is handed ONLY the extracted facts (parse %, deaths, avoidable
    rank, prep, a class-appropriate utility signal) and told never to invent or infer beyond them.
    Numbers it cites must come from the bundle. A structured JSON-array output keeps it on rails.
  • Fully guarded — no ANTHROPIC_API_KEY, no `anthropic` SDK, a network error, or malformed output
    all return `{}`; the caller simply leaves `perfSummaries` unset and the dashboard renders exactly
    as before. This module NEVER raises into the pipeline.
  • Cheap & infrequent — ~25 short summaries once a week. Default model is Haiku 4.5 (override with
    PERF_SUMMARY_MODEL). Runs only on a real prod run (main, not --test-db / not reprocess), so it
    never spends on proofs or offline replays.

Tone follows CLAUDE.md: clean, principal-level, raiders who know the game. Positive call-outs are
fine; NO naming-and-shaming, no moralizing. Frame a weak number as a concrete, fixable observation.
"""
from __future__ import annotations

import os
import json

# Cap how many summaries we ask for in one call (the whole 25-man roster fits comfortably; this is
# just a sanity backstop so a malformed week can't balloon the request).
MAX_RAIDERS = 40
DEFAULT_MODEL = "claude-haiku-4-5"


def _rank_map(items, key, *, reverse=True):
    """name → 1-based rank over `items` by numeric `key` (1 = best). Ties share order; Nones dropped."""
    vals = [(it.get("name"), it.get(key)) for it in (items or [])
            if isinstance(it, dict) and isinstance(it.get(key), (int, float))]
    vals.sort(key=lambda t: t[1], reverse=reverse)
    return {name: i + 1 for i, (name, _) in enumerate(vals)}


def _facts_for_week(mapped: dict) -> list:
    """Extract a compact, grounded fact bundle per raider from the mapped WEEK_DATA.

    Everything here is a value already on the page — we only RESHAPE and add raid-relative RANKS
    (so the model can say 'top healer' without us asking it to rank, which it would do unreliably).
    """
    roster = mapped.get("roster") or {}
    if not roster:
        return []

    dps_players = ((mapped.get("damageBySelection") or {}).get("players")) or []
    healing     = mapped.get("healing") or []
    tanks       = mapped.get("tankScorecard") or []
    deaths      = {d.get("name"): d for d in (mapped.get("deaths") or []) if isinstance(d, dict)}
    avoid       = {a.get("name"): a for a in (mapped.get("avoidableDmg") or []) if isinstance(a, dict)}
    ff          = {f.get("name"): f for f in (mapped.get("friendlyFire") or []) if isinstance(f, dict)}
    mcl         = {m.get("name"): m for m in (mapped.get("mcLiable") or []) if isinstance(m, dict)}
    prep        = {c.get("name"): c for c in (mapped.get("consumables") or []) if isinstance(c, dict)}
    usage       = {u.get("name"): u for u in (mapped.get("consumableUsage") or []) if isinstance(u, dict)}
    interrupts  = {i.get("name"): i.get("count") for i in (mapped.get("interrupts") or []) if isinstance(i, dict)}
    saves       = {s.get("name"): s for s in (mapped.get("saves") or []) if isinstance(s, dict)}
    sunder      = {s.get("name"): s for s in ((mapped.get("sunderArmor") or {}).get("players") or [])
                   if isinstance(s, dict)}

    dps_by_name = {p.get("name"): p for p in dps_players if isinstance(p, dict)}
    heal_by_name = {h.get("name"): h for h in healing if isinstance(h, dict)}
    tank_by_name = {t.get("name"): t for t in tanks if isinstance(t, dict)}

    # raid-relative ranks (1 = best). DPS over the All-selection total; healing over eff_hps.
    dps_total = lambda p: ((p.get("all") or {}).get("total"))
    dps_rank = _rank_map([{"name": p.get("name"), "_t": dps_total(p)} for p in dps_players], "_t")
    heal_rank = _rank_map(healing, "eff_hps")
    avoid_rank = _rank_map(list(avoid.values()), "dmg")   # 1 = MOST avoidable (worst)

    out = []
    for name, info in roster.items():
        cls = (info or {}).get("class", "")
        role = (info or {}).get("role", "")
        spec = (info or {}).get("spec", "")
        f = {"name": name, "class": cls, "spec": spec, "role": role}

        # Performance — the role-appropriate parse % (the colored WCL number; 50 typical, 95+ elite).
        if name in tank_by_name:
            t = tank_by_name[name]
            f["tank_dtps"] = round(t.get("dtps") or 0)
            if t.get("vs_replacement") is not None:
                f["threat_parse_pct"] = t["vs_replacement"]
            if isinstance(t.get("survival"), dict) and t["survival"].get("score") is not None:
                f["survival_grade"] = t["survival"]["score"]
        if name in dps_by_name:
            p = dps_by_name[name]
            if p.get("vs_replacement") is not None:
                f["dps_parse_pct"] = p["vs_replacement"]
            if dps_total(p):
                f["dps_total"] = round(dps_total(p))
            if name in dps_rank:
                f["dps_rank"] = dps_rank[name]
            tk = p.get("toolkit") or {}
            if tk.get("label") and tk.get("value"):
                f["toolkit"] = f"{tk['label']}: {tk['value']}"
        if name in heal_by_name:
            h = heal_by_name[name]
            f["eff_hps"] = round(h.get("eff_hps") or 0)
            if h.get("vs_replacement") is not None:
                f["hps_parse_pct"] = h["vs_replacement"]
            if h.get("overheal_pct") is not None:
                f["overheal_pct"] = round(h["overheal_pct"])
            if name in heal_rank:
                f["healer_rank"] = heal_rank[name]

        # Preparation — the 0–10 raid-prep score.
        if name in prep and prep[name].get("score") is not None:
            f["prep_score"] = prep[name]["score"]            # 0–10

        # Execution inputs — deaths, avoidable (with rank so the model knows clean vs careless),
        # friendly fire, MC liability. Absent ⇒ none, which is GOOD; we omit the key.
        d = deaths.get(name)
        if d and (d.get("total") or 0) > 0:
            f["deaths"] = d.get("total"); f["deaths_on_trash"] = d.get("trash") or 0
        a = avoid.get(name)
        if a and (a.get("dmg") or 0) > 0:
            f["avoidable_dmg"] = round(a["dmg"])
            if name in avoid_rank:
                f["avoidable_rank_worst"] = avoid_rank[name]   # 1 = took the MOST avoidable
        if ff.get(name) and (ff[name].get("dmg") or 0) > 0:
            f["friendly_fire_dmg"] = round(ff[name]["dmg"])
        if mcl.get(name) and (mcl[name].get("dmg") or 0) > 0:
            f["mc_liability_dmg"] = round(mcl[name]["dmg"])    # AoE'd into a charmed ally

        # Utility — a class-appropriate signal (mirrors the dashboard's class-shaped util pillar).
        u = usage.get(name) or {}
        if cls == "Warlock" and u.get("stones_made"):
            f["stones_made"] = u["stones_made"]
        if cls == "Priest" and role == "Healer" and u.get("fear_ward"):
            f["fear_ward_casts"] = u["fear_ward"]
        if cls == "Paladin":
            if u.get("blessing"): f["greater_blessings"] = u["blessing"]
            if u.get("judge_util"): f["judgement_upkeep"] = u["judge_util"]
        if interrupts.get(name):
            f["interrupts"] = interrupts[name]
        if name in sunder and (sunder[name].get("total") or 0) > 0:
            f["sunder_applications"] = sunder[name]["total"]
        if saves.get(name) and (saves[name].get("total") or 0) > 0:
            f["external_saves"] = saves[name]["total"]         # protective casts ON allies

        out.append(f)
        if len(out) >= MAX_RAIDERS:
            break
    return out


SYSTEM = (
    "You are a raid analyst writing private, officer-only one-line performance notes for a 25-man "
    "World of Warcraft (TBC) raid. The audience are experienced raiders and officers.\n\n"
    "RULES — follow exactly:\n"
    "1. Use ONLY the numbers in each raider's fact object. Never invent, estimate, or infer a number "
    "that is not present. If a fact is absent, do not mention it.\n"
    "2. One or two sentences, max ~30 words. Lead with what stands out (best OR most-fixable).\n"
    "3. Parse % is the WCL percentile (50 = typical, 75+ strong, 95+ elite). prep_score is out of 10. "
    "avoidable_rank_worst = 1 means they took the MOST avoidable damage in the raid. dps_rank/healer_rank "
    "= 1 is best.\n"
    "4. Tone: clean and principal-level. Positive call-outs are great. Frame a weak number as a "
    "concrete, fixable observation (e.g. 'consumables slipped to 6/10' or 'led the raid in avoidable "
    "damage'). NEVER moralize, shame, or use harsh language. No exclamation marks.\n"
    "5. Refer to the raider by name. Be specific to THEIR numbers — no generic filler.\n\n"
    "Return a JSON object: {\"summaries\": [{\"name\": <raider name>, \"summary\": <text>}, ...]} with "
    "one entry per raider, names matching the input exactly."
)

_OUT_SCHEMA = {
    "type": "object",
    "properties": {
        "summaries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "summary": {"type": "string"}},
                "required": ["name", "summary"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["summaries"],
    "additionalProperties": False,
}


def generate(mapped: dict, *, model: str | None = None, verbose: bool = True) -> dict:
    """Return {name: one-line summary} for the week, or {} on ANY failure. Never raises.

    Guarded end-to-end: missing key/SDK, the API call, and output parsing each fall through to {}.
    """
    try:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            if verbose:
                print("  perf summaries: ANTHROPIC_API_KEY not set — skipping (dashboard unaffected)")
            return {}

        facts = _facts_for_week(mapped)
        if not facts:
            return {}

        try:
            import anthropic
        except ImportError:
            # Self-provision on first real use (mirrors wcl_client's requests bootstrap). Only reached
            # once a key is set, so we never install for users who'll never call the API.
            try:
                import subprocess, sys as _sys
                subprocess.check_call([_sys.executable, "-m", "pip", "install", "anthropic", "--quiet"])
                import anthropic
            except Exception:
                if verbose:
                    print("  perf summaries: `anthropic` SDK unavailable (pip install anthropic) — skipping")
                return {}

        model = model or os.getenv("PERF_SUMMARY_MODEL") or DEFAULT_MODEL
        client = anthropic.Anthropic(api_key=api_key)
        user_payload = json.dumps(facts, ensure_ascii=False)

        resp = client.messages.create(
            model=model,
            max_tokens=4096,
            system=SYSTEM,
            messages=[{"role": "user", "content":
                       "Write a one-line note for each raider. Facts:\n" + user_payload}],
            output_config={"format": {"type": "json_schema", "schema": _OUT_SCHEMA}},
        )

        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        data = json.loads(text)
        valid = {info.get("name") for info in facts}
        summaries = {}
        for row in (data.get("summaries") or []):
            n, s = row.get("name"), (row.get("summary") or "").strip()
            if n in valid and s:
                summaries[n] = s
        if verbose:
            print(f"  perf summaries: generated {len(summaries)}/{len(facts)} via {model}")
        return summaries
    except Exception as e:
        if verbose:
            print(f"  perf summaries: skipped ({e.__class__.__name__}: {e}) — dashboard unaffected")
        return {}


def attach(mapped: dict, *, model: str | None = None, verbose: bool = True) -> dict:
    """Generate and attach `mapped['perfSummaries']` in place (only if non-empty). Returns `mapped`."""
    summaries = generate(mapped, model=model, verbose=verbose)
    if summaries:
        mapped["perfSummaries"] = summaries
    return mapped
