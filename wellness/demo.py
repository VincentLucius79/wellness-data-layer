"""Demo mode: an invented person's data, loaded by the real pipeline.

    python3 sync_all.py demo                 # -> ./demo/dashboard/index.html
    python3 sync_all.py demo --out DIR

Nothing here comes from anyone's real data. Every figure is either taken
from a published reference, cited where it is used, or invented and labelled
so. Don't tune a constant toward something measured: the point of the demo
is that its screenshots can't be read as anyone's results.

The generator writes each source in its own format: GarminDB-shaped SQLite
files, a toneget-format Tonal export, and Withings `getmeas` groups.
sync_all.cmd_demo then runs the real loaders, guard, snapshot and dashboard
on them, so the invariants are enforced by the code that enforces them on
real data, not promised here. A source that comes back anything but a clean
`ok` fails the demo.

The demo never reads config.toml's data, a secret or the network. It writes
only to its own folder, and refuses one that is, holds, or sits inside a
real data folder.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import shutil
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO / "demo"
MARKER = ".wellness-demo"
TZ_NAME = "America/New_York"       # not the author's; it has DST, so tz handling is exercised
SEED = 4242
KG_PER_LB = 0.45359237
NS = uuid.UUID("6f0f8a52-4c55-4a63-9c39-0d6c2f6f5d00")   # for the demo's stable ids

# ----------------------------------------------------------- the timeline
# Days before `today` each source starts. Invented, round.
WITHINGS_DAYS = 6 * 365            # weight and fat %
MUSCLE_DAYS = 5 * 365              # muscle and bone mass from a newer scale
SEGMENT_DAYS = 548                 # BodyScan segments: the last 18 months
ALGO_CHANGE_DAYS = 270             # a Withings algorithm update, inside the segment era
GARMIN_DAYS = 5 * 365
TONAL_DAYS = 913                   # 30 months
TONAL_GAP = (420, 371)             # 7 weeks away from Tonal, annotated
BAD_READING_DAYS = 3 * 365 + 40    # one internally consistent, implausible fat % reading

# -------------------------------------------------------------- the person
# Invented: 1.78 m. Weight eases from 84 to 78 kg over six years, with a
# regain in the middle and a small winter rise. That is BMI 26.5 to 24.6, the
# WHO's "pre-obese" band into "normal".
WEIGHT_START_KG, WEIGHT_END_KG = 84.0, 78.0
WEIGHT_HUMP_KG = 2.0
WEIGHT_WINTER_KG = 0.6
FAT_START_PCT, FAT_END_PCT = 25.0, 20.0

# Hard limits, asserted after generation (check_limits). A value outside one
# is a generator bug, never clipped quietly into range.
LIMITS = {
    "weight_kg": (70.0, 95.0), "fat_pct": (12.0, 32.0),
    "sleep_h": (4.5, 9.5), "sleep_score": (40, 100), "rhr": (45, 75),
    "bb": (0, 100), "steps": (1000, 25000), "stress": (0, 100),
    "tonal_lb_per_arm": (0.0, 100.0),
}

# Segment shares of body mass, from Dempster's cadaver data as tabulated in
# Winter, D.A., Biomechanics and Motor Control of Human Movement, 4th ed.
# (Wiley, 2009), Table 4.1: trunk 0.497, head and neck 0.081, each total arm
# 0.050, each total leg 0.161. Withings' five segments add up to the whole
# body, so the head and neck go with the trunk here. These are shares of
# total mass, not of fat-free mass; the fat in each segment is set below.
# Don't replace them with a reading from any real scan.
SEGMENT_SHARE = {"torso": 0.497 + 0.081, "arm_left": 0.050, "arm_right": 0.050,
                 "leg_left": 0.161, "leg_right": 0.161}
# Invented: the LEFT arm is the dominant one, with about 3% more mass than
# the right. The legs are matched. So the arms' balance caption has a lead
# to report and the legs' doesn't.
ARM_DOMINANCE = 0.015
# Invented, round: how fat each segment is relative to the whole body
# (renormalised so the segments' fat adds up to the whole body's).
SEGMENT_FAT = {"torso": 0.95, "arm_left": 1.05, "arm_right": 1.05,
               "leg_left": 1.10, "leg_right": 1.10}
# Invented, round: bone is 5% of fat-free mass, the rest is muscle, in every
# segment. Withings reports bone for the whole body only (88), and there it
# is fat-free minus muscle, which the guard checks.
BONE_SHARE = 0.05
# Withings positions: 2 right arm, 3 left arm, 10 left leg, 11 right leg,
# 12 torso, 7 whole body (withings_position).
POSITION = {"arm_right": 2, "arm_left": 3, "leg_left": 10, "leg_right": 11, "torso": 12}
ALGO_OLD, ALGO_A, ALGO_B = 2000100, 3000100, 3000200   # invented version numbers
LEG_ALGO_SHIFT = 0.03           # the update reads legs 3 points fatter, overnight

# ----------------------------------------------------------- the movements
# Invented names by default. When reference/tonal-movements.json has been
# fetched (sync_all.py fetch-catalog), each slot takes a real movement of the
# same kind from it instead.


@dataclass(frozen=True)
class Slot:
    key: str
    name: str
    muscle: str
    region: str
    two_sided: bool
    arms_together: bool | None     # None off the machine
    on_machine: bool
    base_lb: float                 # per arm where arms_together; invented
    # A rope hangs from one arm, so a rope move is one-armed on the machine
    # however many hands hold it: its weight is that arm's whole load.
    reps: int
    accessory: str | None


SLOTS = {s.key: s for s in (
    Slot("bench_press", "Bench Press", "Chest", "UpperBody", False, True, True, 32, 10, "Smart Bar"),
    Slot("overhead_press", "Overhead Press", "Shoulders", "UpperBody", False, True, True, 22, 10, "Smart Bar"),
    Slot("one_arm_press", "Single-Arm Chest Press", "Chest", "UpperBody", True, False, True, 28, 10, "Handles"),
    Slot("triceps_ext", "Triceps Extension", "Triceps", "UpperBody", False, False, True, 30, 12, "Rope"),
    Slot("lateral_raise", "Lateral Raise", "Shoulders", "UpperBody", True, False, True, 9, 12, "Handles"),
    Slot("push_up", "Push-Up", "Chest", "UpperBody", False, None, False, 0, 12, None),
    Slot("seated_row", "Seated Row", "Back", "UpperBody", False, True, True, 36, 10, "Smart Bar"),
    Slot("lat_pulldown", "Lat Pulldown", "Back", "UpperBody", False, True, True, 40, 10, "Smart Bar"),
    Slot("one_arm_row", "Single-Arm Row", "Back", "UpperBody", True, False, True, 32, 10, "Handles"),
    Slot("biceps_curl", "Biceps Curl", "Biceps", "UpperBody", False, True, True, 18, 12, "Smart Bar"),
    Slot("face_pull", "Face Pull", "Shoulders", "UpperBody", False, False, True, 28, 12, "Rope"),
    Slot("hammer_curl", "Hammer Curl", "Biceps", "UpperBody", True, False, True, 16, 12, "Handles"),
    Slot("deadlift", "Deadlift", "Hamstrings", "LowerBody", False, True, True, 62, 8, "Smart Bar"),
    Slot("goblet_squat", "Goblet Squat", "Quads", "LowerBody", False, True, True, 45, 10, "Handles"),
    Slot("rdl", "Romanian Deadlift", "Hamstrings", "LowerBody", False, True, True, 55, 10, "Smart Bar"),
    Slot("reverse_lunge", "Reverse Lunge", "Glutes", "LowerBody", True, True, True, 30, 10, "Handles"),
    Slot("split_squat", "Split Squat", "Quads", "LowerBody", True, True, True, 28, 10, "Handles"),
    Slot("calf_raise", "Calf Raise", "Calves", "LowerBody", False, True, True, 50, 15, "Smart Bar"),
    Slot("pallof_press", "Pallof Press", "Obliques", "Core", True, False, True, 15, 12, "Handles"),
    Slot("kneeling_crunch", "Kneeling Crunch", "Abs", "Core", False, False, True, 40, 12, "Rope"),
    Slot("woodchop", "Woodchop", "Obliques", "Core", True, False, True, 16, 12, "Handles"),
)}

# Routines: template id -> (title, workoutType, slot keys). Invented titles.
ROUTINES = {
    "demo-upper-push": ("Upper Push", "PROGRAM",
                        ["bench_press", "overhead_press", "one_arm_press", "triceps_ext",
                         "lateral_raise", "push_up"]),
    "demo-upper-pull": ("Upper Pull", "PROGRAM",
                        ["seated_row", "lat_pulldown", "one_arm_row", "biceps_curl",
                         "face_pull", "hammer_curl"]),
    "demo-lower": ("Lower Body", "PROGRAM",
                   ["deadlift", "goblet_squat", "rdl", "reverse_lunge", "split_squat",
                    "calf_raise", "kneeling_crunch"]),
    "demo-full": ("Full Body", "PROGRAM",
                  ["goblet_squat", "one_arm_row", "bench_press", "reverse_lunge",
                   "pallof_press", "woodchop"]),
    "demo-arms": ("Arms Finisher", "Custom",
                  ["biceps_curl", "triceps_ext", "hammer_curl", "lateral_raise"]),
}
# Weekday -> (template, local start hour, how often it happens). Invented.
WEEK = {0: ("demo-upper-push", 18.0, 0.88), 2: ("demo-lower", 18.0, 0.88),
        4: ("demo-upper-pull", 18.0, 0.85), 5: ("demo-full", 9.5, 0.5)}


def _sid(*parts) -> str:
    return str(uuid.uuid5(NS, "/".join(map(str, parts))))


def movement_id(slot: Slot) -> str:
    return _sid("movement", slot.key)


def invented_catalog() -> list[dict]:
    """The demo's own movement catalog, in the vendored catalog's shape."""
    return [_catalog_entry(s) for s in SLOTS.values()]


def _catalog_entry(s: Slot) -> dict:
    entry = {"id": movement_id(s), "name": s.name, "shortName": s.name,
             "muscleGroups": [s.muscle], "bodyRegion": s.region,
             "isTwoSided": s.two_sided, "isAlternating": False, "onMachine": s.on_machine,
             "skillLevel": 1, "active": True}
    if s.on_machine:
        entry["onMachineInfo"] = {"trainerArmsPulledAtSameTime": s.arms_together,
                                  "accessory": s.accessory}
    return entry


def pick_movements(catalog: list[dict] | None) -> tuple[dict[str, str], list[dict]]:
    """slot key -> movement id, and the catalog the demo loads. From the real
    catalog when there is one: for each slot, a movement on (or off) the
    machine like it, with the same sides and arms, chosen by a stable hash of
    the slot so every run picks the same. A slot with no such movement keeps
    its invented one."""
    ids: dict[str, str] = {}
    out: list[dict] = []
    used: set[str] = set()
    for s in SLOTS.values():
        choice = None
        if catalog:
            def fits(m, primary):
                info = m.get("onMachineInfo") if isinstance(m.get("onMachineInfo"), dict) else {}
                groups = m.get("muscleGroups") or []
                return (isinstance(m.get("id"), str) and m["id"] not in used
                        and m.get("active") is not False and m.get("name")
                        and bool(m.get("onMachine")) == s.on_machine
                        and bool(m.get("isTwoSided")) == s.two_sided
                        and (not s.on_machine
                             or bool(info.get("trainerArmsPulledAtSameTime")) == bool(s.arms_together))
                        and ((groups[:1] == [s.muscle]) if primary else s.muscle in groups))
            for primary in (True, False):
                pool = sorted((m for m in catalog if isinstance(m, dict) and fits(m, primary)),
                              key=lambda m: m["name"])
                if pool:
                    h = int(hashlib.sha256(s.key.encode()).hexdigest(), 16)
                    choice = pool[h % len(pool)]
                    break
        if choice is None:
            choice = _catalog_entry(s)
        used.add(choice["id"])
        ids[s.key] = choice["id"]
        out.append(choice)
    return ids, out


# -------------------------------------------------------------- body scans

def body(weight_kg: float, fat_pct: float, *, leg_shift: float = 0.0,
         noise: random.Random | None = None) -> dict:
    """One weigh-in's masses in kg: the whole body and the five segments,
    each {ffm, fat, muscle}, plus whole-body bone. The segments add up to the
    whole body exactly; rounding to Withings' 10 g happens in scan_group.
    `leg_shift` moves that share of each leg's mass from fat-free to fat, as
    an algorithm update does. `noise` adds scan-to-scan wobble."""
    def jitter(sd: float) -> float:
        return noise.gauss(0, sd) if noise else 0.0
    shares = {k: v * (1 + jitter(0.006)) for k, v in SEGMENT_SHARE.items()}
    shares["arm_left"] *= 1 + ARM_DOMINANCE
    shares["arm_right"] *= 1 - ARM_DOMINANCE
    total = sum(shares.values())
    mass = {k: weight_kg * v / total for k, v in shares.items()}
    fat_kg = weight_kg * fat_pct / 100
    weights = {k: mass[k] * SEGMENT_FAT[k] for k in mass}
    wsum = sum(weights.values())
    seg = {}
    for k in mass:
        fat = fat_kg * weights[k] / wsum
        if k.startswith("leg"):
            fat += leg_shift * mass[k]
        ffm = mass[k] - fat
        seg[k] = {"ffm": ffm, "fat": fat, "muscle": ffm * (1 - BONE_SHARE) * (1 + jitter(0.002))}
    whole = {t: sum(s[t] for s in seg.values()) for t in ("ffm", "fat", "muscle")}
    whole["weight"] = whole["ffm"] + whole["fat"]
    whole["bone"] = whole["ffm"] - whole["muscle"]
    return {"whole": whole, "seg": seg}


def scan_group(grpid: int, epoch: int, b: dict, *, segments: bool = True,
               muscle: bool = True, algo: int | None = ALGO_A, modelid: int = 10) -> dict:
    """A `getmeas` measure group for body(), shaped like the API's: weight
    in g (unit -3), masses in 10 g (unit -2), fat ratio in 0.001 %. With
    segments it is a BodyScan weigh-in: whole-body masses at position 7 and
    each segment's at its own position. Whole-body bone is fat-free minus
    muscle after rounding, as Withings reports it."""
    w = b["whole"]
    ffm, fat, mus = (round(w[t] * 100) for t in ("ffm", "fat", "muscle"))
    whole_pos = 7 if segments else None
    extra = {"algo": algo, "fm": 3} if algo is not None else {}

    def m(t, value, unit, pos=None):
        out = {"type": t, "value": value, "unit": unit, **extra}
        if pos is not None:
            out["position"] = pos
        return out

    ms = [m(1, round(w["weight"] * 1000), -3), m(6, round(100 * w["fat"] / w["weight"] * 1000), -3),
          m(5, ffm, -2, whole_pos), m(8, fat, -2, whole_pos)]
    if muscle:
        ms += [m(76, mus, -2, whole_pos), m(88, ffm - mus, -2, whole_pos)]
    if segments:
        for k, pos in POSITION.items():
            s = b["seg"][k]
            ms += [m(173, round(s["ffm"] * 100), -2, pos), m(174, round(s["fat"] * 100), -2, pos),
                   m(175, round(s["muscle"] * 100), -2, pos)]
    return {"grpid": grpid, "category": 1, "attrib": 0, "modelid": modelid,
            "date": epoch, "measures": ms}


# --------------------------------------------------------------- the data

@dataclass
class Dataset:
    today: date
    now: datetime
    garmin_days: list[dict] = field(default_factory=list)
    sleeps: list[dict] = field(default_factory=list)
    activities: list[dict] = field(default_factory=list)
    weights_lb: dict[str, float] = field(default_factory=dict)
    withings: list[dict] = field(default_factory=list)
    tonal: dict = field(default_factory=dict)
    catalog: list[dict] = field(default_factory=list)
    annotation: dict = field(default_factory=dict)
    # Every generated value LIMITS bounds, for check_limits.
    seen: dict[str, list[float]] = field(default_factory=dict)

    def saw(self, key: str, v: float) -> None:
        self.seen.setdefault(key, []).append(v)


def _hms(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _gdt(dt: datetime) -> str:
    """GarminDB's datetime text: naive local wall-clock time."""
    return dt.strftime("%Y-%m-%d %H:%M:%S.000000")


