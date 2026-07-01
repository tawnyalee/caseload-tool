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


# Email link color — applied to the <a> tags the rich-text editor emits so
# links render in a consistent color regardless of the recipient client's
# default hyperlink style. Live-global, mirroring the templates_dir() pattern;
# set from Settings on startup and when the user changes it.
DEFAULT_EMAIL_LINK_COLOR = "#1a73e8"


def _norm_hex_color(color) -> str:
    """Normalize a #RGB / #RRGGBB hex string to lowercase #RRGGBB; fall back
    to the default for anything malformed. Keeps the value safe to drop into a
    style="" attribute (no injection from a hand-typed setting)."""
    import re
    s = (color or "").strip()
    if re.fullmatch(r"#[0-9A-Fa-f]{6}", s):
        return s.lower()
    if re.fullmatch(r"#[0-9A-Fa-f]{3}", s):
        return ("#" + "".join(c * 2 for c in s[1:])).lower()
    return DEFAULT_EMAIL_LINK_COLOR


_active_email_link_color = DEFAULT_EMAIL_LINK_COLOR


def email_link_color() -> str:
    """The active email link color (hex #RRGGBB)."""
    return _active_email_link_color


def set_email_link_color(color) -> str:
    """Set the active email link color (normalized; invalid → default)."""
    global _active_email_link_color
    _active_email_link_color = _norm_hex_color(color)
    return _active_email_link_color


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

# WGU's "passed in the last 30 days" caseload view, downloaded as CSV like any
# caseload export. It's the ONLY source of final pass outcomes (the live
# caseload drops passers), so it's ingested into history.db's `outcomes` table.
# The user drops the file here (or it's auto-detected in Downloads); the glob
# tolerates the browser's "(1)" dedupe suffixes.
OUTCOMES_ARCHIVE_GLOB = "results_archive*.csv"
# Where the AUTO-downloaded archive is saved (the app switches the caseload list
# view to "Archive (Last 30 Days)", exports, and lands the CSV here). Ingested
# then deleted, so no extra plaintext PII lingers.
OUTCOMES_ARCHIVE_DOWNLOAD = USER_CONFIG_DIR / "results_archive.csv"


def outcomes_archive_search_dirs() -> list[Path]:
    """Folders scanned (newest match wins) for a freshly-downloaded passed
    archive: the user config dir (where the user drops it) and the OS Downloads
    folder (where the browser saves it)."""
    dirs = [USER_CONFIG_DIR]
    try:
        downloads = Path.home() / "Downloads"
        if downloads.exists():
            dirs.append(downloads)
    except Exception:
        pass
    return dirs


def find_latest_outcomes_archive() -> Optional[Path]:
    """Most recently modified ``results_archive*.csv`` across the search dirs,
    or None if there isn't one. Used for auto-ingest on reload."""
    best: Optional[Path] = None
    best_mtime = -1.0
    for d in outcomes_archive_search_dirs():
        try:
            for p in d.glob(OUTCOMES_ARCHIVE_GLOB):
                if not p.is_file():
                    continue
                m = p.stat().st_mtime
                if m > best_mtime:
                    best, best_mtime = p, m
        except Exception:
            continue
    return best

# Local per-student Success Path state (see src/success_path.py): the data
# the user enters (field_values) plus an append-only log of step
# completions/skips (step_log). Deliberately SEPARATE from history.db —
# this is durable, mutable current state + an event log, not the
# append-only time-series snapshots. Step/path *definitions* live in
# scenarios.yaml; this DB holds only per-student data.
SUCCESS_PATH_DB = USER_CONFIG_DIR / "success_path.db"

# Local data-at-rest encryption (see src/crypto_store.py). The vault metadata
# (salt, password verifier, optional DPAPI-sealed key) lives here; the data
# files below are decrypted to their plain paths on unlock and re-encrypted
# (plaintext shredded) on exit. ENCRYPTED_DATA_FILES is every local file that
# holds student PII — note that the contact-id map lives INSIDE history.db.
VAULT_PATH = USER_CONFIG_DIR / "vault.json"
ENCRYPTED_DATA_FILES = [
    HISTORY_DB, SUCCESS_PATH_DB, NOTE_LOG_CSV, CASELOAD_CSV_PATH,
]

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
    # Color (hex #RRGGBB) applied to links in emails composed with the
    # rich-text editor. Default = the editor's standard preview blue.
    email_link_color: str = "#1a73e8"
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
    # File notes through Salesforce's own note-save endpoint (the same Aura
    # action the Note form posts) instead of driving the on-page form, when a
    # student's Contact id + a harvested session token are available. This
    # sidesteps the form's cold-start Academic-Activity gate (the intermittent
    # "Couldn't tick the Academic Activity … Submit disabled" failure) and is
    # faster. The on-page form stays the automatic fallback for anything not
    # eligible (no Contact id, EA-attached notes, a note left unsubmitted) or
    # if the API call fails. Toggle off to file every note via the form.
    note_save_via_api: bool = True
    # Source the caseload from the live grid JSON (getCaseLoadMainGridData) when
    # that feed is healthy, overlaying any CSV-only columns from the downloaded
    # CSV — so the caseload is complete regardless of how the Salesforce list
    # view is configured (no manual column setup). Falls back to the CSV
    # automatically when the feed is degraded. Toggle off to always use the CSV.
    caseload_source_json: bool = True
    # Local data-at-rest encryption: how often the app password is required.
    #   "every_launch" — prompt on every start (nothing remembered)
    #   "per_restart"  — remember within a boot session; re-prompt after a
    #                    reboot (or the 7-day backstop). DPAPI-sealed key.
    #   "weekly"       — remember for 7 days regardless of reboots
    # Only meaningful once a vault has been set up. See src/crypto_store.py.
    vault_unlock_mode: str = "per_restart"
    # Set once the user has seen the "protect your data" setup offer, so a user
    # who declined encryption isn't nagged on every launch (they can still turn
    # it on from Settings).
    encryption_offer_seen: bool = False
    # Caseload-panel "Fire action" menu: JSON list of scenario names, in the
    # order they appear in the right-click / Right-arrow action menu. Empty =
    # fall back to the per-scenario "Show as a caseload-panel action" flags
    # (or every non-batch scenario when none are flagged).
    panel_action_order: str = ""
    # Passed-outcomes archive (results_archive*.csv → history.db `outcomes`):
    # remind, on reload, to download a fresh archive once the last-downloaded
    # one is older than this many DAYS. The view is a rolling 30-day window, so
    # 14 keeps full coverage with margin. 0 = never remind. A freshly-downloaded
    # archive found in the search dirs is auto-ingested regardless.
    outcomes_archive_reminder_days: int = 14
    # Geometry "WxH+X+Y" of the Data panel when popped out into its own window.
    # Empty means "use the default size".
    data_window_geometry: str = ""


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
