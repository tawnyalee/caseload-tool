"""Tests for the download column-drop guard + EA JSON-feed mapping.

Covers:
  - caseload_csv.dropped_columns / critical_columns_dropped: the pure decision
    behind the anti-clobber guard that REJECTS a caseload export which lost a
    critical join column (StudentID) instead of overwriting the good CSV.
  - student_lookup.ea_rows_from_records: mapping the EA dashboard JSON feed
    (EmployeeEvent__c records) to row dicts, deduped one-per-student.
  - student_lookup.ea_view_missing_student_id: telling "0 EAs" apart from
    "the EA view dropped the Student ID column" (scrape fallback).

Run: python tests/test_caseload_guards.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import caseload_csv  # noqa: E402
from src.student_lookup import (  # noqa: E402
    ea_rows_from_records, ea_view_missing_student_id, _EA_COLCHECK_JS,
)

CRITICAL = ("StudentID",)


# --- dropped_columns -------------------------------------------------------

def test_dropped_columns_basic():
    old = ["StudentID", "Name", "CourseCode", "Momentum"]
    new = ["Name", "CourseCode", "Momentum"]
    assert caseload_csv.dropped_columns(old, new) == ["StudentID"]


def test_dropped_columns_none_when_superset():
    old = ["Name", "CourseCode"]
    new = ["StudentID", "Name", "CourseCode", "Extra"]
    assert caseload_csv.dropped_columns(old, new) == []


def test_dropped_columns_ignores_blanks_and_dedupes():
    old = ["StudentID", "", "StudentID", "Name"]
    new = ["Name"]
    # blank ignored, StudentID reported once
    assert caseload_csv.dropped_columns(old, new) == ["StudentID"]


def test_dropped_columns_empty_inputs():
    assert caseload_csv.dropped_columns([], ["Name"]) == []
    assert caseload_csv.dropped_columns(None, None) == []


# --- critical_columns_dropped ---------------------------------------------

def test_critical_dropped_triggers_on_studentid():
    old = ["StudentID", "Name", "CourseCode"]
    new = ["Name", "CourseCode"]
    assert caseload_csv.critical_columns_dropped(old, new, CRITICAL) == [
        "StudentID"]


def test_critical_dropped_empty_when_noncritical_lost():
    # A cosmetic column vanished but StudentID stayed -> accept the new CSV.
    old = ["StudentID", "Name", "Momentum"]
    new = ["StudentID", "Name"]
    assert caseload_csv.critical_columns_dropped(old, new, CRITICAL) == []
    # (but dropped_columns still flags the cosmetic loss)
    assert caseload_csv.dropped_columns(old, new) == ["Momentum"]


def test_critical_dropped_empty_when_nothing_lost():
    old = ["StudentID", "Name"]
    new = ["StudentID", "Name", "CourseCode"]
    assert caseload_csv.critical_columns_dropped(old, new, CRITICAL) == []


# --- ea_rows_from_records --------------------------------------------------

def _ea_record(sid, name="Stu Dent", reason="Task Returned", course="C769"):
    return {
        "Name": reason,
        "CourseCode__c": course,
        "Status__c": "To Do",
        "DueDate__c": "2026-07-10",
        "Intervention__c": "Outreach",
        "VisibilityStartDate__c": "2026-07-01",
        "Contact__r": {"StudentID__c": sid, "Name": name},
    }


def test_ea_rows_maps_fields():
    rows = ea_rows_from_records([_ea_record("012253133", name="Jane Roe")])
    assert len(rows) == 1
    r = rows[0]
    assert r["student_id"] == "012253133"
    assert r["name"] == "Jane Roe"
    assert r["reason"] == "Task Returned"
    assert r["course"] == "C769"
    assert r["event_progress"] == "To Do"
    assert r["followup_date"] == "2026-07-10"
    assert r["intervention"] == "Outreach"
    assert r["date_added"] == "2026-07-01"


def test_ea_rows_dedupes_one_per_student():
    # Same student with two EAs -> a single row (first wins), matching scrape.
    recs = [_ea_record("111", reason="First"),
            _ea_record("111", reason="Second")]
    rows = ea_rows_from_records(recs)
    assert len(rows) == 1
    assert rows[0]["reason"] == "First"


def test_ea_rows_skips_blank_or_missing_studentid():
    recs = [
        _ea_record("222"),
        {"Name": "No contact", "Contact__r": {}},          # no StudentID__c
        {"Name": "Blank sid", "Contact__r": {"StudentID__c": ""}},
        {"Name": "No contact key"},                         # no Contact__r
        "not a dict",
    ]
    rows = ea_rows_from_records(recs)
    assert [r["student_id"] for r in rows] == ["222"]


def test_ea_rows_empty_input():
    assert ea_rows_from_records([]) == []
    assert ea_rows_from_records(None) == []


# --- ea_view_missing_student_id -------------------------------------------

class _FakePage:
    """Minimal stand-in: page.evaluate(js) returns a canned value (or raises)."""
    def __init__(self, result=None, raises=False):
        self._result = result
        self._raises = raises
        self.last_js = None

    def evaluate(self, js, *args):
        self.last_js = js
        if self._raises:
            raise RuntimeError("evaluate boom")
        return self._result


def test_ea_view_missing_sid_true_when_rows_but_no_sid():
    # Grid rendered data rows, none carry a Student ID cell -> column missing.
    page = _FakePage({"dataRows": 12, "sidRows": 0, "columns": ["Reason"]})
    assert ea_view_missing_student_id(page) is True
    assert page.last_js == _EA_COLCHECK_JS  # used the real probe JS


def test_ea_view_missing_sid_false_when_sid_present():
    page = _FakePage({"dataRows": 12, "sidRows": 12, "columns": ["Student ID"]})
    assert ea_view_missing_student_id(page) is False


def test_ea_view_missing_sid_false_when_no_data_rows():
    # Genuinely empty queue -> not a column problem.
    page = _FakePage({"dataRows": 0, "sidRows": 0, "columns": []})
    assert ea_view_missing_student_id(page) is False


def test_ea_view_missing_sid_false_on_evaluate_error():
    assert ea_view_missing_student_id(_FakePage(raises=True)) is False


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
