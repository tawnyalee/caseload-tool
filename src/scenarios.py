"""Load scenario definitions from scenarios.yaml and run them against an
open note panel. Each scenario is one or more NoteData entries filed in
sequence, optionally followed by closing the Salesforce workspace tab.
"""
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml
from playwright.sync_api import Page

from typing import Callable, Optional

from src.config import SCENARIOS_YAML, NOTES_YAML
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
class Prompt:
    """One at-fire-time input. The user is asked for a value when
    the scenario fires; the value is then bound to `{{var}}` in
    every email and note body / subject / to-field that references
    it. Generalizes the per-note `enter_additional_text` checkbox
    for cases where the same input should reach multiple destinations
    (e.g. a call summary that lands in both the email body and the
    Salesforce note).

    For batch scenarios, prompts are asked ONCE before the loop and
    the same value is applied to every student in the batch."""
    var: str
    label: str = ""
    multiline: bool = True
    prefill: str = ""


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
    # When True, the user is asked to pick which template (and confirm the
    # subject) at fire time, instead of always using `body_html_file`.
    # Lets one "send email" scenario cover many templates.
    pick_template: bool = False
    # Font of the SENT message. Empty family or size=0 means "use
    # Outlook's compose default" (no CSS injection). When both are
    # set, the rendered HTML body is wrapped in an inline-styled div
    # before going to Outlook.
    font_family: str = ""
    font_size: int = 0


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
    prompts: list[Prompt] = field(default_factory=list)
    notes: list[NoteData] = field(default_factory=list)
    # When True, this scenario appears in the caseload panel's
    # "Fire scenario" menus (row right-click + selection action bar).
    # Batch scenarios can't be panel actions — the panel's batch-style
    # workflow is "filter the view → select rows → apply a single action",
    # so the editor disables this for batch scenarios. If NO scenario sets
    # it, the panel falls back to listing every non-batch scenario.
    panel_action: bool = False


@dataclass
class Group:
    """User-defined grouping of scenarios in the launcher UI. Groups
    have a display name + a color (hex string) that scenarios in
    the group adopt for their button background. Scenarios are
    referenced by name; ones not in any group render as "ungrouped"
    at the top of the launcher's scenario list.

    Stored in scenarios.yaml under a top-level `groups:` block parallel
    to `scenarios:`. Order matters — groups display top-to-bottom
    in the order they appear in the list."""
    name: str
    color: str = "#7a7a7a"
    scenarios: list[str] = field(default_factory=list)


def _note_from_dict(d: dict) -> NoteData:
    return NoteData(
        interaction_format=d.get("interaction_format", "Single Interaction"),
        interaction_type=d.get("interaction_type", ""),
        course_code="",  # supplied at runtime
        course_code_override=str(d.get("course_code_override", "") or "").strip(),
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
    try:
        font_size = int(d.get("font_size", 0) or 0)
    except (TypeError, ValueError):
        font_size = 0
    return EmailConfig(
        subject=d.get("subject", ""),
        body_html_file=d.get("body_html_file", ""),
        to=d.get("to", ""),
        signature_file=d.get("signature_file", ""),
        inline_images=list(d.get("inline_images") or []),
        cc_pm=bool(d.get("cc_pm", False)),
        pick_template=bool(d.get("pick_template", False)),
        font_family=str(d.get("font_family", "") or ""),
        font_size=font_size,
    )


def _batch_from_dict(d: Optional[dict]) -> Optional[BatchConfig]:
    if d is None:  # `batch:` key absent — not a batch scenario
        return None
    return BatchConfig(
        filters=list(d.get("filters") or []),
        preview=bool(d.get("preview", True)),
    )


def _groups_from_list(items: Optional[list]) -> list[Group]:
    """Parse the `groups:` block from notes.yaml. Drops entries
    without a name (the only required field). Color falls back to
    neutral gray for entries that don't specify one. Scenario
    references are NOT validated against the scenarios dict at
    load time — the launcher resolves them lazily so a typoed name
    or a since-deleted scenario doesn't crash startup."""
    if not items:
        return []
    out: list[Group] = []
    for d in items:
        if not isinstance(d, dict):
            continue
        name = str(d.get("name", "") or "").strip()
        if not name:
            continue
        out.append(Group(
            name=name,
            color=(str(d.get("color", "") or "#7a7a7a").strip()
                   or "#7a7a7a"),
            scenarios=[
                str(s).strip()
                for s in (d.get("scenarios") or [])
                if s and str(s).strip()
            ],
        ))
    return out


def load_groups(path: Path = SCENARIOS_YAML) -> list[Group]:
    """Read `groups:` from scenarios.yaml. Returns an empty list if the
    file has no groups block (the launcher then shows every
    scenario as ungrouped). Failure to read / parse returns an
    empty list rather than raising — groups are a UI feature and
    shouldn't block startup."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return []
    if not isinstance(raw, dict):
        return []
    return _groups_from_list(raw.get("groups"))


def _prompts_from_list(items: Optional[list]) -> list[Prompt]:
    if not items:
        return []
    out: list[Prompt] = []
    for d in items:
        if not isinstance(d, dict):
            continue
        var = str(d.get("var", "")).strip()
        if not var:
            continue
        out.append(Prompt(
            var=var,
            label=str(d.get("label", "") or var),
            multiline=bool(d.get("multiline", True)),
            prefill=str(d.get("prefill", "")),
        ))
    return out


def load_scenarios(path: Path = SCENARIOS_YAML) -> dict[str, ScenarioConfig]:
    """Load all scenarios from scenarios.yaml, keyed by name."""
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
            prompts=_prompts_from_list(cfg.get("prompts")),
            notes=notes,
            panel_action=bool(cfg.get("panel_action", False)),
        )
    return out


def _substitute_vars(text: str, variables: Optional[dict]) -> str:
    """Plain-text `{{var}}` substitution for note bodies. No HTML
    escaping (note bodies go into Salesforce's editor as text).
    Unknown placeholders are left in place. Returns input unchanged
    if variables is None/empty."""
    if not variables or not text:
        return text
    import re
    pat = re.compile(r"\{\{\s*(\w+)\s*\}\}")
    def replace(m):
        name = m.group(1)
        if name in variables:
            return str(variables[name] or "")
        return m.group(0)
    return pat.sub(replace, text)


def run_scenario(
    target: Page,
    scenario: ScenarioConfig,
    course_code: str,
    clipboard: str = "",
    custom_bodies: Optional[dict[int, str]] = None,
    prompt_vars: Optional[dict[str, str]] = None,
    on_status: Optional[Callable[[str], None]] = None,
) -> bool:
    """Fill (and optionally submit) every note in the scenario against
    the active student.

    - `custom_bodies` maps note-index -> body text. When present, that
      body replaces the template's body for that note (the user
      supplied it via the 'Enter additional text' dialog at fire time).
    - `prompt_vars` is the dict of `{{var}}` substitutions from the
      scenario's `prompts:` block (collected on the main thread at
      fire time). Applied to note bodies AFTER any custom_bodies
      replacement so placeholders inside custom text get resolved too.
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
        # Substitute prompt vars (e.g. {{summary}}) — applies whether
        # the body came from the template OR from custom_bodies.
        base_body = _substitute_vars(base_body, prompt_vars)
        # Per-note course-code override wins over the auto-detected
        # value when set. Falls back to the detected code otherwise.
        # Lets a single scenario file notes against multiple courses
        # by pinning each note to its own.
        per_note_code = template.course_code_override or course_code
        is_custom = i in custom_bodies
        # When the note has a custom body (the user edited it at fire time),
        # the clipboard was already folded into that text during review —
        # don't append it again here.
        will_append_clip = (
            template.append_clipboard and clipboard and not is_custom)
        if will_append_clip or is_custom:
            combined, trimmed = combine_with_clipboard(
                base_body, clipboard if will_append_clip else "",
            )
            if trimmed and on_status:
                on_status(
                    f"Note body trimmed (max {MAX_BODY_LINES} lines / "
                    f"{MAX_BODY_CHARS} chars)."
                )
            note = replace(template, course_code=per_note_code, body=combined)
        else:
            note = replace(template, course_code=per_note_code, body=base_body)
        fill_note(target, note)
        if not template.submit:
            all_submitted = False
            if on_status:
                on_status(
                    f"Note {i + 1} filled but NOT submitted — "
                    "'Submit when filled' is unchecked in the scenario "
                    "editor. Click Submit in Salesforce to save the "
                    "note, or check the box and re-fire."
                )
    if scenario.close_tab_after and all_submitted:
        close_workspace_tab(target)
    return all_submitted
