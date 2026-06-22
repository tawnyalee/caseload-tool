"""Fill a single visible Student Note panel.

Phase 1 contract: the user has already navigated to the student record and
opened the note panel. This module finds the *visible* note form and fills
the supplied fields. It does NOT click Submit by default — the user
reviews and submits manually until we're confident in the selectors.
"""
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

from src import selectors
from src.config import SCREENSHOTS_DIR

InteractionFormat = Literal["Single Interaction", "Multiple Interactions"]


@dataclass
class NoteData:
    interaction_format: InteractionFormat = "Single Interaction"
    interaction_type: str = ""             # exact <option> text in noteType select; "" = skip
    course_code: str = ""                  # text into Enter Course Code; "" = skip (set at runtime)
    course_code_override: str = ""         # persisted; overrides auto-detect for this note only
    subject: str = ""                      # text into Subject; "" = skip (form will reject)
    academic_activities: list[str] = field(default_factory=list)  # checkbox labels to tick
    body: str = ""                         # rich-text body; "" = skip
    submit: bool = False                   # leave False while we dial in selectors
    append_clipboard: bool = False         # paste clipboard text after body at fire time
    enter_additional_text: bool = False    # "edit note at fire time": body/course/activities/EA dialog


def _screenshot_failure(page: Page, tag: str) -> Path:
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    out = SCREENSHOTS_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-{tag}.png"
    page.screenshot(path=str(out), full_page=True)
    return out


def wait_for_submit_enabled(page: Page, timeout_ms: int = 4_000) -> bool:
    """Poll until the visible Submit button is enabled, up to `timeout_ms`.

    Lightning re-enables Submit *reactively*, a beat AFTER the last required
    field is set — e.g. ticking an Academic Activity for "Email from Student".
    Checking `is_enabled()` the instant our fill returns can therefore read a
    stale "disabled" even though the form is actually valid (an intermittent
    failure, more likely on a slow/right-click fire). Polling absorbs that lag.

    Returns True if Submit becomes enabled within the window, else False."""
    deadline = time.monotonic() + timeout_ms / 1000.0
    btn = selectors.submit_button(page)
    while time.monotonic() < deadline:
        try:
            if btn.is_enabled():
                return True
        except Exception:
            pass
        page.wait_for_timeout(150)
    try:
        return btn.is_enabled()
    except Exception:
        return False


def wait_for_submit_complete(page: Page, timeout_ms: int = 15_000) -> None:
    """After clicking Submit, wait until Salesforce has settled the
    submission. Two equally-valid signals: Submit becomes hidden (panel
    closed) or Submit becomes disabled (form cleared, required fields
    empty again). Either means the click was processed."""
    deadline = time.monotonic() + timeout_ms / 1000.0
    submit_role = page.get_by_role("button", name="Submit", exact=True)
    while time.monotonic() < deadline:
        try:
            visible = submit_role.filter(visible=True)
            if visible.count() == 0:
                return
            if not visible.first.is_enabled():
                return
        except Exception:
            return
        page.wait_for_timeout(150)


def close_workspace_tab(page: Page) -> None:
    """Close the current Salesforce Lightning workspace tab via Shift+X."""
    page.keyboard.press("Shift+X")


def _note_form_diag(page) -> str:
    """On a Submit-disabled failure, dump the live note-form state so we can see
    WHY: duplicate forms (deep-link standalone records can render more than one
    note component), the selected type, and whether each Academic Activity box
    is actually checked. Cheap; runs only on failure."""
    try:
        d = page.evaluate(
            """() => {
              const vis = el => { const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0; };
              const sels = [...document.querySelectorAll('select[name=\"noteType\"]')];
              const submits = [...document.querySelectorAll('button')]
                .filter(b => (b.textContent || '').trim() === 'Submit');
              const acts = [...document.querySelectorAll('label[for]')]
                .map(l => {
                  const inp = document.getElementById(l.getAttribute('for'));
                  return { t: (l.textContent || '').trim().slice(0, 36),
                           checked: inp ? !!inp.checked : null,
                           vis: vis(l) }; })
                .filter(x => /Course\\/Program|Academic Goal|obstacle|Learning Occurred/i.test(x.t));
              return {
                noteTypeSelects: sels.map(s => ({ val: s.value,
                  disabled: s.disabled, vis: vis(s) })),
                submits: submits.map(b => ({ disabled: b.disabled, vis: vis(b) })),
                activities: acts,
              };
            }"""
        )
        return f"form-diag: {d}"
    except Exception as e:
        return f"form-diag failed: {e}"


