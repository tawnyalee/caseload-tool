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
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
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

# Fallback timezone for students with a blank/unrecognized Timezone — schedule
# them as Mountain rather than skipping (user's call: MT is the safe default).
DEFAULT_TZ_ABBR = "MST"


def effective_tz(tz_abbr: str) -> str:
    """The student's tz abbreviation if recognized, else the MT default — so a
    student with no/unknown Timezone still gets scheduled (treated as Mountain)."""
    tz = (tz_abbr or "").strip()
    return tz if tz in TZ_ABBR_TO_IANA else DEFAULT_TZ_ABBR


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


_SFID_RE = re.compile(r"^003[0-9A-Za-z]{12}([0-9A-Za-z]{3})?$")


def looks_like_sfid(s: str) -> bool:
    """True if `s` is a Salesforce Contact id (15- or 18-char, '003' prefix).
    Mongoose's recipient search accepts the Contact id as a search term and
    returns the unique contact — blank-mobile-proof, unlike a phone search."""
    return bool(_SFID_RE.match((s or "").strip()))


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
    course: str = ""                            # auto-switch Mongoose to this dept first
    commit: bool = False                        # False = stop at confirm/schedule for review


def _noop(_msg: str) -> None:
    pass


def current_department(page: Page) -> str:
    """The Mongoose department currently selected in the sidebar (e.g. 'C769'),
    or '' if it can't be read."""
    try:
        el = page.locator(".department-name").filter(visible=True).first
        if el.count() == 0:
            return ""
        return (el.inner_text() or "").strip()
    except Exception:
        return ""


def switch_department(page: Page, course: str, *, timeout_ms: int = 10_000) -> None:
    """Make Mongoose's selected department match `course` (e.g. 'C769') so the
    Compose modal offers that course's inbox. No-op if already on it. Raises if
    the department isn't in the team list. Driven via Playwright, which also
    wakes a backgrounded/frozen renderer (where a manual click wouldn't)."""
    course = (course or "").strip()
    if not course:
        return
    try:
        page.bring_to_front()
    except Exception:
        pass
    if current_department(page).lower() == course.lower():
        return
    # Open the department switcher (the sidebar box showing the current dept).
    trigger = page.locator(
        ".department-name.department-dropdown").filter(visible=True).first
    trigger.wait_for(state="visible", timeout=timeout_ms)
    trigger.click()
    # Pick the team whose aria-label starts with the course code
    # (e.g. "C769, 1 unread message" / "C964, Current team" / "D502").
    item = page.locator(
        f'.team-list .v-list-item[aria-label^="{course}"]'
    ).filter(visible=True).first
    try:
        item.wait_for(state="visible", timeout=timeout_ms)
    except PWTimeout:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        raise RuntimeError(
            f"Mongoose has no {course!r} department/team to switch to.")
    item.click()
    # Wait for the switch to take effect (the sidebar dept updates), then let
    # the new department's inbox page render before we open Compose — opening
    # mid-transition leaves the modal stuck before the recipient step.
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        if current_department(page).lower() == course.lower():
            page.wait_for_timeout(1200)
            return
        page.wait_for_timeout(200)
    # Not confirmed in time — proceed; select_inbox will fail loudly if wrong.


def _click_button(page: Page, name: str, *, timeout_ms: int = 10_000) -> None:
    """Click a Vuetify button by its visible text/accessible name."""
    page.get_by_role("button", name=name, exact=True).filter(
        visible=True
    ).first.click(timeout=timeout_ms)


def close_compose(page: Page, *, timeout_ms: int = 5_000) -> None:
    """Close the compose modal if one is open. A left-open modal (e.g. from a
    failed previous group) makes the Compose button `inert` and its overlay
    intercepts clicks — so we reset to a clean slate before each compose."""
    overlay = page.locator(".compose-modal-overlay").filter(visible=True)
    try:
        if overlay.count() == 0:
            return
    except Exception:
        return
    try:
        btn = page.get_by_role(
            "button", name="Close Compose Modal").filter(visible=True)
        if btn.count() > 0:
            btn.first.click(timeout=timeout_ms)
        else:
            page.keyboard.press("Escape")
    except Exception:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
    try:
        overlay.first.wait_for(state="hidden", timeout=timeout_ms)
    except Exception:
        pass


