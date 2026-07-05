"""Apply user-defined criteria to caseload rows.

Each filter is a dict with keys `column`, `op`, and (sometimes) `value`.
Filters AND together — all must match for a row to pass.

Operators accept both short YAML forms and the long user-facing labels
the editor UI will eventually expose. The YAML written by the editor
uses short forms; the engine accepts either:

    short          long-form label (UI)
    ─────          ────────────────
    empty          is empty
    not_empty      is not empty
    equals         is
    not_equals     is not
    contains       contains
    not_contains   does not contain
    before         is before
    after          is after
    on             is on
    within         is within
    gt             more than          (numeric)
    lt             less than          (numeric)
    gte            at least           (numeric)
    lte            at most            (numeric)

Date values can be absolute (YYYY-MM-DD, MM/DD/YYYY, M/D/YYYY,
ISO-with-time) or relative shorthand: `today`, `today-7d`, `today+30d`.
Within-presets: `this week`, `this month`, `next 7 days`, `next 30 days`,
`last 7 days`, `last 30 days`, `last month`.
"""
import re
from datetime import date, datetime, timedelta
from typing import Optional


# Normalize long-form labels to short opcode used by the engine.
_OP_ALIASES = {
    "is empty": "empty",
    "is not empty": "not_empty",
    "is": "equals",
    "is not": "not_equals",
    # Multi-value membership — same engine path as equals/not_equals (which
    # already match ANY / exclude ALL of a comma-separated or list value); these
    # are just clearer UI labels for "match one of several".
    "is any of": "equals",
    "is none of": "not_equals",
    "does not contain": "not_contains",
    "is before": "before",
    "is after": "after",
    "is on": "on",
    "is on or before": "on_or_before",
    "is on or after": "on_or_after",
    "is within": "within",
    "more than": "gt",
    "less than": "lt",
    "at least": "gte",
    "at most": "lte",
    ">": "gt",
    "<": "lt",
    ">=": "gte",
    "<=": "lte",
}


WITHIN_PRESETS = [
    "this week",
    "this month",
    "next 7 days",
    "next 30 days",
    "last 7 days",
    "last 30 days",
    "last month",
]


def normalize_op(op: str) -> str:
    """Return the short form of `op` so the engine has one set of names
    to match against."""
    if not op:
        return ""
    lc = op.strip().lower()
    return _OP_ALIASES.get(lc, lc)


def _parse_date_cell(value: str) -> Optional[date]:
    """Best-effort parse of a Caseload cell into a date. Returns None
    if the value doesn't look date-like."""
    if not value:
        return None
    v = value.strip()
    # ISO with optional time (e.g. '2026-05-21T03:02:22.000Z')
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    # Common formats
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%-m/%-d/%Y"):
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            pass
    # Tolerant fallback: a leading date with trailing junk, e.g. a caseload
    # Task cell like '2026-09-13 (1)' (date + submission count). Lets a
    # column-reference value like {Task 1} compare as a date.
    m = re.match(r"(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4}|\d{1,2}/\d{1,2}/\d{2})", v)
    if m:
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
            try:
                return datetime.strptime(m.group(1), fmt).date()
            except ValueError:
                pass
    return None


def _parse_date_value(spec: str, today: Optional[date] = None) -> Optional[date]:
    """Parse a user-typed date value: `today`, `today-7d`, `today+30d`,
    or an absolute date in the same formats _parse_date_cell handles."""
    if not spec:
        return None
    today = today or date.today()
    spec = spec.strip().lower()
    if spec == "today":
        return today
    m = re.fullmatch(r"today\s*([+-])\s*(\d+)\s*d", spec)
    if m:
        sign = 1 if m.group(1) == "+" else -1
        return today + timedelta(days=sign * int(m.group(2)))
    return _parse_date_cell(spec)


def _within_range(preset: str, today: Optional[date] = None) -> Optional[tuple[date, date]]:
    """Resolve a `within` preset to an inclusive (start, end) date pair."""
    today = today or date.today()
    p = (preset or "").strip().lower()
    if p == "this week":
        start = today - timedelta(days=today.weekday())  # Monday
        return (start, start + timedelta(days=6))
    if p == "this month":
        start = today.replace(day=1)
        if today.month == 12:
            next_month = today.replace(year=today.year + 1, month=1, day=1)
        else:
            next_month = today.replace(month=today.month + 1, day=1)
        return (start, next_month - timedelta(days=1))
    if p == "next 7 days":
        return (today, today + timedelta(days=7))
    if p == "next 30 days":
        return (today, today + timedelta(days=30))
    if p == "last 7 days":
        return (today - timedelta(days=7), today)
    if p == "last 30 days":
        return (today - timedelta(days=30), today)
    if p == "last month":
        this_month_start = today.replace(day=1)
        if today.month == 1:
            prev_month_start = today.replace(year=today.year - 1, month=12, day=1)
        else:
            prev_month_start = today.replace(month=today.month - 1, day=1)
        return (prev_month_start, this_month_start - timedelta(days=1))
    return None


