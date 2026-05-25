"""Build the CaseloadNotes one-folder distribution + a release zip.

Usage:
    .venv\\Scripts\\python.exe build.py

Produces:
    dist/CaseloadNotes/                 <- PyInstaller one-folder build
        CaseloadNotes.exe
        README.txt                      <- user-facing first-run notes
        _internal/...                   <- Python runtime + bundled libs

    CaseloadNotes-vX.Y.Z.zip            <- ready-for-Releases archive
                                           in the project root

Per-user data (notes.yaml, templates/, caseload.csv, browser_data/,
screenshots/) lives in %APPDATA%\\caseload-notes\\ so the install
folder stays clean and read-only.
"""
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from src.version import __version__

DIST_NAME = "CaseloadNotes"

RELEASE_README = """\
Caseload Note Automation — v{version}
=======================================

Quick start
-----------
1. Make sure you have **Microsoft Edge** installed (default on Windows
   10/11). Outlook should also be installed and signed into your
   WGU account.

2. Double-click `{exe}` to launch.

3. On first run, the launcher opens an Edge window controlled by it.
   Sign in to Salesforce via SSO. The session is remembered for
   subsequent runs.

4. Open your Caseload list view (the default "My Students" works).
   Wait for the launcher's `↻ Caseload` auto-refresh to finish —
   it'll show "Caseload cache: N rows" in the activity log.

5. Fire a scenario from the buttons or hotkeys. The first time a
   scenario opens an Outlook email, Outlook may pop a "Programmatic
   Access" warning — click Allow (and check "allow for 10 minutes"
   if it offers).

User config + data
------------------
Your edited scenarios (notes.yaml), email templates, signatures,
cached caseload export, and the browser profile all live under:

    %APPDATA%\\caseload-notes\\

This folder is created on first run and survives reinstalls.

Windows Smart App Control may block the launcher
------------------------------------------------
This build is **not yet code-signed**, so Windows 11 with Smart App
Control enabled may silently refuse to run it. Two options:

A. Temporarily turn SAC off:
     Settings → Privacy & security → Windows Security → App &
     browser control → Smart App Control settings → Off.
   (SAC won't re-enable itself once turned off.)

B. Ask your IT department to whitelist the executable or run from
   an internal share that's already trusted.

A signed release is on the roadmap once a code-signing certificate
is available.

Reporting issues
----------------
Open an issue at:
  https://github.com/ashejim/WGU-Caseload-Note-Tool/issues

— Jim
"""


def folder_size_mb(p: Path) -> float:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) / 1024 / 1024


def make_release_zip(dist_dir: Path, project_root: Path) -> Path:
    """Zip the `dist/CaseloadNotes/` folder into a
    `CaseloadNotes-vX.Y.Z.zip` at the project root. The zip preserves
    the `CaseloadNotes/` top-level folder so users can extract it
    anywhere and run from there."""
    zip_path = project_root / f"{DIST_NAME}-v{__version__}.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in dist_dir.rglob("*"):
            if f.is_file():
                zf.write(f, arcname=f.relative_to(dist_dir.parent))
    return zip_path


def main() -> None:
    project_root = Path(__file__).resolve().parent

    for d in ("build", "dist"):
        target = project_root / d
        if target.exists():
            print(f"Cleaning {target}")
            shutil.rmtree(target)

    print(f"Building CaseloadNotes v{__version__}...")
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm",
         "caseload_notes.spec"],
        check=True, cwd=project_root,
    )

    dist = project_root / "dist" / DIST_NAME
    if not dist.exists():
        sys.exit(f"Expected build output at {dist} but it's missing.")

    # Drop the user-facing README into the dist root.
    readme_path = dist / "README.txt"
    readme_path.write_text(
        RELEASE_README.format(version=__version__, exe=f"{DIST_NAME}.exe"),
        encoding="utf-8",
    )
    print(f"Wrote {readme_path.relative_to(project_root)}")

    zip_path = make_release_zip(dist, project_root)
    zip_size_mb = zip_path.stat().st_size / 1024 / 1024

    print()
    print(f"Build complete: {dist}  ({folder_size_mb(dist):.1f} MB)")
    print(f"Release zip:    {zip_path.name}  ({zip_size_mb:.1f} MB)")
    print(f"Run locally:    {dist / (DIST_NAME + '.exe')}")
    print(f"Distribute:     upload {zip_path.name} to GitHub Releases")
    print("Recipients need Microsoft Edge installed (default on Win10/11).")


if __name__ == "__main__":
    main()
