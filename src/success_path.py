"""Local per-student Success Path state.

A course's *success path* is an ordered checklist of **steps** (defined in
``scenarios.yaml`` — config, not here). This module stores the per-student
**data** that drives and records that checklist, in a small SQLite DB
(``config.SUCCESS_PATH_DB``), kept separate from ``history.db`` because it's
durable, mutable current state plus an event log — not append-only time-series.

Two tables:

- ``field_values`` — the typed facts the *user* enters (and actions may set):
  deadline overrides, flags, etc. Mutable; one current value per
  ``(student_id, course_code, field_name)``. Values are stored as TEXT; the
  field's *type* lives in its definition (scenarios.yaml) and is applied by the
  caller. ``source`` records provenance (``"manual"`` / ``"action:<name>"``).

- ``step_log`` — an **append-only** record of step completions/skips. Each row
  is one event for one step; the step's *current* status is the latest event
  (log-only, no separate current-state table). Append-only so it doubles as an
  analysis signal — joinable to ``history.db`` on ``(student_id, course_code)``
  to ask e.g. "did students who got Task 3 advice within N days of passing
  Task 2 finish faster?". Expected ``event`` values:
    ``completed`` — the step's action fired, or the user ticked it done.
    ``dismissed`` — the user manually chose to skip it (a durable decision,
                    distinct from an *auto-skip*, which the engine computes
                    live from a step's skip-when condition and never logs).
    ``reset``     — undo, back to not-yet-acted.

Keys are ``(student_id, course_code)`` throughout, matching ``history.db`` and
the caseload (a student can be enrolled in several courses at once).

All writes are wrapped so a DB failure is non-fatal to the caller (filing a
note must never break because this couldn't be written).
"""
import csv
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.config import SUCCESS_PATH_DB

_SCHEMA_VERSION = 1

# Recognized step_log events (free-text in the DB, but these are the ones the
# engine understands; documented as constants so callers don't typo them).
EVENT_COMPLETED = "completed"
EVENT_DISMISSED = "dismissed"
EVENT_RESET = "reset"

# Computed DISPLAY status for a step (see compute_steps) — NOT stored; derived
# live from the latest event + the gate/skip-when conditions.
STATUS_DONE = "done"        # the step's action fired, or ticked done
STATUS_SKIPPED = "skipped"  # dismissed by the user, OR auto-skipped by skip_when
STATUS_DUE = "due"          # gate satisfied, not done — actionable now
STATUS_BLOCKED = "blocked"  # gate not satisfied yet (an earlier step is pending)


_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS field_values (
    student_id  TEXT NOT NULL,
    course_code TEXT NOT NULL,
    field_name  TEXT NOT NULL,
    value       TEXT,
    updated_at  TEXT NOT NULL,
    source      TEXT,
    PRIMARY KEY (student_id, course_code, field_name)
);
CREATE INDEX IF NOT EXISTS ix_fv_student ON field_values(student_id, course_code);

CREATE TABLE IF NOT EXISTS step_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id  TEXT NOT NULL,
    course_code TEXT NOT NULL,
    step_id     TEXT NOT NULL,
    event       TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    source      TEXT,
    detail      TEXT
);
CREATE INDEX IF NOT EXISTS ix_sl_student ON step_log(student_id, course_code);
CREATE INDEX IF NOT EXISTS ix_sl_step
    ON step_log(student_id, course_code, step_id, id);
"""


def _connect(db_path=SUCCESS_PATH_DB) -> sqlite3.Connection:
    """Open the success-path DB, creating the schema as needed. Rows come back
    as ``sqlite3.Row`` so columns are addressable by name."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA_DDL)
    conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
    conn.commit()
    return conn


def _now_iso(now: Optional[datetime]) -> str:
    return (now or datetime.now()).isoformat(timespec="seconds")


