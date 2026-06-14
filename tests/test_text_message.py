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


def test_looks_like_sfid():
    assert tm.looks_like_sfid("0033x000038J2X4AAK")   # 18-char
    assert tm.looks_like_sfid("0033x000038J2X4")      # 15-char
    assert not tm.looks_like_sfid("5551234567")       # phone
    assert not tm.looks_like_sfid("(336) 213-2291")
    assert not tm.looks_like_sfid("")
    assert not tm.looks_like_sfid("003")              # too short


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


def test_schedule_asap_within_window():
    # Inside the window now -> send ASAP (now + lead). 3:45 PM ET, lead 10m.
    team = "America/New_York"
    now = datetime(2026, 6, 11, 15, 45, tzinfo=ZoneInfo(team))
    slot = tm.compute_schedule_slot(
        "EST", team, window_start_hour=10, window_end_hour=16, now=now)
    assert (slot.hour12, slot.minute, slot.ampm) == (3, 55, "PM"), slot
    assert slot.date_str == "06/11/2026", slot.date_str
    assert slot.day_label == "Today", slot.day_label


def test_schedule_before_window_uses_start():
    # 7:00 AM ET, window opens 10 AM -> 10:00 AM today.
    team = "America/New_York"
    now = datetime(2026, 6, 11, 7, 0, tzinfo=ZoneInfo(team))
    slot = tm.compute_schedule_slot(
        "EST", team, window_start_hour=10, window_end_hour=16, now=now)
    assert (slot.hour12, slot.minute, slot.ampm) == (10, 0, "AM"), slot
    assert slot.date_str == "06/11/2026", slot.date_str


def test_schedule_after_window_rolls_tomorrow():
    # 5:45 PM ET, past the 4 PM window end -> 10:00 AM tomorrow.
    team = "America/New_York"
    now = datetime(2026, 6, 11, 17, 45, tzinfo=ZoneInfo(team))
    slot = tm.compute_schedule_slot(
        "EST", team, window_start_hour=10, window_end_hour=16, now=now)
    assert (slot.hour12, slot.minute, slot.ampm) == (10, 0, "AM"), slot
    assert slot.date_str == "06/12/2026", slot.date_str
    assert slot.day_label == "Tomorrow", slot.day_label


def test_schedule_lead_time_can_push_past_window_end():
    # 3:55 PM ET + 10m lead = 4:05 PM, past the 4 PM end -> tomorrow 10 AM.
    team = "America/New_York"
    now = datetime(2026, 6, 11, 15, 55, tzinfo=ZoneInfo(team))
    slot = tm.compute_schedule_slot(
        "EST", team, window_start_hour=10, window_end_hour=16, now=now)
    assert (slot.hour12, slot.minute, slot.ampm) == (10, 0, "AM"), slot
    assert slot.date_str == "06/12/2026", slot.date_str


def test_schedule_west_of_team_converts():
    # Mountain student, 9 AM ET = 7 AM MDT (before window) -> 10 AM MDT today,
    # which is 12:00 PM EDT in the team's tz.
    team = "America/New_York"
    now = datetime(2026, 6, 11, 9, 0, tzinfo=ZoneInfo(team))
    slot = tm.compute_schedule_slot(
        "MST", team, window_start_hour=10, window_end_hour=16, now=now)
    assert (slot.hour12, slot.minute, slot.ampm) == (12, 0, "PM"), slot
    assert "10:00 AM" in slot.student_local_str, slot.student_local_str
    assert slot.date_str == "06/11/2026", slot.date_str
    assert slot.clamped is False


def test_schedule_unknown_tz():
    assert tm.compute_schedule_slot("ZZZ", "America/New_York") is None


def test_schedule_clamps_for_western_team():
    # Team = Pacific. Eastern student window-start 10 AM ET = 7 AM PT, before the
    # 8 AM team window -> clamp up to 8:00 AM PT.
    team = "America/Los_Angeles"
    now = datetime(2026, 6, 11, 5, 0, tzinfo=ZoneInfo(team))
    slot = tm.compute_schedule_slot(
        "EST", team, window_start_hour=10, window_end_hour=16, now=now)
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
