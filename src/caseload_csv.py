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
import io
import re
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
    "Followup Note": "CourseFollowupNote",
    "Followup Date": "CourseFollowupDate",
    "Task 1": "Task1",
    "Task 2": "Task2",
    "Task 3": "Task3",
    "Course Instructor": "CourseMentor",
    "Course Start Date": "CourseStartDate",
    "Course End Date": "CourseEndDate",
    "Assignment End Date": "AssignmentEndDate",
    "Assignment Start Date": "caseload.AssignmentStartDate__c",
    "Other Courses": "OtherCourses",
    "Program Name": "ProgramName",
    "Remaining CUs": "TermRemainingCU",
    "Last Assigned CI Contact": "MyCourseContact",
    # Synthetic column injected from the Essential Actions dashboard scrape.
    "Essential Action": "EssentialAction",
    # Derived signal columns computed by the launcher (App._apply_derived_
    # columns_to_rows) from the CSV dates, note_log.csv, and history.db — so
    # success-path gates / filters can express time- and progress-based
    # conditions the raw export doesn't carry.
    "Days Since Last Contact": "DaysSinceLastContact",
    "Days Since Last Action": "DaysSinceLastAction",
    "Last Action Type": "LastActionType",
    "Days Since Course Start": "DaysSinceCourseStart",
    "Days Until Term End": "DaysUntilTermEnd",
    "Task Stalled (days)": "TaskStalledDays",
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
    if name in DISPLAY_TO_CSV:
        return DISPLAY_TO_CSV[name]
    # Synthetic per-task status columns ("Task 2 Status" → "Task2Status"),
    # generated dynamically for whatever task numbers a caseload has.
    m = re.fullmatch(r"Task (\d+) Status", name)
    if m:
        return f"Task{m.group(1)}Status"
    return name


# Reverse direction: CSV header → display name. Used by the editor's
# Filters dropdown to show "Last Assigned CI Contact" instead of the
# raw "MyCourseContact". Unmapped headers pass through unchanged
# (the CSV may carry columns we haven't catalogued).
CSV_TO_DISPLAY: dict[str, str] = {v: k for k, v in DISPLAY_TO_CSV.items()}


def display_for_column(csv_header: str) -> str:
    """Companion to `resolve_column` — CSV header → user-facing label.
    Returns the input unchanged when we don't have a mapping (so
    custom CSV columns still show up in the dropdown, just under
    their raw names)."""
    if not csv_header:
        return csv_header
    if csv_header in CSV_TO_DISPLAY:
        return CSV_TO_DISPLAY[csv_header]
    # Synthetic per-task status columns ("Task2Status" → "Task 2 Status").
    m = re.fullmatch(r"Task(\d+)Status", csv_header)
    if m:
        return f"Task {m.group(1)} Status"
    return csv_header


def _row_from(header: list[str], fields: list[str]) -> dict:
    """Build a row dict from parallel header/field lists, dropping the
    empty-string key (trailing-comma artifact) and stripping values."""
    row: dict = {}
    for k, v in zip(header, fields):
        if k is None or k == "":
            continue
        row[k] = v.strip() if isinstance(v, str) else v
    return row


def _rfc_parse(text: str) -> list[dict]:
    """Standard RFC-4180 parse (the original behavior). Used as a
    fallback when the quote-aware fast path can't be trusted."""
    rows: list[dict] = []
    reader = csv.DictReader(io.StringIO(text))
    for raw in reader:
        row = {
            k: (v.strip() if isinstance(v, str) else v)
            for k, v in raw.items()
            if k is not None and k != ""
        }
        if any(row.values()):
            rows.append(row)
    return rows


def load_caseload_csv(path: Path) -> list[dict]:
    """Read a Salesforce caseload CSV export → list of dicts keyed by CSV
    header. The empty-string key (trailing-comma artifact) is dropped.

    WGU's export quotes EVERY data field but does NOT RFC-escape embedded
    double-quotes (a Cadence auto-text like
    `... a C769 "welcome email." ...` writes a lone `"` instead of `""`).
    A standard parser treats that lone `"` as the field's closing quote
    and the rest of the line spills into extra columns, scrambling that
    student's whole row (and spawning a phantom row). To be robust we
    parse the data region by its STRUCTURAL delimiters instead — fields
    are separated by `","` and records by `"`-newline-`"`, with every
    field wrapped in quotes — so a stray content quote is just text and
    can't break alignment. Falls back to the RFC parser if the export
    isn't in the expected fully-quoted shape; quarantines (skips) any
    single record that still doesn't line up rather than corrupt the set.

    Raises FileNotFoundError if the file doesn't exist."""
    text = Path(path).read_text(encoding="utf-8-sig")
    nl = text.find("\n")
    if nl == -1:
        return _rfc_parse(text)
    try:
        header = next(csv.reader([text[:nl].rstrip("\r")]))
    except Exception:
        return _rfc_parse(text)
    hn = len(header)
    data = text[nl + 1:].rstrip("\r\n")
    # Quote-aware fast path only when the data region is fully quoted.
    if hn and data[:1] == '"' and data[-1:] == '"':
        core = data[1:-1]
        records = re.split(r'"\r?\n"', core)
        out: list[dict] = []
        quarantined = 0
        for rs in records:
            fields = rs.split('","')
            if len(fields) != hn:
                # A record whose field count is off (e.g. content holding
                # a literal '","') — skip it rather than misalign the rest.
                quarantined += 1
                continue
            row = _row_from(header, fields)
            if any(row.values()):
                out.append(row)
        # Trust the fast path only if it recovered the bulk of the rows.
        if out and quarantined <= max(2, len(records) // 20):
            return out
    return _rfc_parse(text)


def csv_header(path: Path) -> list[str]:
    """Return just the header row of a CSV (handles the unquoted-header,
    quoted-data shape). Empty list on any failure."""
    try:
        with Path(path).open(encoding="utf-8-sig", newline="") as f:
            return next(csv.reader(f), [])
    except Exception:
        return []


def dropped_columns(old_header, new_header) -> list[str]:
    """Columns present in `old_header` but missing from `new_header` (order +
    de-duped by first appearance, blanks ignored). Used by the download
    anti-clobber guard to detect when a fresh caseload export lost columns."""
    new = set(new_header or [])
    out, seen = [], set()
    for c in (old_header or []):
        if c and c not in new and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def critical_columns_dropped(old_header, new_header, critical) -> list[str]:
    """Subset of `critical` that `new_header` dropped relative to `old_header`
    — i.e. join columns (e.g. StudentID) the CSV fallback can't work without.
    Non-empty => the guard should REJECT the new export and keep the old CSV."""
    lost = set(dropped_columns(old_header, new_header))
    return [c for c in (critical or []) if c in lost]


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
