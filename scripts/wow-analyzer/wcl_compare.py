"""
wcl_compare.py — Warcraftlogs.com percentile fetcher

Two modes:
  1. API mode (preferred): Requires a WCL public API key (Client ID + Secret).
     Set env vars WCL_CLIENT_ID and WCL_CLIENT_SECRET.
     Docs: https://www.warcraftlogs.com/api/docs

  2. Web-scrape mode (fallback): Scrapes public character pages on warcraftlogs.com.
     Less reliable; may break if WCL changes their HTML.

Usage from analyze.py:
    from wcl_compare import fetch_wcl_percentiles
    data = fetch_wcl_percentiles("Playerame", "Area 52", "US", encounters)
    # Returns: {(boss_name, difficulty): {"percentile": 82, "ilvl": 639, "rank": 41200}}
"""

import os
import re
import json
import urllib.request
import urllib.parse
import urllib.error
from typing import Optional

# ── Encounter ID mapping — TBC Classic T5 ────────────────────────────────────
# Maps encounter name → WCL encounter ID
# NOTE: WCL uses their own internal IDs which differ from retail journal IDs.
# These are the IDs for TBC Classic on Anniversary realms.
# If a lookup returns no data, cross-reference at:
#   https://www.warcraftlogs.com/zone/statistics/<zone_id>
# SSC zone: 1007  |  The Eye (TK) zone: 1008
WCL_ENCOUNTER_IDS: dict[str, int] = {
    # ── Serpentshrine Cavern (SSC) ──
    "Hydross the Unstable":      623,
    "The Lurker Below":          624,
    "Leotheras the Blind":       625,
    "Fathom-Lord Karathress":    626,
    "Morogrim Tidewalker":       627,
    "Lady Vashj":                628,
    # ── The Eye (Tempest Keep) ──
    "Al'ar":                     730,
    "Void Reaver":               731,
    "High Astromancer Solarian": 732,
    "Kael'thas Sunstrider":      733,
}

DIFFICULTY_TO_WCL = {
    # TBC Classic
    "25-man": 4,
    "10-man": 3,
    # Retail (kept for compatibility)
    "Mythic":  5,
    "Heroic":  4,
    "Normal":  3,
    "LFR":     1,
}


# ──────────────────────────────────────────────────────────────────────────────
# API MODE
# ──────────────────────────────────────────────────────────────────────────────

def _wcl_oauth_token(client_id: str, client_secret: str) -> str:
    """Fetch an OAuth2 bearer token from WCL."""
    url = "https://www.warcraftlogs.com/oauth/token"
    creds = f"{client_id}:{client_secret}".encode()
    import base64
    headers = {
        "Authorization": "Basic " + base64.b64encode(creds).decode(),
        "Content-Type": "application/x-www-form-urlencoded",
    }
    body = b"grant_type=client_credentials"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())["access_token"]