def open_compose(page: Page, *, timeout_ms: int = 15_000) -> None:
    """Click Compose and wait for the compose modal to appear. Resets any
    leftover modal first so the (otherwise inert) Compose button is clickable."""
    close_compose(page)
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


def _recipient_box(page: Page):
    """Locator for the compose recipient search input. Find it by its container
    (the Send-to-Contacts combobox), NOT by placeholder/label: once a recipient
    chip is added the floating label changes, so the placeholder/label locator
    stops matching and adding a 2nd+ recipient would hang. The bare text input
    inside the field persists across chips."""
    for sel in (
        ".send-to-contacts input[type='text']",
        ".send-to-contacts .v-field__input input",
        ".v-autocomplete input[type='text']",
    ):
        loc = page.locator(sel).filter(visible=True)
        if loc.count() > 0:
            return loc.first
    # Last resort: the original placeholder/label approach (first recipient).
    box = page.get_by_placeholder("Search by First, Last, ID, or Mobile")
    if box.count() == 0:
        box = page.get_by_label("Search by First, Last, ID, or Mobile")
    return box.filter(visible=True).first


def warm_up_compose(page: Page) -> None:
    """One throwaway open → (select first inbox) → type a dummy search → close,
    to wake the renderer. The FIRST compose of a session is unreliable (the
    recipient step / autocomplete don't engage on a cold/backgrounded tab); a
    Playwright-driven dry run warms it so the first REAL group goes through.
    Fully best-effort — all errors are swallowed."""
    try:
        open_compose(page)
        try:
            select_inbox(page, "")            # first/only inbox, no match needed
            box = _recipient_box(page)
            box.wait_for(state="visible", timeout=8_000)
            box.click()
            box.press_sequentially("0000", delay=15)  # fire the autocomplete once
            page.wait_for_timeout(600)
        except Exception:
            pass
    except Exception:
        pass
    finally:
        try:
            close_compose(page)
        except Exception:
            pass


def open_compose_to_recipient_step(
    page: Page, inbox_label: str, say: Callable[[str], None], *, attempts: int = 2,
) -> None:
    """Open Compose, select the inbox, and wait until the recipient search box
    is visible. The FIRST compose of a session often doesn't engage (the
    renderer needs a warm-up action), so retry once after closing the modal —
    the retry reliably reaches the recipient step."""
    last_err = None
    for attempt in range(attempts):
        try:
            open_compose(page)              # closes any leftover modal first
            select_inbox(page, inbox_label)
            _recipient_box(page).wait_for(state="visible", timeout=12_000)
            return
        except Exception as e:
            last_err = e
            if attempt + 1 < attempts:
                say("  text: compose didn't engage; retrying…")
                close_compose(page)
                page.wait_for_timeout(700)
    raise RuntimeError(
        "compose modal didn't reach the recipient step after "
        f"{attempts} tries ({last_err}).")


