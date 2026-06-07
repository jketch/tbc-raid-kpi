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


def build_summary(wd: dict) -> str:
    meta = wd.get("meta", {})
    head = f"📊 **Raid KPI — {meta.get('date','?')}**  ·  {meta.get('zone','')}  ·  {meta.get('kills','?')}/10 kills"
    bits = []
    dmg = wd.get("damage", [])
    if dmg:
        bits.append(f"⚔️ Top dmg **{dmg[0]['name']}** {hsk(dmg[0].get('total_dmg'))}")
    deaths = sorted(wd.get("deaths", []), key=lambda p: -p.get("total", 0))
    if deaths:
        bits.append(f"💀 Most deaths **{deaths[0]['name']}** {deaths[0].get('total')}")
    av = sorted(wd.get("avoidableDmg", []), key=lambda p: -p.get("dmg", 0))
    if av:
        bits.append(f"💥 Most floor **{av[0]['name']}** {hsk(av[0].get('dmg'))}")
    return head + ("\n" + "  ·  ".join(bits) if bits else "")


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
        shutil.copyfile(DASH, DEPLOY / "index.html")   # root of the site = the dashboard
        r = subprocess.run(
            ["netlify", "deploy", "--prod", "--dir", str(DEPLOY), "--json"],
            cwd=ROOT, capture_output=True, text=True, timeout=240, shell=True)
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

    # ── Discord auto-post: DISABLED — guild has webhooks locked down. ────────────
    # To re-enable once you have a webhook: add DISCORD_WEBHOOK_URL to .env and
    # uncomment the three lines below.
    # webhook = load_env().get("DISCORD_WEBHOOK_URL") or os.getenv("DISCORD_WEBHOOK_URL")
    # if webhook:
    #     post_discord(webhook, summary + (f"\n\n👉 **This week's dashboard:** {url}" if url else ""))


if __name__ == "__main__":
    main()