def _activity_is_checked(page, label) -> bool:
    """True if the Academic Activity checkbox for `label` is currently ticked
    (read via the label's linked input). False if unknown/unchecked."""
    try:
        cb = selectors.academic_activity_checkbox(page, label)
        forid = cb.get_attribute("for")
        if not forid:
            return False
        # Attribute selector (not '#id') — Salesforce ids contain ':' which
        # breaks CSS id syntax.
        return page.locator(f'[id="{forid}"]').is_checked()
    except Exception:
        return False


def _ensure_activity_checked(page, label, attempts: int = 5) -> bool:
    """Click an Academic Activity checkbox and CONFIRM it ticked, re-clicking if
    the click was lost to a Lightning re-render. Only ever clicks a box that's
    confirmed unchecked, so it never toggles an already-ticked one back off.
    Returns True once checked. This is the gate that enables Submit for
    'Email from Student'-type notes."""
    for _ in range(attempts):
        if _activity_is_checked(page, label):
            return True
        _safe_click(selectors.academic_activity_checkbox(page, label))
        try:
            page.wait_for_timeout(300)
        except Exception:
            break
    return _activity_is_checked(page, label)


def _wait_enabled(locator, timeout_ms: int = 8_000) -> bool:
    """Poll until `locator` is enabled (no `disabled` attr), up to timeout_ms.
    A freshly opened record can render the note form's controls disabled for a
    beat — interacting then either silently fails or times out. Returns True if
    it became enabled."""
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        try:
            if locator.is_enabled():
                return True
        except Exception:
            pass
        try:
            locator.page.wait_for_timeout(150)
        except Exception:
            break
    try:
        return locator.is_enabled()
    except Exception:
        return False


def _safe_click(locator) -> None:
    """Click a Lightning form element robustly. The note panel often
    sits in a slide-out sidebar that's wider than the viewport; the
    radio buttons / checkboxes / submit can be "visible in the DOM"
    but outside the click coordinate area Playwright requires. Three-
    step fallback:
      1. JS scrollIntoView (handles horizontal + vertical scroll)
      2. Playwright click(force=True) — proper event propagation,
         triggers Lightning's bindings reliably
      3. JS fallback: focus + click + dispatch `change` on the
         linked input. Pure `el.click()` alone is NOT enough for
         Lightning — it bypasses viewport but synthetic events
         don't always fire onChange on the underlying input.
         The focus() also makes sure subsequent `keyboard.type()`
         lands inside the element."""
    try:
        locator.evaluate(
            "el => el.scrollIntoView({block: 'center', inline: 'center'})"
        )
    except Exception:
        pass
    try:
        locator.click(force=True)
        return
    except Exception:
        pass
    locator.evaluate("""
      el => {
        if (typeof el.focus === 'function') el.focus();
        el.click();
        // For radio / checkbox labels, the underlying input needs a
        // `change` event for Lightning's reactive binding to pick up.
        // Native HTML fires this on a real mouse click; JS .click()
        // on the label sometimes doesn't.
        const forId = el.getAttribute && el.getAttribute('for');
        if (forId) {
          const input = document.getElementById(forId);
          if (input) {
            input.dispatchEvent(new Event('change', {bubbles: true}));
            input.dispatchEvent(new Event('input', {bubbles: true}));
          }
        }
      }
    """)


