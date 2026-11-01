#!/usr/bin/env python3
"""End-to-end test on synthetic fixtures. No network, no real credentials.

Run:  python tests/test_pipeline.py

What it proves:
  * A normal Tonal export loads, and a later export that appends workouts loads.
  * A TRUNCATED export - the toneget dropped-page failure - is rejected, and
    the already-stored history survives intact.
  * Tonal sets are keyed by their own id, never deleted, and an export that
    would lose one is rejected whole. Movement names come from the vendored
    catalog and your own names file, and the catalog's two-sided flag is
    checked against the sides the sets show.
  * The old position-keyed set table is renamed aside, never dropped, and
    the new one is refilled from it.
  * GarminDB's schema is discovered rather than assumed, and 'HH:MM:SS'
    durations become seconds.
  * Withings value*10^unit decoding is right, undocumented measurement types
    are captured instead of dropped, and user objectives (category 2) stay
    out of the analysis views.
  * An incomplete Withings pagination walk is rejected, not committed.
  * A stale source exits 3 so the scheduler alerts, while advisory warnings,
    annotated gaps and data an annotation carries forward all exit 0.
  * The cross-source views line a lift day up with the next day's blood
    pressure.
  * The dashboard draws gaps instead of interpolating, merges Tonal records
    and the watch's copy into one session, never fills Tonal volume from
    Garmin, and reads only the snapshot.
  * Masses are converted for display only, all to the one configured unit.
  * The two Garmin columns whose names lied are renamed on an old database
    without losing a value, and the rename is safe to apply again.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import sqlite3
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import sync_all  # noqa: E402
from wellness import config, dashboard, db, demo, garmin, guard, tonal, withings  # noqa: E402

TZ = ZoneInfo("America/Chicago")
PASS, FAIL = [], []


def check(label: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(label)
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{(' - ' + detail) if detail else ''}")


# --------------------------------------------------------------- fixtures

def write_config(root: Path) -> Path:
    path = root / "config.toml"
    path.write_text(f"""
[paths]
wellness_db      = "{root}/wellness.db"
garmin_db_dir    = "{root}/DBs"
tonal_export_dir = "{root}/tonal"
archive_dir      = "{root}/raw"
withings_token   = "{root}/.withings_token.json"

[local]
timezone = "America/Chicago"

[tonal]
reject_on_missing_workouts = true
reject_on_count_drop = true
gap_warn_days = 21
movement_names = "{root}/tonal-movement-names.toml"
movement_catalog = "{root}/tonal-movements.json"

[garmin]
db_files = ["garmin.db", "garmin_activities.db"]

[withings]
client_id = "test-client"
client_secret = "test-secret"
redirect_uri = "https://localhost/cb"
initial_start_date = "2026-01-01"
strict_pagination = true
""".strip())
    (root / "tonal-movements.json").write_text(json.dumps(CATALOG))
    return path


def build_fake_garmindb(root: Path) -> None:
    """Deliberately uses plausible-but-not-guaranteed GarminDB names, so the
    introspection path is what makes this work."""
    dbs = root / "DBs"
    dbs.mkdir(parents=True, exist_ok=True)

    c = sqlite3.connect(dbs / "garmin.db")
    # As in real GarminDB, `sleep` is created before `daily_summary` and has
    # its own avg_stress (stress during sleep only), which a resolver that
    # takes the first match would wrongly pick for all-day stress.
    c.executescript("""
        CREATE TABLE sleep (
            day TEXT PRIMARY KEY, total_sleep TEXT, deep_sleep TEXT,
            rem_sleep TEXT, light_sleep TEXT, score INTEGER, avg_stress REAL);
        CREATE TABLE daily_summary (
            day TEXT PRIMARY KEY, rhr INTEGER, hr_min INTEGER, hr_max INTEGER,
            steps INTEGER, intensity_time TEXT, moderate_activity_time TEXT,
            vigorous_activity_time TEXT, calories_total INTEGER,
            stress_avg INTEGER, bb_min INTEGER, bb_max INTEGER);
        CREATE TABLE weight (day TEXT PRIMARY KEY, weight REAL);
    """)
    for i in range(8):
        day = (datetime(2026, 9, 1) + timedelta(days=i)).date().isoformat()
        # The last day carries Garmin's -1 "not enough data" stress code.
        c.execute("INSERT INTO daily_summary VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (day, 52 + i % 3, 44, 158, 9000 + i * 310, "01:05:00", "00:45:00",
                   "00:20:00", 2900, 28 + i if i < 7 else -1, 12, 92))
        c.execute("INSERT INTO sleep VALUES (?,?,?,?,?,?,?)",
                  (day, "07:12:00", "01:20:00", "01:35:00", "04:17:00", 78 + i % 5, 15.0))
        c.execute("INSERT INTO weight VALUES (?,?)", (day, 94.8 - i * 0.05))
    c.commit()
    c.close()

    a = sqlite3.connect(dbs / "garmin_activities.db")
    a.executescript("""
        CREATE TABLE activities (
            activity_id TEXT PRIMARY KEY, name TEXT, sport TEXT, sub_sport TEXT,
            start_time TEXT, elapsed_time TEXT, moving_time TEXT,
            calories INTEGER, avg_hr INTEGER, max_hr INTEGER, training_load REAL);
    """)
    # GarminDB's real start_time: naive local wall-clock time, no offset.
    a.execute("INSERT INTO activities VALUES (?,?,?,?,?,?,?,?,?,?,?)",
              ("a1", "Evening Walk", "walking", "generic", "2026-09-03 18:10:00.000000",
               "00:48:00", "00:46:30", 240, 96, 118, 31.5))
    a.commit()
    a.close()


# The demo's invented movement catalog, in the real catalog's shape, so
# fixture sets are named and their sides agree with the catalog's isTwoSided.
# The real one is downloaded, never committed (reference/README.md).
CATALOG = demo.invented_catalog()
ONE_SIDED = [m["id"] for m in CATALOG if not m["isTwoSided"] and m["onMachine"]][:2]
TWO_SIDED = next(m["id"] for m in CATALOG if m["isTwoSided"] and m["onMachine"])
UNKNOWN_MOVE = "0f0f0f0f-0000-4000-8000-000000000001"
ZERO_TIME = "0001-01-01T00:00:00Z"


def make_set(wid: str, n: int, *, movement: str = ONE_SIDED[0], side: int = 0,
             set_id: str | None = None, day: str = "2026-09-01", **over) -> dict:
    """One set as toneget v3.1.0 exports it: its own unique `id`, and a
    `setId` that names the prescribed set and repeats across workouts."""
    begin = f"{day}T19:{30 + n:02d}:00.000Z"
    s = {
        "id": f"{wid}-s{n}", "workoutActivityID": wid, "workoutId": "tpl1", "userId": "u1",
        "setId": set_id or f"ps{n}", "movementId": movement, "sideNumber": side,
        "movementSide": "Both", "blockNumber": 1, "setGroup": 1, "round": 1,
        "sortOrder": n, "repetition": 1, "repetitionTotal": 3,
        "beginTime": begin, "endTime": begin.replace(":00.000Z", ":40.000Z"),
        "duration": 40, "prescribedReps": 10, "repCount": 10, "repsInReserve": 2.5,
        "reps": None, "baseWeight": 45, "avgWeight": 45.0, "maxWeight": 46.0,
        "minWeight": 44.0, "suggestedWeight": 45.0, "eccentricWeight": 0,
        "chainsWeight": 0, "oneRepMax": 60.0, "volume": 450.0, "totalVolume": 450,
        "concentricWork": 1000.0, "totalConcentricWork": 113, "maxConPower": 300.0,
        "velAtMaxConPower": 50.0, "weightAtMaxConPower": 45.0, "avgVelocity": 0,
        "rom": 40.0, "avgRom": 0, "romLengthIn": 400.0, "strugglingScore": 0.4,
        "inconsistencyScore": 0.1,
        **{k: False for k in ("warmUp", "dropSet", "finalSet", "burnout", "practice",
                              "calibration", "progressive", "spotter", "eccentric",
                              "chains")},
    }
    s.update(over)
    return s


def make_workout(n: int, day: str, *, naive_ts: bool = False,
                 template: str = "tpl1", sets: list[dict] | None = None) -> dict:
    """Shaped like toneget v3.1.0's real output: the activity's own key is
    `id`, and `workoutId` is the TEMPLATE it was run from."""
    ts = f"{day}T14:30:00" if naive_ts else f"{day}T19:30:00Z"
    wid = f"w{n}"
    return {
        "id": wid,
        "workoutId": template,
        "beginTime": ts,
        "workoutType": "PROGRAM" if template == "tpl1" else "Custom",
        "workoutTitle": "Back Day",
        "duration": 3300,
        "totalVolume": 11000 + n * 250,
        "totalReps": 110 + n,
        "workoutSetActivity": sets if sets is not None else [
            make_set(wid, 0, day=day, prs=["Power"]),
            make_set(wid, 1, day=day, movement=ONE_SIDED[1], avgWeight=90.0, volume=900.0),
        ],
    }


def write_tonal_export(root: Path, workouts: list[dict], name: str,
                       exported_at: str, pagination: dict | None = None) -> Path:
    d = root / "tonal"
    d.mkdir(parents=True, exist_ok=True)
    # Mirrors toneget v3.1.0 exactly: workoutCatalog and customWorkouts are
    # DICTS keyed by template id, and strengthScoreHistory is a LIST of
    # per-date records each carrying every scope at once.
    payload = {
        "version": "3.0",
        "exportedAt": exported_at,
        "exportedWith": "ToneGet v3.1.0",
        "user": {"id": "u1"},
        "profile": {"totalWorkouts": len(workouts)},
        "workouts": workouts,
        "activityNames": {w["id"]: w.get("workoutTitle") for w in workouts},
        "workoutCatalog": {
            "tpl1": {"id": "tpl1", "title": "Back Day", "workoutType": "PROGRAM"},
            "tplCustom": {"id": "tplCustom", "title": "My Own Back Day",
                          "workoutType": "Custom", "userId": "u1"},
        },
        "customWorkouts": {
            "tplCustom": {"id": "tplCustom", "title": "My Own Back Day"},
        },
        "strengthScoreHistory": [
            {"date": "2026-09-07", "overall": 121.5, "upper": 64.0,
             "lower": 41.0, "core": 16.5},
            {"date": "2026-09-01", "overall": 118.0, "upper": 62.0,
             "lower": 40.0, "core": 16.0},
        ],
        "currentStrengthScores": {
            "raw": [{"strengthBodyRegion": "Upper", "score": 64.0}],
            "parsed": {
                "regions": {"Overall": 121.5, "Upper": 64.0},
                "muscles": {
                    "Lats":   {"score": 42, "region": "Upper",
                               "updatedAt": "2026-09-07T12:00:00Z"},
                    "Biceps": {"score": 31, "region": "Upper",
                               "updatedAt": "2026-09-07T12:00:00Z"},
                    "Glutes": {"score": 22, "region": "Lower",
                               "updatedAt": "2026-09-07T12:00:00Z"},
                },
            },
        },
    }
    if pagination is not None:
        payload["pagination"] = pagination
    path = d / name
    path.write_text(json.dumps(payload))
    return path


def withings_payload() -> list[dict]:
    def epoch(day: str, hhmm: str) -> int:
        h, m = (int(x) for x in hhmm.split(":"))
        return int(datetime.fromisoformat(day).replace(hour=h, minute=m, tzinfo=TZ).timestamp())

    return [
        # BodyScan weigh-in. 9480 * 10^-2 = 94.80 kg
        {"grpid": 1001, "category": 1, "attrib": 0, "modelid": 46,
         "date": epoch("2026-09-02", "07:00"), "measures": [
             {"value": 9480, "type": 1,   "unit": -2},
             {"value": 1830, "type": 6,   "unit": -2},
             {"value": 1735, "type": 8,   "unit": -2},
             {"value": 7745, "type": 5,   "unit": -2},
             {"value": 7210, "type": 76,  "unit": -2},
             {"value": 350,  "type": 88,  "unit": -2},
             {"value": 5200, "type": 77,  "unit": -2},
             {"value": 58,   "type": 11,  "unit": 0},
         ]},
        # Pulse wave velocity and vascular age (155): BodyScan sends them as a
        # group of their own, a minute after the weigh-in, with no weight.
        {"grpid": 1006, "category": 1, "attrib": 8, "modelid": 10,
         "date": epoch("2026-09-02", "07:01"), "measures": [
             {"value": 712, "type": 91,  "unit": -2},
             {"value": 380, "type": 155, "unit": -1},
         ]},
        # BP Vision reading, same morning, separate group.
        {"grpid": 1002, "category": 1, "attrib": 0, "modelid": 93,
         "date": epoch("2026-09-02", "07:10"), "measures": [
             {"value": 122, "type": 10, "unit": 0},
             {"value": 78,  "type": 9,  "unit": 0},
             {"value": 62,  "type": 11, "unit": 0},
         ]},
        # A type nothing names: must be captured, not dropped.
        {"grpid": 1003, "category": 1, "attrib": 0, "modelid": 46,
         "date": epoch("2026-09-02", "07:00"), "measures": [
             {"value": 4150, "type": 999, "unit": -2},
         ]},
        # Segmental body composition: the SAME meastype five times, told
        # apart only by `position`. Keying on grpid+meastype keeps one of
        # five - this is the regression that bug left behind.
        {"grpid": 1005, "category": 1, "attrib": 0, "modelid": 46,
         "date": epoch("2026-09-02", "07:00"), "measures": [
             {"value": 3750, "type": 173, "unit": -2, "position": 12,
              "algo": 3000100, "fm": 3},
             {"value": 1010, "type": 173, "unit": -2, "position": 11,
              "algo": 3000100, "fm": 3},
             {"value": 1010, "type": 173, "unit": -2, "position": 10,
              "algo": 3000100, "fm": 3},
             {"value": 340,  "type": 173, "unit": -2, "position": 3,
              "algo": 3000100, "fm": 3},
             {"value": 330,  "type": 173, "unit": -2, "position": 2,
              "algo": 3000100, "fm": 3},
             {"value": 323,  "type": 175, "unit": -2, "position": 3,
              "algo": 3000100, "fm": 3},
             {"value": 313,  "type": 175, "unit": -2, "position": 2,
              "algo": 3000100, "fm": 3},
         ]},
        # A user objective, not a measurement: must stay out of the views.
        {"grpid": 1004, "category": 2, "attrib": 0, "modelid": 46,
         "date": epoch("2026-09-02", "07:00"), "measures": [
             {"value": 9000, "type": 1, "unit": -2},
         ]},
    ]


# ------------------------------------------------------------------- tests

BRIEF_DAY = date(2026, 9, 30)


def test_tonal_sets(root: Path, cfg, cfg_path: Path) -> None:
    """Sets keyed by their own id, upserted and never deleted, and refused
    whole when an export would lose one. Its own database and names file."""
    print("\nTonal sets - keyed by their own id, append-only, guarded")

    def check_(label: str, cond: bool, detail: str = "") -> None:
        check(label, cond, detail if not cond else "")

    c = db.connect(root / "sets.db")
    db.apply_schema(c)
    names = Path(cfg.tonal["movement_names"])
    names.unlink(missing_ok=True)
    n_exports = iter(range(100))

    def load(workouts: list[dict]) -> dict:
        path = write_tonal_export(root / "sets", workouts,
                                  f"tonal_workouts_sets{next(n_exports):02d}.json",
                                  "2026-09-10T08:00:00Z")
        return tonal.sync(c, cfg, path)

    def count(sql: str = "SELECT COUNT(*) FROM tonal_set", *args) -> int:
        return c.execute(sql, args).fetchone()[0]

    def wk_a(**over) -> dict:
        # One-sided sets, a two-sided pair, a warm-up, and a set never performed.
        d = "2026-09-01"
        sets = [make_set("a", 0, day=d, warmUp=True, avgWeight=30.0, volume=300.0),
                make_set("a", 1, day=d, prs=["Power", "Strength"]),
                make_set("a", 2, day=d, movement=TWO_SIDED, set_id="pp", side=0,
                         avgWeight=50.0, volume=500.0, maxConPower=300.0),
                make_set("a", 3, day=d, movement=TWO_SIDED, set_id="pp", side=1,
                         avgWeight=50.02, volume=500.2, maxConPower=330.0),
                make_set("a", 4, day=d, set_id="ps9", beginTime=ZERO_TIME, endTime=ZERO_TIME,
                         duration=0, repCount=0, avgWeight=0, volume=0.0)]
        for s in sets:
            s.update(over.get(s["id"], {}))
        return {**make_workout(0, d, sets=sets), "id": "a"}

    def wk(wid: str, day: str, sets: list[dict] | None = None) -> dict:
        # The same prescribed set ids (ps0, ps1) as workout a.
        return {**make_workout(0, day, sets=sets if sets is not None else
                               [make_set(wid, 0, day=day), make_set(wid, 1, day=day)]),
                "id": wid}

    b = wk("b", "2026-09-02")
    unk = wk("u", "2026-09-03", [make_set("u", 0, day="2026-09-03", movement=UNKNOWN_MOVE)])
    r = load([wk_a(), b, unk])
    check_("an export of real-shaped sets loads clean", r["status"] == "ok", str(r))
    check_("every set is stored under its own id, though setId repeats across workouts",
           count() == 8 and count("SELECT COUNT(DISTINCT workout_id) FROM tonal_set "
                                  "WHERE prescribed_set_id = 'ps0'") == 3)
    row = c.execute("SELECT * FROM tonal_set WHERE set_uid = 'a-s1'").fetchone()
    check_("set fields land in their columns, flags as 1/0 and PRs as a JSON list",
           row["avg_weight_lb"] == 45.0 and row["rep_count"] == 10 and row["volume"] == 450.0
           and row["prescribed_reps"] == 10 and row["max_con_power"] == 300.0
           and row["rom_length_in"] == 400.0 and row["warm_up"] == 0 and row["spotter"] == 0
           and json.loads(row["prs"]) == ["Power", "Strength"] and row["workout_id"] == "a"
           and row["begin_utc"] == "2026-09-01T19:31:00Z" and row["tz_source"] == "z"
           and row["end_utc"] == "2026-09-01T19:31:40Z" and row["local_date"] == "2026-09-01"
           and json.loads(row["raw_json"])["setId"] == "ps1", str(dict(row)))
    check_("the warm-up flag is kept",
           count("SELECT warm_up FROM tonal_set WHERE set_uid = 'a-s0'") == 1)
    z = c.execute("SELECT begin_utc, local_date, tz_source FROM tonal_set "
                  "WHERE set_uid = 'a-s4'").fetchone()
    check_("a set never performed (Go's zero time) has no timestamp, and says why",
           tuple(z) == (None, None, "zero_time")
           and tonal.parse_ts(ZERO_TIME, TZ) == (None, None, None, "zero_time"), str(tuple(z)))
    check_("a two-sided movement is one row per side",
           [tuple(x) for x in c.execute("SELECT side_number, avg_weight_lb FROM tonal_set "
                                        "WHERE prescribed_set_id='pp' ORDER BY side_number")]
           == [(0, 50.0), (1, 50.02)])
    check_("the run summary counts the sets it loaded",
           r["notes"].startswith("sets: 8 in export, 8 new, 0 already stored")
           and "1 unknown (1 sets" in r["notes"], r["notes"])

    both = {x["set_key"]: x for x in c.execute(
        "SELECT * FROM v_tonal_set_both_sides WHERE workout_id = 'a'")}
    pp = both.get("pp")
    check_("both sides collapse into one set: weight averaged, reps and volume summed",
           pp is not None and pp["sides"] == 2 and abs(pp["avg_weight_lb"] - 50.01) < 1e-9
           and pp["reps"] == 20 and abs(pp["volume"] - 1000.2) < 1e-9, str(pp and dict(pp)))
    check_("and the set never performed is left out", set(both) == {"ps0", "ps1", "pp"},
           str(set(both)))
    hist = {x["movement_id"]: x for x in c.execute(
        "SELECT * FROM v_movement_history WHERE day = '2026-09-01'")}
    names_by_id = {m["id"]: m["name"] for m in CATALOG}
    check_("movement history is named from the catalog, sides collapsed",
           hist[TWO_SIDED]["movement_name"] == names_by_id[TWO_SIDED]
           and hist[TWO_SIDED]["sets"] == 1 and hist[ONE_SIDED[0]]["sets"] == 2,
           str({k: dict(v) for k, v in hist.items()}))
    unknown = c.execute("SELECT * FROM v_tonal_unknown_movements").fetchall()
    check_("a movement no file names is listed as unknown, not dropped",
           [(x["movement_id"], x["sets"]) for x in unknown] == [(UNKNOWN_MOVE, 1)]
           and count("SELECT COUNT(*) FROM tonal_set WHERE movement_id = ?", UNKNOWN_MOVE) == 1)

    # A later export: one value changed, one field gone, and a new workout.
    a2 = wk_a(**{"a-s1": {"repCount": 11}})
    del a2["workoutSetActivity"][0]["avgWeight"]
    r = load([a2, b, unk, wk("d", "2026-09-04")])
    check_("a later export upserts: a changed value updates, and a new workout's sets add",
           r["status"] == "ok" and count() == 10
           and count("SELECT rep_count FROM tonal_set WHERE set_uid = 'a-s1'") == 11, str(r))
    check_("a field missing from the newer export can't NULL the stored value",
           c.execute("SELECT avg_weight_lb FROM tonal_set WHERE set_uid = 'a-s0'").fetchone()[0]
           == 30.0)
    held = [a2, b, unk, wk("d", "2026-09-04")]

    # Bad exports, each refused whole. Workout e is new in each one, so a
    # partial load would show up as e being stored.
    e = wk("e", "2026-09-05")

    def refused(label: str, workouts: list[dict], cause: str) -> None:
        before = count()
        r = load(workouts)
        check_(label, r["status"] == "rejected" and any(cause in a for a in r["anomalies"])
               and count() == before and before == 10
               and count("SELECT COUNT(*) FROM tonal_workout WHERE workout_id = 'e'") == 0,
               str(r["anomalies"]))

    short = wk_a()
    short["workoutSetActivity"].pop(1)
    refused("an export missing one stored set is refused whole", [short, *held[1:], e],
            "missing 1 set(s) already in the database")
    renamed = [{**{k: v for k, v in w.items() if k != "workoutSetActivity"},
                "exerciseSets": w["workoutSetActivity"]} for w in [*held, e]]
    refused("so is one whose sets field was renamed, instead of wiping the stored sets",
            renamed, "missing 10 set(s)")
    no_id = wk("e", "2026-09-05")
    del no_id["workoutSetActivity"][0]["id"]
    refused("so is one with a set that has no id to key it by", [*held, no_id], "no `id`")
    twin = wk("e", "2026-09-05", [make_set("e", 0, day="2026-09-05"),
                                  make_set("e", 1, day="2026-09-05", set_id="ps0")])
    refused("so is one that repeats a workout, setId and side", [*held, twin], "repeat")
    dup = wk("e", "2026-09-05", [make_set("e", 0, day="2026-09-05"),
                                 make_set("e", 1, day="2026-09-05")])
    dup["workoutSetActivity"][1]["id"] = "e-s0"
    refused("so is one that repeats a set id", [*held, dup], "more than once")

    drift = [json.loads(json.dumps(w)) for w in held]
    for w in drift:
        for s in w["workoutSetActivity"]:
            s.pop("avgWeight", None)
    r = load(drift)
    check_("a set field gone from every set warns, and the stored values stay",
           r["status"] == "ok_warn" and any("avgWeight" in a for a in r["anomalies"])
           and count("SELECT COUNT(*) FROM tonal_set WHERE avg_weight_lb IS NULL") == 0,
           str(r["anomalies"]))

    print("\nTonal movements - the catalog, your names, and the side check")
    catalog_file = Path(cfg.tonal["movement_catalog"])
    digest = tonal.sha256_file(catalog_file)
    check_("every catalog movement loads, with its two-sided flag and arms",
           count("SELECT COUNT(*) FROM tonal_movement WHERE origin = 'catalog'") == len(CATALOG)
           and count("SELECT is_two_sided FROM tonal_movement WHERE movement_id = ?",
                     TWO_SIDED) == 1
           and count("SELECT COUNT(*) FROM tonal_movement WHERE arms_together = 1") > 0)
    check_("the sides seen in the sets agree with the catalog's isTwoSided",
           "sides match the catalog on 2 of 2" in tonal.movement_report(c)[0]
           and tonal.movement_report(c)[1] == [], str(tonal.movement_report(c)))

    names.write_text(f'["{UNKNOWN_MOVE}"]\nname = "Mystery Row"\nis_two_sided = false\n'
                     f'muscle_groups = ["Back"]\n\n'
                     f'["{ONE_SIDED[1]}"]\nname = "My Name For It"\n')
    r = load(held)      # an export already seen: names still refresh
    m = c.execute("SELECT * FROM tonal_movement WHERE movement_id = ?", (UNKNOWN_MOVE,)).fetchone()
    check_("a name you supply is applied on the next run, even one that skips its export",
           m is not None and m["name"] == "Mystery Row" and m["origin"] == "manual"
           and m["is_two_sided"] == 0 and json.loads(m["muscle_groups"]) == ["Back"]
           and m["arms_together"] is None
           and not c.execute("SELECT 1 FROM v_tonal_unknown_movements").fetchone(), str(r))
    m = c.execute("SELECT * FROM tonal_movement WHERE movement_id = ?", (ONE_SIDED[1],)).fetchone()
    cat_m = next(x for x in CATALOG if x["id"] == ONE_SIDED[1])
    check_("your entry for a catalog id wins, and keeps the catalog's other fields",
           m["name"] == "My Name For It" and m["origin"] == "manual"
           and m["short_name"] == cat_m["shortName"]
           and m["is_two_sided"] == int(cat_m["isTwoSided"]))
    check_("and the catalog file is untouched",
           tonal.sha256_file(catalog_file) == digest)

    names.write_text(f'["{UNKNOWN_MOVE}"]\nname = "Mystery Row"\nis_two_sided = "no"\n\n'
                     '["not-an-id"]\nname = "x"\n\n'
                     f'["{TWO_SIDED}"]\nshort_name = "nameless"\n')
    r = load(held)
    check_("a bad entry is skipped and named, never guessed at, and the load goes on",
           r["status"] == "ok_warn"
           and any("skipped (bad key(s) is_two_sided)" in a for a in r["anomalies"])
           and any("not a movement id" in a for a in r["anomalies"])
           and any("skipped (no name)" in a for a in r["anomalies"]), str(r["anomalies"]))
    names.write_text("this is = not [toml")
    r = load(held)
    check_("an unreadable names file warns; the catalog still loads",
           r["status"] == "ok_warn" and any("could not be read" in a for a in r["anomalies"])
           and count("SELECT COUNT(*) FROM tonal_movement") >= len(CATALOG), str(r["anomalies"]))
    names.unlink()

    # ONE_SIDED[0] is one-sided in the catalog; give it a side 1.
    odd = wk("f", "2026-09-06", [make_set("f", 0, day="2026-09-06", set_id="pz"),
                                 make_set("f", 1, day="2026-09-06", set_id="pz", side=1)])
    r = load([*held, odd])
    check_("if the sets stop agreeing with isTwoSided, the load warns and names the movement",
           r["status"] == "ok_warn" and any(
               "isTwoSided disagrees" in a and names_by_id[ONE_SIDED[0]] in a
               for a in r["anomalies"]), str(r["anomalies"]))

    # A name, once given, stays when the entry is removed; so use a new id.
    other = UNKNOWN_MOVE[:-1] + "2"
    out = io.StringIO()
    c.execute("INSERT INTO tonal_set (set_uid, workout_id, movement_id, raw_json, "
              "first_seen_utc, last_seen_utc) VALUES ('x', 'f', ?, '{}', 'x', 'x')", (other,))
    with contextlib.redirect_stdout(out):
        sync_all.cmd_status(c, cfg, today=date(2026, 9, 7))
    check_("status lists the movements still without a name",
           "Unnamed Tonal movements" in out.getvalue() and other in out.getvalue()
           and UNKNOWN_MOVE not in out.getvalue(), out.getvalue())
    c.close()


# The set table as it was before 2026-10-22, keyed by position.
OLD_TONAL_SET = """
CREATE TABLE tonal_set (
    set_uid TEXT PRIMARY KEY, workout_id TEXT NOT NULL REFERENCES tonal_workout(workout_id),
    set_index INTEGER NOT NULL, movement TEXT, reps INTEGER, weight_lb REAL, volume REAL,
    rom REAL, power REAL, tempo REAL, raw_json TEXT);
