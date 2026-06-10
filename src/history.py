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

_SCHEMA_VERSION = 2

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
"""


def _init_schema(conn: sqlite3.Connection) -> None:
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    has_tables = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='collections'"
    ).fetchone() is not None
    # Upgrade an EXISTING older schema first (so the DDL below, which indexes
    # the new 'bucket' column, doesn't run against a pre-bucket table).
    if has_tables and v < _SCHEMA_VERSION:
        _migrate_v1_to_v2(conn)
    conn.executescript(_SCHEMA_DDL)  # create from scratch / fill in any gaps
    conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
    conn.commit()


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
