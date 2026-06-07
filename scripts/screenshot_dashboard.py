from playwright.sync_api import sync_playwright
from pathlib import Path
import argparse

DASHBOARD = Path(__file__).parent.parent / "dashboard" / "raid_kpi_dashboard.html"
OUT_DIR   = Path(__file__).parent.parent / "screenshots"

TABS = [
    ("overview",  "Overview"),
    ("shame",     "Accountability"),
    ("healing",   "Healers & Tanks"),
    ("dps",       "DPS"),
    ("utility",   "Utility"),
]

def main(fmt="pdf"):
    OUT_DIR.mkdir(exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page    = browser.new_page(viewport={"width": 1920, "height": 1080})
        page.goto(f"file:///{DASHBOARD.resolve()}")
        page.wait_for_load_state("networkidle")

        if fmt == "png":
            for data_tab, label in TABS:
                page.locator(f"button[data-tab='{data_tab}']").click()
                page.wait_for_timeout(300)
                out = OUT_DIR / f"{data_tab}.png"
                page.screenshot(path=out, full_page=True)
                print(f"  ✓ {label} → {out.name}")

        else:  # pdf
            try:
                from pypdf import PdfWriter
            except ImportError:
                import subprocess, sys
                subprocess.check_call([sys.executable, "-m", "pip", "install", "pypdf", "--quiet"])
                from pypdf import PdfWriter

            tmp_dir = OUT_DIR / "_tmp"
            tmp_dir.mkdir(exist_ok=True)
            tmp_files = []

            for data_tab, label in TABS:
                page.locator(f"button[data-tab='{data_tab}']").click()
                page.wait_for_timeout(300)
                tmp_path = tmp_dir / f"{data_tab}.pdf"
                page.pdf(path=tmp_path, format="A3", landscape=True, print_background=True)
                tmp_files.append(tmp_path)
                print(f"  ✓ {label}")

            browser.close()

            writer = PdfWriter()
            for f in tmp_files:
                writer.append(f)
            out_path = OUT_DIR / "raid_kpi_dashboard.pdf"
            writer.write(out_path)
            writer.close()

            for f in tmp_files:
                f.unlink()
            tmp_dir.rmdir()

            print(f"\n  → {out_path}")
            return

        browser.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--format", choices=["png", "pdf"], default="pdf",
                        help="Output format: png (one file per tab) or pdf (single multipage)")
    args = parser.parse_args()
    main(args.format)
