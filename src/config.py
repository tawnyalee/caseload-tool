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

# The user's scenario/group definitions. Renamed from the legacy
# "notes.yaml" (the file only ever held scenarios + groups, never notes).
SCENARIOS_YAML = USER_CONFIG_DIR / "scenarios.yaml"
# Back-compat alias so older imports keep working.
NOTES_YAML = SCENARIOS_YAML
# The bundled sample scenario file (read-only reference): in a frozen
# build it sits in the bundle, in dev it's the project-root copy. Used to
# seed first-run and to let the user reload the samples from Settings.
DEFAULT_SCENARIOS_FILE = (
    (_bundle / "default_scenarios.yaml") if _bundle is not None
    else (PROJECT_ROOT / "default_scenarios.yaml")
)
BROWSER_DATA_DIR = USER_CONFIG_DIR / "browser_data"
SCREENSHOTS_DIR = USER_CONFIG_DIR / "screenshots"
NOTE_LOG_CSV = USER_CONFIG_DIR / "note_log.csv"

# Email templates. Renamed from the bare "templates" folder. Migrate the
# legacy folder name so upgraders keep their templates.
EMAIL_TEMPLATES_DIR = USER_CONFIG_DIR / "email_templates"
_legacy_templates_dir = USER_CONFIG_DIR / "templates"
if not EMAIL_TEMPLATES_DIR.exists() and _legacy_templates_dir.exists():
    try:
        _legacy_templates_dir.rename(EMAIL_TEMPLATES_DIR)
    except Exception:
        pass
EMAIL_TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)

# The user can keep several template folders and switch the active one
# from Settings. `templates_dir()` is the live folder used for reading,
# writing, listing, and rendering templates; `set_templates_dir()` swaps
# it. Defaults to EMAIL_TEMPLATES_DIR.
_active_templates_dir = EMAIL_TEMPLATES_DIR


def templates_dir() -> Path:
    """The currently-active email-templates folder."""
    return _active_templates_dir


def set_templates_dir(path) -> Path:
    """Switch the active email-templates folder (created if missing)."""
    global _active_templates_dir
    p = Path(path)
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    _active_templates_dir = p
    return p


# Back-compat constant (snapshot of the default folder). New code should
# call templates_dir() so folder switches take effect.
TEMPLATES_DIR = EMAIL_TEMPLATES_DIR

# Bundled sample template folder — seeds first-run + restorable from
# Settings (mirrors DEFAULT_SCENARIOS_FILE).
DEFAULT_EMAIL_TEMPLATES_DIR = (
    (_bundle / "default_email_templates") if _bundle is not None
    else (PROJECT_ROOT / "default_email_templates")
)

# Salesforce caseload export. Round 1 — user manually drops a CSV
# here; round 2 will populate it via Playwright export automation.
CASELOAD_CSV_PATH = USER_CONFIG_DIR / "caseload.csv"

# Local longitudinal history of the dynamic caseload fields (Momentum,
# task status, …) snapshotted on every CSV reload. SQLite so it reads
# straight into pandas; see src/history.py.
HISTORY_DB = USER_CONFIG_DIR / "history.db"

# Default to WGU's standard Caseload page. Override via .env in the user
# config dir if your campus / org uses a different Salesforce instance.
DEFAULT_CASELOAD_URL = "https://srm.lightning.force.com/lightning/n/Caseload_App_Page"
CASELOAD_URL = os.getenv("CASELOAD_URL", DEFAULT_CASELOAD_URL).strip()

# WGU's Essential Actions dashboard (cross-caseload list of open EAs).
# Scraped to surface EAs in the viewer. Override via .env if needed.
DEFAULT_ESSENTIAL_ACTIONS_URL = (
    "https://srm.lightning.force.com/lightning/n/Essential_Actions")
ESSENTIAL_ACTIONS_URL = os.getenv(
    "ESSENTIAL_ACTIONS_URL", DEFAULT_ESSENTIAL_ACTIONS_URL).strip()


def _seed_user_scenarios_yaml() -> None:
    """First-run setup for the user's scenario file, in priority order:
    1. If scenarios.yaml already exists, leave it alone.
    2. Migrate a legacy notes.yaml (the file was renamed) by moving it,
       so an upgrading user keeps all their scenarios.
    3. Otherwise seed the bundled default_scenarios.yaml sample so a
       first-time user has example batches/scenarios/emails to edit.
    """
    if SCENARIOS_YAML.exists():
        return
    legacy = USER_CONFIG_DIR / "notes.yaml"
    if legacy.exists():
        try:
            legacy.rename(SCENARIOS_YAML)
            return
        except Exception:
            try:
                SCENARIOS_YAML.write_bytes(legacy.read_bytes())
                return
            except Exception:
                pass
    bundled_default = (
        (_bundle / "default_scenarios.yaml") if _bundle is not None
        else (PROJECT_ROOT / "default_scenarios.yaml")
    )
    if bundled_default.exists():
        SCENARIOS_YAML.write_bytes(bundled_default.read_bytes())


def _seed_user_templates() -> None:
    """First-run convenience: copy the bundled sample email templates into
    the user's email_templates dir if it's empty. Doesn't overwrite
    existing files."""
    if any(EMAIL_TEMPLATES_DIR.iterdir()):
        return
    src = DEFAULT_EMAIL_TEMPLATES_DIR
    if not src.exists():
        return
    for f in src.iterdir():
        if f.is_file():
            (EMAIL_TEMPLATES_DIR / f.name).write_bytes(f.read_bytes())


