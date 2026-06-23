"""Local longitudinal history of the caseload's dynamic fields.

The Salesforce caseload is exported as a CSV and reloaded fresh each time
the app refreshes. Fields like ``Momentum`` and ``LatestTaskStatus`` change
over time, and a student who passes or drops simply *disappears* from the
export — so without recording snapshots there's no way to review a trend or
notice that someone left and needs a follow-up.

This module snapshots the dynamic fields into a small SQLite DB
(``config.HISTORY_DB``) on reload. SQLite (stdlib ``sqlite3``) so the data
reads straight into pandas (``pd.read_sql_query``) and supports cheap
timeline / "who-departed" queries; an Export-to-CSV escape hatch is provided.

Design notes:
- **Sampling = at most once per interval window.** Captures are bucketed into
  interval-aligned windows (``_bucket``): a 24 h interval gives one sample per
  calendar day; 6 h gives four. Re-running within the same window upserts
  (newest export wins) rather than duplicating, and never touches an earlier
  window's sample. The interval is a user setting
  (``Settings.history_capture_interval_hours``).
- **Departure** = a ``(student_id, course_code)`` present in the most recent
  *prior-day* collection but absent now. Comparison is always day-grained
  (prior calendar day, gap-aware) regardless of the sampling interval, so
  sub-day samples never generate departure noise.
- ``extra_json`` keeps every non-core CSV column so nothing is lost (the export
  is ~106 columns wide); core fields accept both API and display-name header
  spellings, so changing the export's columns doesn't drop data.

All writes are wrapped so a DB failure is non-fatal to the caller (the reload
path must never break because history couldn't be written).
"""
import csv
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from src import caseload_csv
from src.config import HISTORY_DB

_SCHEMA_VERSION = 3

# Ordinal Momentum scale as it appears in the WGU export. Unknown / blank
# values map to NULL rank (they still store the raw label).
_MOMENTUM_RANK = {"Low": 1, "Med Low": 2, "Med": 3, "Med High": 4, "High": 5}

# CSV header -> snapshot column. Everything NOT listed here is preserved in
# the per-row ``extra_json`` blob. Core fields also accept the display-name
# spelling (see _candidate_headers).
_CORE_HEADERS = {
    "StudentID": "student_id",
    "CourseCode": "course_code",
    "Name": "name",
    "StudentEmail": "student_email",
    "Momentum": "momentum",
    "LatestTaskStatus": "latest_task_status",
    "Task1": "task1",
    "Task2": "task2",
    "Task3": "task3",
    "LatestCourseNote": "latest_course_note",
    "CourseFollowupNote": "followup_note",
    "CourseFollowupDate": "followup_date",
}

# Column order for snapshot inserts.
_SNAP_COLS = [
    "collected_at", "collected_date", "student_id", "course_code", "name",
    "student_email", "momentum", "momentum_rank", "latest_task_status",
    "task1", "task2", "task3", "latest_course_note", "followup_note",
    "followup_date", "extra_json",
]

# If a fresh export has fewer than this fraction of the prior collection's
# rows, treat it as a truncated/filtered export and suppress departures.
_PARTIAL_EXPORT_FRACTION = 0.5


def momentum_rank(label: str) -> Optional[int]:
    """1..5 for the ordinal Momentum label, or None if unknown/blank."""
    return _MOMENTUM_RANK.get((label or "").strip())


def _bucket(now: datetime, interval_hours: int) -> str:
    """Interval-aligned window id for ``now``. A >=24 h interval buckets by
    calendar date (one sample/day, exactly the original behavior); a smaller
    interval splits the day into ``24 // interval`` fixed slots so windows
    never cross a sample. Same bucket => the sample is updated in place."""
    d = now.date().isoformat()
    if interval_hours >= 24 or interval_hours <= 0:
        return d
    return f"{d}#{now.hour // interval_hours}"


# ----------------------------------------------------------------------
# connection + schema (+ v1->v2 migration)
# ----------------------------------------------------------------------
def _connect(db_path=HISTORY_DB) -> sqlite3.Connection:
    """Open the history DB (creating/upgrading the schema as needed). Rows
    come back as ``sqlite3.Row`` so columns are addressable by name."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    _init_schema(conn)
    return conn


_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS collections (
    collected_at   TEXT PRIMARY KEY,
    collected_date TEXT NOT NULL,
    bucket         TEXT NOT NULL,
    csv_mtime      TEXT,
    row_count      INTEGER NOT NULL,
    note           TEXT
);
CREATE INDEX IF NOT EXISTS ix_collections_date   ON collections(collected_date);
CREATE INDEX IF NOT EXISTS ix_collections_bucket ON collections(bucket);

CREATE TABLE IF NOT EXISTS snapshots (
    collected_at       TEXT NOT NULL,
    collected_date     TEXT NOT NULL,
    student_id         TEXT NOT NULL,
    course_code        TEXT NOT NULL,
    name               TEXT,
    student_email      TEXT,
    momentum           TEXT,
    momentum_rank      INTEGER,
    latest_task_status TEXT,
    task1 TEXT, task2 TEXT, task3 TEXT,
    latest_course_note TEXT,
    followup_note      TEXT,
    followup_date      TEXT,
    extra_json         TEXT,
    PRIMARY KEY (collected_at, student_id, course_code)
);
CREATE INDEX IF NOT EXISTS ix_snap_student_at ON snapshots(student_id, collected_at);
CREATE INDEX IF NOT EXISTS ix_snap_date       ON snapshots(collected_date);

-- Small key/value store for cross-cutting bookkeeping (e.g. when the passed
-- outcomes archive was last ingested, for the staleness reminder).
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

-- Resolved *outcomes* for students who have left the live caseload because they
-- passed. The live export drops passers, so the daily snapshots never capture
-- the final result; the only source is WGU's "passed in the last 30 days"
-- caseload view, downloaded as CSV and ingested here. One row per
-- (student, course); re-ingesting a fresh archive upserts (first_seen_at is
-- preserved, everything else refreshed). ``ic_end_date`` is the extension
-- deadline (IC End Date) and takes precedence over ``term_end_date`` when
-- judging whether a pass was "in time".
CREATE TABLE IF NOT EXISTS outcomes (
    student_id    TEXT NOT NULL,
    course_code   TEXT NOT NULL,
    name          TEXT,
    student_email TEXT,
    momentum_at_outcome      TEXT,
    momentum_rank_at_outcome INTEGER,
    latest_task_status       TEXT,
    outcome       TEXT,                 -- 'passed' (only outcome this view carries)
    pass_date     TEXT,                 -- ActualEndDate
    ic_end_date   TEXT,                 -- Icenddate (extension deadline)
    term_end_date TEXT,                 -- TermEndDate
    course_start_date TEXT,
    term_start_date   TEXT,
    other_courses      TEXT,            -- raw OtherCourses list at outcome time
    other_course_count INTEGER,         -- distinct courses OTHER than this one
    entry_momentum       TEXT,          -- Momentum at/near course entry (frozen
    entry_momentum_rank  INTEGER,       --   from the snapshot nearest CourseStart)
    entry_captured       INTEGER,       -- 1 if a genuine at-entry reading exists
    first_seen_at TEXT,                 -- ingest ts we first recorded this outcome
    last_ingest_at TEXT,                -- ingest ts of the most recent refresh
    source_file   TEXT,
    extra_json    TEXT,
    PRIMARY KEY (student_id, course_code)
);
CREATE INDEX IF NOT EXISTS ix_outcomes_course ON outcomes(course_code);
"""


