"""Centralized locators for the Caseload / Salesforce Student Note panel.

Findings from inspecting the saved HTML:
- The DOM can contain MULTIPLE copies of the note form (one per open
  Salesforce workspace tab). Every locator below scopes to the visible
  copy via .filter(visible=True).first.
- Form fields have stable handles — we deliberately avoid auto-generated
  Lightning IDs like input-235, radio-0-226, 1615:0.

Stable handles used:
- Interaction Format    -> role=radio, name="Single Interaction" / "Multiple Interactions"
- Interaction Type      -> select[name="noteType"]   (real HTML <select>)
- Enter Course Code     -> get_by_label("Enter Course Code")
- Subject               -> input[name="subject"]
- Academic Activities   -> role=checkbox, name=<label text>
- Note body             -> role=textbox, name="Compose text"   (Quill editor)
- Submit / Clear        -> role=button, name="Submit" / "Clear"
"""
from playwright.sync_api import Locator, Page


def _visible_first(locator: Locator) -> Locator:
    return locator.filter(visible=True).first


def interaction_format_radio(page: Page, label: str) -> Locator:
    """Returns the visible <label> element associated with the radio.
    Lightning Web Components route state changes through the label's
    click handler, so clicking the underlying <input> (even forced)
    doesn't actually update the form. We target the label instead."""
    return _visible_first(
        page.locator("label[for]").filter(has_text=label)
    )


def interaction_type_select(page: Page) -> Locator:
    return _visible_first(page.locator('select[name="noteType"]'))


def course_code_input(page: Page) -> Locator:
    return _visible_first(page.get_by_label("Enter Course Code", exact=True))


def subject_input(page: Page) -> Locator:
    return _visible_first(page.locator('input[name="subject"]'))


def academic_activity_checkbox(page: Page, label: str) -> Locator:
    """Returns the visible <label> element associated with the checkbox.
    Same Lightning rationale as interaction_format_radio."""
    return _visible_first(
        page.locator("label[for]").filter(has_text=label)
    )


def note_body_editor(page: Page) -> Locator:
    return _visible_first(page.get_by_role("textbox", name="Compose text", exact=True))


def submit_button(page: Page) -> Locator:
    return _visible_first(page.get_by_role("button", name="Submit", exact=True))


def clear_button(page: Page) -> Locator:
    return _visible_first(page.get_by_role("button", name="Clear", exact=True))


ACADEMIC_ACTIVITY_LABELS = [
    "Course/Program Information Discussed",
    "Course/Program Information Requested",
    "Set Academic Goals",
    "Student Learning Occurred",
    "Personal obstacles/non-academic content covered",
]
