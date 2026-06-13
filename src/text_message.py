"""Compose + schedule student text messages through Mongoose ("Cadence").

Texting goes through the Mongoose web app (sms.mongooseresearch.com), a
separate site the app drives in its own browser context. Templates and
variables are handled HERE (not Mongoose's own templates) — we render the
final plain-text body and inject it into Mongoose's compose box.

The browser-free core (template rendering, timezone grouping, student-local ->
team-timezone schedule math) is unit-tested in tests/test_text_message.py.

The Playwright driver further down fills the Mongoose compose modal per the DOM
map in the `mongoose_texting_dom` memory. SCAFFOLD STATUS: the recipient join
(search by Mobile) and the field selectors are confirmed from live captures,
but the full click-through (step transitions, Datepicker fill, final Send /
Schedule) has NOT yet been run end-to-end against the live site. The driver
therefore defaults to commit=False: it composes + advances to the confirm /
schedule step and STOPS for the user to review and click Send/Schedule (mirrors
note_form's submit=False and the email reviewer). Set commit=True to automate
the final click once verified.

Scheduling model (see `texting_milestone_scope` memory): one Mongoose compose
sends to all its recipients at ONE absolute time, entered in the TEAM's tz. To
reach each student at a good *local* hour we batch by timezone: one scheduled
compose per zone, each at the target local hour converted to the team's tz.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from src import email_template

# WGU caseload exports a bare timezone abbreviation (EST/CST/...). Map to IANA
# zones so DST is handled correctly. NOTE: scripts/launcher.py has its own copy
# (`_TZ_ABBR_TO_IANA`) used by `student_local_time`; consolidate to this one
# when convenient.
TZ_ABBR_TO_IANA = {
    "EST": "America/New_York", "EDT": "America/New_York",
    "CST": "America/Chicago", "CDT": "America/Chicago",
    "MST": "America/Denver", "MDT": "America/Denver",
    "PST": "America/Los_Angeles", "PDT": "America/Los_Angeles",
    "AKST": "America/Anchorage", "AKDT": "America/Anchorage",
    "HST": "Pacific/Honolulu",                      # Hawaii — no DST
    "ChS": "Pacific/Guam", "ChST": "Pacific/Guam",  # Chamorro — no DST
}

# The Mongoose compose textarea caps the body (maxlength="306" in the DOM).
MAX_SMS_LEN = 306

# Default local hour to target when scheduling (10:00 AM in the student's tz).
# Overridable per fire; surfaced as a setting in the UI.
DEFAULT_TARGET_HOUR = 10
DEFAULT_TARGET_MINUTE = 0

# Mongoose only allows scheduling inside the team's working window
# (the DOM said "8:00 AM and 12:00 AM EDT"). We clamp the team-tz hour into
# [EARLIEST_TEAM_HOUR, 24).
EARLIEST_TEAM_HOUR = 8


def render_message(template_text: str, variables: dict) -> str:
    """Render {{var}} placeholders into a plain-text SMS body.

    Reuses the email template engine's plain-text path (no HTML escaping),
    so texting shares the exact variable set as email
    (first_name/preferred_name/course_code/...). Unknown placeholders are left
    in place so a typo is visible rather than silently dropped."""
    return email_template.render_plain(template_text or "", variables or {})


def over_length(body: str) -> int:
    """Characters by which `body` exceeds the Mongoose limit (0 if within)."""
    return max(0, len(body or "") - MAX_SMS_LEN)


def group_by_timezone(
    students: list[dict], *, tz_key: str = "timezone",
) -> dict[str, list[dict]]:
    """Group student rows by their timezone abbreviation.

    Returns {tz_abbr: [student, ...]}. Rows with a blank/missing tz are grouped
    under "" so the caller can surface them (we can't time-shift an unknown
    zone). Preserves input order within each group."""
    groups: dict[str, list[dict]] = {}
    for s in students:
        tz = (s.get(tz_key) or "").strip()
        groups.setdefault(tz, []).append(s)
    return groups


@dataclass
class ScheduleSlot:
    """A computed Mongoose schedule slot, expressed in the TEAM's timezone
    (the tz Mongoose's scheduler enters times in). The h12/minute/ampm/date_str
    fields map straight onto the compose-modal Date + Time controls."""
    team_dt: datetime          # tz-aware datetime in the team's tz
    date_str: str              # MM/DD/YYYY (Datepicker input)
    hour12: int                # 1-12 (vc-time-select-hours)
    minute: int                # 0-59 (vc-time-select-minutes)
    ampm: str                  # "AM" / "PM"
    student_local_str: str     # e.g. "10:00 AM MDT" — for display/confirmation
    clamped: bool = False      # True if we bumped into the allowed window


def compute_schedule_slot(
    student_tz_abbr: str,
    team_iana: str,
    *,
    target_hour: int = DEFAULT_TARGET_HOUR,
    target_minute: int = DEFAULT_TARGET_MINUTE,
    now: Optional[datetime] = None,
) -> Optional[ScheduleSlot]:
    """When to schedule so the student receives the text at
    target_hour:target_minute in THEIR local timezone.

    Returns a ScheduleSlot in the TEAM's tz (what Mongoose's scheduler expects),
    or None if the student's tz abbreviation is unknown.

    Picks the next occurrence of the target local time that is still in the
    future (today if it hasn't passed in the student's tz, else tomorrow), then
    converts that absolute instant to the team tz. Defensively clamps the
    team-tz time up to EARLIEST_TEAM_HOUR if it would land before the allowed
    window (shouldn't happen for an Eastern team, but matters if the team is
    further west)."""
    iana = TZ_ABBR_TO_IANA.get((student_tz_abbr or "").strip())
    if not iana:
        return None
    student_tz = ZoneInfo(iana)
    team_tz = ZoneInfo(team_iana)

    if now is None:
        now = datetime.now(team_tz)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=team_tz)

    now_student = now.astimezone(student_tz)
    target = now_student.replace(
        hour=target_hour, minute=target_minute, second=0, microsecond=0,
    )
    if target <= now_student:
        target = target + timedelta(days=1)

    team_dt = target.astimezone(team_tz)
    clamped = False
    if team_dt.hour < EARLIEST_TEAM_HOUR:
        team_dt = team_dt.replace(
            hour=EARLIEST_TEAM_HOUR, minute=0, second=0, microsecond=0,
        )
        clamped = True

    return ScheduleSlot(
        team_dt=team_dt,
        date_str=team_dt.strftime("%m/%d/%Y"),
        hour12=int(team_dt.strftime("%I")),
        minute=team_dt.minute,
        ampm=team_dt.strftime("%p"),
        student_local_str=target.strftime("%I:%M %p %Z").lstrip("0"),
        clamped=clamped,
    )


def team_iana_from_abbr(tz_abbr: str) -> Optional[str]:
    """Resolve the team's tz abbreviation (read from Mongoose's
    `.timezone-label`, e.g. 'EDT') to an IANA zone for the schedule math."""
    return TZ_ABBR_TO_IANA.get((tz_abbr or "").strip())


_NON_DIGITS = re.compile(r"\D+")


def normalize_phone(raw: str) -> str:
    """Strip a phone number to bare digits, dropping a leading US country code.
    Mongoose accepts both '5551234567' and '(336) 213-2291'; we normalize to 10
    digits for a consistent search term. Returns '' if there aren't 10 digits."""
    digits = _NON_DIGITS.sub("", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else ""


# ---------------------------------------------------------------------------
# Playwright driver for the Mongoose compose modal.
#
# Selectors come from the live captures (see `mongoose_texting_dom` memory).
# Confirmed from the DOM: the Compose button, inbox-select, the recipient
# autocomplete (search by Mobile -> v-list-item options), the message textarea,
# the Send Now / Schedule buttons, and the schedule Date/Time controls.
# NOT yet run end-to-end live: exact step transitions, Datepicker fill, the
# final Send/Schedule click. Hence commit=False by default.
# ---------------------------------------------------------------------------

from playwright.sync_api import Page  # noqa: E402
from playwright.sync_api import TimeoutError as PWTimeout  # noqa: E402


@dataclass
class TextMessage:
    body: str                                   # final plain text (already rendered)
    recipients_mobile: list[str] = field(default_factory=list)  # raw or normalized
    inbox_label: str = ""                       # e.g. "C769 Inbox"; "" = pick first/only
    schedule: Optional[ScheduleSlot] = None     # None = Send Now path
    schedule_name: str = ""                     # Mongoose "Message Name" on the schedule step
    commit: bool = False                        # False = stop at confirm/schedule for review


def _noop(_msg: str) -> None:
    pass


def _click_button(page: Page, name: str, *, timeout_ms: int = 10_000) -> None:
    """Click a Vuetify button by its visible text/accessible name."""
    page.get_by_role("button", name=name, exact=True).filter(
        visible=True
    ).first.click(timeout=timeout_ms)


def open_compose(page: Page, *, timeout_ms: int = 15_000) -> None:
    """Click Compose and wait for the compose modal to appear."""
    _click_button(page, "Compose", timeout_ms=timeout_ms)
    page.locator(".compose-modal").filter(visible=True).first.wait_for(
        state="visible", timeout=timeout_ms
    )


def select_inbox(page: Page, inbox_label: str, *, timeout_ms: int = 6_000) -> None:
    """On the "Select Inbox" step, click the inbox whose label matches
    `inbox_label`. If the step is shown but NO inbox matches, raise (so the
    caller fails loudly instead of silently sending from the wrong inbox) and
    list what's available — usually means Mongoose is on the wrong department.
    No label → click the first/only inbox. No-op if the step isn't shown
    (inbox already selected)."""
    items = page.locator(".inbox-select").filter(visible=True)
    try:
        items.first.wait_for(state="visible", timeout=timeout_ms)
    except PWTimeout:
        return  # step not shown — inbox already selected
    if not inbox_label:
        items.first.click()
        return
    match = items.filter(has_text=inbox_label)
    if match.count() == 0:
        avail = []
        try:
            for i in range(min(items.count(), 10)):
                t = (items.nth(i).inner_text() or "").strip().replace("\n", " ")
                if t:
                    avail.append(t)
        except Exception:
            pass
        raise RuntimeError(
            f"Inbox {inbox_label!r} isn't available in Mongoose right now. "
            f"Available: {', '.join(avail) or '(none)'}. Switch Mongoose to the "
            "matching department, or fix the action's inbox name.")
    match.first.click()


def add_recipient(page: Page, mobile: str, *, timeout_ms: int = 10_000) -> bool:
    """Type a (normalized) mobile number into the recipient search and click the
    first matching result. Returns True if a result was added. The caller should
    have normalized the number; a full 10-digit search should yield one match."""
    term = normalize_phone(mobile) or (mobile or "").strip()
    if not term:
        return False
    box = page.get_by_placeholder("Search by First, Last, ID, or Mobile")
    if box.count() == 0:
        box = page.get_by_label("Search by First, Last, ID, or Mobile")
    box = box.filter(visible=True).first
    box.click()
    box.fill(term)
    # Results render in an overlay listbox of .v-list-item options.
    option = page.locator(
        '.v-overlay-container .v-list-item[role="option"], '
        '.v-autocomplete__content .v-list-item'
    ).filter(visible=True)
    try:
        option.first.wait_for(state="visible", timeout=timeout_ms)
    except PWTimeout:
        return False
    option.first.click()
    return True


def set_message(page: Page, body: str) -> None:
    """Fill the compose message textarea with the rendered body."""
    ta = page.locator('textarea[aria-label="compose-message textarea"]').filter(
        visible=True
    ).first
    ta.wait_for(state="visible", timeout=10_000)
    ta.fill(body or "")


def fill_schedule(page: Page, slot: ScheduleSlot) -> None:
    """Fill the Schedule step's Date + Time controls from a ScheduleSlot
    (already expressed in the team's tz)."""
    # Time first, while the date picker's calendar is closed (entering the date
    # opens an overlay that can cover the time controls).
    # Three native <select>s — hours (1-12), minutes (00-59), AM/PM.
    page.locator("select.vc-time-select-hours").first.select_option(
        label=str(slot.hour12)
    )
    page.locator("select.vc-time-select-minutes").first.select_option(
        label=f"{slot.minute:02d}"
    )
    # AM/PM select carries option values "true" (AM) / "false" (PM).
    page.locator('select:has(option[value="true"])').first.select_option(
        value=("true" if slot.ampm.upper() == "AM" else "false")
    )

    # Date last. @vuepic/vue-datepicker accepts typed MM/DD/YYYY but only
    # *commits* it on Enter — a bare fill() sets the text without parsing it and
    # leaves the calendar overlay open (covering the Schedule button -> "not
    # visible / not stable"). So: focus, clear, type, Enter to commit + close.
    date_input = page.locator(
        'input.dp__input[aria-label="Datepicker input"]'
    ).filter(visible=True).first
    date_input.wait_for(state="visible", timeout=10_000)
    date_input.click()
    date_input.fill("")
    date_input.press_sequentially(slot.date_str, delay=20)
    date_input.press("Enter")
    # Let the overlay close + the form revalidate (enables the Schedule button).
    page.wait_for_timeout(300)


def send_text(
    page: Page, msg: TextMessage, *, on_status: Optional[Callable[[str], None]] = None,
) -> bool:
    """Drive the Mongoose compose modal: open -> select inbox -> add
    recipient(s) -> set message -> advance to confirm -> Send Now or fill
    Schedule. Stops before the final commit click unless msg.commit is True.

    Returns True if it reached the confirm/schedule step without error (whether
    or not the final click was made). Raises on a hard driver failure."""
    say = on_status or _noop
    open_compose(page)
    select_inbox(page, msg.inbox_label)

    added = 0
    for raw in msg.recipients_mobile:
        if add_recipient(page, raw):
            added += 1
        else:
            say(f"  text: no Mongoose match for {raw!r} — skipped")
    if added == 0:
        raise RuntimeError("No recipients could be added to the text.")
    say(f"  text: {added} recipient(s) added")

    set_message(page, msg.body)

    # Advance from compose to the confirm step.
    _click_button(page, "Preview")

    if msg.schedule is None:
        say("  text: composed — review and click Send Now" if not msg.commit
            else "  text: sending now…")
        if msg.commit:
            _click_button(page, "Send Now")
        return True

    # Schedule path: open the schedule step, name it, fill date/time.
    _click_button(page, "Schedule")
    if msg.schedule_name:
        try:
            name_input = page.locator(
                "input#schedule-message-name-input"
            ).filter(visible=True).first
            name_input.wait_for(state="visible", timeout=8_000)
            name_input.fill(msg.schedule_name)
        except PWTimeout:
            pass  # name may be optional; continue
    fill_schedule(page, msg.schedule)
    say(f"  text: scheduled for {msg.schedule.student_local_str} "
        f"(student-local) — review and click Schedule" if not msg.commit
        else f"  text: scheduling for {msg.schedule.student_local_str}…")
    if msg.commit:
        _click_button(page, "Schedule")
    return True