def _init_schema(conn: sqlite3.Connection) -> None:
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    has_tables = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='collections'"
    ).fetchone() is not None
    # Upgrade an EXISTING older schema first (so the DDL below, which indexes
    # the new 'bucket' column, doesn't run against a pre-bucket table). v2->v3
    # only ADDS tables (meta, outcomes), which the IF NOT EXISTS DDL handles —
    # no data migration needed, so only the v1->v2 rebuild is gated here.
    if has_tables and v < 2:
        _migrate_v1_to_v2(conn)
    conn.executescript(_SCHEMA_DDL)  # create from scratch / fill in any gaps
    _ensure_outcome_columns(conn)    # additive columns on an existing outcomes
    conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
    conn.commit()


def _ensure_outcome_columns(conn: sqlite3.Connection) -> None:
    """Add later-introduced outcome columns to a pre-existing table (CREATE
    TABLE IF NOT EXISTS won't alter an existing one). Cheap + idempotent."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(outcomes)")}
    add = {
        "other_courses": "TEXT", "other_course_count": "INTEGER",
        "entry_momentum": "TEXT", "entry_momentum_rank": "INTEGER",
        "entry_captured": "INTEGER",
    }
    for name, typ in add.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE outcomes ADD COLUMN {name} {typ}")


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """v1 keyed snapshots by (collected_date, …) — one sample/day. v2 keys by
    (collected_at, …) so sub-day sampling is possible, and collections gain a
    ``bucket`` column. Data is preserved (v1 was daily, so existing rows are
    unique under the new key)."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(collections)")]
    if cols and "bucket" not in cols:
        conn.execute("ALTER TABLE collections ADD COLUMN bucket TEXT")
        conn.execute("UPDATE collections SET bucket = collected_date "
                     "WHERE bucket IS NULL")
    # v1 had a UNIQUE index on collected_date; drop it so >1 sample/day is OK.
    conn.execute("DROP INDEX IF EXISTS ix_collections_date")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_collections_date "
                 "ON collections(collected_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_collections_bucket "
                 "ON collections(bucket)")
    # Rebuild snapshots to change the PRIMARY KEY (SQLite can't ALTER a PK).
    snap_pk = conn.execute(
        "SELECT 1 FROM pragma_table_info('snapshots') "
        "WHERE name='collected_date' AND pk > 0"
    ).fetchone()
    if snap_pk:  # still the v1 per-day PK -> rebuild
        cols_csv = ", ".join(_SNAP_COLS)
        conn.executescript(
            "CREATE TABLE snapshots__v2 ("
            " collected_at TEXT NOT NULL, collected_date TEXT NOT NULL,"
            " student_id TEXT NOT NULL, course_code TEXT NOT NULL, name TEXT,"
            " student_email TEXT, momentum TEXT, momentum_rank INTEGER,"
            " latest_task_status TEXT, task1 TEXT, task2 TEXT, task3 TEXT,"
            " latest_course_note TEXT, followup_note TEXT, followup_date TEXT,"
            " extra_json TEXT,"
            " PRIMARY KEY (collected_at, student_id, course_code));"
            f"INSERT INTO snapshots__v2 ({cols_csv}) "
            f"SELECT {cols_csv} FROM snapshots;"
            "DROP TABLE snapshots;"
            "ALTER TABLE snapshots__v2 RENAME TO snapshots;"
            "CREATE INDEX IF NOT EXISTS ix_snap_student_at "
            "ON snapshots(student_id, collected_at);"
            "CREATE INDEX IF NOT EXISTS ix_snap_date ON snapshots(collected_date);"
        )


# ----------------------------------------------------------------------
# row mapping
# ----------------------------------------------------------------------
def _candidate_headers(csv_header: str) -> list[str]:
    """A core field can arrive under its API header ('Task1', 'StudentID') or
    the display-name spelling ('Task 1', 'Student ID') depending on the user's
    Caseload view config. Reuse caseload_csv's mapping to accept either, so
    changing the export's column NAMING doesn't drop core fields."""
    disp = caseload_csv.CSV_TO_DISPLAY.get(csv_header)
    cands = [csv_header]
    if disp and disp != csv_header:
        cands.append(disp)
    return cands


_CORE_CANDIDATES = {h: _candidate_headers(h) for h in _CORE_HEADERS}
_CORE_HEADER_ALIASES = {a for cands in _CORE_CANDIDATES.values() for a in cands}


