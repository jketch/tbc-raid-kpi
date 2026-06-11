#!/usr/bin/env python
"""Publish the weekly dashboard: deploy the HTML to Netlify, post the link to Discord.

Run at the end of run_weekly.bat. SAFE to run before setup is finished — every step
warns and skips if it isn't configured yet, so it never breaks the weekly run.

────────────────────────────────────────────────────────────────────────────────
ONE-TIME SETUP — already done. Recorded here in case the link is ever lost:
  1. (already installed)  npm install -g netlify-cli
  2. netlify login                       # opens a browser — click Authorize
  3. netlify sites:create                # creates + auto-links a new site; pick your
                                         #   team (Marvels) and a site name. This writes
                                         #   .netlify/state.json, which deploy_netlify()
                                         #   checks for. (We use sites:create, NOT
                                         #   `netlify init` — init is for git-based CI we
                                         #   don't use; this script deploys --dir manually.)
Live site:  https://clinquant-taffy-c345c2.netlify.app
Admin:      https://app.netlify.com/projects/clinquant-taffy-c345c2
After that, every run_weekly.bat auto-deploys the latest dashboard to that URL.

Discord auto-post is DISABLED (guild has webhooks locked). The script prints a
copy/paste blurb instead. To re-enable later: add DISCORD_WEBHOOK_URL to .env and
uncomment the three lines at the bottom of main().
────────────────────────────────────────────────────────────────────────────────
Only stdlib used (urllib/json/subprocess) — no extra pip installs required.
"""
import os, sys, json, shutil, subprocess
from pathlib import Path

# Windows cp1252 console can't encode the ✓/⚠/emoji glyphs we print — force UTF-8.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT      = Path(__file__).resolve().parent.parent
DASH      = ROOT / "dashboard" / "raid_kpi_dashboard.html"
DEPLOY    = ROOT / ".deploy"            # netlify serves index.html at the site root
LAST_DEPLOY = ROOT / "cache" / "last_deploy.json"   # persisted baseline for the section-loss guard


