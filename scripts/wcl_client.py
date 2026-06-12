"""WarcraftLogs v2 transport — OAuth + the GraphQL POST wrapper.

A pure leaf: depends on nothing in the pipeline (only `requests` + stdlib). Everything that
talks to WCL goes through `gql()`; `wcl_auto_dashboard` re-exports these so existing `W.gql` /
`W.get_token` call sites (week_build, publish, reprocess, …) keep working unchanged.

OAuth host is fresh.warcraftlogs.com; ALL queries hit www.warcraftlogs.com/api/v2/client
(same API, different token issuer) — see CLAUDE.md.
"""
from __future__ import annotations

import time

# Auto-install requests if missing (mirrors the main module's bootstrap).
try:
    import requests as _req
except ImportError:
    import sys, subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests", "--quiet"])
    import requests as _req

WCL_TOKEN_URL = "https://fresh.warcraftlogs.com/oauth/token"
WCL_API_URL   = "https://www.warcraftlogs.com/api/v2/client"


def get_token(client_id: str, client_secret: str, retries: int = 5) -> str:
    # OAuth host (fresh.warcraftlogs.com) flaps during WCL incidents — retry on timeouts/5xx so a
    # transient blip doesn't kill the whole run before it starts (it had NO retry before).
    for attempt in range(retries):
        try:
            resp = _req.post(
                WCL_TOKEN_URL,
                data={"grant_type": "client_credentials"},
                auth=(client_id, client_secret),
                timeout=30
            )
            if resp.status_code in (500, 502, 503, 504):
                raise _req.exceptions.HTTPError(f"{resp.status_code} from OAuth host")
            if not resp.ok:
                print(f"  Auth failed [{resp.status_code}]: {resp.text}")
                resp.raise_for_status()
            return resp.json()["access_token"]
        except Exception as e:
            if attempt == retries - 1:
                raise
            wait = min(30, 3 * 2 ** attempt)
            print(f"  Auth retry {attempt+1}/{retries} after {e.__class__.__name__}; waiting {wait}s")
            time.sleep(wait)


def gql(token: str, query: str, variables: dict | None = None, retries: int = 5) -> dict:
    attempt = 0          # network/HTTP retry budget
    rate_waits = 0       # 429s use their OWN bounded counter so a slow point-budget
    MAX_RATE_WAITS = 6   # cooldown doesn't burn the network-retry budget
    while True:
        try:
            resp = _req.post(
                WCL_API_URL,
                json={"query": query, "variables": variables or {}},
                headers={"Authorization": f"Bearer {token}"},
                timeout=30
            )
            # Rate limited: WCL's 300-points/min ceiling can need a far longer cooldown than
            # the exponential backoff. Honor Retry-After and try again without spending the
            # network-retry budget (a bounded number of times).
            if resp.status_code == 429:
                rate_waits += 1
                if rate_waits > MAX_RATE_WAITS:
                    resp.raise_for_status()
                ra = resp.headers.get("Retry-After", "")
                wait = int(ra) if ra.isdigit() else 5 * rate_waits
                print(f"  Rate limited (429) — waiting {wait}s ({rate_waits}/{MAX_RATE_WAITS})")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            payload = data.get("data")
            if data.get("errors"):
                # A partial response still carries usable `data` for the aliases/fields that
                # succeeded — one bad fight-alias must not nuke a whole batch. Only a
                # null/missing payload is a hard failure. GraphQL-level errors are
                # deterministic, so they are NOT retried.
                if payload is not None:
                    print(f"  GraphQL partial errors (returning partial data): {data['errors']}")
                    return payload
                raise RuntimeError(f"GraphQL errors: {data['errors']}")
            return payload
        except RuntimeError:
            raise   # deterministic GraphQL error — retrying would just re-fail
        except Exception as e:
            attempt += 1
            if attempt >= retries:
                raise
            wait = min(30, 2 ** attempt)   # 2,4,8,16,30 — patient enough to ride out a WCL flap
            print(f"  Retry {attempt}/{retries} after error ({e.__class__.__name__}); waiting {wait}s")
            time.sleep(wait)