# ----------------------------------------------------------------------
# field_values — the data the user enters / actions set
# ----------------------------------------------------------------------
def set_field(student_id: str, course_code: str, field_name: str, value,
              *, source: str = "manual", db_path=SUCCESS_PATH_DB,
              now: Optional[datetime] = None) -> bool:
    """Upsert one field's current value for a student+course. ``value`` is
    coerced to TEXT (the field's real type lives in its definition). Returns
    True on success; never raises (a write failure is non-fatal)."""
    try:
        sval = "" if value is None else str(value)
        conn = _connect(db_path)
        try:
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO field_values "
                    "(student_id, course_code, field_name, value, updated_at, "
                    "source) VALUES (?, ?, ?, ?, ?, ?)",
                    (student_id, course_code, field_name, sval,
                     _now_iso(now), source),
                )
        finally:
            conn.close()
        return True
    except Exception:
        return False


def clear_field(student_id: str, course_code: str, field_name: str,
                *, db_path=SUCCESS_PATH_DB) -> bool:
    """Remove one field value. Returns True on success; never raises."""
    try:
        conn = _connect(db_path)
        try:
            with conn:
                conn.execute(
                    "DELETE FROM field_values WHERE student_id = ? AND "
                    "course_code = ? AND field_name = ?",
                    (student_id, course_code, field_name),
                )
        finally:
            conn.close()
        return True
    except Exception:
        return False


def get_field(student_id: str, course_code: str, field_name: str,
              *, db_path=SUCCESS_PATH_DB) -> Optional[str]:
    """Current value of one field, or None if unset."""
    conn = _connect(db_path)
    try:
        r = conn.execute(
            "SELECT value FROM field_values WHERE student_id = ? AND "
            "course_code = ? AND field_name = ?",
            (student_id, course_code, field_name),
        ).fetchone()
        return r["value"] if r is not None else None
    finally:
        conn.close()


def get_fields(student_id: str, course_code: str,
               *, db_path=SUCCESS_PATH_DB) -> dict:
    """All field values for one student+course as ``{field_name: value}``."""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT field_name, value FROM field_values "
            "WHERE student_id = ? AND course_code = ?",
            (student_id, course_code),
        ).fetchall()
        return {r["field_name"]: r["value"] for r in rows}
    finally:
        conn.close()


def all_field_values(*, db_path=SUCCESS_PATH_DB) -> dict:
    """Every field value, keyed ``{(student_id, course_code): {field: value}}``.
    One query — for enriching the whole caseload at once (rule evaluation)."""
    conn = _connect(db_path)
    try:
        out: dict = {}
        for r in conn.execute(
            "SELECT student_id, course_code, field_name, value FROM field_values"
        ):
            out.setdefault((r["student_id"], r["course_code"]), {})[
                r["field_name"]] = r["value"]
        return out
    finally:
        conn.close()


# ----------------------------------------------------------------------
# step_log — append-only completions / skips; status = latest event
# ----------------------------------------------------------------------
def log_step(student_id: str, course_code: str, step_id: str, event: str,
             *, source: str = "manual", detail: str = "",
             db_path=SUCCESS_PATH_DB, now: Optional[datetime] = None) -> bool:
    """Append one step event (``completed`` / ``dismissed`` / ``reset``).
    Returns True on success; never raises."""
    try:
        conn = _connect(db_path)
        try:
            with conn:
                conn.execute(
                    "INSERT INTO step_log (student_id, course_code, step_id, "
                    "event, occurred_at, source, detail) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (student_id, course_code, step_id, event,
                     _now_iso(now), source, detail),
                )
        finally:
            conn.close()
        return True
    except Exception:
        return False


def step_status(student_id: str, course_code: str,
                *, db_path=SUCCESS_PATH_DB) -> dict:
    """Current status per step for one student+course, as ``{step_id: event}``
    — the latest event for each step (log-only; ``reset`` reads as not-acted).
    Ordering is by row ``id`` (monotonic), so the newest event wins even when
    two events share a timestamp."""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT step_id, event FROM step_log WHERE id IN ("
            "  SELECT MAX(id) FROM step_log "
            "  WHERE student_id = ? AND course_code = ? GROUP BY step_id)",
            (student_id, course_code),
        ).fetchall()
        return {r["step_id"]: r["event"] for r in rows}
    finally:
        conn.close()


