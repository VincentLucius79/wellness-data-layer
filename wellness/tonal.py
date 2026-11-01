"""Tonal loader: toneget's JSON export -> normalized tables.

toneget's field names are not a published contract; it forwards whatever the
Tonal API returns, minus a hand-maintained removal list. So every field is
looked up through a candidate list and anything unresolved is reported rather
than silently nulled.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import guard
from .db import utc_now

# --------------------------------------------------------------- field maps

# Verified against toneget v3.1.0's actual output: a workout activity's own
# key is `id`, and `workoutId` is the TEMPLATE it was run from - so workoutId
# must not appear in W_ID or every workout would be keyed by its template.
W_ID = ("id", "workoutActivityID", "workoutActivityId", "activityId", "uuid", "_id")
W_BEGIN = ("beginTime", "startTime", "beginAt", "startedAt", "createdAt", "begin_time")
W_TYPE = ("workoutType", "type", "activityType")
W_TITLE = ("workoutTitle", "title", "name", "activityName")
W_TEMPLATE = ("workoutId", "templateId", "workoutTemplateId", "workoutDefinitionId")
W_DURATION = ("duration", "durationSeconds", "totalDuration", "elapsedTime", "totalTime")
W_VOLUME = ("totalVolume", "volume")
W_REPS = ("totalReps", "reps")
W_SETS = ("workoutSetActivity", "workoutSetActivities", "sets", "setActivities", "workoutSets")

# Set fields, by their exact names in toneget v3.1.0's export (verified
# against a real multi-year export): (column, field, type). A set's whole dict
# is kept in tonal_set.raw_json too, so a field left out can be added later.
#
# Left out, and why:
# - userId: yours, the same on every set. workoutId: the template, already
#   on tonal_workout. beginTimeMCB/endTimeMCB: the machine's own clock.
# - Always empty: reps, dualMotorReps, triggeredFeedback (null);
#   cvRepCount, offMachineModifiedWeight, userWeightPounds,
#   suggestedWeightChange (0); movementSide ("Both").
# - totalVolume, totalOnMachineVolume: rounded copies of volume, some 10x
#   off. `volume` is the one that sums to the workout's total.
# - The per-dimension struggling/inconsistency sub-scores (rom*, duration*,
#   maxVel*), meanMaxPos, romWeight/romWeightFrac/romWeightMode,
#   chainsWeightFrac, eccentricWeightFrac, weightPercentage,
#   weightControlMode, spotterMode, maxSpottedWeight, isoModeSpeed,
#   resistanceLevel, suggestedResistanceLevel, durationBasedRepGoal,
#   totalConDuration: machine settings and internals with no documented
#   meaning, most of them 0 on most sets.
# - flex, blockStart, skipDemo, skipSetup, inchesUpdated, powerUpdated,
#   focus, progressiveType: app and program state.
SET_ID = "id"
SET_WORKOUT = "workoutActivityID"
SET_FIELDS = (
    ("prescribed_set_id", "setId", str),        # NOT unique: see schema.sql
    ("movement_id", "movementId", str),
    ("original_movement_id", "originalMovementId", str),
    ("side_number", "sideNumber", int),
    ("block_number", "blockNumber", int),
    ("set_group", "setGroup", int),
    ("round", "round", int),
    ("sort_order", "sortOrder", int),
    ("repetition", "repetition", int),
    ("repetition_total", "repetitionTotal", int),
    ("duration_s", "duration", int),
    ("prescribed_reps", "prescribedReps", int),
    ("prescribed_duration_s", "prescribedDuration", int),
    ("rep_count", "repCount", int),
    ("reps_in_reserve", "repsInReserve", float),
    ("base_weight_lb", "baseWeight", float),
    ("avg_weight_lb", "avgWeight", float),
    ("max_weight_lb", "maxWeight", float),
    ("min_weight_lb", "minWeight", float),
    ("suggested_weight_lb", "suggestedWeight", float),
    ("eccentric_weight_lb", "eccentricWeight", float),
    ("chains_weight_lb", "chainsWeight", float),
    ("one_rep_max_lb", "oneRepMax", float),
    ("volume", "volume", float),
    ("concentric_work", "concentricWork", float),
    ("total_concentric_work", "totalConcentricWork", float),
    ("max_con_power", "maxConPower", float),
    ("vel_at_max_con_power", "velAtMaxConPower", float),
    ("weight_at_max_con_power_lb", "weightAtMaxConPower", float),
    ("avg_velocity", "avgVelocity", float),
    ("rom", "rom", float),
    ("avg_rom", "avgRom", float),
    ("rom_length_in", "romLengthIn", float),
    ("struggling_score", "strugglingScore", float),
    ("inconsistency_score", "inconsistencyScore", float),
)
SET_FLAGS = (
    ("warm_up", "warmUp"), ("drop_set", "dropSet"), ("final_set", "finalSet"),
    ("burnout", "burnout"), ("practice", "practice"), ("calibration", "calibration"),
    ("progressive", "progressive"), ("spotter", "spotter"), ("eccentric", "eccentric"),
    ("chains", "chains"),
)
# On only some sets by nature, so a set without them isn't drift.
SET_OPTIONAL = {"originalMovementId", "prescribedReps", "prescribedDuration",
                "repsInReserve", "sortOrder", "prs"}
SET_COLUMNS = ("set_uid", "workout_id", *(c for c, _, _ in SET_FIELDS),
               "begin_utc", "end_utc", "begin_epoch", "local_date", "tz_source",
               *(c for c, _ in SET_FLAGS), "prs", "raw_json")

REPO = Path(__file__).resolve().parent.parent
MOVEMENT_CATALOG = REPO / "reference" / "tonal-movements.json"
# The catalog is not redistributed here (it carries Tonal's own text), so
# `sync_all.py fetch-catalog` downloads it from this pinned gist revision
# and refuses anything whose hash differs. reference/README.md says more.
CATALOG_URL = ("https://gist.githubusercontent.com/BrianVia/aa79415d999696b50cc4aff9bc1f9f0c/"
               "raw/623fa360af417e73fe7015e76a269780d5d2e344/tonal-movements.json")
CATALOG_SHA256 = "208a9f7cc0ab546d6e066c867558604f5d21c722be6e8cb6861eaca939cda1bb"
MOVEMENT_NAMES = REPO / "reference" / "tonal-movement-names.toml"
# What tonal-movement-names.toml may set, and the type each must be.
NAME_KEYS = {"name": str, "short_name": str, "muscle_groups": list, "body_region": str,
             "is_two_sided": bool, "is_alternating": bool, "arms_together": bool,
             "on_machine": bool, "skill_level": int, "accessory": str}
MOVEMENT_COLUMNS = tuple(NAME_KEYS)
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

DATE_KEYS = ("date", "day", "observedAt", "timestamp", "createdAt", "recordedAt", "updatedAt")
SCORE_KEYS = ("score", "value", "strengthScore", "points")
SCOPES = ("overall", "upper", "lower", "core")


def _id_keyed(blob) -> list[tuple]:
    """toneget emits workoutCatalog and customWorkouts as dicts KEYED BY id,
    not as lists. Iterating them naively yields bare strings and silently
    finds nothing. Accept either shape and return (key, entry) pairs."""
    if isinstance(blob, dict):
        return list(blob.items())
    if isinstance(blob, list):
        return [(None, e) for e in blob]
    return []


def _first(d: dict, keys, default=None):
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d[k]
    return default


def _num(v, cast=float):
    if v is None:
        return None
    try:
        return cast(v)
    except (TypeError, ValueError):
        return None


def _flag(v) -> int | None:
    """A source boolean as 1/0. Anything else is unknown, not false."""
    return int(v) if isinstance(v, bool) or v in (0, 1) else None


def _text(v) -> str | None:
    return str(v) if v is not None else None


# ------------------------------------------------------------- timestamps

def parse_ts(raw, tz: ZoneInfo) -> tuple[str | None, int | None, str | None, str]:
    """Return (utc_iso, epoch, local_date, tz_source).

    tz_source is how we knew the offset. 'assumed_local' means the source gave
    none and we applied the configured timezone - worth knowing, because
    toneget stamps its own exportedAt field with a local time and a 'Z'.

    'zero_time' is Go's zero time, 0001-01-01T00:00:00Z, which Tonal writes
    on a set that was prescribed but never performed. It means "no time",
    so it gives no timestamp rather than a date in year 1.
    """
    if raw is None:
        return None, None, None, "unknown"

    s = str(raw).strip()
    if not s:
        return None, None, None, "unknown"

    src: str
    if s.lstrip("-").isdigit():
        v = int(s)
        if abs(v) > 10_000_000_000:          # milliseconds
            dt, src = datetime.fromtimestamp(v / 1000, timezone.utc), "epoch_ms"
        else:
            dt, src = datetime.fromtimestamp(v, timezone.utc), "epoch_s"
    else:
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None, None, None, "unknown"
        if dt.year == 1:
            return None, None, None, "zero_time"
        if dt.tzinfo is None:
            dt, src = dt.replace(tzinfo=tz), "assumed_local"
        else:
            src = "z" if s.rstrip().endswith(("Z", "z")) else "offset"

    try:
        utc = dt.astimezone(timezone.utc)
        return (
            utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            int(utc.timestamp()),
            utc.astimezone(tz).date().isoformat(),
            src,
        )
    except (OverflowError, ValueError, OSError):    # a year near 1 or 9999
        return None, None, None, "unknown"


# ----------------------------------------------------------------- reading

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_export(path: Path) -> dict:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def newest_export(directory: Path) -> Path | None:
    candidates = [
        p for p in directory.glob("tonal_workouts_*")
        if p.suffix in (".gz", ".json")
    ]
    if not candidates:
        candidates = [p for p in directory.glob("*.json*")]
    return max(candidates, key=lambda p: p.stat().st_mtime) if candidates else None


# ------------------------------------------------------------ normalizing

def normalize(export: dict, tz: ZoneInfo) -> tuple[list[dict], list[str]]:
    """Flatten the export into workout dicts. Returns (workouts, notes)."""
    notes: list[str] = []
    raw_workouts = export.get("workouts") or []
    if not isinstance(raw_workouts, list):
        return [], ["Export 'workouts' key is not a list."]

    catalog: dict[str, str | None] = {}
    for key, entry in _id_keyed(export.get("workoutCatalog")):
        cid = key
        title = None
        if isinstance(entry, dict):
            cid = cid if cid is not None else _first(entry, ("id", "workoutId"))
            title = _first(entry, ("title", "name"))
        if cid is not None:
            catalog[str(cid)] = title

    custom_ids: set[str] = set()
    for key, entry in _id_keyed(export.get("customWorkouts")):
        cid = key
        if cid is None and isinstance(entry, dict):
            cid = _first(entry, ("id", "workoutId"))
        if cid is not None:
            custom_ids.add(str(cid))

    synthetic_ids = 0
    out: list[dict] = []
    seen: set[str] = set()

    for raw in raw_workouts:
        if not isinstance(raw, dict):
            continue

        wid = _first(raw, W_ID)
        if wid is None:
            # No usable key. Derive a stable one so the row is not lost, but
            # make the problem visible rather than inventing silently.
            basis = json.dumps(
                [_first(raw, W_BEGIN), _first(raw, W_TYPE), _first(raw, W_VOLUME)],
                sort_keys=True, default=str,
            )
            wid = "synthetic:" + hashlib.sha256(basis.encode()).hexdigest()[:16]
            synthetic_ids += 1
        wid = str(wid)
        if wid in seen:
            continue
        seen.add(wid)

        utc_iso, epoch, local_date, tz_source = parse_ts(_first(raw, W_BEGIN), tz)
        template_id = _first(raw, W_TEMPLATE)
        template_id = str(template_id) if template_id is not None else None

        raw_sets = _first(raw, W_SETS) or []
        if not isinstance(raw_sets, list):
            raw_sets = []

        sets = [normalize_set(rs, wid, tz) for rs in raw_sets if isinstance(rs, dict)]

        title = _first(raw, W_TITLE) or (catalog.get(template_id) if template_id else None)

        out.append({
            "workout_id": wid,
            "begin_utc": utc_iso,
            "begin_epoch": epoch,
            "local_date": local_date,
            "tz_source": tz_source,
            "workout_type": _first(raw, W_TYPE),
            "title": title,
            "template_id": template_id,
            "is_custom": 1 if (template_id and template_id in custom_ids) else 0,
            "duration_s": _num(_first(raw, W_DURATION), int),
            "total_volume": _num(_first(raw, W_VOLUME)),
            "total_reps": _num(_first(raw, W_REPS), int),
            "set_count": len(sets),
            "raw_json": json.dumps(raw, separators=(",", ":"), default=str),
            "_sets": sets,
        })

    if synthetic_ids:
        notes.append(
            f"{synthetic_ids} workout(s) had no recognizable id field; a content hash "
            f"was used. Check toneget's field names against {W_ID}."
        )
    if out and all(w["total_volume"] is None for w in out):
        notes.append("No workout matched a volume field - the export's field names may have changed.")
    if out and not any(w["_sets"] for w in out):
        notes.append(f"No workout matched a sets field - tried {W_SETS}.")
    elif out and all(s["rep_count"] is None for w in out for s in w["_sets"]):
        notes.append(
            "Sets carry no rep counts. toneget's default trimming deletes per-set "
            "fields - re-run it with --full to keep them."
        )

    return out, notes


def normalize_set(rs: dict, workout_id: str, tz: ZoneInfo) -> dict:
    """One exported set -> a tonal_set row. Keyed by the set's own `id`,
    which is unique; `setId` repeats and is only stored.

    Every side of a two-sided movement is its own row here, and is stored
    as such. Side 0 is always performed first, so anything computed per
    side mixes the limb with the order: a per-side difference, in power
    say, can't be read as a strength asymmetry. Weights are prescribed the same on both
    sides, so collapse them (v_tonal_set_both_sides)."""
    sid = rs.get(SET_ID)
    begin, epoch, local_date, tz_source = parse_ts(rs.get("beginTime"), tz)
    row = {"set_uid": _text(sid), "workout_id": workout_id,
           "begin_utc": begin, "end_utc": parse_ts(rs.get("endTime"), tz)[0],
           "begin_epoch": epoch, "local_date": local_date, "tz_source": tz_source}
    for col, key, cast in SET_FIELDS:
        row[col] = _text(rs.get(key)) if cast is str else _num(rs.get(key), cast)
    for col, key in SET_FLAGS:
        row[col] = _flag(rs.get(key))
    prs = rs.get("prs")
    row["prs"] = json.dumps([str(p) for p in prs]) if isinstance(prs, list) and prs else None
    row["raw_json"] = json.dumps(rs, separators=(",", ":"), default=str)
    # For the guard: the workout the set names, against the one it sits in.
    row["_claimed_workout"] = _text(rs.get(SET_WORKOUT))
    return row


def set_field_drift(export: dict) -> list[str]:
    """Set fields this loader reads that no set in the export carries.
    toneget renaming a field shows up here instead of as quiet NULLs."""
    present: set[str] = set()
    any_sets = False
    for w in export.get("workouts") or []:
        for rs in (_first(w, W_SETS) or []) if isinstance(w, dict) else []:
            if isinstance(rs, dict):
                any_sets = True
                present.update(rs)
    if not any_sets:
        return []
    wanted = [SET_ID, *(k for _, k, _ in SET_FIELDS), *(k for _, k in SET_FLAGS),
              "beginTime", "endTime"]
    return [k for k in wanted if k not in present and k not in SET_OPTIONAL]


# --------------------------------------------------------------- movements

def _catalog_row(m: dict) -> dict:
    info = m.get("onMachineInfo") if isinstance(m.get("onMachineInfo"), dict) else {}
    groups = m.get("muscleGroups")
    return {
        "name": _text(m.get("name")),
        "short_name": _text(m.get("shortName")),
        "muscle_groups": json.dumps(groups) if isinstance(groups, list) else None,
        "body_region": _text(m.get("bodyRegion")),
        "is_two_sided": _flag(m.get("isTwoSided")),
        "is_alternating": _flag(m.get("isAlternating")),
        "arms_together": _flag(info.get("trainerArmsPulledAtSameTime")),
        "on_machine": _flag(m.get("onMachine")),
        "skill_level": _num(m.get("skillLevel"), int),
        "accessory": _text(info.get("accessory")),
    }


def install_catalog(data: bytes, dest: Path = MOVEMENT_CATALOG,
                    sha256: str = CATALOG_SHA256) -> Path:
    """Write a downloaded catalog to `dest`, atomically, only if its hash is
    the pinned one. A mismatch raises and leaves any existing file alone."""
    got = hashlib.sha256(data).hexdigest()
    if got != sha256:
        raise ValueError(f"movement catalog hash is {got}, not the pinned {sha256}; "
                         "not installed")
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, dest)
    return dest


def fetch_catalog(dest: Path = MOVEMENT_CATALOG, url: str = CATALOG_URL,
                  opener=None) -> str:
    """Download the movement catalog and install it. Returns what it did."""
    if opener is None:
        from urllib.request import urlopen as opener
    with opener(url, timeout=60) as resp:
        data = resp.read()
    path = install_catalog(data, dest)
    return f"Movement catalog installed at {path} ({len(data)} bytes, sha256 verified)."


def read_movement_names(path: Path) -> tuple[dict[str, dict], list[str]]:
    """Your names from tonal-movement-names.toml: ({id: columns}, problems).
    A bad entry is skipped and named, never guessed at; a file that won't
    parse skips them all. Neither stops the Tonal load."""
    if not path.is_file():
        return {}, []
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        return {}, [f"{path.name} could not be read, so no manual movement names "
                    f"were applied: {exc}"]
    names: dict[str, dict] = {}
    problems: list[str] = []
    for mid, entry in raw.items():
        if not UUID.fullmatch(mid) or not isinstance(entry, dict):
            problems.append(f"{path.name}: [{mid[:40]}] is not a movement id table; skipped.")
            continue
        bad = sorted(k for k, v in entry.items()
                     if k not in NAME_KEYS or not isinstance(v, NAME_KEYS[k])
                     or (NAME_KEYS[k] is int and isinstance(v, bool))
                     or (k == "muscle_groups" and not all(isinstance(g, str) for g in v)))
        name = entry.get("name")
        if bad or not isinstance(name, str) or not name.strip():
            why = f"bad key(s) {', '.join(bad)}" if bad else "no name"
            problems.append(f"{path.name}: {mid} skipped ({why}).")
            continue
        names[mid] = {k: json.dumps(v) if k == "muscle_groups"
                      else int(v) if isinstance(v, bool) else v
                      for k, v in entry.items()}
    return names, problems


UPSERT_MOVEMENT = (
    f"INSERT INTO tonal_movement (movement_id, {', '.join(MOVEMENT_COLUMNS)}, origin, loaded_utc) "
    f"VALUES (:movement_id, {', '.join(':' + c for c in MOVEMENT_COLUMNS)}, :origin, :now) "
    "ON CONFLICT(movement_id) DO UPDATE SET "
    + ", ".join(f"{c} = excluded.{c}" for c in (*MOVEMENT_COLUMNS, "origin"))
    + ", loaded_utc = excluded.loaded_utc"
)


def load_movements(conn: sqlite3.Connection, cfg) -> list[str]:
    """Refresh tonal_movement from the vendored catalog and your names file,
    yours winning. Reference data, so rows are replaced, not merged. A
    movement dropped from both files keeps its last row. Returns problems."""
    catalog_path = Path(cfg.tonal.get("movement_catalog", MOVEMENT_CATALOG)).expanduser()
    names_path = Path(cfg.tonal.get("movement_names", MOVEMENT_NAMES)).expanduser()
    problems: list[str] = []
    rows: dict[str, dict] = {}
    try:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        if not isinstance(catalog, list):
            raise ValueError("not a list")
    except (OSError, ValueError) as exc:
        catalog = []
        problems.append(f"Movement catalog {catalog_path.name} not loaded ({exc}).")
    for m in catalog:
        if isinstance(m, dict) and isinstance(m.get("id"), str):
            rows[m["id"]] = {**_catalog_row(m), "origin": "catalog"}
    names, bad = read_movement_names(names_path)
    problems += bad
    for mid, given in names.items():
        base = rows.get(mid) or dict.fromkeys(MOVEMENT_COLUMNS)
        rows[mid] = {**base, **given, "origin": "manual"}
    now = utc_now()
    with conn:
        conn.executemany(UPSERT_MOVEMENT, [{"movement_id": mid, **r, "now": now}
                                           for mid, r in rows.items()])
    return problems


def movement_report(conn: sqlite3.Connection) -> tuple[str, list[str]]:
    """(summary, warnings) about the movements the stored sets use.

    The warning is the side check: a movement the catalog calls two-sided
    must show a side 1 in its sets, and a one-sided one never may. It held
    for every named movement in a real history when the catalog was first
    used. If it stops
    holding, either the catalog is stale or the export changed shape."""
    used, named, unknown, unknown_sets = conn.execute(
        "SELECT COUNT(*), SUM(m.name IS NOT NULL), SUM(m.name IS NULL), "
        "       SUM(CASE WHEN m.name IS NULL THEN u.n END) "
        "FROM (SELECT movement_id, COUNT(*) n FROM tonal_set "
        "      WHERE movement_id IS NOT NULL GROUP BY movement_id) u "
        "LEFT JOIN tonal_movement m ON m.movement_id = u.movement_id").fetchone()
    checked = conn.execute(
        "SELECT COALESCE(m.name, s.movement_id), m.is_two_sided, MAX(s.side_number) >= 1 "
        "FROM tonal_set s JOIN tonal_movement m ON m.movement_id = s.movement_id "
        "WHERE m.is_two_sided IS NOT NULL AND s.side_number IS NOT NULL "
        "GROUP BY s.movement_id").fetchall()
    wrong = sorted(f"{n} (listed {'two-sided' if two else 'one-sided'}, "
                   f"{'has' if seen else 'no'} side 1 in the sets)"
                   for n, two, seen in checked if bool(two) != bool(seen))
    summary = (f"movements: {used or 0} used, {named or 0} named, {unknown or 0} unknown"
               + (f" ({unknown_sets} sets; name them in reference/tonal-movement-names.toml)"
                  if unknown else "")
               + f"; sides match the catalog on {len(checked) - len(wrong)} of {len(checked)}")
    warnings = ([f"isTwoSided disagrees with the sets for {len(wrong)} movement(s): "
                 + "; ".join(wrong[:5]) + ". The catalog is stale or the export changed shape."]
                if wrong else [])
    return summary, warnings


# --------------------------------------------------------------- migration

def migrate(conn: sqlite3.Connection, cfg) -> list[str]:
    """Refill tonal_set from tonal_set_v1, once, after db.retire_tables has
    moved the old position-keyed table aside. Its raw_json is each set as
    exported (checked identical to the export on 2026-10-22), so this needs
    no export file. Runs only while tonal_set is empty; the next accepted
    export upserts over it as usual. tonal_set_v1 itself is kept."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' "
                        "AND name = 'tonal_set_v1'").fetchone():
        return []
    if conn.execute("SELECT 1 FROM tonal_set LIMIT 1").fetchone():
        return []
    old = conn.execute("SELECT workout_id, raw_json FROM tonal_set_v1").fetchall()
    if not old:
        return []
    problems = load_movements(conn, cfg)
    now = utc_now()
    rows, unkeyed = [], 0
    for workout_id, raw in old:
        try:
            rs = json.loads(raw) if raw else None
        except ValueError:
            rs = None
        if not isinstance(rs, dict) or rs.get(SET_ID) is None:
            unkeyed += 1
            continue
        row = normalize_set(rs, workout_id, cfg.timezone)
        row.pop("_claimed_workout")
        rows.append({**row, "now": now})
    try:
        with conn:
            conn.executemany(UPSERT_SET, rows)
    except sqlite3.IntegrityError as exc:
        return [f"tonal_set NOT refilled from tonal_set_v1 ({exc}); "
                "the next accepted Tonal export will fill it."] + problems
    return [f"tonal_set refilled from tonal_set_v1: {len(rows)} set(s)"
            + (f"; {unkeyed} without an id stay in tonal_set_v1 only" if unkeyed else "")
            + "."] + problems


