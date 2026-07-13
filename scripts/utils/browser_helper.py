import time as _t
from src.student_lookup import get_active_student_name

# Define the set locally so the function can check it without relying on launcher.py globals
ACTIVITY_DISABLE_TYPES_SINGLE = {
    "Email to Student", "Voicemail to Student", "Admin Note", "Mass Email",
}

def activities_disabled_for(fmt: str, typ: str) -> bool:
    return fmt == "Single Interaction" and typ in ACTIVITY_DISABLE_TYPES_SINGLE

def _wait_grid_settled(page, max_ms: int = 1500) -> None:
    """After typing into the caseload row-filter, wait until the grid has
    settled — no visible loading spinner and the row count stable across two
    polls — BOUNDED by max_ms."""
    try:
        page.wait_for_timeout(120)  # let a spinner appear before polling
    except Exception:
        return
    last, stable = -1, 0
    deadline = _t.monotonic() + max_ms / 1000.0
    while _t.monotonic() < deadline:
        try:
            spinner = page.locator(".slds-spinner_container").filter(visible=True).count()
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
    active student name resolves) — BOUNDED by max_ms."""
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