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
class EmailConfig:
    """Outlook email step config for a scenario.

    `inline_images` is a list of image filenames living in the user's
    templates directory. Each image's CID (referenced in the HTML body
    as `<img src="cid:NAME">`) is auto-derived from the filename stem
    — e.g. `signature.png` → CID `signature`.

    `to` is an optional override of the recipient address. If left
    empty, the email goes to `{{student_email}}` from the caseload
    row. Otherwise the string is rendered with the same variables as
    the subject/body — useful for test mode, e.g.
    `to: "test{{first_name}}{{last_name}}@wgu.edu"`.

    `signature_file` is the filename stem (no `.htm`) of one of the
    user's Outlook signatures, used for **auto-sent batch emails**
    only (the interactive Display() flow captures the signature via
    Outlook directly). If empty, the launcher falls back to the
    user's default new-mail signature; if none can be detected,
    auto-sent emails go without a signature."""
    subject: str = ""
    body_html_file: str = ""
    to: str = ""
    signature_file: str = ""
    inline_images: list[str] = field(default_factory=list)
    cc_pm: bool = False


@dataclass
class BatchConfig:
    """Batch-mode config for a scenario. When present, find_first is
    ignored — the batch driver picks students by applying `filters`
    against the live Caseload rows.

    Each filter dict has keys: `column`, `op`, and (for most ops)
    `value`. See `src/caseload_filter.py` for the operator vocabulary
    and date/number value formats. Filters AND together."""
    filters: list[dict] = field(default_factory=list)
    preview: bool = True  # show review/confirm dialog before running


@dataclass
class ScenarioConfig:
    name: str
    hotkey: str
    close_tab_after: bool
    find_first: bool = False
    email: Optional[EmailConfig] = None
    batch: Optional[BatchConfig] = None
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
        enter_additional_text=bool(d.get("enter_additional_text", False)),
    )


def _email_from_dict(d: Optional[dict]) -> Optional[EmailConfig]:
    if not d:
        return None
    return EmailConfig(
        subject=d.get("subject", ""),
        body_html_file=d.get("body_html_file", ""),
        to=d.get("to", ""),
        signature_file=d.get("signature_file", ""),
        inline_images=list(d.get("inline_images") or []),
        cc_pm=bool(d.get("cc_pm", False)),
    )


def _batch_from_dict(d: Optional[dict]) -> Optional[BatchConfig]:
    if d is None:  # `batch:` key absent — not a batch scenario
        return None
    return BatchConfig(
        filters=list(d.get("filters") or []),
        preview=bool(d.get("preview", True)),
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
            find_first=bool(cfg.get("find_first", False)),
            email=_email_from_dict(cfg.get("email")),
            batch=_batch_from_dict(cfg.get("batch")),
            notes=notes,
        )
    return out


def run_scenario(
    target: Page,
    scenario: ScenarioConfig,
    course_code: str,
    clipboard: str = "",
    custom_bodies: Optional[dict[int, str]] = None,
    on_status: Optional[Callable[[str], None]] = None,
) -> bool:
    """Fill (and optionally submit) every note in the scenario against
    the active student.

    - `custom_bodies` maps note-index -> body text. When present, that
      body replaces the template's body for that note (the user
      supplied it via the 'Enter additional text' dialog at fire time).
    - `clipboard` is appended after the body for any note that has
      append_clipboard=True.
    - The size cap (MAX_BODY_LINES / MAX_BODY_CHARS) applies whenever
      the body grew at fire time (custom dialog or clipboard append).
      Templates that go through unchanged are not capped, preserving
      pre-existing behavior.

    Returns True only if every note had submit=True."""
    all_submitted = True
    custom_bodies = custom_bodies or {}
    for i, template in enumerate(scenario.notes):
        base_body = custom_bodies.get(i, template.body)
        will_append_clip = template.append_clipboard and clipboard
        is_custom = i in custom_bodies
        if will_append_clip or is_custom:
            combined, trimmed = combine_with_clipboard(
                base_body, clipboard if will_append_clip else "",
            )
            if trimmed and on_status:
                on_status(
                    f"Note body trimmed (max {MAX_BODY_LINES} lines / "
                    f"{MAX_BODY_CHARS} chars)."
                )
            note = replace(template, course_code=course_code, body=combined)
        else:
            note = replace(template, course_code=course_code)
        fill_note(target, note)
        if not template.submit:
            all_submitted = False
    if scenario.close_tab_after and all_submitted:
        close_workspace_tab(target)
    return all_submitted
