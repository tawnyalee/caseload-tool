"""Build the CaseloadNotes one-folder distribution.

Usage:
    .venv\\Scripts\\python.exe build.py

Produces:
    dist/CaseloadNotes/
        CaseloadNotes.exe
        _internal/
            ms-playwright/        <- bundled Chromium browser
            notes.yaml            <- default note presets
            ... (Python runtime, libs, etc.)

To distribute: zip dist/CaseloadNotes/ and send the archive. The
recipient extracts it anywhere and double-clicks CaseloadNotes.exe.

Per-user data (their edited notes.yaml, browser_data/, screenshots/)
lives in %APPDATA%\\caseload-notes\\ so the install folder stays
clean and the app works across user accounts.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

DIST_NAME = "CaseloadNotes"


def find_chromium_root() -> Path:
    base = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if not base:
        base = str(Path.home() / "AppData" / "Local" / "ms-playwright")
    return Path(base)


def folder_size_mb(p: Path) -> float:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) / 1024 / 1024


def main() -> None:
    project_root = Path(__file__).resolve().parent
    chromium_src = find_chromium_root()
    if not chromium_src.exists():
        sys.exit(
            f"Playwright Chromium not found at {chromium_src}\n"
            "Install it first:\n"
            "    python -m playwright install chromium"
        )

    for d in ("build", "dist"):
        target = project_root / d
        if target.exists():
            print(f"Cleaning {target}")
            shutil.rmtree(target)

    print("Running PyInstaller...")
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm",
         "caseload_notes.spec"],
        check=True, cwd=project_root,
    )

    dist = project_root / "dist" / DIST_NAME
    if not dist.exists():
        sys.exit(f"Expected build output at {dist} but it's missing.")

    chromium_dst = dist / "_internal" / "ms-playwright"
    print(f"Copying Chromium from {chromium_src} -> {chromium_dst}")
    shutil.copytree(chromium_src, chromium_dst)

    print()
    print(f"Build complete: {dist}  ({folder_size_mb(dist):.1f} MB)")
    print(f"Run:            {dist / (DIST_NAME + '.exe')}")
    print(f"Distribute:     zip the {DIST_NAME}/ folder and share")


if __name__ == "__main__":
    main()
