"""Parse a ThatsBIS "received loot" CSV export into the per-week shape the dashboard renders.

The export (`/export/loot/...` "received" rows) is a SEASON-long ledger; the dashboard is per-week,
so `parse_loot` filters to a single raid date and groups the awards by character.

This is an EXTERNAL, manual data source (not WCL) — it has no WCL headline. A missing/empty file is
not an error: `parse_loot` returns `{}` (falsy) and the caller hides the Loot card. The shaped dict
is baked into `WEEK_DATA.loot`, so the offline reprocess + publish pipelines pick it up for free.

Shape returned:
    { "players": [ { "name", "class",
                     "items": [ {"item_id", "item_name", "boss", "instance", "offspec": bool} ] } ],
      "total": <int item count>, "offspec": <int OS count> }
Players are sorted by item count (desc), then name; each player's items keep CSV order.
"""
import csv
from collections import OrderedDict


def parse_loot(csv_path: str, raid_date: str) -> dict:
    """Filter a ThatsBIS received-loot CSV to `raid_date` (YYYY-MM-DD) and group by character.

    Returns {} when the file is missing/unreadable or no rows match the week (caller guards)."""
    try:
        with open(csv_path, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
    except (OSError, UnicodeError) as e:
        print(f"  ⚠ loot: could not read {csv_path}: {e}")
        return {}

    by_player = OrderedDict()
    total = offspec = 0
    for r in rows:
        if (r.get("received_at") or "")[:10] != raid_date:
            continue
        name = (r.get("character_name") or "").strip()
        if not name:
            continue
        item_id = (r.get("item_id") or "").strip()
        is_os = (r.get("is_offspec") or "0").strip() == "1"
        entry = by_player.setdefault(name, {"name": name,
                                            "class": (r.get("character_class") or "").strip(),
                                            "items": []})
        entry["items"].append({
            "item_id":   int(item_id) if item_id.isdigit() else item_id,
            "item_name": (r.get("item_name") or "").strip(),
            "boss":      (r.get("source_name") or "").strip(),
            "instance":  (r.get("instance_name") or "").strip(),
            "offspec":   is_os,
        })
        total += 1
        offspec += 1 if is_os else 0

    if not total:
        return {}

    players = sorted(by_player.values(), key=lambda p: (-len(p["items"]), p["name"]))
    return {"players": players, "total": total, "offspec": offspec, "date": raid_date}


if __name__ == "__main__":  # quick manual check: python scripts/loot_parser.py <csv> <YYYY-MM-DD>
    import sys, json
    path = sys.argv[1] if len(sys.argv) > 1 else "loot/received-export.csv"
    date = sys.argv[2] if len(sys.argv) > 2 else "2026-06-08"
    print(json.dumps(parse_loot(path, date), indent=2, ensure_ascii=False))
