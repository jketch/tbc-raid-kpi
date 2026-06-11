#!/usr/bin/env python
"""Install the tracked git hooks (hooks/*) into .git/hooks/.

.git/hooks/ isn't version-controlled, so the real hook lives in the tracked hooks/
dir and this copies it in. Run once per clone (and after editing hooks/pre-commit):

    python scripts/install_hooks.py

Idempotent. On a fresh clone this is the only setup step the integrity gate needs.
"""
import shutil, stat, sys
from pathlib import Path

# Windows cp1252 console can't encode the ✓/⚠ glyphs we print — force UTF-8.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
SRC  = ROOT / "hooks"
DST  = ROOT / ".git" / "hooks"


def main():
    if not (ROOT / ".git").exists():
        sys.exit("  ⚠ no .git here — run from inside the repo")
    DST.mkdir(parents=True, exist_ok=True)
    n = 0
    for src in SRC.glob("*"):
        if src.is_dir():
            continue
        dst = DST / src.name
        shutil.copyfile(src, dst)
        dst.chmod(dst.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        print(f"  ✓ installed {src.name} → .git/hooks/{src.name}")
        n += 1
    print(f"  {n} hook(s) installed" if n else "  (no hooks in hooks/)")


if __name__ == "__main__":
    main()