CREATE INDEX ix_tonal_set_workout  ON tonal_set(workout_id);
CREATE INDEX ix_tonal_set_movement ON tonal_set(movement);
CREATE VIEW v_movement_history AS
SELECT w.local_date AS day, s.movement, COUNT(*) AS sets, MAX(s.weight_lb) AS top_weight_lb
FROM tonal_set s JOIN tonal_workout w ON w.workout_id = s.workout_id
GROUP BY w.local_date, s.movement;
"""


def test_tonal_migration(root: Path, cfg) -> None:
    print("\nSchema - the old position-keyed tonal_set is kept aside and refilled")

    def check_(label: str, cond: bool, detail: str = "") -> None:
        check(label, cond, detail if not cond else "")

    old = db.connect(root / "oldsets.db")
    db.apply_schema(old)
    for v in ("v_movement_history", "v_tonal_set_both_sides", "v_tonal_unknown_movements"):
        old.execute(f"DROP VIEW {v}")
    old.execute("DROP TABLE tonal_set")
    old.executescript(OLD_TONAL_SET)
    old.execute("INSERT INTO tonal_workout (workout_id, begin_utc, local_date, tz_source, "
                "raw_json, first_seen_utc, last_seen_utc) VALUES "
                "('w0', '2026-09-01T19:30:00Z', '2026-09-01', 'z', '{}', 'x', 'x')")
    sets = [make_set("w0", 0), make_set("w0", 1, movement=TWO_SIDED, set_id="pp"),
            make_set("w0", 2, movement=TWO_SIDED, set_id="pp", side=1)]
    for i, s in enumerate(sets):
        old.execute("INSERT INTO tonal_set (set_uid, workout_id, set_index, movement, raw_json) "
                    "VALUES (?, 'w0', ?, ?, ?)", (f"w0:{i}", i, s["movementId"], json.dumps(s)))
    # From an export trimmed long ago: no id, so nothing to key it by.
    old.execute("INSERT INTO tonal_set (set_uid, workout_id, set_index, movement, raw_json) "
                "VALUES ('w0:3', 'w0', 3, 'Lat Pulldown', '{\"reps\": 10}')")
    old.commit()

    db.apply_schema(old)
    msgs = tonal.migrate(old, cfg)
    cols = {r[1] for r in old.execute("PRAGMA table_info(tonal_set)")}
    check_("the old table is renamed aside, every row kept",
           old.execute("SELECT COUNT(*) FROM tonal_set_v1").fetchone()[0] == 4
           and "set_index" in {r[1] for r in old.execute("PRAGMA table_info(tonal_set_v1)")})
    check_("the new tonal_set is refilled from the old raw_json, keyed by each set's id",
           "set_index" not in cols and "prescribed_set_id" in cols
           and sorted(r[0] for r in old.execute("SELECT set_uid FROM tonal_set"))
           == ["w0-s0", "w0-s1", "w0-s2"]
           and msgs[:1] == ["tonal_set refilled from tonal_set_v1: 3 set(s); "
                            "1 without an id stay in tonal_set_v1 only."], str(msgs))
    indexes = dict(old.execute("SELECT name, tbl_name FROM sqlite_master WHERE type = 'index' "
                               "AND name LIKE 'ix_tonal_set%'").fetchall())
    check_("its index names now belong to the new table",
           indexes == {"ix_tonal_set_workout": "tonal_set", "ix_tonal_set_movement": "tonal_set"},
           str(indexes))
    check_("the views read the new table",
           old.execute("SELECT sets FROM v_movement_history WHERE movement_id = ?",
                       (TWO_SIDED,)).fetchone()[0] == 1)
    db.apply_schema(old)
    check_("applying the schema again moves nothing and refills nothing",
           db.retire_tables(old) == [] and tonal.migrate(old, cfg) == []
           and old.execute("SELECT COUNT(*) FROM tonal_set").fetchone()[0] == 3
           and old.execute("SELECT COUNT(*) FROM tonal_set_v1").fetchone()[0] == 4)
    old.close()
    fresh = db.connect(root / "freshsets.db")
    db.apply_schema(fresh)
    check_("a new database has no old table to refill from",
           tonal.migrate(fresh, cfg) == []
           and not fresh.execute("SELECT 1 FROM sqlite_master WHERE name = 'tonal_set_v1'"
                                 ).fetchone())
    fresh.close()


def test_tonal_tab(root: Path, cfg) -> None:
    """The Tonal tab's numbers: working load per movement per day."""
    print("\nDashboard - the Tonal tab: working load per movement, one point per day")

    def check_(label: str, cond: bool, detail: str = "") -> None:
        check(label, cond, detail if not cond else "")

    info = lambda m: m.get("onMachineInfo") or {}
    PAIR = next(m["id"] for m in CATALOG if m["onMachine"] and not m["isTwoSided"]
                and info(m).get("trainerArmsPulledAtSameTime"))
    ONE = next(m["id"] for m in CATALOG if m["onMachine"] and not m["isTwoSided"]
               and info(m).get("trainerArmsPulledAtSameTime") is False)
    two = next(m for m in CATALOG if m["isTwoSided"] and m["onMachine"])
    OFF = next(m["id"] for m in CATALOG if not m["onMachine"])
    names = {m["id"]: m["name"] for m in CATALOG}

    c = db.connect(root / "tonal-tab.db")
    db.apply_schema(c)
    check_("a database with no sets gives no Tonal tab data",
           dashboard.movement_history(c) is None)
    tonal.load_movements(c, cfg)
    n = iter(range(10_000))

    def workout(wid: str, title: str, day: str, hh: int, sets: list[tuple]) -> None:
        """sets: (movement, avg weight, warm-up, performed, side, set id)."""
        c.execute("INSERT INTO tonal_workout (workout_id, begin_utc, local_date, tz_source, title, "
                  "raw_json, first_seen_utc, last_seen_utc) VALUES (?,?,?,?,?,?,?,?)",
                  (wid, f"{day}T{hh:02d}:00:00Z", day, "z", title, "{}", "x", "x"))
        for mv, wt, warm, done, side, sid in sets:
            k = next(n)
            c.execute("INSERT INTO tonal_set (set_uid, workout_id, prescribed_set_id, movement_id, "
                      "side_number, begin_utc, local_date, tz_source, avg_weight_lb, warm_up, "
                      "raw_json, first_seen_utc, last_seen_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (f"s{k}", wid, sid or f"p{k}", mv, side,
                       f"{day}T{hh:02d}:{k % 60:02d}:00Z" if done else None, day,
                       "z" if done else "zero_time", wt, int(warm), "{}", "x", "x"))

    S = lambda mv, wt, warm=False, done=True, side=0, sid=None: (mv, wt, warm, done, side, sid)
    workout("r1", "Back&Biceps", "2026-06-01", 19,
            [S(PAIR, 30, warm=True), S(PAIR, 45), S(PAIR, 50), S(PAIR, 61), S(PAIR, 0),
             S(PAIR, 0, done=False), S(ONE, 40), S(ONE, 42), S(ONE, 44), S(OFF, 0), S(OFF, 0)])
    workout("r2", "Back&Biceps", "2026-06-08", 19, [S(PAIR, 52), S(PAIR, 52), S(ONE, 0)])
    workout("r3", "_D1-Chest&Tri", "2026-07-20", 19,
            [S(PAIR, 60), S(UNKNOWN_MOVE, 70), S(UNKNOWN_MOVE, 70),
             S(two["id"], 50.0, sid="pp"), S(two["id"], 50.02, side=1, sid="pp")])
    workout("r4", "Back&Biceps", "2026-07-25", 19, [S(OFF, 0), S(ONE, 46), S(PAIR, 58)])
    workout("r5", "Finisher", "2026-07-25", 20, [S(PAIR, 62)])
    c.commit()

    t = dashboard.movement_history(c)
    M = t["movements"]
    pts = lambda mv: [(p[0], p[1], p[2], p[3]) for p in M[mv]["pts"]]
    check_("working load is the median of the day's sets, without warm-ups or weightless sets, "
           "doubled where both arms pull together",
           pts(PAIR)[0] == ("2026-06-01", 100.0, 3, 0), str(pts(PAIR)))
    check_("a one-arm movement is not doubled", pts(ONE)[0] == ("2026-06-01", 42.0, 3, 0),
           str(pts(ONE)))
    check_("a day the movement had no weighted set is a gap, not a zero",
           [p[1] for p in pts(ONE)] == [42.0, None, 46.0] and [p[3] for p in pts(ONE)] == [0, 0, 0],
           str(pts(ONE)))
    check_(f"days within {dashboard.JOIN_DAYS} days are joined; a longer gap is not",
           [p[3] for p in pts(PAIR)] == [0, 1, 0, 1], str(pts(PAIR)))
    check_("two workouts on one day are one point, the median of both",
           pts(PAIR)[3] == ("2026-07-25", 120.0, 2, 1) and len(pts(PAIR)) == 4, str(pts(PAIR)))
    arms2 = 2 if info(two).get("trainerArmsPulledAtSameTime") else 1
    check_("the two sides of a two-sided movement are one set at their shared weight",
           abs(pts(two["id"])[0][1] - round(50.01 * arms2, 1)) < 1e-9 and pts(two["id"])[0][2] == 1,
           str(pts(two["id"])))
    check_("a movement with no weighted set gets no line, so the page lists it",
           not M[OFF]["weighted"] and M[OFF]["pts"] == [] and M[OFF]["sessions"] == 2)
    u = M[UNKNOWN_MOVE]
    check_("an unnamed movement is charted undoubled, with its short id and its routines",
           u["name"] is None and u["arms"] is None and u["short"] == UNKNOWN_MOVE[:8]
           and pts(UNKNOWN_MOVE) == [("2026-07-20", 70.0, 2, 0)] and u["titles"] == ["_D1-Chest&Tri"],
           str(u))
    check_("named movements carry the catalog name and arms flag",
           M[PAIR]["name"] == names[PAIR] and M[PAIR]["arms"] == 1 and M[ONE]["arms"] == 0)
    R = t["routines"]
    check_("routines are listed newest first, so the default is the one done most recently",
           [(r["title"], r["last"], r["sessions"]) for r in R]
           == [("Finisher", "2026-07-25", 1), ("Back&Biceps", "2026-07-25", 3),
               ("_D1-Chest&Tri", "2026-07-20", 1)], str(R))
    check_("a routine's charts are the movements of its newest session, in the order done",
           R[1]["movements"] == [OFF, ONE, PAIR], str(R[1]["movements"]))
    check_("sets are counted per movement across every routine, never-begun sets left out",
           M[PAIR]["sets"] == 10 and M[ONE]["sets"] == 5, f"{M[PAIR]['sets']} {M[ONE]['sets']}")
    check_("the detail chart opens on the movement with the most sets, not the first one done",
           R[1]["promote"] == PAIR and R[0]["promote"] == PAIR, str(R[1].get("promote")))
    old_ = {"sets": 5, "pts": [["2023-08-04", None], ["2023-08-11", 21.9]]}
    new_ = {"sets": 5, "pts": [["2022-08-18", 20.0]]}
    more = {"sets": 6, "pts": [["2026-05-25", 57.6]]}
    check_("on equal sets the earliest first day with a weight wins; more sets beat an earlier start",
           min([old_, new_], key=dashboard.promote_key) is new_
           and min([new_, more], key=dashboard.promote_key) is more)
    check_("and each chart is the movement's whole history, other routines included",
           "2026-07-20" in [p[0] for p in pts(PAIR)]
           and t["titles"][M[PAIR]["pts"][2][4]] == "_D1-Chest&Tri")
    ax = dashboard.load_axis
    check_("each chart is scaled to its own loads, padded so no point sits on an edge",
           M[PAIR]["y"] == ax([100.0, 104.0, 120.0, 120.0])
           and M[PAIR]["y"][0] < 100.0 and M[PAIR]["y"][1] > 120.0 and M[PAIR]["y"][0] > 0
           and M[OFF]["y"] is None, str(M[PAIR]["y"]))
    lo, hi = ax([13.0, 25.0])
    check_("a light movement that nearly doubled gets an axis that shows it, not starting at zero",
           0 < lo < 13 and 25 < hi < 30, f"{lo}..{hi}")
    lo, hi = ax([41.0, 42.0, 43.0])
    check_("a history within a few pounds isn't zoomed into noise: the range has a floor",
           hi - lo >= dashboard.AXIS_MIN_SPAN_LB and lo < 41 and hi > 43, f"{lo}..{hi}")
    lo, hi = ax([150.0, 158.0])
    check_("and the floor grows with the load: a quarter of the heaviest",
           hi - lo >= 0.25 * 158, f"{lo}..{hi}")
    check_("the axis ends are whole units, so its labelled bottom reads cleanly",
           all(v == int(v) for v in ax([13.3, 25.7]) + M[PAIR]["y"]))
    check_("a range whose bottom is near zero starts at zero, and none goes below it",
           ax([2.0, 30.0])[0] == 0.0 and ax([1.0])[0] == 0.0 and ax([0.5, 0.6])[0] == 0.0)
    kg = dashboard.mass_factor("lb", "metric")
    lo, hi = ax([8.0, 9.0], kg)
    check_("the floor is in the display unit too",
           hi - lo >= dashboard.AXIS_MIN_SPAN_LB * kg and hi - lo < dashboard.AXIS_MIN_SPAN_LB,
           f"{lo}..{hi}")
    rows = c.execute("SELECT COUNT(*) FROM tonal_set").fetchone()[0]
    check_("aggregated before embedding: one point per movement per day, never a raw set",
           sum(len(m["pts"]) for m in M.values()) == 9 and rows == 23, f"{rows} sets")

    metric = dashboard.movement_history(c, lb=dashboard.mass_factor("lb", "metric"))
    check_("the load follows the display unit",
           abs(metric["movements"][PAIR]["pts"][0][1] - round(100 * dashboard.KG_PER_LB, 1)) < 1e-9)

    sync_all.cmd_snapshot(c, cfg, str(root / "tonal-tab-ro.db"))
    page = dashboard.render(dashboard.build_payload(
        db.connect(root / "tonal-tab-ro.db", read_only=True), cfg, today=date(2026, 7, 26)))
    blob = page.split('<script id="data" type="application/json">', 1)[1].split("</script>", 1)[0]
    check_("the page has a Tonal tab, with its data embedded and nothing fetched",
           'data-view="tonal"' in page and "<script src" not in page
           and json.loads(blob)["tonal"]["routines"][0]["title"] == "Finisher")

    print("\nTonal tab - quarterly load matrix")
    q = M[PAIR]["quarters"]
    check_("a quarter's cell is the median of its day loads, placed in the row's own range",
           q["start"] == "2026-04-01" and q["cells"] == [[102.0, 2, 0.0], [120.0, 2, 1.0]]
           and q["range"] == [102.0, 120.0], str(q))
    check_("a day without a weighted set counts as done but adds no load",
           M[ONE]["quarters"]["cells"] == [[42.0, 2, 0.0], [46.0, 1, 1.0]],
           str(M[ONE]["quarters"]))
    later = dashboard.movement_history(c, today=date(2026, 12, 1))["movements"][PAIR]["quarters"]
    check_("quarters run to today's, and one not done is absence - no value, no shade",
           len(later["cells"]) == 3 and later["cells"][-1] == [None, 0, None], str(later))
    ql = dashboard.quarterly_load([["2026-01-05", None, 0, 0, 0], ["2026-07-01", 50.0, 1, 0, 0]])
    check_("done with no weight is told apart from not done; a one-value row sits mid-range",
           ql["cells"] == [[None, 1, None], [None, 0, None], [50.0, 1, 0.5]], str(ql))
    check_("the newest load is the newest day that had a weight, with its date",
           M[PAIR]["current"] == ["2026-07-25", 120.0] and M[ONE]["current"] == ["2026-07-25", 46.0],
           str(M[ONE]["current"]))
    check_("a movement far below Tonal's ceiling gets no cap line",
           M[PAIR]["cap"] is None and M[ONE]["cap"] is None)
    workout("r6", "Back&Biceps", "2026-08-03", 19, [S(ONE, 90)])
    c.commit()
    near = dashboard.movement_history(c)["movements"][ONE]
    check_("one near it gets the 100 lb-per-arm cap, and an axis that reaches it",
           near["cap"] == dashboard.TONAL_MAX_LB_PER_ARM and near["y"][1] >= near["cap"], str(near["y"]))
    workout("r7", "Back&Biceps", "2026-08-10", 19, [S(PAIR, 90)])
    c.commit()
    both = dashboard.movement_history(c)["movements"]
    check_("the cap is per movement in total-load terms: 200 where both arms pull, 100 for one arm",
           both[PAIR]["cap"] == 2 * dashboard.TONAL_MAX_LB_PER_ARM and both[ONE]["cap"] == 100.0
           and both[PAIR]["y"][1] >= 200, f"{both[PAIR]['cap']} {both[ONE]['cap']}")
    kg = dashboard.mass_factor("lb", "metric")
    mk = dashboard.movement_history(c, lb=kg)["movements"]
    check_("and in kg on a metric page, still doubled for both arms",
           mk[PAIR]["cap"] == round(200 * kg, 1) and mk[ONE]["cap"] == round(100 * kg, 1),
           f"{mk[PAIR]['cap']} {mk[ONE]['cap']}")
    check_("the page draws the movement's own cap, not a flat 100",
           "c.y(M.cap)" in dashboard.TEMPLATE.read_text())
    c.close()


