"""Render HTML email templates with variable substitution.

Templates are HTML files with {{variable}} placeholders. Two
substitution modes:

- **Plain** (default): the value is HTML-escaped and inserted as-is.
  Right for single-line strings like a name or course code.
- **Smart** (used for prompt-supplied multi-line text): HTML-escape,
  convert blank lines to paragraph breaks, single newlines to <br/>.
  Renders user-typed plain text the way they expect it to look —
  paragraphs preserved, no Markdown required.

Unknown placeholders are LEFT in place so the user sees them in the
draft. Easier to spot a typo'd variable than silent omission.
"""
import html
import re
from pathlib import Path
from typing import Optional


_VAR_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def smart_format_for_html(text: str) -> str:
    """Plain text → HTML chunk preserving paragraphs and line breaks.
    Blank line = paragraph break; single newline = <br/>. HTML-escapes
    special chars first. Returns empty string for empty/whitespace-only
    input."""
    if not text or not text.strip():
        return ""
    escaped = html.escape(text)
    escaped = escaped.replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = [p for p in re.split(r"\n\s*\n", escaped) if p.strip()]
    rendered = [p.replace("\n", "<br/>") for p in paragraphs]
    return "".join(f"<p>{p}</p>" for p in rendered)


def render(
    template_text: str,
    variables: dict,
    *,
    smart_format_vars: Optional[set[str]] = None,
) -> str:
    """Substitute {{name}} placeholders in `template_text`.

    Variables named in `smart_format_vars` go through
    smart_format_for_html (paragraph/break preservation). All other
    variables are HTML-escaped only. Unknown placeholders are left
    untouched so they're visible in the draft for debugging."""
    smart = smart_format_vars or set()

    def replace(match: re.Match) -> str:
        name = match.group(1)
        if name not in variables:
            return match.group(0)
        value = variables[name]
        if value is None:
            value = ""
        value = str(value)
        if name in smart:
            return smart_format_for_html(value)
        return html.escape(value)

    return _VAR_RE.sub(replace, template_text)


# Human-readable labels for known variables, shown in the
# template-preview email so the user can see at a glance what each
# `{{var}}` would be replaced with. Anything not in this dict falls
# back to UPPER_SNAKE_CASE → "UPPER SNAKE CASE".
PLACEHOLDER_LABELS = {
    "first_name": "STUDENT FIRST NAME",
    "last_name": "STUDENT LAST NAME",
    "full_name": "STUDENT FULL NAME",
    "student_email": "STUDENT EMAIL",
    "student_id": "STUDENT ID",
    "course_code": "COURSE CODE",
    "program_name": "PROGRAM NAME",
    "pm_name": "PROGRAM MENTOR NAME",
    "pm_email": "PROGRAM MENTOR EMAIL",
    "user_name": "YOUR NAME",
    "user_email": "YOUR EMAIL",
}


def _placeholder_label(var: str) -> str:
    if var in PLACEHOLDER_LABELS:
        return PLACEHOLDER_LABELS[var]
    return var.replace("_", " ").upper()


def render_with_placeholders(template_text: str) -> str:
    """Render the HTML body for the batch template-preview email.
    Replaces `{{var}}` with `&lt;LABEL&gt;` (HTML-escaped) so the
    angle brackets appear LITERALLY in Outlook rather than being
    interpreted as HTML tags."""
    def replace(match: re.Match) -> str:
        return f"&lt;{_placeholder_label(match.group(1))}&gt;"
    return _VAR_RE.sub(replace, template_text)


def render_plain_with_placeholders(template_text: str) -> str:
    """Like render_with_placeholders, but for plain-text fields
    (subject, To, CC) where angle brackets render as-is."""
    def replace(match: re.Match) -> str:
        return f"<{_placeholder_label(match.group(1))}>"
    return _VAR_RE.sub(replace, template_text)


def render_plain(template_text: str, variables: dict) -> str:
    """Same as render() but with NO HTML escaping. Use for fields
    whose output is plain text (email subject, To/CC addresses) —
    those go into Outlook as-is and should not contain `&amp;`,
    `&lt;`, etc. Unknown placeholders are still preserved."""
    def replace(match: re.Match) -> str:
        name = match.group(1)
        if name not in variables:
            return match.group(0)
        value = variables[name]
        if value is None:
            value = ""
        return str(value)

    return _VAR_RE.sub(replace, template_text)


def wrap_with_font(html: str, font_family: str, font_size: int) -> str:
    """Wrap `html` in an outer <div> with inline font-family +
    font-size CSS. Outlook honors inline styles in HTMLBody, so this
    is how we pin the sent email's font. Returns the html unchanged
    when family is empty or size <= 0 — caller's signal to "use
    Outlook's compose default" (no CSS injection means Outlook
    applies whatever the user has set in Mail > Stationery and Fonts)."""
    family = (font_family or "").strip()
    try:
        size = int(font_size or 0)
    except (TypeError, ValueError):
        size = 0
    if not family or size <= 0:
        return html
    # Quote family so multi-word names (e.g. "Times New Roman") survive.
    # Add a generic fallback so missing fonts on the recipient's side
    # still render readably.
    quoted = f'"{family}"' if " " in family else family
    style = f"font-family: {quoted}, sans-serif; font-size: {size}pt"
    return f'<div style="{style}">{html}</div>'


def load_template(path: Path) -> str:
    """Read a UTF-8 template file."""
    return Path(path).read_text(encoding="utf-8")
