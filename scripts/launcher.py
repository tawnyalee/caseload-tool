"""Caseload Note Automation — launcher.

Two panes side by side:
- Left:  status, course-code field, scenario buttons, activity log
- Right: collapsible editor with one tab per scenario, fields laid out
         like the Caseload note form. Edits write back to scenarios.yaml,
         scenarios reload, hotkeys re-register.

Global hotkeys (defined in scenarios.yaml) trigger scenarios anywhere on
the system. Bare F-keys are claimed system-wide so the browser doesn't
also react to them.

Usage:
    python -m scripts.launcher
"""
import csv
import html
import os
import queue
import re
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Optional

import customtkinter as ctk
import tkinter as tk
from tkinter import ttk
import tkinter.font as tkfont
import yaml
from pynput import keyboard

try:
    from PIL import ImageGrab
    _HAS_PIL = True
except Exception:
    _HAS_PIL = False

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import caseload_csv, caseload_filter, email_template, history
from src.browser import persistent_context
from src.config import (
    CASELOAD_CSV_PATH, CASELOAD_URL, DEFAULT_EMAIL_TEMPLATES_DIR,
    DEFAULT_SCENARIOS_FILE, EMAIL_TEMPLATES_DIR, HISTORY_DB, NOTE_LOG_CSV,
    USER_CONFIG_DIR, Settings, load_settings, save_settings,
    set_templates_dir, templates_dir,
)
from src.version import __version__
from src.note_form import NoteData
from src.scenarios import (
    SCENARIOS_YAML, BatchConfig, EmailConfig, Group, ScenarioConfig,
    load_groups, load_scenarios, run_scenario,
)
from src.student_lookup import (
    click_caseload_row,
    find_and_click_student,
    gather_caseload_matches,
    gather_fuzzy_caseload_matches,
    get_active_student_name,
    lookup_caseload_student,
    _parse_mailto,
    scrape_student_email_from_page,
)


@dataclass
class NoteLogEntry:
    """One filed note. Used both for the in-session tabs and the
    persistent CSV that feeds downstream tools (e.g. the texting app).

    `submitted` is False when any note in the scenario opted out of
    auto-submit (the form was filled but the user is reviewing it).
    `student_id`, `student_email`, `pm_name`, `pm_email` come from the
    Caseload table row when available; the 'Email Student' link has
    the PM as primary (so `pm_email`) and the student as CC.
    """
    timestamp: datetime
    scenario: str
    course_code: str
    student: str
    student_id: str = ""
    student_email: str = ""
    pm_name: str = ""
    pm_email: str = ""
    submitted: bool = True

    @property
    def tab_key(self) -> str:
        return f"{self.course_code} {self.scenario}"

    @property
    def display(self) -> str:
        flag = "" if self.submitted else "  (not submitted)"
        id_suffix = f"  [{self.student_id}]" if self.student_id else ""
        return f"{self.timestamp:%H:%M:%S}  {self.student}{id_suffix}{flag}"


CSV_HEADER = [
    "timestamp", "scenario", "course_code", "student",
    "student_id", "student_email", "pm_name", "pm_email", "submitted",
]

# Old CSV column names that map to current ones. Used by the on-disk
# migration so existing logs upgrade in place rather than losing data.
CSV_COLUMN_RENAMES = {
    "email": "pm_email",  # earlier schema labelled it just "email"
}

# Values matching the Caseload form's dropdown + checkbox labels.
INTERACTION_TYPES_SINGLE = [
    "Email to Student", "Live Call", "Email from Student", "Video Call",
    "Course Chatter Response", "Voicemail to Student",
    "Instant Message (IM) / Text", "Voicemail from Student",
    "Webinar Attendance Noted", "Admin Note", "Mass Email", "Cohort Event",
]
INTERACTION_TYPES_MULTI = [
    "Live Call and Email to Student", "Email Exchange with Student",
    "Voicemail and Email to Student", "Voicemail/Email and Text to Student",
    "Voicemail to Student and Text Message", "Live Call and Text Message",
    "Email to Student and Text Message", "Video Call and Email to Student",
    "Voicemail from Student and Email to Student",
    "Voicemail Full/Email to Student",
]
# Single Interaction types that disable Academic Activities (one-way /
# administrative / outbound interactions where no student engagement
# needs to be characterized).
ACTIVITY_DISABLE_TYPES_SINGLE = {
    "Email to Student", "Voicemail to Student", "Admin Note", "Mass Email",
}
ACADEMIC_ACTIVITY_LABELS = [
    "Course/Program Information Discussed",
    "Course/Program Information Requested",
    "Set Academic Goals",
    "Student Learning Occurred",
    "Personal obstacles/non-academic content covered",
]
INTERACTION_FORMATS = ["Single Interaction", "Multiple Interactions"]


def types_for_format(fmt: str) -> list[str]:
    return INTERACTION_TYPES_MULTI if fmt == "Multiple Interactions" else INTERACTION_TYPES_SINGLE


def activities_disabled_for(fmt: str, typ: str) -> bool:
    return fmt == "Single Interaction" and typ in ACTIVITY_DISABLE_TYPES_SINGLE


# ============================================================
# Browser worker — owns Playwright in its own thread.
# ============================================================


def _typo_variants(query: str) -> list[str]:
    """All adjacent-transposition variants of `query`. Most natural
    one-typo cases (e.g. 'jsoh' for 'josh') are a single adjacent
    swap, so trying each against Salesforce's row filter often
    surfaces the right student even when fuzzy doesn't have enough
    of the table in view."""
    out: list[str] = []
    seen = {query}
    for i in range(len(query) - 1):
        chars = list(query)
        chars[i], chars[i + 1] = chars[i + 1], chars[i]
        v = "".join(chars)
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _wait_grid_settled(page, max_ms: int = 1500) -> None:
    """After typing into the caseload row-filter, wait until the grid has
    settled — no visible loading spinner and the row count stable across two
    polls — BOUNDED by max_ms. Replaces a blind sleep: returns as soon as
    the filter has applied (often a few hundred ms), never later than the
    old fixed wait."""
    import time as _t
    deadline = _t.monotonic() + max_ms / 1000.0
    try:
        page.wait_for_timeout(120)  # let a spinner appear before polling
    except Exception:
        return
    last, stable = -1, 0
    while _t.monotonic() < deadline:
        try:
            spinner = page.locator(
                ".slds-spinner_container").filter(visible=True).count()
        except Exception:
            spinner = 0
        try:
            cnt = page.locator("table tr").count()
        except Exception:
            cnt = -1
        if spinner == 0 and cnt >= 0 and cnt == last:
            stable += 1
            if stable >= 2:
                return
        else:
            stable = 0
        last = cnt
        try:
            page.wait_for_timeout(120)
        except Exception:
            return


def _wait_record_ready(page, max_ms: int = 2000) -> None:
    """After navigating to a record, return as soon as it's ready (the
    active student name resolves) — BOUNDED by max_ms. Replaces a blind
    post-navigation sleep."""
    import time as _t
    deadline = _t.monotonic() + max_ms / 1000.0
    while _t.monotonic() < deadline:
        try:
            if get_active_student_name(page):
                return
        except Exception:
            pass
        try:
            page.wait_for_timeout(150)
        except Exception:
            return


class BrowserWorker:
    SHUTDOWN = object()

    def __init__(
        self,
        on_status: Callable[[str], None],
        on_note_filed: Callable[[NoteLogEntry], None],
        on_multiple_matches: Callable[[str, list[str]], None],
    ):
        self.q: queue.Queue = queue.Queue()
        self.on_status = on_status
        self.on_note_filed = on_note_filed
        self.on_multiple_matches = on_multiple_matches
        # Most recent LIST_MATCHES result, used by CLICK_MATCH to map a
        # chosen name back to its row locator.
        self._last_matches: list[tuple] = []
        # Network-capture mode (for discovering Salesforce's REST API
        # endpoint without speculation). When `_capture_active` is True,
        # the request listener appends every Salesforce-bound POST /
        # PATCH / PUT into `_capture_log`. App drives start/stop +
        # save-to-file. No PII safeguarding yet; user must scrub
        # before sharing.
        self._capture_active = False
        self._capture_log: list[dict] = []
        # One-shot diagnostic latches for the batch-email scrape path
        # in `_click_match_by_filter`. The first batch of the session
        # logs WHAT the row mailto carried (or didn't), and WHAT the
        # contact-card scrape found (or didn't), so users can paste
        # the result here when emails aren't resolving. Quiet after
        # that — chatty diagnostics on every student would drown the
        # actual progress messages.
        self._mailto_diag_logged = False
        self._contact_card_diag_logged = False
        self.ready_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def submit_scenario(
        self,
        scenario: ScenarioConfig,
        course_code_override: str,
        clipboard: str = "",
        custom_bodies: Optional[dict[int, str]] = None,
        prompt_vars: Optional[dict[str, str]] = None,
        on_done: Optional[Callable[[bool], None]] = None,
        ea: Optional[tuple] = None,
    ) -> None:
        """Queue a scenario for the worker to fill notes against the
        active student. `prompt_vars` carries the user-typed values
        for any `prompts:` block in the scenario; they're substituted
        into note bodies (and email body / subject / to, handled on
        the main thread before queueing). `on_done(success)` is
        called from the worker thread when the run finishes.

        `ea` = (reason, course, close) to file the note via the student's
        Essential Action ("Add Note to EA" / "Add Note & Close EA")
        instead of the embedded note panel; None for the normal path."""
        self.q.put((
            "RUN", scenario, course_code_override, clipboard,
            custom_bodies or {}, prompt_vars or {}, on_done, ea,
        ))

    def submit_read_essential_actions(
        self, on_done: Callable[[dict], None],
    ) -> None:
        """Read the active student's open Essential Actions.
        on_done({eas:[{reason,course,event_progress,intervention}]})."""
        self.q.put(("READ_EA", on_done))

    def submit_read_ea_dashboard(
        self, on_done: Callable[[dict], None],
    ) -> None:
        """Scrape the cross-caseload Essential Actions DASHBOARD.
        on_done({eas:[{student_id,name,reason,...}]})."""
        self.q.put(("READ_EA_DASHBOARD", on_done))

    def submit_find_student(self, query: str, new_tab: bool = False,
                            raise_after: Optional[bool] = None) -> None:
        """Navigate to a student record. When `new_tab` is True the
        student opens in a fresh console subtab (leaving existing tabs
        open); otherwise the current tab is reused. `raise_after`
        controls whether the browser is pulled to the foreground when
        done — defaults to (not new_tab); pass False to navigate in the
        background (e.g. while the user is mid-dialog firing a scenario)."""
        self.q.put(("FIND", query, new_tab, raise_after))

    def submit_find_and_settle(
        self, query: str, on_done: Callable[[bool], None],
    ) -> None:
        """Navigate to a student via the FAST find path (Shift+X switch
        on the live Caseload list, no reload), then poll until the note
        panel is actually loaded. Used by fire-from-row so the note/email
        steps run against a ready record without the slower
        list-matches+click path. `on_done(success)` fires from the
        worker thread."""
        self.q.put(("FIND_AND_SETTLE", query, on_done))

    def submit_list_matches(
        self, query: str, on_results: Callable[[list[str]], None],
    ) -> None:
        """Search Caseload for matching names without clicking anything.
        Stores the matches on the worker so a later CLICK_MATCH can
        resolve a chosen name back to its row. `on_results(names)` is
        called from the worker thread when done."""
        self.q.put(("LIST_MATCHES", query, on_results))

    def submit_click_match(
        self, name: str, on_done: Callable[[bool], None],
    ) -> None:
        """Click the row from the most recent LIST_MATCHES whose name
        equals `name`. Includes a brief settle-wait after navigation.
        `on_done(success)` is called from the worker thread."""
        self.q.put(("CLICK_MATCH", name, on_done))

    def submit_get_student_context(
        self,
        on_done: Callable[[Optional[dict]], None],
        name_hint: str = "",
    ) -> None:
        """Read the currently-active student's context (name, email,
        course code, PM email, etc.) from the open note panel and/or
        the Caseload table. `name_hint` is used when the caller knows
        which student they just navigated to but the note panel may
        not be open yet (e.g. straight after a find-first navigation).
        `on_done(info_dict_or_None)` is called from the worker."""
        self.q.put(("GET_STUDENT_CONTEXT", on_done, name_hint))

    def submit_fetch_notes(
        self, query: str, on_done: Callable[[dict], None],
    ) -> None:
        """Open the student's record, wait for their notes to load, and
        scrape them. on_done({notes, count, timings})."""
        self.q.put(("FETCH_NOTES", query, on_done))

    def submit_fetch_task_status(
        self, query: str, on_done: Callable[[dict], None],
    ) -> None:
        """Row-filter the live Caseload list to `query` (a Student ID) and
        read the real task pass/fail (the cell color/title the CSV export
        drops). on_done({statuses: {"1": {state,status,date,attempts}, ...}})
        or {error}."""
        self.q.put(("FETCH_TASK_STATUS", query, on_done))

    def submit_scrape_all_task_status(
        self, on_done: Callable[[dict], None],
    ) -> None:
        """Bulk '2a' scrape: scroll-load the whole Caseload list and read
        every task cell's live pass/fail, keyed by Student ID.
        on_done({by_sid: {sid: {tnum: {...}}}, count}) or {error}."""
        self.q.put(("SCRAPE_ALL_TASK_STATUS", on_done))

    def submit_probe_text(self, on_done: Callable[[dict], None]) -> None:
        """TEMP dev probe: capture the live Mongoose ("Cadence") texting
        composer DOM (for building the texting send selectors)."""
        self.q.put(("PROBE_TEXT", on_done))

    def submit_open_mongoose(self, on_done: Callable[[dict], None]) -> None:
        """Open the Mongoose texting dashboard as a tab in the launcher's OWN
        browser context (so the probe / texting automation can see it)."""
        self.q.put(("OPEN_MONGOOSE", on_done))

    def submit_test_text(
        self, mobile: str, body: str, on_done: Callable[[dict], None],
    ) -> None:
        """TEMP dev: drive the Mongoose compose modal for one test recipient
        (commit=False — stops at the confirm step for review)."""
        self.q.put(("TEST_TEXT", mobile, body, on_done))

    def submit_send_text(
        self, payload: dict, on_done: Callable[[dict], None],
    ) -> None:
        """Drive the Mongoose compose modal from a fired text action. `payload`:
        body, recipients (list of mobiles), inbox_label, schedule (slot dict or
        None), schedule_name, commit."""
        self.q.put(("SEND_TEXT", payload, on_done))

    def submit_set_followup_date(
        self, query: str, date_str: str, on_done: Callable[[dict], None],
    ) -> None:
        """Row-filter the Caseload list to `query` (a Student ID) and set
        that student's Followup Date cell to `date_str` (MM/DD/YYYY).
        on_done({ok, value, error})."""
        self.q.put(("SET_FOLLOWUP_DATE", query, date_str, on_done))

    def submit_set_followup_note(
        self, query: str, note_text: str, on_done: Callable[[dict], None],
    ) -> None:
        """Row-filter the Caseload list to `query` (a Student ID) and set
        that student's Followup Note cell to `note_text`.
        on_done({ok, value, error})."""
        self.q.put(("SET_FOLLOWUP_NOTE", query, note_text, on_done))

    def submit_read_caseload_columns(
        self, on_done: Callable[[list[dict]], None],
    ) -> None:
        """Navigate to Caseload (if not already there) and return the
        list of visible columns + a sniffed type per column. Each
        entry: `{"name": str, "type": "text"|"date"|"number"}`. Used
        by the editor's filter-column dropdown."""
        self.q.put(("READ_CASELOAD_COLUMNS", on_done))

    def submit_read_all_caseload_rows(
        self,
        on_done: Callable[[list[dict]], None],
        on_progress: Optional[Callable[[int], None]] = None,
    ) -> None:
        """Scroll the Caseload table to load every row, then return
        them as a list of dicts keyed by column name. Calls
        `on_progress(row_count)` periodically during the scroll loop."""
        self.q.put(("READ_ALL_CASELOAD_ROWS", on_done, on_progress))

    def submit_click_match_by_filter(
        self,
        query: str,
        on_done: Callable[[bool, dict], None],
        expected_name: str = "",
    ) -> None:
        """Fast batch click: type `query` into Salesforce's row filter,
        wait for the table to narrow, then click the single matching
        row. If `expected_name` is set and the filter returns more
        than one row, only clicks if that name matches one — otherwise
        aborts. ~1.5s per call vs ~25s for the full DOM scan.

        on_done receives `(success, row_info)` where row_info carries
        `student_email` and `pm_email` scraped from the row's
        `mailto:` action link BEFORE the click (and the contact card
        AFTER the click as a fallback). Either field can be empty if
        the page didn't surface it."""
        self.q.put((
            "CLICK_MATCH_BY_FILTER", query, expected_name, on_done,
        ))

    def submit_download_caseload_csv(
        self,
        save_path: Path,
        on_done: Callable[[bool, str], None],
    ) -> None:
        """Drive Salesforce's List View → Export UI and save the
        resulting CSV to `save_path`. `on_done(success, message)` is
        called from the worker thread when the export completes."""
        self.q.put(("DOWNLOAD_CASELOAD_CSV", save_path, on_done))

    def shutdown(self) -> None:
        self.q.put(self.SHUTDOWN)

    def _run(self) -> None:
        try:
            with persistent_context() as ctx:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                if CASELOAD_URL:
                    page.goto(CASELOAD_URL)
                    # TODO: popups stuck at about:blank on fresh launch.
                    # In a Playwright-launched Chromium/Edge, user clicks
                    # that spawn popups (window.open) hang at about:blank
                    # with a "loading…" spinner until *any* Playwright-
                    # driven action runs against the page — e.g. the
                    # first time _handle_find calls goto/click/fill,
                    # popups start working for the rest of the session.
                    # Things we've tried that DON'T fix it:
                    #   - User-initiated Ctrl+R on the parent tab
                    #   - page.bring_to_front()
                    #   - page.reload() right here
                    #   - page.keyboard.press("F19") (synthetic key)
                    #   - --disable-features=CalculateNativeWinOcclusion
                    #     and other throttling-related Chromium flags
                    #   - Monkey-patching window.open in the page
                    #   - Startup "warming" block: wait_for_load_state
                    #     "networkidle" + page.evaluate("document.title")
                    #     + page.locator("body").count() + mouse.move —
                    #     so the unstick trigger isn't generic
                    #     DOM/JS/input activity; it's something specific
                    #     to a user-driven later action.
                    # Workaround documented in README: middle-click or
                    # right-click → "Open link in new tab".

                # Close any tabs left over from a previous session
                # (Edge persists tabs across runs in the user-data
                # dir). Stale tabs frequently start in a bad state
                # — about:blank popup hang, half-navigated — and
                # cause "Target page closed" errors when
                # _active_page picks the wrong one. The session
                # cookies / login state are preserved by the
                # persistent profile; only tab state is reset.
                for extra in list(ctx.pages):
                    if extra is page:
                        continue
                    try:
                        extra.close()
                    except Exception:
                        pass
                # Hook the context-wide request listener so capture
                # mode (when active) sees every page's Salesforce
                # write traffic without us having to wire each page
                # individually.
                try:
                    ctx.on("request", self._on_request)
                except Exception:
                    pass
                self.on_status("Browser ready.")
                self.ready_event.set()
                # Bring the browser to the front so the user can log in to
                # Salesforce; the launcher is foreground at startup, so the
                # raise is honoured. It's minimized again once the startup
                # caseload load finishes (App._minimize_browser).
                try:
                    self._raise_browser_window()
                except Exception:
                    pass
                # NOTE: Mongoose is opened on demand via 🐭 Open Mongoose, NOT
                # at startup. A new_page()+goto() this early still hangs at
                # about:blank — the popup hang only clears after the caseload
                # load runs real Playwright actions later. By the time the user
                # clicks the button, popups work; opening it then is reliable.
                while True:
                    cmd = self.q.get()
                    if cmd is self.SHUTDOWN:
                        return
                    # Outer try/except: any uncaught exception from a
                    # command handler used to kill the entire worker
                    # (next user action would hang forever). Now each
                    # command is sandboxed — the worker logs the
                    # failure and keeps processing future commands.
                    try:
                        self._dispatch_command(ctx, cmd)
                    except Exception as e:
                        self.on_status(
                            f"Command {cmd[0]!r} failed: {e}. "
                            "Worker still running; you may need to "
                            "restart the launcher if the browser is "
                            "in a bad state."
                        )
        except Exception as e:
            self.on_status(f"Browser worker crashed: {e}")

    def _dispatch_command(self, ctx, cmd) -> None:
        """Dispatch one queued command. Each branch is responsible for
        firing any callbacks it owes the caller (in a try/finally) so
        a partial failure doesn't leave the main thread waiting on a
        wait_variable forever."""
        if cmd[0] == "RUN":
            (_, scenario, override, clipboard, custom_bodies,
             prompt_vars, on_done) = cmd[:7]
            ea = cmd[7] if len(cmd) > 7 else None
            success = False
            try:
                success = self._handle_run(
                    ctx, scenario, override, clipboard,
                    custom_bodies=custom_bodies,
                    prompt_vars=prompt_vars, ea=ea,
                )
            finally:
                if on_done is not None:
                    on_done(success)
        elif cmd[0] == "READ_EA":
            _, on_done = cmd
            res = {}
            try:
                res = self._read_essential_actions(ctx)
            finally:
                on_done(res)
        elif cmd[0] == "READ_EA_DASHBOARD":
            _, on_done = cmd
            res = {}
            try:
                res = self._read_ea_dashboard(ctx)
            finally:
                on_done(res)
        elif cmd[0] == "FIND":
            # Back-compat: older callers queued ("FIND", query) with no
            # new_tab / raise_after flags.
            query = cmd[1]
            new_tab = cmd[2] if len(cmd) > 2 else False
            raise_after = cmd[3] if len(cmd) > 3 else None
            self._handle_find(ctx, query, new_tab, raise_after)
        elif cmd[0] == "FIND_AND_SETTLE":
            _, query, on_done = cmd
            ok = False
            try:
                # Fast navigation (Shift+X switch, no reload), kept in the
                # background so it doesn't pop over the fire dialogs.
                self._handle_find(ctx, query, new_tab=False, raise_after=False)
                # Poll until the note panel is actually loaded — exits as
                # soon as get_active_student_name (the same readiness
                # check _handle_run uses) resolves, up to ~5s.
                target = self._active_page(ctx)
                if target is not None:
                    for _ in range(25):
                        try:
                            if get_active_student_name(target):
                                ok = True
                                break
                        except Exception:
                            pass
                        try:
                            target.wait_for_timeout(200)
                        except Exception:
                            break
            finally:
                on_done(ok)
        elif cmd[0] == "LIST_MATCHES":
            _, query, on_results = cmd
            names: list[str] = []
            try:
                names = self._list_matches(ctx, query)
            finally:
                on_results(names)
        elif cmd[0] == "CLICK_MATCH":
            _, name, on_done = cmd
            success = False
            try:
                success = self._click_match_by_name(ctx, name)
                if success:
                    tgt = self._active_page(ctx)
                    if tgt is not None:
                        try:
                            _wait_record_ready(tgt, 2000)
                        except Exception:
                            pass
                        self._bring_browser_forward(tgt)
            finally:
                on_done(success)
        elif cmd[0] == "GET_STUDENT_CONTEXT":
            _, on_done, name_hint = cmd
            info: Optional[dict] = None
            try:
                info = self._read_student_context(ctx, name_hint)
            finally:
                on_done(info)
        elif cmd[0] == "FETCH_NOTES":
            _, query, on_done = cmd
            res = {}
            try:
                res = self._fetch_student_notes(ctx, query)
            finally:
                on_done(res)
        elif cmd[0] == "FETCH_TASK_STATUS":
            _, query, on_done = cmd
            res = {}
            try:
                res = self._fetch_task_status(ctx, query)
            finally:
                on_done(res)
        elif cmd[0] == "SCRAPE_ALL_TASK_STATUS":
            _, on_done = cmd
            res = {}
            try:
                res = self._scrape_all_task_status(ctx)
            finally:
                on_done(res)
        elif cmd[0] == "PROBE_TEXT":
            _, on_done = cmd
            res = {}
            try:
                res = self._probe_text(ctx)
            finally:
                on_done(res)
        elif cmd[0] == "OPEN_MONGOOSE":
            _, on_done = cmd
            res = {}
            try:
                res = self._open_mongoose(ctx)
            finally:
                on_done(res)
        elif cmd[0] == "TEST_TEXT":
            _, mobile, body, on_done = cmd
            res = {}
            try:
                res = self._test_text(ctx, mobile, body)
            finally:
                on_done(res)
        elif cmd[0] == "SEND_TEXT":
            _, payload, on_done = cmd
            res = {}
            try:
                res = self._send_text(ctx, payload)
            finally:
                on_done(res)
        elif cmd[0] == "SET_FOLLOWUP_DATE":
            _, query, date_str, on_done = cmd
            res = {}
            try:
                res = self._set_followup_date(ctx, query, date_str)
            finally:
                on_done(res)
        elif cmd[0] == "SET_FOLLOWUP_NOTE":
            _, query, note_text, on_done = cmd
            res = {}
            try:
                res = self._set_followup_note(ctx, query, note_text)
            finally:
                on_done(res)
        elif cmd[0] == "READ_CASELOAD_COLUMNS":
            _, on_done = cmd
            cols: list[dict] = []
            try:
                cols = self._read_caseload_columns(ctx)
            finally:
                on_done(cols)
        elif cmd[0] == "READ_ALL_CASELOAD_ROWS":
            _, on_done, on_progress = cmd
            rows: list[dict] = []
            try:
                rows = self._read_all_caseload_rows(
                    ctx, on_progress=on_progress,
                )
            finally:
                on_done(rows)
        elif cmd[0] == "CLICK_MATCH_BY_FILTER":
            _, query, expected_name, on_done = cmd
            success = False
            row_info: dict = {"pm_email": "", "student_email": ""}
            try:
                # _click_match_by_filter now owns the post-click
                # settle wait (so its own contact-card scrape runs
                # against a loaded page); the dispatch wrapper no
                # longer adds a redundant second wait.
                success, row_info = self._click_match_by_filter(
                    ctx, query, expected_name=expected_name,
                )
            finally:
                on_done(success, row_info)
        elif cmd[0] == "DOWNLOAD_CASELOAD_CSV":
            _, save_path, on_done = cmd
            success, message = False, ""
            try:
                success, message = self._download_caseload_csv(
                    ctx, save_path,
                )
            finally:
                on_done(success, message)
    def _try_match_or_navigate(self, target, query: str,
                               raise_after: bool = True) -> bool:
        """Look for matches in the current DOM. If exactly one
        highest-priority match, click and return True. If multiple,
        post them to the main thread for a picker and return True
        (handled async). Returns False if nothing matched."""
        matches = gather_caseload_matches(target, query, on_status=self.on_status)
        if not matches:
            return False
        best_priority = matches[0][0]
        top = [m for m in matches if m[0] == best_priority]
        if len(top) == 1:
            priority, row, name, name_idx = top[0]
            self.on_status(f"  [search] match: {name!r} (priority {priority})")
            if click_caseload_row(row, name, name_idx, on_status=self.on_status):
                self.on_status(f"Navigated to {name!r}.")
                # Raise only AFTER the navigation click — see _handle_find.
                # Suppressed for new-tab opens (background, by convention).
                if raise_after:
                    self._bring_browser_forward(target)
                return True
            # Found the row but the click didn't route (Lightning
            # active-view race). Report False so _handle_find falls
            # through to re-activate the list and retry, rather than
            # stopping on a false success.
            self.on_status(
                f"  [search] found {name!r} but clicking it didn't open the "
                "record; re-activating the list to retry."
            )
            return False
        # Multiple matches at the same priority — ask user to pick.
        names = [m[2] for m in top]
        self.on_status(
            f"  [search] {len(names)} matches: {', '.join(names)}"
        )
        self.on_multiple_matches(query, names)
        return True

    def _close_record_subtab(self, target) -> bool:
        """Close the currently-open console workspace subtab (the student
        record) with the Shift+X console shortcut, so the live Caseload
        list underneath becomes active again — far faster than reloading
        it. Returns True only once we're back on the Caseload app page;
        the caller falls back to a full reload otherwise (e.g. focus was
        in a field so the shortcut didn't fire, or another record tab
        was underneath)."""
        try:
            # Right after a row click, focus is on the console (not a text
            # field), so the global Shift+X shortcut fires the tab close.
            target.keyboard.press("Shift+X")
        except Exception as e:
            self.on_status(f"  [debug] close-subtab keypress: {e}")
            return False
        # Poll briefly for the URL to fall back to the Caseload app page.
        try:
            # ~1.5s ceiling before we give up and reload. A successful
            # Shift+X re-activates the list well under a second; waiting
            # longer just delays the reload fallback when there was no
            # closeable record (e.g. focus wasn't on the console).
            for _ in range(10):
                if "Caseload_App_Page" in (target.url or ""):
                    return True
                target.wait_for_timeout(150)
        except Exception:
            pass
        return False

    def _ensure_caseload_list(self, target) -> bool:
        """Navigate `target` to the Caseload list and wait for the list
        table to render. The table must have BOTH a Course Code header
        AND a Name header — the Essential Actions panels match Course
        Code only, so a looser wait would settle on a stale empty table.
        Returns True once the real list is visible."""
        if not CASELOAD_URL:
            return False
        # Lightning sometimes raises "Navigation interrupted" when its own
        # JS triggers a redirect during our goto. The navigation still
        # ultimately succeeds, so we treat the exception as advisory.
        try:
            target.goto(CASELOAD_URL, wait_until="domcontentloaded")
        except Exception as e:
            self.on_status(f"  [debug] goto caseload: {e}")
        try:
            list_table = (
                target.locator("table")
                .filter(has=target.locator('th:has-text("Course Code")'))
                .filter(has=target.locator('th:has-text("Name")'))
            )
            list_table.first.wait_for(state="visible", timeout=20_000)
            return True
        except Exception as e:
            self.on_status(f"Caseload list table didn't load in time: {e}")
            return False

    def _caseload_table_present(self, target) -> bool:
        """Cheap, no-wait check: is the Caseload list table already in the
        DOM right now? Lets _handle_find skip a wasteful full reload when
        the list is plainly present and the student just isn't in the
        ~10 visible rows (the row filter, not a reload, finds those)."""
        try:
            return (
                target.locator("table")
                .filter(has=target.locator('th:has-text("Course Code")'))
                .filter(has=target.locator('th:has-text("Name")'))
                .count() > 0
            )
        except Exception:
            return False

    def _handle_find(self, ctx, query: str, new_tab: bool = False,
                     raise_after: Optional[bool] = None) -> None:
        # `new_tab` is a Salesforce CONSOLE subtab, NOT a browser tab.
        # Clicking a student from the Caseload list already opens it as a
        # new console subtab; the only difference between reuse and
        # new-tab is whether we first close the open record (Shift+X) —
        # see the on_caseload block below. We always drive the SAME
        # browser page (never ctx.new_page(), which would spawn a second
        # Edge tab with its own Caseload).
        target = self._active_page(ctx)
        if target is None:
            self.on_status("No browser pages open.")
            return
        # NOTE: we deliberately do NOT raise the browser here. Raising /
        # bring_to_front() activates the tab and makes Lightning
        # re-render, and clicking a row in that same instant raced the
        # re-render — the click landed before the list was ready and the
        # record didn't switch. Instead we navigate first (below, on the
        # settled page) and raise the window only once navigation
        # succeeds — see _bring_browser_forward() calls.
        self.on_status(f"Searching Caseload for {query!r}...")

        # Lightning routes record navigation only from the ACTIVE view.
        # When a student record is already open the Caseload rows remain
        # in the cached DOM (so a search still "finds" them) but clicking
        # them no longer navigates — the URL stays on the open record.
        # So re-activate the list first whenever we aren't already on it.
        try:
            on_caseload = "Caseload_App_Page" in (target.url or "")
        except Exception:
            on_caseload = False
        reloaded = False
        if not on_caseload:
            if not CASELOAD_URL:
                self.on_status(
                    f"No match for {query!r}; a record is open and "
                    "CASELOAD_URL isn't set, so the Caseload list can't "
                    "be reloaded. Open it manually and try again."
                )
                return
            # Fast path (reuse/double-click only): the open record is a
            # console workspace subtab sitting over a STILL-LIVE Caseload
            # list. Closing it (Shift+X) re-activates that list instantly
            # — far cheaper than a full reload. Falls back to a reload if
            # we don't land back on Caseload (e.g. another student tab was
            # underneath). New-tab opens deliberately keep their existing
            # tabs, so they always reload instead of closing anything.
            closed = (not new_tab) and self._close_record_subtab(target)
            if not closed:
                self.on_status("Reloading Caseload list before search...")
                self._ensure_caseload_list(target)
            reloaded = True

        # First pass: search the (now active) Caseload list. New-tab
        # opens stay in the background (no raise) by convention; an
        # explicit raise_after (e.g. background nav for a row-fire)
        # overrides that default.
        if raise_after is None:
            raise_after = not new_tab
        try:
            if self._try_match_or_navigate(target, query, raise_after):
                return
        except Exception as e:
            self.on_status(f"Search failed: {e}")
            return

        # Miss. If the list table is already in the DOM, the student just
        # isn't in the ~10 rendered rows — reloading won't help (and costs
        # a full goto + render wait), so fall straight through to the row
        # filter, which searches ALL rows server-side. Only reload when the
        # list genuinely isn't present (e.g. URL says Caseload but the
        # table hasn't rendered yet).
        if not reloaded and CASELOAD_URL and not self._caseload_table_present(target):
            self.on_status(
                "Caseload list not in DOM — navigating there to retry...")
            if self._ensure_caseload_list(target):
                try:
                    if self._try_match_or_navigate(target, query, raise_after):
                        return
                except Exception as e:
                    self.on_status(f"Retry search failed: {e}")
                    return

        # Step 3: the Caseload table only renders ~10 rows at a time.
        # If the student isn't in that window, type the query into the
        # 'Search All Rows...' filter to narrow the table to a match.
        self.on_status("Not in visible rows; typing into Caseload's row filter...")
        try:
            filter_input = target.locator(
                'input[placeholder="Search All Rows..."]'
            ).filter(visible=True).first
            if filter_input.count() == 0:
                self.on_status("Couldn't find Caseload's row filter input.")
                return
            # focus() instead of click() — when the user's Caseload
            # view has many columns the search input scrolls off the
            # viewport horizontally; click() refuses (requires viewport
            # visibility), focus() doesn't.
            filter_input.focus()
            filter_input.fill("")
            filter_input.fill(query)
            filter_input.press("Enter")
            # Lightning debounces the filter; give it a moment to update.
            _wait_grid_settled(target, 1500)
        except Exception as e:
            self.on_status(f"Filter step failed: {e}")
            return

        try:
            if not self._try_match_or_navigate(target, query, raise_after):
                self.on_status(
                    f"No match for {query!r} after filtering. "
                    "Try Salesforce global search for students outside your caseload."
                )
        except Exception as e:
            self.on_status(f"Search after filter failed: {e}")

    def _list_matches(self, ctx, query: str) -> list[str]:
        """Multi-pass search: returns matching names without clicking.
        Stores rows on self._last_matches so CLICK_MATCH can resolve.
        Order:
          1. exact on current DOM
          2. reload Caseload, exact
          3. row-filter (Salesforce 'Search All Rows…'), exact with query
          4. row-filter with adjacent-transposition variants ('jsoh' →
             try 'sjoh', 'josh', 'jsho'). Catches the most common
             single-typo case using Salesforce's own search, which can
             see all rows (fuzzy is stuck with the ~10 visible ones).
          5. clear row-filter, fuzzy as a last resort
        """
        target = self._active_page(ctx)
        if target is None:
            self.on_status("No browser pages open.")
            self._last_matches = []
            return []
        self.on_status(f"Find: searching Caseload for {query!r}...")

        matches = gather_caseload_matches(target, query, on_status=self.on_status)
        if matches:
            self._last_matches = matches
            return [m[2] for m in matches]

        filter_input = None  # set in step 2 so step 4 can clear it
        if CASELOAD_URL:
            self.on_status("Caseload not in DOM — reloading and retrying...")
            try:
                target.goto(CASELOAD_URL, wait_until="domcontentloaded")
            except Exception as e:
                self.on_status(f"  [debug] goto note: {e}")
            try:
                list_table = (
                    target.locator("table")
                    .filter(has=target.locator('th:has-text("Course Code")'))
                    .filter(has=target.locator('th:has-text("Name")'))
                )
                list_table.first.wait_for(state="visible", timeout=20_000)
            except Exception as e:
                self.on_status(f"Caseload table didn't load: {e}")
                self._last_matches = []
                return []
            matches = gather_caseload_matches(target, query, on_status=self.on_status)
            if matches:
                self._last_matches = matches
                return [m[2] for m in matches]

            self.on_status("Not in visible rows; using Caseload's row filter...")
            try:
                filter_input = target.locator(
                    'input[placeholder="Search All Rows..."]'
                ).filter(visible=True).first
                if filter_input.count() == 0:
                    filter_input = None
            except Exception as e:
                self.on_status(f"Filter lookup failed: {e}")
                filter_input = None

            def _try_filter(text: str) -> list[tuple]:
                """Fill the row filter and gather exact matches against
                `text`. Returns the raw match tuples (empty on any
                failure or zero results)."""
                if filter_input is None:
                    return []
                try:
                    filter_input.focus()
                    filter_input.fill("")
                    filter_input.fill(text)
                    filter_input.press("Enter")
                    _wait_grid_settled(target, 1500)
                except Exception as e:
                    self.on_status(f"  [debug] filter {text!r}: {e}")
                    return []
                return gather_caseload_matches(
                    target, text, on_status=self.on_status,
                )

            # Step 3: original query.
            matches = _try_filter(query)
            if matches:
                self._last_matches = matches
                return [m[2] for m in matches]

            # Step 4: adjacent-transposition typo variants.
            if len(query) >= 3 and filter_input is not None:
                for variant in _typo_variants(query):
                    self.on_status(f"Trying typo correction {variant!r}...")
                    matches = _try_filter(variant)
                    if matches:
                        self.on_status(
                            f"Found via typo-correction {variant!r} "
                            f"(you typed {query!r})."
                        )
                        self._last_matches = matches
                        return [m[2] for m in matches]

        # Step 5: clear the row filter (if we set it) so fuzzy sees the
        # full caseload again, then fuzzy-match.
        if filter_input is not None:
            try:
                self.on_status("Clearing row filter for fuzzy search...")
                filter_input.focus()
                filter_input.fill("")
                filter_input.press("Enter")
                _wait_grid_settled(target, 1500)
            except Exception as e:
                self.on_status(f"  [debug] clear filter: {e}")

        self.on_status(f"No exact match for {query!r}; trying fuzzy...")
        fuzzy = gather_fuzzy_caseload_matches(
            target, query, on_status=self.on_status,
        )
        self._last_matches = fuzzy
        return [m[2] for m in fuzzy]

    def _click_match_by_name(self, ctx, name: str) -> bool:
        target = self._active_page(ctx)
        if target is None:
            self.on_status("No browser pages open.")
            return False
        for m in self._last_matches:
            if m[2] == name:
                _, row, mname, name_idx = m
                if click_caseload_row(row, mname, name_idx, on_status=self.on_status):
                    self.on_status(f"Navigated to {mname!r}.")
                    return True
                self.on_status(f"Click on {mname!r} failed.")
                return False
        self.on_status(f"Click failed: {name!r} not in last results.")
        return False

    def _click_match_by_filter(
        self, ctx, query: str, expected_name: str = "",
    ) -> tuple[bool, dict]:
        """Skip the slow full-table DOM scan: type `query` into
        Salesforce's row filter, wait, then click the (one) result.
        For batches with known-unique Student IDs this is ~10x faster
        than _list_matches + _click_match_by_name.

        Returns (success, row_info). When the click lands cleanly,
        row_info is `{"pm_email": …, "student_email": …}` extracted
        from the row's `mailto:` action link BEFORE the click (so
        we can populate the email step in batch mode without the
        user having to add email columns to their Caseload view in
        Salesforce). After the click we additionally try the contact
        card via `_extract_wgu_email` as a second source for the
        student address. Any field we couldn't read comes back as
        an empty string."""
        row_info: dict = {"pm_email": "", "student_email": ""}
        target, table = self._open_caseload_table(ctx)
        if table is None:
            return False, row_info
        self.on_status(f"Fast-find: filtering Caseload by {query!r}...")
        try:
            filter_input = target.locator(
                'input[placeholder="Search All Rows..."]'
            ).filter(visible=True).first
            if filter_input.count() == 0:
                self.on_status("No row filter input; can't fast-find.")
                return False, row_info
            # focus() instead of click() — when the user's Caseload
            # view has many columns the search input scrolls off the
            # viewport horizontally; click() refuses (requires viewport
            # visibility), focus() doesn't.
            filter_input.focus()
            filter_input.fill("")
            filter_input.fill(query)
            filter_input.press("Enter")
            _wait_grid_settled(target, 800)
        except Exception as e:
            self.on_status(f"Fast-find filter failed for {query!r}: {e}")
            return False, row_info

        # Resolve Name column index on the filtered table.
        headers_raw = table.locator("th").all_text_contents()
        name_idx = next(
            (j for j, h in enumerate(headers_raw) if h.strip() == "Name"),
            None,
        )
        if name_idx is None:
            name_idx = next(
                (
                    j for j, h in enumerate(headers_raw)
                    if h.strip().startswith("Name")
                ),
                None,
            )
        if name_idx is None:
            self.on_status("Fast-find: no Name column found.")
            return False, row_info

        # Collect matching rows from the filtered table.
        rows_loc = table.locator("tr")
        n_rows = rows_loc.count()
        candidates: list[tuple] = []
        for r in range(1, n_rows):
            row = rows_loc.nth(r)
            try:
                cells = row.locator("td").all_text_contents()
            except Exception:
                continue
            if not cells or name_idx >= len(cells):
                continue
            cname = cells[name_idx].strip()
            if cname:
                candidates.append((row, cname, name_idx))

        if not candidates:
            self.on_status(f"Fast-find: {query!r} returned 0 rows.")
            return False, row_info

        # Disambiguate via expected_name (the row we matched in main).
        if expected_name:
            chosen = next(
                (c for c in candidates if c[1] == expected_name), None,
            )
            if chosen is None:
                self.on_status(
                    f"Fast-find {query!r}: {len(candidates)} rows but "
                    f"none named {expected_name!r}; skipping."
                )
                return False, row_info
        elif len(candidates) > 1:
            names = ", ".join(c[1] for c in candidates)
            self.on_status(
                f"Fast-find {query!r}: {len(candidates)} ambiguous rows "
                f"({names}); skipping (no expected_name to disambiguate)."
            )
            return False, row_info
        else:
            chosen = candidates[0]

        row, cname, name_idx = chosen

        # BEFORE clicking: scrape the row's "Email Student" mailto
        # link. Historically Salesforce puts the PM as primary and
        # the student as CC in that link, so we can capture both for
        # free here — much more reliable than re-scraping after we've
        # navigated away from Caseload. Best-effort: failure here
        # doesn't block the click; the batch can still proceed with
        # whatever the CSV row provided.
        try:
            mailtos = row.locator('a[href^="mailto:"]')
            if mailtos.count() > 0:
                href = mailtos.first.get_attribute("href") or ""
                primary, cc = _parse_mailto(href)
                if "@" in primary:
                    row_info["pm_email"] = primary
                if "@" in cc:
                    row_info["student_email"] = cc
        except Exception:
            pass

        # Quick diagnostic so the next layer (the batch loop) can
        # see whether the mailto step succeeded vs. silently went
        # empty. Only emit once per session — chatty otherwise.
        if not self._mailto_diag_logged:
            self._mailto_diag_logged = True
            if row_info["pm_email"] or row_info["student_email"]:
                self.on_status(
                    f"Row mailto: pm_email={row_info['pm_email']!r}, "
                    f"student_email={row_info['student_email']!r}"
                )
            else:
                self.on_status(
                    "Row had no mailto: link — Caseload view may be "
                    "missing the 'Email Student' action column. Will "
                    "try the contact card after navigation."
                )

        if not click_caseload_row(row, cname, name_idx, on_status=self.on_status):
            self.on_status(f"Fast-find: click on {cname!r} failed.")
            return False, row_info
        self.on_status(f"Fast-find navigated to {cname!r}.")

        # Wait for the destination page to settle BEFORE scraping the
        # contact card. (The dispatch wrapper used to do this 2s
        # wait, but that ran AFTER this function returned — so the
        # earlier post-click scrape was racing an unloaded page and
        # always coming back empty.)
        post_click_target = self._active_page(ctx)
        if post_click_target is not None:
            try:
                # Ready as soon as the record resolves; small extra settle
                # so the contact card (email field) is painted before scrape.
                _wait_record_ready(post_click_target, 2000)
                post_click_target.wait_for_timeout(250)
            except Exception:
                pass

        # AFTER click + settle: try the contact card on the
        # student's record page. Sweeps several common Salesforce
        # email-field labels + a generic mailto fallback so this
        # works regardless of whether the org calls the field
        # "WGU Email", "Personal Email", "Student Email", etc.
        if post_click_target is not None and not row_info["student_email"]:
            try:
                found = scrape_student_email_from_page(
                    post_click_target,
                    pm_email=row_info.get("pm_email", ""),
                )
                if found:
                    row_info["student_email"] = found
                    if not self._contact_card_diag_logged:
                        self._contact_card_diag_logged = True
                        self.on_status(
                            f"Contact-card scrape found student email: {found}"
                        )
                elif not self._contact_card_diag_logged:
                    self._contact_card_diag_logged = True
                    self.on_status(
                        "Contact-card scrape found no student email "
                        "on the record page. The contact's email field "
                        "may use an unusual label or be hidden — paste "
                        "this log to the launcher dev along with the "
                        "label text shown on the student's record."
                    )
            except Exception:
                pass

        return True, row_info

    def _read_student_context(self, ctx, name_hint: str = "") -> Optional[dict]:
        """Build the variable dict used to render emails and notes.
        `name_hint` lets the caller supply the name we just navigated
        to (e.g. from find-first) when the note panel hasn't been
        opened yet so get_active_student_name() would return ''."""
        target = self._active_page(ctx)
        if target is None:
            return None
        name = name_hint or (get_active_student_name(target) or "")
        info = lookup_caseload_student(target, name) if name else {}
        first, _, last = name.partition(" ")
        return {
            "full_name": _capitalize_name(name),
            "first_name": _capitalize_name(first),
            # Preferred name from the caseload row, else the first name.
            "preferred_name": _capitalize_name(
                info.get("preferred_name", "") or first),
            "last_name": _capitalize_name(last),
            "student_email": info.get("student_email", ""),
            "student_id": info.get("student_id", ""),
            "course_code": info.get("course_code", ""),
            "pm_name": _capitalize_name(info.get("pm_name", "")),
            "pm_email": info.get("pm_email", ""),
        }

    # ----- Network capture (for REST-API discovery) -----

    def start_request_capture(self) -> None:
        """Begin recording Salesforce-bound write requests. Call once
        per discovery session; subsequent fires by the user populate
        `_capture_log`."""
        self._capture_active = True
        self._capture_log = []

    def stop_request_capture(self) -> list[dict]:
        """Stop recording and return the accumulated log. Safe to call
        even if capture wasn't running."""
        self._capture_active = False
        return list(self._capture_log)

    def _on_request(self, request) -> None:
        """Context-level request listener. Only records when capture
        mode is active AND the request looks like a Salesforce data
        write (POST / PATCH / PUT against a Salesforce host). Filters
        out auth + asset traffic so the user doesn't drown in noise."""
        if not self._capture_active:
            return
        try:
            url = request.url or ""
            method = request.method or ""
        except Exception:
            return
        if method.upper() not in ("POST", "PATCH", "PUT"):
            return
        if not any(d in url for d in (
            "salesforce.com", "force.com", "lightning.com",
        )):
            return
        # Skip token/auth/session refresh chatter.
        if any(skip in url for skip in (
            "/auth/", "/oauth", "/token", "/session",
            "/aura?aura.token", "/visualforce/session",
        )):
            return
        try:
            self._capture_log.append({
                "url": url,
                "method": method,
                "headers": dict(request.headers),
                "post_data": request.post_data,
            })
        except Exception:
            pass

    @staticmethod
    def _active_page(ctx):
        """Return the best responsive page in `ctx`, or None.

        Prefers the real Salesforce page — the Caseload list first, then
        any Lightning page — over transient tabs. WGU's CSV "Download"
        spawns a short-lived export tab that downloads then closes itself;
        right after the startup auto-refresh that tab is the MOST RECENT
        page, so a naive newest-first pick would grab it. It can even pass
        the responsiveness probe at selection time and then die the moment
        the caller runs a real query ("Target page... has been closed"),
        which is exactly what made the first post-startup search fail.

        Defensive against:
         - stale closed pages (e.g. download-capture tabs Playwright
           hasn't yet cleaned out of ctx.pages),
         - pages where is_closed() returns False but the underlying
           target is mid-teardown,
         - zombie pages that pass both is_closed() AND .url access
           but raise "Target page closed" the moment a locator query
           runs. We do a cheap `locator("html").count()` probe to
           filter these — same kind of operation that subsequent
           callers will run anyway."""
        caseload = lightning = fallback = None
        for page in reversed(ctx.pages):
            try:
                if page.is_closed():
                    continue
                url = page.url or ""
                _ = page.locator("html").count()  # responsive probe
            except Exception:
                continue
            if fallback is None:
                fallback = page  # most-recent responsive page (last resort)
            if "Caseload_App_Page" in url and caseload is None:
                caseload = page
            elif "lightning.force.com" in url and lightning is None:
                lightning = page
        # Caseload list > any Lightning page (e.g. an open record) >
        # whatever responsive page we have (covers about:blank-only states).
        return caseload or lightning or fallback

    def _descendant_pids(self) -> set:
        """PIDs of every process descended from this Python process,
        via a Toolhelp32 snapshot (no third-party deps). Used to tell
        OUR browser (a child of the launcher) apart from any everyday
        Edge/Vivaldi the user has open."""
        import ctypes
        from ctypes import wintypes
        TH32CS_SNAPPROCESS = 0x00000002

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260),
            ]

        kernel32 = ctypes.windll.kernel32
        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap == -1 or snap == 0:
            return set()
        children: dict = {}
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                children.setdefault(entry.th32ParentProcessID, []).append(
                    entry.th32ProcessID)
                ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snap)
        # BFS down from us.
        out: set = set()
        stack = [os.getpid()]
        while stack:
            pid = stack.pop()
            for child in children.get(pid, ()):
                if child not in out:
                    out.add(child)
                    stack.append(child)
        return out

    def _raise_browser_window(self, title_hint: str = "") -> None:
        """Pull the launcher's browser window to the OS foreground.
        `page.bring_to_front()` only activates the tab *within* the
        browser; on Windows it does NOT raise the browser window above
        other apps, so a navigation fired while the launcher has focus
        lands on a window hidden behind the app and looks like nothing
        happened.

        We match the Chromium/Edge top-level window whose owning process
        descends from this launcher (so we never grab the user's
        everyday Edge/Vivaldi), falling back to a page-title match.
        No-op off Windows."""
        if sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes
        except Exception:
            return
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        # Fast path: the browser window is stable for the session, so
        # reuse the handle we found last time and skip the (relatively
        # expensive) process snapshot + window enumeration. Only re-scan
        # if the cached handle is gone (browser relaunched/closed).
        cached = getattr(self, "_browser_hwnd", None)
        if cached and user32.IsWindow(cached) and user32.IsWindowVisible(cached):
            self._raise_hwnd(cached)
            return

        try:
            ours = self._descendant_pids()
        except Exception:
            ours = set()

        # (hwnd, pid, title) for every visible, titled Chromium window.
        cands: list = []
        WNDENUMPROC = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def _cb(hwnd, lparam):
            try:
                if not user32.IsWindowVisible(hwnd):
                    return True
                cls = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, cls, 256)
                if cls.value != "Chrome_WidgetWin_1":
                    return True
                n = user32.GetWindowTextLengthW(hwnd)
                if n <= 0:
                    return True  # toolbars / hidden helpers have no title
                tbuf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, tbuf, n + 1)
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                cands.append((hwnd, pid.value, tbuf.value))
            except Exception:
                pass
            return True

        try:
            user32.EnumWindows(WNDENUMPROC(_cb), 0)
        except Exception:
            return

        # Pick: descendant of us > title matches the page > sole candidate.
        chosen = None  # (hwnd, pid)
        mine = [c for c in cands if c[1] in ours]
        if mine:
            chosen = (mine[0][0], mine[0][1])
        elif title_hint:
            h = title_hint.lower()
            for c in cands:
                if c[2] and h in c[2].lower():
                    chosen = (c[0], c[1])
                    break
        if chosen is None and len(cands) == 1:
            chosen = (cands[0][0], cands[0][1])
        if chosen is None:
            # Only worth a log line when we couldn't find our window —
            # a successful raise is self-evident (the window appears).
            self.on_status(
                f"  [raise] couldn't locate browser window "
                f"({len(cands)} chromium window(s) seen)"
            )
            return
        # Cache hwnd + owning pid for the fast path and the focus guard.
        self._browser_hwnd, self._browser_pid = chosen
        self._raise_hwnd(chosen[0])

    def _locate_browser_hwnd(self):
        """Return the launcher's browser top-level HWND (cached, else
        enumerated), WITHOUT raising/focusing it. None if not found.
        Windows only."""
        if sys.platform != "win32":
            return None
        try:
            import ctypes
            from ctypes import wintypes
        except Exception:
            return None
        user32 = ctypes.windll.user32
        cached = getattr(self, "_browser_hwnd", None)
        if cached and user32.IsWindow(cached):
            return cached
        try:
            ours = self._descendant_pids()
        except Exception:
            ours = set()
        cands: list = []
        WNDENUMPROC = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def _cb(hwnd, lparam):
            try:
                if not user32.IsWindowVisible(hwnd):
                    return True
                cls = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, cls, 256)
                if cls.value != "Chrome_WidgetWin_1":
                    return True
                if user32.GetWindowTextLengthW(hwnd) <= 0:
                    return True
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                cands.append((hwnd, pid.value))
            except Exception:
                pass
            return True

        try:
            user32.EnumWindows(WNDENUMPROC(_cb), 0)
        except Exception:
            return None
        mine = [c for c in cands if c[1] in ours]
        chosen = mine[0] if mine else (cands[0] if len(cands) == 1 else None)
        if chosen is None:
            return None
        self._browser_hwnd, self._browser_pid = chosen
        return chosen[0]

    def set_browser_enabled(self, enabled: bool) -> None:
        """Enable/disable OS input to the launcher's browser window so the
        user can't click/scroll/type into it mid-automation — doing so
        changes the active console record and breaks a running fire
        ('No visible note panel'). Playwright drives the page over CDP,
        not OS input, so automation keeps working while the window is
        disabled. Safe to call from any thread; Windows-only no-op
        otherwise."""
        if sys.platform != "win32":
            return
        try:
            import ctypes
            hwnd = self._locate_browser_hwnd()
            if hwnd:
                ctypes.windll.user32.EnableWindow(hwnd, bool(enabled))
        except Exception:
            pass

    def _user_is_on_us(self) -> bool:
        """True if the current OS foreground window belongs to the
        launcher or to our own browser. When it belongs to some OTHER
        app (the user has moved on while a record loads), we must NOT
        raise the browser — doing so steals focus / grabs the cursor
        from whatever they're now using. Defaults to True off Windows."""
        if sys.platform != "win32":
            return True
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            fg = user32.GetForegroundWindow()
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(fg, ctypes.byref(pid))
            allowed = {os.getpid()}
            bp = getattr(self, "_browser_pid", None)
            if bp:
                allowed.add(bp)
            return pid.value in allowed
        except Exception:
            return True

    def _bring_browser_forward(self, page) -> None:
        """Activate the right tab and pull the browser window to the OS
        foreground. Call this AFTER a navigation has completed — calling
        it before clicking races Lightning's re-render on tab activation
        and the click misses. Skipped entirely when the user has focused
        a different app, so we never yank focus away from their work."""
        if not self._user_is_on_us():
            return
        try:
            if page is not None:
                page.bring_to_front()
        except Exception:
            pass
        self._raise_browser_window()

    def _raise_hwnd(self, hwnd) -> None:
        """Bring a known top-level window to the OS foreground. We rely
        on the fact that the launcher is the foreground process when this
        runs (guarded by _user_is_on_us), so a plain SetForegroundWindow
        is honoured. We deliberately do NOT use AttachThreadInput to
        force it past the foreground lock — that defeats Windows' own
        focus-steal protection and was grabbing the cursor from other
        apps."""
        try:
            import ctypes
            user32 = ctypes.windll.user32
            # Only un-minimize when actually minimized. SW_RESTORE on a
            # snapped (Win+Arrow half-max) or maximized window reverts it
            # to its pre-snap "normal" rectangle — which Windows never
            # updated to the snap location, so the window jumps to a
            # stale, possibly off-screen spot. A snapped window reports
            # as normal (not iconic), so skipping restore leaves the
            # half-max layout untouched.
            if user32.IsIconic(hwnd):
                SW_RESTORE = 9
                user32.ShowWindow(hwnd, SW_RESTORE)
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        except Exception:
            pass

    def _minimize_browser_window(self) -> None:
        """Minimize the launcher's browser window — called once the startup
        caseload load is done so it's out of the user's way. No-op off
        Windows / if the window can't be located."""
        if sys.platform != "win32":
            return
        try:
            import ctypes
            user32 = ctypes.windll.user32
            hwnd = self._locate_browser_hwnd()
            if hwnd and user32.IsWindow(hwnd):
                SW_MINIMIZE = 6
                user32.ShowWindow(hwnd, SW_MINIMIZE)
        except Exception:
            pass

    def _open_caseload_table(self, ctx):
        """Common helper: navigate to Caseload (if not already there)
        and return a locator for the data table.

        Retries on transient failures (page closed mid-goto, target
        died between active_page check and call). If no live page is
        available at all, creates a fresh one rather than giving up
        — keeps the launcher usable even after browser hiccups."""
        last_error = ""
        for attempt in range(3):
            target = self._active_page(ctx)
            if target is None:
                try:
                    target = ctx.new_page()
                except Exception as e:
                    last_error = f"new_page failed: {e}"
                    continue
            try:
                current_url = target.url or ""
            except Exception:
                current_url = ""
            if CASELOAD_URL and "Caseload_App_Page" not in current_url:
                try:
                    target.goto(CASELOAD_URL, wait_until="domcontentloaded")
                except Exception as e:
                    self.on_status(
                        f"  [debug] goto caseload (attempt {attempt + 1}): {e}"
                    )
                    last_error = str(e)
                    continue
            try:
                tables = (
                    target.locator("table")
                    .filter(has=target.locator('th:has-text("Course Code")'))
                    .filter(has=target.locator('th:has-text("Name")'))
                )
                tables.first.wait_for(state="visible", timeout=20_000)
                return target, tables.first
            except Exception as e:
                last_error = str(e)
                continue
        self.on_status(f"Caseload table didn't load after retries: {last_error}")
        if target is not None and self._looks_like_login(target):
            self.on_status(
                "⚠ Salesforce is asking you to sign in. Opening the browser "
                "— please log in, then retry.")
            try:
                self._raise_browser_window()
            except Exception:
                pass
        return None, None

    def _looks_like_login(self, target) -> bool:
        """True if the active page looks like a Salesforce sign-in page — by
        URL (login host / *.salesforce.com/login) or a visible username +
        password field pair. Used to surface 'please sign in' in the log
        (the browser is usually minimized, so a silent failure is confusing)."""
        try:
            url = (target.url or "").lower()
        except Exception:
            url = ""
        if any(s in url for s in (
                "login.salesforce.com", "/login", "/_ui/login", "secur/login")):
            return True
        try:
            u = target.locator('input[name="username"], input#username')
            p = target.locator('input[type="password"]')
            if u.count() > 0 and p.count() > 0:
                return True
        except Exception:
            pass
        return False

    def _fetch_student_notes(
        self, ctx, query: str, max_notes: int = 60,
    ) -> dict:
        """Open the student's record and scrape their note history.

        The notes live in a ShortText datatable that lazy-loads AFTER the
        record opens, so we poll the *visible* ShortText cells until they
        appear (the global caseload Notes-History table is in the DOM too
        but stays hidden when a record subtab is foreground, so `:visible`
        isolates this student's notes). Returns
        {notes:[{type,subject,course,date,author,text}], count, timings}.
        """
        import time as _t
        timings: dict = {}
        t0 = _t.time()
        # 1. Navigate to the student's record (foregrounds the subtab so
        #    the hidden global table drops out of :visible).
        try:
            self._handle_find(ctx, query, new_tab=False, raise_after=False)
        except Exception as e:
            return {"error": f"navigation failed: {e}", "timings": timings}
        timings["nav_ms"] = int((_t.time() - t0) * 1000)
        target = self._active_page(ctx)
        if target is None:
            return {"error": "no active page", "timings": timings}

        try:
            timings["url"] = (target.url or "")[:90]
        except Exception:
            timings["url"] = ""
        # 2. The record opens on the "Essential Actions" tab; the per-student
        #    note table lives in the *Notes History* scoped tab, which
        #    Lightning doesn't render until it's activated. Click it.
        t_tab = _t.time()
        timings["tab"] = "not found"
        for _ in range(20):  # wait up to ~5s for the tab to exist
            try:
                tab = target.locator(
                    'a[data-tab-value="NotesHistoryTab"]').first
                if tab.count() > 0:
                    tab.click(timeout=2000)
                    timings["tab"] = "clicked"
                    break
            except Exception as e:
                timings["tab"] = f"click error: {e}"
            try:
                target.wait_for_timeout(250)
            except Exception:
                break
        timings["tab_ms"] = int((_t.time() - t_tab) * 1000)

        sel = 'td[data-col-key-value*="ShortText__c"]:visible'
        sel_all = 'td[data-col-key-value*="ShortText__c"]'
        # 3. Poll until the per-student notes table populates inside the tab.
        t1 = _t.time()
        count = 0
        for _ in range(40):  # ~10s ceiling (250ms * 40)
            try:
                count = target.locator(sel).count()
            except Exception:
                count = 0
            if count > 0:
                break
            try:
                target.wait_for_timeout(250)
            except Exception:
                break
        timings["notes_load_ms"] = int((_t.time() - t1) * 1000)
        try:
            timings["all_cells"] = target.locator(sel_all).count()
        except Exception:
            timings["all_cells"] = -1

        # 3. Scrape every visible note row in ONE page.evaluate() round-trip
        #    (vs. ~1600 per-cell locator calls — cuts scrape from ~5s to
        #    ~50ms). Column keys from the live DOM: Type__c / CourseCode__c /
        #    Name(=Subject) / ShortText__c / Author__c / WGUCreationDateTime__c.
        #    Full body lives in each cell's data-cell-value attr.
        t2 = _t.time()
        notes: list[dict] = []
        # IMPORTANT: scrape via locator.evaluate_all, NOT page.evaluate.
        # The Notes History datatable is a Lightning Web Component whose
        # cells live inside shadow roots. Playwright's selector engine
        # pierces shadow DOM (so the locator resolves all 270 cells), then
        # hands those elements to the JS; a plain document.querySelectorAll
        # in page.evaluate does NOT pierce shadow DOM and returns nothing.
        js = """
        (cells, maxNotes) => {
          const norm = (s) => (s || "").replace(/\\s+/g, " ").trim();
          const out = [];
          const seen = new Set();
          for (const cell of cells) {
            if (out.length >= maxNotes) break;
            const row = cell.closest('tr');
            if (!row) continue;
            const pick = (key, prefix) => {
              const sel = prefix
                ? 'td[data-col-key-value^="' + key + '"]'
                : 'td[data-col-key-value*="' + key + '"]';
              const td = row.querySelector(sel);
              if (!td) return "";
              return norm(td.getAttribute("data-cell-value") || td.textContent);
            };
            // The Record ID column renders an anchor to the note record,
            // where the full Text lives. Grab its href.
            let url = "";
            const a = row.querySelector('a[href]');
            if (a) url = a.href;
            const rec = {
              type: pick("Type__c", false),
              course: pick("CourseCode__c", false),
              subject: pick("Name-", true),
              text: pick("ShortText__c", false),
              author: pick("Author__c", false),
              date: pick("WGUCreationDateTime__c", false),
              url: url,
            };
            // De-dup if the same table appears twice in the DOM.
            const k = rec.date + "|" + rec.subject + "|" + rec.text.slice(0, 40);
            if (seen.has(k)) continue;
            seen.add(k);
            out.push(rec);
          }
          return out;
        }
        """
        try:
            notes = target.locator(sel_all).evaluate_all(js, max_notes) or []
        except Exception as e:
            timings["scrape_error"] = str(e)
        timings["scrape_ms"] = int((_t.time() - t2) * 1000)
        timings["total_ms"] = int((_t.time() - t0) * 1000)
        return {"notes": notes, "count": count, "timings": timings}

    @staticmethod
    def _clean_caseload_headers(table) -> list[str]:
        """Strip the 'sorting options' / 'column actions' UI noise off
        Lightning's <th> text and dedupe, returning a clean list of
        column names in left-to-right order."""
        import re as _re
        raw = table.locator("th").all_text_contents()
        out: list[str] = []
        for h in raw:
            h = h.strip()
            if not h or h.startswith("Sort by:"):
                continue
            h = _re.sub(
                r"\s*(sorting options|column actions).*$",
                "", h, flags=_re.IGNORECASE,
            ).strip()
            if h and h not in out:
                out.append(h)
        return out

    def _read_caseload_columns(self, ctx) -> list[dict]:
        """Return list of `{name, type}` dicts for every visible column
        in the user's caseload list view. Type is sniffed from a sample
        of cells in each column."""
        target, table = self._open_caseload_table(ctx)
        if table is None:
            return []
        headers = self._clean_caseload_headers(table)
        if not headers:
            return []
        rows = table.locator("tr")
        n_rows = min(rows.count(), 11)  # 1 header + up to 10 data rows
        samples: list[list[str]] = [[] for _ in headers]
        for r in range(1, n_rows):
            try:
                cells = rows.nth(r).locator("td").all_text_contents()
            except Exception:
                continue
            for i in range(len(headers)):
                if i < len(cells):
                    v = cells[i].strip()
                    if v:
                        samples[i].append(v)
        self.on_status(f"Caseload columns refreshed: {len(headers)} visible.")
        return [
            {"name": h, "type": caseload_filter.sniff_column_type(s)}
            for h, s in zip(headers, samples)
        ]

    def _download_caseload_csv(
        self, ctx, save_path: Path,
    ) -> tuple[bool, str]:
        """Click WGU's custom Download button on the Caseload list
        view and save the resulting CSV to `save_path`. Returns
        (success, message).

        The Download button (`title="Download"`) lives directly to
        the left of the Mass-email button in the list view toolbar
        — confirmed unique via the saved Caseload.html snapshot.
        Playwright's `expect_download` context catches the file
        before Edge dumps it into its temp artifacts folder.

        IMPORTANT: clears Salesforce's row filter before clicking
        Download. The Export honors the current filter, so a leftover
        value (from a prior fast-find) would emit a single-row CSV
        and silently corrupt the cache."""
        target, table = self._open_caseload_table(ctx)
        if table is None:
            return False, "caseload table didn't load"

        # Clear any leftover row filter so the export covers the
        # whole caseload, not whatever a previous fast-find narrowed
        # the view to.
        try:
            filter_input = target.locator(
                'input[placeholder="Search All Rows..."]'
            ).filter(visible=True).first
            if filter_input.count() > 0:
                filter_input.focus()
                filter_input.fill("")
                filter_input.press("Enter")
                _wait_grid_settled(target, 800)
        except Exception as e:
            self.on_status(f"  [export] couldn't clear row filter: {e}")

        try:
            btn = target.locator('button[title="Download"]').first
            btn.wait_for(state="visible", timeout=10_000)
        except Exception as e:
            return False, f"Download button not found: {e}"

        try:
            with target.expect_download(timeout=30_000) as dl_info:
                btn.click()
                self.on_status("  [export] clicked Download button")
            download = dl_info.value
        except Exception as e:
            return False, f"download did not start: {e}"

        try:
            sp = Path(save_path)
            sp.parent.mkdir(parents=True, exist_ok=True)
            tmp = sp.with_name(sp.name + ".new")
            download.save_as(str(tmp))
            # Anti-clobber guard: if the new export has FEWER columns than
            # the existing cache (e.g. the browser view lost columns), keep
            # the previous file as .bak and warn — silent data loss here is
            # how a wrong view quietly breaks viewer features.
            dropped: list[str] = []
            if sp.exists():
                try:
                    old_h = caseload_csv.csv_header(sp)
                    new_h = caseload_csv.csv_header(tmp)
                    dropped = [c for c in old_h if c and c not in new_h]
                except Exception:
                    dropped = []
                try:
                    sp.replace(sp.with_name(sp.name + ".bak"))
                except Exception:
                    pass
            tmp.replace(sp)
            if dropped:
                self.on_status(
                    "  [export] WARNING: new download dropped "
                    f"{len(dropped)} column(s) vs the previous CSV "
                    f"({', '.join(dropped[:6])}"
                    f"{'…' if len(dropped) > 6 else ''}). Previous saved as "
                    f"{sp.name}.bak — check your Caseload view if unintended.")
        except Exception as e:
            return False, f"download save failed: {e}"

        # WGU's Download action occasionally drifts the page off the
        # Caseload list URL. If it did, re-activate the live list now —
        # this runs during the (backgrounded) startup auto-refresh, so the
        # user's FIRST student search starts from the fast path
        # (on_caseload True) instead of paying a wasted Shift+X poll + full
        # reload. No-op when the export already ended on Caseload, so it
        # costs nothing in the common case.
        try:
            if "Caseload_App_Page" not in (target.url or ""):
                self._ensure_caseload_list(target)
        except Exception:
            pass
        return True, f"saved to {Path(save_path).name}"

    def _read_all_caseload_rows(
        self, ctx,
        on_progress: Optional[Callable[[int], None]] = None,
    ) -> list[dict]:
        """Scroll the caseload table to load every row, then return
        them as a list of `{column_name: cell_text}` dicts. Lightning
        lazy-loads rows on scroll; we drive the last `<tr>` into view
        in a loop until the row count is stable for two checks.
        Skips rows that are entirely empty (placeholder shells)."""
        target, table = self._open_caseload_table(ctx)
        if table is None:
            return []
        # Clear any active row filter so we see the whole caseload.
        try:
            fi = target.locator(
                'input[placeholder="Search All Rows..."]'
            ).filter(visible=True).first
            if fi.count() > 0:
                fi.click(); fi.fill("")
                fi.press("Enter")
                _wait_grid_settled(target, 1500)
        except Exception:
            pass

        last_count = 0
        stable = 0
        MAX_ITERS = 200
        for _ in range(MAX_ITERS):
            rows = table.locator("tr")
            count = rows.count()
            if on_progress:
                try:
                    on_progress(max(count - 1, 0))  # subtract header
                except Exception:
                    pass
            if count == last_count:
                stable += 1
                if stable >= 2:
                    break
            else:
                stable = 0
            last_count = count
            try:
                last_row = rows.nth(count - 1)
                last_row.scroll_into_view_if_needed(timeout=2000)
            except Exception:
                pass
            try:
                target.wait_for_timeout(400)
            except Exception:
                pass

        headers = self._clean_caseload_headers(table)
        if not headers:
            return []
        rows = table.locator("tr")
        n_rows = rows.count()
        out: list[dict] = []
        for r in range(1, n_rows):
            try:
                cells = rows.nth(r).locator("td").all_text_contents()
            except Exception:
                continue
            row_dict = {}
            for i, h in enumerate(headers):
                row_dict[h] = cells[i].strip() if i < len(cells) else ""
            if any(v for v in row_dict.values()):
                out.append(row_dict)
        self.on_status(f"Caseload loaded: {len(out)} rows.")
        return out

    def _fetch_task_status(self, ctx, query: str) -> dict:
        """Row-filter the live Caseload list to `query` (a Student ID) and
        read the per-task pass/fail from the cell color/title — the bit the
        CSV export drops. Returns {"statuses": {"1": {...}, ...}} or
        {"error": msg}. Restores the row filter afterward so the live list
        isn't left narrowed."""
        from src.student_lookup import lookup_task_status
        q = (query or "").strip()
        if not q:
            return {"error": "no student id"}
        target, table = self._open_caseload_table(ctx)
        if table is None:
            return {"error": "caseload table didn't load"}
        fi = None
        try:
            fi = target.locator(
                'input[placeholder="Search All Rows..."]'
            ).filter(visible=True).first
            if fi.count() > 0:
                fi.click()
                fi.fill(q)
                fi.press("Enter")
                _wait_grid_settled(target, 1200)
        except Exception as e:
            return {"error": f"row filter failed: {e}"}
        err = ""
        statuses: dict = {}
        try:
            statuses = lookup_task_status(target, q)
        except Exception as e:
            err = str(e)
        # Always try to clear the filter so the live list is whole again.
        try:
            if fi is not None and fi.count() > 0:
                fi.click()
                fi.fill("")
                fi.press("Enter")
                target.wait_for_timeout(400)
        except Exception:
            pass
        if err:
            return {"error": err}
        return {"statuses": statuses}

    def _set_followup_date(self, ctx, query: str, date_str: str) -> dict:
        """Row-filter the live Caseload list to `query` (a Student ID), set
        that row's Followup Date to `date_str`, then restore the filter.
        Returns {ok, value, error}."""
        from src.student_lookup import set_followup_date
        q = (query or "").strip()
        if not q:
            return {"ok": False, "error": "no student id"}
        target, table = self._open_caseload_table(ctx)
        if table is None:
            return {"ok": False, "error": "caseload table didn't load"}
        fi = None
        try:
            fi = target.locator(
                'input[placeholder="Search All Rows..."]'
            ).filter(visible=True).first
            if fi.count() > 0:
                fi.click(); fi.fill(q); fi.press("Enter")
                _wait_grid_settled(target, 1200)
        except Exception as e:
            return {"ok": False, "error": f"row filter failed: {e}"}
        try:
            res = set_followup_date(target, date_str)
        except Exception as e:
            res = {"ok": False, "value": "", "error": str(e)}
        # Restore the filter so the live list is whole again.
        try:
            if fi is not None and fi.count() > 0:
                fi.click(); fi.fill(""); fi.press("Enter")
                target.wait_for_timeout(400)
        except Exception:
            pass
        return res

    def _set_followup_note(self, ctx, query: str, note_text: str) -> dict:
        """Row-filter the live Caseload list to `query` (a Student ID), set
        that row's Followup Note to `note_text`, then restore the filter."""
        from src.student_lookup import set_followup_note
        q = (query or "").strip()
        if not q:
            return {"ok": False, "error": "no student id"}
        target, table = self._open_caseload_table(ctx)
        if table is None:
            return {"ok": False, "error": "caseload table didn't load"}
        fi = None
        try:
            fi = target.locator(
                'input[placeholder="Search All Rows..."]'
            ).filter(visible=True).first
            if fi.count() > 0:
                fi.click(); fi.fill(q); fi.press("Enter")
                _wait_grid_settled(target, 1200)
        except Exception as e:
            return {"ok": False, "error": f"row filter failed: {e}"}
        try:
            res = set_followup_note(target, note_text)
        except Exception as e:
            res = {"ok": False, "value": "", "error": str(e)}
        try:
            if fi is not None and fi.count() > 0:
                fi.click(); fi.fill(""); fi.press("Enter")
                target.wait_for_timeout(400)
        except Exception:
            pass
        return res

    def _scrape_all_task_status(self, ctx) -> dict:
        """Bulk '2a' scrape: scroll-load the whole live Caseload list, then
        read every task cell's pass/fail (the colour the CSV export drops),
        keyed by Student ID. Returns {"by_sid": {sid: {tnum: {...}}},
        "count": N} or {"error": msg}. Runs in the background after a refresh
        (App._maybe_bulk_scrape_task_status); the ~5-9s cost is the scroll-
        load, the read itself is ~instant."""
        from src.student_lookup import read_loaded_task_status
        target, table = self._open_caseload_table(ctx)
        if table is None:
            return {"error": "caseload table didn't load"}
        # Clear any active row filter so we scroll the WHOLE caseload.
        try:
            fi = target.locator(
                'input[placeholder="Search All Rows..."]'
            ).filter(visible=True).first
            if fi.count() > 0:
                fi.click(); fi.fill(""); fi.press("Enter")
                _wait_grid_settled(target, 1500)
        except Exception:
            pass
        # Scroll-load: drive the last <tr> into view until the row count is
        # stable for two checks (Lightning lazy-loads rows on scroll).
        last_count, stable = 0, 0
        MAX_ITERS = 200
        for _ in range(MAX_ITERS):
            # Yield to the user: this background scroll-load is the slow part
            # (~5-9s), and the worker is single-threaded, so a note/email/text
            # action fired mid-scrape would otherwise wait for the whole thing.
            # The moment any user command is queued, bail out — the caller
            # re-runs the scrape once the worker is free again.
            if not self.q.empty():
                return {"interrupted": True}
            rows = table.locator("tr")
            count = rows.count()
            if count == last_count:
                stable += 1
                if stable >= 2:
                    break
            else:
                stable = 0
            last_count = count
            try:
                rows.nth(count - 1).scroll_into_view_if_needed(timeout=2000)
            except Exception:
                pass
            try:
                target.wait_for_timeout(400)
            except Exception:
                pass
        try:
            by_sid = read_loaded_task_status(table)
        except Exception as e:
            return {"error": f"bulk task read failed: {e}"}
        return {"by_sid": by_sid, "count": len(by_sid)}

    MONGOOSE_DASHBOARD_URL = "https://sms.mongooseresearch.com/legacy-dashboard"

    def _open_mongoose(self, ctx, *, focus: bool = True) -> dict:
        """Open (or focus) the Mongoose texting dashboard in the launcher's OWN
        persistent context, so the probe / texting automation can see it.
        Reuses an existing mongoose tab if one is already open; otherwise spawns
        a fresh page and navigates. `focus` brings the tab to the foreground —
        pass focus=False at startup so it doesn't steal focus from the
        Salesforce login. Returns {ok, url} or {error}.

        NOTE: a USER-opened new tab while Salesforce is the main page hangs at
        about:blank (the known popup hang — see the startup TODO). A
        Playwright-driven new_page()+goto() does NOT, because the goto is the
        action that unsticks it. So the reliable time to spawn this is startup;
        afterwards this just focuses the already-open tab."""
        # Reuse an existing Mongoose tab if present.
        for page in ctx.pages:
            try:
                if not page.is_closed() and "mongoose" in (page.url or "").lower():
                    if focus:
                        try:
                            page.bring_to_front()
                        except Exception:
                            pass
                    return {"ok": True, "url": page.url or ""}
            except Exception:
                continue
        try:
            page = ctx.new_page()
            page.goto(self.MONGOOSE_DASHBOARD_URL, wait_until="domcontentloaded")
            if focus:
                try:
                    page.bring_to_front()
                except Exception:
                    pass
            return {"ok": True, "url": page.url or ""}
        except Exception as e:
            return {"error": str(e)}

    def _mongoose_page(self, ctx):
        """Return the open Mongoose tab in the launcher's context, or None.
        (Do NOT use _active_page — it prefers the Salesforce tab.)"""
        for page in ctx.pages:
            try:
                if not page.is_closed() and "mongoose" in (page.url or "").lower():
                    return page
            except Exception:
                continue
        return None

    def _send_text(self, ctx, payload: dict) -> dict:
        """Drive the Mongoose compose modal from a fired text action. `payload`
        carries the rendered body, recipient mobiles, inbox label, an optional
        schedule-slot dict, the schedule name, and the commit flag. Returns
        {ok} or {error}."""
        page = self._mongoose_page(ctx)
        if page is None:
            # Auto-open Mongoose in-context if it isn't open yet. (Reliable
            # mid-session — by now the caseload load has cleared the
            # about:blank popup hang.)
            self.on_status("Opening Mongoose…")
            res = self._open_mongoose(ctx, focus=True)
            if res.get("error"):
                return {"error": f"couldn't open Mongoose: {res['error']}"}
            page = self._mongoose_page(ctx)
            if page is None:
                return {"error": "Mongoose didn't open."}
            # Let SSO / the SPA settle before composing.
            try:
                page.wait_for_timeout(1500)
            except Exception:
                pass
        try:
            page.bring_to_front()
        except Exception:
            pass
        from src import text_message as tm
        sch = payload.get("schedule")
        slot = None
        if sch:
            slot = tm.ScheduleSlot(
                team_dt=None,
                date_str=sch.get("date_str", ""),
                hour12=int(sch.get("hour12", 10)),
                minute=int(sch.get("minute", 0)),
                ampm=sch.get("ampm", "AM"),
                student_local_str=sch.get("student_local_str", ""),
            )
        msg = tm.TextMessage(
            body=payload.get("body", ""),
            recipients_mobile=list(payload.get("recipients") or []),
            inbox_label=payload.get("inbox_label", ""),
            schedule=slot,
            schedule_name=payload.get("schedule_name", ""),
            commit=bool(payload.get("commit", False)),
        )
        try:
            tm.send_text(page, msg, on_status=self.on_status)
            return {"ok": True}
        except Exception as e:
            return {"error": str(e)}

    def _test_text(self, ctx, mobile: str, body: str) -> dict:
        """TEMP dev: drive the Mongoose compose modal for one REAL recipient and
        SCHEDULE the text for tomorrow ~10:00 AM (commit=True). The recipient
        must be a contact in the currently-open inbox (inboxes are course-
        scoped). Verify it appears under Scheduled Messages, then delete it —
        it won't actually send until tomorrow. Validates the full driver
        click-through incl. the schedule step."""
        page = self._mongoose_page(ctx)
        if page is None:
            return {"error": "No Mongoose tab open — click 🐭 Open Mongoose first."}
        try:
            page.bring_to_front()
        except Exception:
            pass
        from src import text_message as tm
        tomorrow = datetime.now() + timedelta(days=1)
        slot = tm.ScheduleSlot(
            team_dt=tomorrow,
            date_str=tomorrow.strftime("%m/%d/%Y"),
            hour12=10, minute=0, ampm="AM",
            student_local_str="tomorrow 10:00 AM (test)",
        )
        msg = tm.TextMessage(
            body=body or "Test message — please ignore.",
            recipients_mobile=[mobile],
            schedule=slot,
            schedule_name="TEST – delete me",
            commit=True,
        )
        try:
            tm.send_text(page, msg, on_status=self.on_status)
            return {"ok": True}
        except Exception as e:
            return {"error": str(e)}

    def _probe_text(self, ctx) -> dict:
        """TEMP dev probe: capture the live Mongoose ("Cadence") texting
        composer DOM. The send mechanism is unknown, so this captures BROADLY:
        any visible dialog/panel, form fields (textarea / input / combobox /
        contenteditable), every visible button label, and any element whose
        tag/class/aria mentions text/sms/message/cadence/send/schedule. Open
        the Mongoose inbox + compose view for a student FIRST (and, to capture
        template/schedule pickers, open those too), then click probe.
        → temp/text_probe.html. Returns {url, buttons, matchTags, html, counts}.

        NOTE: does NOT use _active_page — that deliberately PREFERS the
        Salesforce tab, which would shadow the Mongoose tab. We pick the
        Mongoose page (sms.mongooseresearch.com) directly, else fall back to
        the most-recent responsive page; the returned `url` lets the UI warn
        if it ended up on the wrong site."""
        target = fallback = None
        for page in reversed(ctx.pages):
            try:
                if page.is_closed():
                    continue
                url = page.url or ""
                _ = page.locator("html").count()  # responsive probe
            except Exception:
                continue
            if fallback is None:
                fallback = page
            if "mongoose" in url.lower():
                target = page
                break
        target = target or fallback
        if target is None:
            return {"error": "no active page"}
        captured_url = ""
        try:
            captured_url = target.url or ""
        except Exception:
            pass
        js = r'''
        () => {
          const KEEP=['class','data-label','title','role','aria-label',
            'data-row-key-value','data-target-selection-name','data-tab-value',
            'name','value','data-aura-class','href','aria-haspopup','aria-expanded',
            'placeholder','for','type','checked','data-value','data-field',
            'maxlength','data-name'];
          const cls=el=>String((el.className&&el.className.baseVal!==undefined)
            ?el.className.baseVal:(el.className||''));
          function vis(el){try{return el.offsetParent!==null||el.getClientRects().length>0;}catch(e){return false;}}
          function ser(el,d){
            if(!el||d>30) return '';
            const tag=(el.tagName||'').toLowerCase();
            if(['script','style','svg','path','iframe','img'].includes(tag)) return '';
            let s='<'+tag;
            const at=el.attributes;
            if(at) for(let ai=0;ai<at.length;ai++){ const a=at[ai];
              if(a&&KEEP.includes(a.name))
                s+=' '+a.name+'="'+String(a.value||'').slice(0,80).replace(/"/g,'')+'"';
            }
            s+='>';
            const serNode=(n)=>{
              if(n.nodeType===3){const tx=n.textContent.trim();
                return tx?tx.slice(0,200):'';}
              if(n.nodeType!==1) return '';
              return ser(n,d+1);
            };
            if(el.shadowRoot) s+='\n#shadow{\n'+
              [...el.shadowRoot.childNodes].map(serNode).join('')+'}\n';
            for(const c of el.childNodes) s+=serNode(c);
            return s+'</'+tag+'>\n';
          }
          function deepAll(pred){
            const r=[];
            (function w(root){
              for(const el of root.querySelectorAll('*')){
                try{if(pred(el))r.push(el);}catch(e){}
                if(el.shadowRoot) w(el.shadowRoot);
              }
            })(document);
            return r;
          }
          const RX=/text|sms|message|cadence|send|sales.?engagement|hvs|schedul/i;
          // Every visible button/menuitem label — find "Send Text", "Send",
          // "Schedule", template pickers, etc.
          const buttons=[...new Set(deepAll(el=>{
            if(!vis(el))return false;
            const role=el.getAttribute&&el.getAttribute('role');
            return el.tagName==='BUTTON'||el.tagName==='LIGHTNING-BUTTON'||
              role==='button'||role==='menuitem'||
              el.tagName==='LIGHTNING-MENU-ITEM';
          }).map(el=>((el.getAttribute&&el.getAttribute('aria-label'))||
            el.textContent||'').trim().slice(0,60)).filter(Boolean))].slice(0,120);
          // Elements that look texting-related by tag/class/aria.
          const hits=deepAll(el=>{
            const t=(el.tagName||'').toLowerCase();
            const al=(el.getAttribute&&el.getAttribute('aria-label'))||'';
            return RX.test(t)||RX.test(cls(el))||RX.test(al);
          });
          const matchTags=[...new Set(hits.map(el=>
            el.tagName.toLowerCase()+' .'+cls(el).split(/\s+/).slice(0,3).join('.')
          ))].slice(0,60);
          let html='';
          // Visible dialogs / panels / docked composer.
          const dialogs=deepAll(el=>{ if(!vis(el))return false;
            const role=el.getAttribute&&el.getAttribute('role'); const c=cls(el);
            return role==='dialog'||
              /slds-modal__container|slds-modal__content|slds-docked|dockable|slds-panel|cadence|sendText|sms/i.test(c);
          });
          const topD=dialogs.filter(m=>!dialogs.some(o=>o!==m&&o.contains(m)));
          for(const dd of topD.slice(0,3)) html+='\n=== DIALOG / PANEL ===\n'+ser(dd,0);
          // Form fields (the message body, recipient, template, schedule),
          // climbed a few levels to grab their labels + surrounding controls.
          function climb(el,n){let p=el;for(let i=0;i<n&&p&&p.parentElement;i++)p=p.parentElement;return p;}
          const FLD=['TEXTAREA','LIGHTNING-TEXTAREA','LIGHTNING-INPUT',
            'LIGHTNING-COMBOBOX','LIGHTNING-GROUPED-COMBOBOX',
            'LIGHTNING-INPUT-RICH-TEXT','SELECT'];
          const fields=deepAll(el=>{ if(!vis(el))return false;
            const role=el.getAttribute&&el.getAttribute('role');
            return FLD.includes(el.tagName)||role==='combobox'||
              (el.getAttribute&&el.getAttribute('contenteditable')==='true'); });
          const seenC=new Set(); let nForm=0;
          for(const f of fields.slice(0,60)){ const c=climb(f,6);
            if(!c||seenC.has(c))continue; seenC.add(c);
            html+='\n=== FIELD CONTAINER ===\n'+ser(c,0); nForm++;
            if(nForm>=10)break; }
          // Texting-related hits not already inside a dialog we dumped.
          const topHits=hits.filter(h=>vis(h)&&!hits.some(o=>o!==h&&o.contains(h)));
          let nHit=0;
          for(const h of topHits.slice(0,6)){
            html+='\n=== MATCH (text/sms/cadence) ===\n'+ser(h,0); nHit++; }
          return {buttons, matchTags, html:html.slice(0,700000),
            counts:{hits:hits.length, dialogs:topD.length,
              fields:fields.length, field_containers:nForm, match_blocks:nHit}};
        }
        '''
        try:
            data = target.evaluate(js)
            if isinstance(data, dict):
                data["url"] = captured_url
                return data
            return {"error": "no data"}
        except Exception as e:
            return {"error": str(e)}

    def _read_essential_actions(self, ctx) -> dict:
        """Read the active student's open Essential Actions for the
        fire-time attach dialog."""
        target = self._active_page(ctx)
        if target is None:
            return {"error": "no active page"}
        from src.student_lookup import read_essential_actions
        try:
            return {"eas": read_essential_actions(target)}
        except Exception as e:
            return {"error": str(e)}

    def _read_ea_dashboard(self, ctx) -> dict:
        """Navigate to the Essential Actions dashboard, scroll-load + read
        all EAs, then return to the caseload list so subsequent finds/fires
        start from the right page."""
        from src.config import ESSENTIAL_ACTIONS_URL
        from src.student_lookup import read_ea_dashboard_rows
        target = self._active_page(ctx)
        if target is None:
            return {"error": "no active page"}
        if not ESSENTIAL_ACTIONS_URL:
            return {"error": "ESSENTIAL_ACTIONS_URL not set"}
        err, rows = "", []
        try:
            target.goto(ESSENTIAL_ACTIONS_URL, wait_until="domcontentloaded")
            target.wait_for_timeout(800)
            rows = read_ea_dashboard_rows(target)
        except Exception as e:
            err = str(e)
        try:
            if CASELOAD_URL:
                target.goto(CASELOAD_URL, wait_until="domcontentloaded")
        except Exception:
            pass
        if err:
            return {"error": err}
        return {"eas": rows}

    def _handle_run(
        self, ctx, scenario: ScenarioConfig, override: str,
        clipboard: str = "",
        custom_bodies: Optional[dict[int, str]] = None,
        prompt_vars: Optional[dict[str, str]] = None,
        ea: Optional[tuple] = None,
    ) -> bool:
        """Return True iff the note ran without errors (regardless of
        whether all sub-notes were auto-submitted). The batch driver
        uses the return value to track processed-vs-skipped honestly."""
        target = self._active_page(ctx)
        if target is None:
            self.on_status("No browser pages open.")
            return False
        # Note: when scenario.find_first is True, the main thread has
        # already driven the LIST_MATCHES + CLICK_MATCH sequence before
        # queueing this RUN — so by the time we get here, the active
        # student is already loaded.
        # Always try to capture student name — used for auto-detect and
        # for the session log entry on success. Defensive try/except:
        # the page can race-die between _active_page's liveness check
        # and the locator query (especially right after the auto-
        # download download-tab closed). Treat any failure as "no
        # student visible" rather than crashing the run.
        try:
            student = get_active_student_name(target)
        except Exception as e:
            self.on_status(
                f"No visible note panel (page state issue: {e}). "
                "Open one and try again."
            )
            return False
        # Look up the Caseload row once: gets course code, student ID,
        # and email in a single pass. Tolerates the same kind of
        # transient page-state error — fall back to empty info.
        try:
            info = lookup_caseload_student(target, student) if student else {}
        except Exception:
            info = {}
        if override:
            course_code = override
            self.on_status(f"Using course code (manual): {course_code}")
            if student:
                self.on_status(f"Active student: {student}")
        else:
            if not student:
                self.on_status("No visible note panel. Open one and try again.")
                return False
            self.on_status(f"Active student: {student}")
            detected = info.get("course_code", "")
            if not detected:
                self.on_status(
                    f"Could not auto-detect for {student}. Type a code in the field."
                )
                return False
            course_code = detected
            self.on_status(f"Auto-detected course code: {course_code}")
        # Essential-Action path: open the note form via the EA's row action
        # ("Add Note to EA" / "& Close EA") so the note is tied to the EA.
        # run_scenario then fills that form just like the embedded panel.
        if ea:
            from src.student_lookup import open_ea_note_form
            ea_reason, ea_course, ea_close = ea
            self.on_status(
                f"Opening Essential Action note form: {ea_reason!r}"
                f"{' (& close)' if ea_close else ''}…")
            try:
                opened = open_ea_note_form(target, ea_reason, ea_course, ea_close)
            except Exception as e:
                opened = False
                self.on_status(f"EA note form error: {e}")
            if not opened:
                self.on_status(
                    f"Couldn't open the Essential Action note form for "
                    f"{ea_reason!r}. Note not filed.")
                return False
            # Small settle; fill_note then waits for the form's Submit
            # button to be visible, so no long blind sleep is needed.
            target.wait_for_timeout(200)
        self.on_status(f"Running {scenario.name!r}...")
        try:
            all_submitted = run_scenario(
                target, scenario, course_code,
                clipboard=clipboard, custom_bodies=custom_bodies,
                prompt_vars=prompt_vars,
                on_status=self.on_status,
            )
            tail = "" if all_submitted else "  (left open — submit unchecked)"
            self.on_status(f"Done: {scenario.name!r} (course {course_code!r}).{tail}")
            self.on_note_filed(NoteLogEntry(
                timestamp=datetime.now(),
                scenario=scenario.name,
                course_code=course_code,
                student=student or "(unknown)",
                student_id=info.get("student_id", ""),
                student_email=info.get("student_email", ""),
                pm_name=info.get("pm_name", ""),
                pm_email=info.get("pm_email", ""),
                submitted=all_submitted,
            ))
            return True
        except RuntimeError as e:
            self.on_status(f"Failed: {e}")
            return False


# ============================================================
# Hotkey helpers
# ============================================================

def to_pynput_hotkey_string(spec: str) -> str:
    """Convert 'F1' or 'Ctrl+Shift+1' to pynput HotKey.parse syntax."""
    parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
    if not parts:
        raise ValueError("empty hotkey spec")
    out = []
    for p in parts:
        if p in ("ctrl", "control"):
            out.append("<ctrl>")
        elif p == "shift":
            out.append("<shift>")
        elif p == "alt":
            out.append("<alt>")
        elif p in ("cmd", "win", "super"):
            out.append("<cmd>")
        elif p.startswith("f") and p[1:].isdigit():
            out.append(f"<{p}>")
        elif len(p) == 1:
            out.append(p)
        else:
            out.append(f"<{p}>")
    return "+".join(out)


def _standalone_fkey_vk(spec: str) -> Optional[int]:
    parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
    if len(parts) != 1:
        return None
    p = parts[0]
    if p.startswith("f") and p[1:].isdigit():
        n = int(p[1:])
        if 1 <= n <= 24:
            return 0x70 + (n - 1)
    return None


def _keysym_to_hotkey_part(ks: str) -> str:
    """Translate a Tk keysym to our hotkey notation."""
    if ks.startswith("F") and ks[1:].isdigit():
        return ks
    if len(ks) == 1:
        return ks.upper()
    return ks


_HOTKEY_MOD_ORDER = ("Ctrl", "Shift", "Alt")


def open_hotkey_capture(parent, on_done: Callable[[str], None]) -> None:
    """Pop a modal that captures a key combination. Calls on_done with
    the captured string (e.g. 'Ctrl+Shift+A', 'F4') or "" on cancel.
    Modifier keys are not finalized until a non-modifier is pressed."""
    dialog = ctk.CTkToplevel(parent)
    dialog.title("Press hotkey")
    dialog.geometry("420x240")
    dialog.transient(parent)
    dialog.attributes("-topmost", True)
    dialog.grab_set()

    ctk.CTkLabel(
        dialog,
        text=("Press the keys you want as the hotkey.\n"
              "Example: F3, or hold Ctrl+Shift then press A.\n"
              "Esc to cancel.\n\n"
              "Avoid browser-claimed F-keys: F1 (help), F6 (address bar),\n"
              "F11 (fullscreen), F12 (devtools) — Chromium intercepts these\n"
              "before our hook can fire."),
        justify="left",
    ).pack(padx=20, pady=(15, 5))

    preview_var = ctk.StringVar(value="—")
    ctk.CTkLabel(
        dialog, textvariable=preview_var,
        font=ctk.CTkFont(size=18, weight="bold"),
    ).pack(pady=4)

    held: set[str] = set()
    finished = {"done": False}

    def current_mods_str() -> str:
        mods = [m for m in _HOTKEY_MOD_ORDER if m in held]
        return "+".join(mods) if mods else "—"

    def finish(combo: str) -> None:
        if finished["done"]:
            return
        finished["done"] = True
        try:
            dialog.grab_release()
        except Exception:
            pass
        try:
            dialog.destroy()
        except Exception:
            pass
        on_done(combo)

    def on_press(event):
        if finished["done"]:
            return
        ks = event.keysym
        if ks == "Escape":
            finish("")
            return
        if ks in ("Control_L", "Control_R"):
            held.add("Ctrl"); preview_var.set(current_mods_str()); return
        if ks in ("Shift_L", "Shift_R"):
            held.add("Shift"); preview_var.set(current_mods_str()); return
        if ks in ("Alt_L", "Alt_R"):
            held.add("Alt"); preview_var.set(current_mods_str()); return
        if ks in ("Super_L", "Super_R", "Win_L", "Win_R", "Caps_Lock", "Num_Lock"):
            return
        mods = [m for m in _HOTKEY_MOD_ORDER if m in held]
        combo = "+".join(mods + [_keysym_to_hotkey_part(ks)])
        preview_var.set(combo)
        dialog.after(150, lambda: finish(combo))

    def on_release(event):
        ks = event.keysym
        if ks in ("Control_L", "Control_R"): held.discard("Ctrl")
        elif ks in ("Shift_L", "Shift_R"): held.discard("Shift")
        elif ks in ("Alt_L", "Alt_R"): held.discard("Alt")
        if not finished["done"]:
            preview_var.set(current_mods_str())

    dialog.bind("<KeyPress>", on_press)
    dialog.bind("<KeyRelease>", on_release)
    ctk.CTkButton(dialog, text="Cancel", command=lambda: finish(""), width=90).pack(pady=10)
    dialog.focus_set()


# Remembers each user-movable dialog's last geometry across opens so
# they reopen where the user last placed/sized them. Persists only
# within a launcher session (intentional — restart resets to defaults).
# Secondary-button styling. Previously many call sites used
# `fg_color="transparent", border_width=1` which renders the text in
# the same gray as the dark-mode panel background — unreadable until
# hovered. Explicit fg/text/border colors here give high contrast in
# both light and dark mode.
SECONDARY_BTN_KWARGS = dict(
    fg_color=("gray82", "gray28"),
    text_color=("gray10", "gray95"),
    hover_color=("gray72", "gray38"),
    border_width=1,
    border_color=("gray60", "gray45"),
)


# Preset palette for scenario group colors. Muted enough to read
# comfortably on both light and dark backgrounds; tuned so the
# text-color helper (YIQ luminance) gives a sane white/black
# foreground without manual override.
GROUP_COLOR_PALETTE: list[tuple[str, str]] = [
    ("Slate", "#5a6f8a"),
    ("Blue", "#3a7ad9"),
    ("Teal", "#239a8e"),
    ("Green", "#3f9d3f"),
    ("Olive", "#7a9038"),
    ("Amber", "#c08a25"),
    ("Orange", "#d27033"),
    ("Red", "#c14e4e"),
    ("Pink", "#c25c92"),
    ("Purple", "#8d56b8"),
    ("Brown", "#7a5a3c"),
    ("Gray", "#7a7a7a"),
]


def _text_color_for_bg(hex_color: str) -> str:
    """Return '#000000' or '#ffffff' — whichever contrasts better
    against `hex_color`. Uses the YIQ luminance formula which
    weights green most heavily (matches human perception of
    brightness)."""
    h = (hex_color or "").lstrip("#")
    if len(h) == 3:
        h = "".join(c + c for c in h)
    if len(h) != 6:
        return "#000000"
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return "#000000"
    yiq = (r * 299 + g * 587 + b * 114) / 1000
    return "#000000" if yiq >= 128 else "#ffffff"


def _hover_color_for(hex_color: str) -> str:
    """Slightly darker version of `hex_color` for hover state.
    Returns the input unchanged if it can't be parsed."""
    h = (hex_color or "").lstrip("#")
    if len(h) == 3:
        h = "".join(c + c for c in h)
    if len(h) != 6:
        return hex_color
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return hex_color
    f = 0.82
    return f"#{int(r * f):02x}{int(g * f):02x}{int(b * f):02x}"


_DIALOG_GEOMETRY: dict[str, str] = {}
_DIALOG_DEFAULTS: dict[str, str] = {
    "find_and_pick": "480x440",
    "additional_text": "640x420",
    "batch_review": "720x560",
    "html_template_editor": "900x640",
}


# Sentinel shown in the "Email font" dropdown when no font is set —
# meaning Outlook's compose default applies (no CSS injection). Kept
# as a constant so the UI label and the serialize check stay in sync.
EMAIL_FONT_DEFAULT_LABEL = "(Outlook default)"

# Placeholder address shipped in the bundled sample actions/templates
# (default_scenarios.yaml + default_email_templates/). If an action is
# fired while its email still contains this, the user hasn't swapped in
# their own address yet — warn before sending (see App._fire).
SAMPLE_EMAIL_PLACEHOLDER = "your.email@wgu.edu"


# ---- Adjustable text sizes (per "channel") -------------------------------
# Named font channels so each reading/editing surface can carry its own
# user-adjustable text size: 'activity' (log), 'viewer' (caseload table),
# 'email' (FERPA reviewer), 'editor' (note bodies + template editor).
# CTkTextbox surfaces register via register_font_box(); non-CTk surfaces
# (the ttk caseload Treeview) register an apply callback via
# register_font_apply(). Ctrl +/- and Ctrl+MouseWheel on a registered
# widget adjust that channel live. The App wires _FONT_PERSIST to save.
UI_FONT_CHANNELS = ("activity", "viewer", "email", "editor", "notes")
UI_FONT_DEFAULTS = {"activity": 13, "viewer": 11, "email": 12, "editor": 12,
                    "notes": 12}
UI_FONT_MIN, UI_FONT_MAX = 8, 40
_font_sizes: dict = dict(UI_FONT_DEFAULTS)
_font_boxes: dict = {c: [] for c in UI_FONT_CHANNELS}
_font_applies: dict = {c: [] for c in UI_FONT_CHANNELS}
_FONT_PERSIST: list = [None]  # holder for persist callback(channel, size)


def font_size(channel: str) -> int:
    return _font_sizes.get(channel, 13)


def set_font_size(channel: str, n: int, persist: bool = True) -> None:
    """Set a channel's text size (clamped) and apply to all its widgets."""
    n = max(UI_FONT_MIN, min(UI_FONT_MAX, int(n)))
    _font_sizes[channel] = n
    for b in list(_font_boxes.get(channel, [])):
        try:
            b.configure(font=ctk.CTkFont(size=n))
        except Exception:
            try:
                _font_boxes[channel].remove(b)
            except ValueError:
                pass
    for cb in list(_font_applies.get(channel, [])):
        try:
            cb(n)
        except Exception:
            pass
    if persist and _FONT_PERSIST[0]:
        try:
            _FONT_PERSIST[0](channel, n)
        except Exception:
            pass


def bind_font_hotkeys(channel: str, widget) -> None:
    """Bind Ctrl +/- and Ctrl+MouseWheel on `widget` to adjust `channel`."""
    def bump(d):
        set_font_size(channel, _font_sizes[channel] + d)
        return "break"
    widget.bind("<Control-MouseWheel>",
                lambda e: bump(1 if e.delta > 0 else -1))
    widget.bind("<Control-plus>", lambda e: bump(1))
    widget.bind("<Control-equal>", lambda e: bump(1))  # Ctrl+= (no shift)
    widget.bind("<Control-minus>", lambda e: bump(-1))


def register_font_box(channel: str, box, hotkeys: bool = True) -> None:
    """Register a CTkTextbox to a font channel: apply the current size and
    (optionally) bind the zoom hotkeys."""
    _font_boxes.setdefault(channel, []).append(box)
    try:
        box.configure(font=ctk.CTkFont(size=_font_sizes[channel]))
    except Exception:
        pass
    if hotkeys:
        bind_font_hotkeys(channel, box)


def register_font_apply(channel: str, cb) -> None:
    """Register an apply callback cb(size) for a non-CTkTextbox surface
    (e.g. the caseload Treeview style). Called now + on every change."""
    _font_applies.setdefault(channel, []).append(cb)
    try:
        cb(_font_sizes[channel])
    except Exception:
        pass


_viewer_font_registered = [False]
_notes_font_registered = [False]


def apply_caseload_tree_font(size: int) -> None:
    """Apply a text size to the caseload Treeview's global ttk style: row
    font, row height (so taller text fits), and heading font."""
    try:
        style = ttk.Style()
        style.configure("Caseload.Treeview", font=("", size),
                        rowheight=max(18, int(size * 2.05)))
        style.configure("Caseload.Treeview.Heading",
                        font=("", size, "bold"))
    except Exception:
        pass


# ===== Quick-view derived-value helpers =====
#
# The caseload panel's quick-view box shows info that's instantly
# available from the cached CSV row (no Salesforce navigation), plus a
# few values we derive for free: a term-end countdown, the student's
# current local time, and Task pass/fail/not-submitted badges.

# WGU caseload exports a bare timezone abbreviation (EST/CST/...). Map
# to IANA zones so we can show the correct *current* local time with DST
# handled (e.g. EST in June is really EDT).
_TZ_ABBR_TO_IANA = {
    "EST": "America/New_York", "EDT": "America/New_York",
    "CST": "America/Chicago", "CDT": "America/Chicago",
    "MST": "America/Denver", "MDT": "America/Denver",
    "PST": "America/Los_Angeles", "PDT": "America/Los_Angeles",
    "AKST": "America/Anchorage", "AKDT": "America/Anchorage",
    "HST": "Pacific/Honolulu",       # Hawaii — no DST
    "ChS": "Pacific/Guam", "ChST": "Pacific/Guam",  # Chamorro — no DST
}


def student_local_time(tz_abbr: str) -> str:
    """Current local time for a student given their CSV timezone
    abbreviation, e.g. 'EST' -> '2:14 PM'. Empty string if unknown."""
    tz_abbr = (tz_abbr or "").strip()
    iana = _TZ_ABBR_TO_IANA.get(tz_abbr)
    if not iana:
        return ""
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo(iana))
        return now.strftime("%I:%M %p").lstrip("0")
    except Exception:
        return ""


def days_until(date_str: str) -> Optional[int]:
    """Whole days from today until an ISO 'YYYY-MM-DD' date (negative if
    past). None if unparseable."""
    s = (date_str or "").strip()[:10]
    if not s:
        return None
    try:
        d = datetime.strptime(s, "%Y-%m-%d").date()
        return (d - datetime.now().date()).days
    except Exception:
        return None


def parse_task_status(val: str) -> tuple[str, str, int]:
    """Interpret a caseload Task CSV cell, e.g. '2026-06-03 (1)'.

    IMPORTANT: the CSV export only carries the most-recent submission
    DATE and the number in parentheses, and that number is the ATTEMPT
    COUNT — NOT a pass/fail flag. Pass/fail lives only in the live list
    view's cell color/title (e.g. class 'cellColorGreen' + a title like
    '… | Passed | 04/21/2026 | 1 Attempt | System: EMA'), which the CSV
    export drops. So from the CSV alone we can only say 'submitted' vs
    'not submitted' — we must NOT infer 'passed'. The real status is
    fetched on demand (see CaseloadPanel._qv_task_badges).

    Returns (state, date, attempts) where state is 'none' | 'submitted'.
    """
    s = (val or "").strip()
    if not s:
        return "none", "", 0
    m = re.match(r"(\d{4}-\d{2}-\d{2}).*?\((\d+)\)", s)
    if not m:
        return "submitted", s[:10], 0
    return "submitted", m.group(1), int(m.group(2))


# Active name-capitalization mode (kept in sync with the user's setting by
# App._sync_name_cap_mode). Module-level so both variable builders — the
# App-side CSV one and the BrowserWorker-side DOM one (no settings access) —
# share it without threading the setting through.
_NAME_CAP_MODE = "standard"


def _capitalize_name(name: str, mode: Optional[str] = None) -> str:
    """Normalize a name's capitalization for use in template variables.
    `mode` (defaults to the global _NAME_CAP_MODE):
      'off'      — return the name exactly as stored;
      'lower'    — only fix LOWERCASE entry errors ('john' → 'John'); leave
                   ALL-CAPS and mixed case alone;
      'standard' — also normalize ALL-CAPS to Title case ('JANE' → 'Jane'),
                   while PRESERVING intentional mixed case (McDonald,
                   O'Brien, Mary-Jane stay as-is).
    Works per letter-run so hyphen/apostrophe parts are handled
    (mary-jane → Mary-Jane)."""
    if not name:
        return name
    m = mode or _NAME_CAP_MODE
    if m == "off":
        return name
    if m == "lower":
        return re.sub(r"\b[a-z]", lambda mo: mo.group(0).upper(), name)

    # 'standard': title-case any run that's entirely lower OR entirely upper;
    # leave mixed-case runs untouched so deliberate caps survive.
    def _fix(mo):
        w = mo.group(0)
        if w.islower() or w.isupper():
            return w[:1].upper() + w[1:].lower()
        return w
    return re.sub(r"[A-Za-z]+", _fix, name)


# Task badge appearance per state. (mark, fg_color, text_color). 'passed'
# /'returned'/'pending' come from the on-demand live-status fetch (the
# list view's cellColorGreen/Red/Blue); 'submitted' is the CSV-only
# fallback (date known, pass/fail not yet fetched); 'none' = not
# submitted. See CaseloadPanel._qv_task_badges.
TASK_BADGE_STYLES: dict = {
    "passed":    ("✓", ("#1e7a34", "#2ea043"), "#ffffff"),
    "returned":  ("✗", ("#b3261e", "#d13b30"), "#ffffff"),
    # In-process: a WHITE HOURGLASS glyph (U+29D6) — not the ⏳ emoji, which
    # would render in its own colour and ignore text_color. As a plain text
    # glyph it honours the white text_color on the blue badge.
    "pending":   ("⧖", ("#1f6feb", "#2f81f7"), "#ffffff"),
    "submitted": ("•", ("gray70", "gray45"), "#ffffff"),
    "none":      ("–", ("gray80", "gray35"), ("gray30", "gray75")),
}

# Map the live list view's task-cell color class → badge state.
TASK_CELLCOLOR_STATE: dict = {
    "cellColorGreen": "passed",
    "cellColorRed": "returned",
    "cellColorBlue": "pending",
}

# Glyph PREFIX for the main grid's Task columns (a ttk.Treeview, which
# can't colour an individual cell's text or background — tags are per-row
# only). So we prefix a colour-bearing emoji block onto the cell text to
# echo the quick-view badges: ✅ passed · ❌ returned · 🟦 in-process ·
# ⚪ submitted-but-not-yet-scraped ("not loaded"/stale) · blank = no task.
# Real coloured text/cells await the deferred tksheet widget swap; the ⚪
# here is the "not ready" cue (every task cell shows ⚪ until a bulk 2a
# scrape lands, then flips to a colour). See parse_task_status / 2a.
TASK_CELL_GLYPHS: dict = {
    "passed":    "✅",
    "returned":  "❌",
    "pending":   "🟦",
    "submitted": "⚪",
    "none":      "",
}

# Friendly labels for a task's live status (the cache state → user word).
TASK_STATE_LABELS: dict = {
    "returned":  "Returned",
    "pending":   "In Process",
    "passed":    "Passed",
    "submitted": "Submitted",
}

# A single "Task N" filter column routes to one of three HIDDEN facet
# columns depending on the chosen operator, so the user filters one "Task 2"
# entry by date, submission count, OR status (see _rewrite_task_filter):
#   date ops    → Task{N}Date   (YYYY-MM-DD, from the CSV cell)
#   numeric ops → Task{N}Count  (submission count, from the CSV cell)
#   text ops    → Task{N}Status (Passed/Returned/In Process, from the scrape)
_TASK_DATE_OPS = {
    "is before", "is after", "is on", "is on or before", "is on or after",
    "is within", "before", "after", "on", "on_or_before", "on_or_after",
    "within",
}
_TASK_NUM_OPS = {
    "more than", "less than", "at least", "at most",
    "gt", "lt", "gte", "lte",
}


def _is_task_facet_col(col: str) -> bool:
    """True for the hidden per-task facet helper columns (Task1Date,
    Task2Count, Task3Status, …) — kept out of the grid + filter dropdowns;
    the single visible 'Task N' column stands in for all three."""
    return bool(re.fullmatch(r"Task\d+(Date|Count|Status)", col or ""))


def _resolve_filter_columns(f: dict, headers: list) -> dict:
    """Resolve a filter's display-name `column` to its CSV header, AND — for
    a column-comparison value written as `{Display Name}` — resolve the
    referenced column too, so the engine's per-row `row.get(...)` finds it.
    Identity entries pass through unchanged."""
    out = {**f, "column": caseload_csv.resolve_column(
        f.get("column", ""), headers)}
    m = re.fullmatch(r"\{(.+)\}", str(out.get("value", "")).strip())
    if m:
        out["value"] = "{" + caseload_csv.resolve_column(
            m.group(1), headers) + "}"
    return out


def _rewrite_task_filter(f: dict) -> dict:
    """Route a filter whose column is a 'TaskN' column to the right hidden
    facet. Unambiguous ops route by operator: date ops → TaskNDate, numeric
    ops → TaskNCount, empty/not-empty → TaskNDate ('did they submit'). The
    ambiguous text ops (is/is not/contains/does not contain) route by the
    VALUE:
      - all-integer (incl. comma-OR like '2, 3') → submission COUNT,
      - the special word 'Submitted' → 'has a submission' (passed/returned/
        in-process all carry a date), so it maps to TaskNDate is-not-empty
        (is-empty for the negated ops) and works even before the scrape,
      - anything else → a status word → TaskNStatus.
    So 'Task 2 is 2' filters count, 'Task 2 is Submitted' = has any
    submission, 'Task 2 is Returned' filters status. Non-task filters pass
    through. Eval-time only — the visible 'Task N' column is what the user
    picks + sees in review."""
    col = f.get("column") or ""
    m = re.fullmatch(r"Task(\d+)", col)
    if not m:
        return f
    n = m.group(1)
    op = (f.get("op") or "").strip()
    if op in _TASK_DATE_OPS:
        facet = f"Task{n}Date"
    elif op in _TASK_NUM_OPS:
        facet = f"Task{n}Count"
    elif op in ("is empty", "is not empty", "empty", "not_empty"):
        facet = f"Task{n}Date"
    else:  # is / is not / contains / does not contain
        val = str(f.get("value") or "").strip()
        parts = [p.strip() for p in val.split(",") if p.strip()]
        if parts and all(re.fullmatch(r"\d+", p) for p in parts):
            facet = f"Task{n}Count"
        elif val.lower() == "submitted":
            # "Submitted" = has been submitted (passed/returned/in-process —
            # all have a date). Use the date facet's emptiness, not the
            # colour-derived status (which has no real 'Submitted' value).
            negated = op in ("is not", "does not contain",
                             "not_equals", "not_contains")
            return {**f, "column": f"Task{n}Date",
                    "op": "is empty" if negated else "is not empty",
                    "value": ""}
        else:
            facet = f"Task{n}Status"
    return {**f, "column": facet}


def last_logged_action(student_id: str) -> str:
    """Most recent action THIS app logged for a student (from
    note_log.csv), as 'Jun 01 · welcome'. Empty if none / unreadable.
    Matched on student_id (falls back to nothing — ids are unique)."""
    sid = (student_id or "").strip()
    if not sid:
        return ""
    try:
        best = None
        with Path(NOTE_LOG_CSV).open(encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                if (r.get("student_id") or "").strip() != sid:
                    continue
                ts = (r.get("timestamp") or "").strip()
                if best is None or ts > best[0]:
                    best = (ts, r.get("scenario") or "")
        if not best:
            return ""
        ts, scenario = best
        try:
            when = datetime.strptime(ts[:10], "%Y-%m-%d").strftime("%b %d")
        except Exception:
            when = ts[:10]
        return f"{when} · {scenario}" if scenario else when
    except Exception:
        return ""


def note_html_to_text(s: str) -> str:
    """Flatten a note body (Salesforce stores some as HTML in the
    data-cell-value attr) to readable plain text."""
    s = s or ""
    s = re.sub(r"<\s*br\s*/?\s*>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"</\s*(p|div|li)\s*>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>", "", s)          # strip remaining tags
    s = html.unescape(s)                    # &nbsp; &amp; etc.
    s = s.replace("\xa0", " ")
    # Collapse runs of blank lines / trailing space.
    lines = [ln.strip() for ln in s.splitlines()]
    out, blank = [], False
    for ln in lines:
        if not ln:
            if not blank and out:
                out.append("")
            blank = True
        else:
            out.append(ln)
            blank = False
    return "\n".join(out).strip()


def fmt_note_date(iso: str) -> str:
    """'2026-06-02T17:11:20.000Z' -> '6/02 5:11 PM'. Passes through
    anything it can't parse."""
    s = (iso or "").strip()
    if not s:
        return ""
    try:
        d = datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
        return d.strftime("%m/%d %I:%M %p").lstrip("0")
    except Exception:
        return s[:16].replace("T", " ")


_EMA_URL_RE = re.compile(
    r"tasks\.wgu\.edu/student/(\d+)/course/(\d+)/task/(\d+)/score-report",
    re.I)


def parse_ema_url(url: str) -> Optional[dict]:
    """Pull the ids out of an EMA Score Report URL, e.g.
    https://tasks.wgu.edu/student/009930908/course/33860018/task/4521/score-report
    -> {student_id, course_id, task_id}. None if it doesn't match."""
    m = _EMA_URL_RE.search(url or "")
    if not m:
        return None
    return {"student_id": m.group(1), "course_id": m.group(2),
            "task_id": m.group(3)}


def build_ema_url(student_id: str, course_id: str, task_id: str) -> str:
    return (f"https://tasks.wgu.edu/student/{student_id}"
            f"/course/{course_id}/task/{task_id}/score-report")


def _attach_tooltip(widget, text: str) -> None:
    """Lightweight hover tooltip for a widget (no dependency on any UI
    framework — a borderless Toplevel shown on enter, hidden on leave)."""
    state = {"tip": None}

    def show(_e=None):
        if state["tip"] is not None or not text:
            return
        try:
            x = widget.winfo_rootx() + 10
            y = widget.winfo_rooty() + widget.winfo_height() + 4
            tip = tk.Toplevel(widget)
            tip.wm_overrideredirect(True)
            tip.wm_geometry(f"+{x}+{y}")
            tk.Label(
                tip, text=text, justify="left",
                background="#2b2b2b", foreground="#f0f0f0",
                relief="solid", borderwidth=1, padx=6, pady=3,
                font=("", 9),
            ).pack()
            state["tip"] = tip
        except Exception:
            state["tip"] = None

    def hide(_e=None):
        if state["tip"] is not None:
            try:
                state["tip"].destroy()
            except Exception:
                pass
            state["tip"] = None

    widget.bind("<Enter>", show, add="+")
    widget.bind("<Leave>", hide, add="+")


# Variables exposed in the in-app HTML editor's "Insert variable"
# toolbar. Display label → variable name (so users see the friendly
# name but the inserted `{{var}}` matches what the renderer accepts).
_TEMPLATE_INSERT_VARS_STUDENT = [
    ("First name", "first_name"),
    ("Preferred name", "preferred_name"),
    ("Last name", "last_name"),
    ("Full name", "full_name"),
    ("Student email", "student_email"),
    ("Student ID", "student_id"),
    ("Course code", "course_code"),
    ("Program name", "program_name"),
]
_TEMPLATE_INSERT_VARS_PM = [
    ("PM name", "pm_name"),
    ("PM email", "pm_email"),
]
_TEMPLATE_INSERT_VARS_USER = [
    ("Your name", "user_name"),
    ("Your email", "user_email"),
]


# CSV column names we'll look for when building student context
# from a caseload row (batch mode). Salesforce list-view exports
# include whatever columns the user has on their view, and the
# header names vary by configuration — these cover what we've
# seen in the wild. Tried in order; first non-empty match wins.
_CSV_STUDENT_EMAIL_COLS = [
    "StudentEmail", "Student Email", "studentemail", "stuemail",
    "PersonalEmail", "Personal Email", "Email",
]
_CSV_PM_EMAIL_COLS = [
    "MentorEmail", "Mentor Email", "mentoremail",
    "PMEmail", "PM Email",
    "ProgramMentorEmail", "Program Mentor Email",
]


_NAME_TITLES = frozenset({
    "dr", "mr", "mrs", "ms", "prof", "rev", "sir", "madam", "mx",
})


def _names_loosely_match(a: str, b: str) -> bool:
    """Tolerant first/last-name comparison. Strips common titles
    ('Dr.', 'Prof.', etc.), splits on whitespace + commas, lower-
    cases, and checks for ≥2-token overlap.

    Catches all the realistic shapes the same person's name takes
    across Salesforce vs Outlook: 'Jim Ashe' vs 'Ashe, Jim',
    'Dr. Jim Ashe' vs 'Jim Ashe', 'Jim Albert Ashe' vs 'Jim Ashe'
    all match. 'Jim Smith' vs 'Bob Smith' does NOT (one-token
    overlap)."""
    def _tokens(s: str) -> set[str]:
        out: set[str] = set()
        for raw in (s or "").replace(",", " ").split():
            t = raw.strip(".,()[]<>'\"").lower()
            if t and t not in _NAME_TITLES:
                out.add(t)
        return out
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    return len(ta & tb) >= 2


def _first_present_value(row: dict, candidates: list[str]) -> str:
    """Pick the first non-empty value among `candidates` (a list of
    possible column names). Returns "" if none of them exist or all
    are blank. Used to be robust against CSV column-naming variance
    without making the user remember the exact spelling."""
    for c in candidates:
        v = row.get(c, "")
        if v is not None:
            s = str(v).strip()
            if s:
                return s
    return ""


def _email_columns_present(row: dict) -> list[str]:
    """Return every column name in `row` that looks like an email
    column (case-insensitive 'email' substring). For diagnostic
    logging when the known names didn't match — tells the user
    which actual column header to add to our recognizer list."""
    return [k for k in row.keys() if "email" in k.lower()]


def _csv_has_student_email_column(rows: list[dict]) -> bool:
    """Return True iff the cached caseload rows include any
    student-email column the launcher knows how to read. Used by
    the pre-batch warning to detect when the Caseload Tool view
    hasn't been set up yet (and email lookup will have to fall
    back to per-student row-mailto + contact-card scraping)."""
    if not rows:
        return False
    headers = set(rows[0].keys())
    for alias in _CSV_STUDENT_EMAIL_COLS:
        if alias in headers:
            return True
    # Lowercase tolerance for orgs that use a non-standard casing
    # of "Email" (just shows up as "Email" / "email").
    lc = {h.lower() for h in headers}
    return "email" in lc


# Per-appearance-mode color palette for the HTML editor's syntax
# highlighter. Picked to read clearly on each background without
# requiring an exact theme match; close to VS Code defaults.
_HTML_HIGHLIGHT_COLORS: dict[str, dict[str, str]] = {
    "Dark": {
        "comment": "#6a9955",   # mossy green
        "tag":     "#569cd6",   # sky blue
        "value":   "#ce9178",   # warm orange
        "var_fg":  "#ffd966",   # gold
        "var_bg":  "#3a3520",   # dim amber
    },
    "Light": {
        "comment": "#008000",   # green
        "tag":     "#800080",   # purple — classic HTML-tag color
        "value":   "#a31515",   # dark red
        "var_fg":  "#0451a5",   # navy
        "var_bg":  "#fff3c4",   # pale yellow
    },
}

# Compiled regex patterns reused across every highlight pass.
# Comments come first (multi-line, can swallow `<`); tag pattern is
# non-greedy and matches up to the next `>`; var pattern matches
# `{{ name }}` with optional whitespace.
_HTML_HIGHLIGHT_PATTERNS: dict[str, re.Pattern] = {
    "comment": re.compile(r"<!--[\s\S]*?-->"),
    "tag":     re.compile(r"</?[a-zA-Z][^>]*?>", re.DOTALL),
    "var":     re.compile(r"\{\{\s*\w+\s*\}\}"),
    "value":   re.compile(r'"[^"]*"'),
}


def _open_in_edge(uri: str) -> bool:
    """Launch Microsoft Edge with `uri`. If Edge is already running,
    the URL opens as a new tab (standard Edge behavior on Windows);
    otherwise a fresh Edge process opens. Returns True on success.

    We try in order: explicit msedge.exe path → shell `start msedge`
    → fall through to the user's default browser. The standard-
    install paths cover the vast majority of Windows machines; the
    `start` fallback handles Edge installed somewhere unusual but
    still registered with the shell."""
    import subprocess
    edge_paths = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for exe in edge_paths:
        if Path(exe).exists():
            try:
                subprocess.Popen([exe, uri])
                return True
            except Exception:
                continue
    try:
        # `start` lets the shell resolve msedge from registered apps;
        # works for portable installs and non-default locations.
        subprocess.Popen(["cmd", "/c", "start", "", "msedge", uri], shell=False)
        return True
    except Exception:
        pass
    return False


def _open_externally(path: Path) -> tuple[bool, str]:
    """Open `path` in whatever app the OS has associated with the
    file type (`os.startfile` on Windows). Lets users pick their
    own HTML editor — set VS Code / Notepad++ / Sublime / etc. as
    the default for .html in Windows Settings and clicks here will
    route there. Returns (success, message)."""
    try:
        import os
        os.startfile(str(path))
        return True, "Opened in default app."
    except Exception as e:
        return False, f"Couldn't open file: {e}"


def _open_template_in_word(path: Path) -> tuple[bool, str]:
    """Launch MS Word (via COM) opened to `path`. Returns
    (success, message). Falls back gracefully when Word isn't
    installed — caller can fall back to os.startfile or just
    show the message.

    NOTE: kept for backward-compat and as an escape-hatch path,
    but the main editor flow now uses `_open_externally` which is
    more reliable and lets users pick any editor via the .html
    file association."""
    try:
        import win32com.client
        word = win32com.client.Dispatch("Word.Application")
        word.Documents.Open(str(path))
        word.Visible = True
        return True, "Opened in Word."
    except Exception as e:
        return False, f"Word not available: {e}"


def prompt_add_image_dialog(
    parent, templates_dir: Path,
) -> tuple[Optional[str], Optional[str]]:
    """Modal dialog for adding an inline (CID-embedded) image to a
    template. Walks the user through choosing a file, sizing it, and
    optionally linking it; on Insert it copies the file into the
    templates folder (if not already there) and builds an `<img
    src="cid:STEM">` snippet for the editor to drop at the cursor.

    Returns:
        (html_snippet, filename) on Insert — caller drops the
        snippet into the template AND registers `filename` in the
        scenario's inline_images list so the runtime knows to
        attach + bind the CID. Returns (None, None) on Cancel.

    Pillow is used opportunistically to read natural dimensions when
    the user picks a file, so width/height auto-populate."""
    from tkinter import messagebox, filedialog
    import shutil

    dialog = ctk.CTkToplevel(parent)
    dialog.title("Add image")
    dialog.transient(parent)
    dialog.attributes("-topmost", True)
    dialog.grab_set()
    result = {"html": None, "filename": None}
    state = {"src_path": None}

    # Row 1: source file picker
    file_row = ctk.CTkFrame(dialog, fg_color="transparent")
    file_row.pack(fill="x", padx=14, pady=(14, 4))
    ctk.CTkLabel(file_row, text="Image file:", width=80, anchor="w").pack(side="left")
    file_entry = ctk.CTkEntry(
        file_row, placeholder_text="click Browse…", width=320,
    )
    file_entry.pack(side="left", padx=(4, 4))

    def on_browse() -> None:
        path = filedialog.askopenfilename(
            parent=dialog,
            title="Choose image",
            filetypes=[
                ("Images", "*.png *.jpg *.jpeg *.gif *.bmp *.webp"),
                ("All files", "*.*"),
            ],
        )
        if not path:
            return
        p = Path(path)
        state["src_path"] = p
        file_entry.delete(0, "end")
        file_entry.insert(0, str(p))
        # Auto-fill width/height from the image's natural dimensions.
        # Failure (Pillow missing, file unreadable) is silent — the
        # user can still type values manually.
        try:
            from PIL import Image
            with Image.open(p) as im:
                w, h = im.size
            width_entry.delete(0, "end")
            width_entry.insert(0, str(w))
            height_entry.delete(0, "end")
            height_entry.insert(0, str(h))
        except Exception:
            pass

    ctk.CTkButton(
        file_row, text="Browse…", width=90, command=on_browse,
    ).pack(side="left", padx=(4, 0))

    # Row 2: dimensions
    dim_row = ctk.CTkFrame(dialog, fg_color="transparent")
    dim_row.pack(fill="x", padx=14, pady=4)
    ctk.CTkLabel(dim_row, text="Width:", width=80, anchor="w").pack(side="left")
    width_entry = ctk.CTkEntry(
        dim_row, placeholder_text="px (auto)", width=100,
    )
    width_entry.pack(side="left", padx=(4, 12))
    ctk.CTkLabel(dim_row, text="Height:").pack(side="left")
    height_entry = ctk.CTkEntry(
        dim_row, placeholder_text="px (auto)", width=100,
    )
    height_entry.pack(side="left", padx=(4, 0))

    # Row 3: alt text
    alt_row = ctk.CTkFrame(dialog, fg_color="transparent")
    alt_row.pack(fill="x", padx=14, pady=4)
    ctk.CTkLabel(alt_row, text="Alt text:", width=80, anchor="w").pack(side="left")
    alt_entry = ctk.CTkEntry(
        alt_row, placeholder_text="shown if image fails / for accessibility",
        width=380,
    )
    alt_entry.pack(side="left", padx=(4, 0))

    # Row 4: optional clickable link
    link_row = ctk.CTkFrame(dialog, fg_color="transparent")
    link_row.pack(fill="x", padx=14, pady=4)
    ctk.CTkLabel(link_row, text="Link to:", width=80, anchor="w").pack(side="left")
    link_entry = ctk.CTkEntry(
        link_row,
        placeholder_text="optional — clicking the image opens this URL",
        width=380,
    )
    link_entry.pack(side="left", padx=(4, 0))

    # Hint
    ctk.CTkLabel(
        dialog,
        text="The image gets copied to your templates folder and "
             "embedded via cid: so it travels with the email (no "
             "remote-image warning on the recipient's side).",
        font=ctk.CTkFont(size=11),
        text_color=("gray45", "gray65"),
        wraplength=480, justify="left",
    ).pack(padx=14, pady=(6, 4), anchor="w")

    btn_row = ctk.CTkFrame(dialog, fg_color="transparent")
    btn_row.pack(fill="x", padx=14, pady=(8, 14))

    def do_insert() -> None:
        src = state["src_path"]
        if not src or not Path(src).exists():
            messagebox.showerror(
                "No image selected",
                "Click Browse… and choose an image file first.",
                parent=dialog,
            )
            return
        target = templates_dir / src.name
        if src.resolve() != target.resolve():
            try:
                templates_dir.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    if not messagebox.askyesno(
                        "Overwrite?",
                        f"{src.name} already exists in your templates "
                        f"folder. Overwrite with the new file?",
                        parent=dialog,
                    ):
                        return
                shutil.copyfile(src, target)
            except Exception as e:
                messagebox.showerror(
                    "Copy failed",
                    f"Couldn't copy the image into the templates folder:\n\n{e}",
                    parent=dialog,
                )
                return
        cid = target.stem
        # html.escape would over-escape attribute values; for the
        # subset of chars that matter inside an attribute (`"`) a
        # simple replace is enough.
        def _attr(s: str) -> str:
            return s.replace("&", "&amp;").replace('"', "&quot;")
        attrs = [f'src="cid:{cid}"']
        alt = alt_entry.get().strip()
        if alt:
            attrs.append(f'alt="{_attr(alt)}"')
        w = width_entry.get().strip()
        if w:
            attrs.append(f'width="{_attr(w)}"')
        h = height_entry.get().strip()
        if h:
            attrs.append(f'height="{_attr(h)}"')
        attrs.append('style="display:block; border:0;"')
        img_tag = f"<img {' '.join(attrs)} />"
        link = link_entry.get().strip()
        if link:
            snippet = f'<p>\n  <a href="{_attr(link)}">\n    {img_tag}\n  </a>\n</p>'
        else:
            snippet = f"<p>{img_tag}</p>"
        result["html"] = snippet
        result["filename"] = target.name
        try: dialog.grab_release()
        except Exception: pass
        try: dialog.destroy()
        except Exception: pass

    def do_cancel() -> None:
        try: dialog.grab_release()
        except Exception: pass
        try: dialog.destroy()
        except Exception: pass

    ctk.CTkButton(
        btn_row, text="Insert", width=110, command=do_insert,
    ).pack(side="left", padx=4)
    ctk.CTkButton(
        btn_row, text="Cancel", width=90, command=do_cancel,
        **SECONDARY_BTN_KWARGS,
    ).pack(side="left", padx=4)
    dialog.protocol("WM_DELETE_WINDOW", do_cancel)
    dialog.bind("<Escape>", lambda _e: do_cancel())
    dialog.lift()
    dialog.focus_force()

    parent.wait_window(dialog)
    return result["html"], result["filename"]


def _extract_cf_html_fragment(raw: bytes) -> Optional[str]:
    """Pull the copied HTML fragment out of a Windows 'HTML Format' clipboard
    payload. The payload is a header (Version/StartHTML/StartFragment/… BYTE
    offsets) followed by HTML with <!--StartFragment-->…<!--EndFragment-->
    markers around the actual selection. Prefer the byte offsets (exact), fall
    back to the comment markers, then the whole thing."""
    try:
        header = raw[:256].decode("ascii", "replace")
        sf = re.search(r"StartFragment:(\d+)", header)
        ef = re.search(r"EndFragment:(\d+)", header)
        if sf and ef:
            frag = raw[int(sf.group(1)):int(ef.group(1))]
            return frag.decode("utf-8", "replace")
    except Exception:
        pass
    try:
        text = raw.decode("utf-8", "replace")
    except Exception:
        return None
    s = text.find("<!--StartFragment-->")
    e = text.find("<!--EndFragment-->")
    if s != -1 and e != -1:
        return text[s + len("<!--StartFragment-->"):e]
    return text or None


def _clipboard_html_fragment() -> Optional[str]:
    """The clipboard's rich-HTML form (with links/formatting), or None when
    the clipboard has no HTML (so callers fall back to plain-text paste).
    Windows-only via pywin32's win32clipboard (already a dependency)."""
    try:
        import win32clipboard as wc
    except Exception:
        return None
    try:
        wc.OpenClipboard()
    except Exception:
        return None
    try:
        cf = wc.RegisterClipboardFormat("HTML Format")
        if not wc.IsClipboardFormatAvailable(cf):
            return None
        raw = wc.GetClipboardData(cf)
    except Exception:
        return None
    finally:
        try:
            wc.CloseClipboard()
        except Exception:
            pass
    if isinstance(raw, str):
        raw = raw.encode("utf-8", "replace")
    if not isinstance(raw, (bytes, bytearray)):
        return None
    return _extract_cf_html_fragment(bytes(raw))


class _EditorHTMLParser(HTMLParser):
    """Parse simple HTML into a RichTextEditor's Tk Text with editable
    tags. Companion to RichTextEditor.to_html — handles paragraphs,
    headings, b/i/u, links, alignment, images, and (for round-tripping
    older templates) bullet/numbered lists rendered as plain lines."""

    def __init__(self, editor: "RichTextEditor", anchor: str = "end"):
        super().__init__()
        self.ed = editor
        self.t = editor.text
        # Where parsed content lands: "end" when building the whole document
        # (set_html), or "insert" to splice at the cursor (rich paste). `_here`
        # is the matching "current position" index — "end-1c" sits just before
        # the Text widget's permanent trailing newline; the insert mark needs
        # no such offset.
        self.anchor = anchor
        self._here = "end-1c" if anchor == "end" else "insert"
        self._inline: list[str] = []        # active bold/italic/underline
        self._link: Optional[str] = None    # active link tag name
        self._block_start: Optional[str] = None
        self._block: str = "p"              # p | h2
        self._align: str = "left"
        self._list: list[str] = []          # ul/ol nesting (round-trip only)
        self._ol_n: list[int] = []
        self._skip = 0
        self._pending_nl = False            # emit a newline before next block

    _SKIP = {"style", "script", "head", "title", "meta", "link"}

    def _begin_block(self, block: str, align: str) -> None:
        if self._pending_nl:
            self.t.insert(self.anchor, "\n")
        self._pending_nl = False
        self._block_start = self.t.index(self._here)
        self._block = block
        self._align = align

    def _end_block(self) -> None:
        if self._block_start is None:
            return
        end = self.t.index(self._here)
        if self._block == "h2":
            self.t.tag_add("h2", self._block_start, end)
        elif self._block in ("ul", "ol"):
            self.t.tag_add(self._block, self._block_start, end)
        if self._align == "center":
            self.t.tag_add("align_center", self._block_start, end)
        elif self._align == "right":
            self.t.tag_add("align_right", self._block_start, end)
        self._block_start = None
        self._pending_nl = True

    @staticmethod
    def _align_of(attrs: dict) -> str:
        style = (attrs.get("style", "") or "").lower()
        if "text-align:center" in style.replace(" ", ""):
            return "center"
        if "text-align:right" in style.replace(" ", ""):
            return "right"
        return "left"

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
            return
        if self._skip:
            return
        d = dict(attrs)
        if tag in ("p", "div"):
            # div is treated as a paragraph break — web/Outlook content uses
            # it per line; the pending-newline logic keeps siblings on
            # separate lines without breaking on benign nesting.
            self._begin_block("p", self._align_of(d))
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._begin_block("h2", self._align_of(d))
        elif tag == "br":
            self.t.insert(self.anchor, "\n")
        elif tag in ("b", "strong"):
            self._inline.append("bold")
        elif tag in ("i", "em"):
            self._inline.append("italic")
        elif tag == "u":
            self._inline.append("underline")
        elif tag == "a":
            self._link = self.ed._new_link(d.get("href", ""))
        elif tag in ("ul", "ol"):
            self._list.append(tag)
            self._ol_n.append(0)
        elif tag == "li":
            if self._pending_nl:
                self.t.insert(self.anchor, "\n")
            self._pending_nl = False
            self._block_start = self.t.index(self._here)
            kind = "ol" if (self._list and self._list[-1] == "ol") else "ul"
            self._block, self._align = kind, "left"
            mstart = self.t.index(self._here)
            if kind == "ol":
                self._ol_n[-1] += 1
                self.t.insert(self.anchor, "%d. " % self._ol_n[-1])
            else:
                self.t.insert(self.anchor, "• ")
            self.t.tag_add("listmarker", mstart, self._here)
        elif tag == "img":
            self.ed._insert_image_token(
                d.get("src", ""), pending_nl=self._pending_nl,
                anchor=self.anchor)
            self._pending_nl = True

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if tag in ("p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li"):
            self._end_block()
        elif tag in ("b", "strong"):
            self._pop("bold")
        elif tag in ("i", "em"):
            self._pop("italic")
        elif tag == "u":
            self._pop("underline")
        elif tag == "a":
            self._link = None
        elif tag in ("ul", "ol"):
            if self._list:
                self._list.pop()
                self._ol_n.pop()

    def _pop(self, name: str) -> None:
        for i in range(len(self._inline) - 1, -1, -1):
            if self._inline[i] == name:
                del self._inline[i]
                return

    def _insert(self, text: str) -> None:
        tags = tuple(self._inline) + ((self._link,) if self._link else ())
        self.t.insert(self.anchor, text, tags)

    def handle_data(self, data):
        if self._skip:
            return
        collapsed = re.sub(r"\s+", " ", data)
        if not collapsed.strip() and "\n" in data:
            return
        if collapsed:
            self._insert(collapsed)

    def handle_entityref(self, name):
        if not self._skip:
            self._insert(html.unescape(f"&{name};"))

    def handle_charref(self, name):
        if not self._skip:
            self._insert(html.unescape(f"&#{name};"))


class RichTextEditor:
    """Lightweight rich-text editor over tk.Text that round-trips to
    simple, email-friendly HTML. Block model is LINE-BASED: each logical
    line is one block — a paragraph by default, or a heading; alignment is
    a per-line attribute. Inline runs carry bold/italic/underline/link
    tags. {{vars}} are literal text; images are placeholder tokens that
    serialize back to `<img src="cid:…">`. (Lists are a later phase.)"""

    _INLINE = ("bold", "italic", "underline")

    def __init__(self, parent, base_size: int = 11,
                 on_add_image: Optional[Callable] = None,
                 on_insert_var=None):
        self.frame = ctk.CTkFrame(parent, fg_color="transparent")
        self.frame.grid_rowconfigure(1, weight=1)
        self.frame.grid_columnconfigure(0, weight=1)
        self._base_size = base_size
        self._link_seq = 0
        self._links: dict[str, str] = {}
        self._img_seq = 0
        self._imgs: dict[str, str] = {}
        self._on_add_image = on_add_image

        self._build_toolbar()

        wrap = ctk.CTkFrame(self.frame)
        wrap.grid(row=1, column=0, sticky="nsew")
        wrap.grid_rowconfigure(0, weight=1)
        wrap.grid_columnconfigure(0, weight=1)
        dark = ctk.get_appearance_mode() == "Dark"
        bg, fg = ("#2b2b2b", "#dce4ee") if dark else ("#ffffff", "#1a1a1a")
        self.text = tk.Text(
            wrap, wrap="word", undo=True, borderwidth=0,
            font=("Segoe UI", base_size), padx=10, pady=8,
            background=bg, foreground=fg, insertbackground=fg,
            spacing3=4,
        )
        self.text.grid(row=0, column=0, sticky="nsew")
        vsb = ttk.Scrollbar(wrap, command=self.text.yview)
        vsb.grid(row=0, column=1, sticky="ns")
        self.text.configure(yscrollcommand=vsb.set)
        self._configure_tags()
        # Enter continues/exits a list; Space drives Markdown shortcuts.
        self.text.bind("<Return>", self._on_return)
        self.text.bind("<KeyPress-space>", self._on_space)
        # Rich paste: keep links/bold/etc. when the clipboard carries HTML.
        self.text.bind("<<Paste>>", self._on_paste)

    # ----- setup -----

    def _configure_tags(self) -> None:
        base = tkfont.Font(family="Segoe UI", size=self._base_size)
        self.text.tag_configure(
            "bold", font=tkfont.Font(
                family="Segoe UI", size=self._base_size, weight="bold"))
        self.text.tag_configure(
            "italic", font=tkfont.Font(
                family="Segoe UI", size=self._base_size, slant="italic"))
        self.text.tag_configure(
            "underline", font=tkfont.Font(
                family="Segoe UI", size=self._base_size, underline=True))
        self.text.tag_configure(
            "h2", font=tkfont.Font(
                family="Segoe UI", size=self._base_size + 6, weight="bold"),
            spacing1=8, spacing3=4)
        self.text.tag_configure("align_center", justify="center")
        self.text.tag_configure("align_right", justify="right")
        self.text.tag_configure("ul", lmargin1=22, lmargin2=38)
        self.text.tag_configure("ol", lmargin1=22, lmargin2=38)
        self.text.tag_configure("listmarker")  # marks bullet/number prefix
        self.text.tag_configure(
            "image", foreground=("#1f6aa5" if ctk.get_appearance_mode() ==
                                 "Dark" else "#3a7ebf"))
        del base

    def _build_toolbar(self) -> None:
        bar = ctk.CTkFrame(self.frame, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 4))

        def btn(text, cmd, w=34):
            return ctk.CTkButton(
                bar, text=text, width=w, height=26, command=cmd,
                font=ctk.CTkFont(size=12), **SECONDARY_BTN_KWARGS)

        btn("B", lambda: self._toggle_inline("bold"), 30).pack(
            side="left", padx=1)
        btn("I", lambda: self._toggle_inline("italic"), 30).pack(
            side="left", padx=1)
        btn("U", lambda: self._toggle_inline("underline"), 30).pack(
            side="left", padx=1)
        btn("Heading", self._toggle_heading, 64).pack(side="left", padx=(8, 1))
        btn("• List", lambda: self._toggle_list("ul"), 52).pack(
            side="left", padx=(6, 1))
        btn("1. List", lambda: self._toggle_list("ol"), 56).pack(
            side="left", padx=1)
        ctk.CTkLabel(
            bar, text="Align:", font=ctk.CTkFont(size=11),
        ).pack(side="left", padx=(8, 2))
        btn("Left", lambda: self._set_align("left"), 48).pack(
            side="left", padx=1)
        btn("Center", lambda: self._set_align("center"), 58).pack(
            side="left", padx=1)
        btn("Right", lambda: self._set_align("right"), 52).pack(
            side="left", padx=1)
        btn("🔗 Link", self._add_link, 64).pack(side="left", padx=(8, 1))
        if self._on_add_image:
            btn("🖼 Image", lambda: self._on_add_image(), 70).pack(
                side="left", padx=1)

    # ----- formatting actions -----

    def _sel_range(self):
        try:
            return self.text.index("sel.first"), self.text.index("sel.last")
        except tk.TclError:
            return None

    def _toggle_inline(self, tag: str) -> None:
        rng = self._sel_range()
        if not rng:
            return
        a, b = rng
        # If every char already has the tag, remove it; else add it.
        on = all(tag in self.text.tag_names(f"{a}+{i}c")
                 for i in range(self._span(a, b)))
        if on:
            self.text.tag_remove(tag, a, b)
        else:
            self.text.tag_add(tag, a, b)
        self.text.focus_set()

    def _span(self, a: str, b: str) -> int:
        return max(0, len(self.text.get(a, b)))

    def _line_range(self):
        sel = self._sel_range()
        if sel:
            first = int(sel[0].split(".")[0])
            last = int(sel[1].split(".")[0])
        else:
            first = last = int(self.text.index("insert").split(".")[0])
        return first, last

    def _line_block_kind(self, ln: int) -> Optional[str]:
        names = self.text.tag_names(f"{ln}.0")
        for k in ("h2", "ul", "ol"):
            if k in names:
                return k
        return None

    def _strip_marker(self, ln: int) -> None:
        """Remove a leading bullet/number marker from a list line."""
        a = f"{ln}.0"
        rng = self.text.tag_nextrange("listmarker", a, f"{ln}.end")
        if rng and self.text.compare(rng[0], "==", a):
            self.text.delete(rng[0], rng[1])

    def _set_line_block(self, ln: int, block: Optional[str]) -> None:
        """Set a line's block type: None (paragraph), 'h2', 'ul', or 'ol'.
        Markers for list lines are (re)generated by _renumber_lists."""
        a, b = f"{ln}.0", f"{ln + 1}.0"
        self._strip_marker(ln)
        for t in ("h2", "ul", "ol"):
            self.text.tag_remove(t, a, b)
        if block in ("h2", "ul", "ol"):
            self.text.tag_add(block, a, b)

    def _renumber_lists(self) -> None:
        """Rewrite every list line's marker: '• ' for bullets, sequential
        '1. 2. …' for numbered runs (restarting after any break)."""
        last = int(self.text.index("end-1c").split(".")[0])
        prev = None
        n = 0
        for ln in range(1, last + 1):
            kind = self._line_block_kind(ln)
            kind = kind if kind in ("ul", "ol") else None
            if kind == "ol":
                n = (n + 1) if prev == "ol" else 1
            prev = kind
            if kind:
                self._strip_marker(ln)
                marker = (f"{n}. " if kind == "ol" else "• ")
                self.text.insert(f"{ln}.0", marker)
                self.text.tag_add(
                    "listmarker", f"{ln}.0", f"{ln}.0 + {len(marker)}c")
                self.text.tag_add(kind, f"{ln}.0", f"{ln + 1}.0")

    def _toggle_heading(self) -> None:
        first, last = self._line_range()
        all_h2 = all(self._line_block_kind(ln) == "h2"
                     for ln in range(first, last + 1))
        for ln in range(first, last + 1):
            self._set_line_block(ln, None if all_h2 else "h2")
        self.text.focus_set()

    def _toggle_list(self, kind: str) -> None:
        first, last = self._line_range()
        all_kind = all(self._line_block_kind(ln) == kind
                       for ln in range(first, last + 1))
        for ln in range(first, last + 1):
            self._set_line_block(ln, None if all_kind else kind)
        self._renumber_lists()
        self.text.focus_set()

    def _on_return(self, event=None):
        """Inside a list: Enter starts a new item; Enter on an empty item
        leaves the list. Elsewhere: default newline (new paragraph)."""
        ln = int(self.text.index("insert").split(".")[0])
        kind = self._line_block_kind(ln)
        if kind not in ("ul", "ol"):
            return  # default
        line = self.text.get(f"{ln}.0", f"{ln}.end")
        content = re.sub(r"^(?:•\s*|\d+\.\s*)", "", line)
        if not content.strip():
            self._set_line_block(ln, None)
            self._renumber_lists()
            return "break"
        self.text.insert("insert", "\n")
        newln = int(self.text.index("insert").split(".")[0])
        self.text.tag_add(kind, f"{newln}.0", f"{newln + 1}.0")
        self._renumber_lists()
        self.text.see("insert")
        return "break"

    def _on_space(self, event=None):
        """Markdown shortcuts on the space key. Line-start: '# '→heading,
        '- '/'* '/'+ '→bullets, '1. '→numbered. Inline (token then space):
        **bold**, *italic* / _italic_, [text](url)."""
        ln, col = map(int, self.text.index("insert").split("."))
        before = self.text.get(f"{ln}.0", "insert")
        if before == "#":
            self.text.delete(f"{ln}.0", "insert")
            self._set_line_block(ln, "h2")
            return "break"
        if before in ("-", "*", "+"):
            self.text.delete(f"{ln}.0", "insert")
            self._set_line_block(ln, "ul")
            self._renumber_lists()
            return "break"
        if re.fullmatch(r"\d+\.", before):
            self.text.delete(f"{ln}.0", "insert")
            self._set_line_block(ln, "ol")
            self._renumber_lists()
            return "break"
        # Inline tokens ending right at the cursor.
        for pat, tag in (
            (r"\*\*([^*]+)\*\*$", "bold"),
            (r"__([^_]+)__$", "bold"),
            (r"(?<!\*)\*([^*\s][^*]*)\*$", "italic"),
            (r"(?<!_)_([^_\s][^_]*)_$", "italic"),
        ):
            m = re.search(pat, before)
            if m:
                a = f"{ln}.{col - len(m.group(0))}"
                self.text.delete(a, "insert")
                self.text.insert(a, m.group(1), (tag,))
                return  # let the space type normally after the run
        m = re.search(r"\[([^\]]+)\]\(([^)]+)\)$", before)
        if m:
            a = f"{ln}.{col - len(m.group(0))}"
            link_tag = self._new_link(m.group(2))
            self.text.delete(a, "insert")
            self.text.insert(a, m.group(1), (link_tag,))
            return
        return

    def _set_align(self, how: str) -> None:
        first, last = self._line_range()
        for ln in range(first, last + 1):
            # Tk's `justify` only takes effect when the tag spans the full
            # line INCLUDING its newline — hence {ln}.0 .. {ln+1}.0.
            a, b = f"{ln}.0", f"{ln + 1}.0"
            self.text.tag_remove("align_center", a, b)
            self.text.tag_remove("align_right", a, b)
            if how == "center":
                self.text.tag_add("align_center", a, b)
            elif how == "right":
                self.text.tag_add("align_right", a, b)
        self.text.focus_set()

    def _add_link(self) -> None:
        rng = self._sel_range()
        if not rng:
            from tkinter import messagebox
            messagebox.showinfo(
                "Add link", "Select the text to turn into a link first.")
            return
        url = ctk.CTkInputDialog(
            text="Link URL (https://… or mailto:…):", title="Add link").get_input()
        if not url:
            return
        tag = self._new_link(url.strip())
        self.text.tag_add(tag, rng[0], rng[1])
        self.text.focus_set()

    def _new_link(self, href: str) -> str:
        self._link_seq += 1
        tag = f"link#{self._link_seq}"
        self._links[tag] = href
        dark = ctk.get_appearance_mode() == "Dark"
        self.text.tag_configure(
            tag, foreground=("#79b8ff" if dark else "#1a73e8"),
            underline=True)
        return tag

    def insert_text(self, text: str) -> None:
        self.text.insert("insert", text)
        self.text.focus_set()

    def _insert_image_token(self, src: str, pending_nl: bool = False,
                            anchor: str = "end") -> None:
        here = "end-1c" if anchor == "end" else "insert"
        if pending_nl:
            self.text.insert(anchor, "\n")
        stem = src[4:] if src.startswith("cid:") else src.rsplit("/", 1)[-1]
        self._img_seq += 1
        tag = f"img#{self._img_seq}"
        self._imgs[tag] = src
        start = self.text.index(here)
        self.text.insert(anchor, f"🖼 {stem}")
        self.text.tag_add("image", start, here)
        self.text.tag_add(tag, start, here)

    def insert_image(self, src: str) -> None:
        """Insert an image placeholder at a fresh line near the cursor."""
        self.text.insert("insert", "\n")
        stem = src[4:] if src.startswith("cid:") else src.rsplit("/", 1)[-1]
        self._img_seq += 1
        tag = f"img#{self._img_seq}"
        self._imgs[tag] = src if src.startswith("cid:") else f"cid:{stem}"
        start = self.text.index("insert")
        self.text.insert("insert", f"🖼 {stem}")
        self.text.tag_add("image", start, "insert")
        self.text.tag_add(tag, start, "insert")
        self.text.insert("insert", "\n")
        self.text.focus_set()

    # ----- HTML <-> editor -----

    def set_html(self, html_text: str) -> None:
        self.text.delete("1.0", "end")
        for t in list(self._links):
            self._links.pop(t, None)
        self._imgs.clear()
        self._link_seq = self._img_seq = 0
        parser = _EditorHTMLParser(self)
        try:
            parser.feed(html_text or "")
            parser.close()
        except Exception:
            # Fall back to dropping the raw text in unstyled.
            self.text.insert("1.0", html_text or "")
        # Trim a leading blank line the block logic may have produced.
        if self.text.get("1.0", "1.end").strip() == "" and \
                int(self.text.index("end-1c").split(".")[0]) > 1:
            self.text.delete("1.0", "2.0")
        self._renumber_lists()  # normalize bullet/number markers
        self.text.edit_reset()

    def _on_paste(self, event=None):
        """Paste rich content (links, bold, etc.) when the clipboard carries
        HTML, by parsing it in at the cursor. With no HTML on the clipboard,
        returns None so Tk's default plain-text paste runs unchanged."""
        frag = _clipboard_html_fragment()
        if not frag or not frag.strip():
            return None  # plain text only -> let the default <<Paste>> proceed
        try:
            if self.text.tag_ranges("sel"):
                self.text.delete("sel.first", "sel.last")
        except Exception:
            pass
        parser = _EditorHTMLParser(self, anchor="insert")
        try:
            parser.feed(frag)
            parser.close()
        except Exception:
            pass  # best-effort: whatever parsed before the error stays
        self._renumber_lists()
        try:
            self.text.see("insert")
        except Exception:
            pass
        self.text.focus_set()
        return "break"  # consumed — skip the default plain-text paste

    def _inline_key_at(self, idx: str):
        names = self.text.tag_names(idx)
        inline = tuple(n for n in self._INLINE if n in names)
        link = next((n for n in names if n.startswith("link#")), None)
        return inline, link

    def _img_tag_on_line(self, ln: int) -> Optional[str]:
        for n in self.text.tag_names(f"{ln}.0"):
            if n.startswith("img#"):
                return n
        return None

    def _serialize_line(self, ln: int) -> str:
        line = self.text.get(f"{ln}.0", f"{ln}.end")
        runs = []  # (text, (inline_tuple, link))
        for col, ch in enumerate(line):
            key = self._inline_key_at(f"{ln}.{col}")
            if runs and runs[-1][1] == key:
                runs[-1][0].append(ch)
            else:
                runs.append(([ch], key))
        out = []
        for chars, (inline, link) in runs:
            text = html.escape("".join(chars))
            opens, closes = "", ""
            if link:
                href = html.escape(self._links.get(link, ""), quote=True)
                opens += f'<a href="{href}">'
                closes = "</a>" + closes
            for t, (o, c) in (("bold", ("<b>", "</b>")),
                              ("italic", ("<i>", "</i>")),
                              ("underline", ("<u>", "</u>"))):
                if t in inline:
                    opens += o
                    closes = c + closes
            out.append(opens + text + closes)
        return "".join(out)

    def to_html(self) -> str:
        blocks = []
        last = int(self.text.index("end-1c").split(".")[0])
        ln = 1
        while ln <= last:
            img_tag = self._img_tag_on_line(ln)
            if img_tag:
                src = self._imgs.get(img_tag, "")
                if src:
                    blocks.append(f'<img src="{html.escape(src, quote=True)}">')
                ln += 1
                continue
            names0 = self.text.tag_names(f"{ln}.0")
            if "ul" in names0 or "ol" in names0:
                kind = "ul" if "ul" in names0 else "ol"
                items = []
                while ln <= last and kind in self.text.tag_names(f"{ln}.0"):
                    raw = self.text.get(f"{ln}.0", f"{ln}.end")
                    if raw.strip():
                        inner = re.sub(r"^(?:•\s*|\d+\.\s*)", "",
                                       self._serialize_line(ln))
                        items.append(f"<li>{inner}</li>")
                    ln += 1
                if items:
                    blocks.append(f"<{kind}>" + "".join(items) + f"</{kind}>")
                continue
            raw = self.text.get(f"{ln}.0", f"{ln}.end")
            if not raw.strip():
                ln += 1
                continue
            inner = self._serialize_line(ln)
            align = ("center" if "align_center" in names0
                     else "right" if "align_right" in names0 else "")
            style = f' style="text-align:{align}"' if align else ""
            if "h2" in names0:
                blocks.append(f"<h2{style}>{inner}</h2>")
            else:
                blocks.append(f"<p{style}>{inner}</p>")
            ln += 1
        return "\n".join(blocks) + ("\n" if blocks else "")


def prompt_html_template_editor(
    parent,
    path: Path,
    custom_var_names: Optional[list[str]] = None,
    on_image_added: Optional[Callable[[str], None]] = None,
) -> bool:
    """Modal HTML editor for an email body template. Returns True if
    the file was saved (Save, Save as, or Save & Open in MS Word).

    Features:
    - Toolbar with "Insert variable" buttons grouped by category;
      one click drops the corresponding `{{var}}` at the cursor.
    - Font family + size dropdowns (display-only — affects the
      editor view, not the rendered email which uses its own CSS /
      Outlook's defaults).
    - Ctrl+MouseWheel and Ctrl+= / Ctrl+- to zoom.
    - Save as button for cloning a template under a new name.
    - Save & Open in MS Word opens via COM, falls back to the OS
      default if Word isn't installed."""
    from tkinter import messagebox

    dialog = ctk.CTkToplevel(parent)
    dialog.title(f"Edit template — {path.name}")
    _restore_dialog_geometry(dialog, "html_template_editor")
    dialog.transient(parent)
    dialog.attributes("-topmost", True)
    dialog.grab_set()

    # Holders so closures can mutate. `current["path"]` lets Save-as
    # change which file subsequent Saves write to.
    saved = {"value": False}
    # Editor font is Segoe UI (Windows system font, readable for
    # prose + light HTML). Not user-configurable — fewer knobs to
    # tune. The size is configurable for accessibility / quick zoom.
    current = {
        "path": Path(path),
        "font_family": "Segoe UI",
        "font_size": font_size("editor"),  # seed from the 'editor' channel
    }

    # ---- Toolbar — insert-variable rows + font/size selectors. ----
    toolbar = ctk.CTkFrame(dialog, fg_color="transparent")
    toolbar.pack(fill="x", padx=8, pady=(8, 0))

    # Edit mode: "rich" (WYSIWYG) or "html" (raw source). Defaults to
    # rich for a friendly start; existing templates load into it and the
    # user can flip to HTML for fine control.
    mode = {"value": "rich"}

    mode_row = ctk.CTkFrame(toolbar, fg_color="transparent")
    mode_row.pack(fill="x", pady=(0, 2))
    mode_btn = ctk.CTkButton(
        mode_row, text="</> Edit HTML", width=130, height=26,
        command=lambda: _toggle_mode(),
        font=ctk.CTkFont(size=11), **SECONDARY_BTN_KWARGS,
    )
    mode_btn.pack(side="left")
    ctk.CTkLabel(
        mode_row,
        text="  Rich editor — select text, then B / I / U, H, align, 🔗. "
             "Switch to HTML for raw control.",
        font=ctk.CTkFont(size=10), text_color=("gray45", "gray65"),
    ).pack(side="left", padx=(6, 0))

    def insert_var(var: str) -> None:
        token = f"{{{{{var}}}}}"
        if mode["value"] == "rich":
            rich.insert_text(token)
        else:
            text_box.insert("insert", token)
            text_box.focus_force()

    def _make_row(label: str, items: list[tuple[str, str]],
                  highlight: bool = False):
        row = ctk.CTkFrame(toolbar, fg_color="transparent")
        row.pack(fill="x", pady=2)
        ctk.CTkLabel(
            row, text=label, width=70, anchor="w",
            font=ctk.CTkFont(size=11, weight="bold"),
        ).pack(side="left", padx=(0, 4))
        for display, var in items:
            kwargs = dict(SECONDARY_BTN_KWARGS)
            if highlight:
                kwargs["fg_color"] = ("#fff3c4", "#5a4500")
                kwargs["text_color"] = ("gray10", "gray95")
            ctk.CTkButton(
                row, text=display, width=92, height=24,
                command=lambda v=var: insert_var(v),
                font=ctk.CTkFont(size=11),
                **kwargs,
            ).pack(side="left", padx=2)

    _make_row("Student:", _TEMPLATE_INSERT_VARS_STUDENT)
    _make_row(
        "Mentor:",
        _TEMPLATE_INSERT_VARS_PM + _TEMPLATE_INSERT_VARS_USER,
    )
    if custom_var_names:
        _make_row(
            "Variables:",
            [(v, v) for v in custom_var_names],
            highlight=True,
        )

    # Insert-image button row. Opens the image dialog, copies the
    # picked file into templates_dir() (if not already there), drops a
    # `<img src="cid:STEM">` snippet at the cursor, and registers
    # the filename on the scenario's inline_images list via the
    # `on_image_added` callback.
    insert_row = ctk.CTkFrame(toolbar, fg_color="transparent")
    insert_row.pack(fill="x", pady=(6, 2))
    ctk.CTkLabel(
        insert_row, text="Insert:", width=70, anchor="w",
        font=ctk.CTkFont(size=11, weight="bold"),
    ).pack(side="left", padx=(0, 4))

    def on_add_image() -> None:
        html, filename = prompt_add_image_dialog(dialog, templates_dir())
        if html:
            if mode["value"] == "rich":
                stem = Path(filename).stem if filename else ""
                rich.insert_image(f"cid:{stem}" if stem else html)
            else:
                text_box.insert("insert", "\n" + html + "\n")
                text_box.focus_force()
            if on_image_added and filename:
                try:
                    on_image_added(filename)
                except Exception:
                    pass

    ctk.CTkButton(
        insert_row, text="🖼  Add image…", height=24,
        command=on_add_image,
        font=ctk.CTkFont(size=11),
        **SECONDARY_BTN_KWARGS,
    ).pack(side="left", padx=2)

    # View-size row. EDITOR VIEW ONLY — the sent email's font/size is
    # set per-scenario in the email section of the editor (so it
    # applies to every fire), not here.
    size_row = ctk.CTkFrame(toolbar, fg_color="transparent")
    size_row.pack(fill="x", pady=(6, 2))
    ctk.CTkLabel(
        size_row, text="View size:", width=70, anchor="w",
        font=ctk.CTkFont(size=11, weight="bold"),
    ).pack(side="left", padx=(0, 4))

    def apply_font() -> None:
        n = current["font_size"]
        try:
            text_box.configure(font=ctk.CTkFont(
                family=current["font_family"], size=n))
        except Exception:
            pass
        try:
            rich.set_base_size(n)  # resize the rich view + its tag fonts
        except Exception:
            pass
        # Drive the shared 'editor' channel (note bodies + persistence).
        try:
            set_font_size("editor", n)
        except Exception:
            pass

    def on_size_change(value: str) -> None:
        try:
            current["font_size"] = max(8, min(40, int(value)))
        except (ValueError, TypeError):
            return
        apply_font()

    size_combo = ctk.CTkComboBox(
        size_row, values=["8", "10", "11", "12", "13", "14", "16", "18", "22"],
        width=70, command=on_size_change,
    )
    size_combo.set(str(current["font_size"]))
    size_combo.pack(side="left")
    ctk.CTkLabel(
        size_row,
        text="(editor view only — set the sent email's font in the "
             "action's email section)",
        font=ctk.CTkFont(size=10), text_color=("gray45", "gray65"),
    ).pack(side="left", padx=(10, 0))

    # ---- Action row (created + reserved at the BOTTOM now, populated
    # later). Packing it before the editor guarantees the buttons keep
    # their space even though the editor area expands to fill. ----
    btn_row = ctk.CTkFrame(dialog, fg_color="transparent")
    btn_row.pack(fill="x", padx=8, pady=(0, 8), side="bottom")

    # ---- Text editor. ----
    text_box = ctk.CTkTextbox(
        dialog, wrap="word",
        font=ctk.CTkFont(
            family=current["font_family"], size=current["font_size"],
        ),
    )
    text_box.pack(fill="both", expand=True, padx=8, pady=6)
    try:
        content = current["path"].read_text(encoding="utf-8")
    except FileNotFoundError:
        content = ""
    except Exception as e:
        content = f"<!-- failed to load: {e} -->"
    text_box.insert("1.0", content)
    text_box.focus_force()
    text_box.mark_set("insert", "1.0")

    # ---- Syntax highlighting. ----
    # CTkTextbox wraps a tk.Text — its tag system handles colored
    # ranges. We do a full re-tag on every edit (debounced ~120ms)
    # rather than incremental scanning; for template-sized buffers
    # this is well under a millisecond per pass and avoids tricky
    # boundary bookkeeping when text is inserted via the variable
    # buttons.
    inner_text = text_box._textbox
    highlight_after = {"id": None}

    def apply_highlighting() -> None:
        mode = ctk.get_appearance_mode()
        colors = _HTML_HIGHLIGHT_COLORS.get(
            mode, _HTML_HIGHLIGHT_COLORS["Light"],
        )
        # tag_configure is idempotent; re-applying same color is fine
        # and lets us pick up appearance-mode changes for free.
        inner_text.tag_configure("html_comment", foreground=colors["comment"])
        inner_text.tag_configure("html_tag",     foreground=colors["tag"])
        inner_text.tag_configure("html_value",   foreground=colors["value"])
        inner_text.tag_configure(
            "html_var",
            foreground=colors["var_fg"], background=colors["var_bg"],
        )
        for tag in ("html_comment", "html_tag", "html_value", "html_var"):
            inner_text.tag_remove(tag, "1.0", "end")

        buf = inner_text.get("1.0", "end-1c")
        if not buf:
            return

        def _idx(off: int) -> str:
            return inner_text.index(f"1.0 + {off} chars")

        # Pass 1 — comments. Done first because they can legally
        # contain `<` / `>` / `{{` and shouldn't be re-colored by
        # later passes.
        comment_ranges: list[tuple[int, int]] = []
        for m in _HTML_HIGHLIGHT_PATTERNS["comment"].finditer(buf):
            comment_ranges.append((m.start(), m.end()))
            inner_text.tag_add("html_comment", _idx(m.start()), _idx(m.end()))

        def _in_comment(pos: int) -> bool:
            return any(a <= pos < b for a, b in comment_ranges)

        # Pass 2 — tags + their quoted attribute values. We do values
        # nested per-tag-match so a stray `"…"` in body text doesn't
        # get colored as if it were an attribute value.
        for m in _HTML_HIGHLIGHT_PATTERNS["tag"].finditer(buf):
            if _in_comment(m.start()):
                continue
            inner_text.tag_add("html_tag", _idx(m.start()), _idx(m.end()))
            tag_text = m.group(0)
            for vm in _HTML_HIGHLIGHT_PATTERNS["value"].finditer(tag_text):
                inner_text.tag_add(
                    "html_value",
                    _idx(m.start() + vm.start()),
                    _idx(m.start() + vm.end()),
                )

        # Pass 3 — template variables.
        for m in _HTML_HIGHLIGHT_PATTERNS["var"].finditer(buf):
            if _in_comment(m.start()):
                continue
            inner_text.tag_add("html_var", _idx(m.start()), _idx(m.end()))

    def on_text_modified(_event=None) -> None:
        # Tk fires <<Modified>> exactly once when the modified flag
        # flips from False → True. Reset it here so subsequent edits
        # fire again. Debounce so a long paste doesn't trigger N
        # re-passes mid-paste.
        if not inner_text.edit_modified():
            return
        inner_text.edit_modified(False)
        aid = highlight_after["id"]
        if aid is not None:
            try: text_box.after_cancel(aid)
            except Exception: pass
        highlight_after["id"] = text_box.after(120, apply_highlighting)

    inner_text.bind("<<Modified>>", on_text_modified)
    # Initial pass — the loaded buffer needs coloring before the user
    # types anything. Run after a 0ms tick so the textbox is fully
    # laid out (tag_add can no-op against a not-yet-laid-out widget).
    text_box.after(0, apply_highlighting)

    # ---- Rich-text editor (alternate view, default). ----
    rich = RichTextEditor(
        dialog, base_size=current["font_size"], on_add_image=on_add_image)

    def _toggle_mode() -> None:
        if mode["value"] == "rich":
            # Rich → HTML: serialize and show the raw editor.
            try:
                html_src = rich.to_html()
            except Exception as e:
                messagebox.showerror("Switch to HTML failed", str(e))
                return
            text_box.delete("1.0", "end")
            text_box.insert("1.0", html_src)
            rich.frame.pack_forget()
            # Pack AFTER btn_row (already reserved at the bottom) so the
            # expanding editor fills above it and never squeezes it out.
            text_box.pack(fill="both", expand=True, padx=8, pady=6)
            mode["value"] = "html"
            mode_btn.configure(text="✏ Rich editor")
            text_box.after(0, apply_highlighting)
            text_box.focus_set()
        else:
            # HTML → Rich: parse the raw source into the rich view.
            try:
                rich.set_html(text_box.get("1.0", "end-1c"))
            except Exception as e:
                messagebox.showerror("Switch to rich editor failed", str(e))
                return
            text_box.pack_forget()
            rich.frame.pack(fill="both", expand=True, padx=8, pady=6)
            mode["value"] = "rich"
            mode_btn.configure(text="</> Edit HTML")
            rich.text.focus_set()

    # Start in rich mode — UNLESS the template has a table (still not
    # round-trippable), in which case open in HTML so saving can't drop it.
    # Lists ARE supported now, so they open in rich.
    start_rich = not re.search(r"<\s*table\b", content or "", re.IGNORECASE)
    if start_rich:
        try:
            rich.set_html(content)
            text_box.pack_forget()
            rich.frame.pack(fill="both", expand=True, padx=8, pady=6)
            mode_btn.configure(text="</> Edit HTML")
        except Exception:
            start_rich = False
    if not start_rich:
        # Stay in HTML mode (text_box already packed); offer rich opt-in.
        mode["value"] = "html"
        mode_btn.configure(text="✏ Rich editor")

    def _active_html() -> str:
        """Current template HTML from whichever view is active."""
        if mode["value"] == "rich":
            return rich.to_html()
        return text_box.get("1.0", "end-1c")

    # Ctrl+MouseWheel and Ctrl+= / Ctrl+- to zoom the editor view.
    def zoom(delta: int) -> str:
        new_size = max(8, min(40, current["font_size"] + delta))
        if new_size != current["font_size"]:
            current["font_size"] = new_size
            size_combo.set(str(new_size))
            apply_font()
        return "break"  # prevent default scroll

    text_box.bind(
        "<Control-MouseWheel>",
        lambda e: zoom(1 if e.delta > 0 else -1),
    )
    text_box.bind("<Control-plus>", lambda _e: zoom(1))
    text_box.bind("<Control-equal>", lambda _e: zoom(1))  # Ctrl+= (no shift)
    text_box.bind("<Control-minus>", lambda _e: zoom(-1))

    # ---- Action row. ----
    def write_to_disk(target_path: Optional[Path] = None) -> bool:
        tgt = target_path or current["path"]
        try:
            tgt.parent.mkdir(parents=True, exist_ok=True)
            tgt.write_text(_active_html(), encoding="utf-8")
            return True
        except Exception as e:
            messagebox.showerror("Save failed", str(e))
            return False

    def do_save() -> None:
        if write_to_disk():
            saved["value"] = True
            _save_dialog_geometry(dialog, "html_template_editor")
            try: dialog.grab_release()
            except Exception: pass
            try: dialog.destroy()
            except Exception: pass

    def do_save_as() -> None:
        sub = ctk.CTkInputDialog(
            text="Save current content under filename (without .html):",
            title="Save as",
        )
        raw = sub.get_input()
        if not raw or not raw.strip():
            return
        new_name = raw.strip()
        if not new_name.lower().endswith(".html"):
            new_name += ".html"
        new_path = current["path"].parent / new_name
        if new_path.exists():
            if not messagebox.askyesno(
                "Overwrite?",
                f"{new_name} already exists. Overwrite?",
            ):
                return
        if not write_to_disk(new_path):
            return
        # Switch the editor's working file to the new path so further
        # Saves go there too.
        current["path"] = new_path
        dialog.title(f"Edit template — {new_path.name}")
        saved["value"] = True
        messagebox.showinfo("Saved", f"Saved as {new_name}.")

    def do_open_externally() -> None:
        """Save buffer + hand the file to whatever app Windows has
        associated with .html (VS Code if user sets it, Notepad++,
        Sublime, etc.). Replaces the previous Word-via-COM flow,
        which was unreliable for some users."""
        if not write_to_disk():
            return
        saved["value"] = True
        ok, msg = _open_externally(current["path"])
        if not ok:
            messagebox.showerror("Couldn't open file", msg)
            return
        _save_dialog_geometry(dialog, "html_template_editor")
        try: dialog.grab_release()
        except Exception: pass
        try: dialog.destroy()
        except Exception: pass

    def do_cancel() -> None:
        _save_dialog_geometry(dialog, "html_template_editor")
        try: dialog.grab_release()
        except Exception: pass
        try: dialog.destroy()
        except Exception: pass

    def do_preview() -> None:
        """Render the current buffer to a temp HTML file and open it
        in the default browser. Read-only — for layout / styling
        review only. `{{var}}` placeholders are swapped for human-
        readable `<LABEL>` text so the user can see where values
        will land. CID image references are rewritten to relative
        filenames so the browser can load images from templates_dir()
        (where the preview itself is written)."""
        import webbrowser
        import re as _re
        buffer = _active_html()
        rendered = email_template.render_with_placeholders(buffer)

        def _fix_cid(m: _re.Match) -> str:
            stem = m.group(1)
            for f in sorted(templates_dir().glob(f"{stem}.*")):
                if f.suffix.lower() != ".html":
                    return f'src="{f.name}"'
            return m.group(0)  # leave alone if no matching file

        rendered = _re.sub(r'src="cid:([^"]+)"', _fix_cid, rendered)
        # Light browser-side styling for the preview pane only —
        # gives the email a readable margin instead of butting up
        # against the viewport edge. None of this CSS ships in the
        # actual sent email.
        shell = (
            '<!DOCTYPE html>\n<html><head>'
            '<meta charset="utf-8"><title>Template preview</title>'
            '<style>body { max-width: 720px; margin: 24px auto; '
            'padding: 0 24px; font-family: Segoe UI, sans-serif; }'
            '.preview-banner { background:#fff3c4; color:#5a4500; '
            'padding:8px 12px; border-radius:6px; margin-bottom:16px; '
            'font-size:12px; }</style></head><body>'
            '<div class="preview-banner">Preview — read-only · '
            '<code>{{vars}}</code> shown as <code>&lt;LABEL&gt;</code> · '
            'cid: refs rewritten to filenames · close the tab when done.'
            '</div>\n' + rendered + '\n</body></html>'
        )
        preview_path = templates_dir() / "_preview.html"
        try:
            templates_dir().mkdir(parents=True, exist_ok=True)
            preview_path.write_text(shell, encoding="utf-8")
        except Exception as e:
            messagebox.showerror("Preview failed", str(e))
            return
        uri = preview_path.as_uri()
        # Prefer Edge so the preview always lands in a known, stable
        # renderer (matches the runtime Playwright Edge). If Edge
        # isn't reachable, fall back to the user's default browser
        # so the preview still appears somewhere reasonable.
        if not _open_in_edge(uri):
            webbrowser.open(uri, new=2)

    ctk.CTkButton(
        btn_row, text="Save", command=do_save, width=100,
    ).pack(side="left", padx=4)
    ctk.CTkButton(
        btn_row, text="Save as…", command=do_save_as, width=100,
        **SECONDARY_BTN_KWARGS,
    ).pack(side="left", padx=4)
    ctk.CTkButton(
        btn_row, text="Preview", command=do_preview, width=100,
        **SECONDARY_BTN_KWARGS,
    ).pack(side="left", padx=4)
    ctk.CTkButton(
        btn_row, text="Save & Open externally",
        command=do_open_externally, width=180,
        **SECONDARY_BTN_KWARGS,
    ).pack(side="left", padx=4)
    ctk.CTkButton(
        btn_row, text="Cancel", command=do_cancel, width=90,
        **SECONDARY_BTN_KWARGS,
    ).pack(side="right", padx=4)

    dialog.bind("<Escape>", lambda _e: do_cancel())
    dialog.protocol("WM_DELETE_WINDOW", do_cancel)

    parent.wait_window(dialog)
    return saved["value"]


def _restore_dialog_geometry(dialog, key: str) -> None:
    geom = _DIALOG_GEOMETRY.get(key, _DIALOG_DEFAULTS.get(key, ""))
    if geom:
        try:
            dialog.geometry(geom)
        except Exception:
            dialog.geometry(_DIALOG_DEFAULTS.get(key, "400x300"))


def _save_dialog_geometry(dialog, key: str) -> None:
    try:
        _DIALOG_GEOMETRY[key] = dialog.geometry()
    except Exception:
        pass


def prompt_calendar_pick(parent, initial_date=None):
    """Small monthly calendar picker. Click a day → returns that
    `datetime.date`. Returns None on cancel. Built from CTk widgets
    + Python's stdlib `calendar` module — no third-party deps.

    Used by FilterRow's date operators (is before / is after / is
    on) so users can click a date instead of typing the format."""
    import calendar as _cal
    from datetime import date as _date

    today = _date.today()
    base = initial_date if initial_date else today
    state = {"year": base.year, "month": base.month, "selected": None}

    dialog = ctk.CTkToplevel(parent)
    dialog.title("Pick a date")
    dialog.transient(parent)
    dialog.attributes("-topmost", True)
    dialog.grab_set()
    # Pop up near the mouse so the cursor barely travels (general practice
    # for small popups). Offset slightly up-left so the pointer lands just
    # inside, and clamp to the screen so it never opens off-edge.
    _w, _h = 280, 300
    try:
        _px, _py = parent.winfo_pointerxy()
        _sw, _sh = dialog.winfo_screenwidth(), dialog.winfo_screenheight()
        _x = min(max(_px - 20, 0), max(_sw - _w, 0))
        _y = min(max(_py - 20, 0), max(_sh - _h, 0))
        dialog.geometry(f"{_w}x{_h}+{_x}+{_y}")
    except Exception:
        dialog.geometry(f"{_w}x{_h}")
    # Topmost-claw-back (same pattern as our other modals so a busy
    # background window can't bury it).
    dialog.lift()
    dialog.focus_force()
    dialog.after(120, lambda: (dialog.lift(), dialog.focus_force()))

    # Header: ◀ Month Year ▶
    header = ctk.CTkFrame(dialog, fg_color="transparent")
    header.pack(fill="x", padx=8, pady=(8, 0))

    def _change_month(delta: int) -> None:
        m = state["month"] + delta
        y = state["year"]
        while m > 12:
            m -= 12
            y += 1
        while m < 1:
            m += 12
            y -= 1
        state["month"] = m
        state["year"] = y
        _refresh()

    ctk.CTkButton(
        header, text="◀", width=32, height=28,
        command=lambda: _change_month(-1),
        **SECONDARY_BTN_KWARGS,
    ).pack(side="left")
    month_label = ctk.CTkLabel(
        header, text="", font=ctk.CTkFont(size=13, weight="bold"),
    )
    month_label.pack(side="left", expand=True)
    ctk.CTkButton(
        header, text="▶", width=32, height=28,
        command=lambda: _change_month(1),
        **SECONDARY_BTN_KWARGS,
    ).pack(side="right")

    # Day grid
    grid = ctk.CTkFrame(dialog, fg_color="transparent")
    grid.pack(padx=8, pady=4)
    # Sunday-first matches the US convention WGU students likely
    # use. (Tk's calendar.firstweekday=6 starts on Sunday.)
    dow_labels = ("Su", "Mo", "Tu", "We", "Th", "Fr", "Sa")
    for i, d in enumerate(dow_labels):
        ctk.CTkLabel(
            grid, text=d, width=32, anchor="center",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=("gray40", "gray70"),
        ).grid(row=0, column=i, padx=1, pady=1)

    day_buttons: list[ctk.CTkButton] = []

    def _select(day: int) -> None:
        state["selected"] = _date(state["year"], state["month"], day)
        try: dialog.grab_release()
        except Exception: pass
        try: dialog.destroy()
        except Exception: pass

    def _refresh() -> None:
        month_label.configure(
            text=_date(state["year"], state["month"], 1).strftime("%B %Y")
        )
        for b in day_buttons:
            try: b.destroy()
            except Exception: pass
        day_buttons.clear()
        cal_iter = _cal.Calendar(firstweekday=6).monthdayscalendar(
            state["year"], state["month"],
        )
        for row_idx, week in enumerate(cal_iter, start=1):
            for col_idx, day in enumerate(week):
                if day == 0:
                    continue
                btn_kwargs = dict(SECONDARY_BTN_KWARGS)
                # Highlight today in the primary accent color so it's
                # easy to find on the grid.
                if (state["year"], state["month"], day) == (
                    today.year, today.month, today.day
                ):
                    btn_kwargs.pop("fg_color", None)
                    btn_kwargs.pop("text_color", None)
                btn = ctk.CTkButton(
                    grid, text=str(day), width=32, height=28,
                    command=lambda d=day: _select(d),
                    font=ctk.CTkFont(size=11),
                    **btn_kwargs,
                )
                btn.grid(row=row_idx, column=col_idx, padx=1, pady=1)
                day_buttons.append(btn)

    # Bottom: jump-to-today + cancel.
    bottom = ctk.CTkFrame(dialog, fg_color="transparent")
    bottom.pack(fill="x", padx=8, pady=(4, 8))

    def _jump_today() -> None:
        state["year"] = today.year
        state["month"] = today.month
        _refresh()

    ctk.CTkButton(
        bottom, text="Today", width=70, height=26,
        command=_jump_today, **SECONDARY_BTN_KWARGS,
    ).pack(side="left")
    ctk.CTkButton(
        bottom, text="Cancel", width=70, height=26,
        command=lambda: (
            dialog.grab_release() if dialog.winfo_exists() else None,
            dialog.destroy() if dialog.winfo_exists() else None,
        ),
        **SECONDARY_BTN_KWARGS,
    ).pack(side="right")

    dialog.bind("<Escape>", lambda _e: dialog.destroy())
    dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)

    _refresh()
    parent.wait_window(dialog)
    return state["selected"]


def prompt_find_and_pick(
    parent,
    do_search: Callable[[str], list[str]],
) -> Optional[str]:
    """Combined find-and-pick dialog: search entry on top, results list
    below. Workflow: user types query → Enter → results appear below;
    user can retype to refine, OR click a name to commit. Returns the
    selected name, or None on cancel.

    `do_search(query)` runs on the main thread but is expected to
    block (via wait_variable inside) while the worker performs the
    actual search. Returns the list of matching names (exact tiers
    first, then fuzzy fallback as the worker decides).

    The dialog reopens at its last on-screen size/position within the
    session (key 'find_and_pick')."""
    dialog = ctk.CTkToplevel(parent)
    dialog.title("Find student")
    _restore_dialog_geometry(dialog, "find_and_pick")
    dialog.transient(parent)
    dialog.attributes("-topmost", True)
    dialog.grab_set()

    result: dict = {"value": None}

    ctk.CTkLabel(
        dialog,
        text="Type the student's name and press Enter. Matches appear below.",
        justify="left",
    ).pack(padx=12, pady=(12, 4), anchor="w")

    entry = ctk.CTkEntry(dialog, placeholder_text="e.g. Joshua Jacobs")
    entry.pack(fill="x", padx=12, pady=(0, 6))
    entry.focus_force()
    dialog.after(50, entry.focus_force)

    results_frame = ctk.CTkScrollableFrame(dialog, label_text="Matches")
    results_frame.pack(fill="both", expand=True, padx=12, pady=4)

    current_widgets: list = []
    searching = {"in_flight": False}
    pending_cancel = {"value": False}

    def alive() -> bool:
        try:
            return bool(dialog.winfo_exists())
        except Exception:
            return False

    def clear_results() -> None:
        for w in current_widgets:
            try:
                w.destroy()
            except Exception:
                pass
        current_widgets.clear()

    def populate(names: list[str], query: str) -> None:
        if not alive():
            return
        clear_results()
        if not names:
            lbl = ctk.CTkLabel(
                results_frame,
                text=(
                    f"No matches for {query!r}. Try a different "
                    "spelling, or use the full name."
                ),
                anchor="w", justify="left",
            )
            lbl.pack(fill="x", padx=4, pady=8)
            current_widgets.append(lbl)
            return
        for n in names:
            btn = ctk.CTkButton(
                results_frame, text=n, anchor="w", height=32,
                command=lambda nm=n: finish(nm),
            )
            btn.pack(fill="x", pady=2)
            current_widgets.append(btn)

    def run_search(_event=None):
        if searching["in_flight"]:
            return
        query = entry.get().strip()
        if not query:
            return
        searching["in_flight"] = True
        clear_results()
        msg = ctk.CTkLabel(
            results_frame, text=f"Searching for {query!r}…",
            anchor="w", justify="left",
        )
        msg.pack(fill="x", padx=4, pady=8)
        current_widgets.append(msg)
        # Disable the entry so a second Enter while searching can't
        # stack a second wait_variable on top of the first.
        try:
            entry.configure(state="disabled")
        except Exception:
            pass
        dialog.update_idletasks()
        try:
            names = do_search(query)
        finally:
            searching["in_flight"] = False
            if alive():
                try:
                    entry.configure(state="normal")
                    entry.focus_force()
                except Exception:
                    pass
        if pending_cancel["value"]:
            finish(None)
            return
        populate(names, query)

    def finish(name: Optional[str]) -> None:
        result["value"] = name
        _save_dialog_geometry(dialog, "find_and_pick")
        try: dialog.grab_release()
        except Exception: pass
        try: dialog.destroy()
        except Exception: pass

    def cancel(_event=None) -> None:
        # Cancel during an in-flight search defers the close until the
        # worker reports back — we can't kill the search mid-flight.
        if searching["in_flight"]:
            pending_cancel["value"] = True
            return
        finish(None)

    entry.bind("<Return>", run_search)
    dialog.bind("<Escape>", cancel)
    dialog.protocol("WM_DELETE_WINDOW", cancel)

    btn_row = ctk.CTkFrame(dialog, fg_color="transparent")
    btn_row.pack(pady=(4, 10))
    ctk.CTkButton(btn_row, text="Search", command=run_search, width=110).pack(side="left", padx=4)
    ctk.CTkButton(
        btn_row, text="Cancel", command=cancel, width=90,
        **SECONDARY_BTN_KWARGS,
    ).pack(side="left", padx=4)

    parent.wait_window(dialog)
    return result["value"]


def prompt_additional_text(parent, label: str, prefilled: str) -> Optional[str]:
    """Blocking modal: multi-line edit of a note body, pre-filled.
    Returns the new body (no strip), or None if cancelled. Enter
    submits, Shift+Enter inserts a newline, Esc cancels.

    Pre-fill rule: if the body doesn't already end in whitespace, a
    single trailing space is added so the user can start typing
    immediately without manually inserting a separator. The cursor
    is placed at end. Last on-screen position is remembered for the
    rest of the session."""
    dialog = ctk.CTkToplevel(parent)
    dialog.title(f"Edit body — {label}")
    _restore_dialog_geometry(dialog, "additional_text")
    dialog.transient(parent)
    dialog.attributes("-topmost", True)
    dialog.grab_set()

    result: dict = {"value": None}

    ctk.CTkLabel(
        dialog,
        text=(
            f"{label}: edit or add to the body. "
            "Enter = submit · Shift+Enter = newline · Esc = cancel"
        ),
        justify="left",
    ).pack(padx=12, pady=(10, 4), anchor="w")

    text_box = ctk.CTkTextbox(dialog, wrap="word")
    text_box.pack(fill="both", expand=True, padx=12, pady=4)
    content = prefilled
    if content and content[-1] not in (" ", "\n", "\t"):
        content += " "
    text_box.insert("1.0", content)
    text_box.focus_force()
    dialog.after(50, text_box.focus_force)
    text_box.mark_set("insert", "end-1c")

    def submit(_event=None):
        result["value"] = text_box.get("1.0", "end-1c")
        _save_dialog_geometry(dialog, "additional_text")
        try: dialog.grab_release()
        except Exception: pass
        try: dialog.destroy()
        except Exception: pass
        return "break"

    def cancel(_event=None):
        _save_dialog_geometry(dialog, "additional_text")
        try: dialog.grab_release()
        except Exception: pass
        try: dialog.destroy()
        except Exception: pass
        return "break"

    def insert_newline(_event):
        text_box.insert("insert", "\n")
        return "break"

    text_box.bind("<Return>", submit)
    text_box.bind("<Shift-Return>", insert_newline)
    dialog.bind("<Escape>", cancel)
    dialog.protocol("WM_DELETE_WINDOW", cancel)

    btn_row = ctk.CTkFrame(dialog, fg_color="transparent")
    btn_row.pack(pady=(4, 10))
    ctk.CTkButton(btn_row, text="Submit", command=submit, width=110).pack(side="left", padx=4)
    ctk.CTkButton(
        btn_row, text="Cancel", command=cancel, width=90,
        **SECONDARY_BTN_KWARGS,
    ).pack(side="left", padx=4)

    parent.wait_window(dialog)
    return result["value"]


def prompt_edit_note(parent, label, body_prefill, course_default,
                     activities_on, eas):
    """Unified fire-time note dialog: edit the body, course code, and
    academic activities, and — when the student has open Essential
    Actions — attach/close one. Returns
    {body, course, activities, ea} (ea = (reason, course, close) or None)
    or None if cancelled."""
    dialog = ctk.CTkToplevel(parent)
    dialog.title(f"Edit note — {label}")
    _restore_dialog_geometry(dialog, "edit_note")
    dialog.transient(parent)
    dialog.attributes("-topmost", True)
    dialog.grab_set()
    dialog.lift()
    dialog.after(50, lambda: (dialog.lift(), dialog.focus_force()))
    res = {"value": None}

    ctk.CTkLabel(
        dialog, text="Edit this note before it's filed.  Esc = cancel.",
        anchor="w", text_color=("gray35", "gray70"),
    ).pack(fill="x", padx=12, pady=(10, 2))

    crow = ctk.CTkFrame(dialog, fg_color="transparent")
    crow.pack(fill="x", padx=12, pady=(2, 2))
    ctk.CTkLabel(crow, text="Course code:", width=90, anchor="w").pack(side="left")
    course_entry = ctk.CTkEntry(crow, width=160)
    course_entry.pack(side="left")
    if course_default:
        course_entry.insert(0, course_default)

    ctk.CTkLabel(dialog, text="Note body:", anchor="w").pack(
        fill="x", padx=12, pady=(6, 0))
    text_box = ctk.CTkTextbox(dialog, wrap="word", height=150)
    text_box.pack(fill="both", expand=True, padx=12, pady=(0, 4))
    c = body_prefill or ""
    if c and c[-1] not in (" ", "\n", "\t"):
        c += " "
    text_box.insert("1.0", c)
    text_box.mark_set("insert", "end-1c")

    ctk.CTkLabel(dialog, text="Academic activities:", anchor="w").pack(
        fill="x", padx=12, pady=(6, 0))
    act_frame = ctk.CTkFrame(dialog, fg_color=("gray95", "gray18"))
    act_frame.pack(fill="x", padx=12, pady=(0, 4))
    act_vars = {}
    for lbl in ACADEMIC_ACTIVITY_LABELS:
        v = ctk.BooleanVar(value=(lbl in (activities_on or [])))
        act_vars[lbl] = v
        ctk.CTkCheckBox(
            act_frame, text=lbl, variable=v, font=ctk.CTkFont(size=11),
        ).pack(anchor="w", padx=8, pady=1)

    ea_sel = ctk.StringVar(value="skip")
    ea_close = ctk.BooleanVar(value=False)
    if eas:
        ctk.CTkLabel(
            dialog, text=f"Essential Actions ({len(eas)} open):", anchor="w",
            font=ctk.CTkFont(size=12, weight="bold"),
        ).pack(fill="x", padx=12, pady=(6, 0))
        eabox = ctk.CTkFrame(dialog, fg_color=("gray95", "gray18"))
        eabox.pack(fill="x", padx=12, pady=(0, 4))
        ctk.CTkRadioButton(
            eabox, text="Don't attach", variable=ea_sel, value="skip",
        ).pack(anchor="w", padx=8, pady=1)
        for i, ea in enumerate(eas):
            t = ea.get("reason", "")
            if ea.get("course"):
                t += f"   ({ea['course']})"
            ctk.CTkRadioButton(
                eabox, text=t, variable=ea_sel, value=str(i),
            ).pack(anchor="w", padx=8, pady=1)
        ctk.CTkCheckBox(
            dialog, text="Close the Essential Action when the note is saved",
            variable=ea_close, font=ctk.CTkFont(size=11),
        ).pack(anchor="w", padx=12, pady=(0, 4))

    def _cont(_e=None):
        ea_choice = None
        v = ea_sel.get()
        if eas and v != "skip":
            ea = eas[int(v)]
            ea_choice = (ea.get("reason", ""), ea.get("course", ""),
                         bool(ea_close.get()))
        res["value"] = {
            "body": text_box.get("1.0", "end-1c"),
            "course": course_entry.get().strip(),
            "activities": [l for l, vv in act_vars.items() if vv.get()],
            "ea": ea_choice,
        }
        _save_dialog_geometry(dialog, "edit_note")
        _close()

    def _cancel(_e=None):
        _save_dialog_geometry(dialog, "edit_note")
        _close()

    def _close():
        try: dialog.grab_release()
        except Exception: pass
        try: dialog.destroy()
        except Exception: pass

    btn_row = ctk.CTkFrame(dialog, fg_color="transparent")
    btn_row.pack(pady=(2, 10))
    ctk.CTkButton(btn_row, text="Continue", command=_cont, width=110).pack(
        side="left", padx=4)
    ctk.CTkButton(
        btn_row, text="Cancel", command=_cancel, width=90,
        **SECONDARY_BTN_KWARGS,
    ).pack(side="left", padx=4)
    dialog.bind("<Escape>", _cancel)
    dialog.protocol("WM_DELETE_WINDOW", _cancel)
    parent.wait_window(dialog)
    return res["value"]


def prompt_text_review(
    parent, *, who: str, mobile: str, inbox_label: str, when_str: str,
    body: str, char_limit: int, scheduled: bool,
) -> Optional[str]:
    """In-app review/edit for a single outgoing text — the texting equivalent of
    the email previewer. Shows recipient / inbox / scheduled time and lets the
    user edit the message. Returns the (possibly edited) body to send, or None
    on cancel. The caller then drives Mongoose to completion (no manual clicks
    in Mongoose)."""
    dialog = ctk.CTkToplevel(parent)
    dialog.title("Review text")
    dialog.transient(parent)
    dialog.attributes("-topmost", True)
    dialog.grab_set()
    dialog.lift()
    dialog.after(50, lambda: (dialog.lift(), dialog.focus_force()))
    res = {"value": None}

    header = "To:  " + who + (f"   ·   {mobile}" if mobile else "")
    if inbox_label:
        header += f"\nInbox:  {inbox_label}"
    header += f"\n{'Schedule' if scheduled else 'Send'}:  {when_str}"
    ctk.CTkLabel(
        dialog, text=header, justify="left", anchor="w",
        font=ctk.CTkFont(size=12),
    ).pack(fill="x", padx=12, pady=(12, 6))

    ctk.CTkLabel(dialog, text="Message:", anchor="w").pack(
        fill="x", padx=12, pady=(2, 0))
    box = ctk.CTkTextbox(dialog, wrap="word", height=150, width=460)
    box.pack(fill="both", expand=True, padx=12, pady=(0, 2))
    box.insert("1.0", body or "")
    box.mark_set("insert", "end-1c")

    count = ctk.CTkLabel(
        dialog, text="", anchor="e", font=ctk.CTkFont(size=11),
        text_color=("gray40", "gray70"))
    count.pack(fill="x", padx=12, pady=(0, 4))

    def _update_count(_e=None):
        n = len(box.get("1.0", "end-1c"))
        over = n - char_limit
        count.configure(
            text=f"{n}/{char_limit}" + (f"  ({over} over — will be trimmed)"
                                        if over > 0 else ""),
            text_color=("#d11" if over > 0 else ("gray40", "gray70")),
        )

    box.bind("<KeyRelease>", _update_count)
    _update_count()

    def _close():
        try: dialog.grab_release()
        except Exception: pass
        try: dialog.destroy()
        except Exception: pass

    def _send(_e=None):
        res["value"] = box.get("1.0", "end-1c")
        _close()

    def _cancel(_e=None):
        _close()

    btns = ctk.CTkFrame(dialog, fg_color="transparent")
    btns.pack(pady=(2, 10))
    ctk.CTkButton(
        btns, text=("Schedule" if scheduled else "Send"),
        command=_send, width=120,
    ).pack(side="left", padx=4)
    ctk.CTkButton(
        btns, text="Cancel", command=_cancel, width=90, **SECONDARY_BTN_KWARGS,
    ).pack(side="left", padx=4)
    dialog.bind("<Escape>", _cancel)
    dialog.protocol("WM_DELETE_WINDOW", _cancel)
    parent.wait_window(dialog)
    return res["value"]


def ask_yes_no_topmost(
    parent, title: str, message: str,
    yes_label: str = "Yes", no_label: str = "No",
    at: Optional[tuple] = None,
) -> bool:
    """Topmost Yes/No modal. Use AFTER Outlook (or any other window)
    has stolen focus — tkinter's stock messagebox.askyesno doesn't
    have topmost / focus-force handling, so its dialog can open
    BEHIND Outlook and look like the app hung (the user can't see
    where the question is waiting). This variant uses the same
    pattern as `prompt_additional_text` — CTkToplevel + topmost +
    repeated focus_force calls — so the question always lands in
    front of the user.

    Returns True for Yes, False for No / window-close / Esc."""
    dialog = ctk.CTkToplevel(parent)
    dialog.title(title)
    dialog.transient(parent)
    dialog.attributes("-topmost", True)
    dialog.grab_set()
    result = {"value": False}

    ctk.CTkLabel(
        dialog, text=message, justify="left", wraplength=460,
    ).pack(padx=16, pady=(14, 8), anchor="w")

    btn_row = ctk.CTkFrame(dialog, fg_color="transparent")
    btn_row.pack(fill="x", padx=16, pady=(4, 14))

    def _close(value: bool) -> None:
        result["value"] = value
        try: dialog.grab_release()
        except Exception: pass
        try: dialog.destroy()
        except Exception: pass

    yes_btn = ctk.CTkButton(
        btn_row, text=yes_label, width=100,
        command=lambda: _close(True),
    )
    yes_btn.pack(side="left", padx=4)
    ctk.CTkButton(
        btn_row, text=no_label, width=100,
        command=lambda: _close(False),
        **SECONDARY_BTN_KWARGS,
    ).pack(side="left", padx=4)

    dialog.bind("<Return>", lambda _e: _close(True))
    dialog.bind("<Escape>", lambda _e: _close(False))
    dialog.protocol("WM_DELETE_WINDOW", lambda: _close(False))

    # Optionally pop the dialog right where the action was invoked (e.g.
    # over the caseload row-menu the user just clicked), clamped on-screen.
    if at is not None:
        try:
            dialog.update_idletasks()
            w = dialog.winfo_reqwidth()
            h = dialog.winfo_reqheight()
            sw, sh = dialog.winfo_screenwidth(), dialog.winfo_screenheight()
            x = max(0, min(int(at[0]) - 20, sw - w - 8))
            y = max(0, min(int(at[1]) - 10, sh - h - 8))
            dialog.geometry(f"+{x}+{y}")
        except Exception:
            pass

    # Outlook may steal focus right after compose_email returns;
    # claw it back aggressively. The two .after() retries handle the
    # case where Outlook fully renders ~100-500ms after Display().
    dialog.lift()
    dialog.focus_force()
    yes_btn.focus_set()
    dialog.after(100, lambda: (dialog.lift(), dialog.focus_force()))
    dialog.after(500, lambda: (dialog.lift(), dialog.focus_force()))

    parent.wait_window(dialog)
    return result["value"]


class _HTMLToTkRenderer(HTMLParser):
    """Render simplified HTML into a Tk Text widget using tag-based
    formatting. Goal: legible to non-technical reviewers (FERPA), not
    pixel-perfect. Output handles paragraphs, links, basic
    formatting, lists, headings, and images-as-placeholders.

    Highlights any `{{var}}` placeholder that survived rendering in
    red — those are the FERPA risk (template variable referenced
    that didn't get a value)."""

    _SKIP_CONTENT = {"style", "script", "head", "title", "meta", "link"}

    def __init__(self, text_widget, unresolved_var_set: Optional[set] = None):
        super().__init__()
        self.text = text_widget
        # `unresolved_vars` is populated as we find any leftover
        # `{{name}}` in the rendered HTML — caller uses it to
        # populate the "issues" badge on the row.
        self.unresolved_vars: set[str] = (
            unresolved_var_set if unresolved_var_set is not None else set()
        )
        self._format_stack: list[str] = []
        self._link_href = ""
        self._list_stack: list[dict] = []
        self._skip_depth = 0
        self._first_block = True

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP_CONTENT:
            self._skip_depth += 1
            return
        if self._skip_depth > 0:
            return
        d = dict(attrs)

        if tag == "p":
            self._paragraph_break()
        elif tag == "br":
            self.text.insert("end", "\n")
        elif tag in ("strong", "b"):
            self._format_stack.append("bold")
        elif tag in ("em", "i"):
            self._format_stack.append("italic")
        elif tag == "u":
            self._format_stack.append("underline")
        elif tag == "a":
            self._format_stack.append("link")
            self._link_href = d.get("href", "")
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._paragraph_break()
            self._format_stack.append("heading")
        elif tag == "ol":
            self._list_stack.append({"type": "ol", "n": 0})
            self._paragraph_break()
        elif tag == "ul":
            self._list_stack.append({"type": "ul", "n": 0})
            self._paragraph_break()
        elif tag == "li":
            self.text.insert("end", "\n")
            depth = max(0, len(self._list_stack) - 1)
            self.text.insert("end", "    " * depth)
            if self._list_stack and self._list_stack[-1]["type"] == "ol":
                self._list_stack[-1]["n"] += 1
                self.text.insert("end", f"{self._list_stack[-1]['n']}. ")
            else:
                self.text.insert("end", "• ")
        elif tag == "img":
            src = d.get("src", "")
            alt = (d.get("alt", "") or "").strip()
            # cid:STEM is what the live email uses; show the stem
            # so the reviewer can see the file being referenced.
            if src.startswith("cid:"):
                label = src[4:]
            else:
                label = src.rsplit("/", 1)[-1] or src
            marker = f"[Image: {label}"
            if alt:
                marker += f"  ⇨  {alt}"
            marker += "]"
            self._paragraph_break()
            self._insert_tagged(marker, "image")
            self.text.insert("end", "\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP_CONTENT:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth > 0:
            return

        if tag in ("strong", "b"):
            self._pop_format("bold")
        elif tag in ("em", "i"):
            self._pop_format("italic")
        elif tag == "u":
            self._pop_format("underline")
        elif tag == "a":
            self._pop_format("link")
            href = self._link_href
            self._link_href = ""
            # Show the URL in dim text after the link so reviewers
            # can verify what the click goes to. Trim mailto: prefix
            # to keep it tidy.
            if href:
                disp = href[7:] if href.startswith("mailto:") else href
                self._insert_tagged(f"  ({disp})", "url_hint")
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._pop_format("heading")
            self.text.insert("end", "\n")
        elif tag in ("ol", "ul"):
            if self._list_stack:
                self._list_stack.pop()
            self.text.insert("end", "\n")

    def handle_data(self, data):
        if self._skip_depth > 0:
            return
        # Collapse runs of whitespace (HTML semantics) but preserve
        # one separator between words.
        collapsed = re.sub(r"\s+", " ", data)
        if not collapsed:
            return
        # Detect any leftover {{var}} placeholders — these mean the
        # template referenced a variable that didn't get a value,
        # which is the kind of leak FERPA review needs to catch.
        var_re = re.compile(r"\{\{\s*(\w+)\s*\}\}")
        idx = 0
        for m in var_re.finditer(collapsed):
            if m.start() > idx:
                self._insert_tagged(
                    collapsed[idx:m.start()], *self._format_stack,
                )
            self._insert_tagged(m.group(0), "unresolved_var")
            self.unresolved_vars.add(m.group(1))
            idx = m.end()
        if idx < len(collapsed):
            self._insert_tagged(
                collapsed[idx:], *self._format_stack,
            )

    def handle_entityref(self, name):
        if self._skip_depth > 0:
            return
        ch = html.unescape(f"&{name};")
        self._insert_tagged(ch, *self._format_stack)

    def handle_charref(self, name):
        if self._skip_depth > 0:
            return
        ch = html.unescape(f"&#{name};")
        self._insert_tagged(ch, *self._format_stack)

    def _pop_format(self, name):
        # Pop the rightmost matching entry (handles nested tags).
        for i in range(len(self._format_stack) - 1, -1, -1):
            if self._format_stack[i] == name:
                del self._format_stack[i]
                return

    def _paragraph_break(self):
        if self._first_block:
            self._first_block = False
            return
        # Avoid stacking multiple blank lines if the previous block
        # already ended with one.
        tail = self.text.get("end-3c", "end-1c")
        if tail.endswith("\n\n"):
            return
        if tail.endswith("\n"):
            self.text.insert("end", "\n")
            return
        self.text.insert("end", "\n\n")

    def _insert_tagged(self, text, *tags):
        if not text:
            return
        start = self.text.index("end-1c")
        self.text.insert("end", text)
        end = self.text.index("end-1c")
        for tag in tags:
            self.text.tag_add(tag, start, end)


def _configure_email_preview_tags(text_widget) -> None:
    """Set up the tag styles used by `_HTMLToTkRenderer`. Colors
    adapt to the current ctk appearance mode so the preview is
    readable on both light and dark themes."""
    mode = ctk.get_appearance_mode()
    is_dark = mode == "Dark"
    text_widget.tag_configure(
        "bold", font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
    )
    text_widget.tag_configure(
        "italic", font=ctk.CTkFont(family="Segoe UI", size=12, slant="italic"),
    )
    text_widget.tag_configure("underline", underline=True)
    text_widget.tag_configure(
        "link",
        foreground="#79b8ff" if is_dark else "#1a73e8",
        underline=True,
    )
    text_widget.tag_configure(
        "url_hint",
        foreground="#888888" if is_dark else "#666666",
        font=ctk.CTkFont(family="Segoe UI", size=10),
    )
    text_widget.tag_configure(
        "heading", font=ctk.CTkFont(family="Segoe UI", size=15, weight="bold"),
    )
    text_widget.tag_configure(
        "image",
        foreground="#5a4500" if not is_dark else "#ffd966",
        background="#fff3c4" if not is_dark else "#3a3520",
        font=ctk.CTkFont(family="Segoe UI", size=11, slant="italic"),
    )
    text_widget.tag_configure(
        "unresolved_var",
        foreground="#ffffff",
        background="#cc0000",
        font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
    )


def prompt_batch_text_review(
    parent,
    scenario_name: str,
    entries: list[dict],
    filter_summary: str = "",
    *,
    scheduled: bool = True,
) -> Optional[list[int]]:
    """Modal reviewer for batch texts — the texting analog of
    prompt_batch_email_review. Left: a checklist of students (name · course ·
    send time · any issues). Right: the selected student's actual rendered
    message. Returns the list of selected indices into `entries`, or None on
    cancel. Rows with issues (no mobile / unknown tz / over the char limit)
    start unchecked so they're consciously opted in."""
    dialog = ctk.CTkToplevel(parent)
    dialog.title(f"Review texts — {scenario_name}")
    dialog.geometry("900x640")
    dialog.transient(parent)
    dialog.attributes("-topmost", True)
    dialog.grab_set()
    dialog.lift()
    dialog.after(150, lambda: (dialog.lift(), dialog.focus_force()))
    result: dict = {"value": None}
    sel_vars = [ctk.BooleanVar(value=not bool(e.get("issues"))) for e in entries]

    banner = ctk.CTkFrame(dialog, fg_color=("gray92", "gray18"))
    banner.pack(fill="x", padx=8, pady=(8, 0))
    ctk.CTkLabel(
        banner, text=f"Review batch texts: {scenario_name}",
        font=ctk.CTkFont(size=14, weight="bold"), anchor="w",
    ).pack(fill="x", padx=10, pady=(8, 0))
    if filter_summary:
        ctk.CTkLabel(
            banner, text=f"Matched by: {filter_summary}",
            font=ctk.CTkFont(size=11), text_color=("gray40", "gray70"),
            anchor="w",
        ).pack(fill="x", padx=10, pady=(0, 2))
    sel_label = ctk.CTkLabel(
        banner, text="", font=ctk.CTkFont(size=11),
        text_color=("gray35", "gray70"), anchor="w")
    sel_label.pack(fill="x", padx=10, pady=(0, 8))

    body = ctk.CTkFrame(dialog, fg_color="transparent")
    body.pack(fill="both", expand=True, padx=8, pady=6)
    left = ctk.CTkScrollableFrame(body, label_text="Students", width=330)
    left.pack(side="left", fill="y", padx=(0, 6))
    right = ctk.CTkFrame(body, fg_color=("gray95", "gray16"))
    right.pack(side="left", fill="both", expand=True)
    preview_hdr = ctk.CTkLabel(
        right, text="", anchor="w", justify="left", font=ctk.CTkFont(size=12))
    preview_hdr.pack(fill="x", padx=10, pady=(10, 4))
    preview_box = ctk.CTkTextbox(right, wrap="word")
    preview_box.pack(fill="both", expand=True, padx=10, pady=(0, 10))
    preview_box.configure(state="disabled")

    def _update_sel_label():
        n = sum(1 for v in sel_vars if v.get())
        sel_label.configure(text=f"{n} of {len(entries)} selected")

    def _show(i: int):
        e = entries[i]
        hdr = (f"To:  {e['name']}"
               + (f"   ·   {e['mobile']}" if e.get("mobile") else "  ·  (no mobile)")
               + f"\n{'Schedule' if scheduled else 'Send'}:  {e['when_str']}")
        if e.get("recipients_str"):
            hdr += f"\nRecipients:  {e['recipients_str']}"
        if e.get("issues"):
            hdr += f"\n⚠ {', '.join(e['issues'])}"
        preview_hdr.configure(text=hdr)
        preview_box.configure(state="normal")
        preview_box.delete("1.0", "end")
        preview_box.insert("1.0", e.get("body", ""))
        preview_box.configure(state="disabled")

    for i, e in enumerate(entries):
        rowf = ctk.CTkFrame(left, fg_color="transparent")
        rowf.pack(fill="x", pady=1)
        ctk.CTkCheckBox(
            rowf, text="", width=24, variable=sel_vars[i],
            command=_update_sel_label,
        ).pack(side="left", anchor="n")
        # A wrapping label (not a button) so long group labels / "⚠ …" notes
        # don't clip. Clicking it shows the message on the right. Compact sub-
        # line — the full issue text lives in the right preview header.
        warn = "⚠  " if e.get("issues") else ""
        sub = e.get("mobile", "") or e.get("when_str", "")
        lbl = ctk.CTkLabel(
            rowf, text=f"{warn}{e['name']}\n{sub}", anchor="w", justify="left",
            wraplength=250, font=ctk.CTkFont(size=11),
            text_color=(("#a33", "#e88") if e.get("issues")
                        else ("gray10", "gray90")),
        )
        lbl.pack(side="left", fill="x", expand=True)
        lbl.bind("<Button-1>", lambda _ev, x=i: _show(x))
    _update_sel_label()
    if entries:
        _show(0)

    footer = ctk.CTkFrame(dialog, fg_color="transparent")
    footer.pack(fill="x", padx=8, pady=(0, 8))

    def _toggle_all():
        target = not all(v.get() for v in sel_vars)
        for v in sel_vars:
            v.set(target)
        _update_sel_label()

    def _close():
        try: dialog.grab_release()
        except Exception: pass
        try: dialog.destroy()
        except Exception: pass

    def _send():
        result["value"] = [i for i, v in enumerate(sel_vars) if v.get()]
        _close()

    ctk.CTkButton(
        footer, text="Select all / none", command=_toggle_all, width=140,
        **SECONDARY_BTN_KWARGS,
    ).pack(side="left")
    ctk.CTkButton(
        footer, text=("Schedule selected" if scheduled else "Send selected"),
        command=_send, width=150,
    ).pack(side="right", padx=(6, 0))
    ctk.CTkButton(
        footer, text="Cancel", command=_close, width=90, **SECONDARY_BTN_KWARGS,
    ).pack(side="right")
    dialog.bind("<Escape>", lambda _e: _close())
    dialog.protocol("WM_DELETE_WINDOW", _close)
    parent.wait_window(dialog)
    return result["value"]


def prompt_batch_email_review(
    parent,
    scenario_name: str,
    rendered: list[dict],
    filter_summary: str = "",
    *,
    templates: Optional[list[str]] = None,
    current_template: str = "",
    on_template_change: Optional[Callable[[str], list[dict]]] = None,
) -> tuple:
    """Modal reviewer for batch emails. Returns
    `(selected_indices_or_None, chosen_template)` — the indices (into
    `rendered`) the user wants to send to (None on cancel), and the
    template they had selected (the unchanged default unless a
    `templates` dropdown was shown).

    When `templates` is given (the scenario opted into 'choose template
    when fired'), a Template dropdown appears above the preview; changing
    it calls `on_template_change(template_filename)` which must return a
    freshly-rendered `rendered` list, and the preview refreshes live.

    Each entry in `rendered` is a dict with keys:
        name              — student display name
        student_id        — for sub-label
        course_code       — for sub-label
        to                — recipient address (empty if missing)
        cc                — CC address (empty if not CC'ing PM)
        cc_is_self        — bool; show "(you, auto-CC'd as PM)" hint
        subject           — rendered subject line
        body_html         — rendered email body (full HTML)
        issues            — list of strings; common: 'no_email',
                            'render_error: …'

    Rows with `'no_email' in issues` come up unchecked by default
    so the FERPA reviewer has to consciously include them; other
    rows are checked by default. Unresolved `{{var}}` placeholders
    in the body are highlighted in red by the HTML renderer."""
    dialog = ctk.CTkToplevel(parent)
    dialog.title(f"Review emails — {scenario_name}")
    dialog.geometry("1100x720")
    dialog.transient(parent)
    dialog.attributes("-topmost", True)
    dialog.grab_set()

    # Topmost claw-back. Same dance as ask_yes_no_topmost since the
    # Outlook template-preview path used to lose focus to Outlook;
    # the new flow never opens Outlook for review but staying topmost
    # keeps the modal in front of anything else the user clicks.
    dialog.lift()
    dialog.focus_force()
    dialog.after(150, lambda: (dialog.lift(), dialog.focus_force()))

    selected_vars: list[ctk.BooleanVar] = []
    # Default: every row checked, UNLESS the row has an issue (no
    # email, render error). Those start unchecked — FERPA-friendly
    # default since you have to consciously opt them in.
    for entry in rendered:
        v = ctk.BooleanVar(value=not bool(entry.get("issues")))
        selected_vars.append(v)

    state = {"current": 0}

    # ---- Top banner: filter summary + count. ----
    banner = ctk.CTkFrame(dialog, fg_color=("gray92", "gray18"))
    banner.pack(fill="x", padx=8, pady=(8, 0))
    title_line = ctk.CTkLabel(
        banner, text=f"Review batch: {scenario_name}",
        font=ctk.CTkFont(size=14, weight="bold"), anchor="w",
    )
    title_line.pack(fill="x", padx=10, pady=(8, 0))
    if filter_summary:
        ctk.CTkLabel(
            banner,
            text=f"Matched by: {filter_summary}",
            font=ctk.CTkFont(size=11),
            text_color=("gray40", "gray70"),
            anchor="w",
        ).pack(fill="x", padx=10, pady=(0, 2))
    selection_label = ctk.CTkLabel(
        banner, text="", font=ctk.CTkFont(size=11),
        text_color=("gray35", "gray70"), anchor="w",
    )
    selection_label.pack(fill="x", padx=10, pady=(0, 8))

    # Optional template picker (scenario opted into "choose template when
    # fired"). Changing it re-renders every preview via the callback.
    chosen_template_box = {"value": current_template}
    if templates:
        tpl_row = ctk.CTkFrame(banner, fg_color="transparent")
        tpl_row.pack(fill="x", padx=10, pady=(0, 8))
        ctk.CTkLabel(
            tpl_row, text="Template:",
            font=ctk.CTkFont(size=12, weight="bold"),
        ).pack(side="left")
        tpl_combo = ctk.CTkComboBox(
            tpl_row, values=templates, width=300, state="readonly",
        )
        tpl_combo.set(current_template if current_template in templates
                      else templates[0])
        tpl_combo.pack(side="left", padx=(6, 0))
        chosen_template_box["value"] = tpl_combo.get()

        def _on_tpl_change(choice: str) -> None:
            chosen_template_box["value"] = choice
            if on_template_change is None:
                return
            try:
                new = on_template_change(choice)
            except Exception:
                new = None
            if new:
                for i in range(min(len(rendered), len(new))):
                    rendered[i] = new[i]
                edits.clear()  # template re-render discards per-body edits
                _show(state["current"])

        tpl_combo.configure(command=_on_tpl_change)

    # ---- Main split: student list (left) + preview (right). ----
    body = ctk.CTkFrame(dialog, fg_color="transparent")
    body.pack(fill="both", expand=True, padx=8, pady=8)
    body.grid_columnconfigure(0, weight=0, minsize=300)
    body.grid_columnconfigure(1, weight=1)
    body.grid_rowconfigure(0, weight=1)

    # Left: scrollable student list with checkboxes.
    list_frame = ctk.CTkScrollableFrame(
        body, fg_color=("gray95", "gray16"), corner_radius=6,
        label_text=f"Students ({len(rendered)})",
    )
    list_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 8))

    # Right: preview pane.
    preview_frame = ctk.CTkFrame(body, fg_color=("gray95", "gray16"), corner_radius=6)
    preview_frame.grid(row=0, column=1, sticky="nsew")
    preview_frame.grid_columnconfigure(0, weight=1)
    preview_frame.grid_rowconfigure(2, weight=1)

    # Header area (issue banner + To/Cc/Subject lines).
    issue_banner = ctk.CTkLabel(
        preview_frame, text="", anchor="w", justify="left",
        font=ctk.CTkFont(size=11, weight="bold"),
        text_color=("#990000", "#ffcccc"),
        fg_color=("#ffe0e0", "#4a1a1a"), corner_radius=4,
    )
    # Pack/forget toggled per-row when issues exist.
    header_block = ctk.CTkFrame(preview_frame, fg_color="transparent")
    header_block.grid(row=1, column=0, sticky="ew", padx=8, pady=(8, 4))
    header_block.grid_columnconfigure(1, weight=1)
    to_label = ctk.CTkLabel(header_block, text="To:", width=64, anchor="w",
                             font=ctk.CTkFont(size=12, weight="bold"))
    to_label.grid(row=0, column=0, sticky="w")
    to_value = ctk.CTkLabel(header_block, text="", anchor="w", justify="left")
    to_value.grid(row=0, column=1, sticky="ew", padx=(2, 0))
    cc_label = ctk.CTkLabel(header_block, text="Cc:", width=64, anchor="w",
                             font=ctk.CTkFont(size=12, weight="bold"))
    cc_value = ctk.CTkLabel(header_block, text="", anchor="w", justify="left")
    subj_label = ctk.CTkLabel(header_block, text="Subject:", width=64, anchor="w",
                               font=ctk.CTkFont(size=12, weight="bold"))
    subj_label.grid(row=2, column=0, sticky="w")
    subj_value = ctk.CTkLabel(header_block, text="", anchor="w", justify="left",
                               font=ctk.CTkFont(size=12, weight="bold"))
    subj_value.grid(row=2, column=1, sticky="ew", padx=(2, 0))

    # Separator + body text.
    sep = ctk.CTkFrame(preview_frame, height=1, fg_color=("gray70", "gray35"))
    sep.grid(row=2, column=0, sticky="new", padx=8, pady=(2, 4))
    body_text = ctk.CTkTextbox(
        preview_frame, wrap="word",
        font=ctk.CTkFont(family="Segoe UI", size=12),
    )
    body_text.grid(row=3, column=0, sticky="nsew", padx=8, pady=(0, 8))
    preview_frame.grid_rowconfigure(3, weight=1)
    _configure_email_preview_tags(body_text._textbox)
    register_font_box("email", body_text)  # adjustable + Ctrl +/- zoom
    # Disable typing — preview is read-only.
    body_text.configure(state="disabled")

    # ---- Build row widgets in the list. ----
    row_buttons: list[ctk.CTkButton] = []

    def _row_label(entry: dict, idx: int) -> str:
        parts = [entry["name"]]
        sub = []
        sid = entry.get("student_id", "")
        cc = entry.get("course_code", "")
        if sid:
            sub.append(sid)
        if cc:
            sub.append(cc)
        if sub:
            parts.append("  " + " · ".join(sub))
        if entry.get("issues"):
            parts.append("  (!)")
        return "".join(parts)

    def _update_selection_label() -> None:
        n_checked = sum(1 for v in selected_vars if v.get())
        selection_label.configure(
            text=f"{n_checked} of {len(rendered)} selected · "
                 f"showing {state['current'] + 1} of {len(rendered)}"
        )
        send_btn.configure(
            text=f"Send to {n_checked} students"
            if n_checked != 1 else "Send to 1 student"
        )
        send_btn.configure(state=("normal" if n_checked > 0 else "disabled"))

    def _show(idx: int) -> None:
        if not (0 <= idx < len(rendered)):
            return
        state["current"] = idx
        entry = rendered[idx]
        # Highlight the active row (border or fg change).
        for i, b in enumerate(row_buttons):
            if i == idx:
                b.configure(border_width=2,
                             border_color=("#1a73e8", "#79b8ff"))
            else:
                b.configure(border_width=1,
                             border_color=("gray70", "gray35"))
        # Issue banner.
        issues = entry.get("issues", [])
        if issues:
            issue_banner.configure(
                text="⚠  " + "  ·  ".join(issues),
            )
            issue_banner.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 2))
        else:
            issue_banner.grid_remove()
        # To.
        to = entry.get("to", "") or "(missing — will be looked up at send)"
        to_value.configure(
            text=to,
            text_color=(("gray45", "gray60") if not entry.get("to")
                        else ("gray10", "gray90")),
        )
        # Cc — hide row if no CC configured for this scenario at all.
        if entry.get("cc") or entry.get("cc_configured"):
            cc_label.grid(row=1, column=0, sticky="w")
            cc_value.grid(row=1, column=1, sticky="ew", padx=(2, 0))
            cc = entry.get("cc", "") or "(missing — will be looked up at send)"
            hint = "  (you, auto-CC'd as PM)" if entry.get("cc_is_self") else ""
            cc_value.configure(text=cc + hint)
        else:
            cc_label.grid_remove()
            cc_value.grid_remove()
        # Subject (+ an edited marker when this body was hand-edited).
        edited_tag = "   ✏ edited" if state["current"] in edits else ""
        subj_value.configure(text=entry.get("subject", "") + edited_tag)
        # Body render.
        body_text.configure(state="normal")
        body_text.delete("1.0", "end")
        if entry.get("render_error"):
            body_text.insert("1.0", f"Render error:\n\n{entry['render_error']}")
        else:
            try:
                renderer = _HTMLToTkRenderer(body_text._textbox)
                renderer.feed(entry.get("body_html", ""))
                renderer.close()
            except Exception as e:
                body_text.insert("1.0", f"(Preview render failed: {e})\n\n")
                body_text.insert("end", entry.get("body_html", ""))
        body_text.configure(state="disabled")
        _update_selection_label()

    def _on_row_click(idx: int) -> None:
        _show(idx)

    def _on_checkbox_toggle() -> None:
        _update_selection_label()

    for i, entry in enumerate(rendered):
        row_wrap = ctk.CTkFrame(list_frame, fg_color="transparent")
        row_wrap.pack(fill="x", pady=2)
        cb = ctk.CTkCheckBox(
            row_wrap, text="", variable=selected_vars[i],
            command=_on_checkbox_toggle, width=20,
        )
        cb.pack(side="left", padx=(2, 4))
        # Reuse the same SECONDARY_BTN_KWARGS palette, but make the
        # button look like a list row — left-aligned text, click
        # selects this row for preview.
        b_kwargs = dict(SECONDARY_BTN_KWARGS)
        if entry.get("issues"):
            # Red-tint the row to flag the issue at-a-glance.
            b_kwargs["fg_color"] = ("#ffe0e0", "#4a1a1a")
            b_kwargs["text_color"] = ("#990000", "#ffcccc")
        btn = ctk.CTkButton(
            row_wrap, text=_row_label(entry, i), anchor="w",
            height=36, command=lambda idx=i: _on_row_click(idx),
            font=ctk.CTkFont(size=12),
            **b_kwargs,
        )
        btn.pack(side="left", fill="x", expand=True)
        row_buttons.append(btn)
        if entry.get("issues"):
            issue_sub = ctk.CTkLabel(
                list_frame,
                text="    ↳ " + "  ·  ".join(entry["issues"]),
                font=ctk.CTkFont(size=10),
                text_color=("#990000", "#ffcccc"),
                anchor="w", justify="left",
            )
            issue_sub.pack(fill="x", padx=4, pady=(0, 2))

    # ---- List-level action buttons (select all / none). ----
    list_actions = ctk.CTkFrame(list_frame, fg_color="transparent")
    list_actions.pack(fill="x", pady=(8, 0))

    def _select_all() -> None:
        for v in selected_vars:
            v.set(True)
        _update_selection_label()

    def _select_none() -> None:
        for v in selected_vars:
            v.set(False)
        _update_selection_label()

    ctk.CTkButton(
        list_actions, text="Select all", height=24,
        command=_select_all, **SECONDARY_BTN_KWARGS,
        font=ctk.CTkFont(size=11),
    ).pack(side="left", padx=2)
    ctk.CTkButton(
        list_actions, text="None", height=24,
        command=_select_none, **SECONDARY_BTN_KWARGS,
        font=ctk.CTkFont(size=11),
    ).pack(side="left", padx=2)

    # ---- Bottom action row: prev/next, edge, send, cancel. ----
    bottom = ctk.CTkFrame(dialog, fg_color="transparent")
    bottom.pack(fill="x", padx=8, pady=(0, 8))

    def _prev() -> None:
        if state["current"] > 0:
            _show(state["current"] - 1)

    def _next() -> None:
        if state["current"] < len(rendered) - 1:
            _show(state["current"] + 1)

    def _skip_and_next() -> None:
        # Uncheck current + advance.
        selected_vars[state["current"]].set(False)
        _update_selection_label()
        _next()

    def _view_current_in_edge() -> None:
        # Reuse the same _preview.html mechanism as the template
        # editor's Preview button so we don't keep accumulating
        # temp files. cid: references get rewritten to local
        # filenames so the browser can resolve them.
        entry = rendered[state["current"]]
        body_html = entry.get("body_html", "")

        def _fix_cid(m):
            stem = m.group(1)
            for f in sorted(templates_dir().glob(f"{stem}.*")):
                if f.suffix.lower() != ".html":
                    return f'src="{f.name}"'
            return m.group(0)
        body_html = re.sub(r'src="cid:([^"]+)"', _fix_cid, body_html)

        shell = (
            '<!DOCTYPE html>\n<html><head>'
            '<meta charset="utf-8"><title>Email preview</title>'
            '<style>body { max-width: 720px; margin: 24px auto; '
            'padding: 0 24px; font-family: Segoe UI, sans-serif; }'
            '.preview-banner { background:#fff3c4; color:#5a4500; '
            'padding:8px 12px; border-radius:6px; margin-bottom:16px; '
            'font-size:12px; }</style></head><body>'
            '<div class="preview-banner">Email preview for '
            f'<b>{html.escape(entry.get("name", ""))}</b> · To: '
            f'{html.escape(entry.get("to") or "(missing)")} · Cc: '
            f'{html.escape(entry.get("cc") or "(none)")} · Subject: '
            f'{html.escape(entry.get("subject", ""))}</div>\n'
            + body_html + '\n</body></html>'
        )
        preview_path = templates_dir() / "_preview.html"
        try:
            templates_dir().mkdir(parents=True, exist_ok=True)
            preview_path.write_text(shell, encoding="utf-8")
        except Exception:
            return
        uri = preview_path.as_uri()
        # Module-level _open_in_edge — explicit reference to avoid
        # shadowing by this closure's earlier name (which I renamed
        # to _view_current_in_edge).
        if not _open_in_edge(uri):
            import webbrowser
            webbrowser.open(uri, new=2)

    # Per-student body edits the user makes via "Edit body" — {idx: html}.
    # Returned to the caller so the edited body is what actually sends.
    edits: dict = {}

    def _edit_current_body() -> None:
        """One-off: edit THIS student's email body in the rich-text editor.
        The edit applies only to this send, not the saved template."""
        idx = state["current"]
        entry = rendered[idx]
        if entry.get("render_error"):
            return
        tmp = templates_dir() / "_oneoff_edit.html"
        try:
            templates_dir().mkdir(parents=True, exist_ok=True)
            tmp.write_text(entry.get("body_html", ""), encoding="utf-8")
        except Exception:
            return
        saved = prompt_html_template_editor(dialog, tmp)
        if saved:
            try:
                new_html = tmp.read_text(encoding="utf-8")
            except Exception:
                new_html = None
            if new_html is not None:
                rendered[idx]["body_html"] = new_html
                edits[idx] = new_html
                _show(idx)
        try:
            tmp.unlink()
        except Exception:
            pass

    nav_l = ctk.CTkFrame(bottom, fg_color="transparent")
    nav_l.pack(side="left")
    ctk.CTkButton(nav_l, text="◀ Prev", width=80, command=_prev,
                   **SECONDARY_BTN_KWARGS).pack(side="left", padx=2)
    ctk.CTkButton(nav_l, text="Next ▶", width=80, command=_next,
                   **SECONDARY_BTN_KWARGS).pack(side="left", padx=2)
    ctk.CTkButton(nav_l, text="Skip & next", width=110,
                   command=_skip_and_next,
                   **SECONDARY_BTN_KWARGS).pack(side="left", padx=8)
    ctk.CTkButton(nav_l, text="✏ Edit body", width=110,
                   command=_edit_current_body,
                   **SECONDARY_BTN_KWARGS).pack(side="left", padx=2)
    ctk.CTkButton(nav_l, text="Open in Edge", width=120,
                   command=_view_current_in_edge,
                   **SECONDARY_BTN_KWARGS).pack(side="left", padx=2)

    nav_r = ctk.CTkFrame(bottom, fg_color="transparent")
    nav_r.pack(side="right")
    result_box = {"value": None}

    def _do_send() -> None:
        selected = [i for i, v in enumerate(selected_vars) if v.get()]
        if not selected:
            return
        result_box["value"] = selected
        try: dialog.grab_release()
        except Exception: pass
        try: dialog.destroy()
        except Exception: pass

    def _do_cancel() -> None:
        try: dialog.grab_release()
        except Exception: pass
        try: dialog.destroy()
        except Exception: pass

    send_btn = ctk.CTkButton(
        nav_r, text="Send", width=180, command=_do_send,
    )
    ctk.CTkButton(
        nav_r, text="Cancel", width=90, command=_do_cancel,
        **SECONDARY_BTN_KWARGS,
    ).pack(side="right", padx=2)
    send_btn.pack(side="right", padx=2)

    dialog.bind("<Escape>", lambda _e: _do_cancel())
    dialog.bind("<Left>",   lambda _e: _prev())
    dialog.bind("<Right>",  lambda _e: _next())
    dialog.protocol("WM_DELETE_WINDOW", _do_cancel)

    # Initial display.
    if rendered:
        _show(0)

    parent.wait_window(dialog)
    return result_box["value"], chosen_template_box["value"], edits


def _build_checkbox_images():
    """Build (unchecked, checked) 16px checkbox PhotoImages in the CTk
    style — an outlined box, and a filled blue box with a white tick. The
    CALLER must keep a reference (else Tk GCs them and they render blank).
    Needs a live Tk root. Shared by the caseload viewer's select-all header
    and the batch-review popup so both look identical."""
    from PIL import Image, ImageDraw, ImageTk
    dark = ctk.get_appearance_mode() == "Dark"
    blue = "#1f6aa5" if dark else "#3a7ebf"
    border = "#6b6e70" if dark else "#979da2"
    size, scale = 16, 4  # supersample then downscale for smooth edges
    S = size * scale
    pad, rad, bw = 1 * scale, 4 * scale, 2 * scale
    un = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(un).rounded_rectangle(
        [pad, pad, S - pad, S - pad], radius=rad, outline=border, width=bw)
    ch = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(ch)
    d.rounded_rectangle(
        [pad, pad, S - pad, S - pad], radius=rad, fill=blue, outline=blue)
    d.line(
        [(S * 0.27, S * 0.52), (S * 0.44, S * 0.69), (S * 0.74, S * 0.32)],
        fill="white", width=bw, joint="curve")
    return (ImageTk.PhotoImage(un.resize((size, size), Image.LANCZOS)),
            ImageTk.PhotoImage(ch.resize((size, size), Image.LANCZOS)))


def prompt_batch_review(
    parent,
    scenario_name: str,
    rows: list[dict],
    display_columns: list[str],
) -> Optional[list[dict]]:
    """Show matched students before a batch fires. Returns the subset
    the user kept checked + confirmed, or None on cancel.

    `display_columns` is the in-order list of fields shown per row;
    the first column is usually 'Name' so the student is easy to
    identify, followed by whatever fields the scenario filtered on."""
    dialog = ctk.CTkToplevel(parent)
    dialog.title(f"Batch: {scenario_name}")
    _restore_dialog_geometry(dialog, "batch_review")
    dialog.transient(parent)
    dialog.attributes("-topmost", True)
    dialog.grab_set()

    result: dict = {"value": None}

    header_label = ctk.CTkLabel(
        dialog,
        text=(
            f"{len(rows)} students matched. Uncheck anyone to skip, "
            "then click Confirm to start."
        ),
        anchor="w", justify="left",
    )
    header_label.pack(fill="x", padx=12, pady=(12, 4))

    cols_label = ctk.CTkLabel(
        dialog,
        text=" · ".join(display_columns),
        anchor="w", justify="left",
        font=ctk.CTkFont(size=12, weight="bold"),
    )
    cols_label.pack(fill="x", padx=12, pady=(0, 4))

    # Master select-all box: the SAME little checkbox the caseload viewer
    # uses in its header, sitting to the LEFT of a gray "Student (N)" tag.
    # Click toggles all rows — or clears them if they're already all
    # selected — mirroring the viewer's _toggle_select_all. The image refs
    # are kept on the dialog so Tk doesn't GC them (→ blank).
    _chk_un, _chk_ch = _build_checkbox_images()
    dialog._batch_chk_imgs = (_chk_un, _chk_ch)

    sel_row = ctk.CTkFrame(dialog, fg_color="transparent")
    sel_row.pack(fill="x", padx=12, pady=(0, 4))
    sel_all_box = ctk.CTkLabel(sel_row, text="", image=_chk_ch, cursor="hand2")
    sel_all_box.pack(side="left", padx=(4, 8))
    ctk.CTkLabel(
        sel_row, text=f"Student ({len(rows)})",
        fg_color=("gray85", "gray25"), corner_radius=6,
    ).pack(side="left", ipadx=8, ipady=2)

    scroll = ctk.CTkScrollableFrame(dialog)
    scroll.pack(fill="both", expand=True, padx=12, pady=4)

    checked_vars: list[ctk.BooleanVar] = []

    def update_count_label() -> None:
        n = sum(1 for v in checked_vars if v.get())
        confirm_btn.configure(text=f"Confirm {n}")
        # Master box reflects the rows: filled only when ALL are checked.
        all_on = bool(checked_vars) and n == len(checked_vars)
        try:
            sel_all_box.configure(image=_chk_ch if all_on else _chk_un)
        except Exception:
            pass

    def toggle_all(_event=None) -> None:
        # Select all, or clear if already all selected (mirrors the viewer).
        new = not (bool(checked_vars) and all(v.get() for v in checked_vars))
        for v in checked_vars:
            v.set(new)
        update_count_label()

    sel_all_box.bind("<Button-1>", toggle_all)

    for row in rows:
        v = ctk.BooleanVar(value=True)
        checked_vars.append(v)
        text = " · ".join(
            (row.get(c, "") or "")[:60] for c in display_columns
        )
        cb = ctk.CTkCheckBox(
            scroll, text=text, variable=v, command=update_count_label,
        )
        cb.pack(fill="x", padx=4, pady=1, anchor="w")

    btn_row = ctk.CTkFrame(dialog, fg_color="transparent")
    btn_row.pack(pady=(4, 12))

    def confirm(_event=None) -> None:
        selected = [rows[i] for i, v in enumerate(checked_vars) if v.get()]
        result["value"] = selected
        _save_dialog_geometry(dialog, "batch_review")
        try: dialog.grab_release()
        except Exception: pass
        try: dialog.destroy()
        except Exception: pass

    def cancel(_event=None) -> None:
        _save_dialog_geometry(dialog, "batch_review")
        try: dialog.grab_release()
        except Exception: pass
        try: dialog.destroy()
        except Exception: pass

    confirm_btn = ctk.CTkButton(
        btn_row, text=f"Confirm {len(rows)}", command=confirm, width=140,
    )
    confirm_btn.pack(side="left", padx=4)
    ctk.CTkButton(
        btn_row, text="Cancel", command=cancel, width=90,
        **SECONDARY_BTN_KWARGS,
    ).pack(side="left", padx=4)

    dialog.bind("<Escape>", cancel)
    dialog.protocol("WM_DELETE_WINDOW", cancel)

    parent.wait_window(dialog)
    return result["value"]


# ============================================================
# Note editor — one note section inside a scenario tab.
# ============================================================

# Op labels shown in the Filter editor dropdown. Stored verbatim
# in the YAML; the filter engine accepts both these long forms and
# the short forms via caseload_filter.normalize_op().
FILTER_OPS = (
    "is empty",
    "is not empty",
    "is",
    "is not",
    "contains",
    "does not contain",
    "is before",
    "is after",
    "is on",
    "is on or before",
    "is on or after",
    "is within",
    "more than",
    "less than",
    "at least",
    "at most",
)

# Reverse map: when loading a YAML scenario that uses the short
# (engine-internal) op form, translate to the long UI label so the
# dropdown displays it correctly.
_OP_SHORT_TO_LONG = {
    "empty": "is empty",
    "not_empty": "is not empty",
    "equals": "is",
    "not_equals": "is not",
    "not_contains": "does not contain",
    "before": "is before",
    "after": "is after",
    "on": "is on",
    "within": "is within",
    "gt": "more than",
    "lt": "less than",
    "gte": "at least",
    "lte": "at most",
}


class FilterRow:
    """One filter inside a batch scenario's Filters section. Layout:
    `<column ▾>  <op ▾>  <value ▾>  ✕`, plus a small hint label
    below that updates with the chosen op to show expected value
    formats (date shorthand, within-presets, numeric, etc.)."""

    def __init__(self, parent, columns: list[str], on_delete: Callable,
                 value_provider: Optional[Callable[[str], list]] = None):
        self.frame = ctk.CTkFrame(parent, fg_color="transparent")
        self.frame.grid_columnconfigure(2, weight=1)
        # Optional callback: column display-name → a small list of suggested
        # values (e.g. Passed/Returned/In Process, timezones, EA reasons).
        # When it returns values, the value field becomes a dropdown of them
        # (still free-text editable); [] keeps the field plain text.
        self._value_provider = value_provider
        self.column_combo = ctk.CTkComboBox(
            self.frame,
            values=columns if columns else ["(refresh columns)"],
            width=220,
            command=self._on_column_change,
        )
        self.column_combo.grid(row=0, column=0, sticky="w", padx=(0, 4), pady=2)
        self.op_combo = ctk.CTkComboBox(
            self.frame, values=list(FILTER_OPS), width=140,
            state="readonly", command=self._on_op_change,
        )
        self.op_combo.set("is")
        self.op_combo.grid(row=0, column=1, sticky="w", padx=(0, 4), pady=2)
        # Value is a combo (not entry) so we can dynamically populate
        # `values=…` with suggestions per op — within-presets for "is
        # within", relative-date shorthand for date ops, etc. State
        # stays "normal" so the user can still type freeform values
        # the suggestions don't cover.
        self.value_combo = ctk.CTkComboBox(
            self.frame, values=[""], width=200,
        )
        self.value_combo.set("")
        self.value_combo.grid(row=0, column=2, sticky="ew", padx=(0, 4), pady=2)
        # Calendar pick button — visible only for date ops, toggled
        # in `_on_op_change`. Lives between value and ✕ so the date
        # ops show: [column] [op] [value] [📅] [✕]; other ops show
        # the same row minus the 📅 cell.
        self.cal_btn = ctk.CTkButton(
            self.frame, text="📅", width=32, height=28,
            command=self._open_calendar_picker,
            **SECONDARY_BTN_KWARGS,
        )
        # Don't grid here — _on_op_change decides whether to show it
        # based on the initial op.
        ctk.CTkButton(
            self.frame, text="✕", width=28, height=28,
            **SECONDARY_BTN_KWARGS,
            command=lambda: on_delete(self),
        ).grid(row=0, column=4, padx=(4, 0), pady=2)
        # Hint label spans under the row; text changes with op via
        # `_on_op_change`. Lives in row=1 so it doesn't push the
        # main controls around.
        self.hint_label = ctk.CTkLabel(
            self.frame, text="", font=ctk.CTkFont(size=10),
            text_color=("gray40", "gray65"), anchor="w",
            justify="left",
        )
        self.hint_label.grid(
            row=1, column=0, columnspan=4,
            sticky="w", padx=(2, 4), pady=(0, 2),
        )
        # Initialize hint + value-combo suggestions for the default op.
        self._on_op_change("is")

    def _on_column_change(self, _col: str) -> None:
        """Column changed → re-derive the value-field suggestions for the
        current op (the suggestion list depends on which column is chosen)."""
        try:
            self._on_op_change(self.op_combo.get())
        except Exception:
            pass

    def _column_value_suggestions(self, op: str) -> list:
        """Value suggestions for the current column + `op`, via the provider.
        For date/number ops these are `{Other Column}` comparison refs; for
        text ops, a small fixed vocabulary. [] when nothing sensible applies
        (field stays free-text)."""
        if not self._value_provider:
            return []
        try:
            return self._value_provider(self.column_combo.get(), op) or []
        except Exception:
            return []

    def _on_op_change(self, op: str) -> None:
        """Sync the value-field suggestions and hint label to `op`.
        Called whenever the op dropdown selection changes (and once
        at init / once during `load` to set the initial state)."""
        date_ops = ("is before", "is after", "is on",
                    "is on or before", "is on or after")
        numeric_ops = ("more than", "less than", "at least", "at most")
        text_ops = ("is", "is not", "contains", "does not contain")

        # Calendar button is date-only. Default to hidden; date branch
        # below re-shows it. Wrapped in try so a partially-initialized
        # FilterRow (e.g. during load) doesn't blow up if the button
        # isn't laid out yet.
        try:
            self.cal_btn.grid_remove()
        except Exception:
            pass

        if op in date_ops:
            # Relative/absolute date shorthands PLUS any {Other Date Column}
            # comparison refs the provider offers (column-vs-column).
            col_refs = self._column_value_suggestions(op)
            self.value_combo.configure(state="normal", values=[
                "today",
                "today-7d", "today-14d", "today-30d",
                "today+7d", "today+14d", "today+30d",
            ] + col_refs)
            self.hint_label.configure(
                text="e.g. today, today-21d, 2026-05-21, 📅 — or {Another "
                     "Date Column} to compare two dates",
            )
            try:
                self.cal_btn.grid(row=0, column=3, padx=(0, 4), pady=2)
            except Exception:
                pass
        elif op == "is within":
            self.value_combo.configure(
                state="normal", values=list(caseload_filter.WITHIN_PRESETS),
            )
            self.hint_label.configure(
                text="Pick a preset: "
                     + ", ".join(caseload_filter.WITHIN_PRESETS) + ".",
            )
        elif op in numeric_ops:
            col_refs = self._column_value_suggestions(op)
            self.value_combo.configure(
                state="normal", values=col_refs or [""])
            self.hint_label.configure(
                text="Number — e.g. 0, 1.5, 12 — or {Another Number Column} "
                     "to compare two columns.",
            )
        elif op in ("is empty", "is not empty"):
            self.value_combo.set("")
            self.value_combo.configure(state="disabled", values=[""])
            self.hint_label.configure(text="(no value needed for this op)")
        elif op in text_ops:
            suggestions = self._column_value_suggestions(op)
            self.value_combo.configure(
                state="normal", values=suggestions or [""])
            if suggestions:
                self.hint_label.configure(
                    text="Pick a value or type your own. Case-insensitive; "
                         "comma-separate for OR match.",
                )
            else:
                self.hint_label.configure(
                    text="Text — type the value, or comma-separate for OR "
                         "match (e.g. 'Pass, NoPass'). Case-insensitive.",
                )
        else:
            self.value_combo.configure(state="normal", values=[""])
            self.hint_label.configure(text="")

    def _open_calendar_picker(self) -> None:
        """Pop the small monthly calendar widget. If the field already
        contains a parseable absolute date, the picker opens to that
        month; otherwise it opens to the current month. The picked
        date lands in the value combo as YYYY-MM-DD — the engine
        accepts that format directly via _parse_date_cell."""
        from datetime import datetime as _dt
        current = (self.value_combo.get() or "").strip()
        initial = None
        if current and current != "today":
            for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
                try:
                    initial = _dt.strptime(current, fmt).date()
                    break
                except ValueError:
                    continue
        picked = prompt_calendar_pick(
            self.frame.winfo_toplevel(), initial_date=initial,
        )
        if picked is not None:
            self.value_combo.set(picked.isoformat())

    def set_columns(self, columns: list[str]) -> None:
        """Replace the column dropdown's option list. Preserves the
        currently-selected value if it's still in the new list."""
        current = self.column_combo.get()
        new_values = columns if columns else ["(refresh columns)"]
        self.column_combo.configure(values=new_values)
        if current in new_values:
            self.column_combo.set(current)

    def load(self, filt: dict) -> None:
        # Translate stored column (could be raw CSV header or display
        # name from prior saves) to display name for the dropdown.
        # `resolve_column` at runtime maps either form back, so this
        # is purely a UI-side display nicety.
        self.column_combo.set(
            caseload_csv.display_for_column(filt.get("column", "")),
        )
        op = filt.get("op", "")
        long_op = _OP_SHORT_TO_LONG.get(op, op)
        self.op_combo.set(long_op)
        # Run op-change BEFORE writing the value, since "is empty" /
        # "is not empty" disables the value field and a write would
        # be silently rejected.
        self._on_op_change(long_op)
        value = filt.get("value", "")
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value)
        if value and long_op not in ("is empty", "is not empty"):
            self.value_combo.set(str(value))

    def serialize(self) -> dict:
        return {
            "column": self.column_combo.get().strip(),
            "op": self.op_combo.get(),
            "value": self.value_combo.get(),
        }


class PromptRow:
    """One prompt in a scenario's prompts: list. Editable row with
    var name, label text, multiline checkbox, and delete button.
    `prefill` is carried through save/load as an attribute so hand-
    edited YAML survives — we don't expose it in the UI yet because
    most users don't need it."""

    def __init__(
        self, parent, on_delete: Callable, prefill: str = "",
    ):
        self.frame = ctk.CTkFrame(parent, fg_color="transparent")
        self.frame.grid_columnconfigure(1, weight=1)
        self._prefill = prefill

        self.var_entry = ctk.CTkEntry(
            self.frame, placeholder_text="var (e.g. summary)", width=140,
        )
        self.var_entry.grid(row=0, column=0, sticky="w", padx=(0, 4), pady=2)
        self.label_entry = ctk.CTkEntry(
            self.frame, placeholder_text="dialog label",
        )
        self.label_entry.grid(row=0, column=1, sticky="ew", padx=(0, 4), pady=2)
        self.multiline_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            self.frame, text="multiline", variable=self.multiline_var, width=100,
        ).grid(row=0, column=2, sticky="w", padx=(0, 4), pady=2)
        ctk.CTkButton(
            self.frame, text="✕", width=28, height=28,
            **SECONDARY_BTN_KWARGS,
            command=lambda: on_delete(self),
        ).grid(row=0, column=3, padx=(4, 0), pady=2)

    def load(self, prompt) -> None:
        """Populate widgets from a `scenarios.Prompt` dataclass."""
        self.var_entry.delete(0, "end")
        self.var_entry.insert(0, prompt.var)
        self.label_entry.delete(0, "end")
        self.label_entry.insert(0, prompt.label or "")
        self.multiline_var.set(prompt.multiline)
        self._prefill = prompt.prefill

    def serialize(self) -> dict:
        out = {
            "var": self.var_entry.get().strip(),
            "label": self.label_entry.get(),
            "multiline": self.multiline_var.get(),
        }
        if self._prefill:
            out["prefill"] = self._prefill
        return out


class NoteEditor:
    """Widgets for editing a single note. Mirrors the Caseload form:
    Interaction Format, Interaction Type, Academic Activities, Body.
    Collapsible via the ▼/▶ header button."""

    def __init__(
        self, parent, index: int,
        on_delete: Optional[Callable] = None,
        get_scenario_vars: Optional[Callable[[], list[str]]] = None,
    ):
        self.index = index
        self._collapsed = False
        # Callable returning the live list of `var` names from the
        # ScenarioEditor's variable rows — refreshed each time the
        # body field gets focus so freshly-added variables show up.
        self._get_scenario_vars = get_scenario_vars
        self.frame = ctk.CTkFrame(parent)
        self.frame.grid_columnconfigure(0, weight=1)

        # Header row — collapse toggle (fills width) + optional ✕ delete.
        header_row = ctk.CTkFrame(self.frame, fg_color="transparent")
        header_row.grid(row=0, column=0, sticky="ew", padx=4, pady=(4, 0))
        header_row.grid_columnconfigure(0, weight=1)
        self.toggle_btn = ctk.CTkButton(
            header_row, text=self._header_text(),
            command=self._toggle_collapse, anchor="w", height=28,
            fg_color="transparent", text_color=("gray10", "gray90"),
            hover_color=("gray85", "gray25"),
            font=ctk.CTkFont(size=13, weight="bold"),
        )
        self.toggle_btn.grid(row=0, column=0, sticky="ew")
        if on_delete is not None:
            # Visible default fill + red hover so it reads as a delete
            # affordance instead of disappearing into the header.
            ctk.CTkButton(
                header_row, text="✕  Delete note", width=110, height=28,
                fg_color=("gray80", "gray30"),
                hover_color=("#e74c3c", "#c0392b"),
                text_color=("gray10", "gray90"),
                command=lambda: on_delete(self),
            ).grid(row=0, column=1, padx=(8, 4))

        # Content frame — everything below the header lives here so we
        # can grid_remove() it to collapse.
        content = ctk.CTkFrame(self.frame, fg_color="transparent")
        content.grid(row=1, column=0, sticky="ew", padx=4, pady=(0, 4))
        content.grid_columnconfigure(0, weight=1)
        self.content = content

        row = 0
        # Override course code — per-note. Empty falls back to the
        # auto-detected course code at fire time. Pin a value here
        # when this note needs to file against a specific course
        # different from whatever the active record is. (Replaces
        # the old main-window "Override Course Code" field.)
        cc_row = ctk.CTkFrame(content, fg_color="transparent")
        cc_row.grid(row=row, column=0, sticky="ew", padx=8, pady=(4, 0))
        cc_row.grid_columnconfigure(1, weight=1)
        self._cc_override_row = cc_row  # advanced-only (see apply_advanced_visibility)
        ctk.CTkLabel(
            cc_row, text="Override course code:", width=180, anchor="w",
        ).grid(row=0, column=0, sticky="w")
        self.course_code_override_entry = ctk.CTkEntry(
            cc_row, placeholder_text="(empty = auto-detect from active record)",
            width=240,
        )
        self.course_code_override_entry.grid(row=0, column=1, sticky="w")
        row += 1

        # Interaction Format
        ctk.CTkLabel(content, text="Interaction Format").grid(
            row=row, column=0, sticky="w", padx=8, pady=(8, 0)
        )
        row += 1
        self.format_var = ctk.StringVar(value="Single Interaction")
        fmt_frame = ctk.CTkFrame(content, fg_color="transparent")
        fmt_frame.grid(row=row, column=0, sticky="w", padx=8)
        for fmt in INTERACTION_FORMATS:
            ctk.CTkRadioButton(
                fmt_frame, text=fmt, variable=self.format_var, value=fmt,
                command=self._on_format_change,
            ).pack(side="left", padx=(0, 12))

        # Interaction Type
        row += 1
        ctk.CTkLabel(content, text="Interaction Type").grid(
            row=row, column=0, sticky="w", padx=8, pady=(6, 0)
        )
        row += 1
        self.type_combo = ctk.CTkComboBox(
            content, values=INTERACTION_TYPES_SINGLE, state="readonly", width=300,
            command=self._on_type_change,
        )
        self.type_combo.grid(row=row, column=0, sticky="w", padx=8)

        # Academic Activities
        row += 1
        ctk.CTkLabel(content, text="Academic Activities").grid(
            row=row, column=0, sticky="w", padx=8, pady=(8, 0)
        )
        row += 1
        self.activity_vars: dict[str, ctk.BooleanVar] = {}
        self.activity_checkboxes: list[ctk.CTkCheckBox] = []
        activity_frame = ctk.CTkFrame(content, fg_color="transparent")
        activity_frame.grid(row=row, column=0, sticky="w", padx=8)
        for i, label in enumerate(ACADEMIC_ACTIVITY_LABELS):
            v = ctk.BooleanVar(value=False)
            self.activity_vars[label] = v
            cb = ctk.CTkCheckBox(activity_frame, text=label, variable=v)
            cb.grid(row=i, column=0, sticky="w", pady=1)
            self.activity_checkboxes.append(cb)

        # Body
        row += 1
        ctk.CTkLabel(content, text="Body").grid(
            row=row, column=0, sticky="w", padx=8, pady=(8, 0)
        )
        # Variable-insert toolbar above the body. Buttons drop
        # `{{var_name}}` at the cursor. Standard vars first, then
        # scenario-defined vars highlighted in yellow. Rebuilt on
        # FocusIn so newly-defined variables show up without needing
        # to close and reopen the editor.
        row += 1
        self._var_buttons_row = ctk.CTkFrame(content, fg_color="transparent")
        self._var_buttons_row.grid(
            row=row, column=0, sticky="ew", padx=8, pady=(2, 2),
        )
        row += 1
        self.body_text = ctk.CTkTextbox(content, height=80, wrap="word")
        self.body_text.grid(row=row, column=0, sticky="ew", padx=8, pady=(0, 4))
        register_font_box("editor", self.body_text)  # adjustable text size
        self.body_text.bind("<FocusIn>", lambda _e: self._build_var_buttons())
        self._build_var_buttons()

        # Prompt-for-extra-text toggle. When on, firing the scenario
        # pops a dialog pre-filled with this body so the user can edit
        # / paste before it's submitted (same size cap applies).
        row += 1
        self.enter_additional_text_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            content, text="Edit note at fire time "
                          "(body, course, academic activities, Essential Action)",
            variable=self.enter_additional_text_var,
        ).grid(row=row, column=0, sticky="w", padx=8, pady=(0, 4))

        # Append-clipboard toggle. When on, the clipboard at fire time
        # is read on the main thread and appended after the body
        # (capped at 200 lines / 25000 chars total — images replaced
        # with [IMAGE NOT INCLUDED] placeholder).
        # Advanced-only: hidden in basic mode unless the note already
        # has it enabled.
        row += 1
        self.append_clipboard_var = ctk.BooleanVar(value=False)
        self._append_clipboard_checkbox = ctk.CTkCheckBox(
            content, text="Append clipboard contents after body",
            variable=self.append_clipboard_var,
        )
        self._append_clipboard_checkbox.grid(row=row, column=0, sticky="w", padx=8, pady=(0, 4))

        # Submit toggle. Unchecking leaves the form filled for manual
        # review — and the scenario's tab-close step is also skipped
        # whenever any note in the scenario opted out of auto-submit.
        # The warning label next to the checkbox makes the off state
        # visible at a glance so a stray click doesn't silently leave
        # notes unsubmitted across an entire batch.
        row += 1
        submit_row = ctk.CTkFrame(content, fg_color="transparent")
        submit_row.grid(row=row, column=0, sticky="w", padx=8, pady=(0, 8))
        self.submit_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            submit_row, text="Submit and close automatically",
            variable=self.submit_var,
            command=self._update_submit_warning,
        ).pack(side="left")
        self._submit_warning_label = ctk.CTkLabel(
            submit_row, text="",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=("#7a4f00", "#ffd166"),
            fg_color=("#fff3c4", "#3a3520"),
            corner_radius=4,
        )
        # Visibility managed by _update_submit_warning — packed only
        # when the box is unchecked.
        self._update_submit_warning()

    def set_index(self, index: int) -> None:
        """Renumber this note in-place (Note 1, Note 2, …). Called after
        a sibling note is added or deleted."""
        self.index = index
        self.toggle_btn.configure(text=self._header_text())

    def _header_text(self) -> str:
        arrow = "▶" if self._collapsed else "▼"
        return f"{arrow}  Note {self.index + 1}"

    def _toggle_collapse(self) -> None:
        self._collapsed = not self._collapsed
        if self._collapsed:
            self.content.grid_remove()
        else:
            self.content.grid()
        self.toggle_btn.configure(text=self._header_text())

    def _insert_var_in_body(self, var: str) -> None:
        """Drop `{{var}}` at the body's current insert cursor."""
        self.body_text.insert("insert", f"{{{{{var}}}}}")
        self.body_text.focus_force()

    def _build_var_buttons(self) -> None:
        """(Re)build the variable-insert buttons above the body.
        Idempotent — destroys existing children before laying out the
        current set. Called once at init and on every FocusIn of the
        body so scenario-variable additions show up without a tab
        reload. Grid layout wraps to 8 buttons per row."""
        for w in list(self._var_buttons_row.winfo_children()):
            try: w.destroy()
            except Exception: pass

        ctk.CTkLabel(
            self._var_buttons_row, text="Insert:", anchor="w",
            font=ctk.CTkFont(size=11, weight="bold"),
        ).grid(row=0, column=0, sticky="w", padx=(0, 6), pady=1)

        def make_btn(display: str, var: str, slot: int, highlight: bool = False):
            kw = dict(SECONDARY_BTN_KWARGS)
            if highlight:
                kw["fg_color"] = ("#fff3c4", "#5a4500")
                kw["text_color"] = ("gray10", "gray95")
            r, c = divmod(slot, 8)
            ctk.CTkButton(
                self._var_buttons_row, text=display, width=82, height=22,
                command=lambda v=var: self._insert_var_in_body(v),
                font=ctk.CTkFont(size=10),
                **kw,
            ).grid(row=r, column=c + 1, padx=1, pady=1, sticky="w")

        slot = 0
        for display, var in (_TEMPLATE_INSERT_VARS_STUDENT
                             + _TEMPLATE_INSERT_VARS_PM
                             + _TEMPLATE_INSERT_VARS_USER):
            make_btn(display, var, slot)
            slot += 1
        custom = self._get_scenario_vars() if self._get_scenario_vars else []
        for var in custom:
            make_btn(var, var, slot, highlight=True)
            slot += 1

    def _on_format_change(self) -> None:
        fmt = self.format_var.get()
        new_values = types_for_format(fmt)
        self.type_combo.configure(values=new_values)
        if self.type_combo.get() not in new_values:
            # Current selection isn't valid in the new format — clear it.
            self.type_combo.set("")
        self._update_activity_state()

    def _on_type_change(self, _choice=None) -> None:
        self._update_activity_state()

    def _update_activity_state(self) -> None:
        disabled = activities_disabled_for(self.format_var.get(), self.type_combo.get())
        state = "disabled" if disabled else "normal"
        for cb in self.activity_checkboxes:
            cb.configure(state=state)

    def load(self, note: NoteData) -> None:
        fmt = note.interaction_format or "Single Interaction"
        self.format_var.set(fmt)
        self.type_combo.configure(values=types_for_format(fmt))
        self.type_combo.set(note.interaction_type)
        for label, var in self.activity_vars.items():
            var.set(label in note.academic_activities)
        self.body_text.delete("1.0", "end")
        self.body_text.insert("1.0", note.body)
        self.submit_var.set(note.submit)
        self._update_submit_warning()
        self.append_clipboard_var.set(note.append_clipboard)
        self.enter_additional_text_var.set(note.enter_additional_text)
        # Course code override (per-note, replaces the old main-window
        # global field). Empty = auto-detect at fire time.
        self.course_code_override_entry.delete(0, "end")
        if note.course_code_override:
            self.course_code_override_entry.insert(0, note.course_code_override)
        self._update_activity_state()

    def _update_submit_warning(self) -> None:
        """Show / hide the yellow warning chip next to the Submit
        checkbox based on its current state. Called on init, on
        every load, and on every checkbox click."""
        try:
            if self.submit_var.get():
                self._submit_warning_label.pack_forget()
            else:
                self._submit_warning_label.configure(
                    text="  ⚠  won't auto-submit — manual click in Salesforce",
                )
                self._submit_warning_label.pack(side="left", padx=(10, 0))
        except Exception:
            pass

    def apply_advanced_visibility(self, advanced: bool) -> None:
        """Hide advanced-only note rows in basic mode unless this note
        already uses them. Same "show if configured" rule the
        ScenarioEditor uses for its advanced rows."""
        try:
            has_value = bool(self.append_clipboard_var.get())
            if advanced or has_value:
                self._append_clipboard_checkbox.grid()
            else:
                self._append_clipboard_checkbox.grid_remove()
        except Exception:
            pass
        # Override course code — advanced-only; shown if already set.
        try:
            has_cc = bool(self.course_code_override_entry.get().strip())
            if advanced or has_cc:
                self._cc_override_row.grid()
            else:
                self._cc_override_row.grid_remove()
        except Exception:
            pass

    def serialize(self) -> dict:
        out: dict = {
            "interaction_format": self.format_var.get(),
            "interaction_type": self.type_combo.get(),
            "body": self.body_text.get("1.0", "end-1c"),
            "academic_activities": [
                label for label, var in self.activity_vars.items() if var.get()
            ],
            "submit": self.submit_var.get(),
            "append_clipboard": self.append_clipboard_var.get(),
            "enter_additional_text": self.enter_additional_text_var.get(),
        }
        # Only emit the override key when non-empty so scenarios.yaml
        # stays clean for the (common) auto-detect case.
        cc_override = self.course_code_override_entry.get().strip()
        if cc_override:
            out["course_code_override"] = cc_override
        return out


# ============================================================
# Scenario editor — one per scenario, swapped into the editor pane's
# shared content frame by the stacked group tab-strips.
# ============================================================

class ScenarioEditor:
    def __init__(
        self, parent, scenario: ScenarioConfig,
        capture_handler=None,
        get_columns: Optional[Callable[[], list[str]]] = None,
        refresh_columns: Optional[Callable[[], list[str]]] = None,
        get_value_suggestions: Optional[Callable[[str], list]] = None,
        get_groups: Optional[Callable[[], list[str]]] = None,
        get_scenario_group: Optional[Callable[[], str]] = None,
        on_group_change: Optional[Callable[[str, str], None]] = None,
    ):
        self.scenario_name = scenario.name
        self.close_tab_after = scenario.close_tab_after
        # `email` is now fully exposed via the editor; `batch.preview`
        # still rides as a passive round-trip field for now.
        self._batch_preview = scenario.batch.preview if scenario.batch else True
        self.capture_handler = capture_handler  # callable(on_done)
        # Caseload-column hooks for the Filters section. `get_columns`
        # returns whatever's cached now; `refresh_columns` triggers a
        # fresh CSV download (blocking) and returns the updated list.
        self._get_columns = get_columns or (lambda: [])
        self._refresh_columns = refresh_columns or (lambda: [])
        self._get_value_suggestions = get_value_suggestions
        self.frame = ctk.CTkScrollableFrame(parent)
        self.frame.grid_columnconfigure(0, weight=1)

        # Name (editable). Save → tabs, buttons, hotkeys all rebuild
        # under the new name.
        row = 0
        ctk.CTkLabel(
            self.frame, text="Name",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).grid(row=row, column=0, sticky="w", padx=8, pady=(8, 0))
        row += 1
        self.name_entry = ctk.CTkEntry(
            self.frame, placeholder_text="e.g. welcome, approval, custom", width=300,
        )
        self.name_entry.grid(row=row, column=0, sticky="w", padx=8, pady=(0, 8))

        # Group selector — reassign this action's group without opening
        # the group-settings dialog. "(none)" = ungrouped.
        self._get_groups = get_groups or (lambda: [])
        self._get_scenario_group = get_scenario_group or (lambda: "")
        self._on_group_change = on_group_change
        row += 1
        group_row = ctk.CTkFrame(self.frame, fg_color="transparent")
        group_row.grid(row=row, column=0, sticky="w", padx=8, pady=(0, 8))
        ctk.CTkLabel(
            group_row, text="Group", font=ctk.CTkFont(size=12, weight="bold"),
        ).pack(side="left", padx=(0, 8))
        self._group_options = ["(none)"] + list(self._get_groups())
        cur_group = self._get_scenario_group() or "(none)"
        if cur_group not in self._group_options:
            cur_group = "(none)"
        self.group_var = ctk.StringVar(value=cur_group)
        self.group_menu = ctk.CTkOptionMenu(
            group_row, values=self._group_options, variable=self.group_var,
            width=220, command=self._on_group_menu_change,
        )
        self.group_menu.pack(side="left")

        # Hotkey — entry field + "Press to set" capture button.
        row += 1
        ctk.CTkLabel(
            self.frame, text="Hotkey",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).grid(row=row, column=0, sticky="w", padx=8, pady=(8, 0))
        row += 1
        hotkey_row = ctk.CTkFrame(self.frame, fg_color="transparent")
        hotkey_row.grid(row=row, column=0, sticky="w", padx=8, pady=(0, 8))
        self.hotkey_entry = ctk.CTkEntry(
            hotkey_row, placeholder_text="e.g. F3 or Ctrl+Shift+1", width=200,
        )
        self.hotkey_entry.pack(side="left")
        ctk.CTkButton(
            hotkey_row, text="Press to set", width=110, command=self._start_capture,
        ).pack(side="left", padx=(8, 0))

        # Scenario variables — advanced, hidden by default. When on, a
        # section appears below the toggle for defining {{var_name}}
        # values that get asked at fire time and substituted into the
        # email + every note body. Up here (not between email/notes)
        # because conceptually the variables apply to BOTH sections
        # below.
        row += 1
        self.use_vars_var = ctk.BooleanVar(value=False)
        self._use_vars_checkbox = ctk.CTkCheckBox(
            self.frame,
            text="Use action variables (advanced — applies to email + notes below)",
            variable=self.use_vars_var,
            command=self._on_use_vars_toggled,
        )
        self._use_vars_checkbox_row = row
        self._use_vars_checkbox.grid(row=row, column=0, sticky="w", padx=8, pady=(8, 4))
        row += 1
        self._vars_section_row = row
        self._build_vars_section()
        # Visibility set by load() based on scenario.prompts.
        # The toggle checkbox + section are advanced-only — hidden in
        # basic mode unless the scenario already defines variables.

        # Batch-mode toggle. When on, find-first is hidden (mutually
        # exclusive) and the Filters section appears underneath.
        row += 1
        self.batch_mode_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            self.frame,
            text="Batch mode (apply to all matching students)",
            variable=self.batch_mode_var,
            command=self._on_batch_mode_toggled,
        ).grid(row=row, column=0, sticky="w", padx=8, pady=(0, 4))
        row += 1
        self._batch_section_row = row
        self._build_batch_section()
        # Visibility set by load() based on scenario.batch != None.

        # Find-student-first toggle. When on, firing the scenario pops
        # an entry dialog asking for the student name; the worker
        # navigates to them before filling notes. Hidden when batch
        # mode is on (mutually exclusive).
        row += 1
        self._find_first_row = row
        self.find_first_var = ctk.BooleanVar(value=False)
        self.find_first_checkbox = ctk.CTkCheckBox(
            self.frame,
            text="Find student first (prompt at fire time)",
            variable=self.find_first_var,
        )
        self.find_first_checkbox.grid(row=row, column=0, sticky="w", padx=8, pady=(0, 8))

        # Caseload-panel action toggle — surfaces this scenario in the
        # caseload panel's "Fire scenario" menus. Disabled for batch
        # scenarios (the panel does batch-style work via filter + select +
        # single action), with a hint shown while batch mode is on.
        row += 1
        self.panel_action_var = ctk.BooleanVar(value=False)
        self.panel_action_checkbox = ctk.CTkCheckBox(
            self.frame,
            text="Show as a caseload-panel action",
            variable=self.panel_action_var,
        )
        self.panel_action_checkbox.grid(
            row=row, column=0, sticky="w", padx=8, pady=(0, 2))
        row += 1
        self.panel_action_hint = ctk.CTkLabel(
            self.frame,
            text="Batch actions run from the main window — in the panel, "
                 "filter the view and apply a single action instead.",
            font=ctk.CTkFont(size=10), text_color=("gray45", "gray60"),
            wraplength=420, justify="left", anchor="w",
        )
        self.panel_action_hint.grid(
            row=row, column=0, sticky="w", padx=(28, 8), pady=(0, 8))
        self.panel_action_hint.grid_remove()  # shown only in batch mode

        # Send-email toggle + email section (sub-frame visible only
        # when toggle is on). Toggle commands grid_remove/.grid so
        # the row collapses to nothing when emails aren't used.
        row += 1
        self.send_email_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            self.frame,
            text="Send email (open Outlook draft before filing notes)",
            variable=self.send_email_var,
            command=self._on_send_email_toggled,
        ).grid(row=row, column=0, sticky="w", padx=8, pady=(0, 4))
        row += 1
        self._email_section_row = row
        self._build_email_section()
        # Visibility set by load() based on scenario.email != None.

        # Send-text toggle + text (Mongoose) section, same collapse pattern.
        row += 1
        self.send_text_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            self.frame,
            text="Send text (Mongoose)",
            variable=self.send_text_var,
            command=self._on_send_text_toggled,
        ).grid(row=row, column=0, sticky="w", padx=8, pady=(0, 4))
        row += 1
        self._text_section_row = row
        self._build_text_section()
        # Visibility set by load() based on scenario.text != None.

        # Notes live in their own container so add/delete can just
        # pack/destroy children without disturbing the outer grid rows.
        row += 1
        self.notes_container = ctk.CTkFrame(self.frame, fg_color="transparent")
        self.notes_container.grid(row=row, column=0, sticky="ew", padx=0, pady=0)
        self.notes_container.grid_columnconfigure(0, weight=1)
        self.note_editors: list[NoteEditor] = []
        for note in scenario.notes:
            self._add_note_editor(note)

        # + Add note button under the notes container.
        row += 1
        ctk.CTkButton(
            self.frame, text="+ Add note",
            command=self._add_note, width=120, height=32,
        ).grid(row=row, column=0, sticky="w", padx=8, pady=(4, 8))

        self.load(scenario)

    @property
    def current_name(self) -> str:
        return self.name_entry.get().strip()

    def _on_group_menu_change(self, choice: str) -> None:
        """Group dropdown changed — hand the new assignment back to the
        app (which mutates groups + persists). '(none)' = ungrouped."""
        if self._on_group_change is None:
            return
        target = "" if choice == "(none)" else choice
        try:
            self._on_group_change(self.current_name, target)
        except Exception:
            pass

    def refresh_group_options(self) -> None:
        """Re-read the available groups + this action's current group and
        update the dropdown. Cheap; called when an editor is reused across
        a rebuild so its group list/value can't go stale."""
        try:
            opts = ["(none)"] + list(self._get_groups())
            cur = self._get_scenario_group() or "(none)"
            if cur not in opts:
                cur = "(none)"
            self.group_menu.configure(values=opts)
            self.group_var.set(cur)
        except Exception:
            pass

    def _start_capture(self) -> None:
        if self.capture_handler is None:
            return

        def apply(combo: str) -> None:
            if combo:
                self.hotkey_entry.delete(0, "end")
                self.hotkey_entry.insert(0, combo)

        self.capture_handler(apply)

    # ----- Batch / Filters section -----

    def _build_batch_section(self) -> None:
        """Construct the Filters container + action buttons. Always
        created — visibility toggled by `_on_batch_mode_toggled`."""
        frame = ctk.CTkFrame(self.frame)
        frame.grid_columnconfigure(0, weight=1)
        self._batch_section = frame

        ctk.CTkLabel(
            frame, text="Filters",
            font=ctk.CTkFont(size=12, weight="bold"),
        ).grid(row=0, column=0, sticky="w", padx=8, pady=(6, 0))

        self.filters_container = ctk.CTkFrame(frame, fg_color="transparent")
        self.filters_container.grid(
            row=1, column=0, sticky="ew", padx=4, pady=(0, 4),
        )
        self.filters_container.grid_columnconfigure(0, weight=1)
        self.filter_rows: list[FilterRow] = []

        action_row = ctk.CTkFrame(frame, fg_color="transparent")
        action_row.grid(row=2, column=0, sticky="w", padx=4, pady=(0, 8))
        ctk.CTkButton(
            action_row, text="+ Add filter",
            width=110, command=self._add_filter_row,
        ).pack(side="left")
        ctk.CTkButton(
            action_row, text="↻ Refresh columns",
            width=140, command=self._refresh_batch_columns,
            **SECONDARY_BTN_KWARGS,
        ).pack(side="left", padx=(8, 0))

    def _add_filter_row(self, prefilled: Optional[dict] = None) -> FilterRow:
        cols = self._get_columns() or []
        row = FilterRow(
            self.filters_container, cols, on_delete=self._delete_filter_row,
            value_provider=self._get_value_suggestions,
        )
        row.frame.pack(fill="x", padx=4, pady=2)
        if prefilled:
            row.load(prefilled)
        self.filter_rows.append(row)
        return row

    def _delete_filter_row(self, row: FilterRow) -> None:
        try:
            self.filter_rows.remove(row)
        except ValueError:
            return
        try:
            row.frame.destroy()
        except Exception:
            pass

    def _refresh_batch_columns(self) -> None:
        """Trigger a fresh CSV download (blocks the UI with the busy
        spinner), then push the new column list to every filter row."""
        cols = self._refresh_columns()
        for row in self.filter_rows:
            row.set_columns(cols)

    def _on_batch_mode_toggled(self) -> None:
        """Show/hide the Filters section; mutually exclusive with the
        find-first checkbox."""
        if self.batch_mode_var.get():
            self._batch_section.grid(
                row=self._batch_section_row, column=0,
                sticky="ew", padx=8, pady=(0, 6),
            )
            self.find_first_checkbox.grid_remove()
            # Force-off find_first so a hidden checkbox can't still
            # save to YAML via serialize().
            self.find_first_var.set(False)
            # Batch scenarios can't be panel actions — disable + clear the
            # toggle and explain why.
            self.panel_action_var.set(False)
            self.panel_action_checkbox.configure(state="disabled")
            self.panel_action_hint.grid()
        else:
            self._batch_section.grid_remove()
            self.find_first_checkbox.grid(
                row=self._find_first_row, column=0,
                sticky="w", padx=8, pady=(0, 8),
            )
            self.panel_action_checkbox.configure(state="normal")
            self.panel_action_hint.grid_remove()

    # ----- Scenario variables section -----

    def _build_vars_section(self) -> None:
        """Build the 'Scenario variables' section. Doesn't grid it —
        visibility is controlled by `_on_use_vars_toggled`. Each row
        is a PromptRow (internal class name kept as-is to avoid a
        cross-cutting rename; the UI calls them 'variables')."""
        frame = ctk.CTkFrame(self.frame, fg_color=("gray92", "gray18"))
        frame.grid_columnconfigure(0, weight=1)
        self._vars_section = frame
        ctk.CTkLabel(
            frame, text="Action variables",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).grid(row=0, column=0, sticky="w", padx=8, pady=(6, 0))
        ctk.CTkLabel(
            frame,
            text=(
                "Values you'll be asked for when this action fires. "
                "Use {{var_name}} in the email subject/body and any note "
                "body to drop the typed value in."
            ),
            font=ctk.CTkFont(size=11),
            text_color=("gray35", "gray70"),
            wraplength=620, justify="left",
        ).grid(row=1, column=0, sticky="w", padx=8, pady=(0, 4))
        self.prompts_container = ctk.CTkFrame(frame, fg_color="transparent")
        self.prompts_container.grid(
            row=2, column=0, sticky="ew", padx=4, pady=(0, 4),
        )
        self.prompts_container.grid_columnconfigure(0, weight=1)
        self.prompt_rows: list[PromptRow] = []
        ctk.CTkButton(
            frame, text="+ Add variable", width=130,
            command=self._add_prompt_row,
        ).grid(row=3, column=0, sticky="w", padx=8, pady=(0, 6))

    def _on_use_vars_toggled(self) -> None:
        """Show / hide the scenario-variables section based on the
        toggle. Serialize-side gating in `serialize()` also keys off
        this flag, so a hidden section's rows won't get saved."""
        if self.use_vars_var.get():
            self._vars_section.grid(
                row=self._vars_section_row, column=0,
                sticky="ew", padx=8, pady=(0, 6),
            )
        else:
            self._vars_section.grid_remove()

    def _add_prompt_row(self, prefilled=None) -> PromptRow:
        row = PromptRow(
            self.prompts_container,
            on_delete=self._delete_prompt_row,
            prefill=getattr(prefilled, "prefill", "") if prefilled else "",
        )
        row.frame.pack(fill="x", padx=4, pady=2)
        if prefilled is not None:
            row.load(prefilled)
        self.prompt_rows.append(row)
        return row

    def _delete_prompt_row(self, row: PromptRow) -> None:
        try:
            self.prompt_rows.remove(row)
        except ValueError:
            return
        try:
            row.frame.destroy()
        except Exception:
            pass

    # ----- Email section -----

    def _build_email_section(self) -> None:
        """Construct the email-config widgets inside a sub-frame.
        Always created — visibility is toggled by `_on_send_email_toggled`."""
        from src import outlook_signature

        frame = ctk.CTkFrame(self.frame)
        frame.grid_columnconfigure(1, weight=1)
        self._email_section = frame

        # Subject
        ctk.CTkLabel(frame, text="Subject").grid(
            row=0, column=0, sticky="w", padx=8, pady=(6, 0),
        )
        self.email_subject_entry = ctk.CTkEntry(
            frame, placeholder_text="e.g. Welcome to {{course_code}}, {{first_name}}",
        )
        self.email_subject_entry.grid(
            row=0, column=1, sticky="ew", padx=8, pady=(6, 0),
        )

        # Body template (dropdown over templates_dir() + Open button)
        ctk.CTkLabel(frame, text="Body template").grid(
            row=1, column=0, sticky="w", padx=8, pady=(4, 0),
        )
        tpl_row = ctk.CTkFrame(frame, fg_color="transparent")
        tpl_row.grid(row=1, column=1, sticky="ew", padx=8, pady=(4, 0))
        tpl_row.grid_columnconfigure(0, weight=1)
        template_names = self._available_template_files()
        self.email_body_combo = ctk.CTkComboBox(
            tpl_row, values=template_names or ["(none in templates folder)"],
            state="readonly" if template_names else "normal",
        )
        self.email_body_combo.grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(
            tpl_row, text="Edit", width=56,
            command=self._edit_template_in_app,
        ).grid(row=0, column=1, padx=(6, 0))
        ctk.CTkButton(
            tpl_row, text="New", width=56,
            command=self._new_template,
            **SECONDARY_BTN_KWARGS,
        ).grid(row=0, column=2, padx=(4, 0))
        ctk.CTkButton(
            tpl_row, text="Open", width=56,
            command=self._open_template_externally,
            **SECONDARY_BTN_KWARGS,
        ).grid(row=0, column=3, padx=(4, 0))

        # Email override — the To: field on the outgoing message
        # (variable substitution allowed). Empty falls back to
        # {{student_email}} from the caseload row.
        # Advanced-only: hidden in basic mode unless the scenario
        # already has a value set.
        self._email_to_label = ctk.CTkLabel(frame, text="Email override")
        self._email_to_label.grid(
            row=2, column=0, sticky="w", padx=8, pady=(4, 0),
        )
        self.email_to_entry = ctk.CTkEntry(
            frame, placeholder_text="empty = {{student_email}}; or e.g. test{{first_name}}{{last_name}}@wgu.edu",
        )
        self.email_to_entry.grid(row=2, column=1, sticky="ew", padx=8, pady=(4, 0))

        # Signature (dropdown over %APPDATA%\Microsoft\Signatures)
        ctk.CTkLabel(frame, text="Signature").grid(
            row=3, column=0, sticky="w", padx=8, pady=(4, 0),
        )
        sig_names = [""] + outlook_signature.list_signature_names()
        self.email_signature_combo = ctk.CTkComboBox(
            frame, values=sig_names or [""],
            state="readonly" if sig_names else "normal",
        )
        self.email_signature_combo.grid(
            row=3, column=1, sticky="ew", padx=8, pady=(4, 0),
        )

        # Inline images (comma-separated filenames).
        # Advanced-only: hidden in basic mode unless the scenario
        # already has one or more images configured. The 🖼 Add
        # image dialog in the HTML editor still works to add them.
        self._email_images_label = ctk.CTkLabel(frame, text="Inline images")
        self._email_images_label.grid(
            row=4, column=0, sticky="w", padx=8, pady=(4, 0),
        )
        self.email_images_entry = ctk.CTkEntry(
            frame, placeholder_text="e.g. signature.png, banner.png",
        )
        self.email_images_entry.grid(row=4, column=1, sticky="ew", padx=8, pady=(4, 0))

        # Sent-email font + size. "(Outlook default)" leaves no inline
        # CSS so Outlook applies whatever the user configured under
        # File > Options > Mail > Stationery and Fonts. Picking any
        # named font wraps the rendered HTML body in a styled div
        # before Send/Display.
        # Advanced-only: hidden in basic mode unless the scenario has
        # already pinned a non-default font.
        self._email_font_label = ctk.CTkLabel(frame, text="Email font")
        self._email_font_label.grid(
            row=5, column=0, sticky="w", padx=8, pady=(4, 0),
        )
        font_row = ctk.CTkFrame(frame, fg_color="transparent")
        font_row.grid(row=5, column=1, sticky="ew", padx=8, pady=(4, 0))
        self._email_font_row = font_row
        self.email_font_family_combo = ctk.CTkComboBox(
            font_row,
            values=[
                EMAIL_FONT_DEFAULT_LABEL,
                "Calibri", "Arial", "Segoe UI",
                "Times New Roman", "Georgia", "Verdana",
            ],
            width=200,
        )
        self.email_font_family_combo.set(EMAIL_FONT_DEFAULT_LABEL)
        self.email_font_family_combo.pack(side="left")
        ctk.CTkLabel(font_row, text="Size").pack(side="left", padx=(10, 4))
        self.email_font_size_combo = ctk.CTkComboBox(
            font_row, values=["", "9", "10", "11", "12", "14", "16"],
            width=70,
        )
        self.email_font_size_combo.set("")
        self.email_font_size_combo.pack(side="left")
        ctk.CTkLabel(
            font_row,
            text="(blank size + 'Outlook default' = use whatever "
                 "Outlook is set to)",
            font=ctk.CTkFont(size=10), text_color=("gray45", "gray65"),
        ).pack(side="left", padx=(10, 0))

        # CC Program Mentor
        self.email_cc_pm_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            frame, text="CC Program Mentor",
            variable=self.email_cc_pm_var,
        ).grid(row=6, column=0, columnspan=2, sticky="w", padx=8, pady=(6, 4))

        # Pick the template (and confirm the subject) at fire time. Lets
        # one scenario serve many templates — the Body template above is
        # used as the default pre-selection in the fire-time picker.
        self.email_pick_template_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            frame, text="Choose email template when fired",
            variable=self.email_pick_template_var,
        ).grid(row=7, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 8))

    @staticmethod
    def _available_template_files() -> list[str]:
        """List the .html templates in the user's templates dir, in
        sorted order. Empty list if folder is missing or unreadable."""
        try:
            return sorted(p.name for p in templates_dir().glob("*.html"))
        except Exception:
            return []

    def _on_send_email_toggled(self) -> None:
        """Show or hide the email section based on the checkbox."""
        if self.send_email_var.get():
            self._email_section.grid(
                row=self._email_section_row, column=0,
                sticky="ew", padx=8, pady=(0, 8),
            )
        else:
            self._email_section.grid_remove()

    # 12-hour labels for the text-schedule "send at" picker, mapped to/from
    # the 24-hour `target_hour` stored in TextConfig.
    _HOUR_LABELS = [
        "6 AM", "7 AM", "8 AM", "9 AM", "10 AM", "11 AM", "12 PM", "1 PM",
        "2 PM", "3 PM", "4 PM", "5 PM", "6 PM", "7 PM", "8 PM", "9 PM",
    ]

    @staticmethod
    def _hour_to_label(h) -> str:
        try:
            h = int(h) % 24
        except (TypeError, ValueError):
            h = 10
        ap = "AM" if h < 12 else "PM"
        h12 = h % 12 or 12
        return f"{h12} {ap}"

    @staticmethod
    def _label_to_hour(label: str) -> int:
        m = re.match(r"\s*(\d{1,2})\s*(AM|PM)", (label or "").strip(), re.I)
        if not m:
            return 10
        h = int(m.group(1)) % 12
        if m.group(2).upper() == "PM":
            h += 12
        return h

    def _build_text_section(self) -> None:
        """Construct the text (Mongoose) config widgets inside a sub-frame.
        Always created — visibility toggled by `_on_send_text_toggled`."""
        frame = ctk.CTkFrame(self.frame)
        frame.grid_columnconfigure(1, weight=1)
        self._text_section = frame
        # Round-tripped fields the UI doesn't expose (e.g. body_file).
        self._text_body_file = ""

        ctk.CTkLabel(frame, text="Message").grid(
            row=0, column=0, sticky="nw", padx=8, pady=(6, 0))
        self.text_body_box = ctk.CTkTextbox(frame, height=90, wrap="word")
        self.text_body_box.grid(row=0, column=1, sticky="ew", padx=8, pady=(6, 0))
        ctk.CTkLabel(
            frame,
            text="Plain text. Use {{first_name}}, {{preferred_name}}, "
                 "{{course_code}}… (max 306 chars after variables).",
            font=ctk.CTkFont(size=10), text_color=("gray45", "gray60"),
            wraplength=420, justify="left", anchor="w",
        ).grid(row=1, column=1, sticky="w", padx=8, pady=(0, 4))

        self.text_schedule_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            frame, text="Schedule at the student's local time",
            variable=self.text_schedule_var,
            command=self._on_text_schedule_toggled,
        ).grid(row=2, column=1, sticky="w", padx=8, pady=(2, 0))

        self._text_hour_row = ctk.CTkFrame(frame, fg_color="transparent")
        self._text_hour_row.grid(row=3, column=1, sticky="w", padx=8, pady=(2, 0))
        ctk.CTkLabel(self._text_hour_row, text="Send at (local):").pack(side="left")
        self.text_hour_combo = ctk.CTkComboBox(
            self._text_hour_row, values=self._HOUR_LABELS, width=90)
        self.text_hour_combo.pack(side="left", padx=(6, 0))
        self.text_hour_combo.set("10 AM")

        ctk.CTkLabel(frame, text="Inbox").grid(
            row=4, column=0, sticky="w", padx=8, pady=(4, 0))
        self.text_inbox_entry = ctk.CTkEntry(
            frame, placeholder_text="empty = “{course_code} Inbox”")
        self.text_inbox_entry.grid(row=4, column=1, sticky="ew", padx=8, pady=(4, 0))

        self.text_commit_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            frame,
            text="Send automatically (otherwise review/edit in the app first)",
            variable=self.text_commit_var,
        ).grid(row=5, column=1, sticky="w", padx=8, pady=(4, 8))

    def _on_text_schedule_toggled(self) -> None:
        """Show the 'send at' hour picker only when scheduling is on."""
        if self.text_schedule_var.get():
            self._text_hour_row.grid()
        else:
            self._text_hour_row.grid_remove()

    def _on_send_text_toggled(self) -> None:
        """Show or hide the text section based on the checkbox."""
        if self.send_text_var.get():
            self._text_section.grid(
                row=self._text_section_row, column=0,
                sticky="ew", padx=8, pady=(0, 8),
            )
            self._on_text_schedule_toggled()
        else:
            self._text_section.grid_remove()

    def _current_prompt_var_names(self) -> list[str]:
        """Return the live list of `var` names from the prompts
        section, so the in-app HTML editor can offer them as
        insert-buttons even before the scenario is saved."""
        out: list[str] = []
        for pr in self.prompt_rows:
            v = pr.var_entry.get().strip()
            if v and v not in out:
                out.append(v)
        return out

    def apply_advanced_visibility(self, advanced: bool) -> None:
        """Show / hide advanced-only rows based on the global mode.
        Rule: a row stays visible if EITHER the global mode is on,
        OR the scenario already has a non-default value in that row
        (so basic-mode users don't lose access to fields they've
        already configured)."""
        def _show_pair(label, widget, show: bool) -> None:
            try:
                if show:
                    label.grid()
                    widget.grid()
                else:
                    label.grid_remove()
                    widget.grid_remove()
            except Exception:
                pass

        # --- Scenario variables toggle (+ section)
        # Show the checkbox if advanced OR the scenario has any
        # variables defined. The section follows the checkbox's own
        # toggle state via _on_use_vars_toggled, so once we restore
        # the checkbox the section comes back naturally if it was on.
        has_vars = bool(self.prompt_rows)
        try:
            if advanced or has_vars or self.use_vars_var.get():
                self._use_vars_checkbox.grid(
                    row=self._use_vars_checkbox_row, column=0,
                    sticky="w", padx=8, pady=(8, 4),
                )
            else:
                self._use_vars_checkbox.grid_remove()
                # Also hide the section if it was showing.
                try: self._vars_section.grid_remove()
                except Exception: pass
        except Exception:
            pass

        # --- Email override row
        has_email_to = bool(self.email_to_entry.get().strip())
        _show_pair(self._email_to_label, self.email_to_entry,
                   advanced or has_email_to)

        # --- Inline images row
        has_images = bool(self.email_images_entry.get().strip())
        _show_pair(self._email_images_label, self.email_images_entry,
                   advanced or has_images)

        # --- Email font row
        # "Configured" = a real font picked (not the sentinel) OR a
        # non-empty size. Show the row in both cases regardless of
        # advanced.
        try:
            family = self.email_font_family_combo.get().strip()
        except Exception:
            family = ""
        try:
            size = self.email_font_size_combo.get().strip()
        except Exception:
            size = ""
        has_font_override = (
            (family and family != EMAIL_FONT_DEFAULT_LABEL) or size
        )
        _show_pair(self._email_font_label, self._email_font_row,
                   advanced or bool(has_font_override))

        # --- Push the same call into every note editor so the
        # append-clipboard checkbox follows the same rule.
        for ne in self.note_editors:
            try:
                ne.apply_advanced_visibility(advanced)
            except Exception:
                pass

    def _selected_template_path(self) -> Optional[Path]:
        """Return the absolute path of the currently-selected body
        template, or None if the dropdown is empty / pointing at the
        '(none …)' placeholder."""
        name = self.email_body_combo.get().strip()
        if not name or "(none" in name:
            return None
        return templates_dir() / name

    def _edit_template_in_app(self) -> None:
        """Open the in-app HTML editor for the selected template.
        Creates the file if it doesn't exist (so a freshly-typed
        name in the combo box works as 'new template')."""
        from tkinter import messagebox
        path = self._selected_template_path()
        if path is None:
            messagebox.showinfo(
                "Pick a template first",
                "Select a body template in the dropdown (or type a "
                "new filename) before clicking Edit.",
            )
            return
        if not path.exists():
            # First-time create — seed with a minimal stub so the
            # editor opens to something useful, then write it.
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    "<p>Hi {{first_name}},</p>\n\n<p></p>\n",
                    encoding="utf-8",
                )
            except Exception as e:
                messagebox.showerror("Couldn't create file", str(e))
                return
        prompt_html_template_editor(
            self.frame.winfo_toplevel(),
            path,
            custom_var_names=self._current_prompt_var_names(),
            on_image_added=self._add_inline_image,
        )

    def _new_template(self) -> None:
        """Create a fresh `.html` template under templates_dir(). Asks
        for a filename, seeds the file with a minimal stub, refreshes
        the body-template dropdown, selects the new file, and opens
        the in-app editor on it."""
        from tkinter import messagebox
        dialog = ctk.CTkInputDialog(
            text="Filename for new template (without .html):",
            title="New template",
        )
        raw = dialog.get_input()
        if not raw or not raw.strip():
            return
        name = raw.strip()
        if not name.lower().endswith(".html"):
            name += ".html"
        path = templates_dir() / name
        if path.exists():
            if not messagebox.askyesno(
                "File exists",
                f"{name} already exists. Open it for editing instead?",
            ):
                return
        else:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    "<p>Hi {{first_name}},</p>\n\n<p></p>\n",
                    encoding="utf-8",
                )
            except Exception as e:
                messagebox.showerror("Couldn't create file", str(e))
                return
        # Refresh dropdown values so the new file shows up, then
        # select it so subsequent Edit / Word click the right file.
        new_values = self._available_template_files()
        self.email_body_combo.configure(values=new_values)
        self.email_body_combo.set(name)
        prompt_html_template_editor(
            self.frame.winfo_toplevel(),
            path,
            custom_var_names=self._current_prompt_var_names(),
            on_image_added=self._add_inline_image,
        )

    def _add_inline_image(self, filename: str) -> None:
        """Add `filename` to the email's Inline Images field, unless
        already listed. Called by the HTML editor's image-insert
        dialog so the runtime knows to attach this file + bind its
        CID when the email composes."""
        current = self.email_images_entry.get().strip()
        existing = [s.strip() for s in current.split(",") if s.strip()]
        if filename in existing:
            return
        existing.append(filename)
        self.email_images_entry.delete(0, "end")
        self.email_images_entry.insert(0, ", ".join(existing))

    def _open_template_externally(self) -> None:
        """Hand the selected template to whatever app Windows has
        associated with .html — VS Code, Notepad++, Word, Notepad,
        whatever the user has set as default. Replaces the previous
        Word-via-COM flow, which was unreliable for some setups."""
        from tkinter import messagebox
        path = self._selected_template_path()
        if path is None:
            messagebox.showinfo(
                "Pick a template first",
                "Select a body template before clicking Open.",
            )
            return
        if not path.exists():
            messagebox.showinfo(
                "File doesn't exist yet",
                "Click Edit first to create the template, then try "
                "again.",
            )
            return
        ok, msg = _open_externally(path)
        if not ok:
            messagebox.showerror("Couldn't open file", msg)

    def _open_template_file(self) -> None:
        """Legacy: open the selected template in the OS-default editor.
        Kept for parity with older code paths but unused by the email
        section now (Edit + Word buttons replaced it)."""
        import os
        name = self.email_body_combo.get().strip()
        if not name or "(none" in name:
            return
        path = templates_dir() / name
        if not path.exists():
            return
        try:
            os.startfile(str(path))  # Windows-only; falls through on other OSes
        except Exception:
            pass

    def _add_note_editor(self, note_data: NoteData) -> NoteEditor:
        ne = NoteEditor(
            self.notes_container,
            index=len(self.note_editors),
            on_delete=self._delete_note,
            get_scenario_vars=self._current_prompt_var_names,
        )
        ne.frame.pack(fill="x", padx=4, pady=4)
        ne.load(note_data)
        self.note_editors.append(ne)
        return ne

    def _add_note(self) -> None:
        """Append a new blank note. Draft until 'Save changes' — same
        model as scenario add/delete."""
        default = NoteData(
            interaction_format="Single Interaction",
            interaction_type="",
            course_code="", subject="", body="",
            academic_activities=[],
            submit=True, append_clipboard=False,
        )
        self._add_note_editor(default)

    def _delete_note(self, ne: NoteEditor) -> None:
        # load_scenarios() rejects scenarios with zero notes, so
        # deleting the last would just cause Save to fail.
        if len(self.note_editors) <= 1:
            from tkinter import messagebox
            messagebox.showinfo(
                "Can't delete",
                "An action needs at least one note. Use 'Delete "
                "action' in the editor's action row if you want to "
                "remove the whole action.",
            )
            return
        self.note_editors.remove(ne)
        ne.frame.destroy()
        for i, e in enumerate(self.note_editors):
            e.set_index(i)

    def load(self, scenario: ScenarioConfig) -> None:
        self.name_entry.delete(0, "end")
        self.name_entry.insert(0, scenario.name)
        self.hotkey_entry.delete(0, "end")
        self.hotkey_entry.insert(0, scenario.hotkey)
        self.find_first_var.set(scenario.find_first)
        # Panel-action toggle (forced off below for batch scenarios).
        self.panel_action_var.set(scenario.panel_action)
        # Text (Mongoose) section — populate from scenario.text.
        t = scenario.text
        self.send_text_var.set(t is not None)
        self._text_body_file = (t.body_file if t else "") or ""
        self.text_body_box.delete("1.0", "end")
        if t is not None:
            self.text_body_box.insert("1.0", t.body or "")
            self.text_schedule_var.set(bool(t.schedule))
            self.text_hour_combo.set(self._hour_to_label(t.target_hour))
            self.text_inbox_entry.delete(0, "end")
            self.text_inbox_entry.insert(0, t.inbox_label or "")
            self.text_commit_var.set(bool(t.commit))
        else:
            self.text_schedule_var.set(True)
            self.text_hour_combo.set("10 AM")
            self.text_inbox_entry.delete(0, "end")
            self.text_commit_var.set(False)
        self._on_send_text_toggled()
        # Batch config — populate filter rows + visibility.
        self._batch_preview = scenario.batch.preview if scenario.batch else True
        # Rebuild prompt rows fresh from the scenario.
        for pr in list(self.prompt_rows):
            try: pr.frame.destroy()
            except Exception: pass
        self.prompt_rows = []
        for p in scenario.prompts:
            self._add_prompt_row(p)
        # Toggle reflects whether the scenario has any defined vars —
        # otherwise the section stays hidden as an "advanced" feature.
        self.use_vars_var.set(bool(scenario.prompts))
        self._on_use_vars_toggled()
        for r in list(self.filter_rows):
            try: r.frame.destroy()
            except Exception: pass
        self.filter_rows = []
        if scenario.batch is not None:
            self.batch_mode_var.set(True)
            for filt in scenario.batch.filters:
                self._add_filter_row(filt)
        else:
            self.batch_mode_var.set(False)
        self._on_batch_mode_toggled()
        # Email config drives the section's visibility + widget values.
        self.send_email_var.set(scenario.email is not None)
        if scenario.email is not None:
            self.email_subject_entry.delete(0, "end")
            self.email_subject_entry.insert(0, scenario.email.subject)
            if scenario.email.body_html_file:
                self.email_body_combo.set(scenario.email.body_html_file)
            self.email_to_entry.delete(0, "end")
            self.email_to_entry.insert(0, scenario.email.to)
            self.email_signature_combo.set(scenario.email.signature_file)
            self.email_images_entry.delete(0, "end")
            self.email_images_entry.insert(0, ", ".join(scenario.email.inline_images))
            self.email_cc_pm_var.set(scenario.email.cc_pm)
            self.email_pick_template_var.set(scenario.email.pick_template)
            ff = (scenario.email.font_family or "").strip()
            self.email_font_family_combo.set(ff if ff else EMAIL_FONT_DEFAULT_LABEL)
            fs = scenario.email.font_size or 0
            self.email_font_size_combo.set(str(fs) if fs > 0 else "")
        else:
            # Clear out so an old scenario's leftover values don't show.
            self.email_subject_entry.delete(0, "end")
            self.email_to_entry.delete(0, "end")
            self.email_signature_combo.set("")
            self.email_images_entry.delete(0, "end")
            self.email_cc_pm_var.set(False)
            self.email_pick_template_var.set(False)
            self.email_font_family_combo.set(EMAIL_FONT_DEFAULT_LABEL)
            self.email_font_size_combo.set("")
        self._on_send_email_toggled()
        for ne, note in zip(self.note_editors, scenario.notes):
            ne.load(note)

    def serialize(self) -> dict:
        out: dict = {
            "hotkey": self.hotkey_entry.get().strip(),
            "close_tab_after": self.close_tab_after,
            "find_first": self.find_first_var.get(),
            "notes": [ne.serialize() for ne in self.note_editors],
        }
        # Panel action only makes sense for non-batch scenarios; the
        # toggle is force-disabled in batch mode, so this is already False
        # there. Only written when True to keep the YAML uncluttered.
        if self.panel_action_var.get() and not self.batch_mode_var.get():
            out["panel_action"] = True
        if self.send_email_var.get():
            tpl = self.email_body_combo.get().strip()
            if "(none" in tpl:  # placeholder for empty templates folder
                tpl = ""
            inline_csv = self.email_images_entry.get().strip()
            inline_images = [
                s.strip() for s in inline_csv.split(",") if s.strip()
            ]
            out["email"] = {
                "subject": self.email_subject_entry.get(),
                "body_html_file": tpl,
                "to": self.email_to_entry.get(),
                "signature_file": self.email_signature_combo.get(),
                "inline_images": inline_images,
                "cc_pm": self.email_cc_pm_var.get(),
            }
            if self.email_pick_template_var.get():
                out["email"]["pick_template"] = True
            # Only write font fields when the user has set them away
            # from the "Outlook default" sentinel — keeps the YAML
            # uncluttered for scenarios that don't override the font.
            ff = self.email_font_family_combo.get().strip()
            if ff and ff != EMAIL_FONT_DEFAULT_LABEL:
                out["email"]["font_family"] = ff
            try:
                fs = int(self.email_font_size_combo.get().strip() or 0)
            except ValueError:
                fs = 0
            if fs > 0:
                out["email"]["font_size"] = fs
        if self.batch_mode_var.get():
            out["batch"] = {
                "filters": [r.serialize() for r in self.filter_rows],
                "preview": self._batch_preview,
            }
        if self.use_vars_var.get():
            prompts_out = [r.serialize() for r in self.prompt_rows]
            # Drop rows with empty `var` — they're not addressable from
            # YAML/templates and would just be noise in the saved file.
            prompts_out = [p for p in prompts_out if p.get("var")]
            if prompts_out:
                out["prompts"] = prompts_out
        # Text (Mongoose) section.
        if self.send_text_var.get():
            out["text"] = {
                "body": self.text_body_box.get("1.0", "end-1c"),
                "body_file": getattr(self, "_text_body_file", "") or "",
                "schedule": bool(self.text_schedule_var.get()),
                "target_hour": self._label_to_hour(self.text_hour_combo.get()),
                "inbox_label": self.text_inbox_entry.get().strip(),
                "commit": bool(self.text_commit_var.get()),
            }
        return out


# ============================================================
# Caseload panel — in-app sortable/searchable view of the cached
# caseload CSV. Container-agnostic: builds into any parent frame
# (the docked right pane OR a popped-out window), and is fully
# data-driven from app._caseload_rows so a rebuild on dock/undock
# is cheap.
# ============================================================

class CaseloadPanel:
    def __init__(self, parent, app: "App", popped: bool = False) -> None:
        self.app = app
        self.popped = popped  # rendered in a pop-out window vs the dock
        self._sort_col: Optional[str] = None
        self._sort_reverse = False
        self._query = ""
        # Multi-select: checked row keys (Student ID, falling back to
        # Name). Kept on the panel so the selection survives sort/search/
        # filter re-renders AND pop-out/re-dock (the dock rebuilds the
        # panel, but app holds the set — see _mount_caseload_panel).
        self._checked_ids: set[str] = app._caseload_checked_ids
        # Active column filters (serialized FilterRow dicts) applied on top
        # of the search box. Empty = show everything. Ephemeral — reset on
        # rebuild (pop-out / re-dock).
        self._active_filters: list[dict] = []
        self._filters_open = False
        # Column drag-reorder state (header click-drag).
        self._coldrag: Optional[dict] = None
        self._suppress_next_sort = False
        self.frame = ctk.CTkFrame(parent, fg_color="transparent")
        self.frame.grid_columnconfigure(0, weight=1)
        # Rows: 0 bar · 1 filters (collapsible) · 2 table (stretch) ·
        # 3 selection action bar.
        self.frame.grid_rowconfigure(2, weight=1)
        self._build()
        self.populate()
        # Give the table the larger share of the vertical split once the
        # panel has a real height.
        self.frame.after(150, self._init_sash)

    # ----- construction -----

    def _build(self) -> None:
        bar = ctk.CTkFrame(self.frame, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", padx=4, pady=(4, 2))
        bar.grid_columnconfigure(2, weight=1)
        # Reload sits to the LEFT of the "Caseload" label.
        self.refresh_btn = ctk.CTkButton(
            bar, text="↻", width=36, command=self._on_refresh,
            **SECONDARY_BTN_KWARGS,
        )
        self.refresh_btn.grid(row=0, column=0, padx=(0, 4))
        self.caseload_label = ctk.CTkLabel(
            bar, text="Caseload",
            font=ctk.CTkFont(size=13, weight="bold"),
        )
        self.caseload_label.grid(row=0, column=1, padx=(4, 8))
        self.search_var = ctk.StringVar()
        self.search_entry = ctk.CTkEntry(
            bar, textvariable=self.search_var,
            placeholder_text="Search caseload…",
        )
        self.search_entry.grid(row=0, column=2, sticky="ew", padx=4)
        self.search_var.trace_add("write", lambda *_: self._on_search())
        # Down from the search box drops focus into the rows.
        self.search_entry.bind("<Down>", self._focus_first_row)
        # (The old blue latest-task status dropdown lived here — removed.
        # Task pass/fail is now a normal "Task Status" filter inside the
        # Filters section below, so it works for the viewer AND batch
        # actions via the shared filter engine.)
        # Filters toggle — shows/hides the collapsible column-filter
        # section (same builder the batch scenarios use).
        self.filters_toggle_btn = ctk.CTkButton(
            bar, text="▸ Filters", width=80, command=self._toggle_filters,
            **SECONDARY_BTN_KWARGS,
        )
        self.filters_toggle_btn.grid(row=0, column=4, padx=(0, 4))
        # Column chooser (show/hide + reorder + persisted widths).
        self.columns_btn = ctk.CTkButton(
            bar, text="☰ Columns", width=90, command=self._open_columns_dialog,
            **SECONDARY_BTN_KWARGS,
        )
        self.columns_btn.grid(row=0, column=5, padx=(0, 4))
        self.freshness_lbl = ctk.CTkLabel(
            bar, text="", font=ctk.CTkFont(size=11),
            text_color=("gray40", "gray65"),
        )
        self.freshness_lbl.grid(row=0, column=6, padx=8)
        # Pop the panel into its own window (2nd monitor) / re-dock it.
        self.popout_btn = ctk.CTkButton(
            bar, text=("⧉ Dock" if self.popped else "⧉ Pop out"),
            width=80, command=self.app._toggle_caseload_popout,
            **SECONDARY_BTN_KWARGS,
        )
        self.popout_btn.grid(row=0, column=7, padx=(0, 4))
        # Local history: review students who left the caseload + export the
        # snapshot DB for pandas/Excel.
        self.departures_btn = ctk.CTkButton(
            bar, text="⚑ Departures", width=100,
            command=self._open_departures_dialog, **SECONDARY_BTN_KWARGS,
        )
        self.departures_btn.grid(row=0, column=8, padx=(0, 4))
        self.export_history_btn = ctk.CTkButton(
            bar, text="⤓ Export history", width=110,
            command=self._export_history, **SECONDARY_BTN_KWARGS,
        )
        self.export_history_btn.grid(row=0, column=9, padx=(0, 4))

        # Collapsible filters section (row 1) — hidden until toggled.
        self.filters_wrap = ctk.CTkFrame(self.frame)
        self.filters_wrap.grid(row=1, column=0, sticky="ew", padx=4, pady=(0, 4))
        self.filters_wrap.grid_columnconfigure(0, weight=1)
        self.filters_container = ctk.CTkFrame(
            self.filters_wrap, fg_color="transparent")
        self.filters_container.grid(row=0, column=0, sticky="ew", padx=2, pady=2)
        self.filters_container.grid_columnconfigure(0, weight=1)
        self.filter_rows: list[FilterRow] = []
        filt_actions = ctk.CTkFrame(self.filters_wrap, fg_color="transparent")
        filt_actions.grid(row=1, column=0, sticky="w", padx=2, pady=(0, 4))
        ctk.CTkButton(
            filt_actions, text="+ Add", width=70,
            command=self._add_filter_row, **SECONDARY_BTN_KWARGS,
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            filt_actions, text="Apply", width=70, command=self._apply_filters,
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            filt_actions, text="Clear", width=70, command=self._clear_filters,
            **SECONDARY_BTN_KWARGS,
        ).pack(side="left")
        # Quick toggle: only students with an open Essential Action.
        self._ea_only_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            filt_actions, text="Only open EAs", variable=self._ea_only_var,
            font=ctk.CTkFont(size=11), command=self.populate,
        ).pack(side="left", padx=(14, 0))
        self.filters_wrap.grid_remove()  # collapsed by default

        # Vertical split: the table on top, the student detail pane
        # (quick view + note viewer) below, with a draggable sash. Lives
        # inside self.frame so it rebuilds with the panel on pop-out/dock.
        self.vpane = tk.PanedWindow(
            self.frame, orient="vertical", bd=0,
            sashwidth=5, sashrelief="flat",
            bg=("#3a3a3a" if ctk.get_appearance_mode() == "Dark"
                else "#c8c8c8"),
        )
        self.vpane.grid(row=2, column=0, sticky="nsew", padx=4, pady=(0, 4))
        table_wrap = ctk.CTkFrame(self.vpane)
        table_wrap.grid_columnconfigure(0, weight=1)
        table_wrap.grid_rowconfigure(0, weight=1)
        self._style_tree()
        self._make_check_images()
        # "tree headings" keeps the implicit #0 column visible — we use it
        # for the per-row checkbox image (CTk-style blue filled box).
        self.tree = ttk.Treeview(
            table_wrap, show="tree headings", selectmode="browse",
            style="Caseload.Treeview",
        )
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb = ttk.Scrollbar(table_wrap, orient="vertical",
                            command=self.tree.yview)
        vsb.grid(row=0, column=1, sticky="ns")
        hsb = ttk.Scrollbar(table_wrap, orient="horizontal",
                            command=self.tree.xview)
        hsb.grid(row=1, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        # Zebra striping for readability.
        self.tree.tag_configure("even", background=self._even_bg)
        self.tree.tag_configure("odd", background=self._odd_bg)
        # Double-click → open/switch to that student (reuse current tab).
        # Middle-click → quick "open in new console subtab" (background).
        # Right-click → action menu (Open / Open in new tab / Fire ▸).
        self.tree.bind("<Double-1>", self._on_row_open)
        self.tree.bind("<Button-2>", self._on_row_open_new_tab)
        self.tree.bind("<Button-3>", self._on_row_context_menu)
        # Keyboard equivalent of right-click: Shift+F10 (Windows standard)
        # and the dedicated Menu/Application key. Up/Down row navigation
        # is native to ttk.Treeview once it has focus.
        self.tree.bind("<Shift-F10>", self._on_row_menu_key)
        for seq in ("<App>", "<Menu>", "<Key-Menu>"):
            try:
                self.tree.bind(seq, self._on_row_menu_key)
            except Exception:
                pass
        # Enter on a focused row opens it (switch tab), matching dbl-click.
        self.tree.bind("<Return>", self._on_row_open_key)
        # Left-click in the leading checkbox column toggles that row's
        # selection (handled before the default browse-select so the
        # highlight doesn't jump). Space toggles the focused row.
        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<space>", self._toggle_focused_row)
        self.tree.bind("<Key-space>", self._toggle_focused_row)
        # Ctrl +/- and Ctrl+wheel resize the caseload table text.
        bind_font_hotkeys("viewer", self.tree)
        # Drag a column heading sideways to reorder columns.
        self.tree.bind("<B1-Motion>", self._on_head_drag, add="+")
        self.tree.bind("<ButtonRelease-1>", self._on_head_release, add="+")
        self._row_by_iid: dict[str, dict] = {}
        self._table_wrap = table_wrap
        self.empty_lbl = ctk.CTkLabel(
            table_wrap,
            text="No caseload loaded.\nClick ↻ to download from Salesforce.",
            font=ctk.CTkFont(size=12), justify="center",
            text_color=("gray45", "gray60"),
        )
        # Highlighting a row (mouse or keyboard) fills the quick view.
        self.tree.bind("<<TreeviewSelect>>", self._on_row_highlight, add="+")
        self.vpane.add(table_wrap, minsize=120, stretch="always")
        self._build_detail_pane()
        self.vpane.add(self.detail_frame, minsize=120, stretch="always")

        # Selection action bar (row 2) — hidden until ≥1 row is checked.
        # "N selected" + Clear + Fire-scenario-on-the-selection menu.
        self.action_bar = ctk.CTkFrame(self.frame, fg_color="transparent")
        self.action_bar.grid(row=3, column=0, sticky="ew", padx=4, pady=(0, 4))
        self.action_bar.grid_columnconfigure(0, weight=1)
        self.sel_count_lbl = ctk.CTkLabel(
            self.action_bar, text="", font=ctk.CTkFont(size=12, weight="bold"),
            anchor="w",
        )
        self.sel_count_lbl.grid(row=0, column=0, sticky="w", padx=(4, 8))
        self.clear_sel_btn = ctk.CTkButton(
            self.action_bar, text="Clear", width=60,
            command=self._clear_selection, **SECONDARY_BTN_KWARGS,
        )
        self.clear_sel_btn.grid(row=0, column=1, padx=(0, 4))
        self.fire_sel_btn = ctk.CTkButton(
            self.action_bar, text="Fire action ▸", width=120,
            command=self._on_fire_selected_clicked,
        )
        self.fire_sel_btn.grid(row=0, column=2, padx=(0, 4))
        self.action_bar.grid_remove()  # shown on first selection

        # Responsive bar: collapse labels to symbols when the panel is
        # narrow (docked in a thin pane). Recomputed on resize.
        self._narrow = False
        self.frame.bind("<Configure>", self._on_panel_resize)

    def _bar_button_text(self) -> dict:
        """Button/label text for the current narrow/wide + toggle state."""
        n = self._narrow
        filt_arrow = "▾" if self._filters_open else "▸"
        popped = getattr(self, "popped", False)
        return {
            "filters": filt_arrow if n else f"{filt_arrow} Filters",
            "columns": "☰" if n else "☰ Columns",
            "popout": ("⧉" if popped else "⧉") if n
                      else ("⧉ Dock" if popped else "⧉ Pop out"),
        }

    def _apply_bar_mode(self) -> None:
        t = self._bar_button_text()
        try:
            self.filters_toggle_btn.configure(
                text=t["filters"], width=36 if self._narrow else 80)
            self.columns_btn.configure(
                text=t["columns"], width=36 if self._narrow else 90)
            self.popout_btn.configure(
                text=t["popout"], width=36 if self._narrow else 80)
            if self._narrow:
                self.caseload_label.grid_remove()
            else:
                self.caseload_label.grid()
        except Exception:
            pass

    def _on_panel_resize(self, event=None) -> None:
        try:
            w = self.frame.winfo_width()
        except Exception:
            return
        narrow = w < 460
        if narrow != self._narrow:
            self._narrow = narrow
            self._apply_bar_mode()

    def _init_sash(self) -> None:
        """Place the table/detail sash so the list keeps ~62% of the height."""
        try:
            h = self.vpane.winfo_height()
            if h > 120:
                self.vpane.sash_place(0, 1, int(h * 0.62))
        except Exception:
            pass

    # ----- student detail pane (quick view + note viewer) -----

    # (The old quick latest-task status dropdown + _status_match lived here.
    # Removed: task pass/fail now filters through the shared filter engine
    # via the synthetic "Task Status" column — see App._apply_task_status_
    # to_rows — so it covers the viewer AND batch actions.)

    # Catalog of selectable quick-view fields: key → (label, csv_key,
    # kind). `kind` drives rendering; csv_key is None for derived/special
    # fields. The user picks/orders a subset via the Fields chooser.
    QUICK_VIEW_CATALOG = [
        ("mentor",      "Mentor",        "MentorName",         "text"),
        ("term_end",    "Term end",      "TermEndDate",        "date_countdown"),
        ("ic_end",      "IC end date",   "Icenddate",          "date_countdown"),
        ("timezone",    "Timezone",      "Timezone",           "timezone"),
        ("email",       "Email",         "StudentEmail",       "email"),
        ("phone",       "Phone",         None,                 "phone"),
        ("course",      "Course code",   "CourseCode",         "text"),
        ("student_id",  "Student ID",    "StudentID",          "text"),
        ("last_action", "Last action",   None,                 "last_action"),
        ("followup",    "Followup note", "CourseFollowupNote", "longtext"),
        ("tasks",       "Task badges",   None,                 "tasks"),
    ]
    QUICK_VIEW_DEFAULT = [
        "mentor", "term_end", "timezone", "email", "phone",
        "last_action", "followup", "tasks",
    ]

    def _quickview_field_keys(self) -> list:
        """Configured quick-view field keys in order (Settings →
        quickview_fields), falling back to the default set. Unknown keys
        are dropped so a stale config can't break rendering."""
        valid = {k for k, *_ in self.QUICK_VIEW_CATALOG}
        raw = (self.app.settings.quickview_fields or "").strip()
        if raw:
            try:
                import json
                keys = [k for k in json.loads(raw) if k in valid]
                if keys:
                    return keys
            except Exception:
                pass
        return list(self.QUICK_VIEW_DEFAULT)

    def _open_quickview_dialog(self) -> None:
        """Dialog to choose + reorder which quick-view fields show."""
        import json
        labels = {k: label for k, label, *_ in self.QUICK_VIEW_CATALOG}
        selected = self._quickview_field_keys()
        rest = [k for k, *_ in self.QUICK_VIEW_CATALOG if k not in selected]
        # Working model: [key, visible], selected-in-order first.
        work = [[k, True] for k in selected] + [[k, False] for k in rest]

        dlg = ctk.CTkToplevel(self.frame)
        dlg.title("Quick-view fields")
        dlg.geometry("360x440")
        try:
            dlg.transient(self.frame.winfo_toplevel())
        except Exception:
            pass
        dlg.attributes("-topmost", True)
        try:
            dlg.grab_set()
        except Exception:
            pass
        dlg.grid_columnconfigure(0, weight=1)
        dlg.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(
            dlg, text="Show, hide and reorder quick-view fields",
            font=ctk.CTkFont(size=12, weight="bold"),
        ).grid(row=0, column=0, sticky="w", padx=12, pady=(10, 4))
        scroll = ctk.CTkScrollableFrame(dlg)
        scroll.grid(row=1, column=0, sticky="nsew", padx=8, pady=4)
        scroll.grid_columnconfigure(2, weight=1)

        def move(idx, delta):
            j = idx + delta
            if 0 <= j < len(work):
                work[idx], work[j] = work[j], work[idx]
                redraw()

        def redraw():
            for w in scroll.winfo_children():
                w.destroy()
            for i, pair in enumerate(work):
                var = ctk.BooleanVar(value=pair[1])

                def on_toggle(p=pair, v=var):
                    p[1] = v.get()

                ctk.CTkButton(
                    scroll, text="▲", width=26,
                    command=lambda ix=i: move(ix, -1), **SECONDARY_BTN_KWARGS,
                ).grid(row=i, column=0, padx=(2, 0), pady=1)
                ctk.CTkButton(
                    scroll, text="▼", width=26,
                    command=lambda ix=i: move(ix, 1), **SECONDARY_BTN_KWARGS,
                ).grid(row=i, column=1, padx=(2, 4), pady=1)
                ctk.CTkCheckBox(
                    scroll, text=labels.get(pair[0], pair[0]),
                    variable=var, command=on_toggle,
                ).grid(row=i, column=2, sticky="w", padx=4, pady=1)

        redraw()

        btns = ctk.CTkFrame(dlg, fg_color="transparent")
        btns.grid(row=2, column=0, sticky="ew", padx=8, pady=(4, 10))

        def do_apply():
            chosen = [p[0] for p in work if p[1]]
            self.app.settings.quickview_fields = json.dumps(chosen)
            save_settings(self.app.settings)
            row = self._focused_row()
            if row is not None:
                self.show_quick_view(row)
            dlg.destroy()

        def reset_default():
            self.app.settings.quickview_fields = ""
            save_settings(self.app.settings)
            row = self._focused_row()
            if row is not None:
                self.show_quick_view(row)
            dlg.destroy()

        ctk.CTkButton(
            btns, text="Default", width=80, command=reset_default,
            **SECONDARY_BTN_KWARGS,
        ).pack(side="left")
        ctk.CTkButton(
            btns, text="Cancel", width=80, command=dlg.destroy,
            **SECONDARY_BTN_KWARGS,
        ).pack(side="right", padx=(6, 0))
        ctk.CTkButton(btns, text="Apply", width=80, command=do_apply).pack(
            side="right")

    def _build_detail_pane(self) -> None:
        """Bottom pane of the panel: an instant quick-view box for the
        highlighted student, then (on Enter) the on-demand note viewer.
        Quick view + notes live in ONE scrollable body so neither can be
        squeezed to zero height when the pane is short."""
        self.detail_frame = ctk.CTkFrame(self.vpane)
        self.detail_frame.grid_columnconfigure(0, weight=1)
        self.detail_frame.grid_rowconfigure(1, weight=1)
        self._detail_collapsed = False

        # Header (always visible): collapse · name · Review notes · Fields.
        hdr = ctk.CTkFrame(self.detail_frame, fg_color="transparent")
        hdr.grid(row=0, column=0, sticky="ew", padx=6, pady=(6, 2))
        hdr.grid_columnconfigure(1, weight=1)
        self.detail_collapse_btn = ctk.CTkButton(
            hdr, text="▾", width=24, command=self._toggle_detail_collapsed,
            **SECONDARY_BTN_KWARGS,
        )
        self.detail_collapse_btn.grid(row=0, column=0, padx=(0, 6))
        self.qv_name = ctk.CTkLabel(
            hdr, text="Select a student", anchor="w",
            font=ctk.CTkFont(size=14, weight="bold"),
        )
        self.qv_name.grid(row=0, column=1, sticky="ew")
        self.review_btn = ctk.CTkButton(
            hdr, text="Review notes ⏎", width=110,
            command=self._review_focused, **SECONDARY_BTN_KWARGS,
        )
        self.review_btn.grid(row=0, column=2, padx=(6, 0))
        self.qv_fields_btn = ctk.CTkButton(
            hdr, text="⚙", width=30,
            command=self._open_quickview_dialog, **SECONDARY_BTN_KWARGS,
        )
        self.qv_fields_btn.grid(row=0, column=3, padx=(6, 0))

        # One scrollable body holding the quick view then the notes.
        self.detail_scroll = ctk.CTkScrollableFrame(
            self.detail_frame, fg_color=("gray94", "gray16"),
        )
        self.detail_scroll.grid(row=1, column=0, sticky="nsew",
                                padx=4, pady=(0, 6))
        # Two columns: basic info (left) + Essential Actions (right). Notes
        # span both, below.
        self.detail_scroll.grid_columnconfigure(0, weight=3)
        self.detail_scroll.grid_columnconfigure(1, weight=2)
        self.qv_body = ctk.CTkFrame(self.detail_scroll, fg_color="transparent")
        self.qv_body.grid(row=0, column=0, sticky="new", padx=4, pady=(2, 4))
        self.qv_body.grid_columnconfigure(1, weight=1)
        # Essential Actions panel — to the right of the basic info.
        self.qv_ea_frame = ctk.CTkFrame(
            self.detail_scroll, fg_color=("gray90", "gray22"), corner_radius=6)
        self.qv_ea_frame.grid(row=0, column=1, sticky="new", padx=(4, 4),
                              pady=(2, 4))
        self.qv_ea_frame.grid_columnconfigure(0, weight=1)
        self.notes_title = ctk.CTkLabel(
            self.detail_scroll, text="Notes", anchor="w",
            font=ctk.CTkFont(size=12, weight="bold"),
        )
        self.notes_title.grid(row=1, column=0, columnspan=2, sticky="w",
                              padx=6, pady=(2, 0))
        self.notes_holder = ctk.CTkFrame(
            self.detail_scroll, fg_color="transparent")
        self.notes_holder.grid(row=2, column=0, columnspan=2, sticky="ew",
                               padx=2, pady=2)
        self.notes_holder.grid_columnconfigure(0, weight=1)
        # Ctrl +/- and Ctrl+wheel resize the note text while reading.
        bind_font_hotkeys("notes", self.detail_scroll)
        self._last_notes = None
        self._notes_hint()

    def _toggle_detail_collapsed(self) -> None:
        self._detail_collapsed = not self._detail_collapsed
        try:
            if self._detail_collapsed:
                # Remember the sash so expanding restores the same split.
                try:
                    self._saved_sash = self.vpane.sash_coord(0)[1]
                except Exception:
                    self._saved_sash = None
                self.detail_scroll.grid_remove()
                # Collapse the pane down to just its header bar.
                self.vpane.sash_place(
                    0, 1, max(0, self.vpane.winfo_height() - 44))
                self.detail_collapse_btn.configure(text="▸")
            else:
                self.detail_scroll.grid()
                h = self.vpane.winfo_height()
                pos = getattr(self, "_saved_sash", None)
                if not pos or pos < 80 or pos > h - 60:
                    pos = int(h * 0.62)
                self.vpane.sash_place(0, 1, pos)
                self.detail_collapse_btn.configure(text="▾")
        except Exception:
            pass

    def _ensure_detail_height(self) -> None:
        """Grow the detail pane (move the sash up) so the whole quick view
        for the selected student is visible, while keeping the list usable.
        Grow-only — never fights a manual enlargement or a collapse."""
        if getattr(self, "_detail_collapsed", False):
            return
        try:
            self.detail_frame.update_idletasks()
            # header + quick view + a minimum notes area.
            need = self.qv_body.winfo_reqheight() + 44 + 110
            h = self.vpane.winfo_height()
            if h <= 1:
                return
            max_detail = max(140, h - 150)   # always leave room for the list
            detail = min(need, max_detail)
            cur_detail = h - self.vpane.sash_coord(0)[1]
            if cur_detail < detail - 4:
                self.vpane.sash_place(0, 1, max(0, h - detail))
        except Exception:
            pass

    def _focused_row(self) -> Optional[dict]:
        iid = self.tree.focus()
        if not iid:
            sel = self.tree.selection()
            iid = sel[0] if sel else ""
        return self._row_by_iid.get(iid) if iid else None

    def _on_row_highlight(self, event=None) -> None:
        row = self._focused_row()
        if row is not None:
            self.show_quick_view(row)

    def _open_departures_dialog(self) -> None:
        """Show students who left the caseload since the last history capture
        (passed = informational; anything else = needs follow-up). Reads the
        local history DB fresh so it's always current."""
        try:
            deps = history.find_departures()
        except Exception as e:
            from tkinter import messagebox
            messagebox.showwarning("Departures", f"Couldn't read history: {e}")
            return

        dlg = ctk.CTkToplevel(self.frame)
        dlg.title("Departed students")
        dlg.geometry("580x460")
        try:
            dlg.transient(self.frame.winfo_toplevel())
        except Exception:
            pass
        dlg.attributes("-topmost", True)
        try:
            dlg.grab_set()
        except Exception:
            pass
        dlg.grid_columnconfigure(0, weight=1)
        dlg.grid_rowconfigure(1, weight=1)

        fu = sum(1 for d in deps if d["classification"] == "followup")
        ctk.CTkLabel(
            dlg,
            text=(f"{len(deps)} departed since last capture — "
                  f"{fu} need follow-up"),
            font=ctk.CTkFont(size=12, weight="bold"),
        ).grid(row=0, column=0, sticky="w", padx=12, pady=(10, 4))

        scroll = ctk.CTkScrollableFrame(dlg)
        scroll.grid(row=1, column=0, sticky="nsew", padx=8, pady=4)
        scroll.grid_columnconfigure(1, weight=1)

        if not deps:
            ctk.CTkLabel(
                scroll, text="No departures since the last capture.",
                text_color=("gray40", "gray65"),
            ).grid(row=0, column=0, columnspan=2, padx=8, pady=20)
        else:
            # Follow-ups first, then completed; each block alphabetical-ish by
            # the order find_departures returned (prior-collection order).
            ordered = ([d for d in deps if d["classification"] == "followup"]
                       + [d for d in deps if d["classification"] != "followup"])
            for i, d in enumerate(ordered):
                followup = d["classification"] == "followup"
                tag = ctk.CTkLabel(
                    scroll,
                    text=("FOLLOW UP" if followup else "completed"),
                    width=84, corner_radius=6,
                    fg_color=("#b3261e" if followup else "#3a3a3a"),
                    text_color="white",
                    font=ctk.CTkFont(size=10, weight="bold"),
                )
                tag.grid(row=i, column=0, padx=(4, 8), pady=2, sticky="w")
                last = d.get("last_task_status") or "—"
                seen = d.get("last_seen_date") or "?"
                detail = (f"{d.get('name') or d['student_id']}  "
                          f"[{d['student_id']}]  ·  {d['course_code']}  ·  "
                          f"last: {last}  ·  seen {seen}")
                ctk.CTkLabel(
                    scroll, text=detail, anchor="w", justify="left",
                ).grid(row=i, column=1, padx=4, pady=2, sticky="w")

        btns = ctk.CTkFrame(dlg, fg_color="transparent")
        btns.grid(row=2, column=0, sticky="ew", padx=8, pady=(4, 10))
        ctk.CTkButton(
            btns, text="⤓ Export history", width=120,
            command=self._export_history, **SECONDARY_BTN_KWARGS,
        ).pack(side="left")
        ctk.CTkButton(
            btns, text="Close", width=80, command=dlg.destroy,
        ).pack(side="right")

    def _export_history(self) -> None:
        """Dump the snapshot history to a CSV the user picks (for pandas/Excel)."""
        from tkinter import filedialog, messagebox
        path = filedialog.asksaveasfilename(
            title="Export caseload history",
            defaultextension=".csv",
            initialfile="caseload_history.csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            n = history.export_to_csv(path)
        except Exception as e:
            messagebox.showwarning("Export history", f"Export failed: {e}")
            return
        self.app._append_log(f"Exported {n} history rows → {path}")

    def _cell(self, row: dict, key: str) -> str:
        return str(row.get(key, "") or "").strip()

    def show_quick_view(self, row: dict) -> None:
        """Fill the quick-view box from a cached CSV row — instant, no
        Salesforce. Rebuilds the body each highlight (simple + correct)."""
        for w in self.qv_body.winfo_children():
            w.destroy()
        name = self._cell(row, "Name")
        pref = self._cell(row, "stuprename")
        title = name or pref or "(unknown)"
        if pref and pref.lower() not in name.lower():
            title = f"{title}  ·  {pref}"
        self.qv_name.configure(text=title)

        catalog = {k: (label, csv, kind)
                   for k, label, csv, kind in self.QUICK_VIEW_CATALOG}
        r = 0
        for key in self._quickview_field_keys():
            ent = catalog.get(key)
            if not ent:
                continue
            label, csv_key, kind = ent
            if kind == "tasks":
                self._qv_task_badges(r, row)
                r += 1
            else:
                r = self._qv_field(r, row, label, csv_key, kind)
        # Latest course note (straight from the CSV) — a fast preview that
        # sits just above the on-demand detailed notes. Most of the time
        # this is all the context needed; load full notes only if more is
        # wanted.
        r = self._qv_latest_note(r, row)
        # Editable follow-up date (writes back to Salesforce).
        r = self._qv_followup_editor(r, row)
        # Essential Actions for this student (right-hand panel).
        self._qv_render_ea(row)
        # New student → reset the notes area to the hint.
        self._notes_hint()
        # Make sure the whole quick view is visible by default.
        self._ensure_detail_height()

    def _qv_render_ea(self, row) -> None:
        """Fill the right-hand Essential Actions panel for this student
        (from the dashboard scrape, keyed by Student ID)."""
        for w in self.qv_ea_frame.winfo_children():
            w.destroy()
        sid = self._cell(row, "StudentID")
        ea = (getattr(self.app, "_ea_by_sid", {}) or {}).get(sid)
        ctk.CTkLabel(
            self.qv_ea_frame, text="Essential Action", anchor="w",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=("gray35", "gray70"),
        ).grid(row=0, column=0, sticky="ew", padx=8, pady=(6, 0))
        if not ea:
            ctk.CTkLabel(
                self.qv_ea_frame, text="none open", anchor="w",
                font=ctk.CTkFont(size=12), text_color=("gray45", "gray60"),
            ).grid(row=1, column=0, sticky="ew", padx=8, pady=(0, 6))
            return
        rr = 1
        reason = ea.get("reason", "")
        if reason:
            ctk.CTkLabel(
                self.qv_ea_frame, text=reason, anchor="w", justify="left",
                wraplength=240, font=ctk.CTkFont(size=12, weight="bold"),
            ).grid(row=rr, column=0, sticky="ew", padx=8, pady=(0, 2))
            rr += 1
        meta = []
        if ea.get("event_progress"):
            meta.append(ea["event_progress"])
        if ea.get("followup_date"):
            meta.append(f"follow-up {ea['followup_date']}")
        if ea.get("date_added"):
            meta.append(f"added {ea['date_added']}")
        if meta:
            ctk.CTkLabel(
                self.qv_ea_frame, text="  ·  ".join(meta), anchor="w",
                justify="left", wraplength=240, font=ctk.CTkFont(size=11),
                text_color=("gray40", "gray65"),
            ).grid(row=rr, column=0, sticky="ew", padx=8, pady=(0, 2))
            rr += 1
        interv = ea.get("intervention", "")
        if interv:
            ctk.CTkLabel(
                self.qv_ea_frame, text=interv, anchor="w", justify="left",
                wraplength=240, font=ctk.CTkFont(size=11),
                text_color=("gray35", "gray70"),
            ).grid(row=rr, column=0, sticky="ew", padx=8, pady=(0, 6))

    def _qv_latest_note(self, r, row) -> int:
        """Render the latest course note (CSV `LatestCourseNote`) with its
        date (`MyCourseContact`, = last CI contact) as a compact boxed
        preview above the detailed-notes section. Skipped if no note."""
        note = self._cell(row, "LatestCourseNote")
        if not note:
            return r
        date = self._cell(row, "MyCourseContact")
        box = ctk.CTkFrame(
            self.qv_body, fg_color=("gray92", "gray22"), corner_radius=6)
        box.grid(row=r, column=0, columnspan=2, sticky="ew",
                 padx=4, pady=(6, 2))
        box.grid_columnconfigure(0, weight=1)
        head = "Latest course note" + (f"   ·   {date}" if date else "")
        ctk.CTkLabel(
            box, text=head, anchor="w",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=("gray35", "gray70"),
        ).grid(row=0, column=0, sticky="ew", padx=8, pady=(4, 0))
        ctk.CTkLabel(
            box, text=note, anchor="w", justify="left", wraplength=320,
            font=ctk.CTkFont(size=12),
        ).grid(row=1, column=0, sticky="ew", padx=8, pady=(0, 6))
        return r + 1

    def _qv_followup_editor(self, r, row) -> int:
        """Editable Follow-up Date: shows the current value (CSV
        CourseFollowupDate), lets the user type or pick a date and write it
        back to Salesforce. The CSV value won't refresh until the next ↻, so
        success is confirmed in the activity log."""
        sid = self._cell(row, "StudentID")
        if not sid:
            return r
        cur = self._cell(row, "CourseFollowupDate")
        frame = ctk.CTkFrame(self.qv_body, fg_color="transparent")
        frame.grid(row=r, column=0, columnspan=2, sticky="ew",
                   padx=4, pady=(4, 2))
        frame.grid_columnconfigure(4, weight=1)
        ctk.CTkLabel(
            frame, text="Follow-up date", anchor="w",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=("gray35", "gray70"),
        ).grid(row=0, column=0, sticky="w", padx=(4, 6))
        entry = ctk.CTkEntry(frame, width=104, placeholder_text="MM/DD/YYYY")
        if cur:
            entry.insert(0, cur)
        entry.grid(row=0, column=1, sticky="w")

        def pick() -> None:
            from datetime import datetime as _dt
            initial = None
            cur_val = entry.get().strip()
            for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
                try:
                    initial = _dt.strptime(cur_val, fmt).date()
                    break
                except ValueError:
                    continue
            d = prompt_calendar_pick(self.frame.winfo_toplevel(), initial)
            if d:
                entry.delete(0, "end")
                entry.insert(0, d.strftime("%m/%d/%Y"))

        ctk.CTkButton(
            frame, text="📅", width=32, command=pick, **SECONDARY_BTN_KWARGS,
        ).grid(row=0, column=2, padx=2)
        def do_set() -> None:
            val = entry.get().strip()

            def _applied(res) -> None:
                # On success, write the new date into the cached row AND
                # re-render the grid so the Followup Date column shows it
                # immediately — no need to wait for a ↻ CSV reload. (We know
                # exactly what Salesforce stored, so we mirror it locally.)
                if res.get("ok"):
                    try:
                        row["CourseFollowupDate"] = val
                        self.populate()
                    except Exception:
                        pass

            self.app._set_followup_date_for(sid, val, on_apply=_applied)

        ctk.CTkButton(
            frame, text="Set", width=46, command=do_set,
        ).grid(row=0, column=3, padx=2)

        # Follow-up note row (writes back to Salesforce; commit = blur). Own
        # sub-frame so the entry can stretch full width between label + Set.
        cur_note = self._cell(row, "CourseFollowupNote")
        note_row = ctk.CTkFrame(frame, fg_color="transparent")
        note_row.grid(row=1, column=0, columnspan=5, sticky="ew", pady=(2, 0))
        ctk.CTkLabel(
            note_row, text="Follow-up note", anchor="w",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=("gray35", "gray70"),
        ).pack(side="left", padx=(4, 6))

        def do_set_note() -> None:
            txt = note_entry.get().strip()

            def _applied_note(res) -> None:
                if res.get("ok"):
                    try:
                        row["CourseFollowupNote"] = txt
                        self.populate()
                    except Exception:
                        pass

            self.app._set_followup_note_for(sid, txt, on_apply=_applied_note)

        ctk.CTkButton(
            note_row, text="Set", width=46, command=do_set_note,
        ).pack(side="right", padx=2)
        note_entry = ctk.CTkEntry(note_row, placeholder_text="follow-up note…")
        if cur_note:
            note_entry.insert(0, cur_note)
        note_entry.pack(side="left", fill="x", expand=True, padx=(0, 4))
        return r + 1

    def _qv_field(self, r, row, label, key, kind) -> int:
        val = self._cell(row, key) if key else ""
        color = None
        widget = None
        if kind == "date_countdown":
            d = days_until(val)
            if val and d is not None:
                suffix = (f"{d}d" if d >= 0 else f"{-d}d ago")
                val = f"{val}  ({suffix})"
                color = ("#b00020", "#ff7b72") if d <= 14 else \
                        ("#9a6700", "#ffd166") if d <= 30 else None
            if not self._cell(row, key):
                return r
        elif kind == "timezone":
            if not val:
                return r
            lt = student_local_time(val)
            if lt:
                val = f"{val}  ·  {lt} local"
        elif kind == "last_action":
            val = last_logged_action(self._cell(row, "StudentID"))
            if not val:
                return r
        elif kind == "email":
            if not val:
                return r
        elif kind == "phone":
            val = self._phone_value(row)
            if not val:
                return r
        elif kind == "longtext":
            if not val:
                return r
        else:  # plain text
            if not val:
                return r

        lbl = ctk.CTkLabel(
            self.qv_body, text=f"{label}:", anchor="nw",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=("gray35", "gray70"), width=70,
        )
        lbl.grid(row=r, column=0, sticky="nw", padx=(0, 6), pady=1)
        valframe = ctk.CTkFrame(self.qv_body, fg_color="transparent")
        valframe.grid(row=r, column=1, sticky="ew", pady=1)
        valframe.grid_columnconfigure(0, weight=1)
        wrap = 320 if not self._narrow else 180
        vlbl = ctk.CTkLabel(
            valframe, text=val, anchor="w", justify="left",
            wraplength=wrap, font=ctk.CTkFont(size=12),
        )
        # Email / phone are clickable links (open in the OS default app)
        # and get a Copy button.
        if kind in ("email", "phone") and val:
            link_color = ("#1f6feb", "#58a6ff")
            vlbl.configure(text_color=link_color, cursor="hand2")
            opener = (self._open_mailto if kind == "email"
                      else self._open_tel)
            vlbl.bind("<Button-1>", lambda e, v=val: opener(v))
            vlbl.grid(row=0, column=0, sticky="ew")
            ctk.CTkButton(
                valframe, text="Copy", width=46,
                command=lambda v=val: self._copy_text(v),
                **SECONDARY_BTN_KWARGS,
            ).grid(row=0, column=1, padx=(6, 0))
        else:
            if color:
                vlbl.configure(text_color=color)
            vlbl.grid(row=0, column=0, sticky="ew")
        return r + 1

    def _phone_value(self, row: dict) -> str:
        """Phone isn't a fixed CSV column — find any header containing
        'phone' (so it works whatever the user names the column they add
        to their Salesforce caseload view)."""
        for k, v in row.items():
            if "phone" in k.lower() and str(v or "").strip():
                return str(v).strip()
        return ""

    def _open_mailto(self, email: str) -> None:
        try:
            os.startfile(f"mailto:{email.strip()}")
        except Exception as e:
            self.app._append_log(f"Couldn't open mail app: {e}", error=True)

    def _open_tel(self, phone: str) -> None:
        digits = re.sub(r"[^\d+]", "", phone or "")
        if not digits:
            return
        try:
            os.startfile(f"tel:{digits}")
        except Exception as e:
            self.app._append_log(f"Couldn't open phone handler: {e}",
                                 error=True)

    def _qv_task_badges(self, r, row) -> None:
        bar = ctk.CTkFrame(self.qv_body, fg_color="transparent")
        bar.grid(row=r, column=0, columnspan=2, sticky="w", pady=(4, 1))
        ctk.CTkLabel(
            bar, text="Tasks:", font=ctk.CTkFont(size=11, weight="bold"),
            text_color=("gray35", "gray70"),
        ).pack(side="left", padx=(0, 6))
        sid = self._cell(row, "StudentID")
        # Track which student the badges currently represent, so a
        # late-arriving status fetch for a previously-selected student
        # doesn't recolor the wrong badges.
        self._qv_status_sid = sid
        badges: dict[str, tuple] = {}
        for i in (1, 2, 3):
            state, date, attempts = parse_task_status(
                self._cell(row, f"Task{i}"))
            badge = ctk.CTkLabel(
                bar, text=f"T{i}", corner_radius=6,
                font=ctk.CTkFont(size=11, weight="bold"),
                width=42, height=20,
            )
            badge.pack(side="left", padx=2)
            # Initial paint from the CSV: submitted vs not. Pass/fail is
            # NOT in the CSV (see parse_task_status), so a submitted task
            # shows a neutral dot until the live status fetch returns —
            # never a (possibly wrong) green check.
            self._apply_task_badge(badge, i, state, date, attempts, "", row)
            badges[str(i)] = (badge, date, attempts)
        # Fetch the real pass/fail (cell color/title) on demand, debounced
        # + cached. Recolors the badges in place when it returns.
        self._schedule_task_status_fetch(sid, badges, row)

    def _apply_task_badge(self, badge, i, state, date, attempts,
                          status_text, row) -> None:
        """(Re)paint one task badge for `state`, with tooltip + EMA click.
        Safe to call repeatedly (initial CSV paint, then after the live
        status fetch)."""
        try:
            if not badge.winfo_exists():
                return
        except Exception:
            return
        mark, bg, fg = TASK_BADGE_STYLES.get(state, TASK_BADGE_STYLES["submitted"])
        badge.configure(text=f"T{i} {mark}", fg_color=bg, text_color=fg)
        default_label = {
            "passed": "passed", "returned": "not passed",
            "pending": "in progress", "none": "not submitted",
            "submitted": "submitted (loading pass/fail…)",
        }.get(state, "submitted")
        label = status_text or default_label
        note = f"Task {i}: {label}"
        if date:
            note += f"  ({date})"
        if attempts:
            note += f"  ·  {attempts} attempt" + ("s" if attempts != 1 else "")
        if state != "none":
            note += "\nclick to open the EMA Score Report"
            badge.configure(cursor="hand2")
            badge.bind(
                "<Button-1>",
                lambda e, t=i, rw=row: self._open_task_report_for(rw, t))
        _attach_tooltip(badge, note)

    def _schedule_task_status_fetch(self, sid, badges, row) -> None:
        """Debounced trigger for the on-demand live task-status fetch, so
        arrowing quickly through students doesn't queue a fetch per row."""
        if not sid:
            return
        h = getattr(self, "_qv_status_after", None)
        if h:
            try:
                self.frame.after_cancel(h)
            except Exception:
                pass

        def go():
            self.app._fetch_task_status_for(
                sid,
                lambda st, want=sid: self._apply_fetched_status(
                    want, st, badges, row),
            )
        try:
            self._qv_status_after = self.frame.after(350, go)
        except Exception:
            pass

    def _apply_fetched_status(self, want_sid, statuses, badges, row) -> None:
        """Recolor the task badges once the live status fetch returns —
        only if the quick-view still shows the same student."""
        if getattr(self, "_qv_status_sid", None) != want_sid:
            return
        for i in (1, 2, 3):
            info = (statuses or {}).get(str(i))
            entry = badges.get(str(i))
            if not entry:
                continue
            badge, date, attempts = entry
            if not info:
                continue
            self._apply_task_badge(
                badge, i, info.get("state", "submitted"),
                date or info.get("date") or "",
                attempts or info.get("attempts") or 0,
                info.get("status") or "", row)

    def _open_task_report_for(self, row: dict, task_num: int) -> None:
        student_id = self._cell(row, "StudentID")
        course_code = self._cell(row, "CourseCode")
        name = self._cell(row, "Name") or student_id
        self.app._open_task_report(student_id, course_code, task_num, name)

    def _copy_text(self, text: str) -> None:
        try:
            self.frame.clipboard_clear()
            self.frame.clipboard_append(text)
            self.app._append_log(f"Copied: {text}")
        except Exception:
            pass

    # ----- note viewer -----

    def _notes_hint(self) -> None:
        self._last_notes = None
        for w in self.notes_holder.winfo_children():
            w.destroy()
        self.notes_title.configure(text="Notes")
        ctk.CTkLabel(
            self.notes_holder,
            text="Press Enter (or Review notes) to load this\n"
                 "student's notes from Salesforce.",
            justify="left", text_color=("gray45", "gray60"),
            font=ctk.CTkFont(size=12),
        ).grid(row=0, column=0, sticky="w", padx=6, pady=6)

    def _review_focused(self) -> None:
        iid = self.tree.focus()
        query, label = self._query_label_for_iid(iid)
        if query:
            self.review_notes(query, label)

    def review_notes(self, query: str, label: str) -> None:
        """Open the student (navigation) and load their notes into the
        viewer. Guarded against running mid-batch."""
        if getattr(self.app, "_is_busy", False):
            self.show_notes_message("Busy — finish the current run first.")
            return
        self.show_notes_loading(label)
        self.app._append_log(f"Loading notes for {label}…")
        self.app._review_notes(query, label, self)

    def _scroll_to_notes(self) -> None:
        """Scroll the detail body so the notes section is at the top of the
        viewport (the quick view is reference info; once you ask for notes
        you want to see them, not scroll for them)."""
        def do():
            try:
                c = self.detail_scroll._parent_canvas
                c.update_idletasks()
                bbox = c.bbox("all")
                if not bbox:
                    return
                content_h = bbox[3] - bbox[1]
                y = self.notes_title.winfo_y()
                if content_h > 0:
                    c.yview_moveto(max(0.0, min(1.0, y / content_h)))
            except Exception:
                pass
        try:
            self.frame.after_idle(do)
        except Exception:
            do()

    def show_notes_loading(self, label: str) -> None:
        for w in self.notes_holder.winfo_children():
            w.destroy()
        self.notes_title.configure(text="Notes")
        ctk.CTkLabel(
            self.notes_holder, text=f"Loading notes for {label}…  (~2–3s)",
            text_color=("gray40", "gray65"), font=ctk.CTkFont(size=12),
        ).grid(row=0, column=0, sticky="w", padx=6, pady=6)
        self._scroll_to_notes()

    def show_notes_message(self, msg: str) -> None:
        for w in self.notes_holder.winfo_children():
            w.destroy()
        ctk.CTkLabel(
            self.notes_holder, text=msg, font=ctk.CTkFont(size=12),
            text_color=("#b00020", "#ff7b72"), justify="left", wraplength=360,
        ).grid(row=0, column=0, sticky="w", padx=6, pady=6)
        self._scroll_to_notes()

    def show_notes_error(self, label: str, msg: str) -> None:
        self.show_notes_message(f"Couldn't load notes for {label}: {msg}")

    def _refresh_notes_font(self) -> None:
        """Re-render the currently shown notes at the new 'notes' text
        size (called when the size changes via Settings or Ctrl +/-)."""
        notes = getattr(self, "_last_notes", None)
        if notes is not None:
            self.show_notes(getattr(self, "_last_notes_label", ""), notes)

    def show_notes(self, label: str, notes: list) -> None:
        self._last_notes = notes
        self._last_notes_label = label
        for w in self.notes_holder.winfo_children():
            w.destroy()
        self.notes_title.configure(text=f"Notes for {label} ({len(notes)})")
        if not notes:
            ctk.CTkLabel(
                self.notes_holder, text="No notes found for this student.",
                text_color=("gray45", "gray60"), font=ctk.CTkFont(size=12),
            ).grid(row=0, column=0, sticky="w", padx=6, pady=6)
            self._scroll_to_notes()
            return
        for i, nt in enumerate(notes):
            self._render_note_row(i, nt)
        self._scroll_to_notes()

    def _render_note_row(self, i: int, nt: dict) -> None:
        date = fmt_note_date(nt.get("date", ""))
        ntype = (nt.get("type") or "").strip()
        subject = (nt.get("subject") or "").strip()
        author = (nt.get("author") or "").strip()
        url = (nt.get("url") or "").strip()
        body = note_html_to_text(nt.get("text") or "")
        head = subject or ntype or "(note)"
        meta_bits = [b for b in (date, ntype, author) if b]
        meta = "  ·  ".join(meta_bits)

        card = ctk.CTkFrame(self.notes_holder, fg_color=("gray90", "gray22"))
        card.grid(row=i, column=0, sticky="ew", padx=2, pady=2)
        card.grid_columnconfigure(0, weight=1)
        state = {"open": False}

        preview = body.replace("\n", " ")
        preview = (preview[:90] + "…") if len(preview) > 90 else preview

        ns = font_size("notes")
        btn = ctk.CTkButton(
            card, text=f"▸ {head}", anchor="w", height=24,
            fg_color="transparent", text_color=("gray10", "gray90"),
            hover_color=("gray80", "gray30"),
            font=ctk.CTkFont(size=ns + 1, weight="bold"),
        )
        btn.grid(row=0, column=0, sticky="ew", padx=2, pady=(2, 0))
        meta_lbl = ctk.CTkLabel(
            card, text=meta, anchor="w", justify="left",
            text_color=("gray45", "gray60"),
            font=ctk.CTkFont(size=max(8, ns - 2)),
        )
        meta_lbl.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 0))
        prev_lbl = ctk.CTkLabel(
            card, text=preview, anchor="w", justify="left",
            text_color=("gray35", "gray70"),
            font=ctk.CTkFont(size=max(8, ns - 1)), wraplength=420,
        )
        prev_lbl.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 4))
        full_lbl = ctk.CTkLabel(
            card, text=body, anchor="w", justify="left",
            font=ctk.CTkFont(size=ns), wraplength=420,
        )
        # Footer (only when expanded): open the full note record where the
        # complete Text field lives.
        footer = ctk.CTkFrame(card, fg_color="transparent")
        if url:
            ctk.CTkButton(
                footer, text="Open full note ↗", width=130, height=22,
                command=lambda u=url: self._open_note_record(u),
                **SECONDARY_BTN_KWARGS,
            ).pack(side="left", padx=2, pady=(0, 4))

        def toggle():
            state["open"] = not state["open"]
            if state["open"]:
                prev_lbl.grid_remove()
                full_lbl.grid(row=2, column=0, sticky="ew", padx=10,
                              pady=(0, 2))
                footer.grid(row=3, column=0, sticky="ew", padx=6)
                btn.configure(text=f"▾ {head}")
            else:
                full_lbl.grid_remove()
                footer.grid_remove()
                prev_lbl.grid(row=2, column=0, sticky="ew", padx=10,
                              pady=(0, 4))
                btn.configure(text=f"▸ {head}")
        btn.configure(command=toggle)

    def _open_note_record(self, url: str) -> None:
        """Open a note record (the Record ID link) in the user's default
        browser, where the full Text field is visible."""
        try:
            os.startfile(url)
        except Exception as e:
            self.app._append_log(f"Couldn't open note: {e}", error=True)

    def _style_tree(self) -> None:
        """Style the ttk.Treeview to approximate the CTk theme. Uses the
        'clam' ttk theme because the native Windows theme ignores
        Treeview background config."""
        if ctk.get_appearance_mode() == "Dark":
            bg, fg, sel, hbg = "#2b2b2b", "#dce4ee", "#1f6aa5", "#343638"
            self._even_bg, self._odd_bg = "#2b2b2b", "#333333"
        else:
            bg, fg, sel, hbg = "#ffffff", "#1a1a1a", "#3a7ebf", "#e5e5e5"
            self._even_bg, self._odd_bg = "#ffffff", "#f0f0f0"
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(
            "Caseload.Treeview", background=bg, foreground=fg,
            fieldbackground=bg, bordercolor=bg, borderwidth=0,
        )
        style.map("Caseload.Treeview",
                  background=[("selected", sel)],
                  foreground=[("selected", "#ffffff")])
        style.configure(
            "Caseload.Treeview.Heading", background=hbg, foreground=fg,
            relief="flat",
        )
        style.map("Caseload.Treeview.Heading", background=[("active", sel)])
        # Font + row height come from the 'viewer' channel (adjustable).
        apply_caseload_tree_font(font_size("viewer"))
        if not _viewer_font_registered[0]:
            register_font_apply("viewer", apply_caseload_tree_font)
            _viewer_font_registered[0] = True

    def _make_check_images(self) -> None:
        """Build checked/unchecked checkbox images that match the CTk
        scenario-editor style — a filled blue rounded box with a white
        tick when checked, an outlined box when not. Kept on the instance
        so Tk retains the reference (else they'd be GC'd and render
        blank). Colours follow the current light/dark appearance."""
        self._img_unchecked, self._img_checked = _build_checkbox_images()

    def _chk_img(self, checked: bool):
        return self._img_checked if checked else self._img_unchecked

    def set_base_size(self, n: int) -> None:
        """Change the base text size and rebuild the tag fonts so bold /
        heading / etc. scale with it."""
        self._base_size = max(8, min(40, int(n)))
        try:
            self.text.configure(font=("Segoe UI", self._base_size))
            self._configure_tags()
        except Exception:
            pass

    # ----- data + interaction -----

    @staticmethod
    def _sortkey(v):
        s = str(v).strip()
        try:
            return (0, float(s.replace(",", "")))
        except ValueError:
            return (1, s.lower())

    def _on_search(self) -> None:
        self._query = self.search_var.get()
        self.populate()

    def _sort_by(self, col: str) -> None:
        # A heading drag (reorder) fires the heading command on release too;
        # swallow that one sort so reordering doesn't also re-sort.
        if self._suppress_next_sort:
            self._suppress_next_sort = False
            return
        if self._sort_col == col:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_col = col
            self._sort_reverse = False
        self.populate()

    def _on_refresh(self) -> None:
        # Routes through the app's worker-thread download; the cache
        # reload calls back into populate() via _refresh_caseload_panel.
        try:
            self.app._on_caseload_refresh_clicked()
        except Exception:
            pass

    # ----- multi-select (checkboxes) -----

    @staticmethod
    def _row_key(r: dict) -> str:
        """Stable identity for a caseload row: the unambiguous Student ID
        (CSV header 'StudentID', spaced 'Student ID' as a fallback), then
        the name. Used to track checked rows across re-renders."""
        if not r:
            return ""
        return (str(r.get("StudentID", "") or r.get("Student ID", "")).strip()
                or str(r.get("Name", "")).strip())

    def _checked_rows(self) -> list[dict]:
        """The checked rows, resolved fresh from the live caseload (so a
        refresh that drops a student simply drops it from the selection)."""
        rows = self.app._caseload_rows or []
        return [r for r in rows if self._row_key(r) in self._checked_ids]

    def _on_tree_click(self, event):
        """Left-click handler: toggle selection when the click lands in the
        checkbox (#0 tree) column; arm a column drag on a heading press;
        otherwise let the default browse-select / sort / resize run."""
        region = self.tree.identify_region(event.x, event.y)
        if region == "heading":
            name = self._col_name_at(event.x)
            self._coldrag = (
                {"col": name, "x": event.x, "moved": False} if name else None)
            return  # let sort (on release) / resize proceed unless we drag
        self._coldrag = None
        if region != "tree":
            return  # data cell / separator → default behaviour
        iid = self.tree.identify_row(event.y)
        if iid:
            self._toggle_row(iid)
        return "break"  # don't move the highlight on a checkbox click

    def _disp_cols(self) -> list[str]:
        """The currently-displayed data columns (names) in display order."""
        try:
            cols = list(self.tree["columns"])
        except Exception:
            return []
        try:
            dc = self.tree["displaycolumns"]
            dc = list(dc) if not isinstance(dc, str) else []
        except Exception:
            dc = []
        if not dc or dc == ["#all"]:
            return cols
        return [c for c in dc if c in cols]

    def _col_name_at(self, x) -> Optional[str]:
        """Data-column name under x (None for the #0 checkbox column)."""
        colid = self.tree.identify_column(x)
        if not colid or colid == "#0":
            return None
        try:
            idx = int(colid[1:]) - 1
        except (ValueError, IndexError):
            return None
        cols = self._disp_cols()
        return cols[idx] if 0 <= idx < len(cols) else None

    def _on_head_drag(self, event):
        d = self._coldrag
        if not d:
            return
        if not d["moved"] and abs(event.x - d["x"]) > 6:
            d["moved"] = True
            try:
                self.tree.configure(cursor="exchange")
            except Exception:
                pass

    def _on_head_release(self, event):
        d = self._coldrag
        self._coldrag = None
        try:
            self.tree.configure(cursor="")
        except Exception:
            pass
        if not d or not d.get("moved") or not d.get("col"):
            return
        # This release was a drag, not a heading click → don't also sort.
        self._suppress_next_sort = True
        src = d["col"]
        cols = self._disp_cols()
        if src not in cols:
            return
        tgt = self._col_name_at(event.x)
        cols.remove(src)
        if tgt and tgt in cols:
            cols.insert(cols.index(tgt), src)
        elif self.tree.identify_column(event.x) == "#0":
            cols.insert(0, src)  # dropped on the far-left → move to front
        else:
            cols.append(src)  # released past the last column → end
        try:
            self.tree["displaycolumns"] = cols
        except Exception:
            return
        self.persist_column_state()

    def _toggle_focused_row(self, event=None):
        """Space toggles the checkbox of the keyboard-focused row."""
        iid = self.tree.focus()
        if iid:
            self._toggle_row(iid)
        return "break"

    def _toggle_row(self, iid) -> None:
        row = self._row_by_iid.get(iid)
        key = self._row_key(row) if row else ""
        if not key:
            return
        checked = key not in self._checked_ids
        if checked:
            self._checked_ids.add(key)
        else:
            self._checked_ids.discard(key)
        self.tree.item(iid, image=self._chk_img(checked))
        self._after_selection_change()

    def _toggle_select_all(self) -> None:
        """Checkbox-column heading: select all currently-visible rows, or
        clear them if they're already all selected. Operates on the
        filtered/searched view, which is the point of pairing select-all
        with filters."""
        visible = self.tree.get_children()
        keys = [k for k in (self._row_key(self._row_by_iid.get(i, {}))
                            for i in visible) if k]
        if not keys:
            return
        clear = all(k in self._checked_ids for k in keys)
        for iid in visible:
            k = self._row_key(self._row_by_iid.get(iid, {}))
            if not k:
                continue
            if clear:
                self._checked_ids.discard(k)
            else:
                self._checked_ids.add(k)
            self.tree.item(iid, image=self._chk_img(not clear))
        self._after_selection_change()

    def _clear_selection(self) -> None:
        self._checked_ids.clear()
        for iid in self.tree.get_children():
            self.tree.item(iid, image=self._chk_img(False))
        self._after_selection_change()

    def _after_selection_change(self) -> None:
        """Sync the select-all glyph, the count label, and the action
        bar's visibility to the current selection."""
        # Select-all heading image reflects the visible rows.
        visible = self.tree.get_children()
        vkeys = [k for k in (self._row_key(self._row_by_iid.get(i, {}))
                             for i in visible) if k]
        all_vis = bool(vkeys) and all(k in self._checked_ids for k in vkeys)
        try:
            self.tree.heading("#0", image=self._chk_img(all_vis))
        except Exception:
            pass
        n = len(self._checked_rows())
        if n > 0:
            self.sel_count_lbl.configure(text=f"{n} selected")
            self.action_bar.grid()
        else:
            self.action_bar.grid_remove()

    def _panel_action_scenarios(self) -> list:
        """Scenarios offered in the panel's Fire menus: those flagged
        'Show as a caseload-panel action' (non-batch only). Falls back to
        every non-batch scenario when none are flagged yet, so the menu is
        never empty for users who haven't curated."""
        nonbatch = [s for s in self.app.scenarios.values()
                    if s.batch is None]
        flagged = [s for s in nonbatch
                   if getattr(s, "panel_action", False)]
        return flagged or nonbatch

    def _on_fire_selected_clicked(self) -> None:
        """Post a menu of the curated panel-action scenarios; firing one
        runs it across the checked students as a mini-batch."""
        if not self._checked_rows():
            return
        nonbatch = self._panel_action_scenarios()
        if not nonbatch:
            self.app._append_log("No non-batch actions to fire.")
            return
        menu = tk.Menu(self.fire_sel_btn, tearoff=0)
        for sc in nonbatch:
            menu.add_command(
                label=sc.name,
                command=lambda s=sc: self.app._fire_on_selected(
                    s, self._checked_rows()))
        try:
            x = self.fire_sel_btn.winfo_rootx()
            y = self.fire_sel_btn.winfo_rooty() + self.fire_sel_btn.winfo_height()
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    # ----- column filters (collapsible; reuses the batch FilterRow) -----

    def _panel_columns(self) -> list[str]:
        """Display-name columns for the filter dropdowns, from the loaded
        caseload (CSV header → display name, same as the batch editor)."""
        rows = self.app._caseload_rows or []
        if not rows:
            return []
        return [caseload_csv.display_for_column(h) for h in rows[0].keys()
                if not _is_task_facet_col(h)]

    def _toggle_filters(self) -> None:
        if self._filters_open:
            self.filters_wrap.grid_remove()
            self._filters_open = False
        else:
            self.filters_wrap.grid()
            self._filters_open = True
            if not self.filter_rows:
                self._add_filter_row()
        self._apply_bar_mode()  # refresh the ▸/▾ arrow (respects narrow mode)

    def _add_filter_row(self, prefilled: Optional[dict] = None) -> "FilterRow":
        row = FilterRow(
            self.filters_container, self._panel_columns(),
            on_delete=self._delete_filter_row,
            value_provider=self.app._filter_value_suggestions,
        )
        row.frame.pack(fill="x", padx=4, pady=2)
        if prefilled:
            row.load(prefilled)
        self.filter_rows.append(row)
        return row

    def _delete_filter_row(self, row: "FilterRow") -> None:
        try:
            self.filter_rows.remove(row)
        except ValueError:
            return
        try:
            row.frame.destroy()
        except Exception:
            pass

    def _apply_filters(self) -> None:
        """Read the filter rows → active filter set → re-render. Rows with
        no column chosen are ignored."""
        self._active_filters = [
            f for f in (r.serialize() for r in self.filter_rows)
            if f.get("column", "").strip()
        ]
        self.populate()

    def _clear_filters(self) -> None:
        for row in list(self.filter_rows):
            self._delete_filter_row(row)
        self._active_filters = []
        self.populate()

    def _apply_active_filters(self, rows: list[dict]) -> list[dict]:
        """Apply the active column filters via the shared filter engine.
        Display-name columns are resolved to CSV headers; identity entries
        pass through. Returns all rows when nothing is active or on error
        (fail-open — the search box + visible list still work)."""
        if not self._active_filters or not rows:
            return list(rows)
        headers = list(rows[0].keys())
        filters = [
            _rewrite_task_filter(_resolve_filter_columns(f, headers))
            for f in self._active_filters
        ]
        try:
            return caseload_filter.apply_filters(filters, rows)
        except Exception:
            return list(rows)

    # ----- column show/hide + reorder + width persistence -----

    def _load_col_prefs(self) -> dict:
        """Parse the persisted caseload column layout. Always returns a
        dict with visible / hidden / widths keys."""
        import json
        raw = (self.app.settings.caseload_columns or "").strip()
        if not raw:
            return {"visible": [], "hidden": [], "widths": {}}
        try:
            d = json.loads(raw)
            return {
                "visible": [str(c) for c in d.get("visible", [])],
                "hidden": [str(c) for c in d.get("hidden", [])],
                "widths": {str(k): v for k, v in dict(d.get("widths", {})).items()},
            }
        except Exception:
            return {"visible": [], "hidden": [], "widths": {}}

    def _resolve_display_columns(self, headers: list[str],
                                 prefs: Optional[dict] = None) -> list[str]:
        """Ordered list of visible CSV headers from saved prefs. Columns
        never seen before (new CSV exports) default to visible and append
        after the saved order; explicitly-hidden ones stay hidden."""
        prefs = prefs if prefs is not None else self._load_col_prefs()
        hidden = set(prefs.get("hidden", []))
        visible = [c for c in prefs.get("visible", []) if c in headers]
        seen = set(visible) | hidden
        for c in headers:
            if c not in seen:
                visible.append(c)
        return visible or list(headers)

    def _apply_column_layout(self, headers: list[str]) -> None:
        """Set the Treeview's displaycolumns (show/hide + order) and saved
        widths from the persisted prefs."""
        prefs = self._load_col_prefs()
        visible = self._resolve_display_columns(headers, prefs)
        # Defensive: displaycolumns MUST only name columns the tree actually
        # defines — one stray name makes ttk reject the WHOLE assignment, so a
        # single bad entry would silently break show/hide entirely.
        try:
            defined = set(self.tree["columns"])
        except Exception:
            defined = set(headers)
        safe = [v for v in visible if v in defined] or \
            [h for h in headers if h in defined]
        try:
            self.tree["displaycolumns"] = safe
        except Exception:
            try:
                self.tree.configure(displaycolumns="#all")
            except Exception:
                pass
        widths = prefs.get("widths", {})
        for h, w in widths.items():
            if h in headers and isinstance(w, int) and w > 20:
                try:
                    self.tree.column(h, width=w)
                except Exception:
                    pass

    def _current_col_widths(self) -> dict:
        """Current pixel width of every defined column, keyed by header."""
        out = {}
        try:
            cols = list(self.tree["columns"])
        except Exception:
            return out
        for c in cols:
            try:
                out[c] = int(self.tree.column(c, "width"))
            except Exception:
                pass
        return out

    def persist_column_state(self) -> None:
        """Capture the current visible/order + widths into Settings. Called
        on Apply, and on teardown (pop-out/re-dock, app close) so a
        drag-resize survives even without opening the chooser."""
        import json
        try:
            cols = list(self.tree["columns"])
        except Exception:
            cols = []
        if not cols:
            return
        try:
            dc = self.tree["displaycolumns"]
            dc = list(dc) if not isinstance(dc, str) else [dc]
            if not dc or dc == ["#all"]:
                visible = list(cols)
            else:
                visible = [c for c in dc if c in cols]
        except Exception:
            visible = list(cols)
        hidden = [c for c in cols if c not in visible]
        payload = {
            "visible": visible, "hidden": hidden,
            "widths": self._current_col_widths(),
        }
        try:
            self.app.settings.caseload_columns = json.dumps(payload)
            save_settings(self.app.settings)
        except Exception:
            pass

    def _open_columns_dialog(self) -> None:
        """Dialog to show/hide and reorder the caseload columns."""
        rows = self.app._caseload_rows or []
        if not rows:
            return
        # Same column set the grid actually has — EXCLUDE the hidden per-task
        # facet helpers (Task1Date/…); otherwise Apply would try to put them
        # in displaycolumns, which the tree doesn't define → ttk error → the
        # whole show/hide silently no-ops.
        headers = [h for h in rows[0].keys() if not _is_task_facet_col(h)]
        # Capture any drag-resizes done since load so they aren't lost.
        self.persist_column_state()
        prefs = self._load_col_prefs()
        visible_order = self._resolve_display_columns(headers, prefs)
        hidden = [c for c in headers if c not in visible_order]
        # Working model: [header, visible_bool], visible first in order.
        work = [[c, True] for c in visible_order] + [[c, False] for c in hidden]

        dlg = ctk.CTkToplevel(self.frame)
        dlg.title("Choose columns")
        dlg.geometry("440x500")
        try:
            dlg.transient(self.frame.winfo_toplevel())
        except Exception:
            pass
        dlg.attributes("-topmost", True)
        try:
            dlg.grab_set()
        except Exception:
            pass
        dlg.grid_columnconfigure(0, weight=1)
        dlg.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(
            dlg, text="Show, hide and reorder caseload columns",
            font=ctk.CTkFont(size=12, weight="bold"),
        ).grid(row=0, column=0, sticky="w", padx=12, pady=(10, 4))
        scroll = ctk.CTkScrollableFrame(dlg)
        scroll.grid(row=1, column=0, sticky="nsew", padx=8, pady=4)
        scroll.grid_columnconfigure(2, weight=1)

        def move(idx: int, delta: int) -> None:
            j = idx + delta
            if 0 <= j < len(work):
                work[idx], work[j] = work[j], work[idx]
                redraw()

        def redraw() -> None:
            for w in scroll.winfo_children():
                w.destroy()
            for i, pair in enumerate(work):
                hdr = pair[0]
                var = ctk.BooleanVar(value=pair[1])

                def on_toggle(p=pair, v=var):
                    p[1] = v.get()

                ctk.CTkButton(
                    scroll, text="▲", width=26, command=lambda ix=i: move(ix, -1),
                    **SECONDARY_BTN_KWARGS,
                ).grid(row=i, column=0, padx=(2, 0), pady=1)
                ctk.CTkButton(
                    scroll, text="▼", width=26, command=lambda ix=i: move(ix, 1),
                    **SECONDARY_BTN_KWARGS,
                ).grid(row=i, column=1, padx=(2, 4), pady=1)
                ctk.CTkCheckBox(
                    scroll, text=caseload_csv.display_for_column(hdr),
                    variable=var, command=on_toggle,
                ).grid(row=i, column=2, sticky="w", padx=4, pady=1)

        redraw()

        btns = ctk.CTkFrame(dlg, fg_color="transparent")
        btns.grid(row=2, column=0, sticky="ew", padx=8, pady=(4, 10))

        def do_apply() -> None:
            visible = [p[0] for p in work if p[1]]
            if not visible:
                from tkinter import messagebox
                messagebox.showinfo(
                    "Columns", "Keep at least one column visible.")
                return
            hidden2 = [p[0] for p in work if not p[1]]
            import json
            payload = {
                "visible": visible, "hidden": hidden2,
                "widths": self._current_col_widths(),
            }
            self.app.settings.caseload_columns = json.dumps(payload)
            save_settings(self.app.settings)
            self._apply_column_layout(headers)
            dlg.destroy()

        def check_all() -> None:
            # Reveal every column WITHOUT changing the current order.
            for p in work:
                p[1] = True
            redraw()

        def copy_caseload() -> None:
            # Match the live Salesforce Caseload view's columns + order.
            from tkinter import messagebox
            if not self.app.worker.ready_event.is_set():
                messagebox.showinfo(
                    "Copy caseload", "Browser not ready yet — try again "
                    "once it's loaded.")
                return
            cols = self.app._read_caseload_columns_blocking()
            if not cols:
                messagebox.showinfo(
                    "Copy caseload",
                    "Couldn't read the caseload view's columns. Make sure "
                    "the browser is on the Caseload list, then try again.")
                return
            view = []
            for c in cols:
                csvh = caseload_csv.resolve_column(c.get("name", ""), headers)
                if csvh in headers and csvh not in view:
                    view.append(csvh)
            if not view:
                messagebox.showinfo(
                    "Copy caseload",
                    "None of the caseload view's columns matched the CSV "
                    "export.")
                return
            rest = [h for h in headers if h not in view]
            work[:] = [[h, True] for h in view] + [[h, False] for h in rest]
            redraw()

        ctk.CTkButton(btns, text="Apply", width=70, command=do_apply).pack(
            side="right", padx=(6, 0))
        ctk.CTkButton(
            btns, text="Cancel", width=70, command=dlg.destroy,
            **SECONDARY_BTN_KWARGS,
        ).pack(side="right", padx=(6, 0))
        ctk.CTkButton(
            btns, text="Show all", width=78, command=check_all,
            **SECONDARY_BTN_KWARGS,
        ).pack(side="left")
        ctk.CTkButton(
            btns, text="Copy caseload", width=110, command=copy_caseload,
            **SECONDARY_BTN_KWARGS,
        ).pack(side="left", padx=(6, 0))

    def _task_cell_value(self, r, header, task_cols):
        """Display value for one grid cell. For Task columns, prefix a colour
        glyph from the live pass/fail cache (✅/❌/🟦); a submitted task whose
        live status hasn't been scraped yet shows ⚪ ("not loaded"). Non-task
        columns and empty cells pass through unchanged. Only the DISPLAY is
        decorated — sort/filter/search still run on the raw CSV dict."""
        raw = r.get(header, "")
        tnum = task_cols.get(header)
        if not tnum or not raw:
            return raw
        sid = (r.get("StudentID") or "").strip()
        cache = getattr(self.app, "_task_status_cache", None) or {}
        info = (cache.get(sid) or {}).get(tnum)
        state = info.get("state") if info else "submitted"
        glyph = TASK_CELL_GLYPHS.get(state, "")
        return f"{glyph} {raw}" if glyph else raw

    def populate(self) -> None:
        rows = self.app._caseload_rows or []
        if not rows:
            self.tree.delete(*self.tree.get_children())
            self.tree["columns"] = ()
            self.empty_lbl.grid(row=0, column=0, sticky="nsew",
                                padx=20, pady=20)
            self.update_freshness()
            return
        self.empty_lbl.grid_remove()
        # Per-task facet columns (Task1Date/Count/Status, …) are hidden
        # helpers behind the single visible "Task N" column — keep them out
        # of the grid (the Task1/2/3 columns already show date+count+glyph).
        headers = [h for h in rows[0].keys() if not _is_task_facet_col(h)]
        if tuple(self.tree["columns"]) != tuple(headers):
            # Reset displaycolumns to "#all" BEFORE swapping the column set.
            # ttk validates the existing displaycolumns against the new
            # columns, so a stale entry (a column the previous CSV had but
            # the new one doesn't — e.g. after the user changes their
            # Salesforce view) makes `columns=` throw "Invalid column index".
            try:
                self.tree.configure(displaycolumns="#all")
            except Exception:
                pass
            self.tree["columns"] = headers
            # #0 (the tree column) holds the checkbox image; its heading is
            # a select-all/none toggle over the currently-visible rows.
            self.tree.column("#0", width=36, minwidth=36, stretch=False,
                             anchor="center")
            self.tree.heading("#0", command=self._toggle_select_all)
            for h in headers:
                self.tree.heading(
                    h, command=lambda c=h: self._sort_by(c))
                self.tree.column(h, width=120, minwidth=60,
                                 anchor="w", stretch=False)
            # Apply the saved show/hide + order + widths.
            self._apply_column_layout(headers)
        # Heading labels (display names) + sort arrow on the active one.
        for h in headers:
            disp = caseload_csv.display_for_column(h)
            if h == self._sort_col:
                disp += "  " + ("▼" if self._sort_reverse else "▲")
            self.tree.heading(h, text=disp)
        # Column filters first (the batch filter engine — now also carries
        # task pass/fail via the synthetic "Task Status" column), then the
        # search box (substring) on top.
        base = self._apply_active_filters(rows)
        if getattr(self, "_ea_only_var", None) is not None and \
                self._ea_only_var.get():
            base = [r for r in base if (r.get("EssentialAction") or "").strip()]
        q = self._query.strip().lower()
        view = ([r for r in base
                 if any(q in str(v).lower() for v in r.values())]
                if q else list(base))
        if self._sort_col:
            view.sort(key=lambda r: self._sortkey(r.get(self._sort_col, "")),
                      reverse=self._sort_reverse)
        # Task columns (Task1/Task2/…) get a colour glyph prefix from the
        # live pass/fail cache: ✅ passed · ❌ returned · 🟦 in-process ·
        # ⚪ submitted-but-not-yet-scraped. Map header → task number once.
        task_cols = {h: m.group(1) for h in headers
                     if (m := re.fullmatch(r"Task(\d+)", h))}
        self.tree.delete(*self.tree.get_children())
        self._row_by_iid = {}
        for i, r in enumerate(view):
            checked = self._row_key(r) in self._checked_ids
            iid = self.tree.insert(
                "", "end", image=self._chk_img(checked),
                values=[self._task_cell_value(r, h, task_cols)
                        for h in headers],
                tags=("even" if i % 2 == 0 else "odd",),
            )
            self._row_by_iid[iid] = r
        self._after_selection_change()
        self.update_freshness(count=len(view), total=len(rows))

    def _on_row_open(self, event=None) -> None:
        """Open the double-clicked student in Salesforce, reusing the
        current browser tab. Prefers the unambiguous Student ID,
        falling back to Name."""
        # A double-click in the checkbox (#0) column is a (double) toggle,
        # not an open — don't navigate.
        if event is not None and \
                self.tree.identify_region(event.x, event.y) == "tree":
            return "break"
        self._open_row(event, new_tab=False)

    def _on_row_open_new_tab(self, event=None) -> None:
        """Middle-click → open the student in a new console subtab,
        leaving any already-open student tabs as they are."""
        self._open_row(event, new_tab=True)

    def _query_label_for_iid(self, iid):
        """(query, label) for a row iid. `query` is the unambiguous
        Student ID (falling back to Name); `label` is the display name.
        Returns (None, None) if there's no usable row."""
        row = self._row_by_iid.get(iid) if iid else None
        if not row:
            return None, None
        name = str(row.get("Name", "")).strip()
        query = str(row.get("StudentID", "")).strip() or name
        if not query:
            return None, None
        return query, (name or query)

    def _row_query_label(self, event):
        """Resolve a MOUSE event's row to (query, label), selecting it."""
        iid = self.tree.identify_row(event.y) if event is not None else \
            self.tree.focus()
        if not iid:
            return None, None
        self.tree.selection_set(iid)
        self.tree.focus(iid)
        return self._query_label_for_iid(iid)

    def _open_row(self, event, new_tab: bool) -> None:
        query, _label = self._row_query_label(event)
        if query:
            self.app._find_student_by_query(query, new_tab=new_tab)

    def _focus_first_row(self, event=None):
        """Move keyboard focus from the search box into the rows,
        selecting the focused/first row so Up/Down navigation starts
        somewhere sensible."""
        kids = self.tree.get_children()
        if not kids:
            return "break"
        self.tree.focus_set()
        cur = self.tree.focus()
        target = cur if cur else kids[0]
        self.tree.selection_set(target)
        self.tree.focus(target)
        self.tree.see(target)
        return "break"

    def _on_row_open_key(self, event=None):
        """Enter on the focused row → open the student AND load their notes
        into the viewer (the open IS the navigation the fetch needs).
        Double-click stays open-only for a quick switch without notes."""
        iid = self.tree.focus()
        query, label = self._query_label_for_iid(iid)
        if query:
            self.tree.selection_set(iid)
            self.review_notes(query, label)
        return "break"

    def _on_row_context_menu(self, event=None) -> None:
        """Right-click a row → action menu (posted at the cursor)."""
        iid = self.tree.identify_row(event.y) if event is not None else \
            self.tree.focus()
        if not iid:
            return
        self.tree.selection_set(iid)
        self.tree.focus(iid)
        self._show_row_menu(iid, event.x_root, event.y_root)

    def _on_row_menu_key(self, event=None):
        """Shift+F10 / Menu key → action menu for the focused row,
        posted just below that row."""
        iid = self.tree.focus()
        if not iid:
            kids = self.tree.get_children()
            if not kids:
                return "break"
            iid = kids[0]
            self.tree.selection_set(iid)
            self.tree.focus(iid)
        bbox = self.tree.bbox(iid)
        if bbox:
            x = self.tree.winfo_rootx() + bbox[0] + 24
            y = self.tree.winfo_rooty() + bbox[1] + bbox[3]
        else:
            x = self.tree.winfo_rootx() + 30
            y = self.tree.winfo_rooty() + 30
        self._show_row_menu(iid, x, y)
        return "break"

    def _show_row_menu(self, iid, x_root, y_root) -> None:
        """Build + post the per-row action menu: Open / Open in new tab /
        Fire scenario ▸ (this row) / Fire on N selected ▸ (the checked
        rows, when any). Curated to the panel-action scenarios."""
        query, label = self._query_label_for_iid(iid)
        row = self._row_by_iid.get(iid)
        if not query or not row:
            return
        menu = tk.Menu(self.tree, tearoff=0)
        menu.add_command(
            label="Open (switch tab)",
            command=lambda: self.app._find_student_by_query(query))
        menu.add_command(
            label="Open in new tab",
            command=lambda: self.app._find_student_by_query(
                query, new_tab=True))
        menu.add_command(
            label="Review notes",
            command=lambda q=query, l=label: self.review_notes(q, l))
        nonbatch = self._panel_action_scenarios()
        if nonbatch:
            menu.add_separator()
            # Fire on THIS row (single-student mini-batch → same previewer).
            fire_menu = tk.Menu(menu, tearoff=0)
            for sc in nonbatch:
                fire_menu.add_command(
                    label=sc.name,
                    command=lambda s=sc, r=row: self.app._fire_on_selected(
                        s, [r], near=(x_root, y_root)))
            menu.add_cascade(label="Fire action", menu=fire_menu)
            # Fire on the whole checked selection, when there is one.
            checked = self._checked_rows()
            if checked:
                sel_menu = tk.Menu(menu, tearoff=0)
                for sc in nonbatch:
                    sel_menu.add_command(
                        label=sc.name,
                        command=lambda s=sc: self.app._fire_on_selected(
                            s, self._checked_rows(), near=(x_root, y_root)))
                menu.add_cascade(
                    label=f"Fire on {len(checked)} selected", menu=sel_menu)
        try:
            menu.tk_popup(x_root, y_root)
        finally:
            menu.grab_release()

    def update_freshness(self, count: Optional[int] = None,
                         total: Optional[int] = None) -> None:
        mt = caseload_csv.csv_mtime(CASELOAD_CSV_PATH)
        age = caseload_csv.csv_age_human(CASELOAD_CSV_PATH) if mt else "no CSV"
        if count is not None and total is not None and count != total:
            txt = f"{count}/{total} shown · {age}"
        elif total is not None:
            txt = f"{total} rows · {age}"
        else:
            txt = age
        # Essential Actions freshness (scraped separately from the dashboard).
        ea_mt = getattr(self.app, "_ea_mtime", None)
        if ea_mt is not None:
            ea_mins = (datetime.now() - ea_mt).total_seconds() / 60
            ea_age = ("just now" if ea_mins < 1
                      else f"{int(ea_mins)} min ago" if ea_mins < 60
                      else f"{int(ea_mins // 60)} hr ago")
            txt += f"  ·  EAs {ea_age}"
        else:
            txt += "  ·  EAs not loaded"
        color = ("gray40", "gray65")
        if mt is not None:
            mins = (datetime.now() - mt).total_seconds() / 60
            color = (("#2e7d32", "#7ee787") if mins < 30
                     else ("#9a6700", "#ffd166") if mins < 120
                     else ("#b00020", "#ff7b72"))
        self.freshness_lbl.configure(text=txt, text_color=color)


# ============================================================
# Main app
# ============================================================

class App:
    def __init__(self) -> None:
        ctk.set_appearance_mode("System")
        ctk.set_default_color_theme("blue")

        self.scenarios = load_scenarios()
        # User-defined scenario groupings — see Group dataclass.
        # Empty list means every scenario renders as ungrouped.
        self.groups: list[Group] = load_groups()
        # User preferences (advanced/dev mode toggle + future settings).
        # Loaded once at startup; saved via the Settings dialog when
        # the user toggles. Default state hides advanced features.
        self.settings: Settings = load_settings()
        self._sync_name_cap_mode()  # share the name-casing pref with builders
        # Overall UI scale (CustomTkinter widget scaling) — apply before
        # widgets are built.
        try:
            scale = float(getattr(self.settings, "ui_scale", 1.0) or 1.0)
            ctk.set_widget_scaling(max(0.8, min(1.6, scale)))
        except Exception:
            pass
        # Seed each per-area text size BEFORE widgets are built so every
        # box registers at the right size; wire persistence so live Ctrl
        # +/- changes are remembered.
        for _ch in UI_FONT_CHANNELS:
            _saved = int(getattr(self.settings, f"font_{_ch}", 0) or 0)
            if _saved:
                set_font_size(_ch, _saved, persist=False)
        _FONT_PERSIST[0] = self._persist_font_size
        # Re-render the live note viewer when its text size changes.
        if not _notes_font_registered[0]:
            register_font_apply("notes", self._reapply_notes_font)
            _notes_font_registered[0] = True
        # Point at the saved email-templates folder (if the user picked a
        # non-default one and it still exists).
        _tpl = (self.settings.email_templates_dir or "").strip()
        if _tpl and Path(_tpl).is_dir():
            set_templates_dir(_tpl)

        # In-memory caseload cache populated from CASELOAD_CSV_PATH.
        # Set by _reload_caseload_cache() (called on startup and via
        # the Reload button). When None, batches fall back to the
        # DOM-scroll scrape — slower but always works.
        self._caseload_rows: Optional[list[dict]] = None
        self._caseload_csv_mtime = None
        # Checked row keys for the caseload panel's multi-select. Held on
        # the app (not the panel) so the selection survives the panel
        # rebuild that pop-out / re-dock performs.
        self._caseload_checked_ids: set[str] = set()
        # Topmost scrim shown over the browser during a run (see
        # _show_browser_lock); None when not locked.
        self._lock_overlay = None
        # Updated by _reload_caseload_cache; True iff the cached CSV
        # carries a student-email column the launcher recognizes.
        # Drives the Settings status line + pre-batch warning when
        # the Caseload Tool view hasn't been set up yet.
        self._csv_has_student_email: bool = False
        # Session latch: "Don't ask this session" choice from the
        # pre-batch CSV-email warning. Reset every restart.
        self._csv_email_warning_skipped: bool = False
        # Latch for the per-session CSV-email-columns diagnostic. We
        # log the helpful "your CSV doesn't have an email column we
        # recognize, here's what IS there" message once per session
        # rather than on every student in a batch.
        self._email_diag_logged = False
        # Summary from the most recent history snapshot (status + departures);
        # read by the ⚑ Departures button. None until the first reload runs.
        self._last_history_summary = None
        self._reload_caseload_cache(silent=True)

        # Busy-state guard so the user can't fire a second action
        # while one is in flight (auto-refresh, manual refresh, a
        # scenario, or a batch). Toggled by _set_busy / _set_idle.
        self._is_busy = False
        self._busy_message = ""
        self._busy_spinner_index = 0

        self.root = ctk.CTk()
        self.root.title(f"Caseload Note Automation — v{__version__}")
        self.root.minsize(420, 520)
        # Restore the saved window size/position if it's still sane and
        # on-screen; otherwise fall back to the default.
        self._restore_window_geometry()
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(0, weight=1)

        # Horizontal split between the main (left) pane and the editor
        # (right) pane, joined by a draggable vertical sash line.
        msash_bg = "#555555" if ctk.get_appearance_mode() == "Dark" else "#a0a0a0"
        self.main_paned = tk.PanedWindow(
            self.root, orient="horizontal", bd=0,
            sashwidth=4, sashrelief="flat", bg=msash_bg,
        )
        # The 8px inset goes on the PanedWindow (shows the theme bg);
        # panes themselves add with no padding so the only gray the
        # PanedWindow paints is the thin sash line between them.
        self.main_paned.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)

        # The scenario editor is a focused MODE you enter on demand: it
        # starts hidden, and opening it hides the main fire pane (keeping
        # the caseload panel). Editing and firing are separate tasks.
        self._editor_visible = False
        # Per-(course,scenario) tabs in the log area + flat list of all
        # entries for the persistent CSV. Initialized before building
        # the UI so the tabview helpers can use them.
        self.note_log_entries: list[NoteLogEntry] = []
        self.note_tabs: dict[str, dict] = {}  # tab_key -> {frame, list_frame}
        self.caseload_panel: Optional[CaseloadPanel] = None
        self._build_main_pane()
        self._build_editor_pane()
        self._build_caseload_pane()

        # Place the dividers at their saved spots once the panes have a
        # real laid-out width.
        self.root.after(0, self._restore_main_sash)

        self.worker = BrowserWorker(
            on_status=self._post_status,
            on_note_filed=self._post_note_filed,
            on_multiple_matches=self._post_multiple_matches,
        )
        self.worker.start()

        self.hotkey_listener: Optional[keyboard.Listener] = None
        self._hotkeys: list[keyboard.HotKey] = []
        self._suppress_vks: set[int] = set()
        self._start_hotkeys()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Apply the saved advanced/basic mode preference once the UI
        # has finished its first layout pass. Deferred via after(0)
        # so toolbar geometry events have flushed before we start
        # pack_forget-ing the Capture button.
        self.root.after(0, self._apply_advanced_mode)

        # First-run popup: if the user hasn't been through the
        # welcome flow yet, pop it after the main UI has settled.
        # Deferred so the launcher window is visible behind it
        # (popping before the main window paints makes the dialog
        # look orphaned).
        if not self.settings.first_run_complete:
            # First-time view is uncluttered: editor already starts hidden;
            # also collapse the viewer (they set up the caseload first).
            self.root.after(0, self._hide_viewer_initially)
            # Size the window to comfortably show the sample groups +
            # toolbar + a square-ish log (runs after the viewer hides and
            # advanced-mode trims the toolbar, so the measurement is right).
            self.root.after(0, self._apply_first_run_geometry)
            self.root.after(400, self._show_first_run_setup)

        # Once the worker has the browser open, auto-refresh the
        # caseload CSV in the background so the first batch fire is
        # instant. Failures log a hint but never block startup.
        self.root.after(500, self._poll_worker_then_auto_download)

    # ----- Main (left) pane -----

    def _build_main_pane(self) -> None:
        pane = ctk.CTkFrame(self.main_paned)
        pane.grid_columnconfigure(0, weight=1)
        self.main_pane = pane
        # stretch="never": the main pane keeps its width when the window is
        # resized — extra horizontal space goes to the caseload viewer.
        self.main_paned.add(pane, minsize=260, stretch="never")

        # Status + busy/refresh notification, pinned to the TOP so the
        # caseload-refresh / run status is always visible up top.
        self.status_var = ctk.StringVar(value="Launching browser...")
        topbar = ctk.CTkFrame(pane, fg_color="transparent")
        topbar.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
        topbar.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            topbar, textvariable=self.status_var,
            font=ctk.CTkFont(size=13), anchor="w",
        ).grid(row=0, column=0, sticky="ew")
        self.busy_label = ctk.CTkLabel(
            topbar, text="", anchor="e", justify="right",
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color="transparent",
            text_color=("#7a4f00", "#ffd166"), corner_radius=6,
        )
        self.busy_label.grid(row=0, column=1, sticky="e", padx=(8, 0))

        # Find student — searches the in-DOM Caseload table.
        # HIDDEN from view (2026-05-31): the caseload panel's own search
        # box has superseded this. We keep the widgets + wiring intact
        # (grid_remove retains the layout) so it can be restored with a
        # single `self._find_frame.grid()` if we decide to bring it back;
        # the final remove-or-keep decision is deferred.
        find_frame = ctk.CTkFrame(pane)
        find_frame.grid(row=1, column=0, sticky="ew", padx=8, pady=4)
        find_frame.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(find_frame, text="Find student:").grid(row=0, column=0, padx=8, pady=8)
        self.search_var = ctk.StringVar()
        search_entry = ctk.CTkEntry(
            find_frame, textvariable=self.search_var,
            placeholder_text="name, email, or Student ID",
        )
        search_entry.grid(row=0, column=1, sticky="ew", padx=(0, 8), pady=8)
        search_entry.bind("<Return>", lambda _e: self._find_student())
        ctk.CTkButton(
            find_frame, text="Find", width=70, command=self._find_student,
        ).grid(row=0, column=2, padx=(0, 8), pady=8)
        self._find_frame = find_frame
        find_frame.grid_remove()  # hidden; code retained intentionally

        # Course-code override moved to per-note in v0.4.x — each
        # NoteEditor carries its own "Override course code" field, so
        # a scenario filing notes against multiple courses can pin
        # each note independently. `course_var` is kept around as an
        # empty StringVar so any leftover call sites (fire flow) read
        # "" and fall through to auto-detect or the per-note override.
        self.course_var = ctk.StringVar(value="")

        # Scenario buttons
        self.button_frame = ctk.CTkFrame(pane)
        self.button_frame.grid(row=3, column=0, sticky="ew", padx=8, pady=4)
        self.scenario_buttons: dict[str, ctk.CTkButton] = {}
        self._rebuild_scenario_buttons()

        # Editor toggle row (also hosts the caseload-cache refresh).
        toggle_frame = ctk.CTkFrame(pane, fg_color="transparent")
        toggle_frame.grid(row=4, column=0, sticky="ew", padx=8, pady=(4, 0))
        self.editor_toggle_btn = ctk.CTkButton(
            toggle_frame, text="✎ Edit actions", width=140,
            command=self._toggle_editor, **SECONDARY_BTN_KWARGS,
        )
        self.editor_toggle_btn.pack(side="left")
        self._btn_add_group = ctk.CTkButton(
            toggle_frame, text="+ Add group", width=110,
            command=self._add_group, **SECONDARY_BTN_KWARGS,
        )
        self._btn_add_group.pack(side="left", padx=(8, 0))
        self.caseload_toggle_btn = ctk.CTkButton(
            toggle_frame, text="Hide viewer", width=120,
            command=self._toggle_caseload, **SECONDARY_BTN_KWARGS,
        )
        self.caseload_toggle_btn.pack(side="left", padx=(8, 0))
        self.caseload_refresh_btn = ctk.CTkButton(
            toggle_frame, text="↻ Caseload",
            width=120, command=self._on_caseload_refresh_clicked,
            **SECONDARY_BTN_KWARGS,
        )
        self.caseload_refresh_btn.pack(side="left", padx=(8, 0))
        # Settings button — opens a small modal for user preferences.
        # Currently just the advanced-mode toggle; designed to grow.
        self._btn_settings = ctk.CTkButton(
            toggle_frame, text="⚙ Settings",
            width=110, command=self._open_settings,
            **SECONDARY_BTN_KWARGS,
        )
        self._btn_settings.pack(side="left", padx=(8, 0))
        # Discovery: capture Salesforce's note-submission network
        # traffic so we can later replay it via REST API instead of
        # driving the UI. One-click toggle; on stop, writes the
        # captured requests to a JSON file in the user config dir.
        # Advanced-only: hidden in basic mode via _apply_advanced_mode.
        self._capture_active = False
        self.capture_btn = ctk.CTkButton(
            toggle_frame, text="🔬 Capture",
            width=100, command=self._on_capture_toggle,
            **SECONDARY_BTN_KWARGS,
        )
        self.capture_btn.pack(side="left", padx=(8, 0))
        # TEMP dev probe (remove after texting exploration): capture the live
        # Cadence "Send Text" composer DOM so we can build the texting send
        # selectors. Open the Cadence text composer for a student first
        # (template/schedule pickers too, if you want them captured), then click.
        self._btn_probe_text = ctk.CTkButton(
            toggle_frame, text="🧪 Probe Text",
            width=120, command=self._dev_probe_text,
            **SECONDARY_BTN_KWARGS,
        )
        self._btn_probe_text.pack(side="left", padx=(8, 0))
        # TEMP dev helper (remove with the text probe): open Mongoose in the
        # launcher's OWN browser context so the probe can see it. Click this,
        # navigate to your inbox + compose view, then click Probe Text.
        self._btn_open_mongoose = ctk.CTkButton(
            toggle_frame, text="🐭 Open Mongoose",
            width=140, command=self._dev_open_mongoose,
            **SECONDARY_BTN_KWARGS,
        )
        self._btn_open_mongoose.pack(side="left", padx=(8, 0))
        # TEMP dev helper (remove after texting verification): drive the Mongoose
        # compose modal for one test recipient (commit=False — stops at the
        # confirm step for review). Click 🐭 Open Mongoose first.
        self._btn_test_text = ctk.CTkButton(
            toggle_frame, text="🧪 Test Text",
            width=120, command=self._dev_test_text,
            **SECONDARY_BTN_KWARGS,
        )
        self._btn_test_text.pack(side="left", padx=(8, 0))
        # (Busy/refresh indicator now lives in the top bar — see topbar.)
        # Collapse the rightmost toolbar buttons to emoji-only when the
        # bar gets too narrow (they're the first to clip).
        self._toggle_row_frame = toggle_frame
        self._toggle_row_mode = None
        toggle_frame.bind("<Configure>", self._relayout_toggle_row)

        # Activity-log header: collapse toggle + Copy-to-clipboard. The
        # log box (row 6) can be collapsed to reclaim vertical space.
        self._log_pane = pane
        self._log_visible = True
        log_header = ctk.CTkFrame(pane, fg_color="transparent")
        log_header.grid(row=5, column=0, sticky="ew", padx=8, pady=(8, 0))
        self._log_collapse_btn = ctk.CTkButton(
            log_header, text="▼ Activity log", width=130, anchor="w",
            command=self._toggle_log, **SECONDARY_BTN_KWARGS,
        )
        self._log_collapse_btn.pack(side="left")
        self._log_copy_btn = ctk.CTkButton(
            log_header, text="📋 Copy", width=80, command=self._copy_log,
            **SECONDARY_BTN_KWARGS,
        )
        self._log_copy_btn.pack(side="right")

        # Activity + per-note-type tabs.
        self.log_tabview = ctk.CTkTabview(pane)
        self.log_tabview.grid(row=6, column=0, sticky="nsew", padx=8, pady=(2, 0))
        activity_tab = self.log_tabview.add("Activity")
        activity_tab.grid_columnconfigure(0, weight=1)
        activity_tab.grid_rowconfigure(0, weight=1)
        self.log = ctk.CTkTextbox(activity_tab, wrap="word")
        self.log.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
        self.log.configure(state="disabled")
        # Red highlight for failure lines (see _append_log).
        try:
            self.log._textbox.tag_configure("logerror", foreground="#e0524f")
        except Exception:
            pass
        register_font_box("activity", self.log)  # Ctrl +/-, Ctrl+wheel
        pane.grid_rowconfigure(6, weight=1)

        # Bottom row. Quit is packed first (side=right) so it always
        # reserves its slot and stays visible; the other two collapse to
        # short labels when the row is too narrow.
        bottom = ctk.CTkFrame(pane, fg_color="transparent")
        bottom.grid(row=7, column=0, sticky="ew", padx=8, pady=(0, 8))
        ctk.CTkButton(
            bottom, text="Quit", width=80, command=self._on_close,
        ).pack(side="right")
        self._btn_hide_taskbar = ctk.CTkButton(
            bottom, text="Hide to taskbar", width=120, command=self._hide,
        )
        self._btn_hide_taskbar.pack(side="left")
        self._btn_open_log = ctk.CTkButton(
            bottom, text="Open log", width=90, command=self._open_log_file,
        )
        self._btn_open_log.pack(side="left", padx=(8, 0))
        self._bottom_row_frame = bottom
        self._bottom_row_mode = None
        bottom.bind("<Configure>", self._relayout_bottom_row)

    def _relayout_toggle_row(self, event=None) -> None:
        """Collapse the rightmost toolbar buttons (Templates/Settings/
        Capture) to emoji-only when the bar is too narrow, full labels
        when there's room. Hide-editor and Caseload (leftmost) keep
        their labels."""
        try:
            w = (event.width if event is not None
                 else self._toggle_row_frame.winfo_width())
        except Exception:
            return
        if w <= 1:
            return
        mode = "wide" if w >= 540 else "narrow"
        if mode == self._toggle_row_mode:
            return
        self._toggle_row_mode = mode
        if mode == "wide":
            self._btn_settings.configure(text="⚙ Settings", width=110)
            self.capture_btn.configure(text="🔬 Capture", width=100)
        else:
            self._btn_settings.configure(text="⚙", width=40)
            self.capture_btn.configure(text="🔬", width=40)

    def _relayout_bottom_row(self, event=None) -> None:
        """Shrink 'Hide to taskbar'/'Open log' to short labels when the
        bottom row is narrow so the (right-pinned) Quit button always
        stays visible."""
        try:
            w = (event.width if event is not None
                 else self._bottom_row_frame.winfo_width())
        except Exception:
            return
        if w <= 1:
            return
        mode = "wide" if w >= 320 else "narrow"
        if mode == self._bottom_row_mode:
            return
        self._bottom_row_mode = mode
        if mode == "wide":
            self._btn_hide_taskbar.configure(text="Hide to taskbar", width=120)
            self._btn_open_log.configure(text="Open log", width=90)
        else:
            self._btn_hide_taskbar.configure(text="Hide", width=60)
            self._btn_open_log.configure(text="Log", width=50)

    def _rebuild_scenario_buttons(self) -> None:
        """Render the scenario button list. Layout depends on whether
        the user has defined any groups:

        - No groups: flat 2-column grid (legacy behavior).
        - With groups: Ungrouped section at top (only if non-empty),
          followed by each group as a collapsible color-coded
          section. Plus "+ Add group" button at the bottom."""
        for w in self.button_frame.winfo_children():
            w.destroy()
        self.scenario_buttons.clear()
        self.button_frame.grid_columnconfigure(0, weight=1)
        self.button_frame.grid_columnconfigure(1, weight=1)

        # Track collapse state per group across rebuilds.
        if not hasattr(self, "_group_collapsed"):
            self._group_collapsed: dict[str, bool] = {}

        def _scenario_btn(parent, name: str, sc: ScenarioConfig,
                          color: Optional[str] = None) -> ctk.CTkButton:
            label = name + (f"  ({sc.hotkey})" if sc.hotkey else "")
            kwargs: dict = dict(
                text=label, command=lambda s=sc: self._fire(s),
                width=160, height=36,
            )
            if color:
                kwargs["fg_color"] = color
                kwargs["text_color"] = _text_color_for_bg(color)
                kwargs["hover_color"] = _hover_color_for(color)
            btn = ctk.CTkButton(parent, **kwargs)
            self.scenario_buttons[name] = btn
            return btn

        # No groups → flat grid (original behavior preserved), still
        # offering "+ Add group" so the first group can be created.
        if not self.groups:
            scenario_items = list(self.scenarios.items())
            for i, (name, sc) in enumerate(scenario_items):
                btn = _scenario_btn(self.button_frame, name, sc)
                btn.grid(row=i // 2, column=i % 2, padx=6, pady=6, sticky="ew")
            return

        # With groups → sectioned layout.
        row = 0
        grouped_names: set[str] = set()
        for g in self.groups:
            grouped_names.update(s for s in g.scenarios if s in self.scenarios)
        ungrouped = [n for n in self.scenarios if n not in grouped_names]

        if ungrouped:
            ctk.CTkLabel(
                self.button_frame, text="Ungrouped",
                font=ctk.CTkFont(size=11, weight="bold"),
                text_color=("gray40", "gray70"),
                anchor="w",
            ).grid(row=row, column=0, columnspan=2,
                   sticky="ew", padx=6, pady=(4, 2))
            row += 1
            for i, name in enumerate(ungrouped):
                btn = _scenario_btn(self.button_frame, name, self.scenarios[name])
                btn.grid(row=row + i // 2, column=i % 2,
                         padx=6, pady=4, sticky="ew")
            row += (len(ungrouped) + 1) // 2

        for group in self.groups:
            collapsed = self._group_collapsed.get(group.name, False)
            # Each group is a box outlined in its own color. The header
            # mirrors the note-editor dropdown (transparent fill, bold
            # text, ▼/▶ arrow) so it reads as a section title rather
            # than another scenario button; member buttons sit indented
            # inside the box.
            box = ctk.CTkFrame(
                self.button_frame, fg_color="transparent",
                border_width=2, border_color=group.color, corner_radius=8,
            )
            box.grid(row=row, column=0, columnspan=2,
                     sticky="ew", padx=4, pady=(8, 2))
            box.grid_columnconfigure(0, weight=1)
            box.grid_columnconfigure(1, weight=1)
            row += 1

            header = ctk.CTkFrame(box, fg_color="transparent")
            header.grid(row=0, column=0, columnspan=2,
                        sticky="ew", padx=6, pady=(4, 2))
            header.grid_columnconfigure(0, weight=1)
            arrow = "▶" if collapsed else "▼"
            ctk.CTkButton(
                header, text=f"{arrow}  {group.name}",
                anchor="w", height=28,
                fg_color="transparent",
                text_color=("gray10", "gray90"),
                hover_color=("gray85", "gray25"),
                font=ctk.CTkFont(size=13, weight="bold"),
                command=lambda gn=group.name: self._toggle_group(gn),
            ).grid(row=0, column=0, sticky="ew")
            # '+' adds a new action directly into this group.
            ctk.CTkButton(
                header, text="+", width=32, height=28,
                command=lambda gn=group.name: self._new_scenario_in_group(gn),
                **SECONDARY_BTN_KWARGS,
            ).grid(row=0, column=1, padx=(4, 0))
            ctk.CTkButton(
                header, text="⚙", width=32, height=28,
                command=lambda g=group: self._edit_group(g),
                **SECONDARY_BTN_KWARGS,
            ).grid(row=0, column=2, padx=(4, 0))
            if collapsed:
                continue
            valid = [s for s in group.scenarios if s in self.scenarios]
            for i, name in enumerate(valid):
                btn = _scenario_btn(
                    box, name, self.scenarios[name], color=group.color,
                )
                # Extra left/right inset so buttons read as indented
                # children of the group rather than full-width rows.
                btn.grid(row=1 + i // 2, column=i % 2,
                         padx=((14, 6) if i % 2 == 0 else (6, 14)),
                         pady=4, sticky="ew")
            # Trailing inner pad so the last row doesn't touch the border.
            ctk.CTkFrame(box, fg_color="transparent", height=4).grid(
                row=1 + (len(valid) + 1) // 2, column=0, columnspan=2)
        # ("+ Add group" now lives in the Edit-actions toolbar row.)

    def _toggle_group(self, group_name: str) -> None:
        """Flip a group's collapsed flag and re-render the button
        list. Collapse state is per-session (not persisted)."""
        if not hasattr(self, "_group_collapsed"):
            self._group_collapsed = {}
        self._group_collapsed[group_name] = (
            not self._group_collapsed.get(group_name, False)
        )
        self._rebuild_scenario_buttons()

    def _add_group(self) -> None:
        """Open the group dialog with empty fields. On Save, appends
        the new group to self.groups, persists via _save_yaml, and
        rebuilds the scenario button list."""
        self._open_group_dialog(group=None)

    def _edit_group(self, group: Group) -> None:
        """Open the group dialog seeded with `group`'s current
        values. On Save, mutates the existing group entry in place.
        Delete unparents the group's scenarios (they revert to
        ungrouped) and removes the group from self.groups."""
        self._open_group_dialog(group=group)

    def _open_group_dialog(self, group: Optional[Group]) -> None:
        """Modal for creating or editing a group. Fields: name,
        color (palette + custom hex), scenarios (checkbox per
        scenario, all checked by default for new groups). On Save:
        - For a new group: appended to self.groups, persists.
        - For an existing group: mutated in place, persists.
        Delete (only shown when editing) unparents the group's
        scenarios and removes the group itself."""
        is_new = group is None
        dialog = ctk.CTkToplevel(self.root)
        dialog.title("New group" if is_new else f"Edit group — {group.name}")
        dialog.transient(self.root)
        dialog.attributes("-topmost", True)
        dialog.grab_set()
        dialog.geometry("520x640")
        dialog.minsize(480, 520)
        dialog.resizable(True, True)
        dialog.lift()
        dialog.focus_force()

        ctk.CTkLabel(
            dialog,
            text="New group" if is_new else "Edit group",
            font=ctk.CTkFont(size=15, weight="bold"),
            anchor="w",
        ).pack(fill="x", padx=20, pady=(16, 8))

        # --- Name field.
        ctk.CTkLabel(
            dialog, text="Name", font=ctk.CTkFont(size=12, weight="bold"),
            anchor="w",
        ).pack(fill="x", padx=20, pady=(4, 0))
        name_entry = ctk.CTkEntry(
            dialog, placeholder_text="e.g. Welcome emails", width=320,
        )
        name_entry.pack(fill="x", padx=20, pady=(2, 8))
        if not is_new:
            name_entry.insert(0, group.name)

        # --- Color picker: palette + custom hex.
        ctk.CTkLabel(
            dialog, text="Color", font=ctk.CTkFont(size=12, weight="bold"),
            anchor="w",
        ).pack(fill="x", padx=20, pady=(4, 0))
        color_state = {"value": group.color if not is_new else GROUP_COLOR_PALETTE[0][1]}

        palette_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        palette_frame.pack(fill="x", padx=20, pady=(2, 4))
        palette_swatches: list[ctk.CTkButton] = []

        def _refresh_swatches() -> None:
            for sw, (label, hex_c) in zip(palette_swatches, GROUP_COLOR_PALETTE):
                if hex_c.lower() == color_state["value"].lower():
                    sw.configure(border_width=3, border_color=("gray10", "gray95"))
                else:
                    sw.configure(border_width=1, border_color=("gray60", "gray40"))

        def _pick_color(hex_c: str) -> None:
            color_state["value"] = hex_c
            _refresh_swatches()
            _update_preview()

        for i, (label, hex_c) in enumerate(GROUP_COLOR_PALETTE):
            sw = ctk.CTkButton(
                palette_frame, text="", width=36, height=24,
                fg_color=hex_c, hover_color=_hover_color_for(hex_c),
                border_width=1, border_color=("gray60", "gray40"),
                command=lambda c=hex_c: _pick_color(c),
            )
            sw.grid(row=i // 6, column=i % 6, padx=2, pady=2, sticky="w")
            palette_swatches.append(sw)

        # Custom hex entry for power users.
        custom_row = ctk.CTkFrame(dialog, fg_color="transparent")
        custom_row.pack(fill="x", padx=20, pady=(2, 8))
        ctk.CTkLabel(
            custom_row, text="Custom hex:", anchor="w",
            font=ctk.CTkFont(size=11),
            text_color=("gray35", "gray70"),
        ).pack(side="left")
        custom_entry = ctk.CTkEntry(
            custom_row, placeholder_text="#rrggbb", width=100,
        )
        custom_entry.pack(side="left", padx=(8, 4))

        def _apply_custom() -> None:
            v = (custom_entry.get() or "").strip()
            if not v.startswith("#"):
                v = "#" + v
            # Quick validity check
            h = v.lstrip("#")
            if len(h) == 3:
                h = "".join(c + c for c in h)
            if len(h) != 6:
                custom_entry.configure(border_color=("#cc0000", "#cc6666"))
                return
            try:
                int(h, 16)
            except ValueError:
                custom_entry.configure(border_color=("#cc0000", "#cc6666"))
                return
            custom_entry.configure(border_color=("gray60", "gray40"))
            color_state["value"] = "#" + h.lower()
            _refresh_swatches()
            _update_preview()

        ctk.CTkButton(
            custom_row, text="Apply", width=70, height=28,
            command=_apply_custom, **SECONDARY_BTN_KWARGS,
        ).pack(side="left", padx=(0, 4))

        # Live preview: a fake scenario button using the current color.
        preview_label = ctk.CTkLabel(
            dialog, text="Preview", font=ctk.CTkFont(size=11),
            text_color=("gray35", "gray70"),
            anchor="w",
        )
        preview_label.pack(fill="x", padx=20, pady=(8, 2))
        preview_btn = ctk.CTkButton(
            dialog, text="example action  (F3)",
            width=200, height=36, state="disabled",
        )
        preview_btn.pack(anchor="w", padx=20, pady=(0, 8))

        def _update_preview() -> None:
            c = color_state["value"]
            try:
                preview_btn.configure(
                    fg_color=c,
                    text_color=_text_color_for_bg(c),
                    text_color_disabled=_text_color_for_bg(c),
                )
            except Exception:
                pass

        _refresh_swatches()
        _update_preview()

        # --- Scenarios checkbox list (label here; scroll frame packed
        # below, AFTER the bottom button bar so the buttons always win
        # the space contest and stay visible even when cramped).
        ctk.CTkLabel(
            dialog, text="Actions in this group",
            font=ctk.CTkFont(size=12, weight="bold"),
            anchor="w",
        ).pack(fill="x", padx=20, pady=(8, 0))
        # Show which group each scenario is currently in (if any).
        current_membership: dict[str, str] = {}
        for g in self.groups:
            if not is_new and g is group:
                continue
            for s in g.scenarios:
                current_membership[s] = g.name
        scenario_vars: dict[str, ctk.BooleanVar] = {}
        # When deleting, optionally delete the group's actions too (rather
        # than leaving them ungrouped). Off by default — destructive.
        delete_actions_var = ctk.BooleanVar(value=False)

        # --- Buttons row + hint, pinned to the bottom FIRST. Packing
        # these side="bottom" before the expanding scroll frame
        # reserves their space, so they never get pushed off-screen.
        # (Callbacks read scenario_vars, which is populated further
        # below before any button can be clicked — closures by ref.)
        btn_row = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_row.pack(fill="x", padx=20, pady=(4, 16), side="bottom")

        def _do_save() -> None:
            new_name = (name_entry.get() or "").strip()
            if not new_name:
                self._append_log("Group save aborted: name is required.")
                return
            # Reject duplicate names (except when editing the same one).
            for g in self.groups:
                if g is group:
                    continue
                if g.name.lower() == new_name.lower():
                    self._append_log(
                        f"Group save aborted: name {new_name!r} already exists."
                    )
                    return
            picked = [n for n, v in scenario_vars.items() if v.get()]
            # Unparent any newly-checked scenario from its current group.
            for g in self.groups:
                if not is_new and g is group:
                    continue
                g.scenarios = [s for s in g.scenarios if s not in picked]
            if is_new:
                self.groups.append(Group(
                    name=new_name,
                    color=color_state["value"],
                    scenarios=picked,
                ))
            else:
                group.name = new_name
                group.color = color_state["value"]
                group.scenarios = picked
            self._save_yaml()
            try: dialog.grab_release()
            except Exception: pass
            try: dialog.destroy()
            except Exception: pass

        def _do_delete() -> None:
            if is_new or group is None:
                return
            also_actions = bool(delete_actions_var.get())
            member_actions = [s for s in group.scenarios if s in self.scenarios]
            # Destructive when also deleting actions — confirm with a count.
            if also_actions and member_actions:
                if not ask_yes_no_topmost(
                    self.root, "Delete group and its actions?",
                    f"Delete group {group.name!r} AND its "
                    f"{len(member_actions)} action(s)?\n\n"
                    + ", ".join(member_actions)
                    + "\n\nThis can't be undone.",
                    yes_label="Delete all", no_label="Cancel",
                ):
                    return
            if also_actions:
                for s in member_actions:
                    self.scenarios.pop(s, None)
            # Remove the group itself. Without "delete actions", its
            # scenarios simply become ungrouped.
            try:
                self.groups.remove(group)
            except ValueError:
                pass
            if also_actions:
                # Drop the deleted actions' editors before _save_yaml
                # serializes from the editor set.
                self._rebuild_editor_tabs()
            self._save_yaml()
            if also_actions and member_actions:
                self._append_log(
                    f"Deleted group {group.name!r} and "
                    f"{len(member_actions)} action(s).")
            try: dialog.grab_release()
            except Exception: pass
            try: dialog.destroy()
            except Exception: pass

        def _do_cancel() -> None:
            try: dialog.grab_release()
            except Exception: pass
            try: dialog.destroy()
            except Exception: pass

        ctk.CTkButton(
            btn_row, text="Save", width=100, command=_do_save,
        ).pack(side="left", padx=4)
        ctk.CTkButton(
            btn_row, text="Cancel", width=100, command=_do_cancel,
            **SECONDARY_BTN_KWARGS,
        ).pack(side="left", padx=4)
        if not is_new:
            ctk.CTkButton(
                btn_row, text="Delete group", width=120,
                command=_do_delete,
                fg_color=("#cc4444", "#aa3333"),
                hover_color=("#aa3333", "#882222"),
            ).pack(side="right", padx=4)
            ctk.CTkButton(
                btn_row, text="Copy group", width=110,
                command=lambda: self._copy_group(group, dialog),
                **SECONDARY_BTN_KWARGS,
            ).pack(side="right", padx=4)

        # Hint sits just above the (already bottom-pinned) button bar.
        ctk.CTkLabel(
            dialog,
            text=(
                "If an action is in another group, checking it here "
                "moves it to this one (an action can only be in one "
                "group at a time)."
            ),
            font=ctk.CTkFont(size=10, slant="italic"),
            text_color=("gray45", "gray60"),
            wraplength=460, justify="left", anchor="w",
        ).pack(fill="x", padx=20, pady=(0, 6), side="bottom")

        # "Delete actions too" option — only meaningful when editing an
        # existing group. Sits just above the hint/buttons.
        if not is_new:
            ctk.CTkCheckBox(
                dialog,
                text="When deleting: also delete this group's actions "
                     "(otherwise they become ungrouped)",
                variable=delete_actions_var,
                font=ctk.CTkFont(size=11),
            ).pack(fill="x", padx=20, pady=(0, 4), side="bottom")

        # Scrollable scenario list fills whatever space is left between
        # the top fields and the pinned hint/buttons.
        sc_frame = ctk.CTkScrollableFrame(dialog, fg_color=("gray95", "gray18"))
        sc_frame.pack(fill="both", expand=True, padx=20, pady=(2, 6))
        initial_members = set(group.scenarios) if not is_new else set()
        for name in self.scenarios:
            v = ctk.BooleanVar(value=(name in initial_members))
            scenario_vars[name] = v
            row = ctk.CTkFrame(sc_frame, fg_color="transparent")
            row.pack(fill="x", pady=1)
            ctk.CTkCheckBox(
                row, text=name, variable=v,
            ).pack(side="left")
            other_group = current_membership.get(name)
            if other_group:
                ctk.CTkLabel(
                    row, text=f"  (currently in {other_group!r})",
                    font=ctk.CTkFont(size=10, slant="italic"),
                    text_color=("gray40", "gray65"),
                ).pack(side="left", padx=(6, 0))

        dialog.bind("<Escape>", lambda _e: _do_cancel())
        dialog.protocol("WM_DELETE_WINDOW", _do_cancel)

    # ----- Editor (right) pane -----

    def _build_caseload_pane(self) -> None:
        """Add the caseload panel as the rightmost pane in the main
        horizontal split (main | editor | caseload). Built as a
        container-agnostic CaseloadPanel so it can pop out into its own
        window (and re-dock) — see _toggle_caseload_popout."""
        self.caseload_dock = ctk.CTkFrame(self.main_paned)
        self.caseload_dock.grid_columnconfigure(0, weight=1)
        self.caseload_dock.grid_rowconfigure(0, weight=1)
        self.main_paned.add(self.caseload_dock, minsize=300, stretch="always")
        self._caseload_visible = True
        self._caseload_popped = False
        self.caseload_window: Optional[ctk.CTkToplevel] = None
        self._mount_caseload_panel(self.caseload_dock, popped=False)

    def _mount_caseload_panel(self, parent, popped: bool) -> None:
        """(Re)build the caseload panel into `parent`, discarding any
        previous instance. The panel is data-driven (renders from
        self._caseload_rows), so a rebuild on dock/undock is instant —
        tkinter can't reparent a live widget, so we rebuild instead."""
        old = getattr(self, "caseload_panel", None)
        if old is not None:
            try:
                old.persist_column_state()  # keep widths/order across rebuild
            except Exception:
                pass
            try:
                old.frame.destroy()
            except Exception:
                pass
        self.caseload_panel = CaseloadPanel(parent, self, popped=popped)
        self.caseload_panel.frame.grid(row=0, column=0, sticky="nsew")
        self.caseload_panel.populate()

    def _toggle_caseload_popout(self) -> None:
        if getattr(self, "_caseload_popped", False):
            self._dock_caseload()
        else:
            self._pop_out_caseload()

    def _pop_out_caseload(self) -> None:
        """Move the caseload panel into its own resizable window (for a
        second monitor). Removes the docked pane and disables the
        Hide/Show-caseload toggle until it's re-docked."""
        # Remove the docked pane from the split if it's showing.
        if self._caseload_visible:
            try:
                self.main_paned.forget(self.caseload_dock)
            except Exception:
                pass
            self._caseload_visible = False
        win = ctk.CTkToplevel(self.root)
        win.title("Caseload")
        win.minsize(360, 320)
        geo = (self.settings.caseload_window_geometry or "").strip()
        if geo and self._geometry_is_visible(geo):
            win.geometry(geo)
        else:
            win.geometry("700x800")
        win.grid_columnconfigure(0, weight=1)
        win.grid_rowconfigure(0, weight=1)
        # Closing the pop-out re-docks rather than losing the panel.
        win.protocol("WM_DELETE_WINDOW", self._dock_caseload)
        self.caseload_window = win
        self._caseload_popped = True
        self._mount_caseload_panel(win, popped=True)
        # The dock toggle is meaningless while popped out.
        self.caseload_toggle_btn.configure(
            text="Caseload popped", state="disabled")
        win.after(80, win.lift)
        self.root.after(0, self._restore_main_sash)

    def _dock_caseload(self) -> None:
        """Tear down the pop-out window and re-dock the panel as the
        rightmost pane."""
        win = getattr(self, "caseload_window", None)
        if win is not None:
            try:
                self.settings.caseload_window_geometry = win.geometry()
                save_settings(self.settings)
            except Exception:
                pass
        # Re-add the dock pane (rightmost) and rebuild the panel into it.
        try:
            self.main_paned.add(
                self.caseload_dock, minsize=300, stretch="always")
        except Exception:
            pass
        self._caseload_visible = True
        self._caseload_popped = False
        self._mount_caseload_panel(self.caseload_dock, popped=False)
        self.caseload_window = None
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass
        self.caseload_toggle_btn.configure(
            text="Hide viewer", state="normal")
        self.root.after(0, self._restore_main_sash)

    def _toggle_caseload(self) -> None:
        """Show/hide the rightmost caseload pane. Showing it GROWS the
        window to the right by the viewer's width (so the main pane keeps
        its size); hiding it shrinks the window back."""
        DEFAULT_W = 380
        if self._caseload_visible:
            # Remember the viewer's current width to restore on re-show,
            # then shrink the window by it.
            try:
                w = self.caseload_dock.winfo_width()
                if w > 50:
                    self._caseload_last_w = w
            except Exception:
                w = getattr(self, "_caseload_last_w", DEFAULT_W)
            self.main_paned.forget(self.caseload_dock)
            self.caseload_toggle_btn.configure(text="Show viewer")
            self._caseload_visible = False
            self._grow_window_width(-int(w or DEFAULT_W))
        else:
            w = int(getattr(self, "_caseload_last_w", DEFAULT_W) or DEFAULT_W)
            # Capture the main pane's width so we can keep it fixed.
            try:
                main_w = self.main_pane.winfo_width()
            except Exception:
                main_w = 0
            self._grow_window_width(+w)
            self.main_paned.add(
                self.caseload_dock, minsize=300, stretch="always")
            self.caseload_toggle_btn.configure(text="Hide viewer")
            self._caseload_visible = True
            # Keep the main (or editor) pane at its prior width; the new
            # window width goes to the caseload viewer.
            if main_w > 80:
                try:
                    self.root.update_idletasks()
                    n_sash = len(self.main_paned.panes()) - 1
                    if n_sash >= 1:
                        self.main_paned.sash_place(n_sash - 1, main_w, 1)
                except Exception:
                    self._restore_main_sash()
            else:
                self._restore_main_sash()
            return
        self._restore_main_sash()

    def _grow_window_width(self, delta: int) -> None:
        """Widen/narrow the main window by `delta` px, clamped on-screen.
        No-op when maximized."""
        try:
            if self.root.state() == "zoomed":
                return
            geo = self.root.geometry()  # "WxH+X+Y"
            m = re.fullmatch(r"(\d+)x(\d+)([+-]\d+)([+-]\d+)", geo)
            if not m:
                return
            w, h, x, y = (int(m.group(1)), int(m.group(2)),
                          int(m.group(3)), int(m.group(4)))
            sw = self.root.winfo_screenwidth()
            new_w = max(420, min(w + delta, sw))
            # Nudge left if the wider window would run off the right edge.
            if x + new_w > sw:
                x = max(0, sw - new_w)
            self.root.geometry(f"{new_w}x{h}+{x}+{y}")
        except Exception:
            pass

    def _refresh_caseload_panel(self) -> None:
        """Repopulate the caseload panel from the current cache. Safe to
        call from any thread / before the panel exists — marshals onto
        the Tk main loop."""
        panel = getattr(self, "caseload_panel", None)
        root = getattr(self, "root", None)
        if panel is None or root is None:
            return
        try:
            root.after(0, panel.populate)
        except Exception:
            pass

    def _build_editor_pane(self) -> None:
        pane = ctk.CTkFrame(self.main_paned)
        pane.grid_columnconfigure(0, weight=1)
        # Row 0 = the master-detail PanedWindow (takes all stretch); row 1 =
        # the pinned save/Done footer (natural height, never displaced).
        pane.grid_rowconfigure(0, weight=1)
        pane.grid_rowconfigure(1, weight=0)
        self.editor_pane = pane
        # NOT added to main_paned here — the editor starts hidden and is
        # added by _toggle_editor when the user opens it (focused mode).

        # Phase 3 editor area: stacked per-group rows of tab-style
        # buttons (self.editor_tabs) above a single shared content
        # frame (self.editor_content) that swaps in the selected
        # scenario's editor. A draggable sash between them lets the
        # user resize the strip area and also visually separates the
        # two regions. Replaces the old single CTkTabview row.
        # A thin sash *line* — a visible divider, not a chunky grip.
        default_bg = self.editor_pane.cget("fg_color")
        sash_bg = "#555555" if ctk.get_appearance_mode() == "Dark" else "#a0a0a0"
        # HORIZONTAL split (master-detail): LEFT = the action picker (mirrors
        # the main grouped layout but in edit mode — clicking selects an
        # action to edit, never fires), RIGHT = the selected action's form,
        # which now gets the full window height (no more cramped top strip).
        paned = tk.PanedWindow(
            pane, orient="horizontal", bd=0,
            sashwidth=3, sashrelief="flat", bg=sash_bg,
        )
        paned.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
        self.editor_paned = paned

        tabs_holder = ctk.CTkFrame(paned, fg_color=default_bg)
        self._editor_tabs_holder = tabs_holder
        # Banner so it's unmistakable you're in edit mode, not firing.
        ctk.CTkLabel(
            tabs_holder, text="✎ Editing — pick an action",
            anchor="w", font=ctk.CTkFont(size=12, weight="bold"),
            text_color=("gray35", "gray70"),
        ).pack(fill="x", padx=6, pady=(6, 2))
        self.editor_tabs = ctk.CTkScrollableFrame(
            tabs_holder, fg_color="transparent",
        )
        self.editor_tabs.pack(fill="both", expand=True)

        self.editor_content = ctk.CTkFrame(paned, fg_color=default_bg)
        # Cap the form width for readability: column 0 holds the editor
        # (capped to ~a comfortable line length), column 1 is a flexible
        # spacer that absorbs the extra width when the window is wide /
        # maximized, so fields don't stretch across the whole screen.
        self.editor_content.grid_columnconfigure(0, weight=0)
        self.editor_content.grid_columnconfigure(1, weight=1)
        self.editor_content.grid_rowconfigure(0, weight=1)
        self.editor_content.bind("<Configure>", self._cap_editor_width)

        paned.add(tabs_holder, minsize=200, width=250, stretch="never")
        paned.add(self.editor_content, minsize=320, stretch="always")

        self.scenario_editors: dict[str, ScenarioEditor] = {}
        self._editor_tab_buttons: dict[str, tuple] = {}
        self._editor_group_collapsed: dict[str, bool] = {}
        self._current_scenario: Optional[str] = None
        self._rebuild_editor_tabs()

        # Save row — responsive. Full labels when the editor pane is
        # wide; collapses to compact icons as it narrows so the Save
        # button is never pushed off-screen. Revert is always the undo
        # glyph. See _relayout_save_row for the width thresholds.
        # The save bar is a WINDOW-LEVEL FOOTER pinned to the bottom of the
        # editor pane (row 1, weight 0), OUTSIDE the master-detail PanedWindow
        # (row 0). Keeping it out of the paned area is what guarantees Done/
        # Save stay reachable: when it lived inside editor_content (a paned
        # sub-pane) a tall scrollable form could push the whole pane's bottom
        # — and the bar with it — off the window. Spans the full editor width
        # under both the action picker and the form.
        save_frame = ctk.CTkFrame(pane, fg_color="transparent")
        save_frame.grid(row=1, column=0, sticky="ew", padx=4, pady=(2, 4))
        self._save_row_frame = save_frame
        # 3 columns keep the bar aligned UNDER THE FORM (not the whole window):
        # col 0 = left spacer (= the picker pane's width), col 1 = the buttons
        # inside a capped inner frame (so Save/Revert sit at the form's right
        # edge), col 2 = a flexible spacer that absorbs the extra width when
        # maximized. Widths are synced in _align_save_row.
        save_frame.grid_columnconfigure(0, weight=0)
        save_frame.grid_columnconfigure(1, weight=0)
        save_frame.grid_columnconfigure(2, weight=1)
        inner = ctk.CTkFrame(save_frame, fg_color="transparent")
        inner.grid(row=0, column=1, sticky="ew")
        self._save_row_inner = inner
        self._save_row_mode = None
        # Done — leave the focused editor mode and return to the main view.
        self._btn_done_editor = ctk.CTkButton(
            inner, text="✓ Done", command=self._toggle_editor,
            width=90, height=34, **SECONDARY_BTN_KWARGS,
        )
        self._btn_done_editor.pack(side="left", padx=(4, 12), pady=2)
        self._btn_new = ctk.CTkButton(
            inner, text="+ New action",
            command=self._new_scenario, width=140, height=34,
        )
        self._btn_new.pack(side="left", padx=4, pady=2)
        self._btn_delete = ctk.CTkButton(
            inner, text="Delete action",
            command=self._delete_scenario, width=140, height=34,
            **SECONDARY_BTN_KWARGS,
        )
        self._btn_delete.pack(side="left", padx=4, pady=2)
        self._btn_copy = ctk.CTkButton(
            inner, text="Copy", command=self._copy_scenario,
            width=90, height=34, **SECONDARY_BTN_KWARGS,
        )
        self._btn_copy.pack(side="left", padx=4, pady=2)
        self._btn_save = ctk.CTkButton(
            inner, text="Save",
            command=self._save_yaml, width=90, height=34,
        )
        self._btn_save.pack(side="right", padx=4, pady=2)
        self._btn_revert = ctk.CTkButton(
            inner, text="↺",
            command=self._revert_editor, width=40, height=34,
            font=ctk.CTkFont(size=18), **SECONDARY_BTN_KWARGS,
        )
        self._btn_revert.pack(side="right", padx=4, pady=2)
        inner.bind("<Configure>", self._relayout_save_row)
        # Keep the bar aligned to the form as the window / sash resizes.
        self._editor_tabs_holder.bind(
            "<Configure>", lambda e: self._align_save_row(), add="+")

    def _cap_editor_width(self, event=None) -> None:
        """Cap the editor form's width to ~a comfortable line length so the
        fields don't stretch across a maximized window. The cap scales with
        the editor font size (so it's roughly a fixed character count) and
        the extra width goes to the column-1 spacer."""
        try:
            w = (event.width if event is not None
                 else self.editor_content.winfo_width())
        except Exception:
            return
        if not w or w <= 1:
            return
        font_px = int(getattr(self.settings, "font_editor", 0) or 14)
        scale = float(getattr(self.settings, "ui_scale", 1.0) or 1.0)
        # ~110 characters wide; ~0.62 px per pt is a typical UI-font ratio.
        max_px = max(640, min(int(font_px * scale * 0.62 * 110), 1500))
        target = min(w, max_px)
        if getattr(self, "_editor_col0_w", None) == target:
            return  # avoid relayout thrash
        self._editor_col0_w = target
        try:
            self.editor_content.grid_columnconfigure(0, minsize=target)
        except Exception:
            pass
        self._align_save_row()

    def _align_save_row(self) -> None:
        """Line the save/Done footer up under the FORM: left spacer = the
        picker pane's width, the button column = the same cap as the form, so
        Save/Revert sit at the form's bottom-right corner instead of drifting
        to the window's right edge when the window is wide/maximized."""
        try:
            left = self._editor_tabs_holder.winfo_width()
            cap = getattr(self, "_editor_col0_w", None)
            if not cap or left <= 1:
                return
            # +3 ≈ the paned sash width between the picker and the form.
            self._save_row_frame.grid_columnconfigure(0, minsize=left + 3)
            self._save_row_frame.grid_columnconfigure(1, minsize=cap, weight=0)
        except Exception:
            pass

    def _relayout_save_row(self, event=None) -> None:
        """Swap the save-row buttons between full labels and compact
        icons based on the available width, so the Save button is never
        clipped. Three stages:
          wide   → '+ New scenario' / 'Delete scenario' / 'Save'
          medium → '+' / '−'        (Save keeps its label)
          narrow → '+' / '−' / '✓'  (Save becomes a check)
        Revert is always the undo glyph."""
        try:
            w = (event.width if event is not None
                 else self._save_row_inner.winfo_width())
        except Exception:
            return
        if w <= 1:
            return
        mode = "wide" if w >= 440 else ("medium" if w >= 240 else "narrow")
        if mode == self._save_row_mode:
            return
        self._save_row_mode = mode
        if mode == "wide":
            self._btn_new.configure(text="+ New action", width=140)
            self._btn_delete.configure(text="Delete action", width=140)
            self._btn_copy.configure(text="Copy", width=90)
            self._btn_save.configure(text="Save", width=90)
        elif mode == "medium":
            self._btn_new.configure(text="+", width=40)
            self._btn_delete.configure(text="−", width=40)
            self._btn_copy.configure(text="⧉", width=40)
            self._btn_save.configure(text="Save", width=90)
        else:  # narrow
            self._btn_new.configure(text="+", width=40)
            self._btn_delete.configure(text="−", width=40)
            self._btn_copy.configure(text="⧉", width=40)
            self._btn_save.configure(text="✓", width=40)

    def _build_one_editor(self, name: str, sc: ScenarioConfig) -> None:
        """Construct a single ScenarioEditor for `name` into the shared
        content frame (hidden until selected). Each editor is a large
        widget tree, so we build them one at a time and reuse them
        across rebuilds — see _rebuild_editor_tabs."""
        editor = ScenarioEditor(
            self.editor_content, sc,
            capture_handler=self._capture_hotkey,
            get_columns=self._get_caseload_columns,
            refresh_columns=self._refresh_caseload_columns_for_editor,
            get_value_suggestions=self._filter_value_suggestions,
            get_groups=lambda: [g.name for g in self.groups],
            get_scenario_group=(lambda n=name: self._group_of_scenario(n)),
            on_group_change=self._set_scenario_group,
        )
        editor.frame.grid(row=0, column=0, sticky="nsew")
        editor.frame.grid_remove()
        self.scenario_editors[name] = editor
        if hasattr(self, "settings"):
            try:
                editor.apply_advanced_visibility(self.settings.advanced_mode)
            except Exception:
                pass

    def _rebuild_editor_tabs(self, force: bool = False) -> None:
        """(Re)sync the editor area with self.scenarios.

        Incremental by default: existing editors are reused, only added
        scenarios get a fresh (heavy) ScenarioEditor built, and only
        removed ones are destroyed. Rebuilding all editors on every
        save/delete/copy was the source of the UI lag — each editor is a
        big widget tree and the user can have many actions.

        `force=True` tears down and rebuilds everything; use it when the
        underlying content changed out-of-band (revert, load-from-file,
        template-folder switch) so reused editors don't show stale data."""
        prev = getattr(self, "_current_scenario", None)

        if force:
            for ed in self.scenario_editors.values():
                try:
                    ed.frame.destroy()
                except Exception:
                    pass
            self.scenario_editors.clear()
            for w in self.editor_content.winfo_children():
                w.destroy()

        wanted = list(self.scenarios.keys())
        wanted_set = set(wanted)

        # Destroy editors whose scenario no longer exists (delete/rename).
        for name in list(self.scenario_editors.keys()):
            if name not in wanted_set:
                try:
                    self.scenario_editors[name].frame.destroy()
                except Exception:
                    pass
                del self.scenario_editors[name]

        # Build editors only for scenarios that don't have one yet.
        for name in wanted:
            if name not in self.scenario_editors:
                self._build_one_editor(name, self.scenarios[name])

        # Keep the dict ordered like self.scenarios so the YAML save
        # order (which serializes from the editor dict) stays stable.
        self.scenario_editors = {
            n: self.scenario_editors[n]
            for n in wanted if n in self.scenario_editors
        }

        # Reused editors may carry stale group dropdowns (a group was
        # renamed/added/removed, or another action changed groups) —
        # cheap to refresh each one's option list + current value.
        for ed in self.scenario_editors.values():
            try:
                ed.refresh_group_options()
            except Exception:
                pass

        self._build_editor_tab_strips()

        # Restore the previous selection if it survived, else show the
        # first scenario.
        if prev in self.scenarios:
            self._select_editor_scenario(prev)
        elif self.scenarios:
            self._select_editor_scenario(next(iter(self.scenarios)))
        else:
            self._current_scenario = None

    def _build_editor_tab_strips(self) -> None:
        """(Re)render the LEFT-column action picker in self.editor_tabs:
        an optional Ungrouped section, then one collapsible color-outlined
        section per group — mirroring the main grouped layout. Each group
        header carries ＋ (new action in group) and ⚙ (edit group); a
        trailing "+ Add group". Buttons select an action to EDIT (tab-style
        look, current one highlighted) — they never fire."""
        for w in self.editor_tabs.winfo_children():
            w.destroy()
        self._editor_tab_buttons = {}

        def _render_section(key, label, names, color, show_header, group=None):
            collapsed = self._editor_group_collapsed.get(key, False)
            sec = ctk.CTkFrame(
                self.editor_tabs, fg_color="transparent",
                border_width=(2 if color else 0),
                border_color=(color or None), corner_radius=8,
            )
            sec.pack(fill="x", padx=2, pady=(4, 2))
            if show_header:
                arrow = "▶" if collapsed else "▼"
                hdr = ctk.CTkFrame(sec, fg_color="transparent")
                hdr.pack(fill="x", padx=4, pady=(2, 4 if collapsed else 0))
                hdr.grid_columnconfigure(0, weight=1)
                ctk.CTkButton(
                    hdr, text=f"{arrow}  {label}", anchor="w", height=24,
                    fg_color="transparent",
                    text_color=("gray10", "gray90"),
                    hover_color=("gray85", "gray25"),
                    font=ctk.CTkFont(size=12, weight="bold"),
                    command=lambda k=key: self._toggle_editor_group(k),
                ).grid(row=0, column=0, sticky="ew")
                # Group management lives right in the picker (real groups
                # only — not the Ungrouped section).
                if group is not None:
                    ctk.CTkButton(
                        hdr, text="+", width=26, height=24,
                        command=lambda gn=group.name:
                            self._new_scenario_in_group(gn),
                        **SECONDARY_BTN_KWARGS,
                    ).grid(row=0, column=1, padx=(2, 0))
                    ctk.CTkButton(
                        hdr, text="⚙", width=26, height=24,
                        command=lambda g=group: self._edit_group(g),
                        **SECONDARY_BTN_KWARGS,
                    ).grid(row=0, column=2, padx=(2, 0))
                if collapsed:
                    return
            host = ctk.CTkFrame(sec, fg_color="transparent")
            host.pack(fill="x", padx=6, pady=(0, 4))
            host.grid_columnconfigure(0, weight=1)
            for i, name in enumerate(names):
                btn = ctk.CTkButton(
                    host, text=name, height=28, anchor="w",
                    command=lambda n=name: self._select_editor_scenario(n),
                )
                btn.grid(row=i, column=0, padx=3, pady=3, sticky="ew")
                self._editor_tab_buttons[name] = (btn, color)

        # No groups → one flat headerless section.
        if not self.groups:
            if self.scenarios:
                _render_section("__all__", "", list(self.scenarios),
                                None, show_header=False)
        else:
            grouped: set[str] = set()
            for g in self.groups:
                grouped.update(s for s in g.scenarios if s in self.scenarios)
            ungrouped = [n for n in self.scenarios if n not in grouped]
            if ungrouped:
                _render_section("__ungrouped__", "Ungrouped", ungrouped,
                                None, show_header=True)
            for g in self.groups:
                valid = [s for s in g.scenarios if s in self.scenarios]
                _render_section(g.name, g.name, valid, g.color,
                                show_header=True, group=g)

        # "+ Add group" at the bottom (the main toolbar's button is hidden
        # in full-focus edit mode, so surface group creation here).
        ctk.CTkButton(
            self.editor_tabs, text="+ Add group", height=28,
            command=self._add_group, **SECONDARY_BTN_KWARGS,
        ).pack(fill="x", padx=6, pady=(8, 2))
        # Trailing spacer so the last section's border isn't clipped.
        ctk.CTkFrame(self.editor_tabs, fg_color="transparent",
                     height=6).pack(fill="x")

        # Re-apply the selected highlight to the freshly built buttons.
        sel = getattr(self, "_current_scenario", None)
        for n, (btn, color) in self._editor_tab_buttons.items():
            self._style_editor_tab(btn, color, n == sel)

    def _fit_editor_tabs_height(self, est_h: int) -> None:
        """Size the strip pane to its content (within bounds) so an
        expanded group reveals all its buttons. The user can still drag
        the sash; the next expand/collapse re-fits."""
        try:
            h = max(48, min(int(est_h), 360))
            self.editor_paned.paneconfigure(self._editor_tabs_holder, height=h)
        except Exception:
            pass

    def _style_editor_tab(self, btn, color, selected: bool) -> None:
        """Tab look: selected = solid fill, unselected = outline. Uses
        the group color when present, else the default accent."""
        if color:
            if selected:
                btn.configure(
                    fg_color=color, border_width=0,
                    text_color=_text_color_for_bg(color),
                    hover_color=_hover_color_for(color),
                )
            else:
                btn.configure(
                    fg_color="transparent", border_width=2,
                    border_color=color,
                    text_color=("gray10", "gray90"),
                    hover_color=("gray85", "gray25"),
                )
        else:
            if selected:
                btn.configure(
                    fg_color=("#3a7ebf", "#1f538d"), border_width=0,
                    text_color="#ffffff",
                    hover_color=("#325882", "#14375e"),
                )
            else:
                btn.configure(
                    fg_color="transparent", border_width=2,
                    border_color=("gray60", "gray40"),
                    text_color=("gray10", "gray90"),
                    hover_color=("gray85", "gray25"),
                )

    def _select_editor_scenario(self, name: str) -> None:
        """Swap the content frame to `name`'s editor and update the
        tab-button highlights. Replaces CTkTabview.set()."""
        if name not in self.scenario_editors:
            return
        cur = getattr(self, "_current_scenario", None)
        if cur and cur != name and cur in self.scenario_editors:
            try:
                self.scenario_editors[cur].frame.grid_remove()
            except Exception:
                pass
        try:
            self.scenario_editors[name].frame.grid()
        except Exception:
            pass
        self._current_scenario = name
        for n, (btn, color) in self._editor_tab_buttons.items():
            self._style_editor_tab(btn, color, n == name)

    def _toggle_editor_group(self, key: str) -> None:
        """Collapse/expand a group's tab strip (buttons only — the
        selected editor below is unaffected)."""
        self._editor_group_collapsed[key] = (
            not self._editor_group_collapsed.get(key, False)
        )
        self._build_editor_tab_strips()

    def _hide_editor_initially(self) -> None:
        """No-op now that the editor starts hidden; kept so the first-run
        call site stays valid."""
        return

    def _hide_viewer_initially(self) -> None:
        """First-run: collapse the caseload viewer. No-op if already hidden."""
        if getattr(self, "_caseload_visible", False):
            try:
                self._toggle_caseload()
            except Exception:
                pass

    def _hide_main_pane(self) -> None:
        try:
            self.main_paned.forget(self.main_pane)
        except Exception:
            pass

    def _show_main_pane(self) -> None:
        """Re-add the main fire pane as the leftmost pane."""
        kw = dict(minsize=260, stretch="never")
        ref = None
        if self._editor_visible:
            ref = self.editor_pane
        elif (getattr(self, "_caseload_visible", False)
              and getattr(self, "caseload_dock", None) is not None):
            ref = self.caseload_dock
        try:
            if ref is not None:
                self.main_paned.add(self.main_pane, before=ref, **kw)
            else:
                self.main_paned.add(self.main_pane, **kw)
        except Exception:
            try:
                self.main_paned.add(self.main_pane, **kw)
            except Exception:
                pass

    def _toggle_editor(self) -> None:
        """Enter/leave the FOCUSED editor mode. Editing is decluttered: it
        hides BOTH the fire pane and the caseload viewer so the editor fills
        the window; Done restores them (viewer only if it was open)."""
        if self._editor_visible:
            # Warn on unsaved edits before leaving the editor.
            if self._editor_is_dirty():
                choice = self._ask_unsaved_editor()
                if choice == "cancel":
                    return  # stay in the editor
                if choice == "save":
                    self._save_yaml()
                    if self._editor_is_dirty():
                        # Save didn't take (e.g. duplicate/empty name);
                        # _save_yaml already logged why — stay so it's fixed.
                        return
                elif choice == "discard":
                    # Reload from disk so the dropped edits don't linger in
                    # the reused editor widgets.
                    try:
                        self._rebuild_editor_tabs(force=True)
                    except Exception:
                        pass
            # Leave editor → restore the main pane (+ viewer if it was up).
            try:
                self.main_paned.forget(self.editor_pane)
            except Exception:
                pass
            self._editor_visible = False
            self._show_main_pane()
            if (getattr(self, "_viewer_before_edit", False)
                    and not self._caseload_visible):
                try:
                    self._toggle_caseload()  # re-show the viewer
                except Exception:
                    pass
            self.editor_toggle_btn.configure(text="✎ Edit actions")
        else:
            # Enter editor → remember + hide the viewer, hide the main pane,
            # show the editor alone (full-focus).
            self._viewer_before_edit = bool(
                getattr(self, "_caseload_visible", False))
            if self._caseload_visible:
                try:
                    self._toggle_caseload()  # hide viewer
                except Exception:
                    pass
            self._hide_main_pane()
            try:
                self.main_paned.add(
                    self.editor_pane, minsize=340, stretch="always")
            except Exception:
                pass
            self._editor_visible = True
            self.editor_toggle_btn.configure(text="Hide editor")
            # Snapshot the clean state so we can detect unsaved edits on close.
            self._editor_baseline = self._editor_signature()
        # Place the divider synchronously so the panes don't paint at a
        # default split and then snap.
        self._restore_main_sash()

    def _editor_signature(self):
        """Stable string of the current editor + group state (what Save
        would persist). Used to detect unsaved edits. None on any error."""
        import json
        try:
            doc = {}
            for old_name, ed in self.scenario_editors.items():
                doc[ed.current_name or old_name] = ed.serialize()
            groups = [
                {"n": g.name, "c": g.color, "s": list(g.scenarios)}
                for g in self.groups
            ]
            return json.dumps({"s": doc, "g": groups},
                              sort_keys=True, default=str)
        except Exception:
            return None

    def _editor_is_dirty(self) -> bool:
        """True if the editor has changes not yet saved to scenarios.yaml.
        False when no baseline was captured or the signature can't be
        computed (never block on uncertainty)."""
        base = getattr(self, "_editor_baseline", None)
        if base is None:
            return False
        sig = self._editor_signature()
        if sig is None:
            return False
        return sig != base

    def _ask_unsaved_editor(self) -> str:
        """Modal: Save / Discard / Cancel for unsaved editor changes.
        Returns 'save' | 'discard' | 'cancel'."""
        dialog = ctk.CTkToplevel(self.root)
        dialog.title("Unsaved changes")
        dialog.transient(self.root)
        dialog.attributes("-topmost", True)
        dialog.grab_set()
        dialog.geometry("460x190")
        dialog.resizable(False, False)
        dialog.lift()
        dialog.focus_force()
        result = {"v": "cancel"}
        ctk.CTkLabel(
            dialog, text="You have unsaved action changes",
            font=ctk.CTkFont(size=14, weight="bold"), anchor="w",
        ).pack(fill="x", padx=20, pady=(18, 4))
        ctk.CTkLabel(
            dialog,
            text="Closing the editor without saving means these edits "
                 "won't be applied (fires, hotkeys, and buttons use the "
                 "saved version). Save them before closing?",
            font=ctk.CTkFont(size=12), anchor="w",
            wraplength=420, justify="left",
        ).pack(fill="x", padx=20, pady=(0, 12))

        def choose(v: str) -> None:
            result["v"] = v
            try:
                dialog.grab_release()
            except Exception:
                pass
            try:
                dialog.destroy()
            except Exception:
                pass

        row = ctk.CTkFrame(dialog, fg_color="transparent")
        row.pack(fill="x", padx=20, pady=(0, 16), side="bottom")
        ctk.CTkButton(
            row, text="Save", width=110, command=lambda: choose("save"),
        ).pack(side="left", padx=4)
        ctk.CTkButton(
            row, text="Discard", width=110, command=lambda: choose("discard"),
            fg_color=("#cc4444", "#aa3333"),
            hover_color=("#aa3333", "#882222"),
        ).pack(side="left", padx=4)
        ctk.CTkButton(
            row, text="Cancel", width=110, command=lambda: choose("cancel"),
            **SECONDARY_BTN_KWARGS,
        ).pack(side="right", padx=4)
        dialog.bind("<Escape>", lambda _e: choose("cancel"))
        dialog.protocol("WM_DELETE_WINDOW", lambda: choose("cancel"))
        self.root.wait_window(dialog)
        return result["v"]

    # ----- Window geometry + divider persistence -----

    def _geometry_is_visible(self, geo: str) -> bool:
        """Validate a 'WxH+X+Y' string: reject it when the window is
        below our minimum size or when most of it would land off the
        screen (so a saved position from an unplugged monitor doesn't
        reopen the app somewhere invisible)."""
        m = re.fullmatch(r"(\d+)x(\d+)([+-]\d+)([+-]\d+)", (geo or "").strip())
        if not m:
            return False
        w, h, x, y = (int(m.group(1)), int(m.group(2)),
                      int(m.group(3)), int(m.group(4)))
        if w < 420 or h < 520:  # below minsize → reject
            return False
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        # Require a meaningful slice of the window to overlap the
        # screen, and the top-left corner not shoved past either edge.
        vis_w = min(x + w, sw) - max(x, 0)
        vis_h = min(y + h, sh) - max(y, 0)
        if vis_w < 160 or vis_h < 160:
            return False
        if x > sw - 80 or y > sh - 80 or x + w < 80 or y + h < 80:
            return False
        return True

    def _restore_window_geometry(self) -> None:
        """Apply the saved (normal) geometry when it's still on-screen,
        else the built-in default. If the window was closed maximized,
        re-maximize once it's mapped."""
        geo = (self.settings.window_geometry or "").strip()
        if geo and self._geometry_is_visible(geo):
            try:
                self.root.geometry(geo)
            except Exception:
                self.root.geometry("900x600")
        else:
            self.root.geometry("900x600")
        if getattr(self.settings, "window_state", "normal") == "zoomed":
            # Defer so the window is mapped before maximizing. Registered
            # before the sash restore (also after(0)) so the divider is
            # placed against the maximized width.
            self.root.after(0, self._safe_zoom)

    def _safe_zoom(self) -> None:
        try:
            self.root.state("zoomed")
        except Exception:
            pass

    def _apply_first_run_geometry(self) -> None:
        """First-launch default window size (NOT maximized): wide enough to
        show two action buttons side-by-side with the whole toolbar fully
        visible, and tall enough to show every group plus a roughly-square
        activity log. Computed from the laid-out widgets so it adapts to
        the bundled sample groups / font scale, then centered on screen."""
        try:
            self.root.update_idletasks()
            # Width: the wider of the full toolbar row and the 2-column
            # action grid drives it (both live in the main pane).
            content_w = max(
                self._toggle_row_frame.winfo_reqwidth(),
                self.button_frame.winfo_reqwidth(),
            )
            width = content_w + 32  # pane padx + window padx

            # Height: everything in the main pane at its natural height
            # already shows all groups; the log box sits at its minimum,
            # so swap that minimum for a roughly-square log.
            h_min = self.main_pane.winfo_reqheight()
            log_min = self.log_tabview.winfo_reqheight()
            desired_log = int(content_w * 0.8)
            height = h_min - log_min + desired_log + 24

            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            width = max(480, min(width, int(sw * 0.95)))
            height = max(560, min(height, int(sh * 0.92)))
            x = max(0, (sw - width) // 2)
            y = max(0, (sh - height) // 2 - 20)
            self.root.geometry(f"{width}x{height}+{x}+{y}")
        except Exception:
            pass

    def _parse_sash_positions(self) -> list[int]:
        """Saved divider x-positions, newest field first, migrating the
        legacy single `main_sash` when the list field is empty."""
        raw = (getattr(self.settings, "sash_positions", "") or "").strip()
        if raw:
            try:
                return [int(x) for x in raw.split(",") if x.strip()]
            except ValueError:
                return []
        legacy = int(getattr(self.settings, "main_sash", 0) or 0)
        return [legacy] if legacy > 0 else []

    @staticmethod
    def _default_sash_positions(n: int, total: int) -> list[int]:
        """Default divider positions for `n` sashes across `total` px."""
        if n == 1:
            return [int(total * 0.38)]
        if n == 2:  # main | editor | caseload
            return [int(total * 0.30), int(total * 0.64)]
        return [int(total * (i + 1) / (n + 1)) for i in range(n)]

    def _restore_main_sash(self) -> None:
        """Place every divider at its saved x (or a sensible default
        split), clamped within the current width. Handles 1 sash
        (editor hidden) or 2 (main | editor | caseload)."""
        try:
            self.root.update_idletasks()
            n_sash = len(self.main_paned.panes()) - 1
            if n_sash < 1:
                return
            total = self.main_paned.winfo_width()
            if total <= 1:  # not laid out yet — retry shortly
                self.root.after(50, self._restore_main_sash)
                return
            saved = self._parse_sash_positions()
            xs = (saved if len(saved) == n_sash
                  else self._default_sash_positions(n_sash, total))
            for i, x in enumerate(xs):
                xi = max(60, min(int(x), total - 60))
                self.main_paned.sash_place(i, xi, 1)
        except Exception:
            pass

    def _save_window_state(self) -> None:
        """Persist the window geometry, maximized state, and divider
        position so the next launch reopens where the user left it. When
        maximized we keep the last *normal* geometry (don't overwrite it
        with the maximized size) but record the zoomed state so it
        reopens maximized."""
        try:
            state = self.root.state()
            self.settings.window_state = "zoomed" if state == "zoomed" else "normal"
            if state == "normal":
                self.settings.window_geometry = self.root.geometry()
            try:
                n_sash = len(self.main_paned.panes()) - 1
                xs = [int(self.main_paned.sash_coord(i)[0])
                      for i in range(n_sash)]
                if xs:
                    self.settings.sash_positions = ",".join(
                        str(x) for x in xs)
                    self.settings.main_sash = xs[0]  # keep legacy in sync
            except Exception:
                pass
            # Remember the pop-out window's geometry if it's open.
            if (getattr(self, "_caseload_popped", False)
                    and getattr(self, "caseload_window", None) is not None):
                try:
                    self.settings.caseload_window_geometry = \
                        self.caseload_window.geometry()
                except Exception:
                    pass
            save_settings(self.settings)
        except Exception:
            pass

    def _new_scenario(self) -> None:
        """Prompt for a name, create a scenario with sensible defaults,
        persist, rebuild tabs/buttons, and switch to the new tab."""
        dialog = ctk.CTkInputDialog(text="Name for new action:", title="New action")
        raw = dialog.get_input()
        if raw is None:
            return
        name = raw.strip()
        if not name:
            self._append_log("New action: empty name; nothing added.")
            return
        if name in self.scenarios:
            self._append_log(f"Action {name!r} already exists.")
            return
        from src.scenarios import ScenarioConfig
        self.scenarios[name] = ScenarioConfig(
            name=name, hotkey="", close_tab_after=True,
            notes=[NoteData(
                interaction_format="Single Interaction",
                interaction_type="Email to Student",
                course_code="", subject="", body="",
                academic_activities=[], submit=True, append_clipboard=False,
            )],
        )
        self._rebuild_editor_tabs()
        self._rebuild_scenario_buttons()
        try:
            self._save_yaml()  # persist immediately so the new tab survives a restart
        except Exception as e:
            self._append_log(f"Could not save new action: {e}")
            return
        # Switch to the freshly-created scenario. With the grid-based
        # content swap this is immediate (no CTkTabview layout race),
        # so no after(0) deferral is needed.
        try:
            self._select_editor_scenario(name)
        except Exception:
            pass

    def _group_of_scenario(self, name: str) -> str:
        """Return the name of the group containing `name`, or '' if it's
        ungrouped."""
        for g in self.groups:
            if name in g.scenarios:
                return g.name
        return ""

    def _set_scenario_group(self, name: str, target_group: str) -> None:
        """Editor group-dropdown handler: reassign `name` to
        `target_group` ('' = ungrouped). Deferred so the dropdown's own
        event finishes before we rebuild the editor tree out from under
        it."""
        self.root.after(0, lambda: self._do_set_scenario_group(name, target_group))

    def _do_set_scenario_group(self, name: str, target_group: str) -> None:
        # Flush on-screen edits so self.scenarios names match the editors.
        try:
            self._save_yaml()
        except Exception:
            pass
        if name not in self.scenarios:
            return
        if self._group_of_scenario(name) == target_group:
            return  # no change
        # Remove from any current group, then add to the target (if any).
        for g in self.groups:
            if name in g.scenarios:
                g.scenarios = [s for s in g.scenarios if s != name]
        if target_group:
            for g in self.groups:
                if g.name == target_group:
                    if name not in g.scenarios:
                        g.scenarios.append(name)
                    break
        self._rebuild_scenario_buttons()
        try:
            self._save_yaml()  # rebuilds editor tabs from the new groups
        except Exception as e:
            self._append_log(f"Group change failed to save: {e}", error=True)
            return
        try:
            self._select_editor_scenario(name)
        except Exception:
            pass
        self._append_log(
            f"Moved {name!r} to group {target_group!r}." if target_group
            else f"Moved {name!r} to Ungrouped.")

    def _new_scenario_in_group(self, group_name: str) -> None:
        """'+' on a group header: create a new action already assigned to
        that group, reveal the editor, and switch to its tab."""
        dialog = ctk.CTkInputDialog(
            text=f"Name for new action in '{group_name}':", title="New action")
        raw = dialog.get_input()
        if raw is None:
            return
        name = raw.strip()
        if not name:
            self._append_log("New action: empty name; nothing added.")
            return
        if name in self.scenarios:
            self._append_log(f"Action {name!r} already exists.")
            return
        from src.scenarios import ScenarioConfig
        self.scenarios[name] = ScenarioConfig(
            name=name, hotkey="", close_tab_after=True,
            notes=[NoteData(
                interaction_format="Single Interaction",
                interaction_type="Email to Student",
                course_code="", subject="", body="",
                academic_activities=[], submit=True, append_clipboard=False,
            )],
        )
        for g in self.groups:
            if g.name == group_name:
                g.scenarios.append(name)
                break
        self._rebuild_editor_tabs()
        self._rebuild_scenario_buttons()
        try:
            self._save_yaml()
        except Exception as e:
            self._append_log(f"Could not save new action: {e}")
            return
        # Reveal the editor (the '+' lives on the always-visible action
        # panel; the editor may be hidden) so the new tab is visible.
        if not getattr(self, "_editor_visible", True):
            try:
                self._toggle_editor()
            except Exception:
                pass
        try:
            self._select_editor_scenario(name)
        except Exception:
            pass

    def _copy_scenario(self) -> None:
        """Duplicate the currently-open action as '<name>-copy' (cleared
        hotkey), placed in the same group, and switch to it."""
        import copy as _copy
        cur = getattr(self, "_current_scenario", None)
        if not cur:
            self._append_log("No action selected to copy.")
            return
        # Flush on-screen edits so the copy matches what's shown.
        try:
            self._save_yaml()
        except Exception:
            pass
        base = self.scenarios.get(cur)
        if base is None:
            self._append_log(f"Couldn't copy {cur!r} (not found).", error=True)
            return
        new_name = f"{cur}-copy"
        n = 2
        while new_name in self.scenarios:
            new_name = f"{cur}-copy{n}"
            n += 1
        new_cfg = _copy.deepcopy(base)
        new_cfg.name = new_name
        new_cfg.hotkey = ""  # avoid a duplicate global hotkey
        self.scenarios[new_name] = new_cfg
        # Place the copy right after the original in the same group.
        for g in self.groups:
            if cur in g.scenarios:
                g.scenarios.insert(g.scenarios.index(cur) + 1, new_name)
                break
        self._rebuild_editor_tabs()
        self._rebuild_scenario_buttons()
        try:
            self._save_yaml()
        except Exception as e:
            self._append_log(f"Copy failed to save: {e}", error=True)
            return
        self._append_log(f"Copied {cur!r} → {new_name!r}.")
        try:
            self._select_editor_scenario(new_name)
        except Exception:
            pass

    def _copy_group(self, group, dialog=None) -> None:
        """Duplicate a whole group: copies the group as '<name>-copy' and
        every action in it as '<action>-copy', then persists and rebuilds."""
        import copy as _copy
        if group is None:
            return
        # Flush on-screen edits so copies match what's shown.
        try:
            self._save_yaml()
        except Exception:
            pass
        # Re-resolve the group from the reloaded list (by name).
        src = next((g for g in self.groups if g.name == group.name), None)
        if src is None:
            self._append_log(
                f"Couldn't copy group {group.name!r} (not found).", error=True)
            return
        existing_g = {g.name for g in self.groups}
        new_gname = f"{src.name}-copy"
        n = 2
        while new_gname in existing_g:
            new_gname = f"{src.name}-copy{n}"
            n += 1
        new_scen_names: list[str] = []
        for sname in src.scenarios:
            base = self.scenarios.get(sname)
            if base is None:
                continue
            new_sname = f"{sname}-copy"
            k = 2
            while new_sname in self.scenarios:
                new_sname = f"{sname}-copy{k}"
                k += 1
            cfg = _copy.deepcopy(base)
            cfg.name = new_sname
            cfg.hotkey = ""  # avoid duplicate global hotkeys
            self.scenarios[new_sname] = cfg
            new_scen_names.append(new_sname)
        self.groups.append(Group(
            name=new_gname, color=src.color, scenarios=new_scen_names))
        self._rebuild_editor_tabs()
        self._rebuild_scenario_buttons()
        try:
            self._save_yaml()
        except Exception as e:
            self._append_log(f"Group copy failed to save: {e}", error=True)
            return
        self._append_log(
            f"Copied group {src.name!r} → {new_gname!r} "
            f"({len(new_scen_names)} action(s)).")
        if dialog is not None:
            try: dialog.grab_release()
            except Exception: pass
            try: dialog.destroy()
            except Exception: pass

    def _settings_new_scenario(self, dialog=None) -> None:
        """Settings → New scenario: close Settings, reveal the editor (so
        the new tab is visible), then run the normal new-scenario flow."""
        if dialog is not None:
            try:
                dialog.grab_release(); dialog.destroy()
            except Exception:
                pass
        if not getattr(self, "_editor_visible", True):
            try:
                self._toggle_editor()
            except Exception:
                pass
        self._new_scenario()

    def _load_scenarios_dialog(self, dialog=None) -> None:
        """Settings → Load from file: pick a .yaml and load it."""
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            title="Load actions file",
            filetypes=[("Action files", "*.yaml *.yml"), ("All files", "*.*")],
        )
        if path:
            self._load_scenarios_from_file(Path(path), dialog)

    def _save_scenarios_dialog(self) -> None:
        """Settings → Save to file: export the current scenarios.yaml to a
        location of the user's choice (to share or back up). Saves the
        editor's current state first so the export is up to date."""
        # Flush any unsaved editor edits to scenarios.yaml so the export
        # matches what's on screen.
        try:
            self._save_yaml()
        except Exception:
            pass
        from tkinter import filedialog
        path = filedialog.asksaveasfilename(
            title="Save actions to file",
            defaultextension=".yaml",
            initialfile="scenarios.yaml",
            filetypes=[("Action files", "*.yaml *.yml"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            Path(path).write_bytes(SCENARIOS_YAML.read_bytes())
            self._append_log(f"Saved actions to {path}.")
        except Exception as e:
            self._append_log(f"Save actions failed: {e}", error=True)

    def _load_sample_scenarios(self, dialog=None) -> None:
        """Settings → Load samples: restore the bundled sample scenarios."""
        self._load_scenarios_from_file(DEFAULT_SCENARIOS_FILE, dialog)

    def _load_scenarios_from_file(self, src_path, dialog=None) -> None:
        """Validate, confirm, back up the current scenarios.yaml, then
        replace it with `src_path` and reload tabs/buttons/hotkeys."""
        src_path = Path(src_path)
        if not src_path.exists():
            self._append_log(
                f"Load actions: file not found ({src_path}).", error=True)
            return
        try:
            loaded = load_scenarios(src_path)
            load_groups(src_path)
        except Exception as e:
            self._append_log(
                f"Load actions: '{src_path.name}' isn't a valid actions "
                f"file ({e}).", error=True)
            return
        n = len(loaded)
        if not ask_yes_no_topmost(
            self.root, "Load actions?",
            f"Replace your current actions with {n} action(s) from "
            f"'{src_path.name}'?\n\nYour current actions are backed up "
            f"to scenarios.yaml.bak first.",
            yes_label="Load", no_label="Cancel",
        ):
            return
        try:
            if SCENARIOS_YAML.exists():
                SCENARIOS_YAML.with_name("scenarios.yaml.bak").write_bytes(
                    SCENARIOS_YAML.read_bytes())
        except Exception:
            pass
        try:
            SCENARIOS_YAML.write_bytes(src_path.read_bytes())
        except Exception as e:
            self._append_log(f"Load actions failed: {e}", error=True)
            return
        try:
            self.scenarios = load_scenarios()
            self.groups = load_groups()
        except Exception as e:
            self._append_log(f"Loaded but reload failed: {e}", error=True)
            return
        self._rebuild_editor_tabs(force=True)  # content replaced from file
        self._rebuild_scenario_buttons()
        self._restart_hotkeys()
        self._append_log(
            f"Loaded {n} action(s) from '{src_path.name}'. "
            "Previous actions backed up to scenarios.yaml.bak.")
        if dialog is not None:
            try:
                dialog.grab_release(); dialog.destroy()
            except Exception:
                pass

    def _set_active_template_folder(self, path, label_widget=None) -> None:
        """Switch the active email-templates folder, persist it, and
        refresh anything that lists templates."""
        p = set_templates_dir(path)
        self.settings.email_templates_dir = str(p)
        save_settings(self.settings)
        if label_widget is not None:
            try:
                label_widget.configure(text=f"Folder:  {p}")
            except Exception:
                pass
        try:
            self._rebuild_editor_tabs(force=True)  # refresh template dropdowns
        except Exception:
            pass
        self._append_log(f"Email templates folder: {p}")

    def _create_template_folder(self, label_widget=None) -> None:
        """Create a new (empty) email-templates folder under the config
        dir and switch to it. Optionally seed it with the bundled samples."""
        dlg = ctk.CTkInputDialog(
            text="Name for the new email-templates folder:",
            title="Create template folder")
        raw = dlg.get_input()
        if raw is None:
            return
        name = raw.strip().strip("/\\")
        if not name:
            return
        newp = USER_CONFIG_DIR / name
        try:
            newp.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self._append_log(f"Couldn't create folder: {e}", error=True)
            return
        # Offer to seed the new folder with the bundled samples so it's
        # usable immediately (otherwise it starts empty).
        if not any(newp.iterdir()) and DEFAULT_EMAIL_TEMPLATES_DIR.exists():
            if ask_yes_no_topmost(
                self.root, "Seed samples?",
                "Copy the sample templates into the new folder so it's not "
                "empty?", yes_label="Copy samples", no_label="Leave empty",
            ):
                for f in DEFAULT_EMAIL_TEMPLATES_DIR.iterdir():
                    if f.is_file():
                        try:
                            (newp / f.name).write_bytes(f.read_bytes())
                        except Exception:
                            pass
        self._set_active_template_folder(newp, label_widget)

    def _load_template_folder(self, label_widget=None) -> None:
        """Switch the active email-templates folder to an existing one."""
        from tkinter import filedialog
        p = filedialog.askdirectory(title="Choose an email-templates folder")
        if p:
            self._set_active_template_folder(p, label_widget)

    def _capture_hotkey(self, on_done: Callable[[str], None]) -> None:
        """Open the modal capture dialog. Pauses the global pynput
        listener while it's open so the capture dialog sees the
        keystrokes instead of pynput swallowing F-keys."""
        was_running = self.hotkey_listener is not None
        if was_running:
            try:
                self.hotkey_listener.stop()
            except Exception:
                pass
            self.hotkey_listener = None

        def wrapped(combo: str) -> None:
            on_done(combo)
            if was_running:
                self._start_hotkeys()

        open_hotkey_capture(self.root, wrapped)

    def _delete_scenario(self) -> None:
        """Drop the currently-selected scenario from the in-memory dict
        and rebuild tabs/buttons. The deletion is *draft* — scenarios.yaml
        isn't touched until the user clicks 'Save changes'. 'Revert'
        brings the scenario back."""
        name = getattr(self, "_current_scenario", None)
        if not name or name not in self.scenarios:
            self._append_log("No action tab selected.")
            return
        from tkinter import messagebox
        if not messagebox.askyesno(
            "Delete action",
            f"Delete action {name!r}?\n\n"
            "This only updates the editor — click 'Save changes' to "
            "persist, or 'Revert' to undo.",
        ):
            return
        self.scenarios.pop(name, None)
        self._rebuild_editor_tabs()
        self._rebuild_scenario_buttons()
        self._append_log(
            f"Action {name!r} marked for deletion. "
            "Click 'Save changes' to persist or 'Revert' to undo."
        )

    def _revert_editor(self) -> None:
        # Reload from disk and rebuild tabs/buttons so structural drafts
        # (added or deleted scenarios) are undone, not just field edits.
        try:
            self.scenarios = load_scenarios()
            self.groups = load_groups()
        except Exception as e:
            self._append_log(f"Revert failed: {e}")
            return
        self._rebuild_editor_tabs(force=True)  # discard on-screen drafts
        self._rebuild_scenario_buttons()
        self._append_log("Editor reverted to saved YAML.")

    def _scenarios_with_submit_off(
        self, scenario: ScenarioConfig,
    ) -> list[int]:
        """Return 1-based indices of notes in `scenario` that have
        submit=False. Used by the pre-fire warning to tell the user
        WHICH notes will be left for manual review."""
        return [
            i + 1 for i, n in enumerate(scenario.notes)
            if not n.submit
        ]

    def _confirm_submit_off_or_abort(
        self, scenario: ScenarioConfig, *, batch: bool = False,
    ) -> bool:
        """Pop a topmost confirmation when any note in `scenario` has
        Submit unchecked. Returns True to proceed, False to abort.
        For batch fires, the warning frames the impact at scale
        ("…across N students") so the user knows what they're about
        to leave behind."""
        off = self._scenarios_with_submit_off(scenario)
        if not off:
            return True
        plural = "s" if len(off) > 1 else ""
        notes_label = ", ".join(f"Note {n}" for n in off)
        if batch:
            msg = (
                f"Heads up — {notes_label} in this action "
                f"{'have' if len(off) > 1 else 'has'} 'Submit and close "
                f"automatically' unchecked.\n\n"
                "The form will be filled for every student in the batch "
                "but you'll need to click Submit manually in Salesforce "
                "for each one.\n\n"
                "Proceed anyway?"
            )
        else:
            msg = (
                f"{notes_label} {'have' if len(off) > 1 else 'has'} "
                "'Submit and close automatically' unchecked.\n\n"
                "The form will be filled but you'll need to click "
                "Submit manually in Salesforce.\n\n"
                "Proceed anyway?"
            )
        return ask_yes_no_topmost(
            self.root,
            f"Note{plural} won't auto-submit",
            msg, yes_label="Proceed", no_label="Abort",
        )

    def _save_yaml(self) -> None:
        new_doc: dict = {"scenarios": {}}
        seen: set[str] = set()
        # Diagnostic latch: surface the submit state of every note
        # being saved so the user can spot a stray-uncheck if it
        # happens. Cheap to compute; logs once per save.
        submit_summary: list[str] = []
        for old_name, ed in list(self.scenario_editors.items()):
            new_name = ed.current_name or old_name
            if not new_name:
                self._append_log(f"!! Empty action name (was {old_name!r}); aborting.")
                return
            if new_name in seen:
                self._append_log(
                    f"!! Duplicate action name {new_name!r}; aborting save. "
                    "Pick unique names then try again."
                )
                return
            seen.add(new_name)
            try:
                serialized = ed.serialize()
                new_doc["scenarios"][new_name] = serialized
            except Exception as e:
                self._append_log(f"Could not serialize {new_name!r}: {e}")
                return
            # Record which notes would land with Submit unchecked so
            # the activity log surfaces it at save time — a stray
            # uncheck stays visible instead of hiding until the next
            # batch fire shows up with FALSE rows in note_log.csv.
            unchecked = [
                i + 1 for i, n in enumerate(serialized.get("notes", []))
                if not n.get("submit", True)
            ]
            if unchecked:
                submit_summary.append(
                    f"{new_name!r}: Note(s) "
                    + ", ".join(str(i) for i in unchecked)
                    + " have Submit unchecked"
                )

        # Persist groups alongside scenarios. Each group's scenario
        # list is filtered to names that still exist in the saved
        # set — protects against dangling references when a
        # scenario gets renamed or deleted.
        if self.groups:
            saved_names = set(new_doc["scenarios"].keys())
            new_doc["groups"] = [
                {
                    "name": g.name,
                    "color": g.color,
                    "scenarios": [
                        s for s in g.scenarios if s in saved_names
                    ],
                }
                for g in self.groups
            ]

        try:
            SCENARIOS_YAML.write_text(
                yaml.safe_dump(new_doc, sort_keys=False, allow_unicode=True),
                encoding="utf-8",
            )
        except Exception as e:
            self._append_log(f"Save failed: {e}")
            return
        try:
            self.scenarios = load_scenarios()
            self.groups = load_groups()
        except Exception as e:
            self._append_log(f"Saved but reload failed: {e}")
            return

        # Names may have changed — rebuild tabs and buttons so the new
        # names show up everywhere.
        self._rebuild_editor_tabs()
        self._rebuild_scenario_buttons()
        self._restart_hotkeys()
        # Editor now matches disk — reset the unsaved-changes baseline.
        self._editor_baseline = self._editor_signature()
        self._append_log(
            "Actions saved; tabs, buttons, and hotkeys refreshed.")
        # Surface the unchecked-submit summary right after save so a
        # stray uncheck doesn't hide until the next batch fire produces
        # FALSE rows. One log line per offending scenario.
        for line in submit_summary:
            self._append_log(f"  ⚠  {line}")

    # ----- Hotkey listener -----

    def _start_hotkeys(self) -> None:
        self._hotkeys = []
        self._suppress_vks = set()
        for sc in self.scenarios.values():
            if not sc.hotkey:
                continue
            try:
                hk_string = to_pynput_hotkey_string(sc.hotkey)
                parsed = keyboard.HotKey.parse(hk_string)
            except Exception as e:
                self._post_status(f"Skipped hotkey {sc.hotkey!r}: {e}")
                continue
            cb = (lambda s=sc: self._fire_from_hotkey(s))
            self._hotkeys.append(keyboard.HotKey(parsed, cb))
            vk = _standalone_fkey_vk(sc.hotkey)
            if vk is not None:
                self._suppress_vks.add(vk)

        if not self._hotkeys:
            return

        try:
            self.hotkey_listener = keyboard.Listener(
                on_press=self._on_key_press,
                on_release=self._on_key_release,
                win32_event_filter=self._win32_event_filter,
            )
            self.hotkey_listener.daemon = True
            self.hotkey_listener.start()
        except Exception as e:
            self._post_status(f"Could not start hotkey listener: {e}")
            return

        keys = ", ".join(
            f"{sc.hotkey}->{sc.name}" for sc in self.scenarios.values() if sc.hotkey
        )
        suppressed = (
            f"  (claiming {len(self._suppress_vks)} F-keys system-wide)"
            if self._suppress_vks else ""
        )
        self._post_status(f"Hotkeys active: {keys}{suppressed}")

    def _restart_hotkeys(self) -> None:
        try:
            if self.hotkey_listener is not None:
                self.hotkey_listener.stop()
        except Exception:
            pass
        self.hotkey_listener = None
        self._start_hotkeys()

    def _win32_event_filter(self, msg, data) -> bool:
        if data.vkCode in self._suppress_vks and self.hotkey_listener is not None:
            try:
                self.hotkey_listener.suppress_event()
            except Exception:
                pass
        return True

    def _on_key_press(self, key) -> None:
        if self.hotkey_listener is None:
            return
        canonical = self.hotkey_listener.canonical(key)
        for hk in self._hotkeys:
            hk.press(canonical)

    def _on_key_release(self, key) -> None:
        if self.hotkey_listener is None:
            return
        canonical = self.hotkey_listener.canonical(key)
        for hk in self._hotkeys:
            hk.release(canonical)

    def _fire_from_hotkey(self, scenario: ScenarioConfig) -> None:
        self.root.after(0, lambda: self._fire(scenario))

    # ----- Scenario firing -----

    def _find_student(self) -> None:
        self._find_student_by_query(self.search_var.get())

    def _find_student_by_query(self, query: str, new_tab: bool = False,
                               raise_after: Optional[bool] = None,
                               quiet: bool = False) -> None:
        """Navigate the worker browser to a student record by query
        (name / Student ID / email). Shared by the main Find box and
        the caseload panel's row-click. When `new_tab` is True the
        student opens in a fresh console subtab (right/middle-click),
        otherwise the current tab is reused (double-click). `raise_after`
        forwards to the worker (pass False for a background nav, e.g.
        while firing a scenario); `quiet` suppresses the log line."""
        query = (query or "").strip()
        if not query:
            return
        # Block user-initiated navigation while a fire/batch/selection run
        # is in progress — a row double-click here would enqueue a FIND
        # that interleaves with the run's own navigation and yanks the
        # browser to the wrong record ("No visible note panel"). The run's
        # own navigation uses the blocking find paths, not this method, so
        # this guard doesn't affect it.
        if self._is_busy:
            self._append_log(
                "Busy — finish the current task before opening a student."
            )
            return
        if not self.worker.ready_event.is_set():
            self._append_log("Browser not ready yet — wait and try again.")
            return
        if not quiet:
            where = " (new tab)" if new_tab else ""
            self._append_log(f"--- Searching {query!r}{where} ---")
        self.worker.submit_find_student(
            query, new_tab=new_tab, raise_after=raise_after)

    def _email_uses_sample_placeholder(self, scenario: ScenarioConfig) -> bool:
        """True if the scenario's email still carries the shipped sample
        placeholder address (in To, signature, or the template body) —
        i.e. the user hasn't substituted their own email yet."""
        e = scenario.email
        if e is None:
            return False
        ph = SAMPLE_EMAIL_PLACEHOLDER
        if ph in (e.to or "") or ph in (e.signature_file or ""):
            return True
        if e.body_html_file:
            try:
                p = templates_dir() / e.body_html_file
                if p.exists() and ph in p.read_text(
                        encoding="utf-8", errors="ignore"):
                    return True
            except Exception:
                pass
        return False

    def _confirm_no_placeholder_email(self, scenario: ScenarioConfig) -> bool:
        """Warn (and require confirmation) when firing an action whose
        email still uses the sample placeholder address. Returns True to
        proceed, False to abort."""
        if not self._email_uses_sample_placeholder(scenario):
            return True
        return ask_yes_no_topmost(
            self.root, "Sample email address not set",
            f"This action still uses the sample placeholder address "
            f"'{SAMPLE_EMAIL_PLACEHOLDER}'.\n\n"
            "It came from the bundled sample templates. Replace it with "
            "your own address (in the action's To field and signature, "
            "and inside the email template) before sending.\n\n"
            "Send anyway?",
            yes_label="Send anyway", no_label="Cancel",
        )

    def _fire(self, scenario: ScenarioConfig) -> None:
        if self._is_busy:
            self._append_log(
                f"Busy — wait for the current task to finish before "
                f"firing {scenario.name!r}."
            )
            return
        if not self.worker.ready_event.is_set():
            self._append_log("Browser not ready yet — wait and try again.")
            return
        # Guard: bundled sample emails ship a placeholder address. Don't
        # let one go out without the user swapping in their own email.
        if not self._confirm_no_placeholder_email(scenario):
            self._append_log(
                f"{scenario.name!r}: not fired — sample placeholder email "
                "address not replaced.")
            return
        override = self.course_var.get().strip()
        self._append_log(f"--- Firing {scenario.name!r} ---")

        # Batch scenarios have their own driver — load the caseload,
        # apply filters, show a review/confirm dialog, then loop.
        if scenario.batch is not None:
            self._set_busy(f"Batch: {scenario.name}")
            try:
                self._fire_batch(scenario, override)
            finally:
                self._set_idle()
            return

        self._set_busy(f"Running {scenario.name}…")
        try:
            self._fire_per_student(scenario, override)
        finally:
            self._set_idle()

    def _collect_prompt_vars(
        self, scenario: ScenarioConfig,
    ) -> Optional[dict[str, str]]:
        """Pop a prompt dialog for each entry in scenario.prompts and
        return `{var: value}`. Returns None if the user cancelled any
        prompt — caller should abort the fire."""
        prompt_vars: dict[str, str] = {}
        for p in scenario.prompts:
            value = prompt_additional_text(
                self.root, p.label or p.var, p.prefill,
            )
            if value is None:
                self._append_log(
                    f"Prompt {p.var!r} cancelled; action not fired."
                )
                return None
            prompt_vars[p.var] = value
        return prompt_vars


    def _fire_per_student(self, scenario: ScenarioConfig, override: str,
                          *, prenav_query: str = "",
                          prenav_label: str = "", course_hint: str = "") -> None:
        """Per-student (non-batch) scenario fire — wraps the original
        in-line `_fire` body so we can sandwich it between _set_busy
        and _set_idle.

        When `prenav_query` is set (fired from a caseload row), the
        student is already chosen: we navigate to them in the background
        and skip the find/pick prompt, then file against that record."""

        # Pre-flight: if any note has Submit unchecked, confirm before
        # we ask the user to do anything else (FERPA: don't surprise
        # them with notes that need manual submission AFTER they've
        # answered prompts / picked a student).
        if not self._confirm_submit_off_or_abort(scenario, batch=False):
            self._append_log(
                f"{scenario.name!r}: aborted at submit-unchecked warning."
            )
            return

        # Step 1: find + pick (if enabled). Combined dialog lets the
        # user retype if the first query was wrong or surfaces too
        # many candidates. Worker handles search; fuzzy fallback kicks
        # in if there are no exact matches.
        # Text-only actions resolve the student from the caseload CSV, so they
        # DON'T need the Salesforce record opened — skip the navigation (faster
        # and avoids the intermittent "record didn't open" click flake).
        text_only = self._is_text_only(scenario)
        chosen_name = ""
        if prenav_query:
            if text_only:
                chosen_name = prenav_label or prenav_query
            else:
                # Student already chosen (caseload row). Use the FAST
                # navigation (same Shift+X switch as a double-click), then
                # wait only until the note panel is actually loaded — no
                # Caseload reload, no fixed 2s settle. (A bare find returns
                # before the panel renders, which made the note RUN bail with
                # "no visible note panel".)
                if not self._navigate_for_fire_blocking(prenav_query):
                    self._append_log(
                        f"Could not open {prenav_label or prenav_query!r}; "
                        "scenario not fired."
                    )
                    return
                chosen_name = prenav_label
        elif scenario.find_first:
            # Text-only actions search the cached CSV (instant, main-thread) so
            # Find doesn't queue behind a running background scrape; note/email
            # actions search the live list via the worker.
            searcher = (self._list_matches_from_csv if text_only
                        else self._list_matches_blocking)
            chosen = prompt_find_and_pick(self.root, searcher)
            if not chosen:
                self._append_log("Find cancelled; action not fired.")
                return
            if text_only:
                chosen_name = chosen
            elif not self._click_match_blocking(chosen):
                self._append_log(
                    f"Could not navigate to {chosen!r}; action not fired."
                )
                return
            else:
                chosen_name = chosen

        # Step 2: prompts (scenario-level, feed {{var}} into emails
        # and note bodies). Collect BEFORE per-note custom edits so
        # `{{var}}` placeholders inside a custom-edited body get
        # substituted too.
        prompt_vars = self._collect_prompt_vars(scenario)
        if prompt_vars is None:
            return  # user cancelled a prompt

        # Step 3: clipboard FIRST (main-thread read; Tk + PIL aren't
        # thread-safe) so it can be folded into the additional-text review.
        clipboard = ""
        if any(n.append_clipboard for n in scenario.notes):
            clipboard = self._read_clipboard_content()

        # Step 4: per-note fire-time edit. A note that opts into "Edit note
        # at fire time" pops a single dialog to edit the body, course code,
        # academic activities, and — if the student has open Essential
        # Actions — attach/close one (read once; offered in the first such
        # note's dialog). Replaces the old separate body prompt + EA dialog.
        custom_bodies: dict[int, str] = {}
        custom_courses: dict[int, str] = {}
        custom_activities: dict[int, list] = {}
        ea_arg = None
        eas_read = False
        eas: list = []
        edit_idxs = [i for i, n in enumerate(scenario.notes)
                     if n.enter_additional_text]
        # A main-window fire has no caseload row, so there's no course_hint
        # and the fire-time note dialog's Course field would be blank (the
        # right-click path gets the code from the row). Detect the active
        # student's course code up front — same lookup the worker uses at
        # submit — but only when a dialog will actually show and we don't
        # already have a code.
        if edit_idxs and not course_hint and not override:
            try:
                _ctx = self._get_student_context_blocking(name_hint=chosen_name)
                if _ctx and _ctx.get("course_code"):
                    course_hint = _ctx["course_code"]
            except Exception:
                pass
        for pos, i in enumerate(edit_idxs):
            n = scenario.notes[i]
            label = f"Note {i + 1}"
            prefill = n.body
            if n.append_clipboard and clipboard:
                sep = "\n" if prefill and not prefill.endswith("\n") else ""
                prefill = f"{prefill}{sep}{clipboard}"
            if not eas_read:
                self._append_log("Checking this student's Essential Actions…")
                eas = self._read_eas_blocking()
                eas_read = True
            course_default = (n.course_code_override or course_hint
                              or override or "")
            offer_ea = (pos == 0)  # attach at most one EA per fire
            res = prompt_edit_note(
                self.root, label, prefill, course_default,
                list(n.academic_activities), eas if offer_ea else [])
            if res is None:
                self._append_log(f"{label} edit cancelled; action not fired.")
                return
            custom_bodies[i] = res["body"]
            if res.get("course"):
                custom_courses[i] = res["course"]
            custom_activities[i] = res.get("activities", [])
            if offer_ea and res.get("ea"):
                ea_arg = res["ea"]

        # Step 5: email (if scenario has one). Reviewed in the same in-app
        # previewer as batch/selection (incl. the fire-time template
        # dropdown when opted in), then auto-sent. Rendered from the
        # scraped student context since there's no CSV row here.
        if scenario.email is not None:
            student_ctx = self._get_student_context_blocking(name_hint=chosen_name)
            if student_ctx is None:
                self._append_log(
                    "Couldn't read student context for email; action not fired."
                )
                return
            from src import outlook_email
            user_info = outlook_email.get_user_info()
            who = (chosen_name or student_ctx.get("full_name", "")
                   or "this student")

            def _render(scn: ScenarioConfig) -> list[dict]:
                return [self._build_email_preview_data(
                    scn, {}, prompt_vars, user_info, ctx_override=student_ctx)]

            scenario, selected, edits = self._run_email_review(
                scenario, _render, who)
            if not selected:
                self._append_log("Email review cancelled; note not filed.")
                return
            ctx_send = {**student_ctx, **prompt_vars}
            if not self._send_scenario_email(
                scenario.email, ctx_send, auto_send=True,
                body_html_override=edits.get(0),
            ):
                self._append_log("Email send failed; note not filed.")
                return

        # Reading EAs switched the record to the EA tab; if we're NOT
        # attaching, re-open the record so the normal note panel is back.
        if eas_read and ea_arg is None:
            nav_query = prenav_query or chosen_name
            if nav_query:
                self._navigate_for_fire_blocking(nav_query)

        # Apply the fire-time course / academic-activity edits onto a copy
        # of the scenario (run_scenario reads note.course_code_override and
        # note.academic_activities); body edits go via custom_bodies.
        if custom_courses or custom_activities:
            import copy as _copy
            scenario = _copy.deepcopy(scenario)
            for i, cc in custom_courses.items():
                if 0 <= i < len(scenario.notes):
                    scenario.notes[i].course_code_override = cc
            for i, acts in custom_activities.items():
                if 0 <= i < len(scenario.notes):
                    scenario.notes[i].academic_activities = acts

        # Text (Mongoose) channel — fire before the note submit so a text-only
        # action doesn't fall through to the note-form path.
        if scenario.text is not None:
            self._fire_text(scenario, chosen_name, prompt_vars)

        # Note submit — skip for channel-only (text/email) actions with no notes.
        if scenario.notes:
            self.worker.submit_scenario(
                scenario, override, clipboard,
                custom_bodies=custom_bodies,
                prompt_vars=prompt_vars, ea=ea_arg,
            )

    def _caseload_row_by_id(self, student_id: str) -> Optional[dict]:
        """The cached caseload CSV row for a Student ID, or None."""
        sid = (student_id or "").strip()
        if not sid or not self._caseload_rows:
            return None
        for r in self._caseload_rows:
            if (r.get("StudentID") or "").strip() == sid:
                return r
        return None

    def _caseload_row_by_name(self, name: str) -> Optional[dict]:
        """The cached caseload CSV row matching a student Name (case-insensitive),
        or None. Lets a text action resolve a student without opening their
        Salesforce record."""
        n = (name or "").strip().lower()
        if not n or not self._caseload_rows:
            return None
        for r in self._caseload_rows:
            if (r.get("Name") or "").strip().lower() == n:
                return r
        return None

    def _list_matches_from_csv(self, query: str) -> list[str]:
        """Search the cached caseload CSV by name (main thread, no worker, no
        Salesforce). Used for text-action Find so it's instant and doesn't queue
        behind a running background scrape. Tiers: exact, startswith, contains."""
        q = (query or "").strip().lower()
        if not q or not self._caseload_rows:
            return []
        exact, starts, contains = [], [], []
        for r in self._caseload_rows:
            name = (r.get("Name") or "").strip()
            if not name:
                continue
            low = name.lower()
            if low == q:
                exact.append(name)
            elif low.startswith(q):
                starts.append(name)
            elif q in low:
                contains.append(name)
        out, seen = [], set()
        for n in exact + starts + contains:
            k = n.lower()
            if k not in seen:
                seen.add(k)
                out.append(n)
        return out[:50]

    def _text_vars_from_row(self, row: dict) -> dict:
        """Build template variables (the same set email uses) from a caseload
        CSV row — for firing a text without scraping the Salesforce record."""
        name = (row.get("Name") or "").strip()
        first, _, last = name.partition(" ")
        pref = (row.get("stuprename") or "").strip() or first
        course = (row.get("CourseCode") or "").strip()
        try:
            from src.student_lookup import COURSE_CODE_RE
            m = COURSE_CODE_RE.match(course)
            if m:
                course = m.group(1)
        except Exception:
            pass
        return {
            "first_name": _capitalize_name(first),
            "last_name": _capitalize_name(last),
            "full_name": _capitalize_name(name),
            "preferred_name": _capitalize_name(pref),
            "student_email": (row.get("StudentEmail") or "").strip(),
            "student_id": (row.get("StudentID") or "").strip(),
            "course_code": course,
            "pm_name": _capitalize_name((row.get("MentorName") or "").strip()),
            "pm_email": "",
        }

    def _is_text_only(self, scenario: ScenarioConfig) -> bool:
        """A scenario whose only channel is text — no Salesforce note and no
        email. Such actions don't need the student's Salesforce record opened;
        they resolve everything from the caseload CSV."""
        return (scenario.text is not None and not scenario.notes
                and scenario.email is None)

    @staticmethod
    def _texting_opted_in(row: dict) -> bool:
        """True if the caseload row's TextingPreference is 'Opted In'. Students
        who aren't opted in don't exist as textable contacts in Mongoose, so we
        skip them up front (faster + no batch stalls on an un-addable number)."""
        return (row.get("TextingPreference") or "").strip().lower() == "opted in"

    def _send_text_blocking(self, payload: dict) -> Optional[dict]:
        """Queue a SEND_TEXT and block the main thread until the worker returns
        {ok}/{error}."""
        done_var = tk.BooleanVar(value=False)
        holder: dict = {"res": None}

        def on_done(res):
            def set_main():
                holder["res"] = res
                done_var.set(True)
            try:
                self.root.after(0, set_main)
            except Exception:
                holder["res"] = res
                done_var.set(True)

        self.worker.submit_send_text(payload, on_done)
        self.root.wait_variable(done_var)
        return holder["res"]

    # Team timezone for converting a student-local schedule time to the tz
    # Mongoose's scheduler enters times in. The team is Eastern; TODO: make
    # this a setting (read Mongoose's .timezone-label) for non-Eastern teams.
    TEAM_IANA = "America/New_York"

    def _fire_text(self, scenario: ScenarioConfig, chosen_name: str,
                   prompt_vars: Optional[dict]) -> bool:
        """Fire a scenario's text (Mongoose) channel for the active student.
        Resolves Mobile Phone + Timezone from the caseload CSV cache, renders
        the body, computes a timezone-aware schedule slot, and drives Mongoose
        (commit=False stops at the confirm/schedule step for review). Returns
        True on success."""
        from src import text_message as tm
        tcfg = scenario.text
        # Resolve the student WITHOUT needing the Salesforce record open: prefer
        # the cached caseload CSV row (by name) — texting only needs the mobile,
        # timezone, and course, all of which the CSV has. Fall back to scraping
        # the active page only if the name isn't in the cache.
        row = self._caseload_row_by_name(chosen_name) if chosen_name else None
        if row is not None:
            variables = self._text_vars_from_row(row)
        else:
            ctx = self._get_student_context_blocking(name_hint=chosen_name)
            if not ctx:
                self._append_log("Text: couldn't read student context; not sent.",
                                 error=True)
                return False
            row = self._caseload_row_by_id(ctx.get("student_id", ""))
            variables = {
                "first_name": ctx.get("first_name", ""),
                "last_name": ctx.get("last_name", ""),
                "full_name": ctx.get("full_name", ""),
                "preferred_name": ctx.get("preferred_name", ""),
                "student_email": ctx.get("student_email", ""),
                "student_id": (ctx.get("student_id") or "").strip(),
                "course_code": ctx.get("course_code", ""),
                "pm_name": ctx.get("pm_name", ""),
                "pm_email": ctx.get("pm_email", ""),
            }
        if prompt_vars:
            variables.update(prompt_vars)
        who = (variables.get("full_name") or variables.get("student_id")
               or "this student")
        if row is not None and not self._texting_opted_in(row):
            self._append_log(
                f"Text: {who} is not opted in to texting — skipped.", error=True)
            return False
        mobile_raw = (row.get("MobilePhone") if row else "") or ""
        mobile = tm.normalize_phone(mobile_raw)
        if not mobile:
            self._append_log(
                f"Text: no usable Mobile Phone for {who} (got {mobile_raw!r}); "
                "not sent.", error=True)
            return False
        body_tmpl = tcfg.body
        if not body_tmpl and tcfg.body_file:
            try:
                from src import config as _cfg, email_template
                body_tmpl = email_template.load_template(
                    _cfg.templates_dir() / tcfg.body_file)
            except Exception as e:
                self._append_log(
                    f"Text: couldn't load template {tcfg.body_file!r}: {e}",
                    error=True)
                return False
        body = tm.render_message(body_tmpl, variables)
        over = tm.over_length(body)
        if over:
            self._append_log(
                f"Text: message is {over} char(s) over the {tm.MAX_SMS_LEN} "
                "limit; not sent.", error=True)
            return False
        sch_payload = None
        sched_name = ""
        if tcfg.schedule:
            tzc = (row.get("Timezone") if row else "") or ""
            slot = tm.compute_schedule_slot(
                tzc, self.TEAM_IANA, target_hour=tcfg.target_hour)
            if slot is None:
                self._append_log(
                    f"Text: unknown timezone {tzc!r} for {who}; can't schedule "
                    "(set a Timezone, or set schedule:false).", error=True)
                return False
            sch_payload = {
                "date_str": slot.date_str, "hour12": slot.hour12,
                "minute": slot.minute, "ampm": slot.ampm,
                "student_local_str": slot.student_local_str,
            }
            sched_name = (f"{variables['course_code']} "
                          f"{variables['first_name']}").strip() or "Scheduled text"
        inbox_label = tcfg.inbox_label or (
            f"{variables['course_code']} Inbox" if variables["course_code"] else "")
        # Review/edit in the app (unless the action is set to send automatically),
        # then let the tool drive Mongoose to completion — the user never has to
        # touch the Mongoose UI.
        scheduled = sch_payload is not None
        if not tcfg.commit:
            when_str = (f"{sch_payload['student_local_str']} (student-local)"
                        if scheduled else "now")
            edited = prompt_text_review(
                self.root, who=who, mobile=mobile, inbox_label=inbox_label,
                when_str=when_str, body=body, char_limit=tm.MAX_SMS_LEN,
                scheduled=scheduled)
            if edited is None:
                self._append_log("Text review cancelled; not sent.")
                return False
            body = edited
            if tm.over_length(body):
                self._append_log(
                    f"Text: trimmed to the {tm.MAX_SMS_LEN}-char limit.")
        payload = {
            "body": body,
            "recipients": [mobile],
            "inbox_label": inbox_label,
            "schedule": sch_payload,
            "schedule_name": sched_name,
            "commit": True,  # tool finishes in Mongoose; review already happened
        }
        when = (f"scheduling ({sch_payload['student_local_str']} student-local)"
                if scheduled else "sending now")
        self._append_log(
            f"Text: {when} for {who} via {inbox_label or 'current inbox'}…")
        # Lock the browser while the tool drives Mongoose (the review dialog
        # already ran, so the scrim hides nothing the user needs).
        self._lock_browser_for_run()
        try:
            res = self._send_text_blocking(payload)
        finally:
            self._unlock_browser_after_run()
        if not res or res.get("error"):
            self._append_log(f"Text failed: {(res or {}).get('error')}",
                             error=True)
            return False
        self._append_log(
            f"Text: {'scheduled' if scheduled else 'sent'} in Mongoose for {who}.")
        return True

    def _fire_batch(self, scenario: ScenarioConfig, override: str) -> None:
        """Drive a batch scenario end-to-end: load caseload, filter,
        review/confirm, then loop email→note per selected student.
        The activity log is the progress display; cancellation is via
        any modal Cancel button (which aborts the batch from that
        point on)."""
        from tkinter import messagebox

        # Text-only batch: no Salesforce notes/navigation — render each
        # student's text, review them all, then send/schedule each. Its own
        # driver (the note/email path below doesn't apply).
        if self._is_text_only(scenario):
            self._fire_batch_text(scenario)
            return

        # Pre-flight: if any note has Submit unchecked, confirm before
        # we touch the caseload. Different message than single-fire
        # since the impact scales with batch size — a stray uncheck
        # means N students will all need manual Salesforce clicks.
        if not self._confirm_submit_off_or_abort(scenario, batch=True):
            self._append_log(
                f"Batch {scenario.name!r}: aborted at submit-unchecked warning."
            )
            return

        # Pre-flight: CSV missing the student-email column. Warn ONCE
        # per session (skip latch is honored on subsequent fires) so
        # the user can opt to set up the Caseload Tool view, accept
        # the slower fallback for now, or skip the question for the
        # session.
        if not self._confirm_csv_email_present_or_proceed(scenario):
            self._append_log(
                f"Batch {scenario.name!r}: aborted at "
                "CSV-no-email warning."
            )
            return

        self._warn_if_caseload_stale("this batch")

        # Step 1: load the caseload rows. Prefer the CSV cache (~50ms,
        # ~100 fields available); fall back to scroll-load DOM scrape
        # if no CSV is present (slow but always works).
        if self._caseload_rows is not None:
            rows = self._caseload_rows
            age = caseload_csv.csv_age_human(CASELOAD_CSV_PATH)
            self._append_log(
                f"Batch {scenario.name!r}: using cached caseload "
                f"({len(rows)} rows from CSV, {age})"
            )
        else:
            self._append_log(
                f"Batch {scenario.name!r}: no CSV cache; "
                "loading caseload from DOM (5–30s for a full caseload)..."
            )
            rows = self._read_all_caseload_rows_blocking()
            if not rows:
                self._append_log("Batch aborted: couldn't load caseload rows.")
                return

        # Step 2: apply filters. Translate any display-name columns
        # (e.g. 'Last Assigned CI Contact') to the CSV / DOM column
        # the data actually uses (e.g. 'MyCourseContact'). Identity
        # entries pass through unchanged.
        csv_headers = list(rows[0].keys()) if rows else []
        filters = [
            _resolve_filter_columns(f, csv_headers)
            for f in scenario.batch.filters
        ]

        # Safety check: filters referencing columns that aren't in
        # the current CSV are silently mis-evaluated by the engine.
        # Most ops just match nothing (annoying but safe), BUT
        # `is empty` and `is not …` would match EVERYONE — the worst
        # kind of batch bug. Refuse to run until the user fixes the
        # view + refreshes.
        missing = [
            f.get("column", "")
            for f in filters
            if f.get("column") and f.get("column") not in csv_headers
        ]
        if missing:
            self._append_log(
                f"Batch aborted: filter column(s) not in current Caseload "
                f"export: {', '.join(repr(c) for c in missing)}."
            )
            messagebox.showerror(
                "Filter column(s) not in Caseload view",
                f"This action filters on column(s) that aren't in your "
                f"current Caseload export:\n\n  • " +
                "\n  • ".join(missing) +
                "\n\n"
                "Add those columns to your Caseload list view in "
                "Salesforce, then click ↻ Caseload (or ↻ Refresh "
                "columns in the editor) to refresh the cache. Then "
                "try again.",
            )
            return

        # Route any "Task N" filter to its date/count/status facet by op (the
        # original `filters` keep the visible "Task N" column for the safety
        # check above + the review display below; only evaluation is routed).
        eval_filters = [_rewrite_task_filter(f) for f in filters]
        matched = caseload_filter.apply_filters(eval_filters, rows)
        if not matched:
            messagebox.showinfo(
                "No matches",
                f"No students match the filters for {scenario.name!r}.",
            )
            self._append_log("Batch: no matches; nothing to do.")
            return
        self._append_log(f"Filters matched {len(matched)} students.")

        # Step 3: pick the display columns for the review dialog —
        # Name + Student ID (so the user can verify identity at a
        # glance) + every column referenced in the filters (in
        # filter order, deduped).
        display_columns = ["Name", "Student ID"]
        for f in filters:
            col = f.get("column", "")
            if col and col not in display_columns:
                display_columns.append(col)

        # Step 4: prompts FIRST (the new email review modal renders
        # each student's email with these substitutions in place, so
        # they must be collected before review).
        prompt_vars = self._collect_prompt_vars(scenario)
        if prompt_vars is None:
            return  # user cancelled a prompt

        # Step 5: review-and-confirm — combined per-student email
        # preview when the scenario has an email step (FERPA-quality
        # review of every outgoing message); column-based filter
        # review otherwise.
        has_email = scenario.email is not None
        body_overrides: dict = {}
        if has_email:
            filter_summary = ", ".join(
                f"{f.get('column')} {f.get('op')} {f.get('value')!r}".strip()
                for f in scenario.batch.filters
                if f.get("column")
            )
            scenario, confirmed, body_overrides = self._review_emails(
                scenario, matched, prompt_vars, filter_summary,
            )
            if confirmed is None:
                return  # cancelled / nothing selected (already logged)
        elif scenario.batch.preview:
            confirmed = prompt_batch_review(
                self.root, scenario.name, matched, display_columns,
            )
            if confirmed is None:
                self._append_log("Batch cancelled.")
                return
            if not confirmed:
                self._append_log("Batch: 0 students confirmed; nothing to do.")
                return
        else:
            confirmed = matched

        # Steps 6 + 7 (custom-body prompts, clipboard, the per-student
        # loop) are shared with the caseload-panel mini-batch — see
        # _execute_scenario_over_rows.
        self._execute_scenario_over_rows(
            scenario, override, confirmed, prompt_vars,
            has_email=has_email, source="batch", body_overrides=body_overrides,
        )

    def _fire_batch_text(self, scenario: ScenarioConfig) -> None:
        """Batch texting: filter the caseload, render each student's text with
        their own variables, review them all (email-style), then send/schedule
        each via Mongoose — one personalized compose per student, at that
        student's local time. No Salesforce notes or navigation."""
        from tkinter import messagebox
        from src import text_message as tm

        self._warn_if_caseload_stale("this batch")
        # Load rows (CSV cache preferred; DOM fallback).
        if self._caseload_rows is not None:
            rows = self._caseload_rows
            age = caseload_csv.csv_age_human(CASELOAD_CSV_PATH)
            self._append_log(
                f"Batch {scenario.name!r}: using cached caseload "
                f"({len(rows)} rows from CSV, {age})")
        else:
            self._append_log(
                f"Batch {scenario.name!r}: no CSV cache; loading caseload from "
                "DOM (5-30s)…")
            rows = self._read_all_caseload_rows_blocking()
            if not rows:
                self._append_log("Batch aborted: couldn't load caseload rows.")
                return

        # Filter (same engine + safety check as the note/email batch).
        csv_headers = list(rows[0].keys()) if rows else []
        filters = [_resolve_filter_columns(f, csv_headers)
                   for f in scenario.batch.filters]
        missing = [f.get("column", "") for f in filters
                   if f.get("column") and f.get("column") not in csv_headers]
        if missing:
            self._append_log(
                "Batch aborted: filter column(s) not in the Caseload export: "
                f"{', '.join(repr(c) for c in missing)}.")
            messagebox.showerror(
                "Filter column(s) not in Caseload view",
                "These filter column(s) aren't in your current Caseload "
                "export:\n\n  • " + "\n  • ".join(missing) +
                "\n\nAdd them to your list view, click ↻ Caseload, and retry.")
            return
        eval_filters = [_rewrite_task_filter(f) for f in filters]
        matched = caseload_filter.apply_filters(eval_filters, rows)
        if not matched:
            messagebox.showinfo(
                "No matches",
                f"No students match the filters for {scenario.name!r}.")
            self._append_log("Batch: no matches; nothing to do.")
            return
        self._append_log(f"Filters matched {len(matched)} students.")

        # Prompts first, so each per-student render includes them.
        prompt_vars = self._collect_prompt_vars(scenario)
        if prompt_vars is None:
            return

        tcfg = scenario.text
        body_tmpl = tcfg.body
        if not body_tmpl and tcfg.body_file:
            try:
                from src import config as _cfg, email_template
                body_tmpl = email_template.load_template(
                    _cfg.templates_dir() / tcfg.body_file)
            except Exception as e:
                self._append_log(
                    f"Batch text: couldn't load template {tcfg.body_file!r}: "
                    f"{e}", error=True)
                return

        # Batch texts are NOT personalized — one shared message per group. Group
        # the matched students by (inbox, timezone): the inbox is course-scoped
        # and one Mongoose compose sends ONE body at ONE time, so each
        # (course-inbox, timezone) becomes a single multi-recipient scheduled
        # compose. (Personalized bulk would need Mongoose merge fields — later.)
        prompt_vars = prompt_vars or {}
        groups: dict = {}
        order: list = []
        not_opted = 0
        for row in matched:
            # Skip students not opted in to texting — they aren't textable in
            # Mongoose anyway, so including them only stalls the batch.
            if not self._texting_opted_in(row):
                not_opted += 1
                continue
            rv = self._text_vars_from_row(row)
            course = rv["course_code"]
            inbox_label = tcfg.inbox_label or (
                f"{course} Inbox" if course else "")
            tz = (row.get("Timezone") or "").strip()
            mobile = tm.normalize_phone(row.get("MobilePhone") or "")
            name = rv["full_name"] or row.get("Name", "")
            key = (inbox_label, tz)
            g = groups.get(key)
            if g is None:
                g = {"inbox_label": inbox_label, "tz": tz, "course": course,
                     "mobiles": [], "names": [], "no_mobile": []}
                groups[key] = g
                order.append(key)
            g["names"].append(name)
            if mobile:
                g["mobiles"].append(mobile)
            else:
                g["no_mobile"].append(name)

        if not_opted:
            self._append_log(
                f"Batch text: skipped {not_opted} student(s) not opted in to "
                "texting.")
        if not order:
            self._append_log(
                "Batch text: no opted-in students to text after filtering.")
            return

        # One review entry per group: the shared (generic) message + slot.
        scheduled = bool(tcfg.schedule)
        entries: list[dict] = []
        for key in order:
            g = groups[key]
            gvars = {"course_code": g["course"]}  # group-level vars only
            gvars.update(prompt_vars)
            body = tm.render_message(body_tmpl, gvars)
            issues: list[str] = []
            when_str, sch_payload, sched_name = "now", None, ""
            if tcfg.schedule:
                slot = tm.compute_schedule_slot(
                    g["tz"], self.TEAM_IANA, target_hour=tcfg.target_hour)
                if slot is None:
                    issues.append(f"unknown tz {g['tz']!r}")
                else:
                    when_str = f"{slot.student_local_str} (local)"
                    sch_payload = {
                        "date_str": slot.date_str, "hour12": slot.hour12,
                        "minute": slot.minute, "ampm": slot.ampm,
                        "student_local_str": slot.student_local_str,
                    }
                    sched_name = f"{g['course']} batch".strip() or "Scheduled text"
            if not g["mobiles"]:
                issues.append("no recipients with a mobile")
            elif g["no_mobile"]:
                issues.append(f"{len(g['no_mobile'])} without a mobile")
            if tm.over_length(body):
                issues.append(f"{tm.over_length(body)} over limit")
            if "{{" in body and "}}" in body:
                # Batch is generic — a leftover {{var}} (e.g. {{first_name}})
                # won't be personalized; surface it so it's caught in review.
                issues.append("unresolved {{variable}} — batch isn't personalized")
            label = f"{g['inbox_label'] or 'inbox?'}  ·  {g['tz'] or 'tz?'}"
            entries.append({
                "name": label,
                "course_code": g["course"],
                "mobile": f"{len(g['mobiles'])} recipient(s)",
                "recipients_str": ", ".join(g["names"]),
                "when_str": when_str, "body": body, "issues": issues,
                "inbox_label": g["inbox_label"], "schedule": sch_payload,
                "schedule_name": sched_name, "mobiles": g["mobiles"],
            })

        filter_summary = ", ".join(
            f"{f.get('column')} {f.get('op')} {f.get('value')!r}".strip()
            for f in scenario.batch.filters if f.get("column"))
        selected = prompt_batch_text_review(
            self.root, scenario.name, entries, filter_summary,
            scheduled=scheduled)
        if selected is None:
            self._append_log("Batch text cancelled.")
            return
        if not selected:
            self._append_log("Batch text: 0 selected; nothing to do.")
            return

        # One compose per selected group. Lock the browser for the loop (review
        # already happened, so the scrim hides nothing the user needs).
        self._lock_browser_for_run()
        sent = 0
        ngroups = len(selected)
        try:
            for n, idx in enumerate(selected, 1):
                e = entries[idx]
                if not e["mobiles"]:
                    self._append_log(
                        f"  text {n}/{ngroups}: skipped {e['name']} "
                        "(no recipients with a mobile).", error=True)
                    continue
                if any(i.startswith("unknown tz") for i in e["issues"]):
                    self._append_log(
                        f"  text {n}/{ngroups}: skipped {e['name']} "
                        "(unknown timezone).", error=True)
                    continue
                payload = {
                    "body": e["body"], "recipients": e["mobiles"],
                    "inbox_label": e["inbox_label"], "schedule": e["schedule"],
                    "schedule_name": e["schedule_name"], "commit": True,
                }
                self._append_log(
                    f"  text {n}/{ngroups}: {e['name']} — "
                    f"{len(e['mobiles'])} recipient(s) ({e['when_str']})…")
                res = self._send_text_blocking(payload)
                if not res or res.get("error"):
                    self._append_log(
                        f"  text failed [{e['name']}]: "
                        f"{(res or {}).get('error')}", error=True)
                else:
                    sent += len(e["mobiles"])
        finally:
            self._unlock_browser_after_run()
        self._append_log(
            f"Batch text complete: {sent} recipient(s) in "
            f"{len(selected)} group(s) {'scheduled' if scheduled else 'sent'}.")

    @staticmethod
    def _row_name_and_query(row: dict) -> tuple[str, str]:
        """(display name, find query) for a caseload row. The query prefers
        the unambiguous Student ID — CSV header 'StudentID', with the
        spaced 'Student ID' alias as a fallback — then the name."""
        name = str(row.get("Name", "")).strip()
        sid = str(row.get("StudentID", "") or row.get("Student ID", "")).strip()
        return name, (sid or name)

    def _execute_scenario_over_rows(
        self, scenario: ScenarioConfig, override: str,
        confirmed: list[dict], prompt_vars: dict, *,
        has_email: bool, source: str = "batch",
        body_overrides: Optional[dict] = None,
    ) -> None:
        """Shared execution core for firing a scenario across many
        students — the full caseload batch AND the panel's hand-picked
        mini-batch. Gathers per-note custom bodies + clipboard, then loops
        fast-find → auto-send email (if configured) → file note. The
        activity log is the progress display. `source` is the noun used in
        log lines ('batch' / 'selection'). `body_overrides` maps a row's
        position in `confirmed` to a hand-edited email body (from the
        reviewer's Edit-body button)."""
        body_overrides = body_overrides or {}
        # Step 6: clipboard FIRST (read once up front — Tk + PIL aren't safe
        # on the worker thread) so it can be folded into the additional-text
        # review, then the per-note custom-body prompts. Gathered AFTER
        # confirmation so cancelled runs don't waste typing.
        clipboard = ""
        if any(n.append_clipboard for n in scenario.notes):
            clipboard = self._read_clipboard_content()
        custom_bodies: dict[int, str] = {}
        for i, n in enumerate(scenario.notes):
            if not n.enter_additional_text:
                continue
            label = f"Note {i + 1} (applies to all {len(confirmed)} students)"
            prefill = n.body
            if n.append_clipboard and clipboard:
                sep = "\n" if prefill and not prefill.endswith("\n") else ""
                prefill = f"{prefill}{sep}{clipboard}"
            edited = prompt_additional_text(self.root, label, prefill)
            if edited is None:
                self._append_log(f"{label}: cancelled; {source} not started.")
                return
            custom_bodies[i] = edited

        total = len(confirmed)

        # Lock the browser for the (non-interactive) loop so a stray user
        # click can't change the active record mid-run — the exact failure
        # mode that produced "No visible note panel" skips. All modals
        # (prompts / review) ran above, so the scrim can't hide anything
        # the user needs to act on. Unlocked in the finally below.
        try:
            self.worker.set_browser_enabled(False)
        except Exception:
            pass
        self._show_browser_lock()

        # Step 7: loop. For each student: fast-find → auto-send
        # email (if configured) → file note.
        processed = 0
        skipped: list[tuple[str, str]] = []
        try:
            for idx, row in enumerate(confirmed, start=1):
                student_name, query = self._row_name_and_query(row)
                self._append_log(
                    f"--- {source} {idx}/{total}: {student_name!r} ---"
                )

                # 7a. Fast-find: row filter on Student ID, then click.
                # The worker also scrapes mailto: emails off the row
                # BEFORE clicking + the contact card AFTER, so we can
                # fill in addresses the CSV view didn't include.
                click_ok, row_emails = self._click_match_by_filter_blocking(
                    query, expected_name=student_name,
                )
                if not click_ok:
                    self._append_log(
                        f"Skipping {student_name!r}: fast-find failed."
                    )
                    skipped.append((student_name, "find/click failed"))
                    continue

                # 7b. Auto-send email (if configured). Failure skips the
                # note for this student but doesn't halt the run.
                # Prompt vars merge into student_ctx so {{summary}}-
                # style placeholders in the email body / subject / to
                # resolve against the run-wide prompt input.
                if has_email:
                    # Build context from the CSV row, then fill any gaps
                    # from the row-level mailto + contact-card scrape we
                    # did during fast-find. The two sources together
                    # mean a user whose Caseload view doesn't include
                    # email columns still gets working emails —
                    # mailto carries the PM, the contact card surfaces
                    # the student, the CSV provides everything else.
                    ctx_info = self._ctx_from_csv_row(row)
                    if not ctx_info["student_email"] and row_emails.get("student_email"):
                        ctx_info["student_email"] = row_emails["student_email"]
                    if not ctx_info["pm_email"] and row_emails.get("pm_email"):
                        ctx_info["pm_email"] = row_emails["pm_email"]
                    # One-time diagnostic only if BOTH sources came back
                    # empty — that points at a Salesforce config issue
                    # the user has to fix in their list view.
                    if (not ctx_info["student_email"]
                            and not self._email_diag_logged):
                        self._email_diag_logged = True
                        present = _email_columns_present(row)
                        if present:
                            self._append_log(
                                "CSV email columns found but not recognized: "
                                + ", ".join(repr(c) for c in present) + ". "
                                "Either rename your Caseload-view columns to "
                                "'Student Email' / 'Mentor Email', or tell the "
                                "launcher dev to add these names to the alias "
                                "list."
                            )
                        else:
                            self._append_log(
                                "Neither the CSV nor the row's Email-Student "
                                "link surfaced a student email. The PM "
                                "scrape may still have succeeded — check the "
                                "next attempt's log line."
                            )
                    ctx_info = {**ctx_info, **prompt_vars}
                    if not self._send_scenario_email(
                        scenario.email, ctx_info, auto_send=True,
                        body_html_override=body_overrides.get(idx - 1),
                    ):
                        skipped.append((student_name, "auto-send failed"))
                        continue

                # 7c. Notes — block until the worker finishes this RUN.
                # Worker returns True iff the run completed without
                # errors; only count those as truly processed.
                if self._submit_scenario_blocking(
                    scenario, override, clipboard, custom_bodies,
                    prompt_vars=prompt_vars,
                ):
                    processed += 1
                else:
                    skipped.append((student_name, "note fill failed"))
        finally:
            # Always unlock the browser when the loop ends (done, error,
            # or a mid-loop exception).
            self._hide_browser_lock()
            try:
                self.worker.set_browser_enabled(True)
            except Exception:
                pass

        self._append_log(
            f"{source.capitalize()} {scenario.name!r} complete: "
            f"{processed}/{total} processed, {len(skipped)} skipped."
        )
        if skipped:
            for name, reason in skipped:
                self._append_log(f"  skipped: {name!r} ({reason})")

    def _fire_on_selected(
        self, scenario: ScenarioConfig, rows: list[dict],
        near: Optional[tuple] = None,
    ) -> None:
        """Fire a (non-batch) scenario across a hand-picked set of caseload
        rows (the panel's checkbox selection) — a mini-batch. Reuses the
        batch execution core, including the FERPA per-student email-review
        modal when the scenario has an email step."""
        if self._is_busy:
            self._append_log(
                f"Busy — wait for the current task to finish before "
                f"firing {scenario.name!r}."
            )
            return
        if not self.worker.ready_event.is_set():
            self._append_log("Browser not ready yet — wait and try again.")
            return
        if scenario.batch is not None:
            self._append_log(
                f"{scenario.name!r} is a batch action; fire it from the "
                "main window, not the panel selection."
            )
            return
        rows = [r for r in (rows or []) if r]
        if not rows:
            self._append_log("No students selected.")
            return

        # Edit-note action on a SINGLE student → route through the
        # per-student fire path, which surfaces the unified fire-time edit
        # dialog (body / course / academic activities / Essential Action).
        # (Mini-batch over multiple rows doesn't do the per-student dialog.)
        if (len(rows) == 1 and any(
                getattr(n, "enter_additional_text", False)
                for n in scenario.notes)):
            name, query = self._row_name_and_query(rows[0])
            override = self.course_var.get().strip()
            course_hint = str(rows[0].get("CourseCode", "")
                              or rows[0].get("Course Code", "")).strip()
            self._set_busy(f"Running {scenario.name}…")
            try:
                self._fire_per_student(
                    scenario, override, prenav_query=query,
                    prenav_label=name, course_hint=course_hint)
            finally:
                self._set_idle()
            return

        # Pre-flight submit-unchecked warning (batch wording — the impact
        # scales with the selection size).
        if not self._confirm_submit_off_or_abort(scenario, batch=True):
            self._append_log(
                f"{scenario.name!r} on selection: aborted at "
                "submit-unchecked warning."
            )
            return

        self._warn_if_caseload_stale("this fire")

        override = self.course_var.get().strip()
        has_email = scenario.email is not None

        # Prompts first — the email review modal renders each student's
        # email with these substitutions in place.
        prompt_vars = self._collect_prompt_vars(scenario)
        if prompt_vars is None:
            return  # user cancelled a prompt

        # Review/confirm. With an email step, reuse the per-student FERPA
        # review modal (same as batch), including the fire-time template
        # dropdown; otherwise a single count/name confirm.
        body_overrides: dict = {}
        if has_email:
            n0 = len(rows)
            summary = (
                f"{n0} hand-picked from the caseload panel" if n0 != 1
                else self._row_name_and_query(rows[0])[0]
            )
            scenario, confirmed, body_overrides = self._review_emails(
                scenario, rows, prompt_vars, summary,
            )
            if confirmed is None:
                return  # cancelled / nothing selected (already logged)
        else:
            n = len(rows)
            who = (self._row_name_and_query(rows[0])[0] if n == 1
                   else f"{n} selected students")
            if not ask_yes_no_topmost(
                self.root, "Fire action?",
                f"Fire {scenario.name!r} on {who}?",
                yes_label="Fire", no_label="Cancel", at=near,
            ):
                self._append_log(f"{scenario.name!r} on selection: cancelled.")
                return
            confirmed = rows

        self._append_log(
            f"--- Firing {scenario.name!r} on {len(confirmed)} selected "
            f"student(s) ---"
        )
        self._set_busy(
            f"Running {scenario.name} on {len(confirmed)} students…"
        )
        try:
            self._execute_scenario_over_rows(
                scenario, override, confirmed, prompt_vars,
                has_email=has_email, source="selection",
                body_overrides=body_overrides,
            )
        finally:
            self._set_idle()

    def _list_matches_blocking(self, query: str) -> list[str]:
        """Run a LIST_MATCHES on the worker and block until results.
        wait_variable spins a nested mainloop so the dialog stays
        interactive while we wait."""
        done_var = tk.BooleanVar(value=False)
        holder: dict = {"names": []}

        def on_results(names: list[str]) -> None:
            def set_main() -> None:
                holder["names"] = names
                done_var.set(True)
            try:
                self.root.after(0, set_main)
            except Exception:
                holder["names"] = names
                done_var.set(True)

        self.worker.submit_list_matches(query, on_results)
        self.root.wait_variable(done_var)
        return holder["names"]

    def _read_eas_blocking(self) -> list[dict]:
        """Read the active student's open Essential Actions, blocking the
        fire flow (nested mainloop) until the worker returns."""
        done_var = tk.BooleanVar(value=False)
        holder: dict = {"eas": []}

        def on_done(res) -> None:
            def set_main() -> None:
                holder["eas"] = (res or {}).get("eas") or []
                if (res or {}).get("error"):
                    self._append_log(
                        f"Essential Actions read failed: {res['error']}",
                        error=True)
                done_var.set(True)
            try:
                self.root.after(0, set_main)
            except Exception:
                holder["eas"] = (res or {}).get("eas") or []
                done_var.set(True)

        self.worker.submit_read_essential_actions(on_done)
        self.root.wait_variable(done_var)
        return holder["eas"]

    def _read_ea_dashboard_blocking(self) -> list:
        """Scrape the cross-caseload EA dashboard, blocking until done."""
        done_var = tk.BooleanVar(value=False)
        holder: dict = {"eas": []}

        def on_done(res) -> None:
            def set_main() -> None:
                holder["eas"] = (res or {}).get("eas") or []
                if (res or {}).get("error"):
                    self._append_log(
                        f"EA dashboard scrape failed: {res['error']}",
                        error=True)
                done_var.set(True)
            try:
                self.root.after(0, set_main)
            except Exception:
                holder["eas"] = (res or {}).get("eas") or []
                done_var.set(True)

        self.worker.submit_read_ea_dashboard(on_done)
        self.root.wait_variable(done_var)
        return holder["eas"]

    def _apply_ea_scrape(self, eas: list) -> None:
        """Index the scraped EAs by Student ID (one per student — the
        dashboard is designed that way), warn loudly if a student appears
        twice, stamp the freshness time, and refresh the panel."""
        from datetime import datetime
        by_sid: dict = {}
        dups: list = []
        for ea in (eas or []):
            sid = (ea.get("student_id") or "").strip()
            if not sid:
                continue
            if sid in by_sid:
                dups.append(ea.get("name") or sid)
                continue  # keep the first
            by_sid[sid] = ea
        self._ea_by_sid = by_sid
        self._ea_mtime = datetime.now()
        for who in dups:
            self._append_log(
                f"⚠ Essential Actions dashboard listed {who!r} more than "
                "once — showing only the first. The design expects one EA "
                "per student; check this student.", error=True)
        self._append_log(
            f"Essential Actions: {len(by_sid)} student(s) with an open EA.")
        self._apply_ea_to_rows()
        self._refresh_caseload_panel()

    def _apply_ea_to_rows(self) -> None:
        """Inject the scraped EA reason into each cached caseload row as a
        synthetic 'EssentialAction' column (matched by Student ID), so it
        shows + filters in the viewer. Re-run after every cache reload."""
        rows = self._caseload_rows or []
        by_sid = getattr(self, "_ea_by_sid", {}) or {}
        for r in rows:
            sid = str(r.get("StudentID", "") or r.get("Student ID", "")).strip()
            ea = by_sid.get(sid)
            r["EssentialAction"] = (ea.get("reason", "") if ea else "")

    # Curated value vocabulary for the task-status filter columns (the
    # aggregate "Task Status" holds combos, so deriving from data would be
    # messy — offer the atomic states instead).
    _TASK_STATUS_VALUES = ["Passed", "Returned", "In Process", "Submitted"]

    def _filter_value_suggestions(self, display_col: str, op: str = "") -> list:
        """Suggested values for a filter row's value field, given the chosen
        column + operator.
        - Date/number ops → `{Other Column}` comparison refs of the matching
          type (so you can compare two columns, e.g. last contact vs a task
          date). Excludes the column itself + hidden facet helpers.
        - Text ops → a small fixed vocabulary: task-status columns get the
          curated states; any other column with few distinct values
          (timezone, Essential Action, momentum…) gets those. [] otherwise."""
        rows = getattr(self, "_caseload_rows", None) or []
        if not rows or not display_col:
            return []
        headers = list(rows[0].keys())
        sop = caseload_filter.normalize_op(op)
        date_ops = {"before", "after", "on", "on_or_before", "on_or_after",
                    "within"}
        num_ops = {"gt", "lt", "gte", "lte"}
        if sop in date_ops or sop in num_ops:
            want = "date" if sop in date_ops else "number"
            sel = caseload_csv.resolve_column(display_col, headers)
            sample = rows[:40]
            refs = []
            for h in headers:
                if _is_task_facet_col(h) or h == sel:
                    continue
                ctype = caseload_filter.sniff_column_type(
                    [str(r.get(h, "") or "") for r in sample])
                if ctype == want:
                    refs.append("{" + caseload_csv.display_for_column(h) + "}")
            return sorted(refs)
        # Text ops → small-vocabulary value suggestions.
        header = caseload_csv.resolve_column(display_col, headers)
        if re.fullmatch(r"Task\d+(Status)?", header or ""):
            return list(self._TASK_STATUS_VALUES)
        vals: set = set()
        for r in rows:
            v = str(r.get(header, "") or "").strip()
            if v:
                vals.add(v)
            if len(vals) > 25:
                return []  # too many distinct values → keep it free-text
        return sorted(vals)

    def _apply_task_status_to_rows(self) -> None:
        """Inject HIDDEN per-task facet columns into each cached caseload row
        so a single visible 'Task N' column can be filtered by date, by
        submission count, OR by status (the operator picks which — see
        _rewrite_task_filter). For each task number N present:
          Task{N}Date   — YYYY-MM-DD from the CSV Task cell (always there)
          Task{N}Count  — submission count from the CSV Task cell
          Task{N}Status — Passed/Returned/In Process from the live scrape
        Date + count come from the CSV so they filter WITHOUT a scrape; status
        needs the background pass. Re-run after every cache reload AND after
        the scrape (mirrors _apply_ea_to_rows). Matched by Student ID."""
        rows = self._caseload_rows or []
        if not rows:
            return
        cache = getattr(self, "_task_status_cache", None) or {}
        # Task numbers to build facets for = CSV Task columns that actually
        # carry data (skip empty Task4-15) ∪ any task seen in the scrape;
        # default 1-3 so the facets exist before the first export/scrape.
        keys = list(rows[0].keys())
        csv_tnums = {m.group(1) for k in keys
                     for m in [re.fullmatch(r"Task(\d+)", k)] if m
                     and any((r.get(k) or "").strip() for r in rows)}
        cache_tnums = {t for tasks in cache.values() for t in tasks}
        tnums = sorted(csv_tnums | cache_tnums, key=lambda x: int(x)) \
            or ["1", "2", "3"]
        for r in rows:
            sid = str(r.get("StudentID", "") or r.get("Student ID", "")).strip()
            tasks = cache.get(sid) or {}
            for n in tnums:
                # Date + count from the CSV Task cell (e.g. "2026-09-13 (1)").
                _, date, attempts = parse_task_status(r.get(f"Task{n}", ""))
                r[f"Task{n}Date"] = date
                r[f"Task{n}Count"] = str(attempts) if date else ""
                # Status from the live cache (blank until the scrape lands).
                info = tasks.get(n)
                st = info.get("state") if info else None
                r[f"Task{n}Status"] = TASK_STATE_LABELS.get(st, "") if st else ""

    def _choose_ea_attachment(self, eas: list) -> tuple:
        """Fire-time dialog: show the student's open EAs and let the user
        attach ONE to this note (+ optional close), or file normally.
        Returns (mode, ea) where mode is 'attach' | 'skip' | 'cancel' and
        ea = (reason, course, close) when attaching."""
        dialog = ctk.CTkToplevel(self.root)
        dialog.title("Essential Actions")
        dialog.transient(self.root)
        dialog.attributes("-topmost", True)
        dialog.grab_set()
        dialog.resizable(False, False)
        dialog.lift()
        dialog.focus_force()
        result = {"mode": "cancel", "ea": None}
        ctk.CTkLabel(
            dialog,
            text=f"This student has {len(eas)} open Essential Action(s)",
            font=ctk.CTkFont(size=14, weight="bold"), anchor="w",
        ).pack(fill="x", padx=20, pady=(18, 2))
        ctk.CTkLabel(
            dialog, text="Attach one to this note?",
            font=ctk.CTkFont(size=12), anchor="w",
            text_color=("gray35", "gray70"),
        ).pack(fill="x", padx=20, pady=(0, 8))

        sel = ctk.StringVar(value="skip")
        box = ctk.CTkFrame(dialog, fg_color=("gray95", "gray18"))
        box.pack(fill="x", padx=20, pady=(0, 8))
        ctk.CTkRadioButton(
            box, text="Don't attach — file a normal note",
            variable=sel, value="skip",
        ).pack(anchor="w", padx=10, pady=(8, 2))
        for i, ea in enumerate(eas):
            lbl = ea.get("reason", "")
            if ea.get("course"):
                lbl += f"   ({ea['course']})"
            prog = ea.get("event_progress")
            if prog:
                lbl += f"   · {prog}"
            ctk.CTkRadioButton(
                box, text=lbl, variable=sel, value=str(i),
            ).pack(anchor="w", padx=10, pady=2)
        ctk.CTkFrame(box, fg_color="transparent", height=4).pack()

        close_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            dialog, text="Close the Essential Action when the note is saved",
            variable=close_var, font=ctk.CTkFont(size=12),
        ).pack(anchor="w", padx=20, pady=(0, 12))

        def _cont() -> None:
            v = sel.get()
            if v == "skip":
                result["mode"] = "skip"
            else:
                ea = eas[int(v)]
                result["mode"] = "attach"
                result["ea"] = (ea.get("reason", ""), ea.get("course", ""),
                                bool(close_var.get()))
            _close()

        def _cancel() -> None:
            result["mode"] = "cancel"
            _close()

        def _close() -> None:
            try: dialog.grab_release()
            except Exception: pass
            try: dialog.destroy()
            except Exception: pass

        row = ctk.CTkFrame(dialog, fg_color="transparent")
        row.pack(fill="x", padx=20, pady=(0, 16))
        ctk.CTkButton(row, text="Continue", width=110, command=_cont).pack(
            side="left", padx=4)
        ctk.CTkButton(
            row, text="Cancel", width=110, command=_cancel,
            **SECONDARY_BTN_KWARGS,
        ).pack(side="right", padx=4)
        dialog.bind("<Escape>", lambda _e: _cancel())
        dialog.protocol("WM_DELETE_WINDOW", _cancel)
        self.root.wait_window(dialog)
        return (result["mode"], result["ea"])

    def _navigate_for_fire_blocking(self, query: str) -> bool:
        """Fast-navigate to a student (Shift+X switch, no reload) and
        block until the note panel is loaded. Used by fire-from-row so
        the note files against a ready record without the slower
        list-matches+click path. Returns True once the panel is ready."""
        done_var = tk.BooleanVar(value=False)
        holder: dict = {"ok": False}

        def on_done(ok: bool) -> None:
            def set_main() -> None:
                holder["ok"] = ok
                done_var.set(True)
            try:
                self.root.after(0, set_main)
            except Exception:
                holder["ok"] = ok
                done_var.set(True)

        self.worker.submit_find_and_settle(query, on_done)
        self.root.wait_variable(done_var)
        return holder["ok"]

    def _click_match_blocking(self, name: str) -> bool:
        """Click the chosen match on the worker and block until the
        navigation has settled."""
        done_var = tk.BooleanVar(value=False)
        holder: dict = {"success": False}

        def on_done(success: bool) -> None:
            def set_main() -> None:
                holder["success"] = success
                done_var.set(True)
            try:
                self.root.after(0, set_main)
            except Exception:
                holder["success"] = success
                done_var.set(True)

        self.worker.submit_click_match(name, on_done)
        self.root.wait_variable(done_var)
        return holder["success"]

    def _download_caseload_csv_blocking(self) -> tuple[bool, str]:
        """Ask the worker to download the caseload CSV. Blocks until
        it's saved (or fails). Reloads the in-memory cache on success."""
        done_var = tk.BooleanVar(value=False)
        holder: dict = {"success": False, "message": ""}

        def on_done(success: bool, message: str) -> None:
            def set_main() -> None:
                holder["success"] = success
                holder["message"] = message
                done_var.set(True)
            try:
                self.root.after(0, set_main)
            except Exception:
                holder["success"] = success
                holder["message"] = message
                done_var.set(True)

        self.worker.submit_download_caseload_csv(CASELOAD_CSV_PATH, on_done)
        self.root.wait_variable(done_var)
        if holder["success"]:
            self._reload_caseload_cache(silent=False)
        return holder["success"], holder["message"]

    def _click_match_by_filter_blocking(
        self, query: str, expected_name: str = "",
    ) -> tuple[bool, dict]:
        """Batch fast path: type the unique value into Caseload's row
        filter and click the result. Returns `(success, row_info)`
        where row_info carries `student_email` and `pm_email`
        scraped from the row before the click (and the contact card
        after). Either email can be empty if the page didn't surface
        it; caller decides how to handle the gap."""
        done_var = tk.BooleanVar(value=False)
        holder: dict = {
            "success": False,
            "info": {"pm_email": "", "student_email": ""},
        }

        def on_done(success: bool, info: dict) -> None:
            def set_main() -> None:
                holder["success"] = success
                holder["info"] = info or holder["info"]
                done_var.set(True)
            try:
                self.root.after(0, set_main)
            except Exception:
                holder["success"] = success
                holder["info"] = info or holder["info"]
                done_var.set(True)

        self.worker.submit_click_match_by_filter(
            query, on_done, expected_name=expected_name,
        )
        self.root.wait_variable(done_var)
        return holder["success"], holder["info"]

    @staticmethod
    def _email_template_files() -> list[str]:
        """Sorted .html template filenames in the templates dir."""
        try:
            return sorted(p.name for p in templates_dir().glob("*.html"))
        except Exception:
            return []

    def _run_email_review(self, scenario: ScenarioConfig, render, summary: str):
        """Core FERPA email-preview review. `render(scenario)` returns the
        list of preview dicts (re-invoked when the fire-time template
        dropdown changes). Returns (scenario, selected_indices):
        selected_indices is None on cancel / empty selection; `scenario`
        carries any template the user chose in the dropdown."""
        from dataclasses import replace as _replace
        rendered = render(scenario)
        pick = bool(scenario.email and scenario.email.pick_template)
        templates = self._email_template_files() if pick else None
        cur_tpl = scenario.email.body_html_file if pick else ""

        def _on_tpl(tpl: str) -> list[dict]:
            return render(_replace(
                scenario, email=_replace(scenario.email, body_html_file=tpl)))

        selected, chosen_tpl, edits = prompt_batch_email_review(
            self.root, scenario.name, rendered, summary,
            templates=templates, current_template=cur_tpl,
            on_template_change=(_on_tpl if pick else None),
        )
        if not selected:
            return scenario, None, {}
        if pick and chosen_tpl and chosen_tpl != scenario.email.body_html_file:
            scenario = _replace(
                scenario,
                email=_replace(scenario.email, body_html_file=chosen_tpl))
            self._append_log(f"Using email template {chosen_tpl!r}.")
        return scenario, selected, edits

    def _review_emails(
        self, scenario: ScenarioConfig, rows: list[dict],
        prompt_vars: dict, summary: str,
    ) -> tuple:
        """Show the email previewer for a list of CSV rows (batch /
        selection). Returns (scenario, confirmed_rows); confirmed_rows is
        None when the user cancelled or selected nobody."""
        from src import outlook_email
        user_info = outlook_email.get_user_info()
        self._append_log(
            f"Rendering {len(rows)} email preview(s) for review…")

        def render(scn: ScenarioConfig) -> list[dict]:
            return [
                self._build_email_preview_data(scn, row, prompt_vars, user_info)
                for row in rows
            ]

        scenario, selected, edits = self._run_email_review(
            scenario, render, summary)
        if selected is None:
            self._append_log(
                f"{scenario.name!r}: email review cancelled / nobody selected.")
            return scenario, None, {}
        confirmed = [rows[i] for i in selected]
        # Re-key any hand-edited bodies from reviewer-index to the position
        # within `confirmed`, which is what the execution loop iterates.
        body_overrides = {
            pos: edits[i] for pos, i in enumerate(selected) if i in edits
        }
        self._append_log(
            f"Email review confirmed: {len(confirmed)} of {len(rows)} "
            "student(s)."
            + (f" ({len(body_overrides)} hand-edited)" if body_overrides
               else "")
        )
        return scenario, confirmed, body_overrides

    def _build_email_preview_data(
        self,
        scenario: ScenarioConfig,
        row: dict,
        prompt_vars: dict,
        user_info: dict,
        *,
        ctx_override: Optional[dict] = None,
    ) -> dict:
        """Render one student's email for the batch review modal.
        Returns the dict shape `prompt_batch_email_review` consumes.

        Renders against CSV row data + the launcher user's Outlook
        identity — NOT against the per-student DOM scrape (which
        requires navigation we haven't done yet at review time).
        Missing addresses are surfaced as 'no_email' issues so the
        reviewer sees them at a glance; the actual send-time path
        still tries the mailto + contact-card scrape to fill the
        gaps before composing.

        `user_info` is the cached Outlook CurrentUser dict passed in
        by the caller — avoids re-dispatching COM per student."""
        email_cfg = scenario.email
        # Base context from the CSV row (batch/selection) OR a prebuilt
        # scraped context (single main-window fire, which has no CSV row),
        # augmented with Outlook user identity (so {{user_name}} /
        # {{user_email}} resolve) and the run-wide prompts.
        ctx = (dict(ctx_override) if ctx_override is not None
               else self._ctx_from_csv_row(row))
        ctx["user_name"] = user_info.get("name", "")
        ctx["user_email"] = user_info.get("email", "")
        ctx = {**ctx, **prompt_vars}

        # PM email self-fallback — same logic as _send_scenario_email,
        # mirrored here so the review shows what the send will use.
        cc_is_self = False
        if (email_cfg.cc_pm
                and not ctx.get("pm_email")
                and ctx.get("pm_name")
                and user_info.get("email")
                and _names_loosely_match(
                    ctx.get("pm_name", ""), user_info.get("name", "")
                )):
            ctx["pm_email"] = user_info["email"]
            cc_is_self = True
        elif email_cfg.cc_pm and ctx.get("pm_email") and user_info.get("email"):
            # Even if pm_email came from CSV, flag self-CC for the hint.
            if ctx["pm_email"].strip().lower() == user_info["email"].strip().lower():
                cc_is_self = True

        # Render the template. Captures any render error so the
        # reviewer sees it instead of silently dropping that row.
        render_error = ""
        body_html = ""
        subject = ""
        to = ""
        cc = ""
        try:
            template_path = templates_dir() / email_cfg.body_html_file
            template_html = email_template.load_template(template_path)
            body_html = email_template.render(template_html, ctx)
            body_html = email_template.wrap_with_font(
                body_html, email_cfg.font_family, email_cfg.font_size,
            )
            subject = email_template.render_plain(email_cfg.subject, ctx)
            if email_cfg.to:
                to = email_template.render_plain(email_cfg.to, ctx).strip()
            else:
                to = ctx.get("student_email", "") or ""
            if email_cfg.cc_pm:
                cc = ctx.get("pm_email", "") or ""
        except Exception as e:
            render_error = f"{type(e).__name__}: {e}"

        issues: list[str] = []
        if render_error:
            issues.append(f"render error: {render_error}")
        if not render_error and not to:
            issues.append(
                "no student email (will look up from Salesforce at send time)"
            )
        # Detect unresolved {{var}} survivors in body or subject.
        leftover = re.findall(r"\{\{\s*(\w+)\s*\}\}", body_html + " " + subject)
        if leftover:
            unique = sorted(set(leftover))
            issues.append(
                f"unresolved variable(s): " + ", ".join(unique)
            )

        return {
            "name": ctx.get("full_name", "") or row.get("Name", ""),
            "student_id": ctx.get("student_id", ""),
            "course_code": ctx.get("course_code", ""),
            "to": to,
            "cc": cc,
            "cc_is_self": cc_is_self,
            "cc_configured": bool(email_cfg.cc_pm),
            "subject": subject,
            "body_html": body_html,
            "render_error": render_error,
            "issues": issues,
        }

    def _ctx_from_csv_row(self, row: dict) -> dict:
        """Build the variable dict an email/note render needs, sourced
        from a single caseload CSV row. Used in batch mode where the
        DOM-scrape path would fail (after fast-find clicks a student
        we're on their record page, not the Caseload list — the
        scrape sees no Caseload table and returns blank emails).

        Tries each catalogued column name (DISPLAY_TO_CSV pairs +
        common display labels) and falls back to "" for any field
        that isn't present. Emails go through the longer alias list
        in `_CSV_STUDENT_EMAIL_COLS` / `_CSV_PM_EMAIL_COLS` since
        their names vary the most across user-configured views.
        `user_name` / `user_email` are NOT set here — _send_scenario_
        email tops those up from Outlook's CurrentUser."""
        def _first(*keys: str) -> str:
            for k in keys:
                v = row.get(k, "")
                if v is not None:
                    s = str(v).strip()
                    if s:
                        return s
            return ""

        name = _first("Name")
        first, _, last = name.partition(" ")
        return {
            "full_name": _capitalize_name(name),
            "first_name": _capitalize_name(first),
            # Preferred name, falling back to the first name when blank.
            "preferred_name": _capitalize_name(_first(
                "stuprename", "Student Preferred Name",
                "PreferredName", "Preferred Name") or first),
            "last_name": _capitalize_name(last),
            "student_email": _first_present_value(
                row, _CSV_STUDENT_EMAIL_COLS,
            ),
            "student_id": _first("StudentID", "Student ID"),
            "course_code": _first("CourseCode", "Course Code"),
            "pm_name": _capitalize_name(_first("MentorName", "Program Mentor")),
            "pm_email": _first_present_value(row, _CSV_PM_EMAIL_COLS),
            "program_name": _first("ProgramName", "Program Name"),
        }

    def _get_student_context_blocking(self, name_hint: str = "") -> Optional[dict]:
        """Ask the worker to read the active student's context (email,
        course code, PM, etc.) and block on the main thread until it
        comes back. Used by the email step before we hand off to
        Outlook."""
        done_var = tk.BooleanVar(value=False)
        holder: dict = {"info": None}

        def on_done(info: Optional[dict]) -> None:
            def set_main() -> None:
                holder["info"] = info
                done_var.set(True)
            try:
                self.root.after(0, set_main)
            except Exception:
                holder["info"] = info
                done_var.set(True)

        self.worker.submit_get_student_context(on_done, name_hint=name_hint)
        self.root.wait_variable(done_var)
        return holder["info"]

    def _read_all_caseload_rows_blocking(self) -> list[dict]:
        """Scroll the Caseload table to load all rows and return them
        as dicts. Blocks the main thread (nested mainloop via
        wait_variable) so the activity log stays responsive."""
        done_var = tk.BooleanVar(value=False)
        holder: dict = {"rows": []}

        def on_done(rows: list[dict]) -> None:
            def set_main() -> None:
                holder["rows"] = rows
                done_var.set(True)
            try:
                self.root.after(0, set_main)
            except Exception:
                holder["rows"] = rows
                done_var.set(True)

        self.worker.submit_read_all_caseload_rows(on_done)
        self.root.wait_variable(done_var)
        return holder["rows"]

    def _read_caseload_columns_blocking(self) -> list[dict]:
        """Read the Caseload list view's column headers + sniffed types.
        Returns list of `{name, type}` dicts. Used by the editor's
        filter UI (build step #7)."""
        done_var = tk.BooleanVar(value=False)
        holder: dict = {"cols": []}

        def on_done(cols: list[dict]) -> None:
            def set_main() -> None:
                holder["cols"] = cols
                done_var.set(True)
            try:
                self.root.after(0, set_main)
            except Exception:
                holder["cols"] = cols
                done_var.set(True)

        self.worker.submit_read_caseload_columns(on_done)
        self.root.wait_variable(done_var)
        return holder["cols"]

    def _submit_scenario_blocking(
        self, scenario: ScenarioConfig, override: str,
        clipboard: str, custom_bodies: dict[int, str],
        prompt_vars: Optional[dict[str, str]] = None,
    ) -> bool:
        """Queue a scenario RUN and block until the worker reports
        completion. Returns True iff the run completed without
        errors — the batch loop uses this for honest processed-vs-
        skipped accounting."""
        done_var = tk.BooleanVar(value=False)
        holder: dict = {"success": False}

        def on_done(success: bool) -> None:
            def set_main() -> None:
                holder["success"] = success
                done_var.set(True)
            try:
                self.root.after(0, set_main)
            except Exception:
                holder["success"] = success
                done_var.set(True)

        self.worker.submit_scenario(
            scenario, override, clipboard,
            custom_bodies=custom_bodies,
            prompt_vars=prompt_vars,
            on_done=on_done,
        )
        self.root.wait_variable(done_var)
        return holder["success"]

    def _show_template_preview(
        self, email_cfg: EmailConfig, n_students: int,
    ) -> bool:
        """Pop a placeholder-rendered Outlook draft so the user can
        review the email template before the batch loop fires. The
        draft is NOT meant to be sent (the To address is a literal
        `<STUDENT EMAIL>` placeholder, which Outlook will reject if
        the user tries Send). The Yes/No modal gates the batch:
        Yes → loop with auto-send, No → abort."""
        from tkinter import messagebox
        from src import outlook_email

        template_path = templates_dir() / email_cfg.body_html_file
        if not template_path.exists():
            self._append_log(
                f"Email template not found: {template_path}; batch aborted."
            )
            messagebox.showerror(
                "Email template missing",
                f"Couldn't find template:\n{template_path}",
            )
            return False

        try:
            template_html = email_template.load_template(template_path)
            preview_body = email_template.render_with_placeholders(template_html)
            # Mirror the runtime path so the preview reflects the
            # scenario's chosen font.
            preview_body = email_template.wrap_with_font(
                preview_body, email_cfg.font_family, email_cfg.font_size,
            )
            preview_subject = email_template.render_plain_with_placeholders(
                email_cfg.subject,
            )
            if email_cfg.to:
                preview_to = email_template.render_plain_with_placeholders(
                    email_cfg.to,
                )
            else:
                preview_to = "<STUDENT EMAIL>"
            preview_cc = "<PROGRAM MENTOR EMAIL>" if email_cfg.cc_pm else ""
        except Exception as e:
            self._append_log(f"Template preview render failed: {e}")
            return False

        inline_images = self._resolve_inline_images(
            preview_body, email_cfg.inline_images,
        )

        self._append_log("Opening template preview in Outlook...")
        try:
            outlook_email.compose_email(
                to=preview_to, cc=preview_cc, subject=preview_subject,
                html_body=preview_body, inline_images=inline_images,
                # auto_send=False — Display() so the user can review.
                # GetInspector here is fine (we never call Send on this draft).
            )
        except Exception as e:
            self._append_log(f"Outlook preview failed: {e}")
            return False

        return messagebox.askyesno(
            "Template preview — send the batch?",
            f"The Outlook draft shows your email TEMPLATE for this "
            f"batch. Each `<PLACEHOLDER>` (e.g. <STUDENT FIRST NAME>, "
            f"<COURSE CODE>) will be replaced with that student's "
            f"actual data when the email is sent.\n\n"
            f"▸ Click Yes to auto-send personalized versions to all "
            f"{n_students} students and file the notes.\n"
            f"▸ Click No to cancel — discard the preview draft in "
            f"Outlook (it won't deliver; the placeholder address is "
            f"invalid).",
        )

    def _resolve_inline_images(
        self, body_html: str, configured: Optional[list] = None,
    ) -> dict:
        """Build the {cid: path} map for compose_email.

        Starts from the scenario's explicitly-configured filenames, then
        auto-adds any `cid:` the rendered body references whose matching
        file exists in templates_dir() (the Add-image dialog writes
        `src="cid:<filename-stem>"`, so cid `image001` ⇢ `image001.*`).

        The auto-add is what prevents the 'linked image cannot be
        displayed' breakage: a scenario can reuse a template whose image
        was never listed in *its* inline_images, and the image still
        embeds because we resolve it straight from the body."""
        images: dict = {}
        for fname in (configured or []):
            p = templates_dir() / fname
            if p.exists():
                images[Path(fname).stem] = p
        for cid in set(re.findall(r'src="cid:([^"]+)"', body_html or "")):
            if cid in images:
                continue
            match = next(iter(sorted(templates_dir().glob(f"{cid}.*"))), None)
            if match is not None:
                images[cid] = match
        return images

    def _send_scenario_email(
        self,
        email_cfg: EmailConfig,
        student_ctx: dict,
        *,
        auto_send: bool = False,
        body_html_override: Optional[str] = None,
    ) -> bool:
        """Render the template and either Display() the draft for
        review (default) or Send() it programmatically (`auto_send`).

        Args:
            auto_send: skip the Outlook window and the per-email
                confirm modal; just send through Outlook. Used by
                the batch driver for every student (template review
                happens upfront via _show_template_preview).

        Returns True to proceed with the note step (or, in
        auto_send mode, True if Send() succeeded). False aborts."""
        from tkinter import messagebox
        from src import outlook_email

        # Augment student context with the launcher operator's identity
        # (read from Outlook's profile). Templates that want to sign
        # with *the sender's* name should use {{user_name}} — vs.
        # {{pm_name}} which is the *student's* Program Mentor from the
        # caseload row (only equals you when you ARE their PM).
        user_info = outlook_email.get_user_info()
        if not user_info.get("name") and not user_info.get("email"):
            # Bad cache from a transient COM hiccup, OR Outlook isn't
            # exposing CurrentUser on this profile. The get_user_info
            # helper now skips caching empty reads, so the next call
            # will retry. Log once so the user can spot template
            # variables coming out blank.
            self._append_log(
                "Outlook didn't surface CurrentUser this attempt — "
                "{{user_name}} / {{user_email}} may be blank in this "
                "email. If it persists, restart Outlook before the "
                "next batch."
            )
        student_ctx = {
            **student_ctx,
            "user_name": user_info.get("name", ""),
            "user_email": user_info.get("email", ""),
        }

        # PM email fallback: when the user has "CC Program Mentor"
        # checked and we couldn't read pm_email from either the
        # caseload row or the contact card scrape, AND the row's
        # Program Mentor name loosely matches the launcher user's
        # own name — they ARE the PM for this student, so the CC
        # they want is themselves. Catches the very common faculty
        # workflow of CCing their own caseload emails for record-
        # keeping. Only fires when names match, so a different PM's
        # email never gets quietly swapped for yours.
        if (email_cfg.cc_pm
                and not student_ctx.get("pm_email")
                and student_ctx.get("pm_name")
                and user_info.get("email")
                and _names_loosely_match(
                    student_ctx.get("pm_name", ""), user_info.get("name", "")
                )):
            student_ctx["pm_email"] = user_info["email"]
            if not getattr(self, "_pm_self_cc_logged", False):
                self._pm_self_cc_logged = True
                self._append_log(
                    f"PM email not in caseload — you are the PM "
                    f"({user_info['name']!r}), so CCing your Outlook "
                    f"address {user_info['email']!r}."
                )

        template_path = templates_dir() / email_cfg.body_html_file
        if not template_path.exists():
            self._append_log(
                f"Email template not found: {template_path}. Action aborted."
            )
            messagebox.showerror(
                "Email template missing",
                f"Couldn't find template:\n{template_path}\n\n"
                "Check the action's body_html_file and the templates folder.",
            )
            return False

        try:
            if body_html_override is not None:
                # Per-student one-off: the user edited this body in the
                # reviewer; send exactly that (already rendered) instead of
                # re-rendering from the template.
                body_html = body_html_override
            else:
                template_html = email_template.load_template(template_path)
                body_html = email_template.render(template_html, student_ctx)
            # If the scenario pins a font, wrap the body in an inline-
            # styled div so Outlook honors it. Otherwise (default) the
            # HTML goes through untouched and Outlook applies the
            # user's compose default.
            body_html = email_template.wrap_with_font(
                body_html, email_cfg.font_family, email_cfg.font_size,
            )
            # Subject and addresses are plain text — never HTML-escape.
            subject = email_template.render_plain(
                email_cfg.subject, student_ctx,
            )
        except Exception as e:
            self._append_log(f"Email template render failed: {e}")
            return False

        # CID images: configured list PLUS any cid: the body references
        # (so a template's image embeds even if this scenario's
        # inline_images list was left empty).
        inline_images = self._resolve_inline_images(
            body_html, email_cfg.inline_images,
        )

        # To: optional override (for test-mode addresses or any custom
        # routing), falling back to the student's email from caseload.
        if email_cfg.to:
            to = email_template.render_plain(email_cfg.to, student_ctx).strip()
        else:
            to = student_ctx.get("student_email", "")
        cc = student_ctx.get("pm_email", "") if email_cfg.cc_pm else ""
        if not to:
            full_name = student_ctx.get('full_name') or 'this student'
            if auto_send:
                # Batch mode — popping a "no email" modal per student
                # would force the user to click through every blank
                # entry in a multi-student run. Just log + skip.
                self._append_log(
                    f"Skipping {full_name!r}: no student email in "
                    "caseload row. Add a Student Email column to your "
                    "Salesforce Caseload view and ↻ refresh."
                )
                return False
            # Interactive (single-student) mode — keep the existing
            # ask-and-proceed flow so the user can opt to file just
            # the note without an email.
            if not messagebox.askyesno(
                "No student email",
                f"Couldn't find an email address for {full_name!r}.\n\n"
                "Proceed with the note only?",
            ):
                return False
            return True  # skip the email, but file the note

        full_name = student_ctx.get("full_name") or to

        if auto_send:
            self._append_log(f"Auto-sending email to {full_name}...")
            # Retry once on transient COM failures. Outlook can throw
            # "Server execution failed" (-2146959355) mid-batch when
            # it's busy launching a reminder popup, finishing a
            # send-receive, or fielding another COM client. A short
            # pause + one retry rescues the vast majority of these
            # without escalating to a true skip.
            import time as _time
            last_err: Optional[Exception] = None
            for attempt in (1, 2):
                try:
                    outlook_email.compose_email(
                        to=to, cc=cc, subject=subject,
                        html_body=body_html, inline_images=inline_images,
                        auto_send=True,
                        signature_name=email_cfg.signature_file,
                    )
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
                    if attempt == 1:
                        self._append_log(
                            f"Outlook hiccup on {full_name}: {e}. "
                            "Retrying in 2s…"
                        )
                        _time.sleep(2)
            if last_err is not None:
                self._append_log(f"Auto-send failed for {full_name}: {last_err}")
                return False
            return True

        self._append_log(f"Opening Outlook draft for {full_name}...")
        try:
            outlook_email.compose_email(
                to=to, cc=cc, subject=subject,
                html_body=body_html, inline_images=inline_images,
            )
        except Exception as e:
            self._append_log(f"Outlook compose failed: {e}")
            # Use topmost dialog: Outlook may have partially opened
            # before failing and could still own focus.
            return ask_yes_no_topmost(
                self.root, "Email failed",
                f"Couldn't open the email in Outlook:\n\n{e}\n\n"
                "Proceed with the note only?",
            )

        # Topmost confirm (NOT messagebox.askyesno) — Outlook just
        # stole focus to show the compose window, and stock tkinter
        # modals can open BEHIND it. The user would then see what
        # looks like a hung app waiting on a question they can't see.
        return ask_yes_no_topmost(
            self.root, "Done with the email?",
            f"A draft is now open in Outlook for {full_name}.\n\n"
            "▸ Send the email yourself from Outlook (or discard it).\n"
            "▸ Then click Yes to file the Salesforce note.\n\n"
            "Click No to skip the note for this student and move on.",
        )

    def _poll_worker_then_auto_download(self) -> None:
        """Wait for the worker to finish browser setup (max ~60s),
        then fire one CSV download in the background. Non-blocking
        — uses root.after polling so the launcher window stays
        responsive throughout. Action buttons are disabled (via
        _set_busy) for the duration so the user can't fire a batch
        against a stale cache while we're refreshing."""
        if not self.worker.ready_event.is_set():
            self.root.after(500, self._poll_worker_then_auto_download)
            return
        self._set_busy("Auto-refreshing caseload + Essential Actions…")
        self._append_log("Auto-refreshing caseload CSV...")

        def on_ea_done(res) -> None:
            def apply() -> None:
                if (res or {}).get("error"):
                    self._append_log(
                        f"EA dashboard scrape failed: {res['error']}",
                        error=True)
                self._apply_ea_scrape((res or {}).get("eas") or [])
                self._set_idle()
                # Kick the background live task pass/fail pass (startup), then
                # minimize the browser once it's done — after the scrape, since
                # the scroll needs the window un-minimized to render.
                self._maybe_bulk_scrape_task_status(
                    "startup", on_complete=self._minimize_browser)
            try:
                self.root.after(0, apply)
            except Exception:
                self._set_idle()

        def on_done(success: bool, message: str) -> None:
            def set_main() -> None:
                if success:
                    self._append_log(f"Caseload CSV: {message}")
                    self._reload_caseload_cache(silent=False)
                    # Scrape the Essential Actions dashboard in the same pass.
                    self._append_log("Scraping Essential Actions dashboard…")
                    self.worker.submit_read_ea_dashboard(on_ea_done)
                else:
                    self._append_log(
                        f"Caseload CSV auto-download failed: {message}. "
                        "You can retry with ↻ Caseload. Until then, "
                        "batches will fall back to the DOM scrape."
                    )
                    self._set_idle()
            try:
                self.root.after(0, set_main)
            except Exception:
                pass

        self.worker.submit_download_caseload_csv(CASELOAD_CSV_PATH, on_done)

    def _open_settings(self) -> None:
        """Modal for user preferences. Currently the advanced /
        developer-mode toggle + Caseload Tool view status. Designed
        to grow as new prefs land.

        Topmost + grab so it can't get buried. Saves to settings.json
        and applies the change immediately (re-runs the visibility
        pass over the toolbar + open scenario editor tabs)."""
        dialog = ctk.CTkToplevel(self.root)
        dialog.title("Settings")
        dialog.transient(self.root)
        dialog.attributes("-topmost", True)
        dialog.grab_set()
        dialog.geometry("560x520")
        dialog.minsize(520, 360)
        dialog.lift()
        dialog.focus_force()

        def _refit() -> None:
            """Resize the dialog to fit its current content (after the
            foldable Display section toggles or the UI scale changes), so
            controls never end up clipped below the bottom edge. Uses raw
            wm_geometry + the content's requested size so it stays correct
            at any UI scale (CTk's geometry() would re-apply the scale)."""
            try:
                dialog.update_idletasks()
                w = max(dialog.winfo_reqwidth(), 480)
                h = min(max(dialog.winfo_reqheight(), 360),
                        int(dialog.winfo_screenheight() * 0.9))
                dialog.wm_geometry(f"{w}x{h}")
            except Exception:
                pass

        advanced_var = ctk.BooleanVar(value=self.settings.advanced_mode)
        ctk.CTkLabel(
            dialog, text="Settings",
            font=ctk.CTkFont(size=15, weight="bold"),
            anchor="w",
        ).pack(fill="x", padx=20, pady=(16, 4))

        ctk.CTkCheckBox(
            dialog,
            text="Advanced / developer mode",
            variable=advanced_var,
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(anchor="w", padx=20, pady=(8, 4))

        ctk.CTkLabel(
            dialog,
            text=(
                "Shows additional features most users don't need:\n\n"
                "  •  Action variables  (advanced template substitution)\n"
                "  •  Inline images  (per-action email attachments)\n"
                "  •  Email font / size override  (per-action)\n"
                "  •  Email override  (To: redirect, for testing)\n"
                "  •  Append clipboard contents  (per-note toggle)\n"
                "  •  📁 Templates folder + Open log buttons\n"
                "  •  🔬 Capture button  (network traffic recording — "
                "for REST API discovery)"
            ),
            wraplength=510, justify="left",
            text_color=("gray35", "gray70"),
            anchor="w",
        ).pack(fill="x", padx=32, pady=(0, 6))

        ctk.CTkLabel(
            dialog,
            text=(
                "If an action already uses any of these features, those "
                "fields stay visible regardless of this setting so you "
                "can see and edit them."
            ),
            wraplength=510, justify="left",
            text_color=("gray45", "gray60"),
            font=ctk.CTkFont(size=11, slant="italic"),
            anchor="w",
        ).pack(fill="x", padx=32, pady=(0, 14))

        # Caseload Tool view section — status + setup button.
        # Separator
        sep = ctk.CTkFrame(dialog, height=1, fg_color=("gray70", "gray35"))
        sep.pack(fill="x", padx=20, pady=(2, 10))

        ctk.CTkLabel(
            dialog, text="Salesforce Caseload view",
            font=ctk.CTkFont(size=13, weight="bold"),
            anchor="w",
        ).pack(fill="x", padx=20, pady=(0, 4))

        if self._csv_has_student_email:
            status_text = "✓  Student-email column detected in cached CSV."
            status_color = ("#2d7d2d", "#7fd97f")
        else:
            status_text = (
                "⚠  Cached CSV doesn't include a student-email column. "
                "Batch emails will fall back to slower per-student "
                "scraping at send time."
            )
            status_color = ("#7a4f00", "#ffd166")
        ctk.CTkLabel(
            dialog, text=status_text,
            wraplength=510, justify="left",
            text_color=status_color, anchor="w",
            font=ctk.CTkFont(size=11),
        ).pack(fill="x", padx=32, pady=(0, 8))

        ctk.CTkButton(
            dialog, text="Set up Caseload Tool view (instructions)",
            command=lambda: self._setup_caseload_tool_view_with_help(dialog),
            width=260,
        ).pack(anchor="w", padx=32, pady=(0, 8))

        # Required-columns check: let the user exclude columns they don't
        # care about so the "missing columns" warning doesn't nag.
        ctk.CTkLabel(
            dialog,
            text="Ignore these columns in the \"missing columns\" check "
                 "(comma-separated CSV names, e.g. LatestTaskStatus):",
            wraplength=510, justify="left", anchor="w",
            text_color=("gray45", "gray60"), font=ctk.CTkFont(size=11),
        ).pack(fill="x", padx=32, pady=(0, 2))
        req_ignore_var = ctk.StringVar(
            value=getattr(self.settings, "required_columns_ignore", "") or "")
        ctk.CTkEntry(
            dialog, textvariable=req_ignore_var, width=420,
            placeholder_text="LatestCourseNote, MyCourseContact",
        ).pack(anchor="w", padx=32, pady=(0, 12))

        # ---- Scenarios ----
        ctk.CTkFrame(dialog, height=1, fg_color=("gray70", "gray35")).pack(
            fill="x", padx=20, pady=(2, 8))
        ctk.CTkLabel(
            dialog, text="Actions",
            font=ctk.CTkFont(size=13, weight="bold"), anchor="w",
        ).pack(fill="x", padx=20, pady=(0, 2))
        ctk.CTkLabel(
            dialog,
            text="Create a new action, load an actions file someone "
                 "shared, or restore the built-in samples. Your current "
                 "actions are backed up first.",
            wraplength=510, justify="left", anchor="w",
            text_color=("gray45", "gray60"), font=ctk.CTkFont(size=11),
        ).pack(fill="x", padx=32, pady=(0, 6))
        scen_row = ctk.CTkFrame(dialog, fg_color="transparent")
        scen_row.pack(fill="x", padx=32, pady=(0, 10))
        ctk.CTkButton(
            scen_row, text="+ New", width=70,
            command=lambda: self._settings_new_scenario(dialog),
        ).pack(side="left")
        ctk.CTkButton(
            scen_row, text="Load file…", width=100,
            command=lambda: self._load_scenarios_dialog(dialog),
            **SECONDARY_BTN_KWARGS,
        ).pack(side="left", padx=(8, 0))
        ctk.CTkButton(
            scen_row, text="Save file…", width=100,
            command=self._save_scenarios_dialog,
            **SECONDARY_BTN_KWARGS,
        ).pack(side="left", padx=(8, 0))
        ctk.CTkButton(
            scen_row, text="Load samples", width=110,
            command=lambda: self._load_sample_scenarios(dialog),
            **SECONDARY_BTN_KWARGS,
        ).pack(side="left", padx=(8, 0))

        # ---- Email templates ----
        ctk.CTkFrame(dialog, height=1, fg_color=("gray70", "gray35")).pack(
            fill="x", padx=20, pady=(2, 8))
        ctk.CTkLabel(
            dialog, text="Email templates",
            font=ctk.CTkFont(size=13, weight="bold"), anchor="w",
        ).pack(fill="x", padx=20, pady=(0, 2))
        tpl_folder_lbl = ctk.CTkLabel(
            dialog, text=f"Folder:  {templates_dir()}",
            wraplength=510, justify="left", anchor="w",
            text_color=("gray45", "gray60"), font=ctk.CTkFont(size=11),
        )
        tpl_folder_lbl.pack(fill="x", padx=32, pady=(0, 6))
        tpl_row = ctk.CTkFrame(dialog, fg_color="transparent")
        tpl_row.pack(fill="x", padx=32, pady=(0, 10))
        ctk.CTkButton(
            tpl_row, text="Create folder…", width=130,
            command=lambda: self._create_template_folder(tpl_folder_lbl),
            **SECONDARY_BTN_KWARGS,
        ).pack(side="left")
        ctk.CTkButton(
            tpl_row, text="Load folder…", width=130,
            command=lambda: self._load_template_folder(tpl_folder_lbl),
            **SECONDARY_BTN_KWARGS,
        ).pack(side="left", padx=(8, 0))
        ctk.CTkButton(
            tpl_row, text="Open folder", width=110,
            command=self._on_open_templates_folder,
            **SECONDARY_BTN_KWARGS,
        ).pack(side="left", padx=(8, 0))

        # ---- Display (foldable) ----
        ctk.CTkFrame(dialog, height=1, fg_color=("gray70", "gray35")).pack(
            fill="x", padx=20, pady=(2, 8))
        disp_open = {"v": False}
        disp_body = ctk.CTkFrame(dialog, fg_color="transparent")

        def _disp_label() -> str:
            return ("▾ Display (text size & scaling)" if disp_open["v"]
                    else "▸ Display (text size & scaling)")

        def _toggle_disp() -> None:
            if disp_open["v"]:
                disp_body.pack_forget()
                disp_open["v"] = False
            else:
                disp_body.pack(fill="x", before=alerts_sep)
                disp_open["v"] = True
            disp_header.configure(text=_disp_label())
            _refit()

        disp_header = ctk.CTkButton(
            dialog, text=_disp_label(), anchor="w", height=28,
            fg_color="transparent", hover=False,
            text_color=("gray10", "gray90"),
            font=ctk.CTkFont(size=13, weight="bold"),
            command=_toggle_disp,
        )
        disp_header.pack(fill="x", padx=16, pady=(0, 2))

        # Overall UI scale (whole-app zoom).
        scale_row = ctk.CTkFrame(disp_body, fg_color="transparent")
        scale_row.pack(fill="x", padx=12, pady=(6, 4))
        ctk.CTkLabel(scale_row, text="Overall UI scale", width=130,
                     anchor="w").pack(side="left")
        cur_scale = int(round(float(
            getattr(self.settings, "ui_scale", 1.0) or 1.0) * 100))
        scale_var = ctk.StringVar(value=f"{cur_scale}%")
        ctk.CTkComboBox(
            scale_row, width=80, variable=scale_var, state="readonly",
            values=["80%", "90%", "100%", "110%", "125%", "140%", "160%"],
            command=lambda v: (ctk.set_widget_scaling(int(v.rstrip('%')) / 100),
                               dialog.after(60, _refit)),
        ).pack(side="left", padx=(6, 6))
        ctk.CTkLabel(
            scale_row, text="(whole app — buttons + text)",
            font=ctk.CTkFont(size=10), text_color=("gray45", "gray65"),
        ).pack(side="left")

        _SIZE_VALUES = [str(s) for s in (8, 10, 11, 12, 13, 14, 16, 18, 20, 24)]

        def _font_row(label: str, channel: str) -> None:
            r = ctk.CTkFrame(disp_body, fg_color="transparent")
            r.pack(fill="x", padx=12, pady=3)
            ctk.CTkLabel(r, text=label, width=130, anchor="w").pack(side="left")
            combo = ctk.CTkComboBox(
                r, width=70, values=_SIZE_VALUES,
                command=lambda v, c=channel: set_font_size(c, int(v)),
            )
            combo.set(str(font_size(channel)))
            combo.pack(side="left", padx=(6, 6))

            def _reset(c=channel, cb=combo) -> None:
                set_font_size(c, UI_FONT_DEFAULTS[c])
                cb.set(str(UI_FONT_DEFAULTS[c]))

            ctk.CTkButton(
                r, text="Default", width=70, command=_reset,
                **SECONDARY_BTN_KWARGS,
            ).pack(side="left")

        ctk.CTkLabel(
            disp_body, text="Text size by area:",
            font=ctk.CTkFont(size=11, weight="bold"), anchor="w",
        ).pack(fill="x", padx=12, pady=(8, 0))
        _font_row("Activity log", "activity")
        _font_row("Caseload viewer", "viewer")
        _font_row("Student notes", "notes")
        _font_row("Email reviewer", "email")
        _font_row("Editors", "editor")
        ctk.CTkLabel(
            disp_body,
            text="Tip: Ctrl +/− and Ctrl+scroll also resize text inside "
                 "these areas.",
            font=ctk.CTkFont(size=10), text_color=("gray45", "gray65"),
            anchor="w", justify="left", wraplength=480,
        ).pack(fill="x", padx=12, pady=(2, 8))

        # ---- Alerts ----
        alerts_sep = ctk.CTkFrame(dialog, height=1,
                                  fg_color=("gray70", "gray35"))
        alerts_sep.pack(fill="x", padx=20, pady=(6, 8))
        ctk.CTkLabel(
            dialog, text="Alerts",
            font=ctk.CTkFont(size=13, weight="bold"), anchor="w",
        ).pack(fill="x", padx=20, pady=(0, 4))

        stale_row = ctk.CTkFrame(dialog, fg_color="transparent")
        stale_row.pack(fill="x", padx=32, pady=(0, 10))
        ctk.CTkLabel(stale_row, text="Warn if caseload older than:").pack(
            side="left")
        _STALE_PRESETS = {
            "Off": 0, "15 min": 15, "30 min": 30, "1 h": 60,
            "6 h": 360, "12 h": 720, "24 h": 1440,
        }
        _MIN_TO_LABEL = {v: k for k, v in _STALE_PRESETS.items()}
        cur_min = int(getattr(self.settings, "caseload_stale_minutes", 0) or 0)
        init_label = _MIN_TO_LABEL.get(cur_min, "Custom")
        stale_var = ctk.StringVar(value=init_label)
        custom_entry = ctk.CTkEntry(stale_row, width=56,
                                    placeholder_text="num")
        unit_var = ctk.StringVar(value="min")
        unit_combo = ctk.CTkComboBox(
            stale_row, width=78, variable=unit_var, state="readonly",
            values=["min", "hours"])

        def _on_stale_preset(v: str) -> None:
            if v == "Custom":
                custom_entry.pack(side="left", padx=(8, 2))
                unit_combo.pack(side="left")
            else:
                custom_entry.pack_forget()
                unit_combo.pack_forget()

        ctk.CTkComboBox(
            stale_row, width=90, variable=stale_var, state="readonly",
            values=list(_STALE_PRESETS.keys()) + ["Custom"],
            command=_on_stale_preset,
        ).pack(side="left", padx=(8, 0))
        if init_label == "Custom" and cur_min > 0:
            if cur_min % 60 == 0:
                custom_entry.insert(0, str(cur_min // 60))
                unit_var.set("hours")
            else:
                custom_entry.insert(0, str(cur_min))
            _on_stale_preset("Custom")

        # Live task pass/fail (the "2a" background scrape). Reads each
        # student's real per-task pass/fail from the live list (the colour
        # the CSV drops) and colours the grid Task cells + quick-view badges.
        # Adds ~5-9s, but runs in the BACKGROUND after a refresh.
        ts_row = ctk.CTkFrame(dialog, fg_color="transparent")
        ts_row.pack(fill="x", padx=32, pady=(0, 10))
        ctk.CTkLabel(ts_row, text="Live task pass/fail scrape:").pack(
            side="left")
        _TS_MODE_LABELS = {
            "Off": "off",
            "Once on startup": "restart",
            "Every refresh": "refresh",
        }
        _TS_MODE_TO_LABEL = {v: k for k, v in _TS_MODE_LABELS.items()}
        cur_ts = (getattr(self.settings, "task_status_scrape_mode",
                          "restart") or "restart")
        ts_var = ctk.StringVar(
            value=_TS_MODE_TO_LABEL.get(cur_ts, "Once on startup"))
        ctk.CTkComboBox(
            ts_row, width=150, variable=ts_var, state="readonly",
            values=list(_TS_MODE_LABELS.keys()),
        ).pack(side="left", padx=(8, 0))

        # Caseload history snapshot frequency (src/history.py). How often a
        # local snapshot of the dynamic fields (Momentum, task status, …) is
        # recorded; the data changes ~daily so "Daily" is plenty, but a user
        # can sample more often. "Off" disables history capture entirely.
        hist_row = ctk.CTkFrame(dialog, fg_color="transparent")
        hist_row.pack(fill="x", padx=32, pady=(0, 10))
        ctk.CTkLabel(hist_row, text="Caseload history snapshots:").pack(
            side="left")
        _HIST_LABELS = {
            "Off": 0, "Every 6 h": 6, "Every 8 h": 8,
            "Every 12 h": 12, "Daily": 24,
        }
        _HIST_TO_LABEL = {v: k for k, v in _HIST_LABELS.items()}
        cur_hist = int(getattr(
            self.settings, "history_capture_interval_hours", 24) or 0)
        hist_var = ctk.StringVar(value=_HIST_TO_LABEL.get(cur_hist, "Daily"))
        ctk.CTkComboBox(
            hist_row, width=130, variable=hist_var, state="readonly",
            values=list(_HIST_LABELS.keys()),
        ).pack(side="left", padx=(8, 0))

        # Name capitalization — how student/PM names are cased in template
        # variables (papers over CSV data-entry casing errors).
        nc_row = ctk.CTkFrame(dialog, fg_color="transparent")
        nc_row.pack(fill="x", padx=32, pady=(0, 10))
        ctk.CTkLabel(nc_row, text="Capitalize names in templates:").pack(
            side="left")
        _NC_MODE_LABELS = {
            "Off (use CSV as-is)": "off",
            "Fix lowercase only": "lower",
            "Standard form": "standard",
        }
        _NC_MODE_TO_LABEL = {v: k for k, v in _NC_MODE_LABELS.items()}
        cur_nc = (getattr(self.settings, "name_capitalization",
                          "standard") or "standard")
        nc_var = ctk.StringVar(
            value=_NC_MODE_TO_LABEL.get(cur_nc, "Standard form"))
        ctk.CTkComboBox(
            nc_row, width=180, variable=nc_var, state="readonly",
            values=list(_NC_MODE_LABELS.keys()),
        ).pack(side="left", padx=(8, 0))

        btn_row = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_row.pack(fill="x", padx=20, pady=(0, 18), side="bottom")

        def _do_save() -> None:
            new_mode = advanced_var.get()
            changed = (new_mode != self.settings.advanced_mode)
            self.settings.advanced_mode = new_mode
            # Required-columns check ignore-list.
            try:
                self.settings.required_columns_ignore = req_ignore_var.get().strip()
            except Exception:
                pass
            # Overall UI scale.
            try:
                self.settings.ui_scale = int(
                    scale_var.get().rstrip("%")) / 100
            except Exception:
                pass
            # Caseload staleness threshold (preset or Custom → minutes).
            sv = stale_var.get().strip()
            if sv in _STALE_PRESETS:
                self.settings.caseload_stale_minutes = _STALE_PRESETS[sv]
            else:  # Custom
                try:
                    num = int(float(custom_entry.get().strip()))
                    self.settings.caseload_stale_minutes = (
                        num * 60 if unit_var.get() == "hours" else num)
                except Exception:
                    pass  # leave the existing value
            # Live task pass/fail scrape mode (Off / startup / every refresh).
            self.settings.task_status_scrape_mode = _TS_MODE_LABELS.get(
                ts_var.get().strip(), "restart")
            # Caseload history snapshot interval (Off=0 / 6 / 8 / 12 / 24 h).
            self.settings.history_capture_interval_hours = _HIST_LABELS.get(
                hist_var.get().strip(), 24)
            # Name capitalization mode (off / lower / standard).
            self.settings.name_capitalization = _NC_MODE_LABELS.get(
                nc_var.get().strip(), "standard")
            self._sync_name_cap_mode()  # apply immediately to the builders
            # Per-area font sizes already persist live via set_font_size.
            save_settings(self.settings)
            if changed:
                self._apply_advanced_mode()
            try: dialog.grab_release()
            except Exception: pass
            try: dialog.destroy()
            except Exception: pass

        def _do_cancel() -> None:
            try: dialog.grab_release()
            except Exception: pass
            try: dialog.destroy()
            except Exception: pass

        ctk.CTkButton(
            btn_row, text="Save", width=100, command=_do_save,
        ).pack(side="left", padx=4)
        ctk.CTkButton(
            btn_row, text="Cancel", width=100, command=_do_cancel,
            **SECONDARY_BTN_KWARGS,
        ).pack(side="left", padx=4)
        dialog.bind("<Escape>", lambda _e: _do_cancel())
        dialog.protocol("WM_DELETE_WINDOW", _do_cancel)
        dialog.after(0, _refit)  # size to the built content

    def _setup_caseload_tool_view_with_help(self, parent_dialog=None) -> None:
        """Open the manual walkthrough for creating the Caseload Tool
        view. Earlier iterations tried to drive the Salesforce UI via
        Playwright; that path proved fragile across the popup variants
        WGU's Salesforce serves, and one attempt crashed Edge. We
        shipped the manual flow as the supported path and removed the
        automation. The manual setup is ~3 minutes and reliable."""
        self._show_caseload_view_help(parent_dialog or self.root)

    def _show_caseload_view_help(self, parent) -> None:
        """Step-by-step manual setup instructions for creating the
        'Caseload Tool' list view. Shipped as the first-iteration
        flow AND as the fallback for when the automation can't
        complete a step against an unfamiliar Salesforce UI."""
        dialog = ctk.CTkToplevel(parent)
        dialog.title("Set up Caseload Tool view")
        dialog.transient(parent)
        dialog.attributes("-topmost", True)
        dialog.grab_set()
        dialog.geometry("640x600")
        dialog.lift()
        dialog.focus_force()

        ctk.CTkLabel(
            dialog,
            text="Set up the Caseload Tool view",
            font=ctk.CTkFont(size=16, weight="bold"),
            anchor="w",
        ).pack(fill="x", padx=20, pady=(18, 4))

        ctk.CTkLabel(
            dialog,
            text=(
                "One-time setup in Salesforce — ~3 minutes. Once "
                "complete, the launcher's CSV download picks up the "
                "Email column automatically and you'll see the green "
                "checkmark in Settings."
            ),
            font=ctk.CTkFont(size=11),
            text_color=("gray35", "gray70"),
            wraplength=580, justify="left", anchor="w",
        ).pack(fill="x", padx=20, pady=(0, 10))

        instructions = (
            "1. Open the Caseload page in Salesforce.\n\n"
            "2. To the right of the List Views dropdown, click the\n"
            "   small disk icon (it's next to the trash icon).\n\n"
            "3. In the 'Update Existing List View' popup, click\n"
            "   'Save As New List View ➕' (top right of the popup).\n\n"
            "4. Type   Caseload Tool   as the name.\n\n"
            "5. Permissions: leave as 'Myself' so only you see the view.\n\n"
            "6. Click 'Save'. The view is created and selected.\n\n"
            "7. Click the gear icon (or the 'Hidden Columns' link) to\n"
            "   open the column picker.\n\n"
            "8. In Available Columns, find any rows with 'Email' in\n"
            "   the name (likely just 'Email'). For each:\n"
            "     - click the row\n"
            "     - click the ►  arrow to move it to Selected Columns\n\n"
            "9. Click 'Save' on the column picker.\n\n"
            "10. (Optional) If the view inherited a filter from your\n"
            "    previous view (e.g., 'Term End Date =(This Month)'),\n"
            "    open the filter editor and remove it so the launcher\n"
            "    sees all your students.\n\n"
            "11. Back in the launcher, click ↻ Caseload to download\n"
            "    the new CSV with the email column.\n\n"
            "Going forward, the launcher will use this view "
            "automatically. The Settings dialog confirms when "
            "Student Email is detected in the CSV."
        )

        text = ctk.CTkTextbox(
            dialog, wrap="word",
            font=ctk.CTkFont(family="Segoe UI", size=12),
        )
        text.pack(fill="both", expand=True, padx=20, pady=8)
        text.insert("1.0", instructions)
        text.configure(state="disabled")

        ctk.CTkButton(
            dialog, text="Close", command=lambda: dialog.destroy(),
            width=120,
        ).pack(pady=(0, 16))

        dialog.bind("<Escape>", lambda _e: dialog.destroy())

    def _show_first_run_setup(self) -> None:
        """Modal that pops on first launch (settings.first_run_complete
        is False). Welcome message, mode picker, optional Caseload
        Tool view setup. On Continue, sets first_run_complete=True
        and applies the chosen mode. The dialog is mandatory — close
        via Continue, no Cancel."""
        dialog = ctk.CTkToplevel(self.root)
        dialog.title("Welcome to Caseload Notes")
        dialog.transient(self.root)
        dialog.attributes("-topmost", True)
        dialog.grab_set()
        dialog.geometry("600x600")
        dialog.lift()
        dialog.focus_force()
        # Repeat focus claw-back so the dialog can't get buried
        # under the launcher window during the first-show pass.
        dialog.after(150, lambda: (dialog.lift(), dialog.focus_force()))

        ctk.CTkLabel(
            dialog, text="Welcome to Caseload Notes",
            font=ctk.CTkFont(size=18, weight="bold"),
            anchor="w",
        ).pack(fill="x", padx=20, pady=(20, 4))
        ctk.CTkLabel(
            dialog,
            text=(
                "A couple of choices to get you set up. You can change "
                "either of these later from the ⚙ Settings dialog."
            ),
            font=ctk.CTkFont(size=12),
            text_color=("gray35", "gray70"),
            wraplength=540, justify="left", anchor="w",
        ).pack(fill="x", padx=20, pady=(0, 16))

        # --- Mode picker.
        mode_frame = ctk.CTkFrame(
            dialog, fg_color=("gray92", "gray18"), corner_radius=6,
        )
        mode_frame.pack(fill="x", padx=16, pady=(0, 12))

        ctk.CTkLabel(
            mode_frame, text="1.  Editor mode",
            font=ctk.CTkFont(size=13, weight="bold"),
            anchor="w",
        ).pack(fill="x", padx=12, pady=(10, 4))

        mode_var = ctk.StringVar(
            value="advanced" if self.settings.advanced_mode else "basic",
        )

        ctk.CTkRadioButton(
            mode_frame, text="Simple   (recommended for most users)",
            variable=mode_var, value="basic",
        ).pack(anchor="w", padx=24, pady=(0, 2))
        ctk.CTkLabel(
            mode_frame,
            text=(
                "Hides advanced options (variables, inline images, "
                "font overrides, network capture)."
            ),
            font=ctk.CTkFont(size=11),
            text_color=("gray35", "gray70"),
            wraplength=480, justify="left",
        ).pack(anchor="w", padx=44, pady=(0, 8))

        ctk.CTkRadioButton(
            mode_frame, text="Advanced",
            variable=mode_var, value="advanced",
        ).pack(anchor="w", padx=24, pady=(0, 2))
        ctk.CTkLabel(
            mode_frame,
            text=(
                "Shows action variables, email font overrides, the "
                "🔬 Capture network-recording tool, etc."
            ),
            font=ctk.CTkFont(size=11),
            text_color=("gray35", "gray70"),
            wraplength=480, justify="left",
        ).pack(anchor="w", padx=44, pady=(0, 12))

        # --- Caseload Tool view section.
        view_frame = ctk.CTkFrame(
            dialog, fg_color=("gray92", "gray18"), corner_radius=6,
        )
        view_frame.pack(fill="x", padx=16, pady=(0, 12))

        ctk.CTkLabel(
            view_frame, text="2.  Salesforce Caseload Tool view",
            font=ctk.CTkFont(size=13, weight="bold"),
            anchor="w",
        ).pack(fill="x", padx=12, pady=(10, 4))
        ctk.CTkLabel(
            view_frame,
            text=(
                "A dedicated Salesforce list view with the columns this "
                "launcher needs (notably Student Email). One-time setup; "
                "the launcher downloads from it going forward, leaving "
                "your normal views untouched. Recommended but not "
                "required — batches still work without it (via slower "
                "per-student email scraping at send time)."
            ),
            font=ctk.CTkFont(size=11),
            text_color=("gray35", "gray70"),
            wraplength=540, justify="left", anchor="w",
        ).pack(fill="x", padx=12, pady=(0, 8))

        ctk.CTkButton(
            view_frame, text="Show Caseload Tool setup instructions",
            command=lambda: self._setup_caseload_tool_view_with_help(dialog),
            width=260,
        ).pack(anchor="w", padx=12, pady=(0, 12))

        # --- Continue button.
        def _continue() -> None:
            self.settings.advanced_mode = (mode_var.get() == "advanced")
            self.settings.first_run_complete = True
            save_settings(self.settings)
            self._apply_advanced_mode()
            try: dialog.grab_release()
            except Exception: pass
            try: dialog.destroy()
            except Exception: pass

        btn_row = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_row.pack(fill="x", padx=20, pady=(0, 18), side="bottom")
        ctk.CTkButton(
            btn_row, text="Continue", command=_continue, width=140,
        ).pack(side="right")

        dialog.protocol("WM_DELETE_WINDOW", _continue)
        # Don't bind Escape — this is the first-run welcome and
        # should be saved-and-closed via the explicit button.

    def _confirm_csv_email_present_or_proceed(
        self, scenario: ScenarioConfig,
    ) -> bool:
        """Pre-batch check: when the scenario has an email step AND
        the cached CSV doesn't have a student-email column, ask the
        user whether to set up the view, proceed with the slower
        fallback, or skip this question for the rest of the session.
        Returns True to proceed with the batch, False to abort."""
        if scenario.email is None:
            return True  # no email step → CSV column doesn't matter
        if self._csv_has_student_email:
            return True
        if self._csv_email_warning_skipped:
            return True

        dialog = ctk.CTkToplevel(self.root)
        dialog.title("Student emails needed — column missing")
        dialog.transient(self.root)
        dialog.attributes("-topmost", True)
        dialog.grab_set()
        dialog.geometry("580x380")
        dialog.lift()
        dialog.focus_force()
        result = {"value": False}

        ctk.CTkLabel(
            dialog, text="Student emails needed",
            font=ctk.CTkFont(size=14, weight="bold"), anchor="w",
        ).pack(fill="x", padx=20, pady=(18, 6))
        ctk.CTkLabel(
            dialog,
            text=(
                "This action sends email, but your caseload CSV doesn't "
                "include a student-email column. The batch can still "
                "proceed — emails will be looked up student-by-student "
                "from Salesforce at send time, which is slower and less "
                "reliable."
            ),
            wraplength=540, justify="left", anchor="w",
            font=ctk.CTkFont(size=12),
        ).pack(fill="x", padx=20, pady=(0, 8))
        ctk.CTkLabel(
            dialog,
            text=(
                "To fix this permanently:\n"
                "  1.  In your Salesforce 'Caseload Tool' list view, add "
                "an Email column (see 'Set up instructions').\n"
                "  2.  Back here, click '↻ Refresh caseload' to "
                "re-download the columns with the email field included."
            ),
            wraplength=540, justify="left", anchor="w",
            font=ctk.CTkFont(size=12),
        ).pack(fill="x", padx=20, pady=(0, 14))

        def _setup_now() -> None:
            result["value"] = False
            try: dialog.grab_release()
            except Exception: pass
            try: dialog.destroy()
            except Exception: pass
            self._setup_caseload_tool_view_with_help(self.root)

        def _refresh_now() -> None:
            # Abort this fire (the CSV needs to re-download first); kick
            # off the refresh so the next fire sees the email column.
            result["value"] = False
            try: dialog.grab_release()
            except Exception: pass
            try: dialog.destroy()
            except Exception: pass
            self._append_log(
                "Refreshing caseload columns — re-fire the action once "
                "the download finishes.")
            try:
                self._on_caseload_refresh_clicked()
            except Exception as e:
                self._append_log(f"Caseload refresh failed: {e}", error=True)

        def _proceed() -> None:
            result["value"] = True
            try: dialog.grab_release()
            except Exception: pass
            try: dialog.destroy()
            except Exception: pass

        def _skip_session() -> None:
            self._csv_email_warning_skipped = True
            result["value"] = True
            try: dialog.grab_release()
            except Exception: pass
            try: dialog.destroy()
            except Exception: pass

        btn_row = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_row.pack(fill="x", padx=20, pady=(0, 16), side="bottom")
        ctk.CTkButton(
            btn_row, text="Set up instructions", width=150, command=_setup_now,
        ).pack(side="left", padx=4)
        ctk.CTkButton(
            btn_row, text="↻ Refresh caseload", width=150,
            command=_refresh_now, **SECONDARY_BTN_KWARGS,
        ).pack(side="left", padx=4)
        ctk.CTkButton(
            btn_row, text="Skip this session", width=140,
            command=_skip_session, **SECONDARY_BTN_KWARGS,
        ).pack(side="right", padx=4)
        ctk.CTkButton(
            btn_row, text="Proceed anyway", width=130,
            command=_proceed, **SECONDARY_BTN_KWARGS,
        ).pack(side="right", padx=4)

        dialog.protocol("WM_DELETE_WINDOW", _proceed)
        self.root.wait_window(dialog)
        return result["value"]

    def _apply_advanced_mode(self) -> None:
        """Show/hide advanced-only UI elements based on
        `self.settings.advanced_mode`. Called once after startup
        (so the launcher boots into the right state) and again
        whenever the user toggles the setting.

        Per-scenario fields are handled inside ScenarioEditor's own
        `apply_advanced_visibility` method, which respects the "show
        if already configured" rule (a scenario with variables
        defined keeps the section visible in basic mode so the user
        can see and edit them)."""
        advanced = self.settings.advanced_mode

        # Toolbar 🔬 Capture button — pure dev tool, no use case for
        # basic users. Hide entirely in basic mode.
        try:
            if advanced:
                self.capture_btn.pack(side="left", padx=(8, 0))
            else:
                self.capture_btn.pack_forget()
        except Exception:
            pass

        # Open log (the log file) is an extra control a first-time user
        # rarely needs — hide in basic mode.
        try:
            if advanced:
                self._btn_open_log.pack(side="left", padx=(8, 0))
            else:
                self._btn_open_log.pack_forget()
        except Exception:
            pass

        # Push the new visibility into every open scenario tab.
        try:
            for editor in self.scenario_editors.values():
                editor.apply_advanced_visibility(advanced)
        except Exception:
            pass

    def _on_capture_toggle(self) -> None:
        """Toggle network capture for Salesforce REST-API discovery.
        Starts the worker's request listener; on stop, dumps the
        accumulated log to a timestamped JSON file in the user
        config dir."""
        if not self.worker.ready_event.is_set():
            self._append_log("Browser not ready yet — wait and try again.")
            return
        if not self._capture_active:
            self.worker.start_request_capture()
            self._capture_active = True
            self.capture_btn.configure(text="⏹ Stop capture")
            self._append_log(
                "Network capture STARTED. Fire a note manually (use a "
                "action or click Submit in Salesforce yourself). "
                "Click ⏹ Stop capture when done."
            )
            return
        log = self.worker.stop_request_capture()
        self._capture_active = False
        self.capture_btn.configure(text="🔬 Capture")
        if not log:
            self._append_log("Capture stopped; no Salesforce write requests recorded.")
            return
        import json
        from datetime import datetime
        out_path = (
            USER_CONFIG_DIR / f"capture-{datetime.now():%Y%m%d-%H%M%S}.json"
        )
        try:
            out_path.write_text(
                json.dumps(log, indent=2, default=str),
                encoding="utf-8",
            )
        except Exception as e:
            self._append_log(f"Capture stopped but save failed: {e}")
            return
        self._append_log(
            f"Capture stopped. {len(log)} request(s) saved to "
            f"{out_path.name} in {USER_CONFIG_DIR}. Scrub auth tokens "
            "from the headers before sharing."
        )

    def _on_open_templates_folder(self) -> None:
        """Open the user's templates directory in Explorer / Finder.
        Lets the user drop in new .html templates or signature images
        without restarting the launcher (the body-template dropdown
        repopulates on next ScenarioEditor rebuild — Save / Revert /
        scenario tab switch all rebuild)."""
        import os
        try:
            os.startfile(str(templates_dir()))
        except AttributeError:
            # os.startfile is Windows-only — fall back for future
            # Mac/Linux support.
            import subprocess, sys
            opener = {"darwin": "open"}.get(sys.platform, "xdg-open")
            try:
                subprocess.Popen([opener, str(templates_dir())])
            except Exception as e:
                self._append_log(f"Couldn't open templates folder: {e}")
        except Exception as e:
            self._append_log(f"Couldn't open templates folder: {e}")

    def _on_caseload_refresh_clicked(self) -> None:
        """Manual caseload-CSV refresh from the toolbar button. Blocks
        the UI with a wait_variable nested mainloop while running; the
        busy-state guard keeps buttons disabled so the user can't
        stack another action on top."""
        if self._is_busy:
            self._append_log(
                "Already working on something — wait for the current "
                "task to finish."
            )
            return
        if not self.worker.ready_event.is_set():
            self._append_log("Browser not ready yet — wait and try again.")
            return
        self._set_busy("Refreshing caseload + Essential Actions…")
        self._append_log("Refreshing caseload CSV (manual)...")
        try:
            success, message = self._download_caseload_csv_blocking()
            if success:
                self._append_log(f"Caseload CSV: {message}")
            else:
                self._append_log(f"Caseload CSV refresh failed: {message}")
            # Scrape the Essential Actions dashboard in the same refresh.
            self._append_log("Scraping Essential Actions dashboard…")
            self._apply_ea_scrape(self._read_ea_dashboard_blocking())
        finally:
            self._set_idle()
        # After the (blocking) refresh, kick the background live task
        # pass/fail pass — only if the setting is "every refresh".
        self._maybe_bulk_scrape_task_status("refresh")

    # ----- Busy-state guard -----

    # Braille-pattern animation frames — looks like a smooth circular
    # spinner in any monospaced font and renders cleanly in CTkLabel.
    _SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

    def _show_browser_lock(self) -> None:
        """Cover the launcher's browser window with a topmost, semi-opaque
        scrim that swallows clicks/keys, so the user can't change the
        active record while automation drives it. Tracks the window's rect
        so it stays aligned if the browser is raised/moved mid-run.
        Windows-only; best-effort no-op otherwise."""
        if sys.platform != "win32":
            return
        if getattr(self, "_lock_overlay", None) is not None:
            return
        rect = self._browser_window_rect()
        if rect is None:
            return  # can't locate / window minimized → skip (nothing to cover)
        x, y, w, h = rect
        try:
            ov = tk.Toplevel(self.root)
            ov.overrideredirect(True)
            ov.attributes("-topmost", True)
            try:
                ov.attributes("-alpha", 0.6)
            except Exception:
                pass
            ov.geometry(f"{w}x{h}+{x}+{y}")
            ov.configure(bg="#0d1117", cursor="watch")
            tk.Label(
                ov,
                text="🔒  Automation running\n\n"
                     "Please don't click in the browser until this finishes.",
                bg="#0d1117", fg="#ffffff",
                font=("Segoe UI", 16, "bold"), justify="center",
            ).place(relx=0.5, rely=0.5, anchor="center")
            # Swallow any interaction that reaches the scrim.
            for seq in ("<Button-1>", "<Button-2>", "<Button-3>",
                        "<Key>", "<MouseWheel>"):
                ov.bind(seq, lambda e: "break")
            ov.lift()
            self._lock_overlay = ov
            self._track_lock_overlay()
        except Exception:
            self._lock_overlay = None

    def _lock_browser_for_run(self) -> None:
        """Disable OS input to the browser window AND show the scrim, so a
        stray click can't disturb automation (Salesforce or Mongoose)."""
        try:
            self.worker.set_browser_enabled(False)
        except Exception:
            pass
        self._show_browser_lock()

    def _unlock_browser_after_run(self) -> None:
        self._hide_browser_lock()
        try:
            self.worker.set_browser_enabled(True)
        except Exception:
            pass

    def _hide_browser_lock(self) -> None:
        ov = getattr(self, "_lock_overlay", None)
        self._lock_overlay = None
        if ov is not None:
            try:
                ov.destroy()
            except Exception:
                pass

    def _browser_window_rect(self):
        """(x, y, w, h) of the launcher's browser window in screen coords,
        or None if it can't be located / is minimized / off-screen."""
        if sys.platform != "win32":
            return None
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            hwnd = self.worker._locate_browser_hwnd()
            if not hwnd or user32.IsIconic(hwnd):
                return None
            r = wintypes.RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
                return None
            w, h = r.right - r.left, r.bottom - r.top
            if w <= 0 or h <= 0 or r.left <= -30000 or r.top <= -30000:
                return None
            return (r.left, r.top, w, h)
        except Exception:
            return None

    def _foreground_is_ours(self) -> bool:
        """True if the OS foreground window belongs to the launcher or to
        our browser. Used to show the lock scrim ONLY while the user is
        actually looking at Salesforce — otherwise a topmost scrim would
        float over whatever other app they switched to. True off Windows."""
        if sys.platform != "win32":
            return True
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            fg = user32.GetForegroundWindow()
            if not fg:
                return False
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(fg, ctypes.byref(pid))
            allowed = {os.getpid()}
            bp = getattr(self.worker, "_browser_pid", None)
            if bp:
                allowed.add(bp)
            return pid.value in allowed
        except Exception:
            return True

    def _track_lock_overlay(self) -> None:
        """Keep the scrim aligned over the browser while it's shown, but
        only WHILE our app is in the foreground — hide it the moment the
        user switches to another app so it never floats over their other
        windows. Re-shows when they return to Salesforce."""
        ov = getattr(self, "_lock_overlay", None)
        if ov is None:
            return
        try:
            rect = self._browser_window_rect()
            if self._foreground_is_ours() and rect is not None:
                x, y, w, h = rect
                ov.geometry(f"{w}x{h}+{x}+{y}")
                ov.deiconify()
                ov.attributes("-topmost", True)
                ov.lift()
            else:
                ov.withdraw()
        except Exception:
            pass
        self.root.after(150, self._track_lock_overlay)

    def _set_busy(self, message: str) -> None:
        """Enter a busy state: disable action buttons, show a spinner
        + status label, and start the animation. Idempotent — calling
        again while busy just updates the message."""
        was_already_busy = self._is_busy
        self._is_busy = True
        self._busy_message = message
        try:
            self.caseload_refresh_btn.configure(state="disabled")
        except Exception:
            pass
        for btn in self.scenario_buttons.values():
            try:
                btn.configure(state="disabled")
            except Exception:
                pass
        if not was_already_busy:
            self._tick_spinner()  # kicks off the animation loop

    def _set_idle(self) -> None:
        """Leave the busy state — re-enable action buttons, clear the
        spinner label and its background tag."""
        self._is_busy = False
        self._busy_message = ""
        # Safety: never leave the browser scrim/disable up if a run exited
        # without its own cleanup.
        self._hide_browser_lock()
        try:
            self.worker.set_browser_enabled(True)
        except Exception:
            pass
        try:
            self.busy_label.configure(text="", fg_color="transparent")
        except Exception:
            pass
        try:
            self.caseload_refresh_btn.configure(state="normal")
        except Exception:
            pass
        for btn in self.scenario_buttons.values():
            try:
                btn.configure(state="normal")
            except Exception:
                pass

    def _tick_spinner(self) -> None:
        if not self._is_busy:
            return
        try:
            frame = self._SPINNER_FRAMES[
                self._busy_spinner_index % len(self._SPINNER_FRAMES)
            ]
            # Yellow pill — pad with non-breaking spaces so the
            # background tag is wide enough to stand out visually.
            self.busy_label.configure(
                text=f"  {frame}  WORKING — {self._busy_message}  ",
                fg_color=("#ffefc1", "#5a4500"),
            )
        except Exception:
            return
        self._busy_spinner_index += 1
        try:
            self.root.after(90, self._tick_spinner)
        except Exception:
            pass

    def _get_caseload_columns(self) -> list[str]:
        """Current caseload columns presented as user-facing display
        names (e.g. 'Last Assigned CI Contact' instead of the raw CSV
        header 'MyCourseContact'). Empty list when no CSV has been
        loaded yet. The runtime filter engine reverses this via
        `caseload_csv.resolve_column`, so the dropdown can save the
        display name and still match the CSV at fire time."""
        if not self._caseload_rows:
            return []
        return [
            caseload_csv.display_for_column(h)
            for h in self._caseload_rows[0].keys()
            if not _is_task_facet_col(h)
        ]

    def _refresh_caseload_columns_for_editor(self) -> list[str]:
        """↻ Refresh columns button in the filter editor: forces a
        fresh CSV download, then returns the new column list. Routes
        through _on_caseload_refresh_clicked so the busy spinner +
        button-disable behavior is identical to clicking the
        toolbar ↻ Caseload button."""
        self._on_caseload_refresh_clicked()
        return self._get_caseload_columns()

    def _ignored_required_columns(self) -> set:
        """CSV columns the user has chosen to exclude from the
        required-columns check (Settings → required_columns_ignore)."""
        import re as _re
        raw = (getattr(self.settings, "required_columns_ignore", "") or "").strip()
        if not raw:
            return set()
        return {c.strip() for c in _re.split(r"[,\n]", raw) if c.strip()}

    def _required_caseload_columns(self) -> set:
        """CSV columns the currently-enabled viewer features depend on.
        Driven by the quick-view field selection plus the status filter and
        latest-note features, so the check only flags what's actually used."""
        req = {"StudentID", "Name", "CourseCode"}
        p = getattr(self, "caseload_panel", None)
        if p is not None:
            try:
                csv_for = {k: csvk for k, _l, csvk, _kind in p.QUICK_VIEW_CATALOG}
                for k in p._quickview_field_keys():
                    c = csv_for.get(k)
                    if c:
                        req.add(c)
                    if k == "tasks":
                        req.update({"Task1", "Task2", "Task3"})
            except Exception:
                pass
        # Feature columns: latest-note preview + the status filter.
        req.update({"LatestCourseNote", "MyCourseContact", "LatestTaskStatus"})
        return req

    def _check_required_caseload_columns(self, rows, *, silent: bool) -> None:
        """Warn when the loaded CSV is missing columns the viewer needs
        (so a wrong/narrow Caseload view doesn't silently break features).
        Logs every reload; offers the setup instructions once per session.
        Honors the user's Settings ignore-list."""
        if not rows or not hasattr(self, "log"):
            return
        have = set(rows[0].keys())
        ignore = self._ignored_required_columns()
        missing = sorted(
            c for c in self._required_caseload_columns()
            if c not in have and c not in ignore)
        if not missing:
            self._req_cols_warned = False
            return
        disp = ", ".join(caseload_csv.display_for_column(c) for c in missing)
        self._append_log(
            f"⚠ Caseload CSV is missing {len(missing)} column(s) the viewer "
            f"uses: {disp}. Add them to your Caseload view and refresh (↻), "
            "or exclude them in ⚙ Settings.", error=True)
        # One interactive nudge per session (not on the early silent load).
        if not silent and not getattr(self, "_req_cols_warned", False):
            self._req_cols_warned = True
            try:
                if ask_yes_no_topmost(
                    self.root, "Caseload columns missing",
                    "Your caseload export is missing columns the viewer "
                    f"uses:\n\n{disp}\n\nAdd them to your Salesforce Caseload "
                    "list view, then re-download (↻ Caseload). You can also "
                    "exclude specific columns from this check in Settings.\n\n"
                    "Show the step-by-step instructions now?",
                    yes_label="Show instructions", no_label="Later",
                ):
                    self._setup_caseload_tool_view_with_help(self.root)
            except Exception:
                pass

    def _reload_caseload_cache(self, *, silent: bool = False) -> bool:
        """Read CASELOAD_CSV_PATH into self._caseload_rows. Returns
        True on success, False if the file doesn't exist or can't be
        parsed. Called on startup (silent=True — the log widget isn't
        built yet) and from the manual Reload button (silent=False
        so the user sees the result)."""
        try:
            rows = caseload_csv.load_caseload_csv(CASELOAD_CSV_PATH)
        except FileNotFoundError:
            if not silent:
                self._append_log(
                    f"No caseload CSV found at {CASELOAD_CSV_PATH}. "
                    "Falling back to DOM scrape for batches."
                )
            self._caseload_rows = None
            self._caseload_csv_mtime = None
            return False
        except Exception as e:
            if not silent:
                self._append_log(f"Caseload CSV load failed: {e}")
            self._caseload_rows = None
            self._caseload_csv_mtime = None
            return False
        self._caseload_rows = rows
        self._caseload_csv_mtime = caseload_csv.csv_mtime(CASELOAD_CSV_PATH)
        # Cache whether the CSV carries a student-email column so the
        # pre-batch warning + Settings status line don't have to scan
        # rows again. Refreshed on every cache reload — picks up the
        # change immediately when the user adds the column.
        self._csv_has_student_email = _csv_has_student_email_column(rows)
        if not silent:
            age = caseload_csv.csv_age_human(CASELOAD_CSV_PATH)
            self._append_log(
                f"Caseload cache: {len(rows)} rows from "
                f"{CASELOAD_CSV_PATH.name} ({age})"
            )
            if not self._csv_has_student_email:
                self._append_log(
                    "  ↳  no student-email column detected — batch "
                    "emails will use the slower per-student row scrape. "
                    "Set up Caseload Tool view in ⚙ Settings to fix."
                )
        # Re-attach scraped Essential Actions + live task pass/fail to the
        # freshly-loaded rows (reload rebuilds rows from the CSV, dropping
        # the synthetic columns).
        self._apply_ea_to_rows()
        self._apply_task_status_to_rows()
        self._refresh_caseload_panel()
        self._check_required_caseload_columns(rows, silent=silent)
        # Snapshot the dynamic fields into the local history DB (non-fatal:
        # the panel is already rendered, and a DB failure must not break the
        # reload). Runs on the silent startup reload too — that's the
        # once-a-day capture baseline.
        interval_h = int(getattr(
            self.settings, "history_capture_interval_hours", 24) or 0)
        if interval_h > 0:  # 0 = history capture disabled in Settings
            try:
                summary = history.record_snapshot(
                    rows, self._caseload_csv_mtime, interval_hours=interval_h,
                    note="startup" if silent else "manual",
                )
                self._last_history_summary = summary
                if not silent and summary.get("status") in ("captured", "updated"):
                    n = summary.get("row_count", 0)
                    dep = summary.get("departure_count", 0)
                    msg = f"History: {summary['status']} {n} rows."
                    if dep:
                        fu = sum(1 for d in summary["departures"]
                                 if d["classification"] == "followup")
                        msg += (f"  {dep} departed since last seen "
                                f"({fu} need follow-up).")
                    self._append_log(msg)
                elif not silent and summary.get("warning"):
                    self._append_log(summary["warning"], error=True)
            except Exception as e:
                if not silent:
                    self._append_log(f"(history snapshot skipped: {e})")
        return True

    def _read_clipboard_content(self) -> str:
        """Pull text from clipboard. If image data is also present,
        append [IMAGE NOT INCLUDED] so the placeholder is preserved in
        the note body where the user pasted it."""
        text = ""
        try:
            text = self.root.clipboard_get(type="STRING")
        except tk.TclError:
            pass
        except Exception:
            pass

        has_image = False
        if _HAS_PIL:
            try:
                img = ImageGrab.grabclipboard()
                # grabclipboard returns: Image | list of file paths | None
                has_image = img is not None and not isinstance(img, list)
            except Exception:
                pass

        if has_image:
            text = (text + "\n[IMAGE NOT INCLUDED]") if text else "[IMAGE NOT INCLUDED]"
        return text

    # ----- Window lifecycle -----

    def _hide(self) -> None:
        self.root.iconify()

    def _on_close(self) -> None:
        self._save_window_state()
        # Persist caseload column layout (widths the user drag-resized).
        try:
            panel = getattr(self, "caseload_panel", None)
            if panel is not None:
                panel.persist_column_state()
        except Exception:
            pass
        try:
            if self.hotkey_listener is not None:
                self.hotkey_listener.stop()
        except Exception:
            pass
        self.worker.shutdown()
        # Wait briefly for the worker to close Playwright cleanly, so the
        # process doesn't exit mid-teardown (which makes the driver print an
        # EPIPE). Bounded so closing never hangs.
        try:
            t = getattr(self.worker, "thread", None)
            if t is not None:
                t.join(timeout=4)
        except Exception:
            pass
        self.root.destroy()

    # ----- Status / log -----

    def _post_status(self, msg: str) -> None:
        # Called from the worker thread. root.after can raise "main thread
        # is not in main loop" if Tk isn't in its event loop yet (startup
        # race) or is tearing down — swallow it so the worker never dies.
        try:
            self.root.after(0, lambda: self._update_status_and_log(msg))
        except Exception:
            print(msg, file=sys.stderr)

    def _update_status_and_log(self, msg: str) -> None:
        self.status_var.set(msg)
        self._append_log(msg)

    # Failure-ish phrasing that should stand out in red. Deliberately
    # excludes plain user "cancelled" (that's a choice, not a failure).
    _LOG_ERROR_RE = re.compile(
        r"\b(fail(?:ed|ure|s)?|error|couldn'?t|could not|abort(?:ed)?|"
        r"crash(?:ed)?|not fired|no visible note panel|not delivered|"
        r"no match|skipp(?:ed|ing)|didn'?t open|timed? out|not found)\b",
        re.IGNORECASE,
    )

    def _append_log(self, msg: str, error: Optional[bool] = None) -> None:
        # Defensive: if called before _build_main_pane has created
        # the widget (e.g. very early startup), fall back to stderr
        # so we don't crash the app with AttributeError.
        if not hasattr(self, "log") or self.log is None:
            print(msg, file=sys.stderr)
            return
        if error is None:
            error = bool(self._LOG_ERROR_RE.search(msg))
        self.log.configure(state="normal")
        start = self.log.index("end-1c")
        self.log.insert("end", msg + "\n")
        if error:
            try:
                self.log._textbox.tag_add("logerror", start, "end-1c")
            except Exception:
                pass
        self.log.see("end")
        self.log.configure(state="disabled")

    def _dev_open_mongoose(self) -> None:
        """TEMP dev helper: open the Mongoose dashboard in the launcher's OWN
        browser context so the probe (and later, the texting automation) can
        see it. Then navigate to your inbox + compose view and click Probe
        Text."""
        try:
            if not self.worker.ready_event.is_set():
                self._append_log("Browser not ready yet.")
                return
        except Exception:
            return
        self._append_log("Opening Mongoose in the launcher's browser…")

        def on_done(res):
            def show():
                if not res or res.get("error"):
                    self._append_log(
                        f"Open Mongoose failed: {(res or {}).get('error')}",
                        error=True)
                    return
                self._append_log(
                    f"Mongoose open: {res.get('url') or '(unknown)'}  "
                    "→ navigate to your inbox + compose view, then click "
                    "🧪 Probe Text.")
            try:
                self.root.after(0, show)
            except Exception:
                pass
        self.worker.submit_open_mongoose(on_done)

    def _dev_test_text(self) -> None:
        """TEMP dev: verify the Mongoose compose driver end-to-end. Prompts for a
        REAL student's mobile (must be a contact in the currently-open inbox —
        inboxes are course-scoped), then drives open -> recipient -> message ->
        Preview -> Schedule for TOMORROW 10 AM and commits it. Verify it under
        Scheduled Messages and delete it (won't send until tomorrow). Open
        🐭 Open Mongoose and land on the matching course inbox first."""
        try:
            if not self.worker.ready_event.is_set():
                self._append_log("Browser not ready yet.")
                return
        except Exception:
            return
        dlg = ctk.CTkInputDialog(
            title="Test Text (schedules for tomorrow)",
            text=("Mobile number of a REAL student in the inbox that's open now "
                  "(inboxes are course-scoped, e.g. C769).\n\n"
                  "This SCHEDULES a test text for tomorrow ~10:00 AM and commits "
                  "it — verify it under Scheduled Messages, then DELETE it. It "
                  "won't send until tomorrow.\n\n"
                  "Open 🐭 Open Mongoose and land on the matching inbox first."),
        )
        mobile = (dlg.get_input() or "").strip()
        if not mobile:
            return
        self._append_log(
            f"Test Text: scheduling a test text for {mobile} tomorrow 10 AM "
            "(verify + delete it after)…")

        def on_done(res):
            def show():
                if not res or res.get("error"):
                    self._append_log(
                        f"Test Text failed: {(res or {}).get('error')}",
                        error=True)
                    return
                self._append_log(
                    "Test Text: driver finished — check Scheduled Messages in "
                    "Mongoose for 'TEST – delete me' and delete it.")
            try:
                self.root.after(0, show)
            except Exception:
                pass
        self.worker.submit_test_text(mobile, "Test message — please ignore.", on_done)

    def _dev_probe_text(self) -> None:
        """TEMP dev probe: dump the live Mongoose ("Cadence") texting composer
        DOM so we can find the texting selectors (message body, recipient,
        template + schedule pickers, Send/Schedule buttons). Open a Mongoose
        inbox (sms.mongooseresearch.com) IN THE LAUNCHER'S OWN browser window
        and bring up the compose view first, then click.
        → temp/text_probe.html"""
        try:
            if not self.worker.ready_event.is_set():
                self._append_log("Browser not ready yet.")
                return
        except Exception:
            return
        self._append_log(
            "Probing Mongoose texting… in THE LAUNCHER'S browser window, open a "
            "Mongoose inbox (sms.mongooseresearch.com) + compose view first.")

        def on_done(res):
            def show():
                if not res or res.get("error"):
                    self._append_log(
                        f"Text probe failed: {(res or {}).get('error')}",
                        error=True)
                    return
                url = res.get("url") or ""
                self._append_log(f"Captured page: {url or '(unknown)'}")
                if "mongoose" not in url.lower():
                    self._append_log(
                        "⚠ That is NOT a Mongoose page — the probe captured "
                        "another tab. Open sms.mongooseresearch.com in the "
                        "LAUNCHER'S OWN browser window (not Vivaldi/another "
                        "browser), then probe again.", error=True)
                c = res.get("counts") or {}
                self._append_log(
                    f"Text probe: text_hits={c.get('hits')} "
                    f"dialogs={c.get('dialogs')} fields={c.get('fields')} "
                    f"field_containers={c.get('field_containers')} "
                    f"match_blocks={c.get('match_blocks')}")
                btns = res.get("buttons") or []
                self._append_log(
                    "Visible buttons: "
                    + (" | ".join(btns) if btns else "(none captured)"))
                for t in (res.get("matchTags") or [])[:15]:
                    self._append_log("  text el: " + t)
                html = res.get("html") or ""
                try:
                    p = CASELOAD_CSV_PATH.parent / "temp" / "text_probe.html"
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(html, encoding="utf-8")
                    self._append_log(
                        f"Text deep DOM → {p}  ({len(html)} chars)")
                except Exception as e:
                    self._append_log(
                        f"couldn't write text probe file: {e}", error=True)
            try:
                self.root.after(0, show)
            except Exception:
                pass
        self.worker.submit_probe_text(on_done)

    def _set_followup_date_for(self, student_id: str, date_str: str,
                               on_apply=None) -> None:
        """WRITE the student's Salesforce Followup Date to `date_str`
        (MM/DD/YYYY) via the live caseload list. Blocks the UI briefly (it's
        a real write that drives the browser). on_apply(res) gets
        {ok, value, error} on the UI thread."""
        sid = (student_id or "").strip()
        date_str = (date_str or "").strip()
        if not sid:
            return
        if not date_str:
            self._append_log("Enter a follow-up date first (MM/DD/YYYY).")
            return
        if getattr(self, "_is_busy", False):
            self._append_log("Busy — try again when the current task finishes.")
            return
        if not self.worker.ready_event.is_set():
            self._append_log("Browser not ready yet.")
            return
        self._set_busy(f"Setting follow-up date for {sid}…")

        def on_done(res):
            def apply():
                self._set_idle()
                if not res or res.get("error"):
                    self._append_log(
                        f"Set follow-up date failed: {(res or {}).get('error')}",
                        error=True)
                else:
                    self._append_log(
                        f"Follow-up date set to {res.get('value') or date_str} "
                        f"for {sid}.")
                if on_apply:
                    try:
                        on_apply(res or {})
                    except Exception:
                        pass
            try:
                self.root.after(0, apply)
            except Exception:
                self._set_idle()

        self._append_log(f"Setting follow-up date {date_str} for {sid}…")
        self.worker.submit_set_followup_date(sid, date_str, on_done)

    def _set_followup_note_for(self, student_id: str, note_text: str,
                               on_apply=None) -> None:
        """WRITE the student's Salesforce Followup Note via the live caseload
        list. Blocks the UI briefly (real write). on_apply(res) on the UI
        thread. An empty note clears it."""
        sid = (student_id or "").strip()
        note_text = (note_text or "").strip()
        if not sid:
            return
        if getattr(self, "_is_busy", False):
            self._append_log("Busy — try again when the current task finishes.")
            return
        if not self.worker.ready_event.is_set():
            self._append_log("Browser not ready yet.")
            return
        self._set_busy(f"Setting follow-up note for {sid}…")

        def on_done(res):
            def apply():
                self._set_idle()
                if not res or res.get("error"):
                    self._append_log(
                        f"Set follow-up note failed: {(res or {}).get('error')}",
                        error=True)
                else:
                    self._append_log(f"Follow-up note set for {sid}.")
                if on_apply:
                    try:
                        on_apply(res or {})
                    except Exception:
                        pass
            try:
                self.root.after(0, apply)
            except Exception:
                self._set_idle()

        self._append_log(f"Setting follow-up note for {sid}…")
        self.worker.submit_set_followup_note(sid, note_text, on_done)

    def _fetch_task_status_for(self, student_id: str, on_apply) -> None:
        """On-demand: fetch a student's real per-task pass/fail from the
        live Caseload list (the CSV can't carry it) and hand the result to
        `on_apply(statuses)`. Cached per session; non-blocking; skipped
        while busy or before the browser is ready."""
        sid = (student_id or "").strip()
        if not sid:
            return
        cache = self.__dict__.setdefault("_task_status_cache", {})
        if sid in cache:
            try:
                on_apply(cache[sid])
            except Exception:
                pass
            return
        if getattr(self, "_is_busy", False):
            return  # don't disturb the live browser mid-run
        try:
            if not self.worker.ready_event.is_set():
                return
        except Exception:
            return

        def on_done(res):
            def apply():
                if res and not res.get("error"):
                    st = res.get("statuses") or {}
                    cache[sid] = st
                    try:
                        on_apply(st)
                    except Exception:
                        pass
                else:
                    msg = (res or {}).get("error")
                    if msg:
                        self._append_log(
                            f"Task status fetch failed for {sid}: {msg}",
                            error=True)
            try:
                self.root.after(0, apply)
            except Exception:
                pass
        self.worker.submit_fetch_task_status(sid, on_done)

    def _sync_name_cap_mode(self) -> None:
        """Push the user's name-capitalization preference into the module
        global the name-variable builders read (the BrowserWorker one has no
        settings access). Call at startup + after the setting changes."""
        global _NAME_CAP_MODE
        _NAME_CAP_MODE = (getattr(self.settings, "name_capitalization",
                                  "standard") or "standard")

    def _minimize_browser(self) -> None:
        """Minimize the launcher's browser once the startup caseload load is
        finished (login's done + data's in), so it's out of the way."""
        try:
            self.worker._minimize_browser_window()
            self._append_log("Caseload loaded — minimized the browser.")
        except Exception:
            pass

    def _maybe_bulk_scrape_task_status(
        self, reason: str, on_complete=None,
    ) -> None:
        """Background '2a' pass: after a refresh, scroll the live Caseload
        list once and read every student's real per-task pass/fail (the
        colour the CSV export drops), populating `_task_status_cache` so the
        grid Task cells get ✅/❌/🟦 glyphs and quick-view badges colour
        instantly. Gated by the `task_status_scrape_mode` setting:
          off     → never;
          restart → only the startup pass  (reason='startup');
          refresh → startup AND every manual ↻  (reason='refresh').
        Non-blocking: queued on the worker, so it never freezes the UI and
        simply runs after whatever refresh just finished. `on_complete` (if
        given) runs once this is done — including the skipped paths — so the
        startup caller can minimize the browser only AFTER the scrape (the
        scroll needs the window un-minimized to render)."""
        def _finish():
            if on_complete:
                try:
                    on_complete()
                except Exception:
                    pass

        mode = (getattr(self.settings, "task_status_scrape_mode", "restart")
                or "restart")
        if mode == "off":
            _finish()
            return
        if reason == "refresh" and mode != "refresh":
            _finish()
            return
        try:
            if not self.worker.ready_event.is_set():
                _finish()
                return
        except Exception:
            _finish()
            return

        def on_done(res):
            def apply():
                # Yielded to a user action mid-scrape — don't apply anything,
                # just re-run shortly so pass/fail still completes once the
                # worker is free. (on_complete deliberately NOT fired here; it
                # only matters at startup, which never interrupts.)
                if res and res.get("interrupted"):
                    self._append_log(
                        "Task pass/fail scan paused for your action — resuming…")
                    try:
                        self.root.after(
                            1500,
                            lambda: self.worker.submit_scrape_all_task_status(
                                on_done))
                    except Exception:
                        pass
                    return
                if not res or res.get("error"):
                    m = (res or {}).get("error")
                    if m:
                        self._append_log(
                            f"Live task pass/fail scrape failed: {m}",
                            error=True)
                    _finish()
                    return
                by_sid = res.get("by_sid") or {}
                cache = self.__dict__.setdefault("_task_status_cache", {})
                cache.update(by_sid)
                self._task_status_scraped = True
                # Sanity check: how many scraped Student IDs actually JOIN to
                # a caseload row (catches a leading-zero / string-vs-int
                # mismatch), plus a per-state tally to compare against the
                # ⏱ Probe 2a numbers.
                rows = getattr(self, "_caseload_rows", None) or []
                csv_sids = {(r.get("StudentID") or "").strip()
                            for r in rows}
                matched = sum(1 for sid in by_sid if sid in csv_sids)
                tally: dict = {}
                for st in by_sid.values():
                    for info in st.values():
                        s = info.get("state", "?")
                        tally[s] = tally.get(s, 0) + 1
                tstr = " ".join(f"{k}={v}" for k, v in sorted(tally.items()))
                self._append_log(
                    f"Live task pass/fail: {len(by_sid)} student(s) scraped, "
                    f"{matched} joined to caseload rows. Tasks: {tstr}")
                if by_sid and matched < len(by_sid):
                    self._append_log(
                        f"  ⚠ {len(by_sid) - matched} scraped student(s) "
                        "didn't match a caseload StudentID — likely a "
                        "leading-zero/format mismatch in the join.",
                        error=True)
                # Refresh the synthetic 'Task Status' filter column from the
                # fresh cache, then re-render so grid Task cells pick up the
                # glyphs and any open quick-view badges recolour.
                self._apply_task_status_to_rows()
                self._refresh_caseload_panel()
                _finish()
            try:
                self.root.after(0, apply)
            except Exception:
                pass

        self._append_log("Reading live task pass/fail in the background…")
        self.worker.submit_scrape_all_task_status(on_done)

    def _review_notes(self, query: str, label: str, panel) -> None:
        """Fetch a student's notes on the worker thread and render them into
        the caseload panel's note viewer. Non-blocking — the callback
        marshals back to the UI thread. The fetch navigates to the record
        (that's the 'open student' part of the flow)."""
        def on_done(res):
            def render():
                try:
                    if res and not res.get("error"):
                        notes = res.get("notes") or []
                        panel.show_notes(label, notes)
                        if notes:
                            self._append_log(
                                f"Notes for {label}: {len(notes)} loaded.")
                        else:
                            t = res.get("timings", {})
                            self._append_log(
                                f"Notes for {label}: 0 found "
                                f"(tab={t.get('tab')}, cells={t.get('all_cells')}, "
                                f"load={t.get('notes_load_ms')}ms, "
                                f"url={t.get('url')}).")
                    else:
                        msg = (res or {}).get("error") or "no notes"
                        panel.show_notes_error(label, msg)
                        self._append_log(
                            f"Notes for {label} failed: {msg}", error=True)
                except Exception as e:
                    self._append_log(f"Notes render error: {e}", error=True)
            try:
                self.root.after(0, render)
            except Exception:
                pass
        self.worker.submit_fetch_notes(query, on_done)

    def _reapply_notes_font(self, size=None) -> None:
        p = getattr(self, "caseload_panel", None)
        if p is not None:
            try:
                p._refresh_notes_font()
            except Exception:
                pass

    def _load_ema_map(self) -> dict:
        import json
        raw = (getattr(self.settings, "ema_report_map", "") or "").strip()
        if not raw:
            return {}
        try:
            d = json.loads(raw)
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _save_ema_map(self, m: dict) -> None:
        import json
        self.settings.ema_report_map = json.dumps(m)
        save_settings(self.settings)

    def _open_task_report(self, student_id: str, course_code: str,
                          task_num: int, label: str) -> None:
        """Open the EMA Score Report for a student's task in the default
        browser. The URL is built from the student's ID + a per-course map
        of courseId/taskId. The map is seeded (once per course+task) by
        pasting a score-report URL the first time — see _seed_ema_and_open."""
        student_id = (student_id or "").strip()
        course_code = (course_code or "").strip()
        if not student_id:
            self._append_log(
                f"No Student ID for {label}; can't open score report.",
                error=True)
            return
        entry = (self._load_ema_map().get(course_code) or {}).get(str(task_num))
        if entry and entry.get("course_id") and entry.get("task_id"):
            url = build_ema_url(student_id, entry["course_id"],
                                entry["task_id"])
            self._append_log(
                f"Opening {course_code} Task {task_num} EMA Score Report "
                f"for {label}.")
            try:
                os.startfile(url)
            except Exception as e:
                self._append_log(f"Couldn't open link: {e}", error=True)
            return
        self._seed_ema_and_open(student_id, course_code, task_num, label)

    def _seed_ema_and_open(self, student_id: str, course_code: str,
                           task_num: int, label: str) -> None:
        """First use for a course+task: ask for one score-report URL, learn
        the courseId/taskId from it (shared by all students in the course),
        save it, and open this student's report."""
        dlg = ctk.CTkInputDialog(
            title=f"EMA Score Report — {course_code or 'course'} "
                  f"Task {task_num}",
            text=(f"First time for {course_code or 'this course'} "
                  f"Task {task_num} — paste its score-report URL once and "
                  f"it's remembered for every {course_code} student.\n\n"
                  f"Where to find it:\n"
                  f"1. In Salesforce, expand the student's caseload row "
                  f"(far-left arrow).\n"
                  f"2. Open the “Performance Assessments” tab.\n"
                  f"3. Click the EMA Score Report link for Task {task_num}.\n"
                  f"4. Let the new tab finish loading, then copy the URL "
                  f"from the address bar — it must end in “/score-report” "
                  f"(ignore any brief “/cb?code=…” sign-in URL) — and paste "
                  f"it below."))
        raw = dlg.get_input()
        if not raw:
            return
        parsed = parse_ema_url(raw)
        if not parsed:
            self._append_log(
                "That isn't a tasks.wgu.edu …/score-report URL — nothing "
                "saved.", error=True)
            return
        m = self._load_ema_map()
        m.setdefault(course_code, {})[str(task_num)] = {
            "course_id": parsed["course_id"], "task_id": parsed["task_id"]}
        self._save_ema_map(m)
        self._append_log(
            f"Saved {course_code} Task {task_num} score-report mapping "
            f"(course {parsed['course_id']}, task {parsed['task_id']}). "
            f"All {course_code} students' Task {task_num} badges now work.")
        url = build_ema_url(student_id, parsed["course_id"], parsed["task_id"])
        try:
            os.startfile(url)
        except Exception as e:
            self._append_log(f"Couldn't open link: {e}", error=True)

    def _clear_ema_map(self) -> None:
        self.settings.ema_report_map = ""
        save_settings(self.settings)
        self._append_log("Cleared saved EMA Score Report links.")

    def _persist_font_size(self, channel: str, n: int) -> None:
        if channel in UI_FONT_CHANNELS:
            setattr(self.settings, f"font_{channel}", int(n))
            save_settings(self.settings)

    def _warn_if_caseload_stale(self, context: str = "") -> None:
        """Red activity-log warning when the cached caseload CSV is older
        than the user's alert threshold (Settings → caseload_stale_minutes;
        0 = off). Advisory only — doesn't block the action."""
        mins = int(getattr(self.settings, "caseload_stale_minutes", 0) or 0)
        if mins <= 0:
            return
        mt = caseload_csv.csv_mtime(CASELOAD_CSV_PATH)
        if mt is None:
            return
        if (datetime.now() - mt).total_seconds() / 60 >= mins:
            where = f" before {context}" if context else ""
            age = caseload_csv.csv_age_human(CASELOAD_CSV_PATH)
            self._append_log(
                f"⚠ Caseload data is {age} old (over your alert "
                f"threshold){where} — click ↻ Caseload to refresh.",
                error=True,
            )

    def _toggle_log(self) -> None:
        """Collapse/expand the activity-log box to reclaim vertical
        space. Collapsed leaves just the header row visible."""
        if self._log_visible:
            self.log_tabview.grid_remove()
            self._log_pane.grid_rowconfigure(6, weight=0)
            self._log_collapse_btn.configure(text="▶ Activity log")
            self._log_visible = False
        else:
            self.log_tabview.grid()
            self._log_pane.grid_rowconfigure(6, weight=1)
            self._log_collapse_btn.configure(text="▼ Activity log")
            self._log_visible = True

    def _copy_log(self) -> None:
        """Copy the full activity-log text to the clipboard, with a brief
        'Copied ✓' confirmation on the button."""
        try:
            text = self.log.get("1.0", "end-1c")
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
        except Exception:
            return
        try:
            self._log_copy_btn.configure(text="Copied ✓")
            self.root.after(
                1200, lambda: self._log_copy_btn.configure(text="📋 Copy"))
        except Exception:
            pass

    # ----- Note log (tabs + CSV) -----

    def _post_note_filed(self, entry: NoteLogEntry) -> None:
        """Called from the worker thread when a scenario completes
        successfully. Bounces onto the Tk main thread to update UI."""
        self.root.after(0, lambda: self._record_note(entry))

    def _record_note(self, entry: NoteLogEntry) -> None:
        self.note_log_entries.append(entry)
        self._append_to_csv(entry)
        self._ensure_note_tab(entry.tab_key)
        self._append_to_note_tab(entry)

    def _append_to_csv(self, entry: NoteLogEntry) -> None:
        try:
            self._migrate_csv_if_needed()
            existed = NOTE_LOG_CSV.exists()
            with open(NOTE_LOG_CSV, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if not existed:
                    w.writerow(CSV_HEADER)
                w.writerow([
                    entry.timestamp.isoformat(timespec="seconds"),
                    entry.scenario,
                    entry.course_code,
                    entry.student,
                    entry.student_id,
                    entry.student_email,
                    entry.pm_name,
                    entry.pm_email,
                    "true" if entry.submitted else "false",
                ])
        except PermissionError:
            # Almost always Excel / another viewer locking the file.
            self.status_var.set(
                f"LOG LOCKED — close {NOTE_LOG_CSV.name} in Excel / Notepad"
            )
            self._append_log(
                f"!! Could not write to {NOTE_LOG_CSV.name} — another program "
                f"(Excel?) has it open. Close it and retry."
            )
        except Exception as e:
            self.status_var.set(f"LOG WRITE FAILED: {e}")
            self._append_log(f"!! Could not append to log file: {e}")

    def _migrate_csv_if_needed(self) -> None:
        """If the existing CSV has an older/different header, rewrite
        it with the current schema. Honors CSV_COLUMN_RENAMES so old
        rows' data lands in the right new column (e.g. old `email`
        was actually the PM's, so it moves to `pm_email`)."""
        if not NOTE_LOG_CSV.exists():
            return
        try:
            with open(NOTE_LOG_CSV, "r", encoding="utf-8", newline="") as f:
                rows = list(csv.reader(f))
            if not rows or rows[0] == CSV_HEADER:
                return
            has_header = bool(rows[0]) and rows[0][0] == "timestamp"
            old_header = rows[0] if has_header else CSV_HEADER[: len(rows[0])]
            data_rows = rows[1:] if has_header else rows

            # Map each old column index -> new column index via the
            # rename table, then drop ones that don't exist anymore.
            old_to_new: dict[int, int] = {}
            for old_idx, name in enumerate(old_header):
                new_name = CSV_COLUMN_RENAMES.get(name, name)
                if new_name in CSV_HEADER:
                    old_to_new[old_idx] = CSV_HEADER.index(new_name)

            with open(NOTE_LOG_CSV, "w", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                w.writerow(CSV_HEADER)
                for row in data_rows:
                    new_row = [""] * len(CSV_HEADER)
                    for old_idx, value in enumerate(row):
                        new_idx = old_to_new.get(old_idx)
                        if new_idx is not None:
                            new_row[new_idx] = value
                    w.writerow(new_row)
        except Exception as e:
            self._append_log(f"(could not migrate log file: {e})")

    def _ensure_note_tab(self, tab_key: str) -> None:
        if tab_key in self.note_tabs:
            return
        tab = self.log_tabview.add(tab_key)
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(0, weight=1)
        list_frame = ctk.CTkScrollableFrame(tab)
        list_frame.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
        list_frame.grid_columnconfigure(0, weight=1)
        ctk.CTkButton(
            tab, text="Remove this tab",
            command=lambda k=tab_key: self._remove_note_tab(k),
            **SECONDARY_BTN_KWARGS, width=140,
        ).grid(row=1, column=0, padx=4, pady=(0, 4), sticky="e")
        self.note_tabs[tab_key] = {"frame": tab, "list": list_frame, "row": 0}
        self.log_tabview.set(tab_key)  # auto-focus the new tab

    def _append_to_note_tab(self, entry: NoteLogEntry) -> None:
        meta = self.note_tabs.get(entry.tab_key)
        if not meta:
            return
        ctk.CTkLabel(
            meta["list"], text=entry.display, anchor="w",
            font=ctk.CTkFont(size=12),
        ).grid(row=meta["row"], column=0, sticky="ew", padx=4, pady=1)
        meta["row"] += 1

    def _remove_note_tab(self, tab_key: str) -> None:
        try:
            self.log_tabview.delete(tab_key)
        except Exception:
            pass
        self.note_tabs.pop(tab_key, None)

    # ----- Multi-match picker -----

    def _post_multiple_matches(self, query: str, names: list[str]) -> None:
        self.root.after(0, lambda: self._show_match_picker(query, names))

    def _show_match_picker(self, query: str, names: list[str]) -> None:
        dialog = ctk.CTkToplevel(self.root)
        dialog.title(f"Pick a student")
        dialog.geometry("420x320")
        dialog.transient(self.root)
        dialog.attributes("-topmost", True)
        dialog.grab_set()

        ctk.CTkLabel(
            dialog,
            text=f"{len(names)} matches for {query!r}. Click one to navigate.",
        ).pack(padx=20, pady=(15, 8))

        scroll = ctk.CTkScrollableFrame(dialog)
        scroll.pack(fill="both", expand=True, padx=20, pady=4)
        for name in names:
            ctk.CTkButton(
                scroll, text=name, anchor="w", height=30,
                command=lambda n=name: self._on_match_picked(n, dialog),
            ).pack(fill="x", pady=2)

        ctk.CTkButton(
            dialog, text="Cancel", command=dialog.destroy,
            **SECONDARY_BTN_KWARGS, width=90,
        ).pack(pady=10)

    def _on_match_picked(self, name: str, dialog) -> None:
        try:
            dialog.destroy()
        except Exception:
            pass
        # Re-submit the search with the exact name — re-uses the worker's
        # match cascade. Since the name is unique and full, priority 1
        # (exact cell match) should fire and click without ambiguity.
        self.search_var.set(name)
        self.worker.submit_find_student(name)

    def _open_log_file(self) -> None:
        if not NOTE_LOG_CSV.exists():
            self._append_log(f"(no log file yet at {NOTE_LOG_CSV})")
            return
        try:
            os.startfile(str(NOTE_LOG_CSV))
        except Exception as e:
            self._append_log(f"(could not open log file: {e})")

    # ----- Entry -----

    def run(self) -> None:
        self.root.mainloop()


_INSTANCE_LOCK_FH = None


def _acquire_single_instance_lock() -> bool:
    """Best-effort single-instance guard via an OS file lock. Returns True
    if we acquired it (or couldn't check — fail open), False if another
    launcher instance already holds it. The lock is released automatically
    by the OS on exit/crash, so a force-killed instance never wedges it.

    Running two instances against the same persistent browser profile is
    what produces the Playwright EPIPE (broken pipe) at launch — one
    driver's pipe dies fighting over the profile."""
    global _INSTANCE_LOCK_FH
    try:
        import msvcrt
    except Exception:
        return True  # non-Windows / no msvcrt — don't block launch
    try:
        lock_path = CASELOAD_CSV_PATH.parent / "launcher.lock"
        fh = open(lock_path, "a+")
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            fh.close()
            return False  # another instance holds the lock
        _INSTANCE_LOCK_FH = fh  # keep open for the process lifetime
        return True
    except Exception:
        return True  # any error → fail open (never block on the guard)


def main() -> None:
    if not _acquire_single_instance_lock():
        try:
            import tkinter.messagebox as _mb
            _r = tk.Tk()
            _r.withdraw()
            _r.attributes("-topmost", True)
            _mb.showwarning(
                "Caseload Notes already running",
                "Another Caseload Notes window is already open.\n\n"
                "Close it first (check the taskbar) before launching again. "
                "Running two at once breaks the shared browser session.")
            _r.destroy()
        except Exception:
            print("Caseload Notes is already running — close the other "
                  "window first.", file=sys.stderr)
        return
    App().run()


if __name__ == "__main__":
    main()
