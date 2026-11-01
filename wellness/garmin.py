"""Garmin loader: copies GarminDB's own SQLite files into wellness.db.

GarminDB stays the owner of its databases - we open them read-only and never
write. Its table and column names vary by version and by which Connect IQ
plugins you have, so nothing here is hardcoded to a schema: targets are
resolved by introspection and whatever was actually chosen is recorded in
garmin_column_map, so a number that looks wrong is traceable to its source.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .db import connect, utc_now

DAY_KEYS = ("day", "date", "cal_date")

# target column -> (candidate source column names, required substring in table name)
DAILY_TARGETS: dict[str, tuple[tuple[str, ...], str | None]] = {
    "rhr":                 (("rhr", "resting_heart_rate"), None),
    "hr_min":              (("hr_min", "min_hr", "heart_rate_min"), None),
    "hr_max":              (("hr_max", "max_hr", "heart_rate_max"), None),
    "steps":               (("steps", "steps_total"), None),
    "intensity_time_s":    (("intensity_time", "intensity_minutes", "intensity_time_total"), None),
    "moderate_activity_s": (("moderate_activity_time", "moderate_activity"), None),
    "vigorous_activity_s": (("vigorous_activity_time", "vigorous_activity"), None),
    "calories_total":      (("calories_total", "calories_burned", "calories"), None),
    # All-day stress. GarminDB's sleep table also has avg_stress - stress
    # during sleep only - and is created before daily_summary, so without
    # the hint it is found first and the column silently means something else.
    "stress_avg":          (("stress_avg", "avg_stress", "stress_level_avg"), "summary"),
    "bb_min":              (("bb_min", "body_battery_min"), None),
    "bb_max":              (("bb_max", "body_battery_max"), None),
    "sleep_total_s":       (("total_sleep", "sleep_total", "total_sleep_time"), "sleep"),
    "sleep_deep_s":        (("deep_sleep", "sleep_deep"), "sleep"),
    "sleep_rem_s":         (("rem_sleep", "sleep_rem"), "sleep"),
    "sleep_light_s":       (("light_sleep", "sleep_light"), "sleep"),
    "sleep_score":         (("sleep_score", "score"), "sleep"),
    # Pounds: GarminDB stores weight in the account's unit system.
    "weight_lb":           (("weight",), "weight"),
}

DURATION_TARGETS = {
    "intensity_time_s", "moderate_activity_s", "vigorous_activity_s",
    "sleep_total_s", "sleep_deep_s", "sleep_rem_s", "sleep_light_s",
}

# Garmin writes a negative code where it could not measure: stress uses -1
# (not enough data) and -2 (too active). Those are NULL, not a stress level.
NEGATIVE_MEANS_MISSING = {"stress_avg"}

ACTIVITY_TARGETS: dict[str, tuple[str, ...]] = {
    # Local wall-clock time with no offset, as GarminDB writes it.
    "start_local":   ("start_time", "timestamp", "start"),
    "sport":         ("sport",),
    "sub_sport":     ("sub_sport",),
    "name":          ("name", "description"),
    "elapsed_s":     ("elapsed_time",),
    "moving_s":      ("moving_time",),
    "calories":      ("calories",),
    "avg_hr":        ("avg_hr", "average_hr"),
    "max_hr":        ("max_hr",),
    "training_load": ("training_load", "training_effect"),
}


# ------------------------------------------------------------- introspection

def discover(cfg) -> dict[str, dict[str, list[str]]]:
    """Map available GarminDB files -> tables -> column names."""
    found: dict[str, dict[str, list[str]]] = {}
    for fname in cfg.garmin.get("db_files", []):
        path = Path(cfg.garmin_db_dir) / fname
        if not path.exists():
            continue
        try:
            src = connect(path, read_only=True)
        except sqlite3.Error:
            continue
        tables: dict[str, list[str]] = {}
        for (tname,) in src.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
        ):
            cols = [r[1] for r in src.execute(f'PRAGMA table_info("{tname}")')]
            if cols:
                tables[tname] = cols
        src.close()
        found[fname] = tables
    return found


def _day_column(cols: list[str]) -> str | None:
    lower = {c.lower(): c for c in cols}
    for k in DAY_KEYS:
        if k in lower:
            return lower[k]
    return None


def resolve_daily(found: dict) -> tuple[dict[str, tuple[str, str, str, str]], list[str]]:
    """target -> (db_file, table, day_col, source_col). Plus unmapped targets."""
    chosen: dict[str, tuple[str, str, str, str]] = {}
    for target, (candidates, hint) in DAILY_TARGETS.items():
        for fname, tables in found.items():
            for tname, cols in tables.items():
                if hint and hint not in tname.lower():
                    continue
                day_col = _day_column(cols)
                if not day_col:
                    continue
                lower = {c.lower(): c for c in cols}
                for cand in candidates:
                    if cand in lower:
                        chosen[target] = (fname, tname, day_col, lower[cand])
                        break
                if target in chosen:
                    break
            if target in chosen:
                break
    unmapped = [t for t in DAILY_TARGETS if t not in chosen]
    return chosen, unmapped


def resolve_activities(found: dict) -> tuple[str, str, dict[str, str]] | None:
    """Find the activities table and its column mapping."""
    best = None
    for fname, tables in found.items():
        for tname, cols in tables.items():
            lower = {c.lower(): c for c in cols}
            if "activity_id" not in lower:
                continue
            mapping = {}
            for target, candidates in ACTIVITY_TARGETS.items():
                for cand in candidates:
                    if cand in lower:
                        mapping[target] = lower[cand]
                        break
            if best is None or len(mapping) > len(best[2]):
                best = (fname, tname, mapping)
    return best


# ---------------------------------------------------------------- coercion

def to_seconds(v):
    """GarminDB stores some durations as 'HH:MM:SS' text and some as numbers."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip()
    if ":" in s:
        parts = s.split(":")
        try:
            parts = [float(p) for p in parts]
        except ValueError:
            return None
        while len(parts) < 3:
            parts.insert(0, 0.0)
        return int(parts[0] * 3600 + parts[1] * 60 + parts[2])
    try:
        return int(float(s))
    except ValueError:
        return None


