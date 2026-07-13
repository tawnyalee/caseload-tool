import html
import re

def types_for_format(fmt: str) -> list[str]:
    if fmt == "Multiple Interactions":
        return [
            "Live Call and Email to Student", "Email Exchange with Student",
            "Voicemail and Email to Student", "Voicemail/Email and Text to Student",
            "Voicemail to Student and Text Message", "Live Call and Text Message",
            "Email to Student and Text Message", "Video Call and Email to Student",
            "Voicemail from Student and Email to Student",
            "Voicemail Full/Email to Student",
        ]
    else:
        return [
            "Email to Student", "Live Call", "Email from Student", "Video Call",
            "Course Chatter Response", "Voicemail to Student",
            "Instant Message (IM) / Text", "Voicemail from Student",
            "Webinar Attendance Noted", "Admin Note", "Mass Email", "Cohort Event",
        ]

def _typo_variants(query: str) -> list[str]:
    """All adjacent-transposition variants of `query`."""
    out: list[str] = []
    seen = {query}
    for i in range(len(query) - 1):
        chars = list(query)
        chars[i], chars[i + 1] = chars[i + 1], chars[i]
        v = "".join(chars)
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out

def _note_body_to_html(text: str) -> str:
    """Convert a plain-text note body into the simple paragraph HTML."""
    lines = (text or "").split("\n")
    parts = [
        ("<p>" + html.escape(ln) + "</p>") if ln.strip() else "<p><br></p>"
        for ln in lines
    ]
    return "".join(parts) or "<p><br></p>"

def _to_iso_date(s: str) -> str:
    """Normalize a follow-up date string to ISO 'YYYY-MM-DD'."""
    s = (s or "").strip()
    if not s:
        return ""
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        y, mo, d = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d}"
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)  # MM/DD/YYYY
    if m:
        mo, d, y = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d}"
    return s