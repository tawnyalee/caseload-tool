"""Read the caseload from a Salesforce CSV export.

When the launcher's worker browser drives the Caseload list view, the
"Export" UI produces a CSV that's much faster to filter against than
scraping the DOM (millisecond reads vs. ~10s scroll-to-load-all). The
CSV also contains *all* fields rather than just the visible columns.

This module:
1. Reads the CSV from a fixed path (`CASELOAD_CSV_PATH` in config)
2. Translates user-facing display column names ("Last Assigned CI
   Contact") to the CSV's actual column names ("MyCourseContact") via
   `DISPLAY_TO_CSV` so YAML scenarios stay readable

Round 2 of the caseload-cache work adds Playwright-driven download
of the CSV (clicking Salesforce's Export → Current view), saving to
the same path with `page.expect_download()`.
"""
import csv
from datetime import datetime
from pathlib import Path
from typing import Optional


# Display name (what the user sees in the Caseload table) → the
# corresponding CSV column header (what Salesforce's export uses).
# Identity entries are listed explicitly so the table is the single
# source of truth for what we recognize.
DISPLAY_TO_CSV: dict[str, str] = {
    "Name": "Name",
    "Student ID": "StudentID",
    "Student Preferred Name": "stuprename",
    "Momentum": "Momentum",
    "Program Mentor": "MentorName",
    "Course Code": "CourseCode",
    "Term End Date": "TermEndDate",
    "IC End date": "Icenddate",
    "Timezone": "Timezone",
    "Task1": "Task1",
    "Task2": "Task2",
    "Task3": "Task3",
    "Course Instructor": "CourseMentor",
    "Course Start Date": "CourseStartDate",
    "Course End Date": "CourseEndDate",
    "Assignment End Date": "AssignmentEndDate",
    "Assignment Start Date": "caseload.AssignmentStartDate__c",
    "Other Courses": "OtherCourses",
    "Program Name": "ProgramName",
    "Remaining CUs": "TermRemainingCU",
    "Last Assigned CI Contact": "MyCourseContact",
}


def resolve_column(name: str, csv_headers: list[str]) -> str:
    """Translate a YAML `column:` value to a CSV header name.

    Strategy:
    1. If the value already matches a CSV header exactly → return as-is.
    2. Else look up in DISPLAY_TO_CSV → translate.
    3. Else return as-is and let the filter engine miss; the caller
       should report this as a "column not found" warning.
    """
    if not name:
        return name
    if name in csv_headers:
        return name
    return DISPLAY_TO_CSV.get(name, name)


def load_caseload_csv(path: Path) -> list[dict]:
    """Read a Salesforce caseload CSV export. Returns a list of dicts
    keyed by CSV header name. Trailing-empty-column rows (Salesforce
    sometimes emits a trailing comma in the header) are tolerated;
    that empty key is dropped from each row.

    Raises FileNotFoundError if the file doesn't exist."""
    rows: list[dict] = []
    with Path(path).open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            # Drop the empty-string key (artifact of trailing comma)
            # and strip whitespace off every value.
            row = {
                k: (v.strip() if isinstance(v, str) else v)
                for k, v in raw.items()
                if k is not None and k != ""
            }
            if any(row.values()):
                rows.append(row)
    return rows


def csv_age_human(path: Path) -> str:
    """Human-readable age string for the file at `path`, used in the
    status bar. 'just now', '12 minutes ago', '3 hours ago', etc."""
    try:
        mtime = datetime.fromtimestamp(Path(path).stat().st_mtime)
    except OSError:
        return "unknown"
    delta = datetime.now() - mtime
    secs = int(delta.total_seconds())
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60} min ago"
    if secs < 86_400:
        return f"{secs // 3600} hr ago"
    return f"{secs // 86_400} days ago"


def csv_mtime(path: Path) -> Optional[datetime]:
    try:
        return datetime.fromtimestamp(Path(path).stat().st_mtime)
    except OSError:
        return None
