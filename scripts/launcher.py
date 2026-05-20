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
import queue
import sys
import threading
from pathlib import Path
from typing import Callable, Optional

import customtkinter as ctk
import yaml
from pynput import keyboard

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.browser import persistent_context
from src.config import CASELOAD_URL
from src.note_form import NoteData
from src.scenarios import NOTES_YAML, ScenarioConfig, load_scenarios, run_scenario
from src.student_lookup import detect_course_code, get_active_student_name

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

    def __init__(self, on_status: Callable[[str], None]):
        self.q: queue.Queue = queue.Queue()
        self.on_status = on_status
        self.ready_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def submit_scenario(self, scenario: ScenarioConfig, course_code_override: str) -> None:
        self.q.put(("RUN", scenario, course_code_override))

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
                        _, scenario, override = cmd
                        self._handle_run(ctx, scenario, override)
        except Exception as e:
            self.on_status(f"Browser worker crashed: {e}")

    def _handle_run(self, ctx, scenario: ScenarioConfig, override: str) -> None:
        target = ctx.pages[-1] if ctx.pages else None
        if target is None:
            self.on_status("No browser pages open.")
            return
        if override:
            course_code = override
            self.on_status(f"Using course code (manual): {course_code}")
        else:
            student = get_active_student_name(target)
            if not student:
                self.on_status("No visible note panel. Open one and try again.")
                return
            self.on_status(f"Active student: {student}")
            detected = detect_course_code(target, student)
            if not detected:
                self.on_status(
                    f"Could not auto-detect for {student}. Type a code in the field."
                )
                return
            course_code = detected
            self.on_status(f"Auto-detected course code: {course_code}")
        self.on_status(f"Running {scenario.name!r}...")
        try:
            run_scenario(target, scenario, course_code)
            self.on_status(f"Done: {scenario.name!r} (course {course_code!r}).")
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
        }


# ============================================================
# Scenario editor — one tab in the editor's tabview.
# ============================================================

class ScenarioEditor:
    def __init__(self, parent, scenario: ScenarioConfig):
        self.scenario_name = scenario.name
        self.close_tab_after = scenario.close_tab_after
        self.frame = ctk.CTkScrollableFrame(parent)
        self.frame.grid_columnconfigure(0, weight=1)

        # Hotkey field at top.
        row = 0
        ctk.CTkLabel(
            self.frame, text="Hotkey",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).grid(row=row, column=0, sticky="w", padx=8, pady=(8, 0))
        row += 1
        self.hotkey_entry = ctk.CTkEntry(
            self.frame, placeholder_text="e.g. F3 or Ctrl+Shift+1", width=200,
        )
        self.hotkey_entry.grid(row=row, column=0, sticky="w", padx=8, pady=(0, 8))

        # One NoteEditor per note in the scenario.
        self.note_editors: list[NoteEditor] = []
        for i, note in enumerate(scenario.notes):
            ne = NoteEditor(self.frame, i)
            ne.frame.grid(row=row + 1 + i, column=0, sticky="ew", padx=4, pady=4)
            self.note_editors.append(ne)

        self.load(scenario)

    def load(self, scenario: ScenarioConfig) -> None:
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
        self._build_main_pane()
        self._build_editor_pane()

        self.worker = BrowserWorker(on_status=self._post_status)
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
        pane.grid_rowconfigure(4, weight=1)
        self.main_pane = pane

        # Status
        self.status_var = ctk.StringVar(value="Launching browser...")
        ctk.CTkLabel(
            pane, textvariable=self.status_var,
            font=ctk.CTkFont(size=13), anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))

        # Course code
        cc_frame = ctk.CTkFrame(pane)
        cc_frame.grid(row=1, column=0, sticky="ew", padx=8, pady=4)
        cc_frame.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(cc_frame, text="Course code:").grid(row=0, column=0, padx=8, pady=8)
        self.course_var = ctk.StringVar()
        ctk.CTkEntry(
            cc_frame, textvariable=self.course_var,
            placeholder_text="(empty = auto-detect)", width=180,
        ).grid(row=0, column=1, sticky="w", padx=(0, 8), pady=8)

        # Scenario buttons
        self.button_frame = ctk.CTkFrame(pane)
        self.button_frame.grid(row=2, column=0, sticky="ew", padx=8, pady=4)
        self.scenario_buttons: dict[str, ctk.CTkButton] = {}
        self._rebuild_scenario_buttons()

        # Editor toggle row
        toggle_frame = ctk.CTkFrame(pane, fg_color="transparent")
        toggle_frame.grid(row=3, column=0, sticky="ew", padx=8, pady=(4, 0))
        self.editor_toggle_btn = ctk.CTkButton(
            toggle_frame, text="Hide editor", width=120, command=self._toggle_editor,
        )
        self.editor_toggle_btn.pack(side="left")

        # Activity log
        log_label = ctk.CTkLabel(pane, text="Activity log", anchor="w")
        log_label.grid(row=4, column=0, sticky="ew", padx=8, pady=(8, 0))
        self.log = ctk.CTkTextbox(pane, height=120, wrap="word")
        self.log.grid(row=5, column=0, sticky="nsew", padx=8, pady=(0, 8))
        self.log.configure(state="disabled")
        pane.grid_rowconfigure(5, weight=1)

        # Bottom row
        bottom = ctk.CTkFrame(pane, fg_color="transparent")
        bottom.grid(row=6, column=0, sticky="ew", padx=8, pady=(0, 8))
        ctk.CTkButton(
            bottom, text="Hide to taskbar", width=120, command=self._hide,
        ).pack(side="left")
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
            editor = ScenarioEditor(tab, sc)
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
        new_doc = {"scenarios": {}}
        for name, ed in self.scenario_editors.items():
            try:
                new_doc["scenarios"][name] = ed.serialize()
            except Exception as e:
                self._append_log(f"Could not serialize {name!r}: {e}")
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
        self._rebuild_scenario_buttons()
        self._restart_hotkeys()
        self._append_log("Saved notes.yaml and re-registered hotkeys.")

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

    def _fire(self, scenario: ScenarioConfig) -> None:
        if not self.worker.ready_event.is_set():
            self._append_log("Browser not ready yet — wait and try again.")
            return
        override = self.course_var.get().strip()
        self._append_log(f"--- Firing {scenario.name!r} ---")
        self.worker.submit_scenario(scenario, override)

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

    # ----- Entry -----

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    App().run()


if __name__ == "__main__":
    main()