def all_step_status(*, db_path=SUCCESS_PATH_DB) -> dict:
    """Latest event per step for everyone, keyed
    ``{(student_id, course_code): {step_id: event}}``. One query — for
    enriching the whole caseload at once."""
    conn = _connect(db_path)
    try:
        out: dict = {}
        for r in conn.execute(
            "SELECT student_id, course_code, step_id, event FROM step_log "
            "WHERE id IN (SELECT MAX(id) FROM step_log "
            "GROUP BY student_id, course_code, step_id)"
        ):
            out.setdefault((r["student_id"], r["course_code"]), {})[
                r["step_id"]] = r["event"]
        return out
    finally:
        conn.close()


def compute_steps(steps, row, events, fields=None, *, today=None) -> list:
    """Compute each step's live DISPLAY status for one student on a course.

    Pure logic (no DB / no I/O) — the caller supplies the inputs:
      ``steps``  ordered PathStep-likes (need .id/.description/.action/.gate/
                 .skip_when),
      ``row``    the student's caseload row (dict of column -> value),
      ``events`` ``{step_id: latest_event}`` from ``step_status()``,
      ``fields`` ``{field_name: value}`` path field values (merged into the row
                 so gate/skip_when predicates can reference them by name).

    Status precedence per step: a logged ``completed`` -> Done and a logged
    ``dismissed`` -> Skipped always win (an explicit user/action decision).
    Otherwise ``skip_when`` (any predicate list that fully matches) auto-skips;
    else the ``gate`` decides Due vs Blocked. An EMPTY gate is the linear
    default — satisfied once the PREVIOUS step is Done/Skipped — so by default
    exactly the first not-done step is Due and the rest Blocked. A non-empty
    gate is evaluated against the row independently (it can be Due out of order).

    Returns a list of dicts (step order preserved):
      ``{id, description, action, status, auto_skipped, is_next}`` where
    ``is_next`` marks the single recommended next action (first Due step)."""
    from src import caseload_filter as cf
    ctx = dict(row or {})
    if fields:
        ctx.update(fields)
    events = events or {}

    def _all(preds) -> bool:
        try:
            return all(cf.evaluate_filter(p, ctx, today=today) for p in preds)
        except Exception:
            return False

    out: list = []
    prev_satisfied = True   # the first step's linear gate is satisfied
    next_assigned = False
    for step in steps:
        ev = events.get(step.id)
        auto = False
        if ev == EVENT_COMPLETED:
            status = STATUS_DONE
        elif ev == EVENT_DISMISSED:
            status = STATUS_SKIPPED
        elif step.skip_when and _all(step.skip_when):
            status, auto = STATUS_SKIPPED, True
        else:
            gate_ok = _all(step.gate) if step.gate else prev_satisfied
            status = STATUS_DUE if gate_ok else STATUS_BLOCKED
        is_next = status == STATUS_DUE and not next_assigned
        if is_next:
            next_assigned = True
        out.append({
            "id": step.id,
            "description": step.description or step.id,
            "action": step.action,
            "status": status,
            "auto_skipped": auto,
            "is_next": is_next,
        })
        prev_satisfied = status in (STATUS_DONE, STATUS_SKIPPED)
    return out


def step_events(student_id: str, course_code: str, step_id: Optional[str] = None,
                *, db_path=SUCCESS_PATH_DB) -> list:
    """Full chronological event history for a student+course (optionally one
    step) — for timelines and analysis. Oldest first."""
    conn = _connect(db_path)
    try:
        if step_id is None:
            rows = conn.execute(
                "SELECT * FROM step_log WHERE student_id = ? AND "
                "course_code = ? ORDER BY id ASC",
                (student_id, course_code),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM step_log WHERE student_id = ? AND "
                "course_code = ? AND step_id = ? ORDER BY id ASC",
                (student_id, course_code, step_id),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def export_log_to_csv(dest_path, *, db_path=SUCCESS_PATH_DB) -> int:
    """Dump the whole step_log to a CSV (for analysis alongside history's
    export). Returns the number of rows written. Raises on a write error so
    the caller can report it."""
    conn = _connect(db_path)
    try:
        cur = conn.execute("SELECT * FROM step_log ORDER BY id ASC")
        header = [d[0] for d in cur.description]
        n = 0
        with open(dest_path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(header)
            for r in cur:
                w.writerow([r[c] for c in header])
                n += 1
        return n
    finally:
        conn.close()
