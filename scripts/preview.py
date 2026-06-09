"""Fast design-iteration loop — NO API, NO DB.

Pulls the already-injected WEEK_DATA out of the generated dashboard and re-injects
it into the freshly-edited template.html, regenerating the output in ~1s. Use it to
eyeball markup/CSS/JS edits to template.html without re-running the weekly pipeline.

    edit dashboard/template.html  →  python scripts/preview.py  →  hard-refresh browser

Note: opening template.html directly does NOT reflect new WEEK_DATA keys — its embedded
snapshot may predate them. This script grafts the real injected data onto the new template.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from wcl_auto_dashboard import inject_into_html, DASH_FILE  # noqa: E402
from publish import extract_week_data  # noqa: E402


def main():
    wd = extract_week_data()
    if not wd:
        print("✗ no WEEK_DATA found in", DASH_FILE.name,
              "— run the full pipeline once before previewing.")
        sys.exit(1)
    # extract_week_data() returns the ALREADY-mapped final WEEK_DATA. Pass it as
    # `mapped=` so inject_into_html writes it verbatim — re-running it through
    # map_to_week_data() (the default path) expects the raw `wcl` dict and would
    # blow away every key it doesn't recognize.
    inject_into_html(None, DASH_FILE, mapped=wd)
    print(f"✓ re-injected WEEK_DATA into {DASH_FILE.name} from the current template "
          f"({len(wd)} keys). Hard-refresh (Ctrl+Shift+R).")


if __name__ == "__main__":
    main()
