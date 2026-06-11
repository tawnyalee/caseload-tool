"""Tests for the browser-free texting core (src/text_message.py).

No pytest in the venv — run directly:  python tests/test_text_message.py
Exits non-zero on the first failed assertion.
"""
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import text_message as tm  # noqa: E402


def test_render_message():
    out = tm.render_message(
        "Hi {{first_name}}, your {{course_code}} task is due.",
        {"first_name": "Sam", "course_code": "C769"},
    )
    assert out == "Hi Sam, your C769 task is due.", out
    # Unknown placeholder is left in place (visible typo, not silent drop).
    assert tm.render_message("Hi {{nope}}", {}) == "Hi {{nope}}"


def test_over_length():
    assert tm.over_length("x" * 306) == 0
    assert tm.over_length("x" * 310) == 4


def test_normalize_phone():
    assert tm.normalize_phone("(336) 213-2291") == "3362132291"
    assert tm.normalize_phone("5551234567") == "5551234567"
    assert tm.normalize_phone("1-555-123-4567") == "5551234567"
    assert tm.normalize_phone("+1 (555) 123-4567") == "5551234567"
    assert tm.normalize_phone("123") == ""        # too few digits
    assert tm.normalize_phone("") == ""


def test_group_by_timezone():
    students = [
        {"name": "A", "timezone": "EST"},
        {"name": "B", "timezone": "MST"},
        {"name": "C", "timezone": "EST"},
        {"name": "D", "timezone": ""},
    ]
    g = tm.group_by_timezone(students)
    assert [s["name"] for s in g["EST"]] == ["A", "C"], g
    assert [s["name"] for s in g["MST"]] == ["B"]
    assert [s["name"] for s in g[""]] == ["D"]


def test_compute_schedule_slot_west_of_team():
    # Team = Eastern. "Now" = 9:00 AM ET on a summer day (EDT/MDT in effect).
    team = "America/New_York"
    now = datetime(2026, 6, 11, 9, 0, tzinfo=ZoneInfo(team))
    # Mountain student, target 10:00 AM local. 10 AM MDT == 12:00 PM EDT.
    slot = tm.compute_schedule_slot("MST", team, target_hour=10, now=now)
    assert slot is not None
    assert (slot.hour12, slot.minute, slot.ampm) == (12, 0, "PM"), slot
    assert slot.date_str == "06/11/2026", slot.date_str
    assert "10:00 AM" in slot.student_local_str, slot.student_local_str
    assert slot.clamped is False


def test_compute_schedule_slot_rolls_to_tomorrow():
    # Eastern student, target 10 AM, but it's already 3 PM ET -> next day.
    team = "America/New_York"
    now = datetime(2026, 6, 11, 15, 0, tzinfo=ZoneInfo(team))
    slot = tm.compute_schedule_slot("EST", team, target_hour=10, now=now)
    assert slot is not None
    assert (slot.hour12, slot.minute, slot.ampm) == (10, 0, "AM"), slot
    assert slot.date_str == "06/12/2026", slot.date_str


def test_compute_schedule_slot_unknown_tz():
    assert tm.compute_schedule_slot("ZZZ", "America/New_York") is None


def test_compute_schedule_slot_clamps_for_western_team():
    # Team = Pacific. Eastern student target 10 AM local = 7 AM PT, before the
    # 8 AM team window -> clamp up to 8:00 AM PT.
    team = "America/Los_Angeles"
    now = datetime(2026, 6, 11, 5, 0, tzinfo=ZoneInfo(team))
    slot = tm.compute_schedule_slot("EST", team, target_hour=10, now=now)
    assert slot is not None
    assert (slot.hour12, slot.minute, slot.ampm) == (8, 0, "AM"), slot
    assert slot.clamped is True


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"All {len(tests)} tests passed.")


if __name__ == "__main__":
    main()
