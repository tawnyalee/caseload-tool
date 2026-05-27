"""Caseload Note Automation — launcher.

Two panes side by side:
- Left:  status, course-code field, scenario buttons, activity log
- Right: collapsible editor with one tab per scenario, fields laid out
         like the Caseload note form. Edits write back to notes.yaml,
         scenarios reload, hotkeys re-register.

Global hotkeys (defined in notes.yaml) trigger scenarios anywhere on
the system. Bare F-keys are claimed system-wide so the browser doesn't
also react to them.

Usage:
    python -m scripts.launcher
"""
import csv
import os
import queue
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import customtkinter as ctk
import tkinter as tk
import yaml
from pynput import keyboard

try:
    from PIL import ImageGrab
    _HAS_PIL = True
except Exception:
    _HAS_PIL = False

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import caseload_csv, caseload_filter, email_template
from src.browser import persistent_context
from src.config import (
    CASELOAD_CSV_PATH, CASELOAD_URL, NOTE_LOG_CSV, TEMPLATES_DIR,
    USER_CONFIG_DIR,
)
from src.version import __version__
from src.note_form import NoteData
from src.scenarios import (
    NOTES_YAML, BatchConfig, EmailConfig, ScenarioConfig,
    load_scenarios, run_scenario,
)
from src.student_lookup import (
    click_caseload_row,
    find_and_click_student,
    gather_caseload_matches,
    gather_fuzzy_caseload_matches,
    get_active_student_name,
    lookup_caseload_student,
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
    ) -> None:
        """Queue a scenario for the worker to fill notes against the
        active student. `prompt_vars` carries the user-typed values
        for any `prompts:` block in the scenario; they're substituted
        into note bodies (and email body / subject / to, handled on
        the main thread before queueing). `on_done(success)` is
        called from the worker thread when the run finishes."""
        self.q.put((
            "RUN", scenario, course_code_override, clipboard,
            custom_bodies or {}, prompt_vars or {}, on_done,
        ))

    def submit_find_student(self, query: str) -> None:
        self.q.put(("FIND", query))

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
        on_done: Callable[[bool], None],
        expected_name: str = "",
    ) -> None:
        """Fast batch click: type `query` into Salesforce's row filter,
        wait for the table to narrow, then click the single matching
        row. If `expected_name` is set and the filter returns more
        than one row, only clicks if that name matches one — otherwise
        aborts. ~1.5s per call vs ~25s for the full DOM scan."""
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
            _, scenario, override, clipboard, custom_bodies, prompt_vars, on_done = cmd
            success = False
            try:
                success = self._handle_run(
                    ctx, scenario, override, clipboard,
                    custom_bodies=custom_bodies,
                    prompt_vars=prompt_vars,
                )
            finally:
                if on_done is not None:
                    on_done(success)
        elif cmd[0] == "FIND":
            _, query = cmd
            self._handle_find(ctx, query)
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
                            tgt.wait_for_timeout(2000)
                        except Exception:
                            pass
            finally:
                on_done(success)
        elif cmd[0] == "GET_STUDENT_CONTEXT":
            _, on_done, name_hint = cmd
            info: Optional[dict] = None
            try:
                info = self._read_student_context(ctx, name_hint)
            finally:
                on_done(info)
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
            try:
                success = self._click_match_by_filter(
                    ctx, query, expected_name=expected_name,
                )
                if success:
                    tgt = self._active_page(ctx)
                    if tgt is not None:
                        try:
                            tgt.wait_for_timeout(2000)
                        except Exception:
                            pass
            finally:
                on_done(success)
        elif cmd[0] == "DOWNLOAD_CASELOAD_CSV":
            _, save_path, on_done = cmd
            success, message = False, ""
            try:
                success, message = self._download_caseload_csv(
                    ctx, save_path,
                )
            finally:
                on_done(success, message)

    def _try_match_or_navigate(self, target, query: str) -> bool:
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
            return True
        # Multiple matches at the same priority — ask user to pick.
        names = [m[2] for m in top]
        self.on_status(
            f"  [search] {len(names)} matches: {', '.join(names)}"
        )
        self.on_multiple_matches(query, names)
        return True

    def _handle_find(self, ctx, query: str) -> None:
        target = self._active_page(ctx)
        if target is None:
            self.on_status("No browser pages open.")
            return
        self.on_status(f"Searching Caseload for {query!r}...")

        # First pass: search whatever's currently in DOM.
        try:
            if self._try_match_or_navigate(target, query):
                return
        except Exception as e:
            self.on_status(f"Search failed: {e}")
            return

        # Miss — Caseload list isn't in DOM (likely because the user
        # navigated into a student record after the last search).
        # Reload the Caseload list and retry once.
        if not CASELOAD_URL:
            self.on_status(
                f"No match for {query!r}; CASELOAD_URL not set so we can't "
                "reload Caseload. Open it manually and try again."
            )
            return

        self.on_status("Caseload list not in DOM — navigating there to retry...")
        # Lightning sometimes raises "Navigation interrupted" when its own
        # JS triggers a redirect during our goto. The navigation still
        # ultimately succeeds, so we treat the exception as advisory.
        try:
            target.goto(CASELOAD_URL, wait_until="domcontentloaded")
        except Exception as e:
            self.on_status(f"  [debug] goto note: {e}")

        # Wait for the real Caseload list table — must have BOTH a
        # Course Code header AND a Name header. The Essential Actions
        # panels match Course Code only, so we'd find a stale empty
        # table if we used the looser wait.
        try:
            list_table = (
                target.locator("table")
                .filter(has=target.locator('th:has-text("Course Code")'))
                .filter(has=target.locator('th:has-text("Name")'))
            )
            list_table.first.wait_for(state="visible", timeout=20_000)
        except Exception as e:
            self.on_status(f"Caseload list table didn't load in time: {e}")
            return

        try:
            if self._try_match_or_navigate(target, query):
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
            target.wait_for_timeout(1500)
        except Exception as e:
            self.on_status(f"Filter step failed: {e}")
            return

        try:
            if not self._try_match_or_navigate(target, query):
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
                    target.wait_for_timeout(1500)
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
                target.wait_for_timeout(1500)
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
    ) -> bool:
        """Skip the slow full-table DOM scan: type `query` into
        Salesforce's row filter, wait, then click the (one) result.
        For batches with known-unique Student IDs this is ~10x faster
        than _list_matches + _click_match_by_name."""
        target, table = self._open_caseload_table(ctx)
        if table is None:
            return False
        self.on_status(f"Fast-find: filtering Caseload by {query!r}...")
        try:
            filter_input = target.locator(
                'input[placeholder="Search All Rows..."]'
            ).filter(visible=True).first
            if filter_input.count() == 0:
                self.on_status("No row filter input; can't fast-find.")
                return False
            # focus() instead of click() — when the user's Caseload
            # view has many columns the search input scrolls off the
            # viewport horizontally; click() refuses (requires viewport
            # visibility), focus() doesn't.
            filter_input.focus()
            filter_input.fill("")
            filter_input.fill(query)
            filter_input.press("Enter")
            target.wait_for_timeout(800)
        except Exception as e:
            self.on_status(f"Fast-find filter failed for {query!r}: {e}")
            return False

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
            return False

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
            return False

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
                return False
        elif len(candidates) > 1:
            names = ", ".join(c[1] for c in candidates)
            self.on_status(
                f"Fast-find {query!r}: {len(candidates)} ambiguous rows "
                f"({names}); skipping (no expected_name to disambiguate)."
            )
            return False
        else:
            chosen = candidates[0]

        row, cname, name_idx = chosen
        if click_caseload_row(row, cname, name_idx, on_status=self.on_status):
            self.on_status(f"Fast-find navigated to {cname!r}.")
            return True
        self.on_status(f"Fast-find: click on {cname!r} failed.")
        return False

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
            "full_name": name,
            "first_name": first,
            "last_name": last,
            "student_email": info.get("student_email", ""),
            "student_id": info.get("student_id", ""),
            "course_code": info.get("course_code", ""),
            "pm_name": info.get("pm_name", ""),
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
        """Return the most-recent responsive page in `ctx`, or None.
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
        for page in reversed(ctx.pages):
            try:
                if page.is_closed():
                    continue
                _ = page.url
                _ = page.locator("html").count()  # responsive probe
                return page
            except Exception:
                continue
        return None

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
        return None, None

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
                target.wait_for_timeout(800)
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
            Path(save_path).parent.mkdir(parents=True, exist_ok=True)
            download.save_as(str(save_path))
        except Exception as e:
            return False, f"download save failed: {e}"
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
                target.wait_for_timeout(1500)
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

    def _handle_run(
        self, ctx, scenario: ScenarioConfig, override: str,
        clipboard: str = "",
        custom_bodies: Optional[dict[int, str]] = None,
        prompt_vars: Optional[dict[str, str]] = None,
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


# Variables exposed in the in-app HTML editor's "Insert variable"
# toolbar. Display label → variable name (so users see the friendly
# name but the inserted `{{var}}` matches what the renderer accepts).
_TEMPLATE_INSERT_VARS_STUDENT = [
    ("First name", "first_name"),
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
import re as _re_html
_HTML_HIGHLIGHT_PATTERNS: dict[str, "_re_html.Pattern"] = {
    "comment": _re_html.compile(r"<!--[\s\S]*?-->"),
    "tag":     _re_html.compile(r"</?[a-zA-Z][^>]*?>", _re_html.DOTALL),
    "var":     _re_html.compile(r"\{\{\s*\w+\s*\}\}"),
    "value":   _re_html.compile(r'"[^"]*"'),
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
        "font_size": 11,
    }

    # ---- Toolbar — insert-variable rows + font/size selectors. ----
    toolbar = ctk.CTkFrame(dialog, fg_color="transparent")
    toolbar.pack(fill="x", padx=8, pady=(8, 0))

    def insert_var(var: str) -> None:
        text_box.insert("insert", f"{{{{{var}}}}}")
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
    # picked file into TEMPLATES_DIR (if not already there), drops a
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
        html, filename = prompt_add_image_dialog(dialog, TEMPLATES_DIR)
        if html:
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
        try:
            text_box.configure(font=ctk.CTkFont(
                family=current["font_family"],
                size=current["font_size"],
            ))
        except Exception:
            pass

    def on_size_change(value: str) -> None:
        try:
            current["font_size"] = max(8, min(40, int(value)))
        except (ValueError, TypeError):
            return
        apply_font()

    size_combo = ctk.CTkComboBox(
        size_row, values=["8", "9", "10", "11", "12", "14", "16", "18", "22"],
        width=70, command=on_size_change,
    )
    size_combo.set("11")
    size_combo.pack(side="left")
    ctk.CTkLabel(
        size_row,
        text="(editor view only — set the sent email's font in the "
             "scenario's email section)",
        font=ctk.CTkFont(size=10), text_color=("gray45", "gray65"),
    ).pack(side="left", padx=(10, 0))

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
            tgt.write_text(
                text_box.get("1.0", "end-1c"), encoding="utf-8",
            )
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
        filenames so the browser can load images from TEMPLATES_DIR
        (where the preview itself is written)."""
        import webbrowser
        import re as _re
        buffer = text_box.get("1.0", "end-1c")
        rendered = email_template.render_with_placeholders(buffer)

        def _fix_cid(m: _re.Match) -> str:
            stem = m.group(1)
            for f in sorted(TEMPLATES_DIR.glob(f"{stem}.*")):
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
        preview_path = TEMPLATES_DIR / "_preview.html"
        try:
            TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
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

    btn_row = ctk.CTkFrame(dialog, fg_color="transparent")
    btn_row.pack(fill="x", padx=8, pady=(0, 8))
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


