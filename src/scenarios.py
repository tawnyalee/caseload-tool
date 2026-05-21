"""Load scenario definitions from notes.yaml and run them against an
open note panel. Each scenario is one or more NoteData entries filed in
sequence, optionally followed by closing the Salesforce workspace tab.
"""
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml
from playwright.sync_api import Page

from typing import Callable, Optional

from src.config import NOTES_YAML
from src.note_form import NoteData, close_workspace_tab, fill_note

# Note-body limits when clipboard is appended (total, including body).
MAX_BODY_LINES = 200
MAX_BODY_CHARS = 25_000
TRIM_MARKER = "\n[trimmed]"


def combine_with_clipboard(body: str, clipboard: str) -> tuple[str, bool]:
    """Return (combined, was_trimmed). Caps the result at MAX_BODY_LINES
    and MAX_BODY_CHARS so a giant paste can't crash the rich-text editor
    or get rejected by Salesforce."""
    combined = body + ("\n" + clipboard if clipboard else "")
    trimmed = False
    lines = combined.splitlines()
    if len(lines) > MAX_BODY_LINES:
        combined = "\n".join(lines[:MAX_BODY_LINES])
        trimmed = True
    if len(combined) > MAX_BODY_CHARS:
        room = MAX_BODY_CHARS - len(TRIM_MARKER)
        combined = combined[:room]
        trimmed = True
    if trimmed:
        combined += TRIM_MARKER
    return combined, trimmed


@dataclass
class ScenarioConfig:
    name: str
    hotkey: str
    close_tab_after: bool
    notes: list[NoteData] = field(default_factory=list)


def _note_from_dict(d: dict) -> NoteData:
    return NoteData(
        interaction_format=d.get("interaction_format", "Single Interaction"),
        interaction_type=d.get("interaction_type", ""),
        course_code="",  # supplied at runtime
        subject=d.get("subject", ""),
        academic_activities=list(d.get("academic_activities", [])),
        body=d.get("body", ""),
        submit=bool(d.get("submit", True)),
        append_clipboard=bool(d.get("append_clipboard", False)),
    )


def load_scenarios(path: Path = NOTES_YAML) -> dict[str, ScenarioConfig]:
    """Load all scenarios from notes.yaml, keyed by name."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    sections = raw.get("scenarios", {})
    if not isinstance(sections, dict):
        raise ValueError(
            f"{path}: 'scenarios' must be a mapping of name -> config"
        )
    out: dict[str, ScenarioConfig] = {}
    for name, cfg in sections.items():
        notes = [_note_from_dict(n) for n in cfg.get("notes", [])]
        if not notes:
            raise ValueError(f"Scenario {name!r} has no notes defined")
        out[name] = ScenarioConfig(
            name=name,
            hotkey=cfg.get("hotkey", ""),
            close_tab_after=bool(cfg.get("close_tab_after", True)),
            notes=notes,
        )
    return out


def run_scenario(
    target: Page,
    scenario: ScenarioConfig,
    course_code: str,
    clipboard: str = "",
    on_status: Optional[Callable[[str], None]] = None,
) -> bool:
    """Fill (and optionally submit) every note in the scenario against
    the active student. If a note has append_clipboard=True and
    `clipboard` is non-empty, the clipboard text is appended after the
    body, then the combined string is capped at MAX_BODY_LINES /
    MAX_BODY_CHARS — when that cap kicks in, `on_status` is invoked
    with a notice. Returns True only if every note had submit=True."""
    all_submitted = True
    for template in scenario.notes:
        note = replace(template, course_code=course_code)
        if template.append_clipboard and clipboard:
            combined, trimmed = combine_with_clipboard(template.body, clipboard)
            if trimmed and on_status:
                on_status(
                    f"Note body trimmed (max {MAX_BODY_LINES} lines / "
                    f"{MAX_BODY_CHARS} chars)."
                )
            note = replace(note, body=combined)
        fill_note(target, note)
        if not template.submit:
            all_submitted = False
    if scenario.close_tab_after and all_submitted:
        close_workspace_tab(target)
    return all_submitted
