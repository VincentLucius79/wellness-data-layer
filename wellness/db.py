"""SQLite helpers. One connection factory, one schema applier, one audit log."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"

# Columns whose old name lied about what they hold: (table, old, new).
# schema.sql runs on every command and SQLite has no RENAME COLUMN IF EXISTS,
# so the rename lives here and runs only while the old name is still there.
RENAMES = (
    # GarminDB writes weight in the unit system it is configured for. On the
    # install this was written for that is pounds: its figure was Withings'
    # same-morning kg times 2.2046.
    ("garmin_daily", "weight_kg", "weight_lb"),
    # GarminDB's activities.start_time is naive local wall-clock time. It
    # trails Tonal's UTC begin times by exactly the CST/CDT offset.
    ("garmin_activity", "start_utc", "start_local"),
)

# Columns added after their table existed: (table, column, declaration).
# CREATE TABLE IF NOT EXISTS won't add a column to an existing table, so
# db.add_columns does, once, before the schema script runs.
ADDED_COLUMNS = (
    ("annotations", "label", "TEXT"),
)

# Tables replaced by a new shape under the same name: (table, a column only
# the old shape has, the name the old table is kept under, its indexes).
# CREATE TABLE IF NOT EXISTS can't change a table, and nothing is ever
# deleted, so the old table is renamed aside, never dropped. Its indexes are
# dropped (they hold no data) so the new table can reuse their names.
RETIRED = (
    # Keyed by workout_id:position and rewritten on every load. tonal.migrate
    # refills the new tonal_set from this table's raw_json.
    ("tonal_set", "set_index", "tonal_set_v1",
     ("ix_tonal_set_workout", "ix_tonal_set_movement")),
)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db_path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    db_path = Path(db_path)
    if read_only:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    if not read_only:
        conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}


def rename_columns(conn: sqlite3.Connection) -> list[str]:
    """Apply RENAMES to a database created under the old names. A no-op on a
    new or already-renamed database. Returns what it renamed."""
    todo = [(t, old, new) for t, old, new in RENAMES
            if old in (cols := _columns(conn, t)) and new not in cols]
    if not todo:
        return []
    conn.commit()           # as executescript does, so BEGIN can't nest
    conn.execute("BEGIN")
    try:
        for table, old, new in todo:
            conn.execute(f'ALTER TABLE "{table}" RENAME COLUMN "{old}" TO "{new}"')
            # The provenance map names target columns too. The next garmin
            # sync rewrites it, but until then it would name a missing column.
            conn.execute(
                "UPDATE garmin_column_map SET target_column = ? "
                "WHERE target_table = ? AND target_column = ?", (new, table, old))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return [f"{t}.{old} -> {new}" for t, old, new in todo]


def retire_tables(conn: sqlite3.Connection) -> list[str]:
    """Move each old-shape table in RETIRED aside, so the schema script can
    create the new one. A no-op once done, or on a new database."""
    todo = [r for r in RETIRED
            if r[1] in _columns(conn, r[0]) and not _columns(conn, r[2])]
    if not todo:
        return []
    conn.commit()
    conn.execute("BEGIN")
    try:
        for table, _marker, kept_as, indexes in todo:
            for ix in indexes:
                conn.execute(f'DROP INDEX IF EXISTS "{ix}"')
            conn.execute(f'ALTER TABLE "{table}" RENAME TO "{kept_as}"')
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return [f"{t} -> {kept}" for t, _m, kept, _i in todo]


def add_columns(conn: sqlite3.Connection) -> list[str]:
    """Apply ADDED_COLUMNS to a database whose table predates the column.
    A no-op on a new database, where the schema script creates it."""
    done = []
    for table, column, decl in ADDED_COLUMNS:
        cols = _columns(conn, table)
        if cols and column not in cols:
            conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {decl}')
            done.append(f"{table}.{column}")
    if done:
        conn.commit()
    return done


def apply_schema(conn: sqlite3.Connection) -> None:
    # Before the script, so its views and dictionary see the new names.
    rename_columns(conn)
    retire_tables(conn)
    add_columns(conn)
    conn.executescript(SCHEMA_PATH.read_text())
    conn.commit()


@contextmanager
def sync_run(conn: sqlite3.Connection, source: str, *, source_file: str | None = None,
             source_sha256: str | None = None):
    """Open an audit row, hand back a mutable result dict, always close the row.

    The caller sets result['status'], ['rows_seen'], ['rows_upserted'],
    ['anomalies'] (list of str) and ['notes']. An exception is recorded as
    'error' and re-raised.
    """
    cur = conn.execute(
        "INSERT INTO sync_runs (source, started_utc, status, source_file, source_sha256) "
        "VALUES (?, ?, 'running', ?, ?)",
        (source, utc_now(), source_file, source_sha256),
    )
    run_id = cur.lastrowid
    conn.commit()

    result: dict = {
        "run_id": run_id,
        "status": "error",
        "rows_seen": None,
        "rows_upserted": None,
        "anomalies": [],
        "notes": None,
    }
    try:
        yield result
    except Exception as exc:  # noqa: BLE001 - recorded then re-raised
        result["status"] = "error"
        result["anomalies"].append(f"{type(exc).__name__}: {exc}")
        raise
    finally:
        conn.execute(
            "UPDATE sync_runs SET finished_utc = ?, status = ?, rows_seen = ?, "
            "rows_upserted = ?, anomalies = ?, notes = ? WHERE run_id = ?",
            (
                utc_now(),
                result["status"],
                result["rows_seen"],
                result["rows_upserted"],
                json.dumps(result["anomalies"]),
                result["notes"],
                run_id,
            ),
        )
        conn.commit()


def last_good_run(conn: sqlite3.Connection, source: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM sync_runs WHERE source = ? AND status IN ('ok', 'ok_warn') "
        "ORDER BY run_id DESC LIMIT 1",
        (source,),
    ).fetchone()