def ask_yes_no_topmost(
    parent, title: str, message: str,
    yes_label: str = "Yes", no_label: str = "No",
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

    scroll = ctk.CTkScrollableFrame(dialog)
    scroll.pack(fill="both", expand=True, padx=12, pady=4)

    checked_vars: list[ctk.BooleanVar] = []

    def update_count_label() -> None:
        n = sum(1 for v in checked_vars if v.get())
        confirm_btn.configure(text=f"Confirm {n}")

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

    def __init__(self, parent, columns: list[str], on_delete: Callable):
        self.frame = ctk.CTkFrame(parent, fg_color="transparent")
        self.frame.grid_columnconfigure(2, weight=1)
        self.column_combo = ctk.CTkComboBox(
            self.frame,
            values=columns if columns else ["(refresh columns)"],
            width=220,
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
        ctk.CTkButton(
            self.frame, text="✕", width=28, height=28,
            **SECONDARY_BTN_KWARGS,
            command=lambda: on_delete(self),
        ).grid(row=0, column=3, padx=(4, 0), pady=2)
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

    def _on_op_change(self, op: str) -> None:
        """Sync the value-field suggestions and hint label to `op`.
        Called whenever the op dropdown selection changes (and once
        at init / once during `load` to set the initial state)."""
        date_ops = ("is before", "is after", "is on")
        numeric_ops = ("more than", "less than", "at least", "at most")
        text_ops = ("is", "is not", "contains", "does not contain")

        if op in date_ops:
            self.value_combo.configure(state="normal", values=[
                "today", "today-7d", "today-30d",
                "today+7d", "today+30d",
            ])
            self.hint_label.configure(
                text="Date — pick a relative shorthand from the dropdown, "
                     "or type 2026-05-21 / 5/21/2026.",
            )
        elif op == "is within":
            self.value_combo.configure(
                state="normal", values=list(caseload_filter.WITHIN_PRESETS),
            )
            self.hint_label.configure(
                text="Pick a preset: "
                     + ", ".join(caseload_filter.WITHIN_PRESETS) + ".",
            )
        elif op in numeric_ops:
            self.value_combo.configure(state="normal", values=[""])
            self.hint_label.configure(
                text="Number — e.g. 0, 1.5, 12. The column's cell must "
                     "also parse as a number.",
            )
        elif op in ("is empty", "is not empty"):
            self.value_combo.set("")
            self.value_combo.configure(state="disabled", values=[""])
            self.hint_label.configure(text="(no value needed for this op)")
        elif op in text_ops:
            self.value_combo.configure(state="normal", values=[""])
            self.hint_label.configure(
                text="Text — type the value, or comma-separate for OR "
                     "match (e.g. 'Pass, NoPass'). Case-insensitive.",
            )
        else:
            self.value_combo.configure(state="normal", values=[""])
            self.hint_label.configure(text="")

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
        # Interaction Format
        ctk.CTkLabel(content, text="Interaction Format").grid(
            row=row, column=0, sticky="w", padx=8, pady=(4, 0)
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
        self.body_text.bind("<FocusIn>", lambda _e: self._build_var_buttons())
        self._build_var_buttons()

        # Prompt-for-extra-text toggle. When on, firing the scenario
        # pops a dialog pre-filled with this body so the user can edit
        # / paste before it's submitted (same size cap applies).
        row += 1
        self.enter_additional_text_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            content, text="Enter additional text at fire time",
            variable=self.enter_additional_text_var,
        ).grid(row=row, column=0, sticky="w", padx=8, pady=(0, 4))

        # Append-clipboard toggle. When on, the clipboard at fire time
        # is read on the main thread and appended after the body
        # (capped at 200 lines / 25000 chars total — images replaced
        # with [IMAGE NOT INCLUDED] placeholder).
        row += 1
        self.append_clipboard_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            content, text="Append clipboard contents after body",
            variable=self.append_clipboard_var,
        ).grid(row=row, column=0, sticky="w", padx=8, pady=(0, 4))

        # Submit toggle. Unchecking leaves the form filled for manual
        # review — and the scenario's tab-close step is also skipped
        # whenever any note in the scenario opted out of auto-submit.
        row += 1
        self.submit_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            content, text="Submit and close automatically",
            variable=self.submit_var,
        ).grid(row=row, column=0, sticky="w", padx=8, pady=(0, 8))

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
        self.append_clipboard_var.set(note.append_clipboard)
        self.enter_additional_text_var.set(note.enter_additional_text)
        self._update_activity_state()

    def serialize(self) -> dict:
        return {
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


# ============================================================
# Scenario editor — one tab in the editor's tabview.
# ============================================================

class ScenarioEditor:
    def __init__(
        self, parent, scenario: ScenarioConfig,
        capture_handler=None,
        get_columns: Optional[Callable[[], list[str]]] = None,
        refresh_columns: Optional[Callable[[], list[str]]] = None,
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
        ctk.CTkCheckBox(
            self.frame,
            text="Use scenario variables (advanced — applies to email + notes below)",
            variable=self.use_vars_var,
            command=self._on_use_vars_toggled,
        ).grid(row=row, column=0, sticky="w", padx=8, pady=(8, 4))
        row += 1
        self._vars_section_row = row
        self._build_vars_section()
        # Visibility set by load() based on scenario.prompts.

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
        else:
            self._batch_section.grid_remove()
            self.find_first_checkbox.grid(
                row=self._find_first_row, column=0,
                sticky="w", padx=8, pady=(0, 8),
            )

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
            frame, text="Scenario variables",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).grid(row=0, column=0, sticky="w", padx=8, pady=(6, 0))
        ctk.CTkLabel(
            frame,
            text=(
                "Values you'll be asked for when this scenario fires. "
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

        # Body template (dropdown over TEMPLATES_DIR + Open button)
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

        # To override (variable substitution allowed)
        ctk.CTkLabel(frame, text="To override").grid(
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

        # Inline images (comma-separated filenames)
        ctk.CTkLabel(frame, text="Inline images").grid(
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
        ctk.CTkLabel(frame, text="Email font").grid(
            row=5, column=0, sticky="w", padx=8, pady=(4, 0),
        )
        font_row = ctk.CTkFrame(frame, fg_color="transparent")
        font_row.grid(row=5, column=1, sticky="ew", padx=8, pady=(4, 0))
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
        ).grid(row=6, column=0, columnspan=2, sticky="w", padx=8, pady=(6, 8))

    @staticmethod
    def _available_template_files() -> list[str]:
        """List the .html templates in the user's templates dir, in
        sorted order. Empty list if folder is missing or unreadable."""
        try:
            return sorted(p.name for p in TEMPLATES_DIR.glob("*.html"))
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

    def _selected_template_path(self) -> Optional[Path]:
        """Return the absolute path of the currently-selected body
        template, or None if the dropdown is empty / pointing at the
        '(none …)' placeholder."""
        name = self.email_body_combo.get().strip()
        if not name or "(none" in name:
            return None
        return TEMPLATES_DIR / name

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
        """Create a fresh `.html` template under TEMPLATES_DIR. Asks
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
        path = TEMPLATES_DIR / name
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
        path = TEMPLATES_DIR / name
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
                "A scenario needs at least one note. Use 'Delete "
                "scenario' in the editor's action row if you want to "
                "remove the whole scenario.",
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
        return out


# ============================================================
# Main app
# ============================================================

class App:
    def __init__(self) -> None:
        ctk.set_appearance_mode("System")
        ctk.set_default_color_theme("blue")

        self.scenarios = load_scenarios()

        # In-memory caseload cache populated from CASELOAD_CSV_PATH.
        # Set by _reload_caseload_cache() (called on startup and via
        # the Reload button). When None, batches fall back to the
        # DOM-scroll scrape — slower but always works.
        self._caseload_rows: Optional[list[dict]] = None
        self._caseload_csv_mtime = None
        self._reload_caseload_cache(silent=True)

        # Busy-state guard so the user can't fire a second action
        # while one is in flight (auto-refresh, manual refresh, a
        # scenario, or a batch). Toggled by _set_busy / _set_idle.
        self._is_busy = False
        self._busy_message = ""
        self._busy_spinner_index = 0

        self.root = ctk.CTk()
        self.root.title(f"Caseload Note Automation — v{__version__}")
        self.root.geometry("900x600")
        self.root.minsize(420, 520)
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_columnconfigure(1, weight=2)
        self.root.grid_rowconfigure(0, weight=1)

        self._editor_visible = True
        # Per-(course,scenario) tabs in the log area + flat list of all
        # entries for the persistent CSV. Initialized before building
        # the UI so the tabview helpers can use them.
        self.note_log_entries: list[NoteLogEntry] = []
        self.note_tabs: dict[str, dict] = {}  # tab_key -> {frame, list_frame}
        self._build_main_pane()
        self._build_editor_pane()

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

        # Once the worker has the browser open, auto-refresh the
        # caseload CSV in the background so the first batch fire is
        # instant. Failures log a hint but never block startup.
        self.root.after(500, self._poll_worker_then_auto_download)

    # ----- Main (left) pane -----

    def _build_main_pane(self) -> None:
        pane = ctk.CTkFrame(self.root)
        pane.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        pane.grid_columnconfigure(0, weight=1)
        self.main_pane = pane

        # Status
        self.status_var = ctk.StringVar(value="Launching browser...")
        ctk.CTkLabel(
            pane, textvariable=self.status_var,
            font=ctk.CTkFont(size=13), anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))

        # Find student — searches the in-DOM Caseload table.
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

        # Course code (override for auto-detect)
        cc_frame = ctk.CTkFrame(pane)
        cc_frame.grid(row=2, column=0, sticky="ew", padx=8, pady=4)
        cc_frame.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(cc_frame, text="Course code:").grid(row=0, column=0, padx=8, pady=8)
        self.course_var = ctk.StringVar()
        ctk.CTkEntry(
            cc_frame, textvariable=self.course_var,
            placeholder_text="(empty = auto-detect)", width=180,
        ).grid(row=0, column=1, sticky="w", padx=(0, 8), pady=8)

        # Scenario buttons
        self.button_frame = ctk.CTkFrame(pane)
        self.button_frame.grid(row=3, column=0, sticky="ew", padx=8, pady=4)
        self.scenario_buttons: dict[str, ctk.CTkButton] = {}
        self._rebuild_scenario_buttons()

        # Editor toggle row (also hosts the caseload-cache refresh).
        toggle_frame = ctk.CTkFrame(pane, fg_color="transparent")
        toggle_frame.grid(row=4, column=0, sticky="ew", padx=8, pady=(4, 0))
        self.editor_toggle_btn = ctk.CTkButton(
            toggle_frame, text="Hide editor", width=120, command=self._toggle_editor,
        )
        self.editor_toggle_btn.pack(side="left")
        self.caseload_refresh_btn = ctk.CTkButton(
            toggle_frame, text="↻ Caseload",
            width=120, command=self._on_caseload_refresh_clicked,
            **SECONDARY_BTN_KWARGS,
        )
        self.caseload_refresh_btn.pack(side="left", padx=(8, 0))
        ctk.CTkButton(
            toggle_frame, text="📁 Templates",
            width=110, command=self._on_open_templates_folder,
            **SECONDARY_BTN_KWARGS,
        ).pack(side="left", padx=(8, 0))
        # Discovery: capture Salesforce's note-submission network
        # traffic so we can later replay it via REST API instead of
        # driving the UI. One-click toggle; on stop, writes the
        # captured requests to a JSON file in the user config dir.
        self._capture_active = False
        self.capture_btn = ctk.CTkButton(
            toggle_frame, text="🔬 Capture",
            width=100, command=self._on_capture_toggle,
            **SECONDARY_BTN_KWARGS,
        )
        self.capture_btn.pack(side="left", padx=(8, 0))
        # Busy indicator — right-aligned spinner + text. Empty when
        # idle; high-contrast yellow background when active so it's
        # impossible to miss in the row of action buttons.
        self.busy_label = ctk.CTkLabel(
            toggle_frame, text="", anchor="e", justify="right",
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color="transparent",
            text_color=("#7a4f00", "#ffd166"),
            corner_radius=6,
        )
        self.busy_label.pack(side="right", padx=(8, 0), fill="x", expand=True)

        # Activity + per-note-type tabs.
        self.log_tabview = ctk.CTkTabview(pane)
        self.log_tabview.grid(row=5, column=0, sticky="nsew", padx=8, pady=(8, 0))
        activity_tab = self.log_tabview.add("Activity")
        activity_tab.grid_columnconfigure(0, weight=1)
        activity_tab.grid_rowconfigure(0, weight=1)
        self.log = ctk.CTkTextbox(activity_tab, wrap="word")
        self.log.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
        self.log.configure(state="disabled")
        pane.grid_rowconfigure(5, weight=1)

        # Bottom row
        bottom = ctk.CTkFrame(pane, fg_color="transparent")
        bottom.grid(row=6, column=0, sticky="ew", padx=8, pady=(0, 8))
        ctk.CTkButton(
            bottom, text="Hide to taskbar", width=120, command=self._hide,
        ).pack(side="left")
        ctk.CTkButton(
            bottom, text="Open log", width=90, command=self._open_log_file,
        ).pack(side="left", padx=(8, 0))
        ctk.CTkButton(
            bottom, text="Quit", width=80, command=self._on_close,
        ).pack(side="right")

    def _rebuild_scenario_buttons(self) -> None:
        for w in self.button_frame.winfo_children():
            w.destroy()
        self.scenario_buttons.clear()
        for i, (name, sc) in enumerate(self.scenarios.items()):
            label = f"{name}" + (f"  ({sc.hotkey})" if sc.hotkey else "")
            btn = ctk.CTkButton(
                self.button_frame, text=label, command=lambda s=sc: self._fire(s),
                width=160, height=36,
            )
            btn.grid(row=i // 2, column=i % 2, padx=6, pady=6, sticky="ew")
            self.button_frame.grid_columnconfigure(i % 2, weight=1)
            self.scenario_buttons[name] = btn

    # ----- Editor (right) pane -----

    def _build_editor_pane(self) -> None:
        pane = ctk.CTkFrame(self.root)
        pane.grid(row=0, column=1, sticky="nsew", padx=(0, 8), pady=8)
        pane.grid_columnconfigure(0, weight=1)
        pane.grid_rowconfigure(0, weight=1)
        self.editor_pane = pane

        self.tabview = ctk.CTkTabview(pane)
        self.tabview.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
        self.scenario_editors: dict[str, ScenarioEditor] = {}
        self._rebuild_editor_tabs()

        # Save row
        save_frame = ctk.CTkFrame(pane, fg_color="transparent")
        save_frame.grid(row=1, column=0, sticky="ew", padx=4, pady=(0, 4))
        ctk.CTkButton(
            save_frame, text="+ New scenario",
            command=self._new_scenario, width=140, height=34,
        ).pack(side="left", padx=4, pady=2)
        ctk.CTkButton(
            save_frame, text="Delete scenario",
            command=self._delete_scenario, width=140, height=34,
            **SECONDARY_BTN_KWARGS,
        ).pack(side="left", padx=4, pady=2)
        ctk.CTkButton(
            save_frame, text="Save changes",
            command=self._save_yaml, width=140, height=34,
        ).pack(side="right", padx=4, pady=2)
        ctk.CTkButton(
            save_frame, text="Revert",
            command=self._revert_editor, width=100, height=34,
            **SECONDARY_BTN_KWARGS,
        ).pack(side="right", padx=4, pady=2)

    def _rebuild_editor_tabs(self) -> None:
        # CTkTabview doesn't have a clean "remove all" — recreate it.
        for name in list(self.tabview._tab_dict.keys()):
            self.tabview.delete(name)
        self.scenario_editors.clear()
        for name, sc in self.scenarios.items():
            tab = self.tabview.add(name)
            tab.grid_columnconfigure(0, weight=1)
            tab.grid_rowconfigure(0, weight=1)
            editor = ScenarioEditor(
                tab, sc,
                capture_handler=self._capture_hotkey,
                get_columns=self._get_caseload_columns,
                refresh_columns=self._refresh_caseload_columns_for_editor,
            )
            editor.frame.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
            self.scenario_editors[name] = editor

    def _toggle_editor(self) -> None:
        if self._editor_visible:
            self.editor_pane.grid_remove()
            self.editor_toggle_btn.configure(text="Show editor")
            self.root.geometry("440x600")
            self._editor_visible = False
        else:
            self.editor_pane.grid()
            self.editor_toggle_btn.configure(text="Hide editor")
            self.root.geometry("900x600")
            self._editor_visible = True

    def _new_scenario(self) -> None:
        """Prompt for a name, create a scenario with sensible defaults,
        persist, rebuild tabs/buttons, and switch to the new tab."""
        dialog = ctk.CTkInputDialog(text="Name for new scenario:", title="New scenario")
        raw = dialog.get_input()
        if raw is None:
            return
        name = raw.strip()
        if not name:
            self._append_log("New scenario: empty name; nothing added.")
            return
        if name in self.scenarios:
            self._append_log(f"Scenario {name!r} already exists.")
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
            self._append_log(f"Could not save new scenario: {e}")
            return
        try:
            self.tabview.set(name)
        except Exception:
            pass

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
        and rebuild tabs/buttons. The deletion is *draft* — notes.yaml
        isn't touched until the user clicks 'Save changes'. 'Revert'
        brings the scenario back."""
        try:
            name = self.tabview.get()
        except Exception:
            self._append_log("No scenario tab selected.")
            return
        if not name or name not in self.scenarios:
            self._append_log("No scenario tab selected.")
            return
        from tkinter import messagebox
        if not messagebox.askyesno(
            "Delete scenario",
            f"Delete scenario {name!r}?\n\n"
            "This only updates the editor — click 'Save changes' to "
            "persist, or 'Revert' to undo.",
        ):
            return
        self.scenarios.pop(name, None)
        self._rebuild_editor_tabs()
        self._rebuild_scenario_buttons()
        self._append_log(
            f"Scenario {name!r} marked for deletion. "
            "Click 'Save changes' to persist or 'Revert' to undo."
        )

    def _revert_editor(self) -> None:
        # Reload from disk and rebuild tabs/buttons so structural drafts
        # (added or deleted scenarios) are undone, not just field edits.
        try:
            self.scenarios = load_scenarios()
        except Exception as e:
            self._append_log(f"Revert failed: {e}")
            return
        self._rebuild_editor_tabs()
        self._rebuild_scenario_buttons()
        self._append_log("Editor reverted to saved YAML.")

    def _save_yaml(self) -> None:
        new_doc: dict = {"scenarios": {}}
        seen: set[str] = set()
        for old_name, ed in list(self.scenario_editors.items()):
            new_name = ed.current_name or old_name
            if not new_name:
                self._append_log(f"!! Empty scenario name (was {old_name!r}); aborting.")
                return
            if new_name in seen:
                self._append_log(
                    f"!! Duplicate scenario name {new_name!r}; aborting save. "
                    "Pick unique names then try again."
                )
                return
            seen.add(new_name)
            try:
                new_doc["scenarios"][new_name] = ed.serialize()
            except Exception as e:
                self._append_log(f"Could not serialize {new_name!r}: {e}")
                return

        try:
            NOTES_YAML.write_text(
                yaml.safe_dump(new_doc, sort_keys=False, allow_unicode=True),
                encoding="utf-8",
            )
        except Exception as e:
            self._append_log(f"Save failed: {e}")
            return
        try:
            self.scenarios = load_scenarios()
        except Exception as e:
            self._append_log(f"Saved but reload failed: {e}")
            return

        # Names may have changed — rebuild tabs and buttons so the new
        # names show up everywhere.
        self._rebuild_editor_tabs()
        self._rebuild_scenario_buttons()
        self._restart_hotkeys()
        self._append_log("Saved notes.yaml; tabs, buttons, and hotkeys refreshed.")

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
        query = self.search_var.get().strip()
        if not query:
            return
        if not self.worker.ready_event.is_set():
            self._append_log("Browser not ready yet — wait and try again.")
            return
        self._append_log(f"--- Searching {query!r} ---")
        self.worker.submit_find_student(query)

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
                    f"Prompt {p.var!r} cancelled; scenario not fired."
                )
                return None
            prompt_vars[p.var] = value
        return prompt_vars

    def _fire_per_student(self, scenario: ScenarioConfig, override: str) -> None:
        """Per-student (non-batch) scenario fire — wraps the original
        in-line `_fire` body so we can sandwich it between _set_busy
        and _set_idle."""

        # Step 1: find + pick (if enabled). Combined dialog lets the
        # user retype if the first query was wrong or surfaces too
        # many candidates. Worker handles search; fuzzy fallback kicks
        # in if there are no exact matches.
        chosen_name = ""
        if scenario.find_first:
            chosen = prompt_find_and_pick(self.root, self._list_matches_blocking)
            if not chosen:
                self._append_log("Find cancelled; scenario not fired.")
                return
            if not self._click_match_blocking(chosen):
                self._append_log(
                    f"Could not navigate to {chosen!r}; scenario not fired."
                )
                return
            chosen_name = chosen

        # Step 2: prompts (scenario-level, feed {{var}} into emails
        # and note bodies). Collect BEFORE per-note custom edits so
        # `{{var}}` placeholders inside a custom-edited body get
        # substituted too.
        prompt_vars = self._collect_prompt_vars(scenario)
        if prompt_vars is None:
            return  # user cancelled a prompt

        # Step 3: body edits. The user is committed to a student now,
        # so the dialogs are filled with the right context in mind.
        custom_bodies: dict[int, str] = {}
        for i, n in enumerate(scenario.notes):
            if not n.enter_additional_text:
                continue
            label = f"Note {i + 1}"
            edited = prompt_additional_text(self.root, label, n.body)
            if edited is None:
                self._append_log(f"{label} edit cancelled; scenario not fired.")
                return
            custom_bodies[i] = edited

        # Step 4: clipboard (main-thread read; Tk + PIL aren't thread-safe).
        clipboard = ""
        if any(n.append_clipboard for n in scenario.notes):
            clipboard = self._read_clipboard_content()

        # Step 5: email (if scenario has one). Opens an Outlook draft for
        # FERPA review; user reviews + sends from Outlook, then confirms
        # before the note fires. Prompt vars merge into the student
        # context so {{summary}}-style placeholders in the email body
        # / subject / to-field resolve correctly.
        if scenario.email is not None:
            student_ctx = self._get_student_context_blocking(name_hint=chosen_name)
            if student_ctx is None:
                self._append_log(
                    "Couldn't read student context for email; scenario not fired."
                )
                return
            student_ctx = {**student_ctx, **prompt_vars}
            if not self._send_scenario_email(scenario.email, student_ctx):
                self._append_log("Email step aborted; note not filed.")
                return

        self.worker.submit_scenario(
            scenario, override, clipboard,
            custom_bodies=custom_bodies,
            prompt_vars=prompt_vars,
        )

    def _fire_batch(self, scenario: ScenarioConfig, override: str) -> None:
        """Drive a batch scenario end-to-end: load caseload, filter,
        review/confirm, then loop email→note per selected student.
        The activity log is the progress display; cancellation is via
        any modal Cancel button (which aborts the batch from that
        point on)."""
        from tkinter import messagebox

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
            {**f, "column": caseload_csv.resolve_column(
                f.get("column", ""), csv_headers,
            )}
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
                f"This scenario filters on column(s) that aren't in your "
                f"current Caseload export:\n\n  • " +
                "\n  • ".join(missing) +
                "\n\n"
                "Add those columns to your Caseload list view in "
                "Salesforce, then click ↻ Caseload (or ↻ Refresh "
                "columns in the editor) to refresh the cache. Then "
                "try again.",
            )
            return

        matched = caseload_filter.apply_filters(filters, rows)
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

        # Step 4: review-and-confirm dialog (unless preview is off).
        if scenario.batch.preview:
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

        # Step 5: one-time per-batch prompts — both the new
        # `prompts:` schema (which feeds {{var}} into bodies) and
        # the legacy per-note `enter_additional_text` checkbox.
        # Each input is asked ONCE before the loop and the same
        # value is applied to every student in the batch.
        prompt_vars = self._collect_prompt_vars(scenario)
        if prompt_vars is None:
            return  # user cancelled a prompt
        custom_bodies: dict[int, str] = {}
        for i, n in enumerate(scenario.notes):
            if not n.enter_additional_text:
                continue
            label = f"Note {i + 1} (applies to all {len(confirmed)} students)"
            edited = prompt_additional_text(self.root, label, n.body)
            if edited is None:
                self._append_log(f"{label}: cancelled; batch not started.")
                return
            custom_bodies[i] = edited

        # Clipboard is read once at the start of the batch — Tk + PIL
        # aren't safe to call from the worker thread.
        clipboard = ""
        if any(n.append_clipboard for n in scenario.notes):
            clipboard = self._read_clipboard_content()

        # Step 6: template preview (only if an email is configured).
        # One placeholder-rendered draft opens in Outlook; the user
        # reviews + clicks Yes/No. Yes proceeds to the loop with
        # auto-send for everyone; No aborts the whole batch.
        total = len(confirmed)
        has_email = scenario.email is not None
        if has_email:
            if not self._show_template_preview(scenario.email, total):
                self._append_log("Batch aborted at template preview.")
                return

        # Step 7: loop. For each student: fast-find → auto-send
        # email (if configured) → file note. Everyone is treated
        # the same now that template review happened upfront.
        processed = 0
        skipped: list[tuple[str, str]] = []
        for idx, row in enumerate(confirmed, start=1):
            student_name = row.get("Name", "")
            student_id = row.get("Student ID", "")
            self._append_log(
                f"--- batch {idx}/{total}: {student_name!r} ---"
            )

            # 7a. Fast-find: row filter on Student ID, then click.
            query = student_id or student_name
            if not self._click_match_by_filter_blocking(
                query, expected_name=student_name,
            ):
                self._append_log(
                    f"Skipping {student_name!r}: fast-find failed."
                )
                skipped.append((student_name, "find/click failed"))
                continue

            # 7b. Auto-send email (if configured). Failure skips the
            # note for this student but doesn't halt the batch.
            # Prompt vars merge into student_ctx so {{summary}}-
            # style placeholders in the email body / subject / to
            # resolve against the batch-wide prompt input.
            if has_email:
                ctx_info = self._get_student_context_blocking(
                    name_hint=student_name,
                )
                if ctx_info is None:
                    self._append_log(
                        f"Skipping {student_name!r}: couldn't read context."
                    )
                    skipped.append((student_name, "no context"))
                    continue
                ctx_info = {**ctx_info, **prompt_vars}
                if not self._send_scenario_email(
                    scenario.email, ctx_info, auto_send=True,
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

        self._append_log(
            f"Batch {scenario.name!r} complete: "
            f"{processed}/{total} processed, {len(skipped)} skipped."
        )
        if skipped:
            for name, reason in skipped:
                self._append_log(f"  skipped: {name!r} ({reason})")

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
    ) -> bool:
        """Batch fast path: type the unique value into Caseload's row
        filter and click the result. Returns True on success."""
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

        self.worker.submit_click_match_by_filter(
            query, on_done, expected_name=expected_name,
        )
        self.root.wait_variable(done_var)
        return holder["success"]

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

        template_path = TEMPLATES_DIR / email_cfg.body_html_file
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

        inline_images = {
            Path(fname).stem: TEMPLATES_DIR / fname
            for fname in email_cfg.inline_images
        }

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

    def _send_scenario_email(
        self,
        email_cfg: EmailConfig,
        student_ctx: dict,
        *,
        auto_send: bool = False,
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
        student_ctx = {
            **student_ctx,
            "user_name": user_info.get("name", ""),
            "user_email": user_info.get("email", ""),
        }

        template_path = TEMPLATES_DIR / email_cfg.body_html_file
        if not template_path.exists():
            self._append_log(
                f"Email template not found: {template_path}. Scenario aborted."
            )
            messagebox.showerror(
                "Email template missing",
                f"Couldn't find template:\n{template_path}\n\n"
                "Check the scenario's body_html_file and the templates folder.",
            )
            return False

        try:
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

        # CID auto-derived from filename stem (signature.png → 'signature').
        inline_images = {
            Path(fname).stem: TEMPLATES_DIR / fname
            for fname in email_cfg.inline_images
        }

        # To: optional override (for test-mode addresses or any custom
        # routing), falling back to the student's email from caseload.
        if email_cfg.to:
            to = email_template.render_plain(email_cfg.to, student_ctx).strip()
        else:
            to = student_ctx.get("student_email", "")
        cc = student_ctx.get("pm_email", "") if email_cfg.cc_pm else ""
        if not to:
            if not messagebox.askyesno(
                "No student email",
                f"Couldn't find an email address for "
                f"{student_ctx.get('full_name') or 'this student'!r}.\n\n"
                "Proceed with the note only?",
            ):
                return False
            return True  # skip the email, but file the note

        full_name = student_ctx.get("full_name") or to

        if auto_send:
            self._append_log(f"Auto-sending email to {full_name}...")
            try:
                outlook_email.compose_email(
                    to=to, cc=cc, subject=subject,
                    html_body=body_html, inline_images=inline_images,
                    auto_send=True,
                    signature_name=email_cfg.signature_file,
                )
            except Exception as e:
                self._append_log(f"Auto-send failed for {full_name}: {e}")
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
        self._set_busy("Auto-refreshing caseload CSV…")
        self._append_log("Auto-refreshing caseload CSV...")

        def on_done(success: bool, message: str) -> None:
            def set_main() -> None:
                if success:
                    self._append_log(f"Caseload CSV: {message}")
                    self._reload_caseload_cache(silent=False)
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
                "scenario or click Submit in Salesforce yourself). "
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
            os.startfile(str(TEMPLATES_DIR))
        except AttributeError:
            # os.startfile is Windows-only — fall back for future
            # Mac/Linux support.
            import subprocess, sys
            opener = {"darwin": "open"}.get(sys.platform, "xdg-open")
            try:
                subprocess.Popen([opener, str(TEMPLATES_DIR)])
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
        self._set_busy("Refreshing caseload CSV…")
        self._append_log("Refreshing caseload CSV (manual)...")
        try:
            success, message = self._download_caseload_csv_blocking()
            if success:
                self._append_log(f"Caseload CSV: {message}")
            else:
                self._append_log(f"Caseload CSV refresh failed: {message}")
        finally:
            self._set_idle()

    # ----- Busy-state guard -----

    # Braille-pattern animation frames — looks like a smooth circular
    # spinner in any monospaced font and renders cleanly in CTkLabel.
    _SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

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
        ]

    def _refresh_caseload_columns_for_editor(self) -> list[str]:
        """↻ Refresh columns button in the filter editor: forces a
        fresh CSV download, then returns the new column list. Routes
        through _on_caseload_refresh_clicked so the busy spinner +
        button-disable behavior is identical to clicking the
        toolbar ↻ Caseload button."""
        self._on_caseload_refresh_clicked()
        return self._get_caseload_columns()

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
        if not silent:
            age = caseload_csv.csv_age_human(CASELOAD_CSV_PATH)
            self._append_log(
                f"Caseload cache: {len(rows)} rows from "
                f"{CASELOAD_CSV_PATH.name} ({age})"
            )
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
        try:
            if self.hotkey_listener is not None:
                self.hotkey_listener.stop()
        except Exception:
            pass
        self.worker.shutdown()
        self.root.destroy()

    # ----- Status / log -----

    def _post_status(self, msg: str) -> None:
        self.root.after(0, lambda: self._update_status_and_log(msg))

    def _update_status_and_log(self, msg: str) -> None:
        self.status_var.set(msg)
        self._append_log(msg)

    def _append_log(self, msg: str) -> None:
        # Defensive: if called before _build_main_pane has created
        # the widget (e.g. very early startup), fall back to stderr
        # so we don't crash the app with AttributeError.
        if not hasattr(self, "log") or self.log is None:
            print(msg, file=sys.stderr)
            return
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

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


def main() -> None:
    App().run()


if __name__ == "__main__":
    main()
