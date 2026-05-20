"""Read student context from the Salesforce / Caseload DOM.

The visible note panel's header tells us WHICH student we're working on.
The Caseload table (a separate Lightning tab — still in DOM as long as it
was opened this session) tells us what course code to file the note under.
"""
import re
from typing import Optional

from playwright.sync_api import Page

NOTE_HEADER_PREFIX = "Create a New Student Note for "
COURSE_CODE_RE = re.compile(r"\s*([A-Z]\d{3})")


def get_active_student_name(page: Page) -> Optional[str]:
    """Read the student name from the visible note panel header.
    Returns None if no note panel is open."""
    loc = page.get_by_text(NOTE_HEADER_PREFIX, exact=False).filter(visible=True)
    if loc.count() == 0:
        return None
    text = (loc.first.text_content() or "").strip()
    if NOTE_HEADER_PREFIX in text:
        return text.split(NOTE_HEADER_PREFIX, 1)[1].strip()
    return None


def detect_course_code(page: Page, student_name: str) -> Optional[str]:
    """Look up the student's course code from any Caseload-style table in
    DOM. Returns None if no such table exists, the student isn't in it, or
    the cell doesn't parse as a course code."""
    tables = page.locator("table").filter(
        has=page.locator("th", has_text="Course Code")
    )
    table_count = tables.count()
    for i in range(table_count):
        table = tables.nth(i)
        headers = table.locator("th").all_text_contents()
        col_idx = next(
            (j for j, h in enumerate(headers) if "Course Code" in h),
            None,
        )
        if col_idx is None:
            continue
        rows = table.locator("tr").filter(has_text=student_name)
        for r in range(rows.count()):
            row = rows.nth(r)
            cells = row.locator("td").all_text_contents()
            if col_idx < len(cells):
                m = COURSE_CODE_RE.match(cells[col_idx])
                if m:
                    return m.group(1)
    return None
