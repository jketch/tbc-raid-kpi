"""WarcraftLogs v2 transport — OAuth + the GraphQL POST wrapper.

A pure leaf: depends on nothing in the pipeline (only `requests` + stdlib). Everything that
talks to WCL goes through `gql()`; `wcl_auto_dashboard` re-exports these so existing `W.gql` /
`W.get_token` call sites (week_build, publish, reprocess, …) keep working unchanged.

OAuth host is fresh.warcraftlogs.com; ALL queries hit www.warcraftlogs.com/api/v2/client
(same API, different token issuer) — see CLAUDE.md.
"""
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


def get_token(client_id: str, client_secret: str) -> str:
    resp = _req.post(
        WCL_TOKEN_URL,
        data={"grant_type": "client_credentials"},
        auth=(client_id, client_secret),
        timeout=15
    )
    if not resp.ok:
        print(f"  Auth failed [{resp.status_code}]: {resp.text}")
        resp.raise_for_status()
    return resp.json()["access_token"]


def gql(token: str, query: str, variables: dict = None, retries: int = 3) -> dict:
    for attempt in range(retries):
        try:
            resp = _req.post(
                WCL_API_URL,
                json={"query": query, "variables": variables or {}},
                headers={"Authorization": f"Bearer {token}"},
                timeout=30
            )
            resp.raise_for_status()
            data = resp.json()
            if "errors" in data:
                raise RuntimeError(f"GraphQL errors: {data['errors']}")
            return data["data"]
        except Exception as e:
            if attempt == retries - 1:
                raise
            print(f"  Retry {attempt+1}/{retries} after error: {e}")
            time.sleep(2 ** attempt)
