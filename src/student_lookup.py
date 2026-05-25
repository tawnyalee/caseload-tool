"""Read student context from the Salesforce / Caseload DOM.

The visible note panel's header tells us WHICH student we're working on.
The Caseload table (a separate Lightning tab — still in DOM as long as it
was opened this session) tells us what course code to file the note
under, the student's ID, and the email address from the row's
'Email Student' action link.
"""
import difflib
import re
from typing import Optional
from urllib.parse import parse_qs, unquote, urlparse

from playwright.sync_api import Page

NOTE_HEADER_PREFIX = "Create a New Student Note for "
COURSE_CODE_RE = re.compile(r"\s*([A-Z]\d{3})")


def _parse_mailto(href: str) -> tuple[str, str]:
    """Extract (primary, cc) addresses from a mailto: href. Returns
    empty strings for missing fields. Handles URL encoding."""
    try:
        parsed = urlparse(href)
        primary = unquote(parsed.path).strip()
        cc_list = parse_qs(parsed.query).get("cc", [])
        cc = unquote(cc_list[0]).strip() if cc_list else ""
        return primary, cc
    except Exception:
        return "", ""


def _extract_wgu_email(page: Page) -> str:
    """Read the student's email from a visible 'WGU Email' lightning
    output field on the contact view. Salesforce's 'Email Student'
    action link in the Caseload row mails the PM only — student email
    isn't in that link — so we have to find it elsewhere when the
    contact card is on screen."""
    try:
        fields = (
            page.locator("lightning-output-field")
            .filter(has_text="WGU Email")
            .filter(visible=True)
        )
        if fields.count() == 0:
            return ""
        mailtos = fields.first.locator('a[href^="mailto:"]')
        if mailtos.count() == 0:
            return ""
        href = mailtos.first.get_attribute("href") or ""
        addr, _ = _parse_mailto(href)
        if "@" in addr:
            return addr
    except Exception:
        pass
    return ""


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


def lookup_caseload_student(page: Page, student_name: str) -> dict:
    """Look up student context from any Caseload-style table in DOM.

    Returns a dict with keys: 'course_code', 'student_id',
    'student_email', 'pm_name', 'pm_email'. The two emails come from
    the row's 'Email Student' action link, whose mailto: href has the
    PM as the primary recipient and the student as CC. PM name comes
    from the 'Program Mentor' column. Missing fields come back as
    empty strings — empty CSV cells rather than a hard failure.
    """
    out = {
        "course_code": "", "student_id": "",
        "student_email": "", "pm_name": "", "pm_email": "",
    }
    tables = page.locator("table").filter(
        has=page.locator("th", has_text="Course Code")
    )
    for i in range(tables.count()):
        table = tables.nth(i)
        headers = table.locator("th").all_text_contents()
        col_idx: dict[str, int] = {}
        for j, h in enumerate(headers):
            if "Course Code" in h and "course_code" not in col_idx:
                col_idx["course_code"] = j
            if "Student ID" in h and "student_id" not in col_idx:
                col_idx["student_id"] = j
            if "Program Mentor" in h and "pm_name" not in col_idx:
                col_idx["pm_name"] = j
        rows = table.locator("tr").filter(has_text=student_name)
        for r in range(rows.count()):
            row = rows.nth(r)
            cells = row.locator("td").all_text_contents()

            if "course_code" in col_idx and col_idx["course_code"] < len(cells):
                m = COURSE_CODE_RE.match(cells[col_idx["course_code"]])
                if m and not out["course_code"]:
                    out["course_code"] = m.group(1)

            if "student_id" in col_idx and col_idx["student_id"] < len(cells):
                sid = cells[col_idx["student_id"]].strip()
                if sid and not out["student_id"]:
                    out["student_id"] = sid

            if "pm_name" in col_idx and col_idx["pm_name"] < len(cells):
                name = cells[col_idx["pm_name"]].strip()
                if name and not out["pm_name"]:
                    out["pm_name"] = name

            # The 'Email Student' link's mailto: has PM as primary, student as CC.
            if not out["pm_email"] or not out["student_email"]:
                try:
                    mailtos = row.locator('a[href^="mailto:"]')
                    if mailtos.count() > 0:
                        href = mailtos.first.get_attribute("href") or ""
                        primary, cc = _parse_mailto(href)
                        if "@" in primary and not out["pm_email"]:
                            out["pm_email"] = primary
                        if "@" in cc and not out["student_email"]:
                            out["student_email"] = cc
                except Exception:
                    pass

            # Once we've nailed the row (got course_code), stop searching.
            if out["course_code"]:
                break
        if out["course_code"]:
            break

    # Fallback for student email — only present when the contact card
    # is visible (i.e., the user navigated from Caseload into the
    # student's contact record). Empty when only the Notes tab is up.
    if not out["student_email"]:
        out["student_email"] = _extract_wgu_email(page)

    return out


