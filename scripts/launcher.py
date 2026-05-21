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

from src.browser import persistent_context
from src.config import CASELOAD_URL, NOTE_LOG_CSV
from src.note_form import NoteData
from src.scenarios import NOTES_YAML, ScenarioConfig, load_scenarios, run_scenario
from src.student_lookup import (
    click_caseload_row,
    find_and_click_student,
    gather_caseload_matches,
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
        self.ready_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def submit_scenario(
        self,
        scenario: ScenarioConfig,
        course_code_override: str,
        clipboard: str = "",
    ) -> None:
        self.q.put(("RUN", scenario, course_code_override, clipboard))

    def submit_find_student(self, query: str) -> None:
        self.q.put(("FIND", query))

    def shutdown(self) -> None:
        self.q.put(self.SHUTDOWN)

    def _run(self) -> None:
        try:
            with persistent_context() as ctx:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                if CASELOAD_URL:
                    page.goto(CASELOAD_URL)
                self.on_status("Browser ready.")
                self.ready_event.set()
                while True:
                    cmd = self.q.get()
                    if cmd is self.SHUTDOWN:
                        return
                    if cmd[0] == "RUN":
                        _, scenario, override, clipboard = cmd
                        self._handle_run(ctx, scenario, override, clipboard)
                    elif cmd[0] == "FIND":
                        _, query = cmd
                        self._handle_find(ctx, query)
        except Exception as e:
            self.on_status(f"Browser worker crashed: {e}")

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
        target = ctx.pages[-1] if ctx.pages else None
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
            filter_input.click()
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

    def _handle_run(self, ctx, scenario: ScenarioConfig, override: str, clipboard: str = "") -> None:
        target = ctx.pages[-1] if ctx.pages else None
        if target is None:
            self.on_status("No browser pages open.")
            return
        # Always try to capture student name — used for auto-detect and
        # for the session log entry on success.
        student = get_active_student_name(target)
        # Look up the Caseload row once: gets course code, student ID,
        # and email in a single pass.
        info = lookup_caseload_student(target, student) if student else {}
        if override:
            course_code = override
            self.on_status(f"Using course code (manual): {course_code}")
            if student:
                self.on_status(f"Active student: {student}")
        else:
            if not student:
                self.on_status("No visible note panel. Open one and try again.")
                return
            self.on_status(f"Active student: {student}")
            detected = info.get("course_code", "")
            if not detected:
                self.on_status(
                    f"Could not auto-detect for {student}. Type a code in the field."
                )
                return
            course_code = detected
            self.on_status(f"Auto-detected course code: {course_code}")
        self.on_status(f"Running {scenario.name!r}...")
        try:
            all_submitted = run_scenario(
                target, scenario, course_code,
                clipboard=clipboard, on_status=self.on_status,
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
        except RuntimeError as e:
            self.on_status(f"Failed: {e}")


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


# ============================================================
# Note editor — one note section inside a scenario tab.
# ============================================================

class NoteEditor:
    """Widgets for editing a single note. Mirrors the Caseload form:
    Interaction Format, Interaction Type, Academic Activities, Body.
    Collapsible via the ▼/▶ header button."""

    def __init__(self, parent, index: int):
        self.index = index
        self._collapsed = False
        self.frame = ctk.CTkFrame(parent)
        self.frame.grid_columnconfigure(0, weight=1)

        # Header button — clicking collapses/expands the content frame.
        self.toggle_btn = ctk.CTkButton(
            self.frame, text=self._header_text(),
            command=self._toggle_collapse, anchor="w", height=28,
            fg_color="transparent", text_color=("gray10", "gray90"),
            hover_color=("gray85", "gray25"),
            font=ctk.CTkFont(size=13, weight="bold"),
        )
        self.toggle_btn.grid(row=0, column=0, sticky="ew", padx=4, pady=(4, 0))

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
        row += 1
        self.body_text = ctk.CTkTextbox(content, height=80, wrap="word")
        self.body_text.grid(row=row, column=0, sticky="ew", padx=8, pady=(0, 4))

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
        }


# ============================================================
# Scenario editor — one tab in the editor's tabview.
# ============================================================

class ScenarioEditor:
    def __init__(self, parent, scenario: ScenarioConfig, capture_handler=None):
        self.scenario_name = scenario.name
        self.close_tab_after = scenario.close_tab_after
        self.capture_handler = capture_handler  # callable(on_done)
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

        # One NoteEditor per note in the scenario.
        self.note_editors: list[NoteEditor] = []
        for i, note in enumerate(scenario.notes):
            ne = NoteEditor(self.frame, i)
            ne.frame.grid(row=row + 1 + i, column=0, sticky="ew", padx=4, pady=4)
            self.note_editors.append(ne)

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

    def load(self, scenario: ScenarioConfig) -> None:
        self.name_entry.delete(0, "end")
        self.name_entry.insert(0, scenario.name)
        self.hotkey_entry.delete(0, "end")
        self.hotkey_entry.insert(0, scenario.hotkey)
        for ne, note in zip(self.note_editors, scenario.notes):
            ne.load(note)

    def serialize(self) -> dict:
        return {
            "hotkey": self.hotkey_entry.get().strip(),
            "close_tab_after": self.close_tab_after,
            "notes": [ne.serialize() for ne in self.note_editors],
        }


# ============================================================
# Main app
# ============================================================

class App:
    def __init__(self) -> None:
        ctk.set_appearance_mode("System")
        ctk.set_default_color_theme("blue")

        self.scenarios = load_scenarios()

        self.root = ctk.CTk()
        self.root.title("Caseload Note Automation")
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

        # Editor toggle row
        toggle_frame = ctk.CTkFrame(pane, fg_color="transparent")
        toggle_frame.grid(row=4, column=0, sticky="ew", padx=8, pady=(4, 0))
        self.editor_toggle_btn = ctk.CTkButton(
            toggle_frame, text="Hide editor", width=120, command=self._toggle_editor,
        )
        self.editor_toggle_btn.pack(side="left")

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
            save_frame, text="Save changes",
            command=self._save_yaml, width=140, height=34,
        ).pack(side="right", padx=4, pady=2)
        ctk.CTkButton(
            save_frame, text="Revert",
            command=self._revert_editor, width=100, height=34,
            fg_color="transparent", border_width=1,
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
            editor = ScenarioEditor(tab, sc, capture_handler=self._capture_hotkey)
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

    def _revert_editor(self) -> None:
        # Reload from disk; throw away unsaved widget state.
        try:
            self.scenarios = load_scenarios()
        except Exception as e:
            self._append_log(f"Revert failed: {e}")
            return
        for name, ed in self.scenario_editors.items():
            if name in self.scenarios:
                ed.load(self.scenarios[name])
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
        if not self.worker.ready_event.is_set():
            self._append_log("Browser not ready yet — wait and try again.")
            return
        override = self.course_var.get().strip()
        self._append_log(f"--- Firing {scenario.name!r} ---")

        # Read clipboard on the main thread (Tk + PIL aren't thread-safe)
        # only if at least one note in this scenario asked for it.
        clipboard = ""
        if any(n.append_clipboard for n in scenario.notes):
            clipboard = self._read_clipboard_content()

        self.worker.submit_scenario(scenario, override, clipboard)

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
            fg_color="transparent", border_width=1, width=140,
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
            fg_color="transparent", border_width=1, width=90,
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