def _series(blob) -> list[tuple[str, float]]:
    """Pull (date, score) pairs out of a loosely-shaped history list."""
    pairs: list[tuple[str, float]] = []
    if isinstance(blob, dict):
        blob = blob.get("history") or blob.get("values") or blob.get("data") or []
    if not isinstance(blob, list):
        return pairs
    for item in blob:
        if not isinstance(item, dict):
            continue
        d = _first(item, DATE_KEYS)
        v = _num(_first(item, SCORE_KEYS))
        if d is None or v is None:
            continue
        pairs.append((str(d)[:10], v))
    return pairs


def normalize_strength(export: dict) -> tuple[list[tuple], list[tuple]]:
    """Return (strength_rows, muscle_rows).

    toneget v3.1.0's real shapes:
      strengthScoreHistory -> LIST of per-date records, each carrying every
                              scope at once: {date, overall, upper, lower, core}
      currentStrengthScores -> {'raw': [...], 'parsed': {
                                   'regions': {name: score},
                                   'muscles': {name: {score, region, updatedAt}}}}
    Other shapes are still accepted as a fallback, because none of this is a
    published contract.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    strength: list[tuple] = []
    muscles: list[tuple] = []

    hist = export.get("strengthScoreHistory")

    if isinstance(hist, list):
        for item in hist:
            if not isinstance(item, dict):
                continue
            raw_date = _first(item, DATE_KEYS)
            day = str(raw_date)[:10] if raw_date is not None else today
            found = False
            for scope in SCOPES:
                v = _num(item.get(scope))
                if v is not None:
                    strength.append((day, scope, v))
                    found = True
            if not found:
                v = _num(_first(item, SCORE_KEYS))
                if v is not None:
                    strength.append((day, "overall", v))
    elif isinstance(hist, dict):
        for scope in SCOPES:
            for d, v in _series(hist.get(scope)):
                strength.append((d, scope, v))
        if not strength:
            for d, v in _series(hist):
                strength.append((d, "overall", v))

    cur = export.get("currentStrengthScores") or {}
    parsed = cur.get("parsed") if isinstance(cur, dict) else None

    if isinstance(parsed, dict):
        for region, score in (parsed.get("regions") or {}).items():
            v = _num(score)
            if v is not None:
                strength.append((today, str(region).lower(), v))
        for name, info in (parsed.get("muscles") or {}).items():
            if isinstance(info, dict):
                v = _num(info.get("score"))
                stamp = info.get("updatedAt")
                day = str(stamp)[:10] if stamp else today
            else:
                v, day = _num(info), today
            if v is not None:
                muscles.append((day, str(name), v))
    elif cur:
        # Unknown shape: walk it for name/number pairs rather than dropping it.
        def walk(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    if isinstance(v, (int, float)):
                        muscles.append((today, k, float(v)))
                    else:
                        walk(v)
            elif isinstance(node, list):
                for item in node:
                    if isinstance(item, dict):
                        nm = _first(item, ("muscle", "name", "muscleGroup", "region"))
                        sc = _num(_first(item, SCORE_KEYS))
                        if nm is not None and sc is not None:
                            muscles.append((today, str(nm), sc))
                        else:
                            walk(item)

        walk(cur)

    # dedupe on the primary keys
    s_seen: dict[tuple, tuple] = {}
    for row in strength:
        s_seen[(row[0], row[1])] = row
    m_seen: dict[tuple, tuple] = {}
    for row in muscles:
        m_seen[(row[0], row[1])] = row
    return list(s_seen.values()), list(m_seen.values())


# -------------------------------------------------------------------- sync

UPSERT_WORKOUT = """
INSERT INTO tonal_workout (
    workout_id, begin_utc, begin_epoch, local_date, tz_source, workout_type,
    title, template_id, is_custom, duration_s, total_volume, total_reps,
    set_count, raw_json, first_seen_utc, last_seen_utc
) VALUES (
    :workout_id, :begin_utc, :begin_epoch, :local_date, :tz_source, :workout_type,
    :title, :template_id, :is_custom, :duration_s, :total_volume, :total_reps,
    :set_count, :raw_json, :now, :now
)
ON CONFLICT(workout_id) DO UPDATE SET
    begin_utc = excluded.begin_utc,
    begin_epoch = excluded.begin_epoch,
    local_date = excluded.local_date,
    tz_source = excluded.tz_source,
    workout_type = excluded.workout_type,
    title = COALESCE(excluded.title, tonal_workout.title),
    template_id = COALESCE(excluded.template_id, tonal_workout.template_id),
    is_custom = excluded.is_custom,
    duration_s = COALESCE(excluded.duration_s, tonal_workout.duration_s),
    total_volume = COALESCE(excluded.total_volume, tonal_workout.total_volume),
    total_reps = COALESCE(excluded.total_reps, tonal_workout.total_reps),
    set_count = excluded.set_count,
    raw_json = excluded.raw_json,
    last_seen_utc = excluded.last_seen_utc