def _parse_number(value) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(str(value).strip())
    except (ValueError, TypeError):
        return None


def _expand_to_values(value) -> list[str]:
    """Coerce a filter `value` into a list of strings. Accepts:
      - a YAML list (`[a, b, c]`) → as-is
      - a comma-separated string (`"a, b, c"`) → split + stripped
      - a plain string → single-element list
    Text ops use this to support OR semantics across multiple values.
    Date/number ops still take a single value (commas don't appear
    naturally in those types)."""
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    if value is None:
        return []
    s = str(value).strip()
    if not s:
        return []
    if "," in s:
        return [p.strip() for p in s.split(",") if p.strip()]
    return [s]


def sniff_column_type(samples: list[str]) -> str:
    """Classify a column as 'date', 'number', or 'text' from its first
    non-empty cells. Majority-rule with 'text' as the tiebreaker."""
    non_empty = [s for s in samples if s and s.strip()]
    if not non_empty:
        return "text"
    n = len(non_empty)
    n_date = sum(1 for s in non_empty if _parse_date_cell(s) is not None)
    n_num = sum(1 for s in non_empty if _parse_number(s) is not None)
    if n_date * 2 > n:
        return "date"
    if n_num * 2 > n:
        return "number"
    return "text"


def evaluate_filter(filt: dict, row: dict, *, today: Optional[date] = None) -> bool:
    """Return True iff `row` matches the single filter `filt`."""
    column = filt.get("column", "")
    op = normalize_op(filt.get("op", ""))
    value = filt.get("value", "")
    cell = row.get(column, "")
    cell_str = "" if cell is None else str(cell)
    cell_lower = cell_str.strip().lower()
    value_str = "" if value is None else str(value)
    value_lower = value_str.strip().lower()

    # Column-to-column comparison: a value written as "{Other Column}" means
    # "compare this row's `column` against this row's `Other Column`". Resolve
    # it to that cell's value here so the op below compares the two cells.
    value_is_ref = False
    m_ref = re.fullmatch(r"\{(.+)\}", value_str.strip())
    if m_ref:
        value_is_ref = True
        ref_cell = row.get(m_ref.group(1), "")
        value = "" if ref_cell is None else str(ref_cell)
        value_str = value
        value_lower = value_str.strip().lower()

    if op == "empty":
        return cell_str.strip() == ""
    if op == "not_empty":
        return cell_str.strip() != ""

    # Text ops accept multi-value: cell passes if it matches ANY value
    # for the positive ops, NONE for the negative ops. Values come from
    # either a YAML list or a comma-separated string.
    if op in ("equals", "not_equals", "contains", "not_contains"):
        targets = [t.lower() for t in _expand_to_values(value)]
        if not targets:
            # No targets to compare against — only meaningful answer is
            # 'false' for positive ops, 'true' for negative ops.
            return op.startswith("not_")
        if op == "equals":
            return any(cell_lower == t for t in targets)
        if op == "not_equals":
            return all(cell_lower != t for t in targets)
        if op == "contains":
            return any(t in cell_lower for t in targets)
        if op == "not_contains":
            return all(t not in cell_lower for t in targets)

    if op in ("before", "after", "on", "on_or_before", "on_or_after"):
        cell_date = _parse_date_cell(cell_str)
        # A column-ref value is a CELL (parse as a date cell); a literal value
        # may use relative shorthand (today-7d), so parse it as a date value.
        target_date = (_parse_date_cell(value_str) if value_is_ref
                       else _parse_date_value(value_str, today=today))
        if cell_date is None or target_date is None:
            return False
        if op == "before":
            return cell_date < target_date
        if op == "after":
            return cell_date > target_date
        if op == "on_or_before":
            return cell_date <= target_date
        if op == "on_or_after":
            return cell_date >= target_date
        return cell_date == target_date

    if op == "within":
        cell_date = _parse_date_cell(cell_str)
        if cell_date is None:
            return False
        rng = _within_range(value_str, today=today)
        if rng is None:
            return False
        return rng[0] <= cell_date <= rng[1]

    cv = _parse_number(cell_str)
    tv = _parse_number(value_str)
    if op == "gt":
        return cv is not None and tv is not None and cv > tv
    if op == "lt":
        return cv is not None and tv is not None and cv < tv
    if op == "gte":
        return cv is not None and tv is not None and cv >= tv
    if op == "lte":
        return cv is not None and tv is not None and cv <= tv

    # Unknown op — fail closed (don't accidentally match everything).
    return False


def apply_filters(
    filters: list[dict],
    rows: list[dict],
    *,
    today: Optional[date] = None,
) -> list[dict]:
    """Return the subset of `rows` for which every filter passes.
    Empty filter list returns all rows unchanged."""
    if not filters:
        return list(rows)
    return [
        r for r in rows
        if all(evaluate_filter(f, r, today=today) for f in filters)
    ]
