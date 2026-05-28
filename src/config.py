"""Project paths and runtime configuration.

When running from source (`python -m scripts.launcher`), config and data
live in the project root. When running from a packaged PyInstaller exe,
they live in `%APPDATA%\\caseload-notes\\` so the install dir can stay
read-only and the app works across user accounts.
"""
import json
import os
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv


def _bundle_root() -> Optional[Path]:
    """Directory containing bundled data files when running as a frozen
    exe, or None when running from source.

    Detection covers:
    - PyInstaller: sets sys._MEIPASS to the extracted-bundle path.
    - Nuitka standalone: adds `__compiled__` to compiled module globals
      and the exe sits in the bundle directory itself.
    - Other freezers that follow the sys.frozen convention.
    """
    if hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)  # PyInstaller
    if "__compiled__" in globals() or getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent  # Nuitka / others
    return None


def _is_frozen() -> bool:
    return _bundle_root() is not None


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _user_config_dir() -> Path:
    if _is_frozen():
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(appdata) / "caseload-notes"
    return PROJECT_ROOT


USER_CONFIG_DIR = _user_config_dir()
USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)

# Point Playwright at the bundled Chromium when running from the exe.
# This must happen before any module imports playwright.sync_api.
_bundle = _bundle_root()
if _bundle is not None:
    _bundled_browsers = _bundle / "ms-playwright"
    if _bundled_browsers.exists():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(_bundled_browsers)

# Load .env from user config (priority) and project root (dev fallback).
load_dotenv(USER_CONFIG_DIR / ".env")
if not _is_frozen():
    load_dotenv(PROJECT_ROOT / ".env")

NOTES_YAML = USER_CONFIG_DIR / "notes.yaml"
BROWSER_DATA_DIR = USER_CONFIG_DIR / "browser_data"
SCREENSHOTS_DIR = USER_CONFIG_DIR / "screenshots"
NOTE_LOG_CSV = USER_CONFIG_DIR / "note_log.csv"
TEMPLATES_DIR = USER_CONFIG_DIR / "templates"
TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)

# Salesforce caseload export. Round 1 — user manually drops a CSV
# here; round 2 will populate it via Playwright export automation.
CASELOAD_CSV_PATH = USER_CONFIG_DIR / "caseload.csv"

# Default to WGU's standard Caseload page. Override via .env in the user
# config dir if your campus / org uses a different Salesforce instance.
DEFAULT_CASELOAD_URL = "https://srm.lightning.force.com/lightning/n/Caseload_App_Page"
CASELOAD_URL = os.getenv("CASELOAD_URL", DEFAULT_CASELOAD_URL).strip()


def _seed_user_notes_yaml() -> None:
    """First-run convenience: copy the bundled default notes.yaml into
    the user's config dir if they don't have one yet."""
    if NOTES_YAML.exists():
        return
    bundled_default = (_bundle / "notes.yaml") if _bundle is not None else (PROJECT_ROOT / "notes.yaml")
    if bundled_default.exists():
        NOTES_YAML.write_bytes(bundled_default.read_bytes())


def _seed_user_templates() -> None:
    """First-run convenience: copy bundled starter email templates into
    the user's templates dir if it's empty. Doesn't overwrite existing
    files."""
    if any(TEMPLATES_DIR.iterdir()):
        return
    src = (_bundle / "templates") if _bundle is not None else (PROJECT_ROOT / "templates")
    if not src.exists():
        return
    for f in src.iterdir():
        if f.is_file():
            (TEMPLATES_DIR / f.name).write_bytes(f.read_bytes())


_seed_user_notes_yaml()
_seed_user_templates()


# ===== Settings (user-toggleable preferences) =====
#
# A small JSON file alongside notes.yaml in USER_CONFIG_DIR. Currently
# only carries the advanced-mode toggle, but designed to grow — future
# preferences (auto-refresh intervals, default themes, etc.) get added
# as fields on the Settings dataclass without needing migration code.

SETTINGS_PATH = USER_CONFIG_DIR / "settings.json"


@dataclass
class Settings:
    """User preferences persisted across sessions. Defaults are the
    "basic user" configuration — advanced features hidden until the
    user opts in via the Settings dialog."""
    advanced_mode: bool = False
    # Set True once the user has been through the first-run setup
    # (mode picker + Caseload Tool view introduction). When False on
    # startup, the launcher pops the first-run welcome dialog before
    # anything else happens.
    first_run_complete: bool = False


def load_settings() -> Settings:
    """Read settings.json. Unknown keys are dropped (forwards-compat
    against settings written by a newer launcher), missing keys fall
    back to the dataclass defaults. Any failure returns a default
    Settings so the launcher never bricks on a corrupt file."""
    if not SETTINGS_PATH.exists():
        return Settings()
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return Settings()
    if not isinstance(data, dict):
        return Settings()
    valid = {f.name for f in fields(Settings)}
    filtered = {k: v for k, v in data.items() if k in valid}
    try:
        return Settings(**filtered)
    except Exception:
        return Settings()


def save_settings(settings: Settings) -> None:
    """Persist `settings` to disk. Best-effort — a failure to write
    just means the toggle won't survive the next restart."""
    try:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(
            json.dumps(asdict(settings), indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass
