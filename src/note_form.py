"""Fill a single visible Student Note panel.

Phase 1 contract: the user has already navigated to the student record and
opened the note panel. This module finds the *visible* note form and fills
the supplied fields. It does NOT click Submit by default — the user
reviews and submits manually until we're confident in the selectors.
"""
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

from src import selectors
from src.config import SCREENSHOTS_DIR

InteractionFormat = Literal["Single Interaction", "Multiple Interactions"]


@dataclass
class NoteData:
    interaction_format: InteractionFormat = "Single Interaction"
    interaction_type: str = ""             # exact <option> text in noteType select; "" = skip
    course_code: str = ""                  # text into Enter Course Code; "" = skip
    subject: str = ""                      # text into Subject; "" = skip (form will reject)
    academic_activities: list[str] = field(default_factory=list)  # checkbox labels to tick
    body: str = ""                         # rich-text body; "" = skip
    submit: bool = False                   # leave False while we dial in selectors
    append_clipboard: bool = False         # paste clipboard text after body at fire time


def _screenshot_failure(page: Page, tag: str) -> Path:
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    out = SCREENSHOTS_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-{tag}.png"
    page.screenshot(path=str(out), full_page=True)
    return out


def wait_for_submit_complete(page: Page, timeout_ms: int = 15_000) -> None:
    """After clicking Submit, wait until Salesforce has settled the
    submission. Two equally-valid signals: Submit becomes hidden (panel
    closed) or Submit becomes disabled (form cleared, required fields
    empty again). Either means the click was processed."""
    deadline = time.monotonic() + timeout_ms / 1000.0
    submit_role = page.get_by_role("button", name="Submit", exact=True)
    while time.monotonic() < deadline:
        try:
            visible = submit_role.filter(visible=True)
            if visible.count() == 0:
                return
            if not visible.first.is_enabled():
                return
        except Exception:
            return
        page.wait_for_timeout(150)


def close_workspace_tab(page: Page) -> None:
    """Close the current Salesforce Lightning workspace tab via Shift+X."""
    page.keyboard.press("Shift+X")


def fill_note(page: Page, data: NoteData, *, timeout_ms: int = 10_000) -> None:
    try:
        # Wait for the note panel to be present and visible. The Submit
        # button is a good anchor because every panel has one.
        selectors.submit_button(page).wait_for(state="visible", timeout=timeout_ms)

        # Lightning Web Components: clicking the underlying <input> doesn't
        # trigger the reactive state update; the label is the real click
        # target. selectors return the <label> element; .click() is enough
        # (no .check() — that would try to verify the input's `checked`
        # attribute, which Lightning re-renders away).
        if data.interaction_format:
            selectors.interaction_format_radio(page, data.interaction_format).click()

        if data.interaction_type:
            selectors.interaction_type_select(page).select_option(label=data.interaction_type)

        if data.course_code:
            cc = selectors.course_code_input(page)
            cc.click()
            cc.fill(data.course_code)

        if data.subject:
            subj = selectors.subject_input(page)
            subj.click()
            subj.fill(data.subject)

        for label in data.academic_activities:
            selectors.academic_activity_checkbox(page, label).click()

        if data.body:
            editor = selectors.note_body_editor(page)
            editor.click(force=True)
            # Quill editor reacts to keyboard input; .fill works for contenteditable
            # but using type() preserves newlines correctly.
            for i, line in enumerate(data.body.splitlines() or [data.body]):
                if i > 0:
                    page.keyboard.press("Enter")
                page.keyboard.type(line)

        if data.submit:
            submit_btn = selectors.submit_button(page)
            if not submit_btn.is_enabled():
                raise RuntimeError(
                    "Submit button is disabled — a required field is missing "
                    "(check Interaction Type, Subject/Course Code, body, and "
                    "any Academic Activity gates)."
                )
            submit_btn.click()
            wait_for_submit_complete(page)

    except PlaywrightTimeoutError as e:
        path = _screenshot_failure(page, "timeout")
        raise RuntimeError(
            f"Timed out filling note. Screenshot: {path}. Underlying: {e}"
        ) from e
    except Exception as e:
        path = _screenshot_failure(page, "error")
        raise RuntimeError(
            f"Error filling note. Screenshot: {path}. Underlying: {e}"
        ) from e