def _get(row: dict, csv_header: str) -> str:
    """First non-empty value among a core field's accepted header spellings,
    stripped; '' if absent/blank."""
    for h in _CORE_CANDIDATES[csv_header]:
        val = row.get(h)
        if isinstance(val, str):
            val = val.strip()
        if val:
            return val
    return ""


def _row_to_record(row: dict) -> Optional[dict]:
    """Map one CSV row dict to a snapshot record (sans collected_at/date).
    Returns None for rows lacking a usable (StudentID, CourseCode) key.
    Accepts API and display-name header spellings; non-core columns -> extra_json."""
    rec = {dest: _get(row, src) for src, dest in _CORE_HEADERS.items()}
    if not rec["student_id"] or not rec["course_code"]:
        return None
    rec["momentum_rank"] = momentum_rank(rec.get("momentum", ""))
    extra = {k: v for k, v in row.items()
             if k and k not in _CORE_HEADER_ALIASES}
    rec["extra_json"] = json.dumps(extra, ensure_ascii=False)
    return rec


def _classify(latest_task_status: str) -> str:
    """A departed student whose last status was 'Passed' likely completed;
    anything else (incl. blank) is treated as needing follow-up."""
    return ("completed" if (latest_task_status or "").strip() == "Passed"
            else "followup")


def _departure_dict(r: sqlite3.Row) -> dict:
    return {
        "student_id": r["student_id"],
        "course_code": r["course_code"],
        "name": r["name"],
        "student_email": r["student_email"],
        "last_task_status": r["latest_task_status"],
        "last_seen_date": r["collected_date"],
        "momentum": r["momentum"],
        "followup_note": r["followup_note"],
        "followup_date": r["followup_date"],
        "classification": _classify(r["latest_task_status"]),
    }


_PRIOR_ROW_COLS = ("student_id, course_code, name, student_email, "
                   "latest_task_status, collected_date, momentum, "
                   "followup_note, followup_date")


# ----------------------------------------------------------------------
# capture
# ----------------------------------------------------------------------
def record_snapshot(rows, csv_mtime, *, interval_hours: int = 24,
                    db_path=HISTORY_DB, note: str = "",
                    now: Optional[datetime] = None) -> dict:
    """Snapshot ``rows`` (the freshly-loaded caseload) into the history DB.

    Sampling rule (bucketed by ``interval_hours``):
      - no sample in this window yet   -> capture (status 'captured')
      - sample exists, same csv mtime  -> skip    (status 'skipped_stale')
      - sample exists, mtime moved     -> replace this window in place ('updated')

    Computes departures vs the most recent *prior-day* collection (gap-aware,
    always day-grained) before writing. Returns a summary dict; never raises
    (errors come back as ``{"status": "error", "error": ...}``).

    ``now`` is a testability seam so tests can simulate distinct days/windows.
    """
    try:
        now = now or datetime.now()
        today = now.date().isoformat()
        collected_at = now.isoformat(timespec="seconds")
        bucket = _bucket(now, interval_hours)
        csv_mtime_iso = (csv_mtime.isoformat(timespec="seconds")
                         if isinstance(csv_mtime, datetime) else None)

        # Map rows up front so we can bail BEFORE touching the DB if the export
        # can't be keyed (e.g. StudentID column dropped from the view) — an
        # unkeyable empty collection would poison the next departure diff.
        records, incoming_keys = [], set()
        for row in rows:
            rec = _row_to_record(row)
            if rec is None:
                continue
            rec["collected_at"] = collected_at
            rec["collected_date"] = today
            records.append(rec)
            incoming_keys.add((rec["student_id"], rec["course_code"]))
        row_count = len(records)
        if rows and not records:
            return {
                "status": "skipped_no_keys", "row_count": 0,
                "departures": [], "departure_count": 0,
                "warning": ("history snapshot skipped: no rows had a "
                            "StudentID / CourseCode — check your export columns"),
            }

        conn = _connect(db_path)
        try:
            with conn:
                cur = conn.cursor()
                existing = cur.execute(
                    "SELECT collected_at, csv_mtime, row_count "
                    "FROM collections WHERE bucket = ?", (bucket,),
                ).fetchone()
                if existing is not None:
                    if (csv_mtime_iso is not None
                            and existing["csv_mtime"] == csv_mtime_iso):
                        return {
                            "status": "skipped_stale",
                            "collected_at": existing["collected_at"],
                            "row_count": existing["row_count"],
                            "departures": [], "departure_count": 0,
                        }
                    # Fresher export in the same window -> replace this sample.
                    cur.execute("DELETE FROM snapshots WHERE collected_at = ?",
                                (existing["collected_at"],))
                    cur.execute("DELETE FROM collections WHERE collected_at = ?",
                                (existing["collected_at"],))
                    status = "updated"
                else:
                    status = "captured"

                departures, prior_count = _departures_vs_prior_day(
                    cur, today, incoming_keys)
                partial = bool(prior_count
                               and row_count < _PARTIAL_EXPORT_FRACTION * prior_count)
                if partial:
                    departures = []  # truncated export — not real attrition

                cur.execute(
                    "INSERT INTO collections"
                    "(collected_at, collected_date, bucket, csv_mtime, "
                    "row_count, note) VALUES (?, ?, ?, ?, ?, ?)",
                    (collected_at, today, bucket, csv_mtime_iso, row_count, note),
                )
                placeholders = ", ".join(":" + c for c in _SNAP_COLS)
                cur.executemany(
                    f"INSERT OR REPLACE INTO snapshots "
                    f"({', '.join(_SNAP_COLS)}) VALUES ({placeholders})",
                    records,
                )
        finally:
            conn.close()

        return {
            "status": status,
            "collected_at": collected_at,
            "row_count": row_count,
            "departures": departures,
            "departure_count": len(departures),
            "partial_export": partial,
        }
    except Exception as e:  # never break the reload path
        return {"status": "error", "error": str(e)}