def _zulu(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _at(d: date, hours: float, tz: ZoneInfo) -> datetime:
    """Local wall-clock time on day d, `hours` after midnight (may pass 24)."""
    return datetime.combine(d, time(0), tzinfo=tz) + timedelta(hours=hours)


def _spans(rng: random.Random, start: date, end: date, per_year: float,
           length: tuple[int, int], keep_clear: date) -> list[tuple[date, date]]:
    """A few random runs of days between start and end, none on or after
    keep_clear."""
    out = []
    years = max(1, round((end - start).days / 365 * per_year))
    for _ in range(years):
        d0 = start + timedelta(days=rng.randrange(max(1, (end - start).days)))
        d1 = d0 + timedelta(days=rng.randint(*length) - 1)
        if d1 < keep_clear:
            out.append((d0, d1))
    return out


def _within(d: date, spans) -> bool:
    return any(a <= d <= b for a, b in spans)


def generate(today: date, now: datetime | None = None, *, seed: int = SEED,
             catalog: list[dict] | None = None) -> Dataset:
    """The whole invented history, ending `now` (local) on `today`. Anything
    timed after `now` today isn't there yet. The same seed and day give the
    same data; another day gives the same shape of history ending there."""
    tz = ZoneInfo(TZ_NAME)
    now = now or datetime.combine(today, time(11, 0), tzinfo=tz)
    rng = random.Random(seed)
    ds = Dataset(today=today, now=now)
    ids, ds.catalog = pick_movements(catalog)

    w_start = today - timedelta(days=WITHINGS_DAYS)
    span = (today - w_start).days
    tonal_start = today - timedelta(days=TONAL_DAYS)
    gap = (today - timedelta(days=TONAL_GAP[0]), today - timedelta(days=TONAL_GAP[1]))
    ds.annotation = {"source": "tonal", "start_date": gap[0].isoformat(),
                     "end_date": gap[1].isoformat(),
                     "note": "Seven weeks away: trained at a gym, not on Tonal. "
                             "The watch logged those sessions.",
                     "recorded_by": "demo", "label": "trained elsewhere — volume not captured"}

    # ---- weight and fat %, one trend per day
    trend: dict[date, tuple[float, float]] = {}
    ar = 0.0
    for i in range(span + 1):
        d = w_start + timedelta(days=i)
        x = i / span
        hump = WEIGHT_HUMP_KG * math.exp(-((x - 0.5) / 0.12) ** 2)
        winter = WEIGHT_WINTER_KG * math.cos(2 * math.pi * (d.timetuple().tm_yday - 15) / 365.25)
        ar = 0.7 * ar + rng.gauss(0, 0.3)
        kg = WEIGHT_START_KG + (WEIGHT_END_KG - WEIGHT_START_KG) * x + hump + winter + ar
        fat = FAT_START_PCT + (FAT_END_PCT - FAT_START_PCT) * x + 0.5 * hump + 0.3 * winter
        trend[d] = (kg, fat)

    # ---- Withings weigh-ins
    travel = _spans(rng, w_start, today, 2, (4, 9), today - timedelta(days=3))
    muscle_from = today - timedelta(days=MUSCLE_DAYS)
    seg_from = today - timedelta(days=SEGMENT_DAYS)
    algo_change = today - timedelta(days=ALGO_CHANGE_DAYS)
    bad_day = today - timedelta(days=BAD_READING_DAYS)
    grpid = 900000000
    for d, (kg, fat) in trend.items():
        if _within(d, travel) or (rng.random() > 0.85 and d != today and d != bad_day):
            continue
        times = [6.5 + rng.random() * 1.5]
        if rng.random() < 0.05:
            times.append(19.0 + rng.random() * 2.5)      # an evening re-weigh
        for n, hours in enumerate(times):
            taken = _at(d, hours, tz)
            if taken > now:
                continue
            w = kg + rng.gauss(0, 0.2) + (0.9 + rng.gauss(0, 0.3) if n else 0)
            f = fat + rng.gauss(0, 0.6) + (0.3 if n else 0)
            segs = d >= seg_from
            if segs:
                algo = ALGO_B if d >= algo_change else ALGO_A
            else:
                algo = ALGO_OLD
            b = body(w, f, leg_shift=LEG_ALGO_SHIFT if (segs and algo == ALGO_B) else 0.0,
                     noise=rng)
            if d == bad_day and n == 0:
                # The scale's model failing on one reading: a normal weight,
                # but fat-free mass about half its neighbours'. Internally
                # consistent, so it loads, and the page must not hide it.
                for part in [b["whole"], *b["seg"].values()]:
                    part["fat"] += part["ffm"] / 2
                    part["ffm"] /= 2
                    part["muscle"] = part["ffm"] * (1 - BONE_SHARE)
                b["whole"]["bone"] = b["whole"]["ffm"] - b["whole"]["muscle"]
            grpid += 1
            ds.withings.append(scan_group(grpid, int(taken.timestamp()), b, segments=segs,
                                          muscle=d >= muscle_from, algo=algo,
                                          modelid=10 if segs else 5))
            pct = 100 * b["whole"]["fat"] / b["whole"]["weight"]
            ds.saw("weight_kg", b["whole"]["weight"])
            if d != bad_day:
                ds.saw("fat_pct", pct)
            if n == 0:
                ds.weights_lb.setdefault(d.isoformat(), round(b["whole"]["weight"] / KG_PER_LB, 1))

    # ---- Tonal, and the watch's view of it
    g_start = today - timedelta(days=GARMIN_DAYS)
    sessions: list[tuple[datetime, int]] = []        # (local start, seconds) the watch saw
    workouts, strength_hist = [], []
    week_n = 0
    d = tonal_start
    while d <= today:
        if d.weekday() == 0:
            week_n += 1
        if gap[0] <= d <= gap[1]:
            if d.weekday() in (0, 2, 4) and rng.random() < 0.85:
                start = _at(d, 17.5 + rng.random() * 0.5, tz)
                sessions.append((start, int(2700 + rng.random() * 900)))
        elif d.weekday() in WEEK and rng.random() < WEEK[d.weekday()][2]:
            tpl, hour, _ = WEEK[d.weekday()]
            if d.weekday() == 5 and rng.random() < 0.3:
                tpl = "demo-arms"
            start = _at(d, hour + rng.uniform(-0.6, 0.6), tz)
            if start <= now:
                w = _workout(rng, tpl, start, d, tonal_start, gap, week_n, ids, ds)
                workouts.append(w)
                if rng.random() < 0.85:
                    sessions.append((start - timedelta(minutes=rng.randint(1, 4)),
                                     w["duration"] + rng.randint(120, 360)))
        if d.weekday() == 6 and d >= tonal_start + timedelta(days=6):
            months = (d - tonal_start).days / 30.44
            overall = round(250 + 170 * (1 - math.exp(-months / 18)) - (25 if _within(d, [gap]) else 0), 1)
            upper, lower = round(overall * 0.45, 1), round(overall * 0.40, 1)
            strength_hist.append({"date": d.isoformat(), "overall": overall, "upper": upper,
                                  "lower": lower, "core": round(overall - upper - lower, 1)})
        d += timedelta(days=1)

    latest = strength_hist[-1] if strength_hist else {"overall": 250.0, "upper": 112.5,
                                                       "lower": 100.0, "core": 37.5}
    ds.tonal = {
        "version": "3.0", "exportedAt": _zulu(now), "exportedWith": "ToneGet v3.1.0",
        "user": {"id": "demo-user"}, "profile": {"totalWorkouts": len(workouts)},
        "workouts": workouts,
        "activityNames": {w["id"]: w["workoutTitle"] for w in workouts},
        "workoutCatalog": {t: {"id": t, "title": title, "workoutType": kind}
                           for t, (title, kind, _) in ROUTINES.items()},
        "customWorkouts": {t: {"id": t, "title": title}
                           for t, (title, kind, _) in ROUTINES.items() if kind == "Custom"},
        "strengthScoreHistory": list(reversed(strength_hist)),
        "currentStrengthScores": {
            "raw": [{"strengthBodyRegion": "Upper", "score": latest["upper"]}],
            "parsed": {"regions": {"Overall": latest["overall"], "Upper": latest["upper"],
                                   "Lower": latest["lower"], "Core": latest["core"]},
                       "muscles": {name: {"score": round(latest["overall"] * share / 3, 1),
                                          "region": region, "updatedAt": _zulu(now)}
                                   for name, region, share in (
                                       ("Chest", "Upper", 0.30), ("Back", "Upper", 0.34),
                                       ("Quads", "Lower", 0.32), ("Glutes", "Lower", 0.31),
                                       ("Abs", "Core", 0.12))}}},
        "pagination": {"declaredTotal": len(workouts), "failedPages": 0},
    }

    # ---- Garmin: the watch, through yesterday, plus this morning's night
    unworn = _spans(rng, g_start, today, 0.6, (2, 5), today - timedelta(days=10))
    no_sleep = {g_start + timedelta(days=rng.randrange(GARMIN_DAYS)) for _ in range(6)}
    ill: dict[date, float] = {}
    for a, _b in _spans(rng, g_start, today, 3, (2, 4), today + timedelta(days=1)):
        for k in range(4):
            ill[a + timedelta(days=k)] = max(ill.get(a + timedelta(days=k), 0),
                                             (4 + rng.random()) * (1 - 0.25 * k))
    walks: list[tuple[datetime, int]] = []
    trained = {s.date() for s, _ in sessions}
    act_id = 7000000000
    for i in range(GARMIN_DAYS + 1):
        d = g_start + timedelta(days=i)
        if _within(d, unworn):
            continue
        x = i / GARMIN_DAYS
        weekend = d.weekday() >= 5          # the night ending Saturday or Sunday morning
        sleep_h = rng.gauss(7.58 if weekend else 6.83, 0.67)
        sleep_h = min(max(sleep_h, 4.6), 9.4)
        bed = (23.67 if weekend else 23.0) + rng.gauss(0, 0.45)
        bed = min(max(bed, 22.25), 24.95)           # never 01:00-03:00: DST
        awake_h = rng.uniform(20, 50) / 60
        start = _at(d - timedelta(days=1), bed, tz)
        # Elapsed time, not wall-clock: a DST night is an hour longer or shorter.
        end = (start.astimezone(timezone.utc)
               + timedelta(hours=sleep_h + awake_h)).astimezone(tz)
        if end <= now:
            if d in no_sleep:
                ds.sleeps.append({"day": d, "start": start, "end": end, "total": 0,
                                  "deep": 0, "light": 0, "rem": 0, "awake": 0, "score": None})
            else:
                asleep = sleep_h * 3600
                deep, rem = asleep * rng.uniform(0.13, 0.18), asleep * rng.uniform(0.18, 0.24)
                score = round(55 + (sleep_h - 5.5) * 9 + rng.gauss(0, 5))
                score = min(max(score, 42), 99)
                ds.sleeps.append({"day": d, "start": start, "end": end, "total": asleep,
                                  "deep": deep, "rem": rem, "light": asleep - deep - rem,
                                  "awake": awake_h * 3600, "score": score})
                ds.saw("sleep_h", sleep_h)
                ds.saw("sleep_score", score)
        if d >= today:
            continue                          # the nightly loads through yesterday
        workout = d in trained
        rhr = round(58 - 4 * x + ill.get(d, 0) + rng.gauss(0, 1.2) + (0.6 if sleep_h < 6 else 0))
        rhr = min(max(rhr, 47), 72)
        steps = int(8500 * math.exp(rng.gauss(0, 0.3)))
        if rng.random() < 3 / 7:
            walk_start = _at(d, 12.0 + rng.random(), tz)
            walk_s = rng.randint(25, 50) * 60
            walks.append((walk_start, walk_s))
            steps += walk_s // 60 * 100
        steps = min(max(steps, 1500), 24000)
        bb_max = round(60 + (sleep_h - 5) * 10 + rng.gauss(0, 6))
        bb_min = round(20 + rng.gauss(0, 6) - (5 if workout else 0))
        mod = max(0.0, rng.gauss(22, 12)) * 60
        vig = (rng.uniform(15, 35) * 60) if workout else max(0.0, rng.gauss(4, 4)) * 60
        ds.garmin_days.append({
            "day": d, "rhr": rhr, "hr_min": rhr - rng.randint(3, 6),
            "hr_max": round(min(185, (150 if workout else 118) + rng.gauss(0, 9))),
            "steps": steps, "stress_avg": min(max(round(32 + rng.gauss(0, 6)), 12), 60),
            "bb_max": min(max(bb_max, 52), 100), "bb_min": min(max(bb_min, 5), 40),
            "calories_total": round(1800 + steps * 0.045 + (250 if workout else 0) + rng.gauss(0, 80)),
            "moderate": mod, "vigorous": vig, "intensity": mod + 2 * vig,
        })
        ds.saw("rhr", rhr)
        ds.saw("bb", ds.garmin_days[-1]["bb_max"])
        ds.saw("bb", ds.garmin_days[-1]["bb_min"])
        ds.saw("steps", steps)
        ds.saw("stress", ds.garmin_days[-1]["stress_avg"])

    for st, secs in sorted(sessions):
        if st.date() < g_start or st.date() >= today or _within(st.date(), unworn):
            continue
        act_id += 1
        ds.activities.append({"activity_id": str(act_id), "name": "Strength", "sport": "training",
                              "sub_sport": "strength_training", "start": st, "elapsed": secs,
                              "calories": round(secs / 60 * 6.5), "avg_hr": rng.randint(102, 124),
                              "max_hr": rng.randint(140, 166), "training_load": round(secs / 60 * 1.4, 1)})
    for st, secs in walks:
        act_id += 1
        ds.activities.append({"activity_id": str(act_id), "name": "Walk", "sport": "walking",
                              "sub_sport": "generic", "start": st, "elapsed": secs,
                              "calories": round(secs / 60 * 4.2), "avg_hr": rng.randint(88, 104),
                              "max_hr": rng.randint(108, 122), "training_load": round(secs / 60 * 0.5, 1)})
    check_limits(ds)
    return ds


def _load(slot: Slot, d: date, tonal_start: date, gap, week_n: int) -> float:
    """The working load for a movement on day d, per arm where arms_together.
    Invented: quick gains that slow (about 2% a month at first), a lighter
    week every twelfth, and some strength lost over the seven weeks away."""
    months = (d - tonal_start).days / 30.44
    load = slot.base_lb * (1 + 0.6 * (1 - math.exp(-months / 30)))
    if week_n % 12 == 11:
        load *= 0.94
    if d > gap[1]:
        load *= 1 - 0.08 * math.exp(-(d - gap[1]).days / 21)
    return min(load, LIMITS["tonal_lb_per_arm"][1])


def _workout(rng, tpl, start, d, tonal_start, gap, week_n, ids, ds) -> dict:
    title, kind, keys = ROUTINES[tpl]
    wid = str(uuid.UUID(int=rng.getrandbits(128), version=4))
    clock = start + timedelta(minutes=rng.uniform(1, 3))
    sets, sort = [], 0
    total_volume, total_reps = 0.0, 0
    for mi, key in enumerate(keys):
        slot = SLOTS[key]
        load = _load(slot, d, tonal_start, gap, week_n) if slot.on_machine else 0.0
        plan = ([("warm", 0)] if mi == 0 and slot.on_machine else []) + [("work", n) for n in range(3)]
        for kind_, n in plan:
            reps_p = 10 if kind_ == "warm" else slot.reps
            reps = reps_p - (0 if rng.random() < 0.7 else rng.randint(1, 2))
            weight = round(load * (0.55 if kind_ == "warm" else rng.uniform(0.97, 1.0)), 1)
            performed = not (kind_ == "work" and n == 2 and rng.random() < 0.01)
            set_id = _sid(tpl, mi, kind_, n)
            for side in ((0, 1) if slot.two_sided else (0,)):
                sort += 1
                dur = reps * 3.2 + rng.uniform(-2, 2)
                begin, clock = clock, clock + timedelta(seconds=dur)
                vol = weight * reps * (2 if slot.arms_together else 1)
                rom = rng.uniform(28, 44)
                s = {
                    "id": str(uuid.UUID(int=rng.getrandbits(128), version=4)),
                    "workoutActivityID": wid, "workoutId": tpl, "userId": "demo-user",
                    "setId": set_id, "movementId": ids[key], "sideNumber": side,
                    "blockNumber": mi // 2 + 1, "setGroup": 1, "round": n + 1,
                    "sortOrder": sort, "repetition": n + 1, "repetitionTotal": 3,
                    "beginTime": _zulu(begin), "endTime": _zulu(clock), "duration": round(dur),
                    "prescribedReps": reps_p, "repCount": reps,
                    "repsInReserve": round(rng.uniform(1, 3), 1),
                    "baseWeight": round(load), "avgWeight": weight,
                    "maxWeight": round(weight + rng.uniform(0.5, 2), 1) if weight else 0,
                    "minWeight": round(max(0.0, weight - rng.uniform(0.5, 2)), 1) if weight else 0,
                    "suggestedWeight": round(load), "eccentricWeight": 0, "chainsWeight": 0,
                    "oneRepMax": round(weight * (1 + reps / 30), 1),
                    "volume": round(vol, 1), "totalVolume": round(vol),
                    "concentricWork": round(vol * 0.4 * 8.85, 1),
                    "totalConcentricWork": round(vol * 0.4),
                    "maxConPower": round(weight * rng.uniform(5, 7), 1),
                    "velAtMaxConPower": round(rng.uniform(20, 40), 1),
                    "weightAtMaxConPower": weight, "avgVelocity": round(rng.uniform(15, 30), 1),
                    "rom": round(rom, 1), "avgRom": round(rom - rng.uniform(0, 2), 1),
                    "romLengthIn": round(rom * reps, 1),
                    "strugglingScore": round(rng.uniform(0, 1), 2),
                    "inconsistencyScore": round(rng.uniform(0, 0.3), 2),
                    "warmUp": kind_ == "warm", "dropSet": False, "finalSet": kind_ == "work" and n == 2,
                    "burnout": False, "practice": False, "calibration": False, "progressive": False,
                    "spotter": False, "eccentric": False, "chains": False,
                }
                if not performed:
                    # Prescribed, never begun: Tonal writes Go's zero time and 0s.
                    s.update(beginTime="0001-01-01T00:00:00Z", endTime="0001-01-01T00:00:00Z",
                             duration=0, repCount=0, avgWeight=0, maxWeight=0, minWeight=0,
                             volume=0, totalVolume=0, concentricWork=0, totalConcentricWork=0,
                             maxConPower=0, weightAtMaxConPower=0)
                elif rng.random() < 0.02:
                    s["prs"] = ["Power"]
                if performed:
                    total_volume += s["volume"]
                    total_reps += reps
                    if slot.on_machine:
                        ds.saw("tonal_lb_per_arm", weight)
                sets.append(s)
            clock += timedelta(seconds=rng.uniform(40, 90))
    return {"id": wid, "workoutId": tpl, "beginTime": _zulu(start), "workoutType": kind,
            "workoutTitle": title, "duration": int((clock - start).total_seconds()) + 120,
            "totalVolume": round(total_volume), "totalReps": total_reps,
            "workoutSetActivity": sets}


def check_limits(ds: Dataset) -> None:
    """Raise if any generated value is outside LIMITS. The 0 s nights and
    the one bad reading are the deliberate exceptions, and never reach here."""
    for key, values in ds.seen.items():
        lo, hi = LIMITS[key]
        bad = [v for v in values if not lo <= v <= hi]
        if bad:
            raise ValueError(f"demo generator: {len(bad)} {key} value(s) outside "
                             f"{lo}..{hi}, e.g. {bad[0]}")


# ------------------------------------------------------------ the sources

def write_garmindb(ds: Dataset, dbs: Path) -> None:
    """GarminDB-shaped files, with GarminDB's own table and column names
    (3.9.0). Durations are 'HH:MM:SS' text and times naive local, as
    GarminDB writes them. Only the columns the loader reads, and a few
    neighbours."""
    dbs.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(dbs / "garmin.db")
    c.executescript("""
        CREATE TABLE sleep (day DATETIME PRIMARY KEY, start DATETIME, "end" DATETIME,
            total_sleep TIME, deep_sleep TIME, light_sleep TIME, rem_sleep TIME, awake TIME,
            avg_spo2 FLOAT, avg_rr FLOAT, avg_stress FLOAT, score INTEGER, qualifier VARCHAR);
        CREATE TABLE daily_summary (day DATETIME PRIMARY KEY, hr_min INTEGER, hr_max INTEGER,
            rhr INTEGER, stress_avg INTEGER, step_goal INTEGER, steps INTEGER,
            moderate_activity_time TIME, vigorous_activity_time TIME, intensity_time TIME,
            calories_total INTEGER, bb_max INTEGER, bb_min INTEGER, description VARCHAR);
        CREATE TABLE weight (day DATETIME PRIMARY KEY, weight FLOAT);
    """)
    c.executemany("INSERT INTO sleep VALUES (?,?,?,?,?,?,?,?,NULL,NULL,NULL,?,NULL)", [
        (f"{s['day']} 00:00:00.000000", _gdt(s["start"]), _gdt(s["end"]), _hms(s["total"]),
         _hms(s["deep"]), _hms(s["light"]), _hms(s["rem"]), _hms(s["awake"]), s["score"])
        for s in ds.sleeps])
    c.executemany("INSERT INTO daily_summary VALUES (?,?,?,?,?,7000,?,?,?,?,?,?,?,NULL)", [
        (g["day"].isoformat(), g["hr_min"], g["hr_max"], g["rhr"], g["stress_avg"], g["steps"],
         _hms(g["moderate"]), _hms(g["vigorous"]), _hms(g["intensity"]), g["calories_total"],
         g["bb_max"], g["bb_min"]) for g in ds.garmin_days])
    first = ds.garmin_days[0]["day"].isoformat() if ds.garmin_days else ""
    c.executemany("INSERT INTO weight VALUES (?,?)",
                  [(d, lb) for d, lb in ds.weights_lb.items() if d >= first])
    c.commit()
    c.close()
    c = sqlite3.connect(dbs / "garmin_activities.db")
    c.execute("""CREATE TABLE activities (activity_id VARCHAR PRIMARY KEY, name VARCHAR,
        description VARCHAR, type VARCHAR, sport VARCHAR, sub_sport VARCHAR,
        training_load FLOAT, start_time DATETIME, stop_time DATETIME, elapsed_time TIME,
        moving_time TIME, avg_hr INTEGER, max_hr INTEGER, calories INTEGER)""")
    c.executemany("INSERT INTO activities VALUES (?,?,NULL,NULL,?,?,?,?,?,?,?,?,?,?)", [
        (a["activity_id"], a["name"], a["sport"], a["sub_sport"], a["training_load"],
         _gdt(a["start"].replace(tzinfo=None)),
         _gdt((a["start"] + timedelta(seconds=a["elapsed"])).replace(tzinfo=None)),
         _hms(a["elapsed"]), _hms(a["elapsed"] * 0.9), a["avg_hr"], a["max_hr"], a["calories"])
        for a in ds.activities])
    c.commit()
    c.close()


CONFIG = """# Written by `sync_all.py demo`. Every path is inside this folder.
[paths]
wellness_db      = "{out}/wellness.db"
garmin_db_dir    = "{out}/DBs"
tonal_export_dir = "{out}/tonal_exports"
archive_dir      = "{out}/raw"
withings_token   = "{out}/withings_token.json"
dashboard_dir    = "{out}/dashboard"

[local]
timezone = "{tz}"

[display]
units = "imperial"

[garmin]
db_files = ["garmin.db", "garmin_activities.db"]

[tonal]
movement_catalog = "{out}/movements.json"
movement_names   = "{out}/movement-names.toml"

[withings]
# Not a credential: the demo's Withings data never comes from the API.
client_id = "demo"
"""


def real_data_paths(config_path: Path | None = None) -> list[Path]:
    """Folders the demo must keep out of: the default data and secrets
    folders, and every path the real config names, if there is one."""
    home = Path.home()
    paths = [home / "HealthData", home / ".secrets", REPO / "reference"]
    cfg_path = config_path or REPO / "config.toml"
    if cfg_path.is_file():
        from . import config            # only its paths are read
        cfg = config.load(cfg_path)
        paths += [cfg.wellness_db.parent, cfg.garmin_db_dir, cfg.tonal_export_dir,
                  cfg.archive_dir, cfg.dashboard_dir, cfg.withings_token.parent]
    return [p.expanduser().resolve() for p in paths]


def _touches(a: Path, b: Path) -> bool:
    return a == b or a in b.parents or b in a.parents


def prepare(out: Path, *, forbidden: list[Path]) -> None:
    """Make `out` an empty demo folder, or refuse. Refuses a folder that is,
    holds or sits inside a real data folder or the repo itself, and a
    non-empty folder the demo didn't make."""
    out = out.expanduser().resolve()
    for p in [*forbidden, REPO.resolve()]:
        if _touches(out, p) and not (p == REPO.resolve() and REPO.resolve() in out.parents):
            raise SystemExit(f"Refusing to write the demo to {out}: it overlaps {p}.")
    if out.exists() and any(out.iterdir()):
        if not (out / MARKER).is_file():
            raise SystemExit(f"Refusing to write the demo to {out}: it isn't empty, and "
                             f"it isn't a demo folder (no {MARKER} in it).")
        for child in out.iterdir():
            if child.name != MARKER:
                shutil.rmtree(child) if child.is_dir() and not child.is_symlink() else child.unlink()
    out.mkdir(parents=True, exist_ok=True)
    (out / MARKER).write_text("Made by sync_all.py demo. Rebuilt, contents and all, on every run.\n")


def write_sources(ds: Dataset, out: Path) -> Path:
    """Write every source file and the demo's config. Returns the config."""
    out = out.resolve()
    write_garmindb(ds, out / "DBs")
    exports = out / "tonal_exports"
    exports.mkdir(parents=True, exist_ok=True)
    (exports / f"tonal_workouts_{ds.today:%Y%m%d}.json").write_text(json.dumps(ds.tonal))
    (out / "movements.json").write_text(json.dumps(ds.catalog, indent=1))
    cfg = out / "config.toml"
    cfg.write_text(CONFIG.format(out=out.as_posix(), tz=TZ_NAME))
    return cfg


def real_catalog() -> list[dict] | None:
    """The fetched movement catalog, if it is here and readable."""
    path = REPO / "reference" / "tonal-movements.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, list) else None