def _day_str(v) -> str | None:
    if v is None:
        return None
    return str(v)[:10]


# -------------------------------------------------------------------- sync

def sync(conn: sqlite3.Connection, cfg) -> dict:
    found = discover(cfg)
    if not found:
        return {"status": "error", "rows_seen": 0, "rows_upserted": 0,
                "anomalies": [f"No GarminDB files found in {cfg.garmin_db_dir}"]}

    anomalies: list[str] = []
    chosen, unmapped = resolve_daily(found)
    if unmapped:
        anomalies.append(
            "Unmapped daily columns (left NULL): " + ", ".join(sorted(unmapped))
            + ". Run with --inspect-garmin to see the real schema."
        )

    now = utc_now()
    # Record the mapping that was actually used.
    conn.execute("DELETE FROM garmin_column_map")
    for target in DAILY_TARGETS:
        src = chosen.get(target)
        conn.execute(
            "INSERT INTO garmin_column_map (target_table, target_column, source_db, "
            "source_table, source_column, mapped, resolved_utc) VALUES (?,?,?,?,?,?,?)",
            ("garmin_daily", target, src[0] if src else None, src[1] if src else None,
             src[3] if src else None, 1 if src else 0, now),
        )

    # Gather day -> {target: value}, grouped by source table so each table is
    # read once.
    by_day: dict[str, dict[str, object]] = {}
    groups: dict[tuple[str, str, str], list[tuple[str, str]]] = {}
    for target, (fname, tname, day_col, col) in chosen.items():
        groups.setdefault((fname, tname, day_col), []).append((target, col))

    duration_passthrough = set()
    for (fname, tname, day_col), pairs in groups.items():
        src = connect(Path(cfg.garmin_db_dir) / fname, read_only=True)
        cols = ", ".join(f'"{c}"' for _, c in pairs)
        try:
            rows = src.execute(
                f'SELECT "{day_col}" AS _day, {cols} FROM "{tname}" WHERE "{day_col}" IS NOT NULL'
            ).fetchall()
        except sqlite3.Error as exc:
            anomalies.append(f"Read failed on {fname}.{tname}: {exc}")
            src.close()
            continue
        src.close()
        for row in rows:
            day = _day_str(row["_day"])
            if not day:
                continue
            slot = by_day.setdefault(day, {})
            for target, col in pairs:
                val = row[col]
                if val is None:
                    continue
                if target in NEGATIVE_MEANS_MISSING and isinstance(val, (int, float)) and val < 0:
                    continue
                if target in DURATION_TARGETS:
                    if isinstance(val, (int, float)):
                        duration_passthrough.add(target)
                    val = to_seconds(val)
                slot[target] = val

    if duration_passthrough:
        anomalies.append(
            "Numeric (not HH:MM:SS) duration values passed through unchanged for: "
            + ", ".join(sorted(duration_passthrough))
            + ". Confirm the units with --verify-day before trusting them."
        )

    targets = list(DAILY_TARGETS)
    cols_sql = ", ".join(targets)
    placeholders = ", ".join(f":{t}" for t in targets)
    updates = ", ".join(f"{t} = COALESCE(excluded.{t}, garmin_daily.{t})" for t in targets)
    upsert = (
        f"INSERT INTO garmin_daily (day, {cols_sql}, loaded_utc) "
        f"VALUES (:day, {placeholders}, :loaded_utc) "
        f"ON CONFLICT(day) DO UPDATE SET {updates}, loaded_utc = excluded.loaded_utc"
    )

    with conn:
        for day, slot in by_day.items():
            params = {t: slot.get(t) for t in targets}
            params["day"] = day
            params["loaded_utc"] = now
            conn.execute(upsert, params)

    act_rows = 0
    act = resolve_activities(found)
    if act is None:
        anomalies.append("No activities table found; garmin_activity not populated.")
    else:
        fname, tname, mapping = act
        src = connect(Path(cfg.garmin_db_dir) / fname, read_only=True)
        sel = ", ".join(f'"{c}" AS "{t}"' for t, c in mapping.items())
        try:
            rows = src.execute(
                f'SELECT "activity_id" AS activity_id{", " + sel if sel else ""} FROM "{tname}"'
            ).fetchall()
        except sqlite3.Error as exc:
            rows = []
            anomalies.append(f"Read failed on {fname}.{tname}: {exc}")
        src.close()

        with conn:
            for row in rows:
                keys = row.keys()
                start = str(row["start_local"]) if "start_local" in keys and row["start_local"] else None
                params = {
                    "activity_id": str(row["activity_id"]),
                    "start_local": start,
                    "local_date": start[:10] if start else None,
                    "sport": row["sport"] if "sport" in keys else None,
                    "sub_sport": row["sub_sport"] if "sub_sport" in keys else None,
                    "name": row["name"] if "name" in keys else None,
                    "elapsed_s": to_seconds(row["elapsed_s"]) if "elapsed_s" in keys else None,
                    "moving_s": to_seconds(row["moving_s"]) if "moving_s" in keys else None,
                    "calories": row["calories"] if "calories" in keys else None,
                    "avg_hr": row["avg_hr"] if "avg_hr" in keys else None,
                    "max_hr": row["max_hr"] if "max_hr" in keys else None,
                    "training_load": row["training_load"] if "training_load" in keys else None,
                    "loaded_utc": now,
                }
                conn.execute(
                    "INSERT INTO garmin_activity (activity_id, start_local, local_date, sport, "
                    "sub_sport, name, elapsed_s, moving_s, calories, avg_hr, max_hr, "
                    "training_load, loaded_utc) VALUES (:activity_id, :start_local, :local_date, "
                    ":sport, :sub_sport, :name, :elapsed_s, :moving_s, :calories, :avg_hr, "
                    ":max_hr, :training_load, :loaded_utc) "
                    "ON CONFLICT(activity_id) DO UPDATE SET "
                    "start_local = COALESCE(excluded.start_local, garmin_activity.start_local), "
                    "local_date = COALESCE(excluded.local_date, garmin_activity.local_date), "
                    "sport = COALESCE(excluded.sport, garmin_activity.sport), "
                    "name = COALESCE(excluded.name, garmin_activity.name), "
                    "loaded_utc = excluded.loaded_utc",
                    params,
                )
                act_rows += 1

    status = "ok_warn" if anomalies else "ok"
    return {"status": status, "rows_seen": len(by_day) + act_rows,
            "rows_upserted": len(by_day) + act_rows, "anomalies": anomalies,
            "notes": f"{len(chosen)}/{len(DAILY_TARGETS)} daily columns mapped"}


def verify_day(cfg, day: str) -> str:
    """Print the raw source rows behind one day, for unit sanity-checking."""
    found = discover(cfg)
    chosen, _ = resolve_daily(found)
    seen: set[tuple[str, str, str]] = set()
    out = [f"Raw GarminDB rows for {day}:"]
    for target, (fname, tname, day_col, _col) in chosen.items():
        key = (fname, tname, day_col)
        if key in seen:
            continue
        seen.add(key)
        src = connect(Path(cfg.garmin_db_dir) / fname, read_only=True)
        try:
            row = src.execute(
                f'SELECT * FROM "{tname}" WHERE "{day_col}" LIKE ? LIMIT 1', (f"{day}%",)
            ).fetchone()
        except sqlite3.Error as exc:
            out.append(f"\n{fname}.{tname}: read failed ({exc})")
            src.close()
            continue
        src.close()
        out.append(f"\n{fname}.{tname}:")
        if row is None:
            out.append("  (no row)")
        else:
            for k in row.keys():
                if row[k] is not None:
                    out.append(f"  {k} = {row[k]!r}")
    return "\n".join(out)