def _departures_vs_prior_day(cur: sqlite3.Cursor, today: str,
                             incoming_keys: set):
    """(departures, prior_row_count) comparing the most recent collection from
    a calendar day BEFORE ``today`` against ``incoming_keys``. ([], 0) if the
    DB has no earlier-day collection yet."""
    prior = cur.execute(
        "SELECT collected_at, row_count FROM collections "
        "WHERE collected_date < ? ORDER BY collected_at DESC LIMIT 1",
        (today,),
    ).fetchone()
    if prior is None:
        return [], 0
    prior_rows = cur.execute(
        f"SELECT {_PRIOR_ROW_COLS} FROM snapshots WHERE collected_at = ?",
        (prior["collected_at"],),
    ).fetchall()
    deps = [_departure_dict(r) for r in prior_rows
            if (r["student_id"], r["course_code"]) not in incoming_keys]
    return deps, prior["row_count"]


# ----------------------------------------------------------------------
# queries
# ----------------------------------------------------------------------
def find_departures(*, db_path=HISTORY_DB) -> list[dict]:
    """Students present in the most recent prior-day collection but absent in
    the latest one, classified completed/followup. [] if there's no earlier
    day, or if the latest looks like a truncated export (partial-export guard)."""
    conn = _connect(db_path)
    try:
        latest = conn.execute(
            "SELECT collected_at, collected_date, row_count FROM collections "
            "ORDER BY collected_at DESC LIMIT 1"
        ).fetchone()
        if latest is None:
            return []
        prior = conn.execute(
            "SELECT collected_at, row_count FROM collections "
            "WHERE collected_date < ? ORDER BY collected_at DESC LIMIT 1",
            (latest["collected_date"],),
        ).fetchone()
        if prior is None:
            return []
        if prior["row_count"] and latest["row_count"] < (
                _PARTIAL_EXPORT_FRACTION * prior["row_count"]):
            return []
        prior_rows = conn.execute(
            f"SELECT {_PRIOR_ROW_COLS} FROM snapshots WHERE collected_at = ?",
            (prior["collected_at"],),
        ).fetchall()
        latest_keys = {
            (r["student_id"], r["course_code"]) for r in conn.execute(
                "SELECT student_id, course_code FROM snapshots "
                "WHERE collected_at = ?", (latest["collected_at"],),
            ).fetchall()
        }
        return [_departure_dict(r) for r in prior_rows
                if (r["student_id"], r["course_code"]) not in latest_keys]
    finally:
        conn.close()


# ----------------------------------------------------------------------
# outcomes ingest (passed-in-last-30-days archive)
# ----------------------------------------------------------------------
def _val(row: dict, csv_header: str) -> str:
    """First non-empty value among a header's API + display-name spellings,
    stripped; '' if absent. Same robustness as ``_get`` but for any header
    (not just the precomputed core set)."""
    for h in _candidate_headers(csv_header):
        v = row.get(h)
        if isinstance(v, str):
            v = v.strip()
        if v:
            return v
    return ""


# Archive columns we pull into typed outcome fields; everything else is kept
# verbatim in extra_json (so the full ~106-column row is never lost).
_OUTCOME_SRC = [
    "StudentID", "CourseCode", "Name", "StudentEmail", "Momentum",
    "ActualEndDate", "Icenddate", "TermEndDate", "CourseStartDate",
    "TermStartDate", "LatestTaskStatus", "CourseStatus", "OtherCourses",
]

# The archive's CourseStatus column resolves each departed student. Normalize
# to 'passed' / 'not_passed'; an unrecognized value (e.g. a column-shifted row
# where a program name lands in this field) returns None and the row is skipped.
_OUTCOME_STATUS = {
    "passed": "passed",
    "not passed": "not_passed",
    "unenrolled": "not_passed",
    "term end": "not_passed",
    "withdrawn": "not_passed",
    "dropped": "not_passed",
    "failed": "not_passed",
}


def _norm_outcome(course_status: str) -> Optional[str]:
    return _OUTCOME_STATUS.get((course_status or "").strip().lower())


def count_other_courses(raw: str, course_code: str) -> int:
    """Distinct courses in an OtherCourses list OTHER than ``course_code``. 0 if
    blank or the list only echoes the current course. The OtherCourses field
    enumerates every course the student is enrolled in (including this one), so
    'other' = the rest. WGU advises students juggling multiple courses to finish
    the others first, so a non-pass here may be deliberate deprioritization
    rather than a Momentum miss — which is why we track it for calibration."""
    if not raw:
        return 0
    cur = (course_code or "").strip().upper()
    seen = set()
    for tok in str(raw).replace(";", ",").split(","):
        c = tok.strip().upper()
        if c and c != cur:
            seen.add(c)
    return len(seen)
_OUTCOME_ALIASES = {a for h in _OUTCOME_SRC for a in _candidate_headers(h)}

_OUTCOME_COLS = [
    "student_id", "course_code", "name", "student_email",
    "momentum_at_outcome", "momentum_rank_at_outcome", "latest_task_status",
    "outcome", "pass_date", "ic_end_date", "term_end_date",
    "course_start_date", "term_start_date", "other_courses",
    "other_course_count", "entry_momentum", "entry_momentum_rank",
    "entry_captured", "first_seen_at", "last_ingest_at",
    "source_file", "extra_json",
]

# A snapshot within this many days of CourseStart counts as a genuine
# at-enrollment Momentum reading (vs a mid-course, already-adjusted proxy).
_ENTRY_WINDOW_DAYS = 21


def _entry_reading_for(snap_index: dict, key, course_start, window=_ENTRY_WINDOW_DAYS):
    """(momentum, rank, captured) for the snapshot nearest ``course_start`` among
    this student's history. ``captured`` is True only when that nearest snapshot
    is within ``window`` days of the start — i.e. we actually observed them at
    enrollment, so the reading is a fair entry prediction rather than a
    mid-course (self-corrected) proxy. ('', None, False) if we have no usable
    snapshot for them."""
    seq = snap_index.get(key)
    if not seq or course_start is None:
        return ("", None, False)
    best, best_gap = None, None
    for d, mom, rank in seq:
        dt = _parse_date(d)
        if dt is None:
            continue
        gap = abs((dt - course_start).days)
        if best_gap is None or gap < best_gap:
            best, best_gap = (mom, rank), gap
    if best is None:
        return ("", None, False)
    return (best[0] or "", best[1], best_gap <= window)

# UPSERT: refresh every field on conflict EXCEPT first_seen_at, so we keep the
# timestamp at which an outcome first entered our records.
_OUTCOME_UPSERT = (
    f"INSERT INTO outcomes ({', '.join(_OUTCOME_COLS)}) "
    f"VALUES ({', '.join(':' + c for c in _OUTCOME_COLS)}) "
    "ON CONFLICT(student_id, course_code) DO UPDATE SET "
    + ", ".join(f"{c}=excluded.{c}" for c in _OUTCOME_COLS
                if c not in ("student_id", "course_code", "first_seen_at"))
)


def ingest_outcomes_csv(path, *, db_path=HISTORY_DB,
                        now: Optional[datetime] = None) -> dict:
    """Ingest a WGU "passed in the last 30 days" archive CSV into ``outcomes``.

    Upserts one row per (student, course): a re-downloaded archive refreshes
    existing rows (preserving ``first_seen_at``) and adds newly-passed students.
    Because the archive is a rolling 30-day window, downloading at least every
    ~30 days guarantees no passer is missed.

    Returns a summary dict (``status`` 'ok' | 'empty' | 'error'); never raises.
    """
    try:
        now = now or datetime.now()
        ts = now.isoformat(timespec="seconds")
        try:
            archive_mtime = datetime.fromtimestamp(
                Path(path).stat().st_mtime).isoformat(timespec="seconds")
        except Exception:
            archive_mtime = ts
        records = []
        skipped = 0
        with open(path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                sid, cc = _val(row, "StudentID"), _val(row, "CourseCode")
                if not sid or not cc:
                    continue
                status = _norm_outcome(_val(row, "CourseStatus"))
                if status is None:
                    skipped += 1   # blank/unrecognized status (e.g. shifted row)
                    continue
                mom = _val(row, "Momentum")
                extra = {k: v for k, v in row.items()
                         if k and k not in _OUTCOME_ALIASES}
                records.append({
                    "student_id": sid, "course_code": cc,
                    "name": _val(row, "Name"),
                    "student_email": _val(row, "StudentEmail"),
                    "momentum_at_outcome": mom,
                    "momentum_rank_at_outcome": momentum_rank(mom),
                    "latest_task_status": _val(row, "LatestTaskStatus"),
                    "outcome": status,
                    "pass_date": _val(row, "ActualEndDate"),
                    "ic_end_date": _val(row, "Icenddate"),
                    "term_end_date": _val(row, "TermEndDate"),
                    "course_start_date": _val(row, "CourseStartDate"),
                    "term_start_date": _val(row, "TermStartDate"),
                    "other_courses": _val(row, "OtherCourses"),
                    "other_course_count": count_other_courses(
                        _val(row, "OtherCourses"), cc),
                    # Filled from the snapshot index inside the txn below.
                    "entry_momentum": "", "entry_momentum_rank": None,
                    "entry_captured": 0,
                    "first_seen_at": ts, "last_ingest_at": ts,
                    "source_file": Path(path).name,
                    "extra_json": json.dumps(extra, ensure_ascii=False),
                })
        if not records:
            return {"status": "empty", "ingested": 0, "new": 0, "updated": 0,
                    "skipped": skipped, "file": Path(path).name}
        n_passed = sum(1 for r in records if r["outcome"] == "passed")
        n_not_passed = len(records) - n_passed

        conn = _connect(db_path)
        try:
            with conn:
                # Freeze each student's ENTRY Momentum (reading nearest their
                # CourseStart) onto the outcome — the only fair basis for a
                # performance-vs-prediction assessment, captured durably now so
                # it survives even if snapshots are later pruned.
                snap_index: dict = {}
                for r in conn.execute(
                        "SELECT student_id, course_code, collected_date, "
                        "momentum, momentum_rank FROM snapshots "
                        "ORDER BY collected_date ASC"):
                    snap_index.setdefault(
                        (r["student_id"], r["course_code"]), []).append(
                            (r["collected_date"], r["momentum"],
                             r["momentum_rank"]))
                for rec in records:
                    mom, rank, captured = _entry_reading_for(
                        snap_index, (rec["student_id"], rec["course_code"]),
                        _parse_date(rec["course_start_date"]))
                    rec["entry_momentum"] = mom
                    rec["entry_momentum_rank"] = rank
                    rec["entry_captured"] = 1 if captured else 0

                existing = {(r["student_id"], r["course_code"]) for r in
                            conn.execute("SELECT student_id, course_code "
                                         "FROM outcomes")}
                new = sum(1 for r in records
                          if (r["student_id"], r["course_code"]) not in existing)
                conn.executemany(_OUTCOME_UPSERT, records)
                conn.execute("INSERT OR REPLACE INTO meta(key, value) "
                             "VALUES('outcomes_last_ingest_at', ?)", (ts,))
                conn.execute("INSERT OR REPLACE INTO meta(key, value) "
                             "VALUES('outcomes_last_ingest_file', ?)",
                             (Path(path).name,))
                conn.execute("INSERT OR REPLACE INTO meta(key, value) "
                             "VALUES('outcomes_last_ingest_count', ?)",
                             (str(len(records)),))
                # The archive file's mtime = when the user downloaded it. That,
                # not our ingest run time, is what the staleness reminder keys
                # off — re-ingesting an old file shouldn't reset the clock.
                conn.execute("INSERT OR REPLACE INTO meta(key, value) "
                             "VALUES('outcomes_archive_mtime', ?)",
                             (archive_mtime,))
                total = conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0]
        finally:
            conn.close()
        return {"status": "ok", "ingested": len(records), "new": new,
                "updated": len(records) - new, "total_outcomes": total,
                "passed": n_passed, "not_passed": n_not_passed,
                "skipped": skipped, "file": Path(path).name}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def _meta_get(conn: sqlite3.Connection, key: str) -> Optional[str]:
    r = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return r["value"] if r else None


