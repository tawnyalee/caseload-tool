"""Build CaseloadNotes using Nuitka.

Nuitka compiles Python to C, then to a native Windows binary. The
output is less likely to trigger Smart App Control than PyInstaller's
bootloader, which is the reason we maintain a parallel build path.

Tradeoffs vs PyInstaller (build.py):
- Build time: ~10-30 minutes (vs ~10 seconds)
- Needs a C compiler. Nuitka can download MinGW64 on first run if MSVC
  isn't available (we pass --assume-yes-for-downloads to allow that).
- Smaller startup time at runtime, real compiled binaries.

Output layout matches build.py for symmetry:
    dist-nuitka/CaseloadNotes/
        CaseloadNotes.exe
        ms-playwright/        <- bundled Chromium
        notes.yaml            <- default presets
        ... (Nuitka runtime libs)

Usage:
    .venv\\Scripts\\python.exe build_nuitka.py
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

DIST_PARENT = "dist-nuitka"
FINAL_NAME = "CaseloadNotes"


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
            "Install: python -m playwright install chromium"
        )

    dist_parent = project_root / DIST_PARENT
    if dist_parent.exists():
        print(f"Cleaning {dist_parent}")
        shutil.rmtree(dist_parent)

    print("Running Nuitka (typically 10-30 minutes — first run is slowest)...")
    cmd = [
        sys.executable, "-m", "nuitka",
        "--standalone",
        "--windows-console-mode=disable",
        "--enable-plugin=tk-inter",
        "--include-package=customtkinter",
        "--include-package=pynput",
        "--include-package=playwright",
        "--include-package=yaml",
        "--include-package-data=customtkinter",
        "--include-package-data=playwright",
        "--include-data-files=notes.yaml=notes.yaml",
        f"--output-dir={DIST_PARENT}",
        f"--output-filename={FINAL_NAME}",
        "--assume-yes-for-downloads",
        "scripts/launcher.py",
    ]
    subprocess.run(cmd, check=True, cwd=project_root)

    # Nuitka names the output folder after the input file: launcher.dist
    nuitka_out = dist_parent / "launcher.dist"
    final_dir = dist_parent / FINAL_NAME
    if not nuitka_out.exists():
        sys.exit(f"Nuitka output not found at {nuitka_out}")
    nuitka_out.rename(final_dir)
    print(f"Renamed {nuitka_out.name} -> {final_dir.name}")

    chromium_dst = final_dir / "ms-playwright"
    print(f"Copying Chromium -> {chromium_dst}")
    shutil.copytree(chromium_src, chromium_dst)

    print()
    print(f"Build complete: {final_dir}  ({folder_size_mb(final_dir):.1f} MB)")
    print(f"Run:            {final_dir / (FINAL_NAME + '.exe')}")
    print(f"Distribute:     zip the {FINAL_NAME}/ folder and share")


if __name__ == "__main__":
    main()