_seed_user_scenarios_yaml()
_seed_user_templates()


# ===== Settings (user-toggleable preferences) =====
#
# A small JSON file alongside scenarios.yaml in USER_CONFIG_DIR. Currently
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
    # Window geometry "WxH+X+Y" and the x-position of the main
    # left/right divider, persisted on close and restored on launch
    # (only when the saved window would still be mostly on-screen).
    # Empty / 0 means "use the built-in default".
    window_geometry: str = ""
    main_sash: int = 0
    # All horizontal divider x-positions, comma-separated (main|editor,
    # editor|caseload, ...). Supersedes the single main_sash, which is
    # kept in sync for backward compatibility.
    sash_positions: str = ""
    # "normal" or "zoomed" (maximized). When zoomed, window_geometry
    # holds the last *normal* size to fall back to on un-maximize.
    window_state: str = "normal"
    # Geometry "WxH+X+Y" of the caseload panel when popped out into its
    # own window (for a second monitor). Empty means "use the default".
    caseload_window_geometry: str = ""
    # Caseload-panel column layout as a JSON string:
    #   {"visible": [csv_header,...ordered], "hidden": [csv_header,...],
    #    "widths": {csv_header: px}}
    # Empty means "show every column in CSV order at default width".
    # Columns the user has never seen (new CSV exports) default to visible
    # and append after the saved order.
    caseload_columns: str = ""
    # Saved viewer "Views": named bundles of {columns + filters} the user can
    # switch between from the viewer's View dropdown. JSON list of
    # {"name", "visible": [...], "hidden": [...], "filters": [...]}.
    caseload_views: str = ""
    caseload_current_view: str = ""   # last-applied view name (for the dropdown)
    # Warn (red activity-log line) before a batch/selection fire when the
    # cached caseload CSV is older than this many MINUTES. 0 = never warn.
    caseload_stale_minutes: int = 720  # 12 h
    # Caseload history: minimum HOURS between local snapshots (src/history.py).
    # A reload only records a new sample once this much time has passed since
    # the last one; within the window a fresher CSV updates the last sample in
    # place. 24 = once a day (the data changes ~daily); lower (e.g. 6/8) samples
    # more often. Departure detection stays day-grained regardless.
    history_capture_interval_hours: int = 24
    # Overall UI scale (CustomTkinter widget scaling); 1.0 = 100%.
    ui_scale: float = 1.0
    # Per-area text sizes (pt). 0 = use the built-in default for that area.
    # Adjustable live (Ctrl +/-, Ctrl+wheel) and via the Settings dialog.
    font_activity: int = 0
    font_viewer: int = 0
    font_email: int = 0
    font_editor: int = 0
    font_notes: int = 0
    # Caseload quick-view fields: JSON list of field keys (see
    # CaseloadPanel.QUICK_VIEW_CATALOG) in display order. Empty = the
    # built-in default set.
    quickview_fields: str = ""
    # Active email-templates folder (absolute path). Empty = the default
    # email_templates folder. Lets the user keep several template sets.
    email_templates_dir: str = ""
    # EMA Score Report links: per-course map of courseId + task ids, as a
    # JSON string {courseCode: {"1": {"course_id","task_id"}, ...}}. Seeded
    # by pasting a score-report URL the first time a task badge is clicked.
    ema_report_map: str = ""
    # Caseload columns to EXCLUDE from the "required columns missing"
    # check (comma/newline separated CSV header names). Lets a user who
    # deliberately doesn't use a feature silence its warning.
    required_columns_ignore: str = ""
    # Bulk live task pass/fail ("2a"): scroll-load the whole Caseload list
    # and read each task cell's color (passed/returned/in-process), the bit
    # the CSV export drops, then color the grid + quick-view badges. Adds
    # ~5-9s (scroll-load) and runs in the BACKGROUND after a refresh so it
    # never blocks the UI. Mode:
    #   "off"     — never bulk-scrape (badges still fetch per-student on
    #               open; grid task cells stay neutral/⚪)
    #   "restart" — one background pass after the startup auto-refresh only
    #   "refresh" — a background pass after startup AND every ↻ refresh
    task_status_scrape_mode: str = "restart"
    # How student/PM NAMES are capitalized when rendered into note/email
    # variables ({{first_name}}, {{preferred_name}}, {{pm_name}}, …), to
    # paper over CSV data-entry casing errors:
    #   "off"      — use the name exactly as stored in the CSV
    #   "lower"    — only fix lowercase ('john' → 'John'); leave ALL-CAPS
    #   "standard" — also normalize ALL-CAPS to Title case, preserving
    #                intentional mixed case (McDonald, O'Brien)
    name_capitalization: str = "standard"
    # Note-body editors: when True (default), Enter submits the note and
    # Shift+Enter inserts a newline. When False, Enter inserts a newline like
    # a normal text box and the note is submitted only via the button.
    enter_submits_note: bool = True
    # Caseload-panel "Fire action" menu: JSON list of scenario names, in the
    # order they appear in the right-click / Right-arrow action menu. Empty =
    # fall back to the per-scenario "Show as a caseload-panel action" flags
    # (or every non-batch scenario when none are flagged).
    panel_action_order: str = ""


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