def last_outcomes_ingest(*, db_path=HISTORY_DB) -> Optional[datetime]:
    """Timestamp of the most recent outcomes-archive ingest, or None if never."""
    conn = _connect(db_path)
    try:
        v = _meta_get(conn, "outcomes_last_ingest_at")
    finally:
        conn.close()
    if not v:
        return None
    try:
        return datetime.fromisoformat(v)
    except Exception:
        return None


def outcomes_archive_mtime(*, db_path=HISTORY_DB) -> Optional[datetime]:
    """Download time (file mtime) of the most recently ingested archive, or
    None if never ingested. This is the age of the *data*, which is what the
    staleness reminder should reflect."""
    conn = _connect(db_path)
    try:
        v = (_meta_get(conn, "outcomes_archive_mtime")
             or _meta_get(conn, "outcomes_last_ingest_at"))
    finally:
        conn.close()
    if not v:
        return None
    try:
        return datetime.fromisoformat(v)
    except Exception:
        return None


def outcomes_stale_days(*, db_path=HISTORY_DB,
                        now: Optional[datetime] = None) -> Optional[int]:
    """Whole days since the most recently ingested archive was *downloaded*
    (file mtime), or None if never ingested. The caller decides what to do at
    the never/stale thresholds. Keyed off download time so re-ingesting an old
    file doesn't falsely reset the reminder."""
    mt = outcomes_archive_mtime(db_path=db_path)
    if mt is None:
        return None
    return ((now or datetime.now()) - mt).days


def outcomes_count(*, db_path=HISTORY_DB) -> int:
    conn = _connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0]
    finally:
        conn.close()


def outcomes_entry_coverage(*, db_path=HISTORY_DB) -> dict:
    """How many resolved outcomes we can FAIRLY assess — i.e. for which we
    froze a genuine at-entry Momentum reading (``entry_captured``). The rest
    started before we were tracking them, so only their self-corrected later
    Momentum is known. Returns {total, captured}."""
    conn = _connect(db_path)
    try:
        total = conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0]
        captured = conn.execute(
            "SELECT COUNT(*) FROM outcomes WHERE entry_captured = 1"
        ).fetchone()[0]
        # Drift: of students with both an entry and an exit reading, how many
        # had Momentum CHANGE — the evidence that exit Momentum can't fairly
        # measure performance against the prediction.
        both = conn.execute(
            "SELECT COUNT(*) FROM outcomes WHERE entry_captured = 1 "
            "AND entry_momentum_rank IS NOT NULL "
            "AND momentum_rank_at_outcome IS NOT NULL").fetchone()[0]
        drifted = conn.execute(
            "SELECT COUNT(*) FROM outcomes WHERE entry_captured = 1 "
            "AND entry_momentum_rank IS NOT NULL "
            "AND momentum_rank_at_outcome IS NOT NULL "
            "AND entry_momentum_rank != momentum_rank_at_outcome").fetchone()[0]
    finally:
        conn.close()
    return {"total": total, "captured": captured,
            "both": both, "drifted": drifted}


def all_outcomes(*, db_path=HISTORY_DB) -> list[dict]:
    """Every recorded outcome (for the calibration report). One row per
    (student, course)."""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM outcomes ORDER BY course_code, student_id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ----------------------------------------------------------------------
# momentum calibration (entry prediction vs actual outcome)
# ----------------------------------------------------------------------
def _parse_date(s):
    """Lenient date parse for the assorted CSV date spellings; None on failure."""
    if not s:
        return None
    s = str(s).strip()
    for cand in (s, s[:10]):
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%d/%m/%Y"):
            try:
                return datetime.strptime(cand, fmt).date()
            except Exception:
                pass
    return None


def _ej_get(ej: dict, header: str) -> str:
    """Pull a non-core field from a snapshot's extra_json, tolerating API vs
    display-name header spellings."""
    for h in _candidate_headers(header):
        v = ej.get(h)
        if isinstance(v, str):
            v = v.strip()
        if v:
            return v
    return ""


# Predicted pass-probability range (%) for each Momentum band, per WGU's model.
_MOMENTUM_BANDS = [
    (5, "High",     "80-100"),
    (4, "Med High", "60-80"),
    (3, "Med",      "40-60"),
    (2, "Med Low",  "20-40"),
    (1, "Low",      "0-20"),
]


