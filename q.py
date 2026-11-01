#!/usr/bin/env python3
"""Run SQL against wellness.db from the shell.

    python3 q.py                                  # list tables and views
    python3 q.py "SELECT * FROM v_segmental LIMIT 5"
    python3 q.py "UPDATE withings_position SET segment='arm_left' WHERE position=2"
    python3 q.py --dict                           # the plain-language data dictionary

Quote the whole statement in single quotes if it contains double quotes, and
vice versa. Writes commit automatically.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from wellness import config  # noqa: E402

MAX_CELL = 44


def _fmt(v) -> str:
    s = "" if v is None else str(v)
    return s if len(s) <= MAX_CELL else s[: MAX_CELL - 1] + "…"


def show(rows) -> None:
    if not rows:
        print("(no rows)")
        return
    cols = list(rows[0].keys())
    widths = [
        max(len(c), max(len(_fmt(r[c])) for r in rows)) for c in cols
    ]
    print("  ".join(c.ljust(w) for c, w in zip(cols, widths)))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print("  ".join(_fmt(r[c]).ljust(w) for c, w in zip(cols, widths)))
    print(f"\n{len(rows)} row(s)")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cfg = config.load()
    if not Path(cfg.wellness_db).exists():
        raise SystemExit(f"No database at {cfg.wellness_db}. Run: python3 sync_all.py init")

    conn = sqlite3.connect(cfg.wellness_db)
    conn.row_factory = sqlite3.Row

    if not argv:
        show(conn.execute(
            "SELECT type, name FROM sqlite_master WHERE type IN ('table','view') "
            "AND name NOT LIKE 'sqlite_%' ORDER BY type DESC, name"
        ).fetchall())
        return 0

    if argv[0] in ("--dict", "-d"):
        show(conn.execute(
            "SELECT object_name, column_name, description FROM data_dictionary "
            "ORDER BY object_name, column_name"
        ).fetchall())
        return 0

    sql = " ".join(argv)
    try:
        cur = conn.execute(sql)
    except sqlite3.Error as exc:
        raise SystemExit(f"SQL error: {exc}")

    rows = cur.fetchall()
    if cur.description:
        show(rows)
    else:
        conn.commit()
        print(f"ok - {cur.rowcount} row(s) affected")
    return 0


if __name__ == "__main__":
    sys.exit(main())