# An invented BodyScan weigh-in from the demo generator, not anyone's scan:
# 80 kg at 20% fat. By construction the whole body is exactly 64 kg
# fat-free, 16 kg fat, 60.8 kg muscle and 3.2 kg bone, and the five segments
# split it by the generator's published shares (wellness/demo.py).
SCAN = demo.body(80.0, 20.0)


def wepoch(day: str, hhmm: str) -> int:
    h, m = (int(x) for x in hhmm.split(":"))
    return int(datetime.fromisoformat(day).replace(hour=h, minute=m, tzinfo=TZ).timestamp())


def scan_group(grpid: int, day: str, hhmm: str, b: dict = SCAN, *, segments: bool = True,
               muscle: bool = True, edit=None) -> dict:
    """One weigh-in group shaped like the API's, from demo.scan_group. `edit`
    changes its measures in place, to break it on purpose."""
    g = demo.scan_group(grpid, wepoch(day, hhmm), b, segments=segments, muscle=muscle,
                        algo=demo.ALGO_A)
    if edit:
        edit(g["measures"])
    return g


def raw_values(g: dict) -> dict:
    """(type, position) -> the decoded value, for one group."""
    return {(m["type"], m.get("position")): m["value"] * 10 ** m["unit"] for m in g["measures"]}


def test_body_scan(root: Path, cfg) -> None:
    """Meastype names, the segment partition, and the Body Scan tab's numbers."""
    print("\nSchema - the nine BodyScan codes, named on an existing database")

    def check_(label: str, cond: bool, detail: str = "") -> None:
        check(label, cond, detail if not cond else "")

    old = db.connect(root / "meastype-names.db")
    db.apply_schema(old)
    # The state a database was in before the names: the loader's discovered
    # rows, a stored reading carrying unknown_155, the wrong 140 seed, and
    # one code the user has already named themselves.
    old.execute("UPDATE withings_meastype SET metric = 'unknown_' || meastype, unit_label = NULL, "
                "origin = 'discovered' WHERE meastype IN (155, 168, 169, 170, 196, 197, 198, 226)")
    old.execute("UPDATE withings_meastype SET metric = 'my_age_number', origin = 'confirmed' "
                "WHERE meastype = 227")
    old.execute("INSERT INTO withings_meastype VALUES (140, 'vascular_age', 'years', 'seeded')")
    old.execute("INSERT INTO withings_measure (measure_uid, grpid, taken_utc, taken_epoch, local_date, "
                "meastype, position, metric, value, category, loaded_utc) VALUES "
                "('9:155:-', '9', '2026-10-20T12:15:00Z', 0, '2026-10-20', 155, NULL, "
                "'unknown_155', 63.0, 1, 'x')")
    old.commit()
    db.apply_schema(old)
    names = dict(old.execute("SELECT meastype, metric FROM withings_meastype").fetchall())
    check_("a re-applied schema renames the discovered rows: a mapping change, no ALTER TABLE",
           names[155] == "vascular_age" and names[226] == "basal_metabolic_rate"
           and names[168] == "extracellular_water", str(names))
    check_("and the reading already stored takes the new name",
           old.execute("SELECT metric FROM withings_measure WHERE measure_uid = '9:155:-'"
                       ).fetchone()[0] == "vascular_age")
    check_("a name the user confirmed is left alone",
           names[227] == "my_age_number", names[227])
    check_("the wrong 140 'vascular_age' seed is gone, so vascular age has one code",
           140 not in names and list(names.values()).count("vascular_age") == 1)
    check_("nothing is left unnamed",
           not old.execute("SELECT 1 FROM v_withings_unknown_types").fetchone())
    old.execute("INSERT INTO withings_meastype VALUES (140, 'vascular_age', 'years', 'seeded')")
    old.execute("INSERT INTO withings_measure (measure_uid, grpid, taken_utc, taken_epoch, local_date, "
                "meastype, value, category, loaded_utc) VALUES "
                "('9:140:-', '9', '2026-10-20T12:15:00Z', 0, '2026-10-20', 140, 1.0, 1, 'x')")
    old.commit()
    db.apply_schema(old)
    check_("but a 140 row with readings behind it is never deleted",
           old.execute("SELECT COUNT(*) FROM withings_meastype WHERE meastype = 140").fetchone()[0] == 1)
    old.close()

    print("\nWithings - BodyScan's five segments partition the whole body")
    scan = scan_group(8001, "2026-10-20", "07:15")
    val = raw_values(scan)
    seg_sum = lambda t, v=val: sum(v[(t, p)] for p in (2, 3, 10, 11, 12))
    TOL = 0.05
    check_("fat-free mass (173) over the five segments is the whole body's (5): 64.00",
           abs(val[(5, 7)] - 64.0) < 1e-9 and abs(seg_sum(173) - val[(5, 7)]) < TOL,
           f"{seg_sum(173)} vs {val[(5, 7)]}")
    check_("muscle mass (175) over the segments is the whole body's (76): 60.80",
           abs(val[(76, 7)] - 60.8) < 1e-9 and abs(seg_sum(175) - val[(76, 7)]) < TOL,
           f"{seg_sum(175)} vs {val[(76, 7)]}")
    check_("fat mass (174) too, to rounding: 16.00",
           abs(val[(8, 7)] - 16.0) < 1e-9 and abs(seg_sum(174) - val[(8, 7)]) < TOL,
           f"{seg_sum(174)} vs {val[(8, 7)]}")
    check_("whole-body fat-free minus muscle is the bone mass Withings reports (88): 3.20",
           abs(val[(5, 7)] - val[(76, 7)] - val[(88, 7)]) < 1e-9 and abs(val[(88, 7)] - 3.2) < 1e-9)
    check_("the sync's partition check passes the scan",
           guard.withings_partition([scan]) == [])

    c = db.connect(root / "bodyscan.db")
    db.apply_schema(c)

    def load(grps: list[dict]) -> dict:
        withings.fetch_measures = lambda cfg, **kw: (grps, 1, 1760000000, True)
        return withings.sync(c, cfg, full=True, ignore_rate_limit=True)

    def torso_fat_plus_1kg(ms):
        next(m for m in ms if m["type"] == 174 and m.get("position") == 12)["value"] += 100

    def no_right_arm(ms):
        ms[:] = [m for m in ms if m.get("position") != 2]

    r = load([scan_group(77, "2026-10-01", "07:00", edit=torso_fat_plus_1kg),
              scan_group(78, "2026-10-02", "07:00", edit=no_right_arm)])
    check_("a weigh-in whose segments stop adding up still loads, but the run warns",
           r["status"] == "ok_warn" and r["rows_upserted"] > 0
           and any(f"type 174 segments sum to {seg_sum(174) + 1:.2f} kg, whole-body type 8 "
                   "is 16.00 kg" in a for a in r["anomalies"])
           and any("type 173 has no segment at position(s) 2" in a for a in r["anomalies"]),
           str(r["anomalies"]))

    print("\nDashboard - Body Scan: each region's percentages over its OWN mass")
    comp = dashboard.composition
    torso = comp(40.0, 10.0, 38.0)        # round, invented: the docstring's example
    check_("a torso of 40 kg fat-free, 10 kg fat, 38 kg muscle: 76% muscle, 20% fat, 4% bone",
           round(torso["muscle_pct"], 6) == 76.0 and round(torso["fat_pct"], 6) == 20.0
           and round(torso["bone_pct"], 6) == 4.0 and torso["mass"] == 50.0, str(torso))
    trap = 100 * 38.0 / (64.0 + 10.0)
    check_("never over the whole body's fat-free mass, which would say 51%",
           round(trap) == 51 and abs(torso["muscle_pct"] - trap) > 20)
    check_("segment bone is derived: fat-free minus muscle, 2 kg",
           abs(torso["bone"] - 2.0) < 1e-9)
    check_("fat, muscle and bone are shares of one mass: they make 100%",
           abs(torso["fat_pct"] + torso["muscle_pct"] + torso["bone_pct"] - 100) < 1e-9)
    whole = comp(64.0, 16.0, 60.8)
    check_("whole body by the same formula: the mass is the weight, the fat % is Withings' own",
           abs(whole["mass"] - 80.0) < 1e-9 and abs(whole["fat_pct"] - 20.0) < 1e-9, str(whole))
    check_("and its derived bone is the bone mass Withings reports",
           abs(whole["bone"] - 3.2) < 1e-9)
    early = comp(85.0, 35.0, None)
    check_("before muscle mass existed: fat % only, nothing guessed for muscle or bone",
           round(early["fat_pct"], 2) == 29.17 and early["muscle_pct"] is None
           and early["bone"] is None and early["bone_pct"] is None, str(early))
    check_("no fat-free or fat mass, no composition", comp(None, 22.0, 80.0) is None)

    # An invented history across three scales: fat % only, then muscle too,
    # then BodyScan segments. Then a heavier re-weigh later the same day.
    first = demo.body(90.0, 28.0)
    second = demo.body(86.0, 24.0)
    seg_start = demo.body(82.0, 21.0)
    reweigh = demo.body(80.8, 20.6)
    r = load([scan_group(1, "2016-03-07", "07:00", first, segments=False, muscle=False),
              scan_group(2, "2020-05-11", "07:00", second, segments=False),
              scan_group(3, "2025-04-14", "07:30", seg_start),
              scan_group(8001, "2026-10-20", "07:15"),
              scan_group(5, "2026-10-20", "19:30", reweigh)])
    check_("the history loads with no partition warning: every weigh-in adds up",
           not any("BodyScan check" in a for a in r["anomalies"]), str(r["anomalies"]))
    c.execute("DELETE FROM withings_measure WHERE grpid IN ('77', '78')")
    c.commit()
    lb = dashboard.mass_factor("kg", "imperial")
    bs = dashboard.body_scan(c, TZ, lb, date(2026, 10, 23))
    R = {x["key"]: x for x in bs["regions"]}
    check_("six regions, whole body first", [x["key"] for x in bs["regions"]]
           == ["whole", "torso", "arm_left", "arm_right", "leg_left", "leg_right"])
    check_("whole-body fat % from the first scale, muscle % from the second, segments from BodyScan",
           R["whole"]["first"] == {"fat_pct": "2016-03-07", "muscle_pct": "2020-05-11"}
           and R["torso"]["first"] == {"fat_pct": "2025-04-14", "muscle_pct": "2025-04-14"},
           str(R["whole"]["first"]))
    check_("only fat % is a series: muscle % mirrors it, so it's kept as a number",
           set(R["torso"]) >= {"series", "weekly", "long", "axis"}
           and R["torso"]["series"]["start"] == "2025-04-14" and "muscle_pct" not in str(R["torso"]["series"]))
    check_("the whole body's series starts with its first reading, and a day between is a gap",
           R["whole"]["series"]["start"] == "2016-03-07" and R["whole"]["series"]["values"][1] is None)
    tf = R["torso"]["series"]
    t_fat, t_ffm, t_mus = val[(174, 12)], val[(173, 12)], val[(175, 12)]
    t_mass = t_fat + t_ffm
    check_("the day's first weigh-in, not a later re-weigh",
           tf["values"][-1] == round(100 * t_fat / t_mass, 2), str(tf["values"][-1]))
    L = bs["latest"]
    check_("the map is the newest day's first full scan",
           L["day"] == "2026-10-20" and L["time"] == "07:15", str(L["time"]))
    rows = {x["key"]: x for x in L["rows"]}
    check_("its masses are in the display unit: the whole body 176.4 lb",
           rows["whole"]["mass"] == round(80.0 * lb, 1) == 176.4
           and rows["torso"]["mass"] == round(t_mass * lb, 1), str(rows["whole"]))
    check_("its percentages are each region's own, from the torso's own mass",
           (rows["torso"]["muscle_pct"], rows["torso"]["fat_pct"], rows["torso"]["bone_pct"])
           == (round(100 * t_mus / t_mass, 1), round(100 * t_fat / t_mass, 1),
               round(100 * (t_ffm - t_mus) / t_mass, 1)), str(rows["torso"]))
    check_("and its composition bar in lb: fat, muscle and derived bone make the mass",
           abs(rows["torso"]["fat"] + rows["torso"]["muscle"] + rows["torso"]["bone"]
               - rows["torso"]["mass"]) < 0.15, str(rows["torso"]))
    check_("the segments add up to the whole, and derived whole-body bone is Withings' own",
           abs(L["segments_mass"] - L["weight"]) <= 0.1
           and rows["whole"]["bone"] == L["bone_measured"] == round(3.2 * lb, 2), str(L))
    pos = {"torso": 12, "arm_left": 3, "arm_right": 2, "leg_left": 10, "leg_right": 11}
    fat_pct = {k: 100 * val[(174, q)] / (val[(173, q)] + val[(174, q)]) for k, q in pos.items()}
    fat_pct["whole"] = 100 * val[(8, 7)] / (val[(5, 7)] + val[(8, 7)])
    check_("the map shades each region off the fat ramp, with the ink the ramp gives it",
           all(rows[k]["colour"] == dashboard.fat_colour(v) and rows[k]["ink"] == dashboard.fat_ink(v)
               for k, v in fat_pct.items()), str(fat_pct))
    segs = [x for k, x in rows.items() if k != "whole"]
    hi, lo = max(segs, key=lambda x: x["fat_pct"]), min(segs, key=lambda x: x["fat_pct"])
    check_("the map's sentence names the widest spread between segments",
           L["spread"] == {"hi": hi["key"], "lo": lo["key"],
                           "points": round(hi["fat_pct"] - lo["fat_pct"], 1)}
           and L["spread"]["lo"] == "torso", str(L["spread"]))
    since = bs["since"]
    check_("Section 4 dates each change from the region's own first reading",
           since["whole"]["fat_pct"]["day"] == "2016-03-07"
           and since["whole"]["muscle_pct"]["day"] == "2020-05-11"
           and since["torso"]["fat_pct"]["day"] == "2025-04-14", str(since["torso"]))
    w0 = raw_values(scan_group(1, "2016-03-07", "07:00", first, segments=False, muscle=False))
    check_("whole-body fat change since the first reading: 20% now against 28% then",
           since["whole"]["fat_pct"]["change"]
           == round(100 * val[(8, 7)] / (val[(5, 7)] + val[(8, 7)])
                    - 100 * w0[(8, None)] / (w0[(5, None)] + w0[(8, None)]), 1) == -8.0,
           str(since["whole"]["fat_pct"]))
    check_("the other sections cover the segmental era only",
           bs["era"] == "2025-04-14" and bs["change"]["All"]["then"] == "2025-04-14"
           and bs["change"]["All"]["now"] == "2026-10-20", str(bs["change"]["All"]))
    check_("the shared axis holds every region; the spread is fattest minus leanest segment",
           bs["shared_axis"]["out"] == 0
           and bs["spread"]["last"] == round(hi["fat_pct"] - lo["fat_pct"], 1),
           str(bs["spread"]["last"]))

    print("\nDashboard - Body Scan: the fat ramp")
    fc = dashboard.fat_colour
    check_("the anchors are exact: 10% #F2CBA8 to 30% #A46634",
           [fc(v) for v, _ in dashboard.FAT_RAMP] == [c for _, c in dashboard.FAT_RAMP]
           and [v for v, _ in dashboard.FAT_RAMP] == [10.0, 15.0, 20.0, 25.0, 30.0])
    check_("a value between anchors is interpolated: 17.5% is halfway from 15 to 20",
           fc(17.5) == "#D6A072", fc(17.5))
    check_("nothing darker than #A46634, nothing paler than #F2CBA8",
           fc(36.4) == "#A46634" and fc(8.0) == "#F2CBA8")
    check_("dark text on the three lightest anchors, light on the two darkest",
           [dashboard.fat_ink(v) for v, _ in dashboard.FAT_RAMP]
           == ["#26170B", "#26170B", "#26170B", "#FFF4EA", "#FFF4EA"])

    print("\nDashboard - Body Scan: what changed, from two actual scans")
    # Round, invented segment masses (fat-free, fat, muscle), in kg.
    SEG = {"torso": (37.5, 8.7, 35.6), "arm_left": (3.4, 0.9, 3.2),
           "arm_right": (3.3, 0.85, 3.1), "leg_left": (10.1, 2.8, 9.6),
           "leg_right": (10.1, 2.8, 9.6)}

    def synth(day: date, algo, leg_fat: float = 2.8, torso_fat: float = 8.7) -> dict:
        seg = {k: {"ffm": v[0], "fat": v[1], "muscle": v[2]} for k, v in SEG.items()}
        seg["leg_left"]["fat"], seg["torso"]["fat"] = leg_fat, torso_fat
        return {"day": day, "t": 0, "utc": f"{day}T14:00:00Z", "algo": algo, "seg": seg,
                "whole": {"ffm": 64.4, "fat": 16.05, "muscle": 61.1}}

    d0 = date(2026, 1, 1)
    scans = ([synth(d0 + timedelta(days=i), "A", torso_fat=8.7 - 0.01 * i) for i in range(10)]
             + [synth(d0 + timedelta(days=10), None, torso_fat=8.6)]
             + [synth(d0 + timedelta(days=11 + i), "B", leg_fat=3.6, torso_fat=8.59 - 0.01 * i)
                for i in range(8)]
             + [synth(d0 + timedelta(days=19), "B", leg_fat=3.6, torso_fat=7.4)])
    pc = [(x["day"], x["algo"], {k: dashboard.region_comp(x, k)["fat_pct"] for k, _ in dashboard.REGIONS})
          for x in scans]
    steps = dashboard.algo_steps(pc)
    check_("an algorithm change is found, and a scan with no version recorded isn't one",
           [s_["day"] for s_ in steps] == ["2026-01-12"], str([s_["day"] for s_ in steps]))
    check_("its step is the median of 7 scans after against 7 before: left leg +4.6",
           steps[0]["steps"]["leg_left"] == round(100 * 3.6 / 13.7 - 100 * 2.8 / 12.9, 1) == 4.6
           and abs(steps[0]["steps"]["torso"]) < 0.5, str(steps[0]["steps"]))
    ch = dashboard.fat_change(scans, date(2026, 1, 20), None, steps)
    check_("then is the first scan, now the latest: no fitted trend",
           ch["then"] == "2026-01-01" and ch["now"] == "2026-01-20" and ch["scans"] == 20, str(ch))
    check_("ranked by points lost, the torso first",
           ch["rows"][0]["key"] == "torso"
           and [r["lost"] for r in ch["rows"]] == sorted((r["lost"] for r in ch["rows"]), reverse=True))
    check_("a leg that 'gained' across the algorithm change is reported, not hidden",
           next(r for r in ch["rows"] if r["key"] == "leg_left")["lost"] < 0
           and ch["algo"] == [{"day": "2026-01-12", "steps": {"leg_left": steps[0]["steps"]["leg_left"]}}],
           str(ch["algo"]))
    check_("a low last reading is named against the median of the 7 scans before it",
           [(e["key"], e["end"]) for e in ch["ends"]] == [("torso", "now")]
           and ch["ends"][0]["n"] == 7, str(ch["ends"]))
    check_("a window with fewer than two scans has nothing to compare",
           dashboard.fat_change(scans, date(2026, 6, 30), 90, steps) is None)
    check_("the page's window buttons are the windows computed",
           list(dashboard.CHANGE_WINDOWS) == ["90d", "1y", "All"]
           and 'const CHANGE_WINS = ["90d", "1y", "All"];' in dashboard.TEMPLATE.read_text())
    a = dashboard.bucket_mean({date(2016, 3, 7): 1.0}, 28)
    b = dashboard.bucket_mean({date(2020, 5, 11): 1.0}, 28)
    check_("28-day means sit on one grid, so lanes that start years apart line up",
           (date.fromisoformat(b["start"]) - date.fromisoformat(a["start"])).days % 28 == 0
           and date.fromisoformat(a["start"]).weekday() == 0, f"{a['start']} {b['start']}")
    page = dashboard.TEMPLATE.read_text()
    check_("the page's bucket grid is the same Monday",
           "const BUCKET_ANCHOR = 4;" in page and dashboard.BUCKET_ANCHOR == date(1970, 1, 5))
    c.close()