def momentum_calibration(*, db_path=HISTORY_DB, eligible_from="2026-06-10",
                         course_load="all",
                         now: Optional[datetime] = None) -> dict:
    """Compare each student's ENTRY-time Momentum band to their actual outcome.

    Entry momentum = the snapshot reading nearest the student's CourseStartDate
    (for a course that started after we began collecting, that's the genuine
    at-enrollment reading; ``eligible_from`` restricts the cohort to those).

    Outcome per (student, course):
      - **passed (in time)**  — in the outcomes archive AND pass_date is on/before
        the deadline (IC End Date if present, else Term End Date),
      - **passed (late)**     — passed but after that deadline,
      - **missed deadline**   — not passed and the deadline is already in the past,
      - **in progress**       — not passed and the deadline hasn't arrived (or is
        unknown); not yet a resolved outcome.

    The calibration pass rate per band = passed-in-time / resolved (resolved =
    passed-in-time + passed-late + missed); in-progress is excluded from the
    rate. Returns a dict with an ordered ``bands`` table plus cohort metadata.
    ``eligible_from`` of an early date (e.g. '1900-01-01') includes everyone,
    using each student's earliest snapshot as a (mid-course, imperfect) proxy.
    """
    from collections import defaultdict
    today = (now or datetime.now()).date()
    elig = _parse_date(eligible_from)

    conn = _connect(db_path)
    try:
        snaps = conn.execute(
            "SELECT student_id, course_code, collected_date, momentum, "
            "momentum_rank, extra_json FROM snapshots ORDER BY collected_at ASC"
        ).fetchall()
        outcomes = {(r["student_id"], r["course_code"]): dict(r)
                    for r in conn.execute("SELECT * FROM outcomes")}
        latest = conn.execute("SELECT collected_at FROM collections "
                              "ORDER BY collected_at DESC LIMIT 1").fetchone()
        latest_keys = set()
        if latest is not None:
            latest_keys = {(r["student_id"], r["course_code"]) for r in
                           conn.execute("SELECT student_id, course_code FROM "
                                        "snapshots WHERE collected_at = ?",
                                        (latest["collected_at"],))}
    finally:
        conn.close()

    per = defaultdict(list)
    for r in snaps:
        per[(r["student_id"], r["course_code"])].append(r)

    # Tally counters per band rank, plus cohort-level bookkeeping. The archive's
    # CourseStatus gives an authoritative negative class ("not_passed"); a
    # departed student not yet in any archive stays "in_progress" until the next
    # archive resolves them. "missed" = still on caseload but past the deadline.
    cells = {rank: {"passed_in_time": 0, "passed_late": 0, "not_passed": 0,
                    "missed": 0, "in_progress": 0}
             for rank, _, _ in _MOMENTUM_BANDS}
    eligible = no_entry_band = not_started = 0
    skipped_no_start = skipped_pre_window = 0
    rows_detail = []  # per-student, for CSV export
    _RANK_LABEL = {rank: label for rank, label, _ in _MOMENTUM_BANDS}

    keys = set(per) | set(outcomes)
    for key in keys:
        rows = per.get(key, [])
        oc = outcomes.get(key)
        latest_ej = json.loads(rows[-1]["extra_json"] or "{}") if rows else {}

        # Course start (eligibility). Prefer a snapshot's value; fall back to the
        # outcome row for a passer we never snapshotted.
        cs_raw = _ej_get(latest_ej, "CourseStartDate") or (
            oc.get("course_start_date") if oc else "")
        cs = _parse_date(cs_raw)
        if cs is None:
            skipped_no_start += 1
            continue
        if elig is not None and cs < elig:
            skipped_pre_window += 1
            continue
        # Course-load filter: students juggling other courses are often told to
        # finish those first, so their outcome here isn't a clean test of the
        # prediction. 'single' = only this course, 'multi' = 1+ others.
        oc_raw = ((oc.get("other_courses") if oc else "")
                  or _ej_get(latest_ej, "OtherCourses"))
        other_count = count_other_courses(oc_raw, key[1])
        if course_load == "single" and other_count != 0:
            continue
        if course_load == "multi" and other_count < 1:
            continue
        eligible += 1

        # Entry momentum = reading nearest CourseStartDate (earliest if no dates).
        entry_rank = None
        if rows:
            dated = [(r, _parse_date(r["collected_date"])) for r in rows]
            dated = [(r, d) for r, d in dated if d is not None]
            if dated:
                best = min(dated, key=lambda rd: abs((rd[1] - cs).days))[0]
                entry_rank = best["momentum_rank"]
        if entry_rank is None:
            no_entry_band += 1
            continue

        # Deadline = IC End Date (extension) if present, else Term End Date.
        ic = (oc.get("ic_end_date") if oc else "") or _ej_get(latest_ej, "Icenddate")
        te = (oc.get("term_end_date") if oc else "") or _ej_get(latest_ej, "TermEndDate")
        deadline = _parse_date(ic) or _parse_date(te)

        if oc is not None:
            if (oc.get("outcome") or "") == "not_passed":
                outcome_label = "not_passed"    # authoritative, from CourseStatus
            else:
                pd = _parse_date(oc.get("pass_date"))
                if deadline and pd and pd > deadline:
                    outcome_label = "passed_late"
                else:
                    outcome_label = "passed_in_time"
        elif deadline and deadline < today and key in latest_keys:
            outcome_label = "missed"            # still here but past the deadline
        else:
            # Not yet resolved — still on caseload pre-deadline, or departed but
            # not yet in an archive (the next archive download will resolve it).
            outcome_label = "in_progress"
            if cs > today:
                not_started += 1
        cells[entry_rank][outcome_label] += 1
        rows_detail.append({
            "student_id": key[0], "course_code": key[1],
            "name": (_ej_get(latest_ej, "Name")
                     or (oc.get("name") if oc else "") or ""),
            "entry_band": _RANK_LABEL.get(entry_rank, ""),
            "entry_rank": entry_rank,
            "outcome": outcome_label,
            "course_start": cs.isoformat() if cs else "",
            "deadline": deadline.isoformat() if deadline else "",
            "pass_date": (oc.get("pass_date") if oc else "") or "",
            "other_course_count": other_count,
            "other_courses": oc_raw,
        })

    bands = []
    for rank, label, rng in _MOMENTUM_BANDS:
        c = cells[rank]
        passed = c["passed_in_time"] + c["passed_late"]
        resolved = passed + c["not_passed"] + c["missed"]
        total = resolved + c["in_progress"]
        rate = (c["passed_in_time"] / resolved) if resolved else None
        bands.append({
            "rank": rank, "label": label, "predicted_range": rng,
            "passed_in_time": c["passed_in_time"],
            "passed_late": c["passed_late"], "not_passed": c["not_passed"],
            "missed": c["missed"],
            "in_progress": c["in_progress"], "resolved": resolved,
            "total": total,
            "pass_in_time_rate": rate,
        })

    return {
        "eligible_from": eligible_from,
        "course_load": course_load,
        "as_of": today.isoformat(),
        "bands": bands,
        "eligible_total": eligible,
        "not_started": not_started,
        "no_entry_band": no_entry_band,
        "skipped_no_start": skipped_no_start,
        "skipped_pre_window": skipped_pre_window,
        "rows": rows_detail,
    }