def _load_last_deploy():
    """The WEEK_DATA of the most recent successful deploy. Persisted (not read from the
    ephemeral .deploy/) so the re-deploy section-loss guard survives a clean checkout / CI run
    where .deploy/ is empty — exactly when the loot-drop incident would otherwise slip through."""
    try:
        if LAST_DEPLOY.exists():
            return json.loads(LAST_DEPLOY.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def _save_last_deploy(wd):
    try:
        LAST_DEPLOY.parent.mkdir(parents=True, exist_ok=True)
        LAST_DEPLOY.write_text(json.dumps(wd, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"  · could not persist last-deploy baseline: {e}")


def load_env() -> dict:
    env, f = {}, ROOT / ".env"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def extract_week_data() -> dict:
    """Pull the injected WEEK_DATA out of the dashboard HTML via the ONE shared string-aware
    loader (week_build.extract_week_data) — was a private brace-walker copy here (#5)."""
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        import week_build as wb
        return wb.extract_week_data(DASH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def hsk(n) -> str:
    n = n or 0
    return f"{n/1e6:.2f}M" if n >= 1e6 else f"{round(n/1e3)}K"


def _killtime(wd: dict) -> str:
    secs = round(sum((wd.get("boss_times") or {}).values()))
    return f"{secs // 60}:{secs % 60:02d}"


def build_summary(wd: dict) -> str:
    """Celebratory recap that matches the dashboard's tone.

    Positive individual call-outs (top dmg/healer) are fine — they're recognition.
    The accountability angle is RAID-WIDE aggregates (total avoidable, interrupt
    breadth, drum coverage), never naming-and-shaming a single raider.
    """
    meta = wd.get("meta", {})
    head = (f"📊 **Raid KPI — {meta.get('date','?')}**  ·  {meta.get('zone','')}"
            f"  ·  {meta.get('kills','?')}/10 kills  ·  ⏱ {_killtime(wd)} on bosses")

    # ── Shout-outs — positive individual recognition ──
    leaders = []
    # Rank "Top dmg" off damageBySelection (the SAME source the dashboard's DPS table uses —
    # All-selection total, already filtered to DPS by effective_role) so the posted blurb names
    # the same #1 and shows the same number as the site (#4). The legacy wd['damage'] list used a
    # different denominator/scope and could disagree. Fall back to it only for pre-damageBySelection
    # snapshots.
    dbs_players = (wd.get("damageBySelection") or {}).get("players") or []
    ranked = sorted((p for p in dbs_players if (p.get("all") or {}).get("total")),
                    key=lambda p: p["all"]["total"], reverse=True)
    if ranked:
        top = ranked[0]
        leaders.append(f"⚔️ Top dmg **{top['name']}** {hsk(top['all']['total'])}")
    elif wd.get("damage"):
        d0 = wd["damage"][0]
        leaders.append(f"⚔️ Top dmg **{d0['name']}** {hsk(d0.get('total_dmg'))}")
    heal = wd.get("healing", [])
    if heal:
        leaders.append(f"💚 Top healer **{heal[0]['name']}** {round(heal[0].get('eff_hps') or 0):,} HPS")

    # ── Raid-wide stats — cohort accountability, no individual names ──
    stats = []
    av_total = sum(p.get("dmg", 0) for p in wd.get("avoidableDmg", []))
    if av_total:
        stats.append(f"💥 {hsk(av_total)} raid avoidable")
    breadth = sum(1 for i in wd.get("interrupts", []) if i.get("count", 0) > 0)
    if breadth:
        stats.append(f"🎯 {breadth} raiders interrupted")
    bpds = [d.get("buffs_per_drum") or 0 for d in wd.get("drums", []) if (d.get("buffs_per_drum") or 0) > 0]
    if bpds:
        cov = min(100, round(sum(bpds) / len(bpds) / 4 * 100))   # avg allies buffed / drum ÷ 4 (matches dashboard)
        stats.append(f"🥁 {cov}% drum coverage")

    lines = [head]
    if leaders:
        lines.append("  ·  ".join(leaders))
    if stats:
        lines.append("  ·  ".join(stats))
    return "\n".join(lines)


def _section_loss_guard(prev_latest, new_latest):
    """Sections whose loss should BLOCK a deploy. Two cases, to avoid cross-week false positives
    (the 'latest' week legitimately changes each weekly deploy and may have different optional
    sections):
      • ALWAYS — a required WCL section blank on a killed week (the pipeline shipped a broken core).
      • RE-DEPLOY ONLY — when the new latest is the SAME report_code as the previous deploy's latest,
        ANY section that was present and is now empty (the loot re-deploy-drop incident).
    Returns a list of human-readable reasons (empty = safe to deploy)."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import week_schema as ws
    reasons = [w for w in ws.validate(new_latest, has_log=None, has_loot=None)["warn"]]
    pc = (prev_latest or {}).get("meta", {}).get("report_code")
    nc = (new_latest or {}).get("meta", {}).get("report_code")
    if prev_latest and pc and pc == nc:
        for sec in ws.regression(prev_latest, new_latest):
            reasons.append(f"{sec}: present in the previous deploy of this week, now empty")
        # field-coverage collapse INSIDE a still-populated section (e.g. WCL parse % blanked by a
        # reprocess from pre-ranking snapshots) — section-level regression() can't see this.
        for fld in ws.coverage_regression(prev_latest, new_latest):
            reasons.append(f"{fld}: every value blanked vs the previous deploy of this week "
                           f"(field-coverage collapse — likely a reprocess over fresh WCL data)")
    return reasons


def deploy_netlify(force: bool = False):
    """Deploy dashboard (as index.html) to the linked Netlify site. Returns live URL or None.
    force: skip the section-loss guard (deploy even if a section regressed to empty)."""
    if not shutil.which("netlify"):
        print("  ⚠ netlify CLI not found — skipping deploy (run: npm install -g netlify-cli)")
        return None
    if not (ROOT / ".netlify").exists():
        print("  ⚠ no site linked yet — skipping deploy (run `netlify login` then `netlify sites:create` once)")
        return None
    try:
        DEPLOY.mkdir(exist_ok=True)
        sys.path.insert(0, str(ROOT / "scripts"))
        import week_build as wb
        # Baseline for the section-loss guard: the last SUCCESSFUL deploy's WEEK_DATA, read from a
        # persisted cache so the guard works even on a clean checkout / CI (where .deploy/ is empty).
        # Fall back to the on-disk staged index if the cache hasn't been written yet.
        prev_latest = _load_last_deploy()
        if prev_latest is None:
            _prev_idx = DEPLOY / "index.html"
            if _prev_idx.exists():
                try:
                    prev_latest = wb.extract_week_data(_prev_idx.read_text(encoding="utf-8"))
                except Exception:
                    pass
        # Stage the rolling multi-week site (index.html + weeks/<report>.json + WEEKS_INDEX).
        # Falls back to a plain single-week copy if staging can't run (e.g. no snapshots yet).
        try:
            import build_site
            build_site.build()
        except Exception as e:
            print(f"  ⚠ multi-week staging skipped ({e}); deploying single week")
            # build_site clears weeks/ early; a mid-way failure can leave a PARTIAL weeks/ dir that
            # would deploy stale/missing past weeks. Remove it so only a clean single week ships.
            _wk = DEPLOY / "weeks"
            if _wk.exists():
                shutil.rmtree(_wk, ignore_errors=True)
            shutil.copyfile(DASH, DEPLOY / "index.html")
        # ── section-loss guard ── compare the newly-staged latest vs the previous deploy.
        # FAIL CLOSED: a staged index we can't parse (or with no WEEK_DATA) is the worst thing to
        # ship over a good live site, so block it unless forced — never silently deploy past it.
        try:
            new_latest = wb.extract_week_data((DEPLOY / "index.html").read_text(encoding="utf-8"))
        except Exception as e:
            new_latest = None
            print(f"  · could not parse staged WEEK_DATA ({e})")
        if not new_latest or not new_latest.get("meta"):
            print("\n  ╔══ DEPLOY BLOCKED — staged index has no readable WEEK_DATA (corrupt build?) ══╗")
            print("  ╚══════════════════════════════════════════════════════════════════════════════╝")
            if not force:
                print("  Re-run with --force to deploy anyway, or rebuild (likely `reprocess.py --all`).")
                return None
            losses = []
        else:
            losses = _section_loss_guard(prev_latest, new_latest)
            # Visible coverage line so a thin/blanked field is obvious BEFORE the push, even on a
            # first deploy (when the guard has no prior deploy to compare against).
            try:
                import week_schema as ws
                cov = ws.coverage(new_latest)
                print("  parse-% coverage: " + "  ·  ".join(f"{k} {v}" for k, v in cov.items()))
            except Exception:
                pass
        if losses and not force:
            print("\n  ╔══ DEPLOY BLOCKED — a dashboard section would be LOST ══╗")
            for r in losses:
                print(f"    ✗ {r}")
            print("  ╚════════════════════════════════════════════════════════╝")
            print("  Re-run with --force to deploy anyway, or fix the data (likely `reprocess.py --all`).")
            return None
        netlify_exe = shutil.which("netlify")
        try:
            r = subprocess.run(
                [netlify_exe, "deploy", "--prod", "--dir", str(DEPLOY), "--json"],
                cwd=ROOT, capture_output=True, text=True, timeout=240)
        except subprocess.TimeoutExpired:
            print("  ⚠ netlify deploy timed out after 240s — it MAY have landed server-side; "
                  "check app.netlify.com before re-deploying")
            return None
        if r.returncode != 0:
            print(f"  ⚠ netlify deploy failed:\n{(r.stderr or r.stdout)[-500:]}")
            return None
        data = json.loads(r.stdout)
        url = data.get("url") or data.get("deploy_url")
        if url and new_latest:
            _save_last_deploy(new_latest)   # baseline for the NEXT deploy's section-loss guard
        return url
    except Exception as e:
        print(f"  ⚠ netlify deploy error: {e}")
        return None


def post_discord(webhook: str, content: str):
    import urllib.request
    body = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(webhook, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=30)
        print("  ✓ posted to Discord")
    except Exception as e:
        print(f"  ⚠ Discord post failed: {e}")


def main():
    if not DASH.exists():
        print("  ⚠ dashboard not found — run the weekly script first"); return
    print("\n[publish] deploy …")
    force   = "--force" in sys.argv   # skip the section-loss guard
    url     = deploy_netlify(force=force)
    summary = build_summary(extract_week_data())

    # Print the link + summary so you can paste it into the raid channel BY HAND
    # (Discord auto-post is disabled below).
    if url:
        print(f"  ✓ live at: {url}")
    print("\n  ─── copy/paste into your raid channel ───")
    print("  " + summary.replace("**", "").replace("\n", "\n  "))
    if url:
        print(f"  👉 {url}")
    print("  ──────────────────────────────────────────")


if __name__ == "__main__":
    main()