def brief_snapshot(path: Path, *, garmin_through: date = BRIEF_DAY,
                   withings_through: date = BRIEF_DAY, sleep_recorded: bool = True,
                   training: str | None = "tonal", calm: bool = False,
                   heavy: bool = False, catchup: bool = False) -> Path:
    """A snapshot with up to 40 days of history before BRIEF_DAY. Each metric
    cycles around a fixed centre, so its median is the centre, and the newest
    reading sits on the centre. Unless `calm`, the newest night's sleep is
    short and its RHR high, both past 2 SD. `heavy` puts the newest weigh-in
    10 kg over. `catchup` leaves the newest day holding only sleep, as the
    mid-morning sleep catch-up does, and then the high RHR is the day before's."""
    path.unlink(missing_ok=True)
    c = db.connect(path)
    db.apply_schema(c)
    today = BRIEF_DAY
    for k in range(40):
        d = today - timedelta(days=k)
        if d > garmin_through:
            continue
        newest = d == garmin_through
        wobble = 0 if newest else (k % 3) - 1     # -1, 0, 1 around the centre
        sleep_h, rhr = 7.0 + 0.5 * wobble, 55 + wobble
        if newest and not calm:
            sleep_h, rhr = 5.9, 59
        if catchup and d == garmin_through - timedelta(days=1) and not calm:
            rhr = 59
        day_only = (None, None, None) if catchup and newest else (rhr, 20 + 2 * wobble,
                                                                  80 + 3 * wobble)
        c.execute("INSERT INTO garmin_daily (day, sleep_total_s, sleep_score, rhr, bb_min, bb_max, "
                  "loaded_utc) VALUES (?, ?, ?, ?, ?, ?, 'test')",
                  (d.isoformat(), round(sleep_h * 3600) if (sleep_recorded or not newest) else 0,
                   80 + 4 * wobble, *day_only))
    for k in range(40):
        d = today - timedelta(days=k)
        if d > withings_through:
            continue
        newest = d == withings_through
        taken = datetime(d.year, d.month, d.day, 12, 0)       # 07:00 in Chicago
        w = (0, 0) if newest else ((k % 5) - 2, (k % 3) - 1)
        weight = 100.0 + 0.2 * w[0] + (10 if newest and heavy else 0)
        for meastype, value in ((1, weight), (6, 20.0 + 0.1 * w[1])):
            c.execute("INSERT INTO withings_measure (measure_uid, grpid, taken_utc, taken_epoch, "
                      "local_date, meastype, position, value, category, loaded_utc) "
                      "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, 1, 'test')",
                      (f"g{k}:{meastype}:-", f"g{k}", taken.strftime("%Y-%m-%dT%H:%M:%SZ"),
                       int(taken.replace(tzinfo=ZoneInfo("UTC")).timestamp()), d.isoformat(),
                       meastype, value))
    yday = today - timedelta(days=1)
    # Tonal is fresh in every case: a session three days back.
    workouts = [("w-old", today - timedelta(days=3), "Legs", 9000.0, 20)]
    if training == "tonal":
        workouts.append(("w-yday", yday, "Upper Body Power", 11000.0, 24))
    for wid, d, title, vol, sets in workouts:
        c.execute("INSERT INTO tonal_workout (workout_id, begin_utc, local_date, tz_source, title, "
                  "duration_s, total_volume, set_count, raw_json, first_seen_utc, last_seen_utc) "
                  "VALUES (?, ?, ?, 'z', ?, 3000, ?, ?, '{}', 'test', 'test')",
                  (wid, f"{d.isoformat()}T23:00:00Z", d.isoformat(), title, vol, sets))
    if training == "garmin":
        c.execute("INSERT INTO garmin_activity (activity_id, start_local, local_date, sport, "
                  "sub_sport, name, elapsed_s, loaded_utc) VALUES ('a1', ?, ?, 'fitness_equipment', "
                  "'strength_training', 'Strength', 2700, 'test')",
                  (f"{yday.isoformat()} 18:00:00", yday.isoformat()))
    for s in ("garmin", "tonal", "withings"):
        c.execute("INSERT INTO sync_runs (source, started_utc, finished_utc, status) "
                  "VALUES (?, '2026-09-30T13:05:00Z', '2026-09-30T13:05:05Z', 'ok')", (s,))
    c.commit()
    c.close()
    return path


def test_brief(root: Path, cfg, cfg_path: Path) -> None:
    from wellness import brief

    def check_(label: str, cond: bool, detail: str = "") -> None:
        # A digest is many lines: print it only when a check fails.
        check(label, cond, "" if cond else "\n" + detail)

    def run(**kw) -> tuple[str, int]:
        return brief.run(cfg, today=BRIEF_DAY, snapshot=brief_snapshot(root / "brief-ro.db", **kw))

    print("\nBrief - a normal day")
    text, rc = run()
    lines = text.splitlines()
    check_("exits 0, and the header gives the date and says all is fresh",
          rc == 0 and lines[0] == "Wed 30 Sep 2026: all sources fresh (synced 08:05)", lines[0])
    check_("the night carries its own date even when it's last night, against your median",
          "Night ending Wed 30 Sep\nSleep 5.9h, 1.1h below your median" in text
          and "Resting HR 59 bpm, 4 bpm above your median" in text, text)
    check_("yesterday's Tonal session with its volume and sets",
          "Yesterday (Tue 29 Sep)\nTonal: Upper Body Power, 11,000 lb, 24 sets" in text, text)
    check_("the weigh-in carries its own date and time, in lb, against the 7- and 30-day averages",
          "Weigh-in Wed 30 Sep, 07:00\nWeight 220.5 lb: " in text and "7-day avg" in text
          and "30-day avg" in text, text)
    mv = text.split("What moved\n")[-1].splitlines()
    check_("what moved names the readings past 2 SD, dated, largest first, and only those",
          "What moved" in text and len(mv) == 2
          and mv[0].startswith("Resting HR 59 bpm on Wed 30 Sep, ")
          and mv[1].startswith("Sleep 5.9h on Wed 30 Sep, ")
          and "SD below your 30-day mean of 7.0" in mv[1], str(mv))
    check_("plain text, short enough for a phone",
          len(text) < 1500 and not any(m in text for m in ("|", "**", "```", "#")), str(len(text)))

    print("\nBrief - a day with no training")
    text, rc = run(training=None)
    check_("says so explicitly", rc == 0 and "Yesterday (Tue 29 Sep)\nNo session." in text, text)
    text, _ = run(training="garmin")
    check_("a watch-only strength session shows its duration",
          "Garmin strength: 45 min (watch only, no volume)" in text, text)

    print("\nBrief - last night not synced yet: the newest night, under its own date")
    text, rc = run(garmin_through=BRIEF_DAY - timedelta(days=1))
    check_("the night before is reported as the night ending Tue 29 Sep, never as last night",
          rc == 0 and "Night ending Tue 29 Sep\nSleep 5.9h, 1.1h below your median" in text
          and "Wed 30 Sep\nSleep" not in text, text)
    check_("and what moved dates it too",
          "Sleep 5.9h on Tue 29 Sep, " in text.split("What moved\n")[-1], text)
    text, _ = run(sleep_recorded=False, calm=True)
    check_("a night with 0 s sleep: sleep is missing, the rest still reported",
          "Night ending Wed 30 Sep\nSleep: missing" in text
          and "Resting HR 55 bpm, at your median" in text, text)
    text, _ = run(garmin_through=BRIEF_DAY - timedelta(days=3), calm=True)
    check_("3 days old is still just dated", "Night ending Sun 27 Sep\n" in text
          and "days ago" not in text, text)

    print("\nBrief - the sleep catch-up loaded this morning's night and nothing else")
    text, rc = run(catchup=True)
    check_("the night is this morning's, and resting HR and body battery are the day before's, "
           "each under that day",
           rc == 0 and "Night ending Wed 30 Sep\nSleep 5.9h, 1.1h below your median\n"
           "Sleep score 80, at your median\n"
           "Resting HR 59 bpm on Tue 29 Sep, 4 bpm above your median\n"
           "Body battery low 20 on Tue 29 Sep, at your median\n"
           "Body battery high 80 on Tue 29 Sep, at your median" in text
           and "missing" not in text, text)
    mv = text.split("What moved\n")[-1].splitlines()
    check_("what moved dates each reading by its own day, not the night's",
          len(mv) == 2 and mv[0].startswith("Resting HR 59 bpm on Tue 29 Sep, ")
          and mv[1].startswith("Sleep 5.9h on Wed 30 Sep, "), str(mv))

    print("\nBrief - a stale source: past 3 days, the age is the story")
    text, rc = run(garmin_through=BRIEF_DAY - timedelta(days=4))
    check_("the header names the stale source and how old its data is, still exit 0",
          rc == 0 and text.splitlines()[0]
          == "Wed 30 Sep 2026: garmin STALE, 4 days old (limit 2) (synced 08:05)",
          text.splitlines()[0])
    check_("the night says how old it is, and its old outliers don't count as moved",
          "Newest night ended Sat 26 Sep, 4 days ago\nSleep 5.9h" in text
          and "What moved" not in text, text)
    text, rc = run(withings_through=BRIEF_DAY - timedelta(days=9), heavy=True, calm=True)
    check_("a 9-day-old weigh-in says so, and stays out of what moved",
          rc == 0 and "Newest weigh-in Mon 21 Sep, 07:00, 9 days ago\nWeight 242.5 lb" in text
          and "withings STALE, 9 days old (limit 2)" in text.splitlines()[0]
          and "What moved" not in text, text)

    print("\nBrief - nothing moved")
    text, rc = run(calm=True)
    check_("an ordinary day has no 'what moved' section at all",
          rc == 0 and "What moved" not in text and "Sleep 7.0h, at your median" in text, text)

    print("\nBrief - the snapshot can't be read")
    text, rc = brief.run(cfg, today=BRIEF_DAY, snapshot=root / "no-such-ro.db")
    check_("a missing snapshot is one line and a non-zero exit",
          rc != 0 and "\n" not in text and text.startswith("Wellness brief unavailable: no snapshot"),
          text)
    junk = root / "junk-ro.db"
    junk.write_bytes(b"this is not a database" * 100)
    text, rc = brief.run(cfg, today=BRIEF_DAY, snapshot=junk)
    check_("an unreadable snapshot is one line and a non-zero exit",
          rc != 0 and "\n" not in text and text.startswith("Wellness brief unavailable: can't read"),
          text)

    out, err = io.StringIO(), io.StringIO()
    brief_snapshot(cfg.wellness_db.with_name("wellness-ro.db"))
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = sync_all.main(["--config", str(cfg_path), "brief"])
    check_("`sync_all.py brief` prints the digest and nothing else",
          rc == 0 and out.getvalue() == brief.run(cfg)[0] + "\n" and err.getvalue() == "",
          repr(out.getvalue()[:80]))
    sh = Path(__file__).resolve().parent.parent / "scripts" / "brief.sh"
    check_("brief.sh is mode 700 and writes no log file",
          sh.exists() and (sh.stat().st_mode & 0o777) == 0o700 and ">>" not in sh.read_text())


GARMIN_GAP = (date(2026, 8, 20), date(2026, 8, 24))


def ask_snapshot(path: Path) -> Path:
    """120 days to BRIEF_DAY (a Wednesday). Tonal at 18:00 every Mon/Wed/Fri
    (10,000 lb, 20 sets); watch-only strength every Saturday (45 min). RHR
    runs 4 bpm higher the morning after a training day. No Garmin data in
    GARMIN_GAP."""
    path.unlink(missing_ok=True)
    c = db.connect(path)
    db.apply_schema(c)
    train = lambda d: d.weekday() in (0, 2, 4, 5)
    for k in range(120):
        d = BRIEF_DAY - timedelta(days=k)
        wobble = (k % 3) - 1
        if not GARMIN_GAP[0] <= d <= GARMIN_GAP[1]:
            c.execute("INSERT INTO garmin_daily (day, sleep_total_s, sleep_score, rhr, bb_min, "
                      "bb_max, loaded_utc) VALUES (?, ?, ?, ?, ?, ?, 'test')",
                      (d.isoformat(), round((7.0 + 0.5 * wobble) * 3600), 80 + 4 * wobble,
                       54 + wobble + (4 if train(d - timedelta(days=1)) else 0),
                       20 + 2 * wobble, 80 + 3 * wobble))
        taken = datetime(d.year, d.month, d.day, 12, 0)
        c.execute("INSERT INTO withings_measure (measure_uid, grpid, taken_utc, taken_epoch, "
                  "local_date, meastype, position, value, category, loaded_utc) "
                  "VALUES (?, ?, ?, ?, ?, 1, NULL, ?, 1, 'test')",
                  (f"a{k}:1:-", f"a{k}", taken.strftime("%Y-%m-%dT%H:%M:%SZ"),
                   int(taken.replace(tzinfo=ZoneInfo("UTC")).timestamp()), d.isoformat(),
                   100.0 + 0.2 * wobble))
        if d.weekday() in (0, 2, 4):
            c.execute("INSERT INTO tonal_workout (workout_id, begin_utc, local_date, tz_source, "
                      "title, duration_s, total_volume, set_count, raw_json, first_seen_utc, "
                      "last_seen_utc) VALUES (?, ?, ?, 'z', 'Full Body', 3000, 10000, 20, '{}', "
                      "'test', 'test')", (f"t{k}", f"{d.isoformat()}T23:00:00Z", d.isoformat()))
        if d.weekday() == 5:
            c.execute("INSERT INTO garmin_activity (activity_id, start_local, local_date, sport, "
                      "sub_sport, name, elapsed_s, loaded_utc) VALUES (?, ?, ?, 'fitness_equipment', "
                      "'strength_training', 'Strength', 2700, 'test')",
                      (f"g{k}", f"{d.isoformat()} 10:00:00", d.isoformat()))
    for s in ("garmin", "tonal", "withings"):
        c.execute("INSERT INTO sync_runs (source, started_utc, finished_utc, status) "
                  "VALUES (?, '2026-09-30T13:05:00Z', '2026-09-30T13:05:05Z', 'ok')", (s,))
    c.commit()
    c.close()
    return path


def test_ask(root: Path, cfg, cfg_path: Path) -> None:
    import subprocess
    from wellness import ask

    snap = ask_snapshot(root / "ask-ro.db")

    def check_(label: str, cond: bool, detail: str = "") -> None:
        # Answers are long: print one only when its check fails.
        check(label, cond, "" if cond else "\n" + detail)

    def q(*argv: str) -> tuple[dict, int]:
        answer, rc = ask.run(list(argv), cfg=cfg, today=BRIEF_DAY, snapshot=snap)
        return json.loads(ask.render(answer, rc)[0]), rc

    print("\nAsk - every query, normal input")
    a, rc = q("list")
    check_("list is the whole catalog, and needs no database",
          rc == 0 and set(a["queries"]) == set(ask.QUERIES) and set(a["metrics"]) == set(ask.METRICS)
          and ask.run(["list"], cfg=cfg, snapshot=root / "absent.db")[1] == 0)
    a, rc = q("latest")
    check_("latest: every metric's newest reading, dated; one with none stored says so",
          rc == 0 and len(a["readings"]) == 7
          and all(r["date"] == "2026-09-30" for r in a["readings"] if r["metric"] != "body_fat")
          and a["readings"][-1] == {"metric": "body_fat", "value": None,
                                    "note": "no readings stored"}, str(a))
    a, rc = q("day", "date=2026-09-28")
    check_("day: readings and the Tonal session on a Monday",
          rc == 0 and a["from"] == a["to"] == "2026-09-28" and a["missing"] == ["body_fat"]
          and a["training"] == {"kind": "tonal", "tonal_volume": 10000, "tonal_sets": 20,
                                "unit": "lb"}, str(a))
    a, _ = q("day", "date=2026-08-22")
    check_("day: a date in a Garmin gap lists its metrics as missing, weight still there",
          set(a["missing"]) == {m for m, x in ask.METRICS.items() if x.source == "garmin"}
          | {"body_fat"}
          and "weight" in a["readings"] and a["training"]["kind"] == "garmin", str(a))
    a, rc = q("metric-window", "metric=sleep_hours", "days=30")
    check_("metric-window: stats over the 30 days ending today, with the count present",
          rc == 0 and (a["from"], a["to"]) == ("2026-09-01", "2026-09-30")
          and a["days_present"] == 30 and a["median"] == 7.0 and a["min"]["value"] == 6.5
          and a["min"]["date"] and a["max"]["date"] and "daily" not in a, str(a))
    a, _ = q("metric-window", "metric=weight", "days=7")
    check_("metric-window: a week also lists each day, in lb",
          a["unit"] == "lb" and len(a["daily"]) == 7 and a["daily"][-1][0] == "2026-09-30", str(a))
    a, rc = q("compare-windows", "metric=resting_hr", "recent=7", "prior=30")
    check_("compare-windows: two dated windows and the difference between their means",
          rc == 0 and (a["recent"]["from"], a["recent"]["to"]) == ("2026-09-24", "2026-09-30")
          and (a["prior"]["from"], a["prior"]["to"]) == ("2026-08-25", "2026-09-23")
          and a["difference"]["mean"] == round(a["recent"]["mean"] - a["prior"]["mean"], 1)
          and "2026-09-24 to 2026-09-30" in a["summary"], str(a))
    a, rc = q("training-frequency", "weeks=4")
    check_("training-frequency: per week, Tonal and watch-only strength labelled apart",
          rc == 0 and a["columns"][1:3] == ["tonal", "garmin_strength_only"]
          and [r[1:3] for r in a["rows"][:3]] == [[3, 1]] * 3
          and a["partial_week"] == {"week_of": "2026-09-28", "through": "2026-09-30"}
          and a["mean_sessions_per_full_week"] == 4.0, str(a))
    a, rc = q("training-volume", "days=28")
    check_("training-volume: Tonal only, says so, and counts the watch sessions it leaves out",
          rc == 0 and a["tonal_sessions"] == 12 and a["tonal_volume_total"] == 120000
          and a["garmin_strength_only_sessions"] == 4 and "Tonal only" in a["source"]
          and a["weekly"][0][0] == a["from"], str(a))
    a, rc = q("recovery-after-training", "metric=resting_hr", "days=90")
    check_("recovery: the morning after training against the morning after rest",
          rc == 0 and a["difference"]["after_training_is"] == "higher"
          and 3.5 <= a["difference"]["mean"] <= 4.5
          and a["after_rest_day"]["missing"] + a["after_training_day"]["missing"] == 5, str(a))
    a, rc = q("extremes", "metric=sleep_hours", "days=30", "n=2")
    check_("extremes: highest and lowest, dated, most recent first among ties",
          rc == 0 and a["highest"] == [["2026-09-28", 7.5], ["2026-09-25", 7.5]]
          and a["lowest"] == [["2026-09-30", 6.5], ["2026-09-27", 6.5]], str(a))
    a, rc = q("coverage", "days=90")
    g = a["sources"]["garmin"]
    check_("coverage: newest and oldest per source, and the Garmin gap with its dates",
          rc == 0 and g["newest"] == "2026-09-30" and g["oldest"] == "2026-06-03"
          and g["gaps"] == [{"from": "2026-08-20", "to": "2026-08-24", "days": 5}]
          and a["sources"]["withings"]["gaps"] == [], str(a))

    every = [q(*argv)[0] for argv in (
        ("latest",), ("day", "date=2026-09-28"), ("metric-window", "metric=body_fat"),
        ("compare-windows", "metric=weight"), ("training-frequency",), ("training-volume",),
        ("recovery-after-training", "metric=sleep_score"), ("extremes", "metric=weight"),
        ("coverage",))]
    check_("every answer says which dates it covers", all(x.get("from") and x.get("to")
                                                          for x in every))
    check_("and every one is small enough for the model's context",
          all(len(ask.render(x, 0)[0]) < ask.MAX_OUTPUT for x in every))
    check_("an answer too big for the context is refused, not truncated",
          ask.render({"x": "a" * ask.MAX_OUTPUT}, 0)[1] != 0)

    print("\nAsk - too little data, and a window with none")
    a, rc = q("metric-window", "metric=sleep_hours", "days=3")
    check_("three days: says it's too few for statistics, still lists the days",
          rc == 0 and "insufficient" in a and "mean" not in a and len(a["daily"]) == 3, str(a))
    a, rc = q("metric-window", "metric=sleep_hours", "days=5", "end=2026-08-24")
    check_("a window inside the Garmin gap: 0 days present, no number",
          rc == 0 and a["days_present"] == 0 and "insufficient" in a and "mean" not in a, str(a))
    empty = {name: q(*argv, "end=2001-01-01") for name, argv in (
        ("metric-window", ("metric-window", "metric=weight")),
        ("compare-windows", ("compare-windows", "metric=weight")),
        ("training-frequency", ("training-frequency",)),
        ("training-volume", ("training-volume",)),
        ("recovery-after-training", ("recovery-after-training", "metric=resting_hr")),
        ("extremes", ("extremes", "metric=weight")),
        ("coverage", ("coverage",)))}
    check_("every windowed query handles a window before any data cleanly",
          all(rc == 0 for _, rc in empty.values())
          and "insufficient" in empty["metric-window"][0]
          and empty["compare-windows"][0]["difference"] is None
          and empty["training-frequency"][0]["totals"] == {"tonal": 0, "garmin_strength_only": 0}
          and empty["training-volume"][0]["tonal_volume_total"] is None
          and empty["recovery-after-training"][0]["difference"] is None
          and "insufficient" in empty["extremes"][0]
          and empty["coverage"][0]["sources"]["garmin"]["gaps"] == [],
          str({k: v[0] for k, v in empty.items()})[:600])

    print("\nAsk - bad input is rejected, and nothing runs")
    rejected = [
        [], ["bogus"], ["LIST"], ["list "], ["metric_window"], ["list;ls"],
        ["metric-window"],                                          # metric is required
        ["metric-window", "metric=bogus"], ["metric-window", "metric=weight", "colour=red"],
        ["metric-window", "metric=weight", "metric=weight"], ["metric-window", "metric"],
        ["latest", "days=7"], ["list"] + ["x=1"] * 9,
        ["recovery-after-training", "metric=weight"],               # not a recovery metric
    ]
    for key, bad in (("days", ["0", "3651", "abc", "1.5", "-1", "+5", "1e3", "0x10", " 5", "５",
                               "007", ""]),
                     ("n", ["0", "11"]), ("weeks", ["0", "53"]), ("recent", ["366"]),
                     ("min_gap", ["0"]),
                     ("end", ["2026-13-01", "20260901", "2026-10-01", "1999-12-31", "today"])):
        query = {"days": "extremes", "n": "extremes", "weeks": "training-frequency",
                 "recent": "compare-windows", "min_gap": "coverage", "end": "extremes"}[key]
        extra = ["metric=weight"] if query in ("extremes", "compare-windows") else []
        rejected += [[query, *extra, f"{key}={v}"] for v in bad]
    rejected.append(["recovery-after-training", "metric=resting_hr", "days=13"])
    results = [q(*argv) for argv in rejected]
    check_(f"{len(rejected)} bad inputs: unknown names, out-of-range, non-numeric, bad dates",
          all(rc == ask.REJECTED and set(a) == {"error"} for a, rc in results),
          str([argv for argv, (a, rc) in zip(rejected, results) if rc != ask.REJECTED]))

    payloads = ["1;DROP TABLE garmin_daily", "1' OR '1'='1", "1 UNION SELECT 1", "1)--",
                "weight' --", "$(id)", "`id`", "../../etc/passwd", "1%27", "1\nlist", "1\x00",
                "sleep_hours;", "x' OR 1=1--", "' or ''='"]
    probes = []
    for name, query in ask.QUERIES.items():
        base = {k: ("weight" if isinstance(p.kind, ask.Choice) and "weight" in p.kind.options
                    else p.kind.options[0] if isinstance(p.kind, ask.Choice)
                    else "2026-09-01" if isinstance(p.kind, ask.Day) else "30")
                for k, p in query.params.items() if p.default is ask.REQUIRED}
        for key in query.params:
            for bad in payloads:
                args = {**base, key: bad}
                probes.append([name, *(f"{k}={v}" for k, v in args.items())])
        probes.append([name, *(f"{k}={v}" for k, v in base.items()), "days;--=1"])
    probes += [[p] for p in payloads] + [["metric-window", "metric' OR=1"]]
    before = {t: sqlite3.connect(snap).execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("garmin_daily", "withings_measure", "tonal_workout")}
    leaked = []
    for argv in probes:
        a, rc = q(*argv)
        text = json.dumps(a)
        if rc != ask.REJECTED or any(p in text for p in payloads if len(p) > 3):
            leaked.append(argv)
    after = {t: sqlite3.connect(snap).execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
             for t in before}
    check_(f"{len(probes)} injection attempts, in every parameter of every query and the name: "
          "all rejected, none echoed back", not leaked and before == after, str(leaked[:5]))

    with contextlib.redirect_stdout(io.StringIO()) as out:
        rc1 = sync_all.main(["ask", "--config=/nonexistent", "list"])
        rc2 = sync_all.main(["ask", "list", "--config=/nonexistent"])
    check_("`sync_all.py ask` never reads an argument as an option like --config",
          rc1 == rc2 == ask.REJECTED
          and all(json.loads(line) == {"error": json.loads(line)["error"]}
                  for line in out.getvalue().splitlines()), out.getvalue())

    print("\nAsk - scripts/ask.sh, the boundary the scheduler calls")
    sh = Path(__file__).resolve().parent.parent / "scripts" / "ask.sh"
    names = re.search(r"QUERIES=\(([^)]*)\)", sh.read_text()).group(1).split()
    check_("ask.sh is mode 700, and allows exactly the catalog's query names",
          (sh.stat().st_mode & 0o777) == 0o700 and sorted(names) == sorted(ask.QUERIES),
          str(names))

    def shell(*argv: str) -> tuple[str, int]:
        p = subprocess.run([str(sh), *argv], capture_output=True, text=True, timeout=60,
                           env={"PATH": "/usr/bin:/bin"})
        return p.stdout, p.returncode

    cases = [("bogus",), ("list;id",), ("$(id)",), ("metric-window", "metric=weight;id"),
             ("metric-window", "metric=weight", "days=1' OR '1'='1"),
             ("metric-window", "--config=/etc/passwd"), ("list",) + ("x=1",) * 9,
             ("metric-window", "METRIC=weight"), ("metric-window", "metric=weight\nid"), ()]
    bad = []
    for argv in cases:
        out, rc = shell(*argv)
        if rc != 64 or len(out.splitlines()) != 1 or json.loads(out).get("by") != "ask.sh" \
                or any(x in out for x in argv if len(x) > 3 and x not in ("bogus",)):
            bad.append((argv, rc, out))
    check_("ask.sh rejects hostile names and arguments itself: one JSON line, exit 64, no echo",
          not bad, str(bad[:3]))
    out, rc = shell("metric-window", "metric=weight", "days=99999")
    check_("a well-formed but out-of-range value passes the shell and is refused by the catalog",
          rc == 64 and json.loads(out) == {"error": "days must be integer 1-3650"}, out)
    out, rc = shell("metric-window", "metric=bogus")
    check_("and so is a well-formed but unknown metric",
          rc == 64 and json.loads(out) == {"error": "metric must be one of "
                                           + ", ".join(ask.METRICS)}, out)


