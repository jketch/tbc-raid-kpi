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
    """Pull the injected WEEK_DATA object out of the HTML (brace-matched)."""
    try:
        t = DASH.read_text(encoding="utf-8")
        m = t.find("const WEEK_DATA =")
        i = t.index("{", m); start = i; depth = 0; in_str = esc = False
        while i < len(t):
            c = t[i]
            if esc: esc = False
            elif c == "\\" and in_str: esc = True
            elif c == '"': in_str = not in_str
            elif not in_str:
                if c == "{": depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        return json.loads(t[start:i + 1])
            i += 1
    except Exception:
        pass
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
    dmg = wd.get("damage", [])
    if dmg:
        leaders.append(f"⚔️ Top dmg **{dmg[0]['name']}** {hsk(dmg[0].get('total_dmg'))}")
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


def deploy_netlify():
    """Deploy dashboard (as index.html) to the linked Netlify site. Returns live URL or None."""
    if not shutil.which("netlify"):
        print("  ⚠ netlify CLI not found — skipping deploy (run: npm install -g netlify-cli)")
        return None
    if not (ROOT / ".netlify").exists():
        print("  ⚠ no site linked yet — skipping deploy (run `netlify login` then `netlify init` once)")
        return None
    try:
        DEPLOY.mkdir(exist_ok=True)
        # Stage the rolling multi-week site (index.html + weeks/<report>.json + WEEKS_INDEX).
        # Falls back to a plain single-week copy if staging can't run (e.g. no snapshots yet).
        try:
            sys.path.insert(0, str(ROOT / "scripts"))
            import build_site
            build_site.build()
        except Exception as e:
            print(f"  ⚠ multi-week staging skipped ({e}); deploying single week")
            shutil.copyfile(DASH, DEPLOY / "index.html")
        netlify_exe = shutil.which("netlify")
        r = subprocess.run(
            [netlify_exe, "deploy", "--prod", "--dir", str(DEPLOY), "--json"],
            cwd=ROOT, capture_output=True, text=True, timeout=240)
        if r.returncode != 0:
            print(f"  ⚠ netlify deploy failed:\n{(r.stderr or r.stdout)[-500:]}")
            return None
        data = json.loads(r.stdout)
        return data.get("url") or data.get("deploy_url")
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
    url     = deploy_netlify()
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