def momentum_calibration_at_exit(*, db_path=HISTORY_DB, course_load="all",
                                 now: Optional[datetime] = None) -> dict:
    """Calibration of Momentum AS RECORDED IN THE ARCHIVE (at course exit) vs.
    the actual outcome — i.e. how well the *final* Momentum reading matched the
    result. Unlike the entry view this needs no snapshot history, so every
    resolved outcome counts (high volume immediately). Directly tests whether
    Momentum self-corrects toward the outcome by the end of the course.

    Same band-table shape as ``momentum_calibration`` (missed/in_progress are
    always 0 here — every archive row is a resolved outcome)."""
    today = (now or datetime.now()).date()
    conn = _connect(db_path)
    try:
        outs = [dict(r) for r in conn.execute("SELECT * FROM outcomes")]
    finally:
        conn.close()

    cells = {rank: {"passed_in_time": 0, "passed_late": 0, "not_passed": 0}
             for rank, _, _ in _MOMENTUM_BANDS}
    no_band = 0
    for oc in outs:
        cnt = oc.get("other_course_count")
        if cnt is None:
            cnt = count_other_courses(oc.get("other_courses") or "",
                                      oc.get("course_code"))
        if course_load == "single" and cnt:
            continue
        if course_load == "multi" and not cnt:
            continue
        rank = oc.get("momentum_rank_at_outcome")
        if rank not in cells:
            no_band += 1
            continue
        if (oc.get("outcome") or "") == "not_passed":
            cells[rank]["not_passed"] += 1
        else:
            deadline = (_parse_date(oc.get("ic_end_date"))
                        or _parse_date(oc.get("term_end_date")))
            pd = _parse_date(oc.get("pass_date"))
            if deadline and pd and pd > deadline:
                cells[rank]["passed_late"] += 1
            else:
                cells[rank]["passed_in_time"] += 1

    bands = []
    for rank, label, rng in _MOMENTUM_BANDS:
        c = cells[rank]
        resolved = c["passed_in_time"] + c["passed_late"] + c["not_passed"]
        rate = (c["passed_in_time"] / resolved) if resolved else None
        bands.append({
            "rank": rank, "label": label, "predicted_range": rng,
            "passed_in_time": c["passed_in_time"],
            "passed_late": c["passed_late"], "not_passed": c["not_passed"],
            "missed": 0, "in_progress": 0, "resolved": resolved,
            "total": resolved, "pass_in_time_rate": rate,
        })
    return {
        "eligible_from": None, "course_load": course_load,
        "as_of": today.isoformat(), "bands": bands,
        "eligible_total": sum(b["resolved"] for b in bands) + no_band,
        "not_started": 0,
        "no_entry_band": no_band, "skipped_no_start": 0,
        "skipped_pre_window": 0, "rows": [],
    }


def export_calibration_csv(dest_path, *, db_path=HISTORY_DB,
                           eligible_from="2026-06-10", course_load="all",
                           now: Optional[datetime] = None) -> int:
    """Write the per-student calibration detail (entry band + outcome) to CSV.
    Returns the row count. Raises on write error (caller reports)."""
    data = momentum_calibration(db_path=db_path, eligible_from=eligible_from,
                                course_load=course_load, now=now)
    cols = ["student_id", "course_code", "name", "entry_band", "entry_rank",
            "outcome", "course_start", "deadline", "pass_date",
            "other_course_count", "other_courses"]
    rows = data["rows"]
    with open(dest_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return len(rows)


def student_timeline(student_id: str, *, db_path=HISTORY_DB) -> list[dict]:
    """Chronological snapshots for one student (for the deferred timeline UI)."""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT collected_date, collected_at, course_code, momentum, "
            "momentum_rank, latest_task_status, task1, task2, task3, "
            "followup_note FROM snapshots WHERE student_id = ? "
            "ORDER BY collected_at ASC, course_code ASC",
            (student_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def task_stall_days(*, db_path=HISTORY_DB,
                    now: Optional[datetime] = None) -> dict:
    """Whole days that each student's task status has been unchanged, keyed
    ``{(student_id, course_code): days}``.

    Derived from the snapshot history: for each student+course, take the most
    recent contiguous run of an identical ``latest_task_status`` and measure
    from the first date of that run to today. So a student whose status last
    changed 17 days ago reads 17; one who changed today reads 0. Keys with no
    snapshots are omitted. Day-grained (matches the snapshot cadence).
    """
    from itertools import groupby
    today = (now or datetime.now()).date()
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT student_id, course_code, collected_date, latest_task_status "
            "FROM snapshots "
            "ORDER BY student_id, course_code, collected_at ASC"
        ).fetchall()
    finally:
        conn.close()
    out: dict = {}
    keyf = lambda r: (r["student_id"], r["course_code"])
    for key, grp in groupby(rows, key=keyf):
        seq = list(grp)
        latest = (seq[-1]["latest_task_status"] or "")
        run_start = seq[-1]["collected_date"]
        for r in reversed(seq):  # walk back while the status is unchanged
            if (r["latest_task_status"] or "") == latest:
                run_start = r["collected_date"]
            else:
                break
        try:
            d = datetime.strptime(run_start[:10], "%Y-%m-%d").date()
            out[key] = (today - d).days
        except Exception:
            pass
    return out


def export_to_csv(dest_path, *, db_path=HISTORY_DB) -> int:
    """Dump the whole snapshots table to a CSV at ``dest_path``. Returns the
    number of data rows written. Raises on a write error (caller reports it)."""
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            "SELECT * FROM snapshots "
            "ORDER BY collected_at, student_id, course_code"
        )
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