def detect_course_code(page: Page, student_name: str) -> Optional[str]:
    """Backward-compatible thin wrapper: returns just the course code,
    or None if not found."""
    return lookup_caseload_student(page, student_name)["course_code"] or None


def gather_caseload_matches(
    page: Page,
    query: str,
    on_status=None,
) -> list[tuple]:
    """Return matching rows from Caseload tables WITHOUT clicking.

    Each tuple is (priority, row_locator, student_name, name_col_idx),
    sorted by priority ascending. Use click_caseload_row() to act on a
    chosen match.
    """
    def diag(msg: str) -> None:
        if on_status:
            on_status(msg)

    q = query.strip()
    if not q:
        return None
    q_lower = q.lower()

    # Each candidate: (priority, row_locator, student_name, name_col_idx)
    candidates: list[tuple[int, "object", str, int]] = []

    tables = page.locator("table").filter(
        has=page.locator("th", has_text="Course Code")
    )
    n_tables = tables.count()
    diag(f"  [search] {n_tables} table(s) with Course Code header in DOM")

    total_rows = 0
    sample_names: list[str] = []

    for i in range(n_tables):
        table = tables.nth(i)
        headers = table.locator("th").all_text_contents()
        diag(f"  [search] table {i} headers: {[h.strip()[:30] for h in headers]}")
        name_idx = next(
            (j for j, h in enumerate(headers) if h.strip() == "Name"),
            None,
        )
        if name_idx is None:
            # Fallback: look for a column whose text *starts* with "Name"
            # (Lightning sometimes appends sorting widgets to header text).
            name_idx = next(
                (j for j, h in enumerate(headers) if h.strip().startswith("Name")),
                None,
            )
        if name_idx is None:
            diag(f"  [search] table {i}: no 'Name' column, skipping")
            continue
        diag(f"  [search] table {i}: Name resolved to col idx {name_idx}")

        rows = table.locator("tr")
        n_rows = rows.count()
        diag(f"  [search] table {i}: {n_rows} <tr>, Name col idx {name_idx}")

        for r in range(1, n_rows):
            row = rows.nth(r)
            cells = row.locator("td").all_text_contents()
            if not cells or name_idx >= len(cells):
                continue
            name = cells[name_idx].strip()
            if not name:
                continue
            total_rows += 1
            if len(sample_names) < 5:
                sample_names.append(name)

            # 1) any cell exact match (Student ID, full name, etc.)
            if any(c.strip() == q for c in cells):
                candidates.append((1, row, name, name_idx))
                continue

            # 2) exact email match
            try:
                mailtos = row.locator('a[href^="mailto:"]')
                hit = False
                for m in range(mailtos.count()):
                    href = mailtos.nth(m).get_attribute("href") or ""
                    parsed = urlparse(href)
                    addrs = [unquote(parsed.path).strip()]
                    addrs += [
                        unquote(x).strip()
                        for x in parse_qs(parsed.query).get("cc", [])
                    ]
                    if any(a.lower() == q_lower for a in addrs):
                        hit = True
                        break
                if hit:
                    candidates.append((2, row, name, name_idx))
                    continue
            except Exception:
                pass

            # 3) name substring (case-insensitive)
            if q_lower in name.lower():
                candidates.append((3, row, name, name_idx))

    diag(f"  [search] scanned {total_rows} data rows; "
         f"first names: {', '.join(sample_names) or '(none)'}")

    candidates.sort(key=lambda x: x[0])
    return candidates


