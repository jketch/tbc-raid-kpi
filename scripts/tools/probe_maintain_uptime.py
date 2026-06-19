import os, sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import wcl_auto_dashboard as W   # noqa: E402 — triggers wcl_client .env autoload
from paths import WEEK_DATA_CACHE

code = sys.argv[1]
tok = W.get_token(os.environ["WCL_CLIENT_ID"], os.environ["WCL_CLIENT_SECRET"])
rep = W.gql(tok, W.Q_REPORT, {"code": code})["reportData"]["report"]
kills = [f for f in rep["fights"] if f.get("kill")]
print(f"report {code}: {len(kills)} kills")
md = W.fetch_master_data(tok, code)
res = W.fetch_maintain_uptime(tok, code, kills, md)

# roster (class/spec) from the cached mapped week_data, for sanity context
roster = {}
p = WEEK_DATA_CACHE / f"{code}.json"
if p.exists():
    roster = (json.loads(p.read_text(encoding="utf-8")).get("roster") or {})

print("\n=== maintain uptime by player ===")
for nm in sorted(res):
    r = roster.get(nm, {})
    tag = f"{r.get('spec','?')} {r.get('class','?')}/{r.get('role','?')}"
    ups = ", ".join(f"{a} {v}%" for a, v in sorted(res[nm].items(), key=lambda x: -x[1]))
    print(f"  {nm:16s} [{tag:24s}] {ups}")
print(f"\n{len(res)} players with tracked uptime")