def add_recipient(page: Page, recipient: str, *, timeout_ms: int = 10_000) -> bool:
    """Type a recipient search term — a Salesforce Contact id (003…) OR a mobile
    number — into the recipient search and click the matching result. Returns
    True if a result was added. A Contact id yields exactly one match; a full
    10-digit mobile usually does too."""
    recipient = (recipient or "").strip()
    if looks_like_sfid(recipient):
        term = recipient  # search Mongoose by the unique Contact id verbatim
    else:
        term = normalize_phone(recipient) or recipient
    if not term:
        return False
    box = _recipient_box(page)
    box.click()
    box.fill("")
    box.press_sequentially(term, delay=15)  # per-keystroke so the search fires
    # Results render in an overlay listbox of .v-list-item options.
    option = page.locator(
        '.v-overlay-container .v-list-item[role="option"], '
        '.v-autocomplete__content .v-list-item'
    ).filter(visible=True)
    try:
        option.first.wait_for(state="visible", timeout=timeout_ms)
    except PWTimeout:
        return False
    # Duplicate contacts can share a name/number; prefer the one "Assigned to
    # You" (the user's own student) over an arbitrary first match.
    assigned = option.filter(has_text="Assigned to")
    target = assigned.first if assigned.count() > 0 else option.first
    target.click()
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
    # Make sure Mongoose is on the right department first (Compose only offers
    # the current department's inbox). Playwright-driven, so it also wakes a
    # frozen/backgrounded renderer.
    if msg.course:
        switch_department(page, msg.course)
    # Open Compose + select the inbox, retrying once if it doesn't reach the
    # recipient step (the first compose of a session is flaky — warm-up).
    open_compose_to_recipient_step(page, msg.inbox_label, say)

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


# ---------------------------------------------------------------------------
# Segment CSV export — auto-download the per-department contacts list (with
# Salesforce Contact ids) the launcher joins to the caseload.
# ---------------------------------------------------------------------------
SEGMENTS_URL = "https://sms.mongooseresearch.com/segments"
# Mongoose maintains an "Auto-Update" segment per department named like this
# (confirmed: "all C769 students"). The export of it carries every contact's
# Contact id. Override per-deployment if a team renames theirs.
SEGMENT_NAME_TEMPLATE = "all {course} students"


def segment_name_for(course: str, template: str = SEGMENT_NAME_TEMPLATE) -> str:
    """The expected segment name for a course (e.g. 'all C769 students')."""
    return template.format(course=(course or "").strip())


def list_segment_names(page: Page) -> list[str]:
    """Visible segment names on the Segments list page (the row links)."""
    try:
        return [t.strip() for t in
                page.locator('a[href*="/segments/"]').all_inner_texts()
                if t.strip()]
    except Exception:
        return []


def export_segment_csv(
    page: Page, course: str, dest_path, *, segment_name: str = "",
    on_status: Optional[Callable[[str], None]] = None, timeout_ms: int = 30_000,
) -> Path:
    """Drive Mongoose to export a department's contacts segment to a CSV.

    Switches to `course`, opens the Segments list, opens the named segment's ⋯
    menu, clicks Export, and saves the (Playwright-intercepted) download to
    `dest_path`. `segment_name` defaults to 'all {course} students'. Returns the
    saved Path. Raises a clear error — including the segment names that ARE
    present — if the expected segment isn't found, so the user knows to create
    or rename one (filter: contact id is not empty)."""
    say = on_status or _noop
    name = segment_name or segment_name_for(course)
    if course:
        switch_department(page, course)
    page.goto(SEGMENTS_URL, wait_until="domcontentloaded")
    # Per-row ⋯ button: aria-label is "<segment name> menu".
    menu_btn = page.locator(
        f'button[aria-label="{name} menu"]').filter(visible=True).first
    try:
        menu_btn.wait_for(state="visible", timeout=timeout_ms)
    except PWTimeout:
        avail = list_segment_names(page)
        raise RuntimeError(
            f"No Mongoose segment named {name!r} for {course or 'this dept'}. "
            f"Segments present: {avail or '(none)'}. Create an Auto-Update "
            "segment with that name (filter: contact id is not empty).")
    say(f"  segment: opening {name!r} menu…")
    menu_btn.click()
    # The dropdown is a v-list overlay; click its Export item while capturing
    # the download (Playwright intercepts it — there's no visible browser UI).
    export_item = page.locator(
        '.v-overlay-container .v-list-item, [role="menuitem"]'
    ).filter(visible=True).filter(has_text="Export").first
    export_item.wait_for(state="visible", timeout=timeout_ms)
    say(f"  segment: exporting {name!r}…")
    with page.expect_download(timeout=timeout_ms) as dl:
        export_item.click()
    dest = Path(dest_path)
    dl.value.save_as(str(dest))
    say(f"  segment: saved {dest.name}")
    return dest