"""

# Sets are upserted, never deleted, and a value missing from a newer export
# can't NULL a stored one. raw_json is always present, so it holds the
# latest export's copy; every earlier copy is in the archive.
UPSERT_SET = (
    f"INSERT INTO tonal_set ({', '.join(SET_COLUMNS)}, first_seen_utc, last_seen_utc) "
    f"VALUES ({', '.join(':' + c for c in SET_COLUMNS)}, :now, :now) "
    "ON CONFLICT(set_uid) DO UPDATE SET "
    + ", ".join(f"{c} = COALESCE(excluded.{c}, tonal_set.{c})" for c in SET_COLUMNS[1:])
    + ", last_seen_utc = excluded.last_seen_utc"
)


def sync(conn: sqlite3.Connection, cfg, export_path: Path | None = None,
         *, force: bool = False) -> dict:
    """Load the newest (or given) toneget export. Returns a summary dict."""
    tz = cfg.timezone
    cfg.tonal_export_dir.mkdir(parents=True, exist_ok=True)
    path = Path(export_path) if export_path else newest_export(cfg.tonal_export_dir)
    if path is None:
        return {"status": "error", "anomalies": [f"No export found in {cfg.tonal_export_dir}"],
                "rows_seen": 0, "rows_upserted": 0, "source_file": None, "sha256": None}

    digest = sha256_file(path)

    # Names come from files in the repo, not the export, so they refresh on
    # every run, even one that skips an export it has already seen.
    movement_problems = load_movements(conn, cfg)

    prior = conn.execute(
        "SELECT verdict FROM tonal_export_archive WHERE sha256 = ?", (digest,)
    ).fetchone()
    if prior and not force:
        summary, side_warnings = movement_report(conn)
        warnings = movement_problems + side_warnings
        return {"status": "ok_warn" if warnings else "ok",
                "anomalies": [f"Export already processed ({prior[0]}); skipping."] + warnings,
                "notes": summary, "rows_seen": 0, "rows_upserted": 0,
                "source_file": str(path), "sha256": digest}

    # Archive the raw payload before anything can go wrong with parsing.
    archive_dir = cfg.tonal_archive_dir
    archive_dir.mkdir(parents=True, exist_ok=True)
    archived = archive_dir / f"{digest[:12]}_{path.name}"
    if not archived.exists():
        shutil.copy2(path, archived)

    export = load_export(path)
    workouts, notes = normalize(export, tz)

    # Present only if toneget was patched to record it (see README).
    pagination = export.get("pagination") or {}
    declared = pagination.get("declaredTotal") or pagination.get("pg_total")
    if pagination.get("failedPages"):
        notes.append(
            f"Exporter reported {pagination['failedPages']} failed page(s) during download."
        )

    verdict = guard.evaluate_tonal(
        conn, workouts,
        declared_total=declared,
        reject_on_missing=cfg.tonal.get("reject_on_missing_workouts", True),
        reject_on_count_drop=cfg.tonal.get("reject_on_count_drop", True),
        gap_warn_days=cfg.tonal.get("gap_warn_days", 21),
        set_field_drift=set_field_drift(export),
    )
    anomalies = notes + verdict.anomalies

    conn.execute(
        "INSERT OR REPLACE INTO tonal_export_archive "
        "(sha256, archived_path, exported_at, workout_count, verdict, seen_utc) "
        "VALUES (?,?,?,?,?,?)",
        (digest, str(archived), str(export.get("exportedAt") or "")[:32],
         len(workouts), verdict.status, utc_now()),
    )
    conn.commit()

    if not verdict.accept:
        return {"status": "rejected", "anomalies": anomalies + movement_problems,
                "rows_seen": len(workouts), "rows_upserted": 0, "source_file": str(path),
                "sha256": digest, "archived": str(archived)}

    now = utc_now()
    upserted = 0
    stored_sets = {r[0] for r in conn.execute("SELECT set_uid FROM tonal_set")}
    incoming_sets = [s for w in workouts for s in w["_sets"]]
    with conn:  # single transaction
        for w in workouts:
            params = {k: v for k, v in w.items() if k != "_sets"}
            params["now"] = now
            conn.execute(UPSERT_WORKOUT, params)
            upserted += 1
            # Upserted by the set's own id, never deleted: the guard has
            # already refused any export missing a set we hold.
            if w["_sets"]:
                conn.executemany(UPSERT_SET, [
                    {k: v for k, v in s.items() if not k.startswith("_")} | {"now": now}
                    for s in w["_sets"]])

        strength, muscles = normalize_strength(export)
        if strength:
            conn.executemany(
                "INSERT INTO tonal_strength_score (observed_date, scope, score) VALUES (?,?,?) "
                "ON CONFLICT(observed_date, scope) DO UPDATE SET score = excluded.score",
                strength,
            )
        if muscles:
            conn.executemany(
                "INSERT INTO tonal_muscle_score (snapshot_date, muscle, score) VALUES (?,?,?) "
                "ON CONFLICT(snapshot_date, muscle) DO UPDATE SET score = excluded.score",
                muscles,
            )
        if not strength:
            anomalies.append("No strength-score history parsed from the export.")

    summary, side_warnings = movement_report(conn)
    warnings = movement_problems + side_warnings
    new_sets = len({s["set_uid"] for s in incoming_sets} - stored_sets)
    notes_line = (f"sets: {len(incoming_sets)} in export, {new_sets} new, "
                  f"{len(incoming_sets) - new_sets} already stored; {summary}")
    status = "ok_warn" if verdict.status == "ok" and warnings else verdict.status
    return {"status": status, "anomalies": anomalies + warnings, "notes": notes_line,
            "rows_seen": len(workouts), "rows_upserted": upserted,
            "source_file": str(path), "sha256": digest, "archived": str(archived)}