def test_sleep_catchup(root: Path) -> None:
    """The mid-morning job. GarminDB is a fixture shaped like the real one, and
    GarminDB's login, request and importer are a fake, so nothing here can
    reach Garmin."""
    import base64
    import hashlib

    from wellness import brief
    from wellness import sleep_catchup as sc

    base = root / "catchup"
    base.mkdir()
    cfg = config.load(write_config(base))
    gdb = base / "DBs" / "garmin.db"
    gdb.parent.mkdir(parents=True)
    today, tomorrow = date(2026, 9, 10), date(2026, 9, 11)       # Thu, Fri
    now = datetime(2026, 9, 10, 10, 30, tzinfo=TZ)

    def store_night(day: str, start: str, end: str, asleep_s: int) -> None:
        # As GarminDB writes it: the day at midnight, naive local times, and
        # durations as TIME text.
        c = sqlite3.connect(gdb)
        c.execute("INSERT OR REPLACE INTO sleep (day, start, end, total_sleep, score) "
                  "VALUES (?,?,?,?,?)",
                  (f"{day} 00:00:00.000000", f"{start}.000000", f"{end}.000000",
                   f"{asleep_s // 3600:02d}:{asleep_s % 3600 // 60:02d}:00.000000", 80))
        c.commit()
        c.close()

    c = sqlite3.connect(gdb)
    c.executescript("""
        CREATE TABLE sleep (day DATE PRIMARY KEY, start DATETIME, end DATETIME,
            total_sleep TIME, deep_sleep TIME, light_sleep TIME, rem_sleep TIME,
            awake TIME, avg_stress FLOAT, score INTEGER, qualifier VARCHAR);
        CREATE TABLE daily_summary (day DATE PRIMARY KEY, rhr INTEGER, steps INTEGER,
            bb_min INTEGER, bb_max INTEGER);
    """)
    for i in range(1, 10):          # whole days through yesterday, as the nightly leaves them
        c.execute("INSERT INTO daily_summary VALUES (?,?,?,?,?)",
                  (f"2026-09-{i:02d} 00:00:00.000000", 58, 9000, 20, 85))
    c.commit()
    c.close()
    for i in range(1, 10):
        store_night(f"2026-09-{i:02d}", f"2026-09-{i:02d} 00:30:00",
                    f"2026-09-{i:02d} 08:45:00", 29700)

    quiet = contextlib.ExitStack()
    with quiet:                     # the nightly: load, snapshot
        quiet.enter_context(contextlib.redirect_stdout(io.StringIO()))
        live = sync_all._open_live(cfg)
        sync_all._run(live, cfg, "garmin", today=today)
        sync_all.cmd_snapshot(live, cfg)
        live.close()

    def reply(day: date, start: str | None = None, end: str | None = None,
              asleep: int | None = None) -> dict:
        def gmt_ms(hhmm):
            return int(datetime.fromisoformat(f"{day}T{hhmm}").replace(
                tzinfo=TZ).timestamp() * 1000) if hhmm else None
        return {"dailySleepDTO": {
            "calendarDate": day.isoformat(), "sleepTimeSeconds": asleep,
            "sleepStartTimestampGMT": gmt_ms(start), "sleepEndTimestampGMT": gmt_ms(end),
            "sleepWindowConfirmationType": "enhanced_confirmed_final" if asleep else None},
            "sleepLevels": [] if asleep else None}

    empty, night = reply(today), reply(today, "00:20", "07:05", 22800)

    class Fake:
        """GarminDB's side: a token file, the request, and an importer that
        writes the night into the fixture as GarminDB's would."""
        made = 0

        def __init__(self, answer=None, *, expires=now + timedelta(hours=20),
                     imports=True, fail=None):
            Fake.made += 1
            self.token_file = base / f"garmin_tokens_{Fake.made}.json"
            claims = base64.urlsafe_b64encode(
                json.dumps({"exp": expires.timestamp()}).encode()).decode().rstrip("=") \
                if expires else None
            self.token_file.write_text(json.dumps(
                {"di_token": f"eyJhbGciOiJSUzI1NiJ9.{claims}.SIGNATURE-NOT-TO-LOG",
                 "di_refresh_token": "REFRESH-NOT-TO-LOG"}) if expires else "not a token")
            self.answer, self.imports, self.fail, self.fetched = answer, imports, fail, 0

        def fetch(self, day):
            self.fetched += 1
            if self.fail:
                raise self.fail
            return self.answer

        def save_and_import(self, day, answer):
            if self.imports:
                dto = answer["dailySleepDTO"]

                def local(ms):
                    return datetime.fromtimestamp(ms / 1000, TZ).strftime("%Y-%m-%d %H:%M:%S")
                store_night(day.isoformat(), local(dto["sleepStartTimestampGMT"]),
                            local(dto["sleepEndTimestampGMT"]), dto["sleepTimeSeconds"])

    def never():
        return Fake(fail=AssertionError("a run with nothing to fetch fetched"))

    def run(source, when=now) -> tuple[int, dict, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = sync_all.cmd_sleep_catchup(cfg, now=when, source=source)
        return rc, json.loads(sc.log_path(cfg).read_text().splitlines()[-1]), out.getvalue()

    def state() -> list[str]:
        """wellness.db, the snapshot and GarminDB's file, byte for byte."""
        return [hashlib.sha256(p.read_bytes()).hexdigest()
                for p in (cfg.wellness_db, dashboard.snapshot_path(cfg), gdb)]

    def raises(fn) -> bool:
        try:
            fn()
        except ValueError:
            return True
        return False

    print("\nSleep catch-up - reading Connect's reply and GarminDB's token")
    check("Connect's empty record before it has the night reads as not yet, not a night",
          sc.asleep_in(empty, today) is None)
    check("a reply holding the night gives its seconds asleep",
          sc.asleep_in(night, today) == 22800)
    check("0 s asleep is a missing night, as everywhere else",
          sc.asleep_in(reply(today, "00:20", "07:05", 0), today) is None)
    check("a reply for another day, or that isn't the sleep record, is an error",
          raises(lambda: sc.asleep_in(reply(date(2026, 9, 9), "00:20", "07:05", 22800), today))
          and raises(lambda: sc.asleep_in([], today)))
    probe = Fake()
    check("the token's expiry comes from its own claim, and unreadable is None",
          sc.token_expiry(probe.token_file) == now + timedelta(hours=20)
          and sc.token_expiry(Fake(expires=None).token_file) is None)

    print("\nSleep catch-up - a run that loads nothing leaves everything as the nightly did")
    before = state()
    source = Fake(empty)
    rc, line, out = run(source)
    check("no night in Connect yet: exit 0, logged as not found",
          rc == 0 and line["result"] == "not_found" and line["found"] is False
          and source.fetched == 1, str(line))
    check("and it says plainly that this is normal",
          "normal on a late morning" in out, out.splitlines()[0])
    check("wellness.db, the snapshot and GarminDB are byte for byte unchanged",
          state() == before)
    source = Fake(night, expires=now - timedelta(minutes=3))
    rc, line, out = run(source)
    check("an expired GarminDB token: exit 0, no request, logged with its expiry",
          rc == 0 and line["result"] == "token_expired" and source.fetched == 0
          and line["token_expires_local"] == "2026-09-10T10:27-05:00", str(line))
    rc, line, _ = run(Fake(night, expires=now + timedelta(seconds=30)))
    check("a token that would lapse partway through the run counts as expired",
          rc == 0 and line["result"] == "token_expired", str(line))
    check("still nothing changed", state() == before)
    source = Fake(night, expires=None)
    rc, line, _ = run(source)
    check("a token whose expiry can't be read is a failure: exit 2, no request",
          rc == 2 and line["result"] == "error" and source.fetched == 0, str(line))
    rc, line, _ = run(Fake(fail=ConnectionError("Connect is down")))
    check("a failed download: exit 2, and the log says why",
          rc == 2 and line["result"] == "error" and "Connect is down" in line["error"], str(line))
    rc, line, _ = run(Fake(reply(date(2026, 9, 9), "00:20", "07:05", 22800)))
    check("a reply for the wrong day: exit 2", rc == 2 and line["result"] == "error", str(line))
    rc, line, _ = run(Fake(night, imports=False))
    check("a night GarminDB's import didn't store: exit 2, though it was found",
          rc == 2 and line["result"] == "error" and line["found"] is True
          and "import" in line["error"], str(line))
    check("and after every one of those, nothing changed", state() == before)
    log = sc.log_path(cfg).read_text()
    check("one line per run, and no token in any of them",
          len(log.splitlines()) == 7 and "NOT-TO-LOG" not in log, f"{len(log.splitlines())} lines")

    print("\nSleep catch-up - the night found, loaded and snapshotted")
    rc, line, out = run(Fake(night))
    check("exit 0, logged as loaded with the wake time and the minutes since",
          rc == 0 and line["result"] == "loaded" and line["found"] is True
          and line["wake_local"] == "2026-09-10T07:05" and line["gap_min"] == 205
          and line["confirmation"] == "enhanced_confirmed_final", str(line))
    check("the snapshot the brief reads holds the night",
          sc.snapshot_night(cfg, today) == 22800)
    text, _ = brief.run(cfg, today=today)
    check("so the brief's night is the one that ended this morning",
          "Night ending Thu 10 Sep" in text, text.splitlines()[1] if "\n" in text else text)
    check("and resting HR and body battery are yesterday's, dated, not 'missing'",
          "Resting HR 58 bpm on Wed 9 Sep, " in text
          and "Body battery high 85 on Wed 9 Sep, " in text and "missing" not in text, text)
    live = db.connect(cfg.wellness_db, read_only=True)
    check("today's sleep-only row doesn't move Garmin's freshness",
          guard.freshness(live, "garmin", today=today, stale_after_days=2).newest
          == date(2026, 9, 9))
    live.close()
    after = state()
    source = never()
    rc, line, _ = run(source)
    check("run again: already loaded, exit 0, no request",
          rc == 0 and line["result"] == "already_loaded" and source.fetched == 0, str(line))
    check("and nothing changed", state() == after)

    print("\nSleep catch-up - a failed snapshot is finished by the next run")
    later = datetime(2026, 9, 11, 10, 30, tzinfo=TZ)
    real_snapshot = sync_all.cmd_snapshot

    def full_disk(conn, cfg):
        raise OSError("disk full")
    sync_all.cmd_snapshot = full_disk
    try:
        rc, line, _ = run(Fake(reply(tomorrow, "01:10", "08:05", 24000),
                               expires=later + timedelta(hours=20)), when=later)
    finally:
        sync_all.cmd_snapshot = real_snapshot
    check("a snapshot that fails: exit 2, and the snapshot doesn't have the night",
          rc == 2 and "disk full" in line["error"] and sc.snapshot_night(cfg, tomorrow) is None,
          str(line))
    source = never()
    rc, line, _ = run(source, when=later)
    check("the next run loads it from GarminDB with no second download",
          rc == 0 and line["result"] == "loaded" and source.fetched == 0
          and sc.snapshot_night(cfg, tomorrow) == 24000, str(line))
    check("each line completes the night before: when you woke, and what the first run found",
          line["previous"] == {"night": "2026-09-10", "wake_local": "2026-09-10T07:05",
                               "found": False, "run_local": "2026-09-10T10:30:00-05:00",
                               "gap_min": 205}, str(line["previous"]))
    live = db.connect(cfg.wellness_db, read_only=True)
    f = guard.freshness(live, "garmin", today=date(2026, 9, 13), stale_after_days=2)
    raw = live.execute("SELECT MAX(day) FROM garmin_daily").fetchone()[0]
    live.close()
    check("a stopped nightly still goes stale, whatever the catch-up loads",
          raw == "2026-09-11" and f.newest == date(2026, 9, 9) and f.stale, f"{raw} {f}")

    print("\nSleep catch-up - wiring")
    seen = []
    real_cmd = sync_all.cmd_sleep_catchup
    sync_all.cmd_sleep_catchup = lambda cfg: seen.append(cfg) or 0
    live = db.connect(cfg.wellness_db, read_only=True)
    applied = live.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
    try:
        rc = sync_all.main(["--config", str(base / "config.toml"), "sleep-catchup"])
    finally:
        sync_all.cmd_sleep_catchup = real_cmd
    check("`sync_all.py sleep-catchup` is routed before wellness.db is opened",
          rc == 0 and len(seen) == 1
          and live.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == applied)
    live.close()
    check("and nothing here imported garmindb", "garmindb" not in sys.modules)
    sh = ROOT / "scripts" / "sleep_catchup.sh"
    text = sh.read_text()
    check("sleep_catchup.sh is mode 700, takes no arguments and sources no secrets",
          (sh.stat().st_mode & 0o777) == 0o700
          and not re.search(r'\$[@*1-9]|\$\{[@*1-9]', text) and "sync.env" not in text)
    check("it runs the catch-up with stdin closed, so a login prompt can't hang it",
          "sync_all.py sleep-catchup < /dev/null" in text)
    check("it rebuilds and publishes only what's newer, and checks the mount before copying",
          '"$SNAPSHOT" -nt "$DASH_SRC"' in text and '"$DASH_SRC" -nt "$PUBLISH_DIR/index.html"' in text
          and text.index('mountpoint -q "$PUBLISH_MOUNT"') < text.index('cp "$DASH_SRC"'))
    for name in ("nightly.sh", "sleep_catchup.sh"):
        body = (ROOT / "scripts" / name).read_text()
        check(f"{name} publishes only to a PUBLISH_DIR you set, read from local.env, never sourced",
              not re.search(r'^\s*PUBLISH_DIR="?/', body, re.M)
              and 'sed -n \'s/^PUBLISH_DIR=//p\' "$LOCAL_ENV"' in body
              and '. "$LOCAL_ENV"' not in body and 'source "$LOCAL_ENV"' not in body
              and 'echo "publish: no PUBLISH_DIR set; skipped"' in body)


def test_catalog_fetch(root: Path) -> None:
    """fetch-catalog installs the movement catalog only if it is the pinned
    revision. No network: the download is a fake."""
    import hashlib
    print("\nTonal movements - fetch-catalog installs only the pinned file")

    def check_(label: str, cond: bool, detail: str = "") -> None:
        check(label, cond, detail if not cond else "")

    readme = (ROOT / "reference" / "README.md").read_text()
    check_("the pinned hash and revision are the ones reference/README.md records",
           f"sha256: `{tonal.CATALOG_SHA256}`" in readme
           and "623fa360af417e73fe7015e76a269780d5d2e344" in tonal.CATALOG_URL
           and "623fa360af417e73fe7015e76a269780d5d2e344" in readme)
    dest = root / "fetched" / "tonal-movements.json"
    good = json.dumps(CATALOG).encode()
    tonal.install_catalog(good, dest, hashlib.sha256(good).hexdigest())
    check_("bytes with the pinned hash are installed", dest.read_bytes() == good)
    try:
        tonal.install_catalog(b"[]", dest, hashlib.sha256(good).hexdigest())
        refused = False
    except ValueError:
        refused = True
    check_("anything else is refused, and the installed file is left alone",
           refused and dest.read_bytes() == good
           and not dest.with_name(dest.name + ".tmp").exists())

    class Reply(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    asked = []
    other = root / "fetched" / "other.json"

    def opener(url, timeout):
        asked.append(url)
        return Reply(good)

    try:
        tonal.fetch_catalog(other, opener=opener)
        refused = False
    except ValueError:
        refused = True
    check_("a download that isn't the pinned revision is refused, and nothing is written",
           refused and asked == [tonal.CATALOG_URL] and not other.exists())
    check_("the catalog is gitignored, so it can't be committed by accident",
           "reference/tonal-movements.json" not in subprocess.run(
               ["git", "-C", str(ROOT), "ls-files", "reference"], capture_output=True,
               text=True).stdout)


def test_demo(root: Path) -> None:
    """sync_all.py demo: an invented history, loaded clean by the real
    loaders and guard, within the agreed limits."""
    import shutil
    print("\nDemo - an invented person, through the real pipeline")

    def check_(label: str, cond: bool, detail: str = "") -> None:
        check(label, cond, detail if not cond else "")

    tz = ZoneInfo(demo.TZ_NAME)
    today = date(2026, 9, 30)
    now = datetime(2026, 9, 30, 11, 0, tzinfo=tz)
    out = root / "demo-out"

    def run(target=out, **kw) -> tuple[int, str]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = sync_all.cmd_demo(str(target), today=today, now=now,
                                   forbidden=kw.pop("forbidden", []), catalog=kw.pop("catalog", None),
                                   **kw)
        return rc, buf.getvalue()

    rc, text = run()
    check_("it exits 0: every source loaded clean through the real loaders and guard",
           rc == 0 and text.count("[ok   ]") == 3 and "Every source loaded clean" in text, text)
    cfg = config.load(out / "config.toml")
    check_("its config keeps every path inside its own folder",
           all(str(x).startswith(str(out.resolve()))
               for x in (cfg.wellness_db, cfg.garmin_db_dir, cfg.tonal_export_dir, cfg.archive_dir,
                         cfg.dashboard_dir, cfg.withings_token))
           and cfg.tz_name == "America/New_York", str(cfg))
    snap = db.connect(out / "wellness-ro.db", read_only=True)
    q = lambda sql: snap.execute(sql).fetchall()
    w = [r[0] for r in q("SELECT weight_kg FROM v_body_composition")]
    fat = sorted(r[0] for r in q("SELECT fat_ratio FROM v_body_composition"))
    sleep = [r[0] / 3600 for r in q("SELECT sleep_total_s FROM garmin_daily WHERE sleep_total_s > 0")]
    rhr = [r[0] for r in q("SELECT rhr FROM garmin_daily WHERE rhr IS NOT NULL")]
    lbs = [r[0] for r in q("SELECT avg_weight_lb FROM tonal_set WHERE avg_weight_lb > 0")]
    check_("weight stays within 70-95 kg, and no night is outside 4.5-9.5 h",
           70 <= min(w) and max(w) <= 95 and 4.5 <= min(sleep) and max(sleep) <= 9.5,
           f"{min(w)}..{max(w)} kg, {min(sleep)}..{max(sleep)} h")
    check_("body fat stays within 12-32%, but for the one deliberate bad reading",
           12 <= fat[0] and fat[-2] <= 32 and fat[-1] > 40, f"{fat[0]} {fat[-2]} {fat[-1]}")
    check_("resting HR within 45-75; Tonal never above its 100 lb per arm",
           45 <= min(rhr) and max(rhr) <= 75 and max(lbs) <= 100.0, f"{min(rhr)} {max(rhr)} {max(lbs)}")
    check_("some nights are Garmin's 0 s, which the page must show as missing",
           q("SELECT COUNT(*) FROM garmin_daily WHERE sleep_total_s = 0")[0][0] > 0)
    ds = demo.generate(today, now)
    check_("every BodyScan weigh-in partitions: the segments add up to the whole body",
           guard.withings_partition(ds.withings) == []
           and any(m.get("position") == 12 for g in ds.withings for m in g["measures"]))
    check_("the same seed gives the same data", json.dumps(ds.tonal) == json.dumps(
        demo.generate(today, now).tonal) and ds.withings == demo.generate(today, now).withings)
    bad = demo.Dataset(today=today, now=now)
    bad.saw("weight_kg", 181.0)
    try:
        demo.check_limits(bad)
        raised = False
    except ValueError:
        raised = True
    check_("a value outside the agreed limits stops the generator, never clipped quietly", raised)

    page = (out / "dashboard" / "index.html").read_text()
    blob = json.loads(page.split('<script id="data" type="application/json">', 1)[1]
                      .split("</script>", 1)[0])
    bal = blob["balance"]
    check_("the invented person is left-side dominant: the arms' caption leads left, the legs' doesn't",
           bal["arms"]["lead"] == "left" and bal["legs"]["lead"] is None, str(bal))
    algo = blob["bodyscan"]["algo"]
    check_("the invented algorithm update is called out on both legs",
           len(algo) == 1 and algo[0]["steps"]["leg_left"] >= demo.LEG_ALGO_SHIFT * 100 - 1
           and algo[0]["steps"]["leg_right"] >= demo.LEG_ALGO_SHIFT * 100 - 1, str(algo))
    check_("the annotated gap carries its own label onto the chart",
           [a["label"] for a in blob["annotations"]] == ["trained elsewhere — volume not captured"])
    night = blob["daily"]["night"]
    check_("this morning's night is sleep-only, so resting HR comes from yesterday, dated",
           night["day"] == "2026-09-30" and next(r for r in night["rows"] if r["key"] == "rhr")["day"]
           == "2026-09-29", str([(r["key"], r["day"]) for r in night["rows"]]))
    names = {r[0] for r in q("SELECT name FROM tonal_movement")}
    check_("movement names are invented unless the real catalog is there",
           names == {sl.name for sl in demo.SLOTS.values()}, str(names))
    snap.close()

    fake = [{**m, "id": m["id"][:-4] + "beef", "name": "Catalog " + m["name"]}
            for m in demo.invented_catalog()]
    rc, text = run(catalog=fake)
    snap = db.connect(out / "wellness-ro.db", read_only=True)
    names = {r[0] for r in snap.execute("SELECT name FROM tonal_movement")}
    check_("with a catalog, each movement is a catalog one of the same kind, and sides still match",
           rc == 0 and names and all(n.startswith("Catalog ") for n in names)
           and "from the fetched catalog" in text, str(names))
    snap.close()

    def refused(target, **kw) -> bool:
        try:
            run(target, **kw)
        except SystemExit:
            return True
        return False

    real = root / "HealthDataLike"
    (real / "DBs").mkdir(parents=True)
    check_("it refuses a folder that is, holds or sits inside a real data folder",
           refused(real, forbidden=[real.resolve()]) and refused(real / "DBs", forbidden=[real.resolve()])
           and refused(root, forbidden=[real.resolve()]))
    check_("and the repo itself", refused(ROOT, forbidden=[]))
    stranger = root / "not-a-demo"
    stranger.mkdir()
    (stranger / "notes.txt").write_text("mine")
    check_("and a non-empty folder it didn't make, leaving it as it was",
           refused(stranger) and (stranger / "notes.txt").read_text() == "mine"
           and len(list(stranger.iterdir())) == 1)
    rc, _ = run()
    check_("its own folder is rebuilt from scratch on every run",
           rc == 0 and (out / demo.MARKER).is_file()
           and len(list((out / "tonal_exports").iterdir())) == 1)

    print("\nWithings - a full pull that loads cleanly is ok, not a warning")
    c = db.connect(root / "fullpull.db")
    db.apply_schema(c)
    (root / "fullpull").mkdir()
    wcfg = config.load(write_config(root / "fullpull"))
    withings.fetch_measures = lambda cfg, **kw: ([scan_group(1, "2026-09-01", "07:00")], 1, 1, True)
    full = withings.sync(c, wcfg, full=True, ignore_rate_limit=True)
    again = withings.sync(c, wcfg, ignore_rate_limit=True)
    check_("first and full pulls say so, and stay ok; an incremental one is ok too",
           full["status"] == "ok" and any("Full pull" in a for a in full["anomalies"])
           and again["status"] == "ok", f"{full} {again}")
    c.close()
    shutil.rmtree(out)


def test_leakcheck(root: Path) -> None:
    """scripts/leakcheck.sh in throwaway repos, one leak planted in each, and
    .gitignore against the paths a real install writes. Planted secrets are
    built by concatenation, so this file never holds one for the check to
    find."""
    import os
    import shutil
    import subprocess

    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    base = root / "leak"
    base.mkdir()
    n = iter(range(100))

    def git(repo: Path, *args: str) -> None:
        subprocess.run(["git", "-C", str(repo), *args], env=env, check=True,
                       capture_output=True)

    def repo(files: dict[str, str | bytes], commit: bool = True) -> Path:
        r = base / f"r{next(n)}"
        (r / "scripts").mkdir(parents=True)
        shutil.copy(ROOT / "scripts" / "leakcheck.sh", r / "scripts" / "leakcheck.sh")
        git(r, "init", "-q", "-b", "main")
        for name, body in {"app.py": "print('hi')\n", **files}.items():
            p = r / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(body if isinstance(body, bytes) else body.encode())
        git(r, "add", "-f", ".")
        if commit:
            git(r, "commit", "-q", "-m", "c")
        return r

    def run(r: Path, *args: str) -> tuple[int, str]:
        p = subprocess.run(["bash", str(r / "scripts" / "leakcheck.sh"), *args], env=env,
                           capture_output=True, text=True, cwd=base)
        return p.returncode, p.stdout + p.stderr

    def ck(label: str, cond: bool, out: str = "") -> None:
        # A run's output is long: print it only when the check fails.
        check(label, cond, "" if cond else "\n" + out)

    def hit(out: str, section: str) -> bool:
        return any(line.startswith("HIT") and section in line for line in out.splitlines())

    print("\nLeak check - a clean repo passes")
    rc, out = run(repo({"wellness/dashboard.html": "<p>__DASHBOARD_DATA__</p>\n",
                        "notes.md": 'client_secret = "test-secret"\n'
                                    'withings_token = "~/.secrets/withings_token.json"\n'
                                    'client_id = "WITHINGS_CLIENT_ID"\n'}))
    ck("exit 0, and test placeholders, env var names and token paths aren't credentials",
          rc == 0 and "leakcheck: clean" in out, out)

    print("\nLeak check - each kind of leak exits 1, in its own section")
    for label, files, section in (
        ("a staged database", {"wellness.db": b"SQLite format 3\0"}, "1. tracked files that look like data"),
        ("a WAL file", {"wellness.db-wal": b"x"}, "1. tracked files that look like data"),
        ("a log", {"sync.log": "x"}, "1. tracked files that look like data"),
        ("the catch-up's run record", {"sleep_catchup.jsonl": "{}"}, "1. tracked files that look like data"),
        ("a Tonal export", {"tonal_workouts_20261009_162543.json": "{}"}, "1. tracked files that look like data"),
        ("anything under HealthData", {"HealthData/notes.txt": "x"}, "1. tracked files that look like data"),
        ("a generated dashboard", {"out/index.html": "<html>"}, "1. tracked files that look like data"),
        ("a built page over the template", {"wellness/dashboard.html": '<script>{"rhr": 61}</script>'},
         "1. the dashboard template is still a template"),
        ("a file over 1 MB", {"big.txt": "x" * (1024 * 1024 + 1)}, "1. tracked files over 1 MB"),
        ("an AWS key", {"a.py": "k = '" + "AKIA" + "Q" * 16 + "'\n"}, "3. literal credentials"),
        ("a quoted password", {"a.py": 'password = "' + "hunter" + "2-xyz" + '"\n'}, "3. literal credentials"),
        ("a Telegram bot token", {"a.sh": "T=" + "1234567890:AA" + "b" * 33 + "\n"}, "3. literal credentials"),
    ):
        rc, out = run(repo(files, commit=False))
        ck(f"{label}: exit 1", rc == 1 and hit(out, section) and "leakcheck: 1 hit(s)" in out, out)

    rc, out = run(repo({"a.py": 'password = "' + "hunter" + '2-xyz"  # leakcheck: allow\n'}))
    ck("a line marked `leakcheck: allow` is let through", rc == 0, out)

    print("\nLeak check - history: a file deleted today is still in every commit that had it")
    r = repo({"wellness.db": b"SQLite format 3\0", "a.py": 'api_key = "' + "zq81" * 3 + '"\n'})
    git(r, "rm", "-q", "--cached", "wellness.db")
    git(r, "rm", "-q", "a.py")
    git(r, "commit", "-q", "-m", "remove")
    rc, out = run(r)
    ck("the index is clean, but the database's name in history is a hit",
          rc == 1 and not hit(out, "1. tracked files that look like data")
          and hit(out, "2. files ever added in history"), out)
    ck("and --history also finds the deleted credential",
          not hit(out, "3. literal credentials anywhere") and
          hit(run(r, "--history")[1], "3. literal credentials anywhere in history"))

    print("\nLeak check - personal strings, from a list that is never tracked")
    r = repo({"a.py": "# Zebulon's weigh-in\n", ".gitignore": ".leakcheck-terms\n"})
    rc, out = run(r)
    ck("without .leakcheck-terms the check is skipped, and says so",
          rc == 0 and "skip  4. personal strings" in out, out)
    (r / ".leakcheck-terms").write_text("# a comment\n\nzebulon\n")
    rc, out = run(r, "--history")
    ck("with it: tracked files, history and authors are searched, case-insensitive",
          rc == 1 and hit(out, "4. personal strings in tracked files")
          and hit(out, "4. personal strings in files in history"), out)
    lone = base / "not-a-repo" / "scripts"
    lone.mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "leakcheck.sh", lone / "leakcheck.sh")
    rc, out = run(lone.parent)
    ck("outside a git repository it exits 2", rc == 2 and "not inside a git repository" in out, out)

    print("\nLeak check - .gitignore covers what a real install writes")
    data = ["index.html", "dashboard/index.html", "wellness.db", "wellness-ro.db",
            "wellness.db-wal", "wellness.db-shm", "wellness.db-journal",
            "wellness.pre-fixes-20261014.db", "wellness.pre-fixes-20261014.db-shm",
            "sync.log", "garmindb.log", "sleep_catchup.jsonl",
            "tonal_workouts_20261009_162543.json", "tonal_exports/x.json", "raw/tonal/x.json",
            "HealthData/DBs/garmin.db", "DBs/garmin.db", "FitFiles/x.fit", "config.toml",
            ".env", "sync.env", ".secrets/withings_token.json", "garmin_tokens.json",
            "scripts/nightly.sh.bak", ".leakcheck-terms", "local.env",
            "reference/tonal-movements.json", "demo/wellness.db", "demo/.wellness-demo",
            "demo/movements.json", "demo/dashboard/index.html"]
    p = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "--no-index", "--stdin"],
                       input="\n".join(data), capture_output=True, text=True, env=env)
    missed = sorted(set(data) - set(p.stdout.split()))
    ck("every database, journal, log, export, token and generated page is ignored",
          not missed, str(missed))
    tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files"], capture_output=True,
                             text=True, env=env).stdout.split()
    p = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "--no-index", "--stdin"],
                       input="\n".join(tracked), capture_output=True, text=True, env=env)
    ck("and no tracked file is", tracked and not p.stdout.split(), p.stdout)


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="wdl-test-"))
    print(f"fixture root: {root}\n")

    cfg_path = write_config(root)
    build_fake_garmindb(root)
    cfg = config.load(cfg_path)

    conn = db.connect(cfg.wellness_db)
    db.apply_schema(conn)

    days = [(datetime(2026, 9, 1) + timedelta(days=i)).date().isoformat() for i in range(7)]

    print("Tonal - first export (5 workouts)")
    e1 = write_tonal_export(root, [make_workout(i, days[i]) for i in range(5)],
                            "tonal_workouts_20261001T080000.json",
                            "2026-10-01T08:00:00Z")
    r1 = tonal.sync(conn, cfg, e1)
    check("first export accepted", r1["status"] in ("ok", "ok_warn"), r1["status"])
    check("5 workouts stored",
          conn.execute("SELECT COUNT(*) FROM tonal_workout").fetchone()[0] == 5)
    check("10 sets stored",
          conn.execute("SELECT COUNT(*) FROM tonal_set").fetchone()[0] == 10)
    # 2 history dates x 4 scopes, plus 2 regions from currentStrengthScores
    check("strength history parsed from the real list-of-records shape",
          conn.execute("SELECT COUNT(*) FROM tonal_strength_score").fetchone()[0] == 10,
          str(conn.execute("SELECT COUNT(*) FROM tonal_strength_score").fetchone()[0]))
    check("every scope resolved, not just overall",
          conn.execute("SELECT COUNT(DISTINCT scope) FROM tonal_strength_score"
                       ).fetchone()[0] == 4)
    check("muscle scores parsed from the nested 'parsed' shape",
          conn.execute("SELECT COUNT(*) FROM tonal_muscle_score").fetchone()[0] == 3)
    check("muscle scores dated from updatedAt, not today",
          conn.execute("SELECT COUNT(*) FROM tonal_muscle_score "
                       "WHERE snapshot_date='2026-09-07'").fetchone()[0] == 3)

    print("\nTonal - dict-keyed catalog and template ids (toneget's real shape)")
    w0 = conn.execute(
        "SELECT template_id, title, is_custom FROM tonal_workout WHERE workout_id='w0'"
    ).fetchone()
    check("template id read from `workoutId`, not confused with the activity id",
          w0["template_id"] == "tpl1", str(w0["template_id"]))
    check("activity keyed by its own `id`",
          conn.execute("SELECT COUNT(*) FROM tonal_workout WHERE workout_id LIKE 'w%'"
                       ).fetchone()[0] == 5)
    check("title resolved", w0["title"] == "Back Day")
    check("a PROGRAM workout is not flagged custom", w0["is_custom"] == 0)

    print("\nTonal - second export appends 2 more, one with a naive timestamp")
    w2 = [make_workout(i, days[i]) for i in range(5)]
    w2.append(make_workout(5, days[5], naive_ts=True))
    w2.append(make_workout(6, days[6], template="tplCustom"))
    e2 = write_tonal_export(root, w2, "tonal_workouts_20261002T080000.json",
                            "2026-10-02T08:00:00Z")
    r2 = tonal.sync(conn, cfg, e2)
    check("append accepted", r2["status"] in ("ok", "ok_warn"), r2["status"])
    check("7 workouts stored",
          conn.execute("SELECT COUNT(*) FROM tonal_workout").fetchone()[0] == 7)
    check("naive timestamp flagged as assumed_local",
          conn.execute("SELECT COUNT(*) FROM tonal_workout WHERE tz_source='assumed_local'"
                       ).fetchone()[0] == 1)
    check("tz warning surfaced to the operator",
          any("no timezone offset" in a for a in r2["anomalies"]))
    check("workout on a custom template flagged from the dict-keyed customWorkouts",
          conn.execute("SELECT is_custom FROM tonal_workout WHERE workout_id='w6'"
                       ).fetchone()[0] == 1)

    print("\nTonal - re-running the same export must be a no-op, not a reload")
    r2b = tonal.sync(conn, cfg, e2)
    check("identical export skipped by content hash",
          r2b["rows_upserted"] == 0 and any("already processed" in a for a in r2b["anomalies"]))
    check("still 7 workouts",
          conn.execute("SELECT COUNT(*) FROM tonal_workout").fetchone()[0] == 7)

    print("\nTonal - TRUNCATED export (a dropped page: 2 workouts vanish)")
    e3 = write_tonal_export(root, [make_workout(i, days[i]) for i in range(5)],
                            "tonal_workouts_20261003T080000.json",
                            "2026-10-03T08:00:00Z")
    r3 = tonal.sync(conn, cfg, e3)
    check("truncated export REJECTED", r3["status"] == "rejected", r3["status"])
    check("rejection names the cause",
          any("missing" in a and "dropped page" in a for a in r3["anomalies"]))
    check("history survived intact (still 7)",
          conn.execute("SELECT COUNT(*) FROM tonal_workout").fetchone()[0] == 7)
    check("no sets were lost",
          conn.execute("SELECT COUNT(*) FROM tonal_set").fetchone()[0] == 14)
    check("rejected export archived for forensics",
          conn.execute("SELECT COUNT(*) FROM tonal_export_archive WHERE verdict='rejected'"
                       ).fetchone()[0] == 1)

    print("\nTonal - with the README's pagination patch, a short export is")
    print("        provably short even when nothing is missing from the DB")
    e4 = write_tonal_export(root, w2, "tonal_workouts_20261004T080000.json",
                            "2026-10-04T08:00:00Z",
                            pagination={"declaredTotal": 9, "collected": 7,
                                        "failedPages": 1})
    r4 = tonal.sync(conn, cfg, e4)
    check("declared-total mismatch REJECTED", r4["status"] == "rejected", r4["status"])
    check("rejection quantifies the loss",
          any("declared 9" in a and "2 were lost" in a for a in r4["anomalies"]))
    check("exporter's own failed-page count surfaced",
          any("1 failed page" in a for a in r4["anomalies"]))
    check("history still intact",
          conn.execute("SELECT COUNT(*) FROM tonal_workout").fetchone()[0] == 7)

    print("\nGarmin - schema discovery and duration coercion")
    rg = garmin.sync(conn, cfg)
    check("garmin sync ok", rg["status"] in ("ok", "ok_warn"), rg["status"])
    check("8 days loaded",
          conn.execute("SELECT COUNT(*) FROM garmin_daily").fetchone()[0] == 8)
    row = conn.execute(
        "SELECT intensity_time_s, sleep_total_s, sleep_score, weight_lb, rhr "
        "FROM garmin_daily WHERE day='2026-09-02'").fetchone()
    check("'01:05:00' -> 3900 seconds", row["intensity_time_s"] == 3900,
          str(row["intensity_time_s"]))
    check("'07:12:00' sleep -> 25920 seconds", row["sleep_total_s"] == 25920,
          str(row["sleep_total_s"]))
    check("sleep score mapped from the sleep table", row["sleep_score"] is not None)
    check("weight mapped from the weight table", row["weight_lb"] is not None)
    check("activity loaded",
          conn.execute("SELECT COUNT(*) FROM garmin_activity").fetchone()[0] == 1)
    act = conn.execute("SELECT start_local, local_date FROM garmin_activity").fetchone()
    check("activity start kept as the local wall-clock time GarminDB wrote",
          tuple(act) == ("2026-09-03 18:10:00.000000", "2026-09-03"), str(tuple(act)))
    mapped = conn.execute(
        "SELECT COUNT(*) FROM garmin_column_map WHERE mapped=1").fetchone()[0]
    check("column provenance recorded", mapped >= 15, f"{mapped} columns mapped")
    stress_src = conn.execute(
        "SELECT source_table, source_column FROM garmin_column_map "
        "WHERE target_column='stress_avg'").fetchone()
    check("stress_avg read from the all-day summary, not the sleep table",
          tuple(stress_src) == ("daily_summary", "stress_avg"), str(tuple(stress_src)))
    check("stress_avg holds the all-day value",
          conn.execute("SELECT stress_avg FROM garmin_daily WHERE day='2026-09-02'"
                       ).fetchone()[0] == 29)
    check("Garmin's -1 'could not measure' stress code stored as NULL",
          conn.execute("SELECT stress_avg FROM garmin_daily WHERE day='2026-09-08'"
                       ).fetchone()[0] is None)

    print("\nFreshness - a load that works on data that stopped advancing")
    # garmin_daily ends 2026-09-08; tonal ends 2026-09-07.
    rf = sync_all._run(conn, cfg, "garmin", today=date(2026, 9, 9))
    check("garmin 1 day old is fresh: plain ok", rf["status"] == "ok", rf["status"])
    rs = sync_all._run(conn, cfg, "garmin", today=date(2026, 9, 14))
    check("garmin 6 days old: ok_warn, not ok", rs["status"] == "ok_warn", rs["status"])
    check("warning names the newest day and its age",
          any("2026-09-08" in a and "6 day(s) old" in a for a in rs["anomalies"]),
          str(rs["anomalies"]))
    check("a stale source still loads", rs["rows_upserted"] > 0)
    check("tonal 7 days old is within its own 14-day limit",
          not guard.evaluate_freshness(conn, "tonal", today=date(2026, 9, 14),
                                       stale_after_days=sync_all._stale_after(cfg, "tonal")
                                       ).anomalies)
    cfg.garmin["stale_after_days"] = 10
    ro = sync_all._run(conn, cfg, "garmin", today=date(2026, 9, 14))
    del cfg.garmin["stale_after_days"]
    check("limit comes from config.toml when set", ro["status"] == "ok", ro["status"])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        sync_all.cmd_status(conn, cfg, today=date(2026, 9, 14))
    rows = {ln.split()[0]: ln for ln in buf.getvalue().splitlines() if "last good:" in ln}
    check("status shows garmin STALE beside its freshness row",
          "newest data: 2026-09-08 (6d)" in rows["garmin"] and "STALE" in rows["garmin"],
          rows["garmin"].strip())
    check("status does not mark tonal stale", "STALE" not in rows["tonal"],
          rows["tonal"].strip())

    print("\nAnnotations - a known gap is acknowledged once, then stays quiet")
    # With their stored sets: an export without them would be refused.
    held = [{"workout_id": r[0], "local_date": r[1],
             "_sets": [{"set_uid": u, "workout_id": r[0], "prescribed_set_id": p,
                        "side_number": sd} for u, p, sd in conn.execute(
                 "SELECT set_uid, prescribed_set_id, side_number FROM tonal_set "
                 "WHERE workout_id = ?", (r[0],))]}
            for r in conn.execute("SELECT workout_id, local_date FROM tonal_workout").fetchall()]
    later = held + [{"workout_id": f"later{i}", "local_date": f"2026-10-0{i}"}
                    for i in (1, 2, 3)]
    v = guard.evaluate_tonal(conn, later)
    check("unexplained 24-day gap warned", any("24-day gap" in a for a in v.anomalies),
          str(v.anomalies))
    check("tonal 28 days old is stale",
          guard.freshness(conn, "tonal", today=date(2026, 10, 5), stale_after_days=14).stale)
    conn.execute("INSERT INTO annotations (source, start_date, end_date, note, recorded_by) "
                 "VALUES ('tonal', '2026-09-08', '2026-09-30', 'Travelling', 'test')")
    conn.commit()
    v = guard.evaluate_tonal(conn, later)
    check("annotated gap no longer warned, so the verdict is plain ok",
          v.status == "ok" and not any("-day gap" in a for a in v.anomalies), str(v))
    f = guard.freshness(conn, "tonal", today=date(2026, 10, 5), stale_after_days=14)
    check("annotation also counts toward staleness",
          not f.stale and f.through == date(2026, 9, 30), str(f))

    gym = db.connect(root / "gym.db")
    db.apply_schema(gym)
    check("the schema seeds no annotations: each install records its own",
          gym.execute("SELECT COUNT(*) FROM annotations").fetchone()[0] == 0)
    gym.execute("INSERT INTO annotations (source, start_date, end_date, note, recorded_by, label) "
                "VALUES ('tonal', '2025-10-06', '2026-01-05', 'Training at a gym, not on Tonal.', "
                "'test', 'trained elsewhere')")
    before = [date(2025, 9, 27) + timedelta(days=i) for i in range(10)]   # ..10-06

    def history(*after: date) -> list[dict]:
        return [{"workout_id": f"h{i}", "local_date": d.isoformat()}
                for i, d in enumerate(before + list(after))]

    v = guard.evaluate_tonal(gym, history(date(2026, 1, 5)))
    check("an annotated 91-day gap is not flagged",
          not any("-day gap" in a for a in v.anomalies), str(v.anomalies))
    v = guard.evaluate_tonal(gym, history(date(2026, 2, 4)))
    check("an annotation covers only its own days",
          any("30-day gap" in a for a in v.anomalies), str(v.anomalies))
    gym.close()

    old = db.connect(root / "annotations-old.db")
    old.executescript("""
        CREATE TABLE annotations (
            annotation_id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL,
            start_date TEXT NOT NULL, end_date TEXT NOT NULL, note TEXT NOT NULL,
            recorded_by TEXT NOT NULL, recorded_utc TEXT,
            UNIQUE (source, start_date, end_date));
        INSERT INTO annotations (source, start_date, end_date, note, recorded_by)
            VALUES ('tonal', '2025-10-06', '2026-01-05', 'away', 'me');""")
    db.apply_schema(old)
    db.apply_schema(old)
    row = old.execute("SELECT note, label FROM annotations").fetchall()
    check("an annotations table from before labels gains the column once, its rows kept",
          [tuple(r) for r in row] == [("away", None)] and db.add_columns(old) == [],
          str([tuple(r) for r in row]))
    old.close()

    print("\nExit codes - stale data alerts, advisory warnings do not")
    # Its own database, so the history added here can't shift later counts.
    ex = db.connect(root / "exit.db")
    db.apply_schema(ex)

    clean = sync_all._run(ex, cfg, "garmin", today=date(2026, 9, 9))
    check("a clean run exits 0",
          clean["status"] == "ok" and sync_all.exit_code({"garmin": clean}) == 0,
          clean["status"])
    stale = sync_all._run(ex, cfg, "garmin", today=date(2026, 9, 14))
    check("a stale source still loads, then exits 3",
          stale["rows_upserted"] > 0 and stale["stale"]
          and sync_all.exit_code({"garmin": stale}) == 3, str(stale["anomalies"]))
    check("rejected or errored outranks stale",
          sync_all.exit_code({"garmin": stale, "tonal": {"status": "rejected"}}) == 1
          and sync_all.exit_code({"garmin": stale, "tonal": {"status": "rejected"},
                                  "withings": {"status": "error"}}) == 2)

    # Ten lift days with a 27-day hole between 2026-08-05 and 2026-09-01.
    lift_days = ([date(2026, 8, 1) + timedelta(days=i) for i in range(5)]
                 + [date(2026, 9, 1) + timedelta(days=i) for i in range(5)])

    def lift(n: int, *, today: date, naive: int | None = None) -> dict:
        wk = [make_workout(i, d.isoformat(), naive_ts=i == naive)
              for i, d in enumerate(lift_days)]
        path = write_tonal_export(root / "exit", wk, f"tonal_workouts_exit{n}.json",
                                  f"2026-09-06T0{n}:00:00Z")
        return sync_all._run(ex, cfg, "tonal", today=today, export_path=path)

    adv = lift(1, today=date(2026, 9, 6), naive=9)
    check("an unannotated gap and an assumed timezone warn, but exit 0",
          adv["status"] == "ok_warn" and not adv["stale"]
          and any("27-day gap" in a for a in adv["anomalies"])
          and any("no timezone offset" in a for a in adv["anomalies"])
          and sync_all.exit_code({"tonal": adv}) == 0, str(adv["anomalies"]))
    ex.execute("INSERT INTO annotations (source, start_date, end_date, note, recorded_by) "
               "VALUES ('tonal', '2026-08-06', '2026-08-31', 'Injured', 'test')")
    ex.commit()
    ann = lift(2, today=date(2026, 9, 6))
    check("an annotated gap exits 0",
          ann["status"] == "ok" and sync_all.exit_code({"tonal": ann}) == 0,
          str(ann["anomalies"]))

    # The newest lift is 2026-09-05: 30 days old on 10-05, past the 14-day
    # limit, unless an annotation starting there carries it forward.
    ex.execute("INSERT INTO annotations (source, start_date, end_date, note, recorded_by) "
               "VALUES ('tonal', '2026-09-06', '2026-09-30', 'Travelling', 'test')")
    ex.commit()
    cont = lift(3, today=date(2026, 10, 5))
    f = guard.freshness(ex, "tonal", today=date(2026, 10, 5), stale_after_days=14)
    check("old data carried forward by a continuing annotation exits 0",
          f.newest == date(2026, 9, 5) and f.through == date(2026, 9, 30)
          and not cont["stale"] and sync_all.exit_code({"tonal": cont}) == 0,
          f"{f}, {cont['status']}")
    over = lift(4, today=date(2026, 10, 20))
    check("once that annotation runs out, it is stale again and exits 3",
          over["stale"] and sync_all.exit_code({"tonal": over}) == 3
          and any("past the annotation ending 2026-09-30" in a for a in over["anomalies"]),
          str(over["anomalies"]))
    ex.close()

    # main() is where the process exit code is decided; _today is its clock.
    real_today, codes = sync_all._today, {}
    try:
        for d in (date(2026, 9, 9), date(2026, 9, 14)):
            sync_all._today = lambda cfg, d=d: d
            with contextlib.redirect_stdout(io.StringIO()):
                codes[d.isoformat()] = sync_all.main(["--config", str(cfg_path), "garmin"])
    finally:
        sync_all._today = real_today
    check("`sync_all.py garmin` exits 0 while fresh and 3 once stale",
          codes == {"2026-09-09": 0, "2026-09-14": 3}, str(codes))

    print("\nWithings - decoding, discovery and category filtering")
    withings.fetch_measures = lambda cfg, **kw: (withings_payload(), 1, 1760000000, True)
    rw = withings.sync(conn, cfg, full=True, ignore_rate_limit=True)
    check("withings sync ok", rw["status"] in ("ok", "ok_warn"), rw["status"])
    wt = conn.execute(
        "SELECT value FROM withings_measure WHERE grpid='1001' AND meastype=1").fetchone()
    check("9480 with unit -2 -> 94.8 kg", abs(wt["value"] - 94.8) < 1e-9, str(wt["value"]))
    bc = conn.execute("SELECT * FROM v_body_composition").fetchall()
    check("one weigh-in in v_body_composition", len(bc) == 1, f"{len(bc)} rows")
    check("user objective (category 2) excluded",
          all(abs(r["weight_kg"] - 90.0) > 1e-9 for r in bc))
    check("vascular age (155) comes through by day, from its own group",
          conn.execute("SELECT vascular_age FROM v_withings_day WHERE day = '2026-09-02'"
                       ).fetchone()[0] == 38.0)
    check("and a weigh-in row doesn't borrow it from that other group",
          bc[0]["vascular_age"] is None and bc[0]["pulse_wave_velocity"] is None)
    check("the nine BodyScan codes carry their confirmed names",
          dict(conn.execute("SELECT meastype, metric FROM withings_meastype "
                            "WHERE origin = 'confirmed'").fetchall())
          == {155: "vascular_age", 168: "extracellular_water", 169: "intracellular_water",
              170: "visceral_fat", 196: "electrodermal_activity_feet",
              197: "electrodermal_activity_left_foot", 198: "electrodermal_activity_right_foot",
              226: "basal_metabolic_rate", 227: "metabolic_age"})
    check("and the stored reading picks the name up",
          tuple(conn.execute("SELECT metric, unit_label FROM withings_measure WHERE meastype = 155"
                             ).fetchone()) == ("vascular_age", "years"))
    bp = conn.execute("SELECT * FROM v_blood_pressure").fetchall()
    check("one BP reading, not blended with the scale", len(bp) == 1, f"{len(bp)} rows")
    check("122/78 with pulse pressure 44",
          bp[0]["systolic"] == 122 and bp[0]["diastolic"] == 78
          and bp[0]["pulse_pressure"] == 44)
    unk = conn.execute("SELECT * FROM v_withings_unknown_types").fetchall()
    check("an unnamed type (999) captured, not dropped",
          len(unk) == 1 and unk[0]["meastype"] == 999)
    check("discovery surfaced to the operator",
          any("Undocumented measurement type" in a for a in rw["anomalies"]))

    print("\nWithings - segmental data: all five positions must survive")
    seg_rows = conn.execute(
        "SELECT COUNT(*) FROM withings_measure WHERE meastype=173").fetchone()[0]
    check("all 5 segments of type 173 stored, not 1", seg_rows == 5, f"{seg_rows} rows")
    check("no key collisions reported",
          not any("collided on an existing key" in a for a in rw["anomalies"]))
    check("algo and fm captured for provenance",
          conn.execute("SELECT COUNT(*) FROM withings_measure "
                       "WHERE algo=3000100 AND fm=3").fetchone()[0] == 7)
    torso = conn.execute(
        "SELECT fat_free_mass_kg, segment FROM v_segmental WHERE position=12").fetchone()
    check("torso fat-free mass decoded to 37.50 kg",
          abs(torso["fat_free_mass_kg"] - 37.50) < 1e-9, str(torso["fat_free_mass_kg"]))
    check("position 12 named 'torso'", torso["segment"] == "torso")
    n_seg = conn.execute("SELECT COUNT(*) FROM v_segmental").fetchone()[0]
    check("v_segmental has one row per segment", n_seg == 5, f"{n_seg} rows")
    sides = dict(conn.execute(
        "SELECT position, segment FROM v_segmental").fetchall())
    check("position 2 is the RIGHT arm, 3 the left",
          sides[2] == "arm_right" and sides[3] == "arm_left")
    check("position 10 is the LEFT leg, 11 the right",
          sides[10] == "leg_left" and sides[11] == "leg_right")
    bal = conn.execute(
        "SELECT * FROM v_segmental_balance WHERE pair='arms'").fetchone()
    # fixture: position 2 (right) = 3.13 kg, position 3 (left) = 3.23 kg
    check("left/right arms resolved, not swapped",
          abs(bal["right_muscle_kg"] - 3.13) < 1e-9
          and abs(bal["left_muscle_kg"] - 3.23) < 1e-9)
    check("delta is right minus left",
          abs(bal["muscle_delta_kg"] - (3.13 - 3.23)) < 1e-6,
          str(bal["muscle_delta_kg"]))
    check("asymmetry expressed as a percentage of the pair",
          abs(bal["muscle_asymmetry_pct"] - (-3.14)) < 0.01,
          str(bal["muscle_asymmetry_pct"]))
    whole = conn.execute(
        "SELECT COUNT(*) FROM withings_measure WHERE position IS NULL").fetchone()[0]
    check("whole-body measures keep a NULL position", whole == 15, f"{whole} rows")

    print("\nWithings - incomplete pagination must be rejected (via the orchestrator)")
    withings.fetch_measures = lambda cfg, **kw: (withings_payload(), 200, 1760000000, False)
    rwx = sync_all._run(conn, cfg, "withings", full=True, ignore_rate_limit=True)
    check("incomplete walk REJECTED", rwx["status"] == "rejected", rwx["status"])
    check("nothing written on rejection", rwx["rows_upserted"] == 0)
    check("rejection recorded in the audit log",
          conn.execute("SELECT COUNT(*) FROM sync_runs WHERE source='withings' "
                       "AND status='rejected'").fetchone()[0] == 1)

    print("\nWithings - documented 10-minute poll limit respected")
    conn.execute("INSERT INTO sync_runs (source, started_utc, status) "
                 "VALUES ('withings', strftime('%Y-%m-%dT%H:%M:%SZ','now'), 'ok')")
    conn.commit()
    withings.fetch_measures = lambda cfg, **kw: (_ for _ in ()).throw(
        AssertionError("API must not be called inside the rate-limit window"))
    rwr = withings.sync(conn, cfg)
    check("second poll skipped without calling the API", rwr["status"] == "ok"
          and any("Skipped" in a for a in rwr["anomalies"]))

    print("\nCross-source views")
    n_all = conn.execute("SELECT COUNT(*) FROM v_day_all").fetchone()[0]
    check("v_day_all spans all three sources", n_all == 8, f"{n_all} days")
    both = conn.execute(
        "SELECT * FROM v_day_all WHERE lift_volume IS NOT NULL AND weight_kg IS NOT NULL"
    ).fetchall()
    check("a day carries lift load and body composition together", len(both) == 1,
          f"{len(both)} rows")
    lp = conn.execute(
        "SELECT * FROM v_load_vs_pressure WHERE workout_day='2026-09-01'").fetchone()
    check("lift day joins to NEXT day's systolic", lp["systolic_next_day"] == 122,
          str(lp["systolic_next_day"]))
    check("lift day joins to next day's resting HR", lp["rhr_next_day"] is not None)
    nd = conn.execute(
        "SELECT * FROM v_next_day_recovery WHERE workout_day='2026-09-01'").fetchone()
    check("next-day sleep joined", nd["sleep_total_s_next"] == 25920,
          str(nd["sleep_total_s_next"]))
    health = {r["source"]: r for r in conn.execute("SELECT * FROM v_data_health")}
    wh = health.get("withings")
    check("rejected runs are visible in v_data_health",
          wh is not None and wh["rejected_runs"] >= 1)

    print("\nSchema - the Garmin columns whose names lied, renamed in place")

    def cols(c: sqlite3.Connection, table: str) -> set[str]:
        return {r[1] for r in c.execute(f"PRAGMA table_info({table})")}

    # An install from before the rename: today's schema with the old names.
    old = db.connect(root / "old.db")
    db.apply_schema(old)
    for table, before, after in db.RENAMES:
        old.execute(f"ALTER TABLE {table} RENAME COLUMN {after} TO {before}")
    old.execute("INSERT INTO garmin_daily (day, weight_kg, loaded_utc) "
                "VALUES ('2026-10-13', 180.52, 'x')")
    old.execute("INSERT INTO garmin_activity (activity_id, start_utc, local_date, loaded_utc) "
                "VALUES ('a9', '2026-10-08 18:47:33.000000', '2026-10-08', 'x')")
    old.execute("INSERT INTO garmin_column_map VALUES "
                "('garmin_daily', 'weight_kg', 'garmin.db', 'weight', 'weight', 1, 'x')")
    old.commit()
    check("the old database starts with the old names",
          "weight_kg" in cols(old, "garmin_daily") and "start_utc" in cols(old, "garmin_activity"))
    db.apply_schema(old)
    check("garmin_daily.weight_kg is now weight_lb, its pounds kept",
          "weight_kg" not in cols(old, "garmin_daily")
          and old.execute("SELECT weight_lb FROM garmin_daily").fetchone()[0] == 180.52)
    check("garmin_activity.start_utc is now start_local, its value kept",
          "start_utc" not in cols(old, "garmin_activity")
          and old.execute("SELECT start_local FROM garmin_activity").fetchone()[0]
          == "2026-10-08 18:47:33.000000")
    check("column provenance follows the rename",
          [r[0] for r in old.execute("SELECT target_column FROM garmin_column_map")] == ["weight_lb"])
    db.apply_schema(old)
    check("applying the schema again renames nothing and loses nothing",
          db.rename_columns(old) == []
          and old.execute("SELECT weight_lb FROM garmin_daily").fetchone()[0] == 180.52)
    described = {(r[0], r[1]) for r in old.execute(
        "SELECT object_name, column_name FROM data_dictionary")}
    check("the data dictionary describes weight_lb and start_local",
          {("garmin_daily", "weight_lb"), ("garmin_activity", "start_local")} <= described)
    old.close()

    print("\nDashboard - the page may not invent what the guard refused")

    def rec(src: str, hhmm: str | None, mins: int, **kw) -> dict:
        start = datetime(2026, 10, 1, *map(int, hhmm.split(":"))) if hhmm else None
        return {"src": src, "day": date(2026, 10, 1), "start": start, "dur_s": mins * 60,
                "title": src, "volume": kw.get("volume"), "sets": kw.get("sets")}

    # An invented evening of the usual shape: warm-up, stretch and main
    # workout on Tonal, and the watch recording all of it as one activity.
    s = dashboard.merge_sessions([
        rec("tonal", "18:01", 10, volume=0, sets=0), rec("tonal", "18:13", 15, volume=0, sets=0),
        rec("tonal", "18:30", 55, volume=9800, sets=28), rec("garmin", "18:03", 85)])
    check("three Tonal records and the watch's copy are one session",
          len(s) == 1 and s[0]["kind"] == "tonal" and len(s[0]["records"]) == 4, str(len(s)))
    s = dashboard.merge_sessions([rec("tonal", "12:30", 10, volume=500, sets=2),
                                  rec("tonal", "18:30", 90, volume=15000, sets=30)])
    check("sessions hours apart stay separate", len(s) == 2, str(len(s)))
    check("a watch-only strength session counts, marked Garmin",
          [x["kind"] for x in dashboard.merge_sessions([rec("garmin", "18:00", 50)])] == ["garmin"])
    check("a stretch on its own is not a strength session",
          [x["kind"] for x in dashboard.merge_sessions(
              [rec("tonal", "08:00", 10, volume=0, sets=0)])] == [None])
    check("an untimed record joins its day's session instead of adding one",
          len(dashboard.merge_sessions([rec("tonal", None, 60, volume=9000, sets=20),
                                        rec("garmin", "18:00", 60)])) == 1)

    recs = [{"src": "garmin", "day": date(2026, 1, 7), "start": datetime(2026, 1, 7, 18),
             "dur_s": 2940, "title": "Strength"},
            {"src": "tonal", "day": date(2026, 3, 4), "start": datetime(2026, 3, 4, 18),
             "dur_s": 3600, "title": "Back", "volume": 15000.0, "sets": 24}]
    wk = dashboard.training_buckets(dashboard.merge_sessions(recs), recs,
                                    date(2026, 1, 5), date(2026, 3, 8))
    check("a gym week counts its session but has no volume, not zero volume",
          wk["garmin"][0] == 1 and wk["volume"][0] is None, str(wk))
    check("Tonal volume lands only on Tonal weeks, as the records carry it",
          wk["tonal"][8] == 1 and wk["volume"][8] == 15000.0)
    check("an empty week is a volume gap and zero sessions",
          wk["volume"][4] is None and wk["tonal"][4] == 0 and wk["garmin"][4] == 0)
    dy = dashboard.training_buckets(dashboard.merge_sessions(recs), recs,
                                    date(2026, 3, 1), date(2026, 3, 8), days=1)
    check("daily buckets put a session on its own day; a rest day is 0 sessions, no volume",
          dy["start"] == "2026-03-01" and len(dy["tonal"]) == 8
          and dy["tonal"][3] == 1 and dy["volume"][3] == 15000.0
          and dy["tonal"][2] == 0 and dy["volume"][2] is None, str(dy))

    wm = dashboard.weekly_mean({date(2026, 9, 1): 7.0, date(2026, 9, 2): 8.0,
                                date(2026, 9, 16): 6.0})
    check("weekly means start on a Monday, average only the days present, "
          "and leave an empty week a gap",
          wm == {"start": "2026-08-31", "days": 7, "values": [7.5, None, 6.0], "n": [2, 0, 1]}, str(wm))

    d = dashboard.dense({date(2026, 9, 1): 7.0, date(2026, 9, 3): 6.5})
    check("a missing day stays a gap, never an interpolated value",
          d["values"] == [7.0, None, 6.5], str(d))

    pts = {date(2026, 9, 30) - timedelta(days=k): 7.0 + (k % 2) for k in range(1, 15)}
    check("no baseline from fewer than half of the 30 days",
          dashboard.baseline(pts, date(2026, 9, 30)) is None)
    pts[date(2026, 9, 15)] = 7.5
    pts[date(2026, 9, 30)] = 99.0
    b = dashboard.baseline(pts, date(2026, 9, 30))
    check("baseline is the 30 days before, never the day itself",
          b is not None and b["n"] == 15 and b["median"] == 7.5, str(b))
    check("a value far outside its own spread moves",
          dashboard.compare(pts, date(2026, 9, 30))["z"] >= dashboard.MOVED_SD)
    pts[date(2026, 9, 30)] = 7.6
    check("a value inside it does not",
          abs(dashboard.compare(pts, date(2026, 9, 30))["z"]) < dashboard.MOVED_SD)
    zrow = lambda z: {"key": "rhr", "label": "Resting HR", "unit": "bpm", "value": 60,
                      "mean": 55, "z": z}
    check("what moved takes 2 SD either way, so on most days it is empty",
          [m["z"] for m in dashboard.moved([zrow(1.9), zrow(-2.1), zrow(2.0), zrow(-1.99)])]
          == [-2.1, 2.0])
    check("a moved reading keeps its own date, since the page shows it above every section",
          dashboard.moved([{**zrow(2.5), "day": "2026-09-30"}])[0]["day"] == "2026-09-30")

    print("\nDashboard - Trends lanes scale to the window, robust to a bad reading")
    la = dashboard.lane_axis
    fat = [24.0 + 0.1 * (i % 7) for i in range(29)] + [55.0]
    a = la(fat)
    check("one 55% among thirty readings near 24% doesn't stretch the lane; it's counted off-scale",
          a["hi"] < 30 and a["lo"] <= 24.0 and a["out"] == 1, str(a))
    sleep = [round(6.0 + 0.1 * i, 1) for i in range(28)] + [5.6, 9.3]
    a = la(sleep)
    check("a month's shortest and best nights are trimmed, then taken back: nothing pushed off the edge",
          a["lo"] <= 5.6 and a["hi"] >= 9.3 and a["out"] == 0, str(a))
    a = la([0.4, 0.5, 0.6, 0.5, 0.45])
    check("a lane never pads below zero for a measure that can't go there", a["lo"] == 0.0, str(a))
    a = la([241.3, 244.9, 247.2, 250.6, 249.0, 246.1])
    check("a lane far from zero keeps its own bottom, rounded out, which the page labels and marks broken",
          a["lo"] == 240.0 and a["hi"] == 252.0 and a["out"] == 0 and a["lo"] > 0, str(a))
    check("an empty window has no lane", la([]) is None)
    era = {date(2022, 8, 1) + timedelta(days=i): 355.0 for i in range(30)}
    era.update({date(2026, 9, 1) + timedelta(days=i): 245.0 + 0.2 * (i % 5) for i in range(30)})
    ax = dashboard.lane_axes(era, date(2026, 9, 30))
    check("each window is scaled to its own days: 30d to this month, All to the whole arc",
          set(ax) == set(dashboard.WINDOWS) and ax["30d"]["hi"] < 250 and ax["All"]["hi"] >= 355,
          str({k: (v["lo"], v["hi"]) for k, v in ax.items()}))
    wins = re.search(r"const WINS = (\[.*?\]);", dashboard.TEMPLATE.read_text()).group(1)
    check("the page's window buttons are the windows the lanes are scaled for",
          [[k, d] for k, d in dashboard.WINDOWS.items()] == json.loads(wins), wins)

    print("\nDashboard - Today's distance-from-median bars are capped")
    dev = dashboard.deviation
    half = dev({"value": 6.25, "median": 7.0, "sd": 0.5})
    check("half the track is 3 SD: 1.5 SD below the median sits halfway to the left end",
          half == {"x": -0.5, "clipped": False}, str(half))
    far = dev({"value": 9.0, "median": 7.0, "sd": 0.5})
    check("4 SD out stops at the end of the track and says it was clipped",
          far == {"x": 1.0, "clipped": True}, str(far))
    check("no bar without a reading, a median or a spread to scale by",
          dev({"value": None, "median": 7, "sd": 1}) is None
          and dev({"value": 7, "median": None, "sd": None}) is None
          and dev({"value": 7, "median": 7, "sd": 0.0}) is None)

    check("GarminDB's naive start time is taken as local wall-clock time",
          dashboard._local("2026-10-08 18:47:33.000000", TZ) == datetime(2026, 10, 8, 18, 47, 33))
    check("a start time with Z is converted to local",
          dashboard._local("2026-09-03T23:10:00Z", TZ) == datetime(2026, 9, 3, 18, 10))

    conn.execute("INSERT INTO garmin_daily (day, sleep_total_s, rhr, loaded_utc) "
                 "VALUES ('2026-08-31', 0, 55, 'test')")
    conn.execute("UPDATE tonal_workout SET title = '</script><b>x' WHERE workout_id = 'w0'")
    conn.commit()
    g = dashboard.garmin_series(conn)
    check("Garmin's 0 s sleep is a missing night, not a 0 h night",
          date(2026, 8, 31) not in g["sleep_h"] and g["rhr"].get(date(2026, 8, 31)) == 55)

    d2, d1 = date(2026, 9, 2), date(2026, 9, 1)
    nrd = dashboard.night_row_days
    check("a sleep-only day (the catch-up's) takes resting HR and body battery from the day before",
          nrd({"sleep_h": {d2: 7}, "sleep_score": {d2: 80}, "rhr": {d1: 55},
               "bb_min": {d1: 20}, "bb_max": {d1: 80}}, d2)
          == {"sleep_h": d2, "sleep_score": d2, "rhr": d1, "bb_min": d1, "bb_max": d1})
    check("a full day missing resting HR keeps it missing on its own day",
          set(nrd({"sleep_h": {d2: 7}, "sleep_score": {}, "rhr": {d1: 55},
                   "bb_min": {d2: 20}, "bb_max": {d2: 80}}, d2).values()) == {d2})

    p = dashboard.build_payload(conn, cfg, today=date(2026, 9, 2))
    dv = p["daily"]
    check("last night is the Garmin row dated this morning", dv["night"]["day"] == "2026-09-02",
          str(dv["night"]["day"]))
    check("too little history means no median, not a made-up one",
          all(r["median"] is None for r in dv["night"]["rows"]))
    check("nothing moves without a baseline: the empty answer", dv["moved"] == [])
    check("and the page is told nothing could be checked, so it doesn't claim 'nothing moved'",
          dv["moved_checked"] == 0)
    st = dv["strip"]
    by = {x["day"]: x for x in st["nights"]}
    check("the sleep strip is the 14 nights ending today, oldest first",
          len(st["nights"]) == dashboard.STRIP_NIGHTS and st["nights"][-1]["day"] == "2026-09-02"
          and st["nights"][0]["day"] == "2026-08-20", str(st["nights"][0]))
    check("a night with no record is a gap, not a zero bar - Garmin's 0 s night included",
          by["2026-08-20"]["h"] is None and by["2026-08-31"]["h"] is None, str(by["2026-08-31"]))
    check("the strip picks out the night Today describes, and marks yesterday's session",
          st["latest"] == "2026-09-02" and by["2026-09-01"]["trained"]
          and not by["2026-08-25"]["trained"], str(st["latest"]))
    check("strip hours are the same hours the Recovery chart draws",
          by["2026-09-02"]["h"] == p["series"]["sleep_h"]["values"][-1]
          and st["hi_h"] == max(x["h"] for x in st["nights"] if x["h"] is not None))
    check("with no median there is no line, so the bars start from zero",
          st["median_h"] is None and st["lo_h"] == 0.0, str({k: st[k] for k in ("median_h", "lo_h")}))

    tr = dv["training"]
    check("yesterday's Tonal session carries its volume and sets",
          tr["day"] == "2026-09-01" and len(tr["tonal"]) == 1
          and tr["tonal"][0]["volume"] == 11000 and tr["tonal"][0]["sets"] == 2, str(tr))
    check("this morning's weigh-in, at its local time",
          dv["morning"]["day"] == "2026-09-02" and dv["morning"]["time"] == "07:00",
          str(dv["morning"]))

    print("\nDashboard - the sleep strip: a median line, and bars from a floor under it")
    snap = db.connect(brief_snapshot(root / "strip-ro.db"), read_only=True)
    ps = dashboard.build_payload(snap, cfg, today=BRIEF_DAY)
    snap.close()
    st2 = ps["daily"]["strip"]
    by2 = {x["day"]: x for x in st2["nights"]}
    sleep_row = next(r for r in ps["daily"]["night"]["rows"] if r["key"] == "sleep_h")
    check("the line is the 30-day median Today shows beside last night's sleep",
          st2["median_h"] == sleep_row["median"] == 7.0, f"{st2['median_h']} {sleep_row['median']}")
    check("with it, the bars start under the usual nights, not at zero: 5.0 h for 5.9 to 7.5 h",
          st2["lo_h"] == 5.0 and st2["hi_h"] == 7.5 and not any(x["below"] for x in st2["nights"]),
          str({k: st2[k] for k in ("lo_h", "hi_h")}))
    check("a dash sits under the night dated the session's day: the night that ended "
          "that morning, before the session",
          ps["daily"]["training"]["day"] == "2026-09-29" and by2["2026-09-29"]["trained"]
          and not by2["2026-09-30"]["trained"] and not by2["2026-09-28"]["trained"])
    fortnight = {date(2026, 8, 10) + timedelta(days=i): h for i, h in enumerate(
        [8.9, 7.1, 6.6, 6.8, 7.3, 3.5, 7.7, 7.2, 8.4, 7.0, 7.2, 6.5, 7.9])}    # invented
    fs = dashboard.sleep_strip(fortnight, {}, date(2026, 8, 23), date(2026, 8, 22), median=7.2)
    check("one 3.5-hour night doesn't drag the floor to zero: it sits below the floor, flagged",
          fs["lo_h"] == 5.0 and [x["day"] for x in fs["nights"] if x["below"]] == ["2026-08-15"],
          str(fs["lo_h"]))
    ax = dashboard.strip_axis([5.9, 7.0, 7.4, 7.8, 7.2, 7.6, 7.1, 7.3], 7.3)
    check("an ordinary short night, 1.4 h under the median, is still a bar",
          ax["lo"] < 5.9, str(ax))
    ax = dashboard.strip_axis([7.1, 7.3, 7.2, 7.4, 7.0, 7.5, 7.2], 7.25)
    check("a quiet fortnight's half-hour spread isn't blown up to fill the strip",
          ax == {"lo": 6.5, "hi": 7.5}, str(ax))
    check("no nights, no axis", dashboard.strip_axis([None, None], 7.0) is None)

    print("\nDashboard - one units setting for every mass, converted at display only")
    lb = 1 / dashboard.KG_PER_LB
    check("units default to imperial", cfg.units == "imperial" and p["units"]["mass"] == "lb")
    wrow = dv["morning"]["rows"][0]
    check("the stored 94.8 kg weigh-in is shown as 209.0 lb",
          wrow["unit"] == "lb" and abs(wrow["value"] - 94.8 * lb) < 1e-3, str(wrow))
    check("the weight series is in lb too",
          abs(p["series"]["weight"]["values"][-1] - 94.8 * lb) < 1e-3)
    # fixture: right arm 3.13 kg, left arm 3.23 kg
    check("segmental right-minus-left follows the same unit",
          abs(p["series"]["arms"]["values"][0] - (3.13 - 3.23) * lb) < 1e-3,
          str(p["series"]["arms"]))
    check("Tonal volume stays in lb as Tonal reported it",
          tr["tonal"][0]["volume"] == 11000)
    check("storage is untouched: Withings weight is still kg in the database",
          abs(conn.execute("SELECT weight_kg FROM v_body_composition").fetchone()[0] - 94.8) < 1e-9)
    cfg.units = "metric"
    pm = dashboard.build_payload(conn, cfg, today=date(2026, 9, 2))
    cfg.units = "imperial"
    check("metric shows the weigh-in in kg, unconverted",
          pm["units"]["mass"] == "kg" and pm["daily"]["morning"]["rows"][0]["unit"] == "kg"
          and abs(pm["daily"]["morning"]["rows"][0]["value"] - 94.8) < 1e-9)
    check("metric shows the arms difference in kg",
          abs(pm["series"]["arms"]["values"][0] - (3.13 - 3.23)) < 1e-9)
    check("metric converts Tonal volume too, so no screen mixes lb with kg",
          abs(pm["daily"]["training"]["tonal"][0]["volume"] - 11000 * dashboard.KG_PER_LB) < 0.1,
          str(pm["daily"]["training"]["tonal"][0]["volume"]))
    bad = root / "bad-units.toml"
    bad.write_text(cfg_path.read_text() + '\n[display]\nunits = "stone"\n')
    try:
        config.load(bad)
        refused = False
    except SystemExit:
        refused = True
    check("an unknown units value is refused, not silently defaulted", refused)

    print("\nDashboard - the balance caption claims a direction only when the data does")
    d0 = date(2026, 1, 1)
    def series(f, n=120):
        return {d0 + timedelta(days=i): f(i) for i in range(n)}
    steady = dashboard.balance_stats(series(lambda i: 0.2 + (0.05 if i % 2 else -0.05)))
    check("a gap bigger than the wobble, same side every month, leads right",
          steady["lead"] == "right" and steady["months_positive"] == steady["months"] == 4,
          str(steady))
    left = dashboard.balance_stats(series(lambda i: -0.2 + (0.05 if i % 2 else -0.05)))
    check("the same gap the other way leads left", left["lead"] == "left", str(left))
    # Right ahead on average, but April's mean goes negative.
    flipped = dashboard.balance_stats(series(lambda i: -0.3 if i >= 90 else 0.3))
    check("one month on the other side means no steady direction",
          flipped["lead"] is None and flipped["months_positive"] == 3, str(flipped))
    noisy = dashboard.balance_stats(series(lambda i: 0.05 + (0.3 if i % 2 else -0.3)))
    check("a gap smaller than the day-to-day wobble means no steady direction",
          noisy["lead"] is None and noisy["months_positive"] == noisy["months"], str(noisy))
    check("the page gets daily and weekly training buckets, and weekly means",
          p["training"]["weekly"]["days"] == 7 and p["training"]["daily"]["days"] == 1
          and p["weekly"]["weight"]["n"][-1] == 1)
    check("each lane has a range for every window, and All covers its readings here",
          all(set(p["lanes"][k]) == set(dashboard.WINDOWS) for k in ("sleep_h", "rhr", "weight", "fat_pct"))
          and all(p["lanes"][k]["All"]["out"] == 0 for k in ("sleep_h", "rhr") if p["lanes"][k]["All"]),
          str(p["lanes"]["rhr"]))
    check("health carries the guard's staleness verdict for every source",
          [x["source"] for x in p["health"]["sources"]] == list(dashboard.SOURCES)
          and all(isinstance(x["stale"], bool) for x in p["health"]["sources"]))
    check("an annotation with no label of its own is drawn with a generic one",
          p["annotations"] and all(a["label"] == f"no {a['source']} data expected"
                                   for a in p["annotations"]), str(p["annotations"]))

    def embedded(page: str) -> dict:
        blob = page.split('<script id="data" type="application/json">', 1)[1]
        return json.loads(blob.split("</script>", 1)[0])

    page = dashboard.render(p)
    dark = page.split("@media (prefers-color-scheme: dark)", 1)[1].split("}\n}", 1)[0]
    check("the dark theme is the navy palette; the light one keeps its own",
          "--page: #0B1220" in dark and "--card: #121D2E" in dark
          and "--page: #fcfcfb" in page.split("@media", 1)[0])
    check("the page leads Today with what moved, under four tab pills",
          '<nav class="tabs">' in page and page.count('data-view="') == 4
          and page.index("renderMoved(root, v);") < page.index('block(root, nd === TODAY'))
    trends_view = page.split('id="view-trends"', 1)[1].split("</div>", 1)[0]
    scan_view = page.split('id="view-bodyscan"', 1)[1].split("</div>", 1)[0]
    check("the Trends tab is one card of lanes, scrubbed by touch",
          'id="p-lanes"' in trends_view and 'id="p-balance"' not in trends_view
          and 'id="p-recovery"' not in page
          and "touch-action: pan-y" in page.split(".lanes {", 1)[1].split("}", 1)[0])
    check("Body Scan: the map, what changed, one axis, region detail, then muscle balance",
          [x for x in re.findall(r'id="(p-[a-z]+)"', scan_view)]
          == ["p-map", "p-change", "p-compare", "p-detail", "p-balance"], scan_view)
    check("Body Scan labels its bone as derived and says why muscle % isn't drawn",
          "Bone is derived, not measured" in page and '"bone*"' in page
          and "a muscle % line would be this one upside down" in page)
    check("each tab opens from its own link, on load and when the link changes",
          "location.hash.slice(1)" in page and 'addEventListener("hashchange"' in page
          and all(f'data-view="{v}"' in page for v in ("today", "trends", "tonal", "bodyscan")))
    check("the page takes its ramp from the payload: no colour of it is typed into the page",
          not any(c_.lower() in dashboard.TEMPLATE.read_text().lower() for _, c_ in dashboard.FAT_RAMP)
          and p["bodyscan"]["ramp"]["anchors"][-1] == [30.0, "#A46634"])
    fig = {k: int(x) for k, x in re.findall(r"(arm_right|arm_left|leg_right|leg_left): \{ x: (\d+)", page)}
    check("the body map puts your right limbs on the right and your left on the left",
          fig["arm_right"] > fig["arm_left"] and fig["leg_right"] > fig["leg_left"]
          and '[[35, "L"], [165, "R"], [79, "L"], [120, "R"]]' in page, str(fig))
    check("tapping a region on the map opens it in the region detail",
          "selectRegion(key, true)" in page and 'scrollIntoView' in page)
    check("the payload carries the Body Scan tab's regions and map",
          [x["key"] for x in p["bodyscan"]["regions"]][0] == "whole" and "latest" in p["bodyscan"])

    # Body Scan's two colour scales: the map's fat ramp, and each region's
    # identity colour everywhere else (theme tokens --rg-<key>).
    def tokens(css: str) -> dict:
        return dict(re.findall(r"--([a-z0-9_-]+): ([^;]+);", css))

    theme = {"light": tokens(page.split("@media", 1)[0])}
    theme["dark"] = {**theme["light"], **tokens(dark)}

    def tok(t: str, name: str) -> str:
        v = theme[t][name].strip()
        return tok(t, v[6:-1]) if v.startswith("var(--") else v

    def lum(c: str) -> float:
        x = [int(c[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        x = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in x]
        return 0.2126 * x[0] + 0.7152 * x[1] + 0.0722 * x[2]

    def contrast(a: str, b: str) -> float:
        hi, lo = sorted((lum(a), lum(b)), reverse=True)
        return (hi + 0.05) / (lo + 0.05)

    def lstar(c: str) -> float:   # CIE L*: lightness as greyscale shows it
        y = lum(c)
        return 116 * y ** (1 / 3) - 16 if y > 0.008856 else 903.3 * y

    keys = [k for k, _ in dashboard.REGIONS]
    rg = {t: {k: tok(t, f"rg-{k}") for k in keys} for t in theme}
    check("Body Scan's region colours on the dark theme are the ones specified",
          {k: v for k, v in tokens(dark).items() if k.startswith("rg-") and "ink" not in k}
          == {"rg-whole": "#EAF0F7", "rg-torso": "#D98A4F", "rg-arm_left": "#A8CBF0",
              "rg-arm_right": "#5B8FD0", "rg-leg_left": "#8FD9C4", "rg-leg_right": "#3C9B84"},
          str(rg["dark"]))
    check("each theme gives the six regions six colours, none of them a ramp colour",
          all(len(set(rg[t].values())) == 6
              and not {c_.upper() for _, c_ in dashboard.FAT_RAMP} & {v.upper() for v in rg[t].values()}
              for t in theme), str(rg))
    check("in a pair the left side is the lighter shade, on both themes, enough to read in greyscale",
          all(lstar(rg[t][f"{s}_left"]) - lstar(rg[t][f"{s}_right"]) >= 15
              for t in theme for s in ("arm", "leg")),
          str({t: [round(lstar(rg[t][k]), 1) for k in keys] for t in theme}))
    check("every region colour stands 3:1 off its card, and the text on it 4.5:1",
          all(contrast(rg[t][k], tok(t, "card")) >= 3
              and contrast(rg[t][k], tok(t, f"rg-ink-{k}")) >= 4.5 for t in theme for k in keys),
          str({t: [round(contrast(rg[t][k], tok(t, "card")), 2) for k in keys] for t in theme}))
    tpl = dashboard.TEMPLATE.read_text()
    in_map = tpl.split("function renderMap(", 1)[1].split("\nfunction ", 1)[0]
    off_map = tpl.replace(in_map, "")
    check("the fat ramp stays inside the map: nothing else reads a region's ramp shade",
          bool(re.search(r"\.(colour|ink)\b", in_map)) and "rampCss()" in in_map
          and not re.search(r"\.(colour|ink)\b", off_map) and "rampCss()" not in off_map)
    check("a region is a series in its identity colour: lines, dots, legends, ranked bars, detail",
          "{stroke:var(--rg-${k})}" in tpl and "{fill:var(--rg-${k})}" in tpl
          and '"rbar rg-" + r.key' in tpl and "`fa rg-${key}`" in tpl
          and "var(--rg-whole) 0 5px" in tpl)
    change_js = tpl.split("function renderChange(", 1)[1].split("\nfunction ", 1)[0]
    check("the region key says lighter is left once, in the first section that uses the colours",
          tpl.lower().count("lighter is left") == 1 and "lighter is left" in change_js
          and "const key = legend(panel, BSR.map(" in change_js)
    check("a lane draws an off-scale reading on its edge instead of stretching",
          "clip: true" in page and "v > this.hi || v < this.lo" in page)
    check("the strip's caption names sleep, says what the dash and the highlight mean, "
          "and never says just 'trained'",
          "`Sleep, last ${n} nights · `" in page and '" night before a strength session"' in page
          and "highlighted: the night the Today tab shows" in page
          and '"line: your 30-day median"' in page and '" trained"' not in page)
    check("the median line is drawn only when the payload has a median",
          "if (S.median_h != null) {" in page and 'class: "smed"' in page)
    check("Today shows each night reading's median beside it, not only on hover",
          'h("span", "med", `med ${fix(r.median' in page)
    check("a Today row from another day than the night's names that day, on screen and aloud",
          "r.day !== v.night.day ? fmtDay(dn(r.day))" in page
          and "devRow(night, by[k], dayText, own(by[k]))" in page
          and "own(by.bb_min) || own(by.bb_max)" in page
          and "`${r.label} · ${own}`" in page and "`Body battery · ${own}`" in page
          and page.count("${own || dayText}.") == 2)
    check("a workout title can't close the data block early",
          page.count("</script>") == dashboard.TEMPLATE.read_text().count("</script>"))
    check("and it survives the round trip intact",
          embedded(page)["daily"]["training"]["tonal"][0]["title"] == "</script><b>x")

    sync_all.cmd_snapshot(conn, cfg)
    conn.execute("UPDATE garmin_daily SET rhr = 99 WHERE day = '2026-09-02'")
    conn.commit()
    out = dashboard.build(cfg, today=date(2026, 9, 2), out=root / "dash" / "index.html")
    rhr = next(r["value"] for r in embedded(out.read_text())["daily"]["night"]["rows"]
               if r["key"] == "rhr")
    check("the page is built from the snapshot, not the live database", rhr != 99, str(rhr))
    check("written whole, with no temp file left beside it",
          not list(out.parent.glob("*.tmp")))
    applied = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
    with contextlib.redirect_stdout(io.StringIO()):
        rc = sync_all.main(["--config", str(cfg_path), "dashboard", str(out)])
    check("`sync_all.py dashboard` never opens the live database",
          rc == 0 and conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == applied)

    test_tonal_sets(root, cfg, cfg_path)
    test_tonal_migration(root, cfg)
    test_tonal_tab(root, cfg)
    test_body_scan(root, cfg)
    test_brief(root, cfg, cfg_path)
    test_ask(root, cfg, cfg_path)
    test_sleep_catchup(root)
    test_leakcheck(root)
    test_catalog_fetch(root)
    test_demo(root)

    conn.close()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print(f"  FAILED: {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