def _wcl_graphql(token: str, query: str, variables: dict) -> dict:
    url = "https://www.warcraftlogs.com/api/v2/client"
    payload = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(url, data=payload, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


RANKINGS_QUERY = """
query CharacterRankings($name: String!, $serverSlug: String!, $serverRegion: String!, $encounterID: Int!, $difficulty: Int!) {
  characterData {
    character(name: $name, serverSlug: $serverSlug, serverRegion: $serverRegion) {
      name
      encounterRankings(
        encounterID: $encounterID
        difficulty: $difficulty
        metric: dps
        includeCombatantInfo: false
      )
    }
  }
}
"""

# Your realm slug for the WCL API call.
# Anniversary realm "Nightfall-US" → slug "nightfall"
DEFAULT_REALM_SLUG = "nightfall"
DEFAULT_REGION     = "US"


def fetch_via_api(char: str, realm: str, region: str, encounters) -> dict:
    client_id     = os.environ.get("WCL_CLIENT_ID", "")
    client_secret = os.environ.get("WCL_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        raise EnvironmentError("WCL_CLIENT_ID / WCL_CLIENT_SECRET not set")

    token = _wcl_oauth_token(client_id, client_secret)
    result = {}
    seen: set[tuple] = set()

    for enc in encounters:
        key = (enc.encounter_name, enc.difficulty)
        if key in seen:
            continue
        seen.add(key)

        enc_id = WCL_ENCOUNTER_IDS.get(enc.encounter_name)
        diff   = DIFFICULTY_TO_WCL.get(enc.difficulty, 4)
        if not enc_id:
            continue

        try:
            data = _wcl_graphql(token, RANKINGS_QUERY, {
                "name":          char,
                "serverSlug":    realm.lower().replace(" ", "-").replace("'", ""),
                "serverRegion":  region.upper(),
                "encounterID":   enc_id,
                "difficulty":    diff,
            })
            rankings = (data.get("data", {})
                            .get("characterData", {})
                            .get("character", {})
                            .get("encounterRankings", {}))
            if rankings and "ranks" in rankings and rankings["ranks"]:
                best = max(rankings["ranks"], key=lambda r: r.get("rankPercent", 0))
                result[key] = {
                    "percentile": round(best.get("rankPercent", 0)),
                    "ilvl":       best.get("gear", {}).get("averageItemLevel", "?"),
                    "rank":       best.get("rank", "?"),
                    "outOf":      best.get("outOf", "?"),
                }
        except Exception as ex:
            print(f"  WCL API error for {enc.encounter_name}: {ex}")

    return result


# ──────────────────────────────────────────────────────────────────────────────
# SCRAPE MODE (fallback)
# ──────────────────────────────────────────────────────────────────────────────

def fetch_via_scrape(char: str, realm: str, region: str, encounters) -> dict:
    """
    Scrape https://www.warcraftlogs.com/character/<region>/<realm>/<name>
    to extract best % rankings per boss.
    NOTE: This is fragile and may break on WCL HTML changes.
    """
    realm_slug = realm.lower().replace(" ", "-").replace("'", "")
    url = (f"https://www.warcraftlogs.com/character/"
           f"{region.lower()}/{realm_slug}/{char.lower()}")

    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (WoW Log Analyzer)",
        "Accept": "text/html",
    })
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} fetching WCL page for {char}") from e

    result = {}
    # WCL renders rankings in JS; look for JSON blobs in <script> tags
    # Pattern: {"name":"BossName",...,"rankPercent":82.5,...}
    # This is a best-effort extraction
    for match in re.finditer(r'"name"\s*:\s*"([^"]+)"[^}]*"rankPercent"\s*:\s*([\d.]+)', html):
        boss_name = match.group(1)
        pct = round(float(match.group(2)))
        for enc in encounters:
            if enc.encounter_name.lower() in boss_name.lower() or boss_name.lower() in enc.encounter_name.lower():
                key = (enc.encounter_name, enc.difficulty)
                if key not in result:
                    result[key] = {"percentile": pct}
    return result


# ──────────────────────────────────────────────────────────────────────────────
# PUBLIC ENTRY POINT
# ──────────────────────────────────────────────────────────────────────────────

def fetch_wcl_percentiles(char: str, realm: str, region: str, encounters) -> dict:
    """
    Try API mode first, fall back to scrape mode.
    Returns dict keyed by (encounter_name, difficulty) →
        {"percentile": int, "ilvl": int|str, "rank": int|str, "outOf": int|str}
    """
    try:
        data = fetch_via_api(char, realm, region, encounters)
        if data:
            print("  WCL data fetched via API.")
            return data
    except Exception as ex:
        print(f"  API mode unavailable ({ex}), trying scrape …")

    try:
        data = fetch_via_scrape(char, realm, region, encounters)
        if data:
            print("  WCL data fetched via web scrape.")
            return data
    except Exception as ex:
        print(f"  Scrape mode failed: {ex}")

    print("  No WCL comparison data available. "
          "Set WCL_CLIENT_ID / WCL_CLIENT_SECRET env vars to enable API mode.\n"
          "  Get free keys at: https://www.warcraftlogs.com/api/clients/")
    return {}