def gather_fuzzy_caseload_matches(
    page: Page,
    query: str,
    on_status=None,
    *,
    cutoff: float = 0.65,
    max_results: int = 10,
) -> list[tuple]:
    """Fallback when gather_caseload_matches returns nothing. Walks the
    same caseload tables and returns rows whose name is close to the
    query under difflib ratio. Useful for typos like 'Jsoh' → 'Joshua *'.

    Comparison strategy per name (best of):
      1. ratio(query, full lowercase name)
      2. ratio(query, each whitespace/comma-delimited token)
      3. ratio(query, token[:len(query)]) — fuzzy prefix match. Critical
         for short typoed queries against long names: ratio("jsoh",
         "joshua") ≈ 0.60 but ratio("jsoh", "josh") = 0.75.

    Returns the same (priority, row, name, name_col_idx) tuple shape as
    gather_caseload_matches, with priority=4 (below all exact tiers).
    Sorted by best ratio descending."""
    def diag(msg: str) -> None:
        if on_status:
            on_status(msg)

    q = query.strip().lower()
    if not q:
        return []

    candidates: list[tuple[int, object, str, int, float]] = []
    tables = page.locator("table").filter(
        has=page.locator("th", has_text="Course Code")
    )
    n_tables = tables.count()
    n_scanned = 0
    for i in range(n_tables):
        table = tables.nth(i)
        headers = table.locator("th").all_text_contents()
        name_idx = next(
            (j for j, h in enumerate(headers) if h.strip() == "Name"),
            None,
        )
        if name_idx is None:
            name_idx = next(
                (j for j, h in enumerate(headers) if h.strip().startswith("Name")),
                None,
            )
        if name_idx is None:
            continue
        rows = table.locator("tr")
        n_rows = rows.count()
        for r in range(1, n_rows):
            row = rows.nth(r)
            cells = row.locator("td").all_text_contents()
            if not cells or name_idx >= len(cells):
                continue
            name = cells[name_idx].strip()
            if not name:
                continue
            n_scanned += 1
            name_lc = name.lower()
            tokens = [t for t in re.split(r"[\s,]+", name_lc) if t]
            best = difflib.SequenceMatcher(None, q, name_lc).ratio()
            for t in tokens:
                ratio = difflib.SequenceMatcher(None, q, t).ratio()
                if ratio > best:
                    best = ratio
                # Fuzzy prefix: query vs token's same-length prefix.
                if len(t) > len(q):
                    prefix_ratio = difflib.SequenceMatcher(
                        None, q, t[: len(q)],
                    ).ratio()
                    if prefix_ratio > best:
                        best = prefix_ratio
            if best >= cutoff:
                candidates.append((4, row, name, name_idx, best))

    candidates.sort(key=lambda x: -x[4])
    diag(f"  [search] fuzzy: scanned {n_scanned} rows; "
         f"{len(candidates)} above cutoff {cutoff:.2f}")
    if candidates:
        top = candidates[0]
        diag(f"  [search] fuzzy top: {top[2]!r} @ {top[4]:.2f}")
    candidates = candidates[:max_results]
    return [(p, r, n, i) for (p, r, n, i, _) in candidates]


def click_caseload_row(row, name: str, name_idx: int, on_status=None) -> bool:
    """Click into the named student's row (in the Name cell). Tries
    several click targets because Lightning's data grid name cells use
    custom Aura components, not native anchors.

    Defensive two-step for off-viewport cells (common when the user's
    Caseload view has many columns enabled — the Name cell scrolls
    off horizontally even though it's clearly in the DOM):
      1. JS scrollIntoView with `inline: 'center'` to bring the cell
         into the horizontal viewport before clicking.
      2. If Playwright's click STILL refuses with "outside of the
         viewport", fall back to a JS `el.click()` — that synthesizes
         the click without requiring the mouse coordinate to land in
         the visible area. Lightning's onClick handlers fire on
         synthetic clicks just like on real ones."""
    def diag(msg: str) -> None:
        if on_status:
            on_status(msg)

    name_cell = row.locator("td").nth(name_idx)

    # Step 1: scroll the name cell into view via JS. Handles both
    # vertical and horizontal scroll within the data table's
    # scrollable container.
    try:
        name_cell.evaluate(
            "el => el.scrollIntoView({block: 'center', inline: 'center'})"
        )
    except Exception:
        pass

    for sub_locator in (
        name_cell.locator("a").filter(has_text=name),
        name_cell.locator("span").filter(has_text=name),
        name_cell,
    ):
        try:
            if sub_locator.count() == 0:
                continue
            # Step 2a: try Playwright click first (gets event
            # propagation right, triggers Lightning handlers
            # reliably).
            try:
                sub_locator.first.click(force=True)
                return True
            except Exception as inner:
                # Step 2b: fall back to JS click — bypasses the
                # viewport requirement entirely.
                sub_locator.first.evaluate("el => el.click()")
                return True
        except Exception as e:
            diag(f"  [search] click attempt failed: {e}")
            continue
    return False


def find_and_click_student(
    page: Page,
    query: str,
    on_status=None,
) -> Optional[str]:
    """Convenience wrapper: gathers matches and clicks the highest-
    priority one. For ambiguous queries (multiple matches at the same
    priority) it still picks the first — callers that want to disambiguate
    should use gather_caseload_matches directly."""
    def diag(msg: str) -> None:
        if on_status:
            on_status(msg)

    matches = gather_caseload_matches(page, query, on_status=on_status)
    if not matches:
        return None
    priority, row, name, name_idx = matches[0]
    diag(f"  [search] match: {name!r} (priority {priority})")
    return name if click_caseload_row(row, name, name_idx, on_status) else None
