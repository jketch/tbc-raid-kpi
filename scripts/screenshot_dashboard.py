from playwright.sync_api import sync_playwright
from pathlib import Path
import argparse
import sys

DASHBOARD = Path(__file__).parent.parent / "dashboard" / "raid_kpi_dashboard.html"
OUT_DIR   = Path(__file__).parent.parent / "screenshots"

TABS = [
    ("overview", "Overview"),
    ("shame",    "Accountability"),
    ("healing",  "Healers & Tanks"),
    ("dps",      "DPS"),
    ("utility",  "Utility"),
]
VALID_KEYS = [k for k, _ in TABS]


def _wait_for_render(page):
    """Block until the boot sequence has run (showTab marks a button active)."""
    page.wait_for_function("() => !!document.querySelector('.tab-btn.active')", timeout=10_000)


def _date_prefix(page):
    raw = page.evaluate("() => (WEEK_DATA.meta||{}).date||''")
    return raw.replace("/", "-").replace(" ", "_") or "dashboard"


def main(fmt="pdf", tabs=None, prefix=None):
    if not DASHBOARD.exists():
        sys.exit(f"Dashboard not found: {DASHBOARD}")

    selected = [(k, v) for k, v in TABS if tabs is None or k in tabs]
    if not selected:
        sys.exit(f"No matching tabs. Valid: {', '.join(VALID_KEYS)}")

    OUT_DIR.mkdir(exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1920, "height": 1080})
            page.goto(f"file:///{DASHBOARD.resolve()}")
            page.wait_for_load_state("networkidle")
            _wait_for_render(page)

            out_prefix = prefix or _date_prefix(page)

            if fmt == "png":
                for data_tab, label in selected:
                    page.locator(f"button[data-tab='{data_tab}']").click()
                    page.wait_for_timeout(300)
                    out = OUT_DIR / f"{out_prefix}_{data_tab}.png"
                    page.screenshot(path=out, full_page=True)
                    print(f"  ✓ {label} → {out.name}")

            else:  # pdf
                try:
                    from pypdf import PdfWriter
                except ImportError:
                    sys.exit("pypdf not installed — run: pip install pypdf")

                tmp_dir = OUT_DIR / "_tmp"
                tmp_dir.mkdir(exist_ok=True)
                tmp_files = []

                for data_tab, label in selected:
                    page.locator(f"button[data-tab='{data_tab}']").click()
                    page.wait_for_timeout(300)
                    tmp_path = tmp_dir / f"{data_tab}.pdf"
                    page.pdf(path=tmp_path, format="A3", landscape=True, print_background=True)
                    tmp_files.append(tmp_path)
                    print(f"  ✓ {label}")

        finally:
            browser.close()

    if fmt == "pdf":
        writer = PdfWriter()
        for f in tmp_files:
            writer.append(f)
        out_path = OUT_DIR / f"{out_prefix}_raid_kpi.pdf"
        writer.write(out_path)
        writer.close()

        for f in tmp_files:
            f.unlink()
        try:
            tmp_dir.rmdir()
        except OSError:
            pass

        print(f"\n  → {out_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export raid KPI dashboard to PNG or PDF.")
    parser.add_argument("--format", choices=["png", "pdf"], default="pdf",
                        help="png = one file per tab; pdf = single multipage (default)")
    parser.add_argument("--tab", metavar="TAB", nargs="+", choices=VALID_KEYS,
                        help=f"Tabs to capture (default: all). Choices: {', '.join(VALID_KEYS)}")
    parser.add_argument("--prefix", metavar="STR",
                        help="Filename prefix (default: date from dashboard meta)")
    args = parser.parse_args()
    main(fmt=args.format, tabs=set(args.tab) if args.tab else None, prefix=args.prefix)