def fill_note(page: Page, data: NoteData, *, timeout_ms: int = 10_000) -> None:
    try:
        # Wait for the note panel to be present and visible. The Submit
        # button is a good anchor because every panel has one.
        selectors.submit_button(page).wait_for(state="visible", timeout=timeout_ms)

        # Lightning Web Components: clicking the underlying <input> doesn't
        # trigger the reactive state update; the label is the real click
        # target. selectors return the <label> element; .click() is enough
        # (no .check() — that would try to verify the input's `checked`
        # attribute, which Lightning re-renders away).
        if data.interaction_format:
            _safe_click(
                selectors.interaction_format_radio(page, data.interaction_format)
            )

        if data.interaction_type:
            sel = selectors.interaction_type_select(page)
            # On a freshly opened record (especially a fast deep-link nav) the
            # type <select> can render disabled for a beat — wait for it to
            # enable so we don't select into a dead control or time out (30s)
            # on a still-initializing form.
            _wait_enabled(sel, timeout_ms=12_000)
            sel.select_option(label=data.interaction_type, timeout=12_000)

        if data.course_code:
            cc = selectors.course_code_input(page)
            _safe_click(cc)
            cc.fill(data.course_code)

        if data.subject:
            subj = selectors.subject_input(page)
            _safe_click(subj)
            subj.fill(data.subject)

        # Academic activities: selecting a type like "Email from Student"
        # REACTIVELY adds this section. Clicking before it has rendered +
        # settled loses the tick — Submit then stays disabled ("required field
        # missing"). On a COLD first fire after launch the section can fail to
        # render at all (the type's change event doesn't reach the still-
        # initializing LWC), so a plain wait times out with no checkboxes
        # ('activities: []'). Wait for the section; if it never appears,
        # RE-SELECT the type to re-fire the reactive render, then wait again.
        if data.academic_activities:
            first_cb = selectors.academic_activity_checkbox(
                page, data.academic_activities[0])

            def _activities_present(timeout_ms: int) -> bool:
                try:
                    first_cb.wait_for(state="visible", timeout=timeout_ms)
                    return True
                except Exception:
                    return False

            if not _activities_present(6_000) and data.interaction_type:
                # The section is reactively added when the note TYPE changes. On
                # a cold first fire the initial select can stick in the DOM
                # WITHOUT the component re-rendering (its change handler isn't
                # wired / early-returns on an unchanged value), so re-selecting
                # the SAME value does nothing — 'activities: []'. Force a real
                # change: switch to a different option, then back to the target.
                # RETRY this toggle a few times — on a slow/cold render one pass
                # isn't always enough (an "Email from Student" note then fails
                # with Submit gated and activities empty; works on re-fire).
                try:
                    sel = selectors.interaction_type_select(page)
                except Exception:
                    sel = None
                for _ in range(3):
                    if sel is None:
                        break
                    try:
                        _wait_enabled(sel, timeout_ms=4_000)
                        others = sel.evaluate(
                            """el => Array.from(el.options)
                                   .map(o => (o.label || o.textContent || '').trim())
                                   .filter(t => t)""")
                        other = next(
                            (t for t in (others or [])
                             if t != data.interaction_type), None)
                        if other:
                            sel.select_option(label=other, timeout=8_000)
                            page.wait_for_timeout(450)
                        sel.select_option(
                            label=data.interaction_type, timeout=8_000)
                    except Exception:
                        pass
                    if _activities_present(6_000):
                        break

            page.wait_for_timeout(400)  # let the reactive re-render finish
            for label in data.academic_activities:
                # Click + verify ticked (re-click if a re-render dropped it) —
                # a lost tick leaves Submit gated. See _ensure_activity_checked.
                if not _ensure_activity_checked(page, label):
                    raise RuntimeError(
                        f"Couldn't tick the Academic Activity {label!r} "
                        "(required to enable Submit for this note type). "
                        + _note_form_diag(page))

        if data.body:
            editor = selectors.note_body_editor(page)
            _safe_click(editor)
            # Explicitly focus the editor BEFORE typing. _safe_click's
            # JS fallback already calls focus(), but Playwright's
            # force-click path doesn't necessarily focus a
            # contenteditable div, and keyboard.type() lands wherever
            # the page is focused — not necessarily the editor.
            try:
                editor.evaluate("el => el.focus()")
            except Exception:
                pass
            # Enter the body fast: insert_text() drops the whole line in a
            # single input event (vs. type()'s per-character keystrokes,
            # which made long clipboard-appended notes crawl). Newlines are
            # still real Enter presses so the Quill editor splits paragraphs
            # correctly. insert_text dispatches a proper input event, so
            # Quill registers the text just like a paste.
            for i, line in enumerate(data.body.splitlines() or [data.body]):
                if i > 0:
                    page.keyboard.press("Enter")
                if line:
                    page.keyboard.insert_text(line)

        if data.submit:
            # Poll instead of checking once: Lightning re-enables Submit a beat
            # after the last required field is set, so an immediate read can
            # spuriously see "disabled" on a valid form (intermittent failure).
            if not wait_for_submit_enabled(page):
                raise RuntimeError(
                    "Submit button is disabled — a required field is missing "
                    "(check Interaction Type, Subject/Course Code, body, and "
                    "any Academic Activity gates). " + _note_form_diag(page)
                )
            _safe_click(selectors.submit_button(page))
            wait_for_submit_complete(page)

    except PlaywrightTimeoutError as e:
        path = _screenshot_failure(page, "timeout")
        raise RuntimeError(
            f"Timed out filling note. Screenshot: {path}. Underlying: {e}"
        ) from e
    except Exception as e:
        path = _screenshot_failure(page, "error")
        raise RuntimeError(
            f"Error filling note. Screenshot: {path}. Underlying: {e}"
        ) from e
