"""Static dashboard: one self-contained HTML page built from the read-only snapshot.

    python3 sync_all.py dashboard            # -> <dashboard_dir>/index.html

scripts/nightly.sh runs this straight after `sync_all.py all` writes
wellness-ro.db. The page carries its data as embedded JSON and draws its own
SVG, so there is no server process, no SQL surface and nothing fetched after
load. Serve the dashboard directory - never ~/HealthData itself, which holds
the databases - with a static server bound to 127.0.0.1.

What the page may and may not do with the data:
  * No interpolation. Series are day-indexed with None where the source has
    no value, and the page breaks the line there.
  * Garmin's 0-second sleep is a night the watch didn't record, not 0 h.
  * Tonal pounds and Garmin minutes are never combined into one load figure.
  * Every mass is converted here, once, to the unit `[display] units` names.
    The database keeps each source's own unit; the page never converts.
  * No composite scores. Every figure is a measurement, or a measurement
    compared with its own history.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import statistics
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import db, guard

TEMPLATE = Path(__file__).resolve().parent / "dashboard.html"
PLACEHOLDER = "__DASHBOARD_DATA__"
SOURCES = ("garmin", "tonal", "withings")

# A baseline is the 30 days before the day being described, never including
# that day, and needs at least half of those days to hold a value.
BASELINE_DAYS = 30
# "What moved" names a metric at least this many SDs from its own 30-day
# mean. Checked against a year of real readings: at 1.0 SD the line is
# almost never empty, and at 2.0 it is empty on most days. Empty is meant to
# be the usual answer, so 2.0. The page and the brief both read this.
MOVED_SD = 2.0
# The Today tab draws each night reading as a bar from its own 30-day median.
# Half the track is this many of the metric's 30-day SDs. A reading further
# out than that stops at the end of the track, flagged `clipped`, and the page
# marks the end with a chevron: an extreme night can't push the bar past the
# card, and it isn't drawn as if it were exactly 3 SD either.
DEV_CAP_SD = 3.0
# Nights in the header's sleep strip, ending with the night that ended today.
STRIP_NIGHTS = 14
# The strip's bars start STRIP_FLOOR of the nights' span below the shortest
# night within STRIP_REACH_H of the 30-day median, rounded down to the half
# hour. The span counts as at least STRIP_MIN_SPAN_H (dashboard.strip_axis).
STRIP_FLOOR = 0.5
STRIP_REACH_H = 2.5
STRIP_MIN_SPAN_H = 1.0
# Records closer than this are one session: a warm-up, a stretch and the main
# workout are three Tonal records but one session, and the watch logs the
# same session again.
SESSION_GAP = timedelta(minutes=30)
GARMIN_STRENGTH = "strength_training"
# Masses arrive in the source's unit - Withings kg, Tonal lb - and leave in
# the display unit. 1 lb is exactly 0.45359237 kg.
KG_PER_LB = 0.45359237
MASS_UNIT = {"imperial": "lb", "metric": "kg"}


# ---------------------------------------------------------------- helpers

def _day(s) -> date | None:
    try:
        return date.fromisoformat(str(s)[:10]) if s else None
    except ValueError:
        return None


def _local(ts, tz: ZoneInfo) -> datetime | None:
    """A timestamp as naive local wall-clock time.

    With an offset or Z it is converted. Without one it is taken as already
    local: garmin_activity.start_local is GarminDB's start_time as written,
    which is local wall-clock time (it trails Tonal's UTC begin times by
    exactly the CST/CDT offset).
    """
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).strip().replace(" ", "T"))
    except ValueError:
        return None
    return dt.astimezone(tz).replace(tzinfo=None) if dt.tzinfo else dt


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _iso(d: date | None) -> str | None:
    return d.isoformat() if d else None


def _fmt_local(utc: str | None, tz: ZoneInfo) -> str | None:
    t = _local(utc, tz)
    return f"{t:%a} {t.day} {t:%b %H:%M}" if t else None


def dense(points: dict[date, float]) -> dict | None:
    """Day-indexed values from the first day with data to the last. A day
    without a value stays None: the page draws a gap there, never a segment."""
    if not points:
        return None
    start, end = min(points), max(points)
    return {"start": start.isoformat(),
            "values": [points.get(start + timedelta(days=i))
                       for i in range((end - start).days + 1)]}


def mass_factor(source_unit: str, units: str) -> float:
    """What to multiply a mass in `source_unit` ('kg' or 'lb') by to show it
    in the display unit for `units` ('imperial' or 'metric')."""
    target = MASS_UNIT[units]
    if source_unit == target:
        return 1.0
    return 1 / KG_PER_LB if source_unit == "kg" else KG_PER_LB


def scaled(points: dict[date, float], factor: float) -> dict[date, float]:
    return {d: round(v * factor, 3) for d, v in points.items()}


# Every bucket of means starts a whole number of buckets after this Monday,
# so two series bucketed alike line up whatever their first day. The page
# snaps to the same grid (BUCKET_ANCHOR in dashboard.html, day 4 = this).
BUCKET_ANCHOR = date(1970, 1, 5)


def bucket_mean(points: dict[date, float], days: int) -> dict | None:
    """Means over consecutive `days`-day buckets on the BUCKET_ANCHOR grid,
    for a span too long to give each day its own mark. A bucket without a
    value stays None, the same gap a missing day is; `n` says how many days
    each mean is made of."""
    if not points:
        return None
    first = (min(points) - BUCKET_ANCHOR).days // days
    b0 = BUCKET_ANCHOR + timedelta(days=first * days)
    buckets: list[list[float]] = [[] for _ in range((max(points) - b0).days // days + 1)]
    for d, v in points.items():
        buckets[(d - b0).days // days].append(v)
    return {"start": b0.isoformat(), "days": days,
            "values": [round(statistics.fmean(b), 3) if b else None for b in buckets],
            "n": [len(b) for b in buckets]}


def weekly_mean(points: dict[date, float]) -> dict | None:
    """Monday-to-Sunday means (the anchor is a Monday)."""
    return bucket_mean(points, 7)


def window(points: dict[date, float], end: date, days: int) -> dict[date, float]:
    """The values on the `days` days ending with `end`, inclusive. A day
    without a value is absent, never filled."""
    return {d: points[d] for d in (end - timedelta(days=k) for k in range(days)) if d in points}


def _window(points: dict[date, float], day: date, days: int) -> list[float]:
    """The values on the `days` days before `day`, never including it."""
    return list(window(points, day - timedelta(days=1), days).values())


def needed(days: int) -> int:
    """The half-filled rule: a window's statistics need a value on at least
    half of its days."""
    return (days + 1) // 2


def enough(n: int, days: int) -> bool:
    return n >= needed(days)


def summarize(vals: list[float]) -> dict:
    """n, mean, median, SD, min and max of the values present."""
    return {"n": len(vals), "mean": statistics.fmean(vals), "median": statistics.median(vals),
            "sd": statistics.stdev(vals) if len(vals) > 1 else 0.0,
            "min": min(vals), "max": max(vals)}


def baseline(points: dict[date, float], day: date, days: int = BASELINE_DAYS) -> dict | None:
    """Median, mean and SD over the `days` days before `day`, or None when
    fewer than half of those days have a value."""
    vals = _window(points, day, days)
    if not vals or not enough(len(vals), days):
        return None
    s = summarize(vals)
    return {k: s[k] for k in ("n", "median", "mean", "sd")}


def average(points: dict[date, float], day: date, days: int) -> float | None:
    """Mean over the `days` days before `day`, with the same half-filled rule."""
    vals = _window(points, day, days)
    return statistics.fmean(vals) if vals and enough(len(vals), days) else None


def compare(points: dict[date, float], day: date) -> dict:
    """One day's value against its own 30-day history."""
    value = points.get(day)
    b = baseline(points, day)
    z = None
    if value is not None and b and b["sd"] > 0:
        z = (value - b["mean"]) / b["sd"]
    return {"value": value,
            "median": b["median"] if b else None,
            "mean": b["mean"] if b else None,
            "sd": b["sd"] if b else None,
            "n": b["n"] if b else 0,
            "z": z}


# ------------------------------------------------------------------ series

GARMIN_KEYS = ("sleep_h", "sleep_score", "rhr", "bb_min", "bb_max")


def garmin_series(conn: sqlite3.Connection) -> dict[str, dict[date, float]]:
    out: dict[str, dict[date, float]] = {k: {} for k in GARMIN_KEYS}
    for r in conn.execute(
        "SELECT day, sleep_total_s, sleep_score, rhr, bb_min, bb_max FROM garmin_daily"
    ):
        d = _day(r["day"])
        if d is None:
            continue
        # GarminDB writes 0 s for a night the watch didn't record, for whole
        # months at a time on some installs. That night is missing; drawing
        # it as 0 h invents a collapse.
        if r["sleep_total_s"]:
            out["sleep_h"][d] = round(r["sleep_total_s"] / 3600, 3)
        for k in GARMIN_KEYS[1:]:
            if r[k] is not None:
                out[k][d] = r[k]
    return out


def weigh_ins(conn: sqlite3.Connection, tz: ZoneInfo) -> dict:
    """Each local day's first weigh-in - the morning one - and the day's first
    body-fat reading. v_withings_day keeps the LAST reading instead, which
    mixes evening re-weighs into a morning trend."""
    weight: dict[date, float] = {}
    fat: dict[date, float] = {}
    taken: dict[date, str | None] = {}
    for r in conn.execute(
        "SELECT day, taken_utc, weight_kg, fat_ratio FROM v_body_composition "
        "ORDER BY taken_utc"
    ):
        d = _day(r["day"])
        if d is None:
            continue
        if d not in weight and r["weight_kg"] is not None:
            weight[d] = round(r["weight_kg"], 3)
            t = _local(r["taken_utc"], tz)
            taken[d] = t.strftime("%H:%M") if t else None
        if d not in fat and r["fat_ratio"] is not None:
            fat[d] = round(r["fat_ratio"], 3)
    return {"weight": weight, "fat": fat, "taken": taken}


def segmental_balance(conn: sqlite3.Connection) -> dict[str, dict[date, float]]:
    """Right minus left muscle mass per day, from the first weigh-in that day
    carrying both sides of the pair.

    Computed per weigh-in rather than read from v_segmental_balance, whose
    MAX per side across a day can pair one weigh-in's left with another's
    right.
    """
    by_group: dict[tuple, dict] = {}
    for r in conn.execute(
        "SELECT s.day, s.grpid, s.segment, s.muscle_mass_kg, "
        "  (SELECT MIN(m.taken_epoch) FROM withings_measure m WHERE m.grpid = s.grpid) AS t "
        "FROM v_segmental s "
        "WHERE s.segment IN ('arm_left', 'arm_right', 'leg_left', 'leg_right') "
        "  AND s.muscle_mass_kg IS NOT NULL"
    ):
        g = by_group.setdefault((r["t"] or 0, r["grpid"]), {"day": _day(r["day"])})
        g[r["segment"]] = r["muscle_mass_kg"]
    out: dict[str, dict[date, float]] = {"arms": {}, "legs": {}}
    for _, g in sorted(by_group.items()):
        for pair, (left, right) in (("arms", ("arm_left", "arm_right")),
                                    ("legs", ("leg_left", "leg_right"))):
            if g["day"] and g["day"] not in out[pair] and left in g and right in g:
                out[pair][g["day"]] = round(g[right] - g[left], 3)
    return out


def balance_stats(points: dict[date, float]) -> dict | None:
    """What the annotation on the balance chart says, computed rather than
    typed in, so it stays true as readings accumulate.

    `sd_change` is the SD of the change in R-L between consecutive days: the
    typical day-to-day wobble the chart's band shows. `months_positive`
    counts months whose mean R-L is above zero.

    `lead` is "right" or "left" only while the difference is steady: the
    same side ahead in every month, and the mean gap bigger than the
    wobble. Otherwise it is None and the caption claims no direction. Steady
    does not mean anatomical; a fixed electrode bias would also be steady.
    """
    if len(points) < 2:
        return None
    days = sorted(points)
    changes = [points[b] - points[a] for a, b in zip(days, days[1:])
               if (b - a).days == 1]
    months: dict[str, list[float]] = {}
    for d in days:
        months.setdefault(d.isoformat()[:7], []).append(points[d])
    mean = statistics.fmean(points.values())
    sd_change = statistics.stdev(changes) if len(changes) > 1 else None
    positive = sum(1 for v in months.values() if statistics.fmean(v) > 0)
    negative = sum(1 for v in months.values() if statistics.fmean(v) < 0)
    lead = None
    if sd_change is not None and abs(mean) > sd_change:
        if mean > 0 and positive == len(months):
            lead = "right"
        elif mean < 0 and negative == len(months):
            lead = "left"
    return {"n": len(days),
            "first": days[0].isoformat(),
            "mean": round(mean, 3),
            "sd_change": round(sd_change, 3) if sd_change is not None else None,
            "months": len(months),
            "months_positive": positive,
            "months_negative": negative,
            "lead": lead}


# ---------------------------------------------------------------- body scan

# The Body Scan tab's six regions, in the order shown. Segment keys are
# withings_position's names: 2 right arm, 3 left arm, 10 left leg, 11 right
# leg, 12 torso. Arms and legs number their sides differently, on purpose.
REGIONS = (("whole", "Whole body"), ("torso", "Torso"), ("arm_left", "Left arm"),
           ("arm_right", "Right arm"), ("leg_left", "Left leg"), ("leg_right", "Right leg"))
SEGMENTS = tuple(k for k, _ in REGIONS[1:])
SHORT = {"whole": "Whole", "torso": "Torso", "arm_left": "L arm", "arm_right": "R arm",
         "leg_left": "L leg", "leg_right": "R leg"}
# Whole-body codes: weight, fat-free mass, fat mass, muscle mass, bone mass.
# Segmental codes: fat-free mass, fat mass, muscle mass, once per segment.
WHOLE_TYPES = {1: "weight", 5: "ffm", 8: "fat", 76: "muscle", 88: "bone"}
SEGMENT_TYPES = {173: "ffm", 174: "fat", 175: "muscle"}
# Means over this many days, for a span where even weekly means would put
# more than one mark on a pixel: a long whole-body history on a phone.
LONG_BUCKET_DAYS = 28


def composition(ffm: float | None, fat: float | None, muscle: float | None) -> dict | None:
    """Mass, fat %, muscle % and derived bone of one region of one weigh-in,
    from that region's own fat-free, fat and muscle mass.

        segment_mass = fat-free + fat
        muscle %     = muscle / segment_mass
        fat %        = fat / segment_mass
        bone %       = (fat-free - muscle) / segment_mass

    THE TRAP: the fat-free mass in the denominator is the REGION'S OWN. Never
    the whole body's. With round numbers: a torso of 40 kg fat-free, 10 kg
    fat and 38 kg muscle is 38 / (40 + 10) = 76% muscle. Put a whole body's
    64 kg fat-free mass in the denominator instead, 38 / (64 + 10), and it
    comes out 51%: wrong, and nothing about it looks wrong.

    For the whole body, fat-free + fat is the weight (to rounding, on real
    weigh-ins), so one formula serves all six regions.

    Bone is DERIVED. Withings reports bone mass (88) for the whole body only.
    There, fat-free minus muscle equals it (to rounding, on real weigh-ins),
    so a segment's bone is taken the same way. The page labels it derived
    wherever it appears. Before an account's scale measured muscle mass,
    only the whole body's mass and fat % are known."""
    if ffm is None or fat is None or ffm + fat <= 0:
        return None
    mass = ffm + fat
    out = {"mass": mass, "fat": fat, "fat_pct": 100 * fat / mass,
           "muscle": muscle, "muscle_pct": None, "bone": None, "bone_pct": None}
    if muscle is not None:
        out.update(muscle_pct=100 * muscle / mass, bone=ffm - muscle,
                   bone_pct=100 * (ffm - muscle) / mass)
    return out


def scan_groups(conn: sqlite3.Connection) -> list[dict]:
    """Every weigh-in with body composition, oldest first: its day, time,
    whole-body masses and each segment's masses, in kg as stored, and the
    algorithm version of its fat mass."""
    groups: dict[str, dict] = {}
    for r in conn.execute(
        "SELECT m.grpid, m.taken_epoch, m.taken_utc, m.local_date, m.meastype, m.position, "
        "       p.segment, m.value, m.algo "
        "FROM withings_measure m LEFT JOIN withings_position p ON p.position = m.position "
        "WHERE m.category = 1 AND m.meastype IN (1, 5, 8, 76, 88, 173, 174, 175)"
    ):
        g = groups.setdefault(r["grpid"], {"t": r["taken_epoch"], "utc": r["taken_utc"],
                                           "day": _day(r["local_date"]), "whole": {}, "seg": {},
                                           "algo": None})
        g["t"] = min(g["t"], r["taken_epoch"])
        # Withings' algorithm version. A new one can shift every reading
        # overnight: one update has been seen to move both legs' fat % by
        # several points from one day to the next.
        if r["algo"] is not None and r["meastype"] == 8:
            g["algo"] = r["algo"]
        if r["meastype"] in SEGMENT_TYPES:
            if r["segment"] in SEGMENTS:
                g["seg"].setdefault(r["segment"], {})[SEGMENT_TYPES[r["meastype"]]] = r["value"]
        elif r["position"] in (None, 7):           # 7 is BodyScan's whole body
            g["whole"][WHOLE_TYPES[r["meastype"]]] = r["value"]
    return sorted((g for g in groups.values() if g["day"]), key=lambda g: (g["t"], g["utc"]))


def region_masses(g: dict, region: str) -> dict:
    return g["whole"] if region == "whole" else g["seg"].get(region, {})


# The body map shades every region by its fat %, off this ramp: darker is
# more fat. It is the map's scale only. Everywhere else on the tab a region is
# a series in its identity colour, a categorical palette the page holds as
# theme tokens (--rg-<key>), so a ramp shade never stands for a region.
# The anchors are invented, round and evenly spaced, 10% to 30%: wide enough
# to span the "athletes" to "average" bands of the American Council on
# Exercise's body-fat chart for women and men alike, so most adults' regions
# land inside it. They are not fitted to anyone's readings; don't tune them
# to one person's range. Values between anchors are interpolated; beyond the
# ends, the end colour. Nothing goes darker than #A46634, which already starts
# to sink into the navy page. Text on a region is dark on the three lightest
# anchors and light on the two darkest: the switch is halfway from 20 to 25.
FAT_RAMP = ((10.0, "#F2CBA8"), (15.0, "#E0AE82"), (20.0, "#CC9261"),
            (25.0, "#B87845"), (30.0, "#A46634"))
INK_DARK, INK_LIGHT, INK_SWITCH = "#26170B", "#FFF4EA", 22.5
# Section 2's windows: what "then" means. They're the tab's own, not Trends'.
CHANGE_WINDOWS = {"90d": 90, "1y": 365, "All": None}
# An algorithm change is called out when it moved a region's fat % by at
# least this many points, measured as the median of the ALGO_SCANS scans
# after it against the median of the ALGO_SCANS before.
ALGO_STEP_NOTE = 1.0
ALGO_SCANS = 7
# A then or now reading is called out when it sits at least END_FLAG points
# from the median of the END_SCANS scans beside it inside the window.
END_FLAG = 1.5
END_SCANS = 7


def fat_colour(pct: float) -> str:
    """The ramp's colour for a fat %, interpolated in sRGB between anchors."""
    if pct <= FAT_RAMP[0][0]:
        return FAT_RAMP[0][1]
    for (a, ca), (b, cb) in zip(FAT_RAMP, FAT_RAMP[1:]):
        if pct <= b:
            t = (pct - a) / (b - a)
            x = [int(ca[i:i + 2], 16) for i in (1, 3, 5)]
            y = [int(cb[i:i + 2], 16) for i in (1, 3, 5)]
            return "#" + "".join(f"{round(u + (v - u) * t):02X}" for u, v in zip(x, y))
    return FAT_RAMP[-1][1]


def fat_ink(pct: float) -> str:
    return INK_DARK if pct < INK_SWITCH else INK_LIGHT


def full_scans(groups: list[dict]) -> list[dict]:
    """Each day's first weigh-in with all six regions, oldest first. Sections
    1 to 3 compare regions within one weigh-in, so they use only these."""
    out: dict[date, dict] = {}
    for g in groups:
        if ({"ffm", "fat", "muscle"} <= set(g["whole"])
                and all(len(g["seg"].get(s, {})) == 3 for s in SEGMENTS)):
            out.setdefault(g["day"], g)
    return [out[d] for d in sorted(out)]


def region_comp(g: dict, region: str) -> dict | None:
    m = region_masses(g, region)
    return composition(m.get("ffm"), m.get("fat"), m.get("muscle"))


def algo_steps(scans: list[tuple[date, int | None, dict]]) -> list[dict]:
    """Where the algorithm version changes along `scans` ([(day, algo,
    {region: fat %})], oldest first), and how far each region's fat % moved
    there: the median of the ALGO_SCANS scans after against the ALGO_SCANS
    before. Scans without a recorded version are skipped, so a gap in the
    record is never read as a change."""
    known = [x for x in scans if x[1] is not None]
    out = []
    for i in range(1, len(known)):
        if known[i][1] == known[i - 1][1]:
            continue
        before, after = known[max(0, i - ALGO_SCANS):i], known[i:i + ALGO_SCANS]
        steps = {}
        for k in known[i][2]:
            b = [x[2][k] for x in before if k in x[2]]
            a = [x[2][k] for x in after if k in x[2]]
            if a and b:
                steps[k] = round(statistics.median(a) - statistics.median(b), 1)
        out.append({"day": known[i][0].isoformat(), "from": known[i - 1][1], "to": known[i][1],
                    "steps": steps})
    return out


def fat_change(scans: list[dict], today: date, days: int | None, steps: list[dict]) -> dict | None:
    """Section 2 for one window: each region's fat % at the first full scan
    in the window and at the latest, and the points lost between them. Two
    actual scans, not a fitted trend, so a bad reading at either end shows
    instead of being smoothed away. That's why the ends are checked: one
    reading END_FLAG or more from the median of the END_SCANS scans beside
    it is named, and so is any algorithm change between the two scans that
    moved a region ALGO_STEP_NOTE points or more."""
    start = today - timedelta(days=days - 1) if days else date.min
    inside = [g for g in scans if g["day"] >= start]
    if len(inside) < 2:
        return None
    then, now = inside[0], inside[-1]
    fat = {k: [region_comp(g, k)["fat_pct"] for g in inside] for k, _ in REGIONS}
    rows = [{"key": k, "label": lab, "then": round(fat[k][0], 1), "now": round(fat[k][-1], 1),
             "lost": round(fat[k][0] - fat[k][-1], 1) + 0.0} for k, lab in REGIONS]
    rows.sort(key=lambda r: -r["lost"])
    ends = []
    for k, lab in REGIONS:
        for end, val, near in (("then", fat[k][0], fat[k][1:1 + END_SCANS]),
                               ("now", fat[k][-1], fat[k][-1 - END_SCANS:-1])):
            med = statistics.median(near) if near else None
            if med is not None and abs(val - med) >= END_FLAG:
                ends.append({"key": k, "label": lab, "end": end,
                             "day": (then if end == "then" else now)["day"].isoformat(),
                             "value": round(val, 1), "median": round(med, 1), "n": len(near)})
    crossed = [{"day": a["day"], "steps": {k: v for k, v in a["steps"].items()
                                          if abs(v) >= ALGO_STEP_NOTE}}
               for a in steps if then["day"].isoformat() < a["day"] <= now["day"].isoformat()]
    return {"then": then["day"].isoformat(), "now": now["day"].isoformat(), "scans": len(inside),
            "rows": rows, "ends": ends, "algo": [a for a in crossed if a["steps"]],
            "algo_all": len(crossed)}


def body_scan(conn: sqlite3.Connection, tz: ZoneInfo, kg: float, today: date) -> dict:
    """Everything the Body Scan tab draws.

    Sections 1 to 3 use full scans only (each day's first weigh-in with all
    six regions), so every comparison between regions is within one weigh-in
    and covers the segmental era. Section 4 carries each region's own fat %
    history, which for the whole body can go back to an older scale.

    Only fat % is drawn over time. Fat, muscle and bone make 100% of each
    region and bone barely moves, so muscle % is close to a constant minus
    fat %: its line would be the fat line upside down. Muscle % stays as a
    number. A day before a series' first reading has no value; nothing is
    filled in."""
    groups = scan_groups(conn)
    scans = full_scans(groups)
    regions = []
    for key, label in REGIONS:
        fat: dict[date, float] = {}
        first_muscle = None
        for g in groups:
            m = region_masses(g, key)
            # A segment needs all three of its masses; the whole body needs
            # fat-free and fat mass, and has muscle only once the scale
            # measured it.
            if key != "whole" and len(m) < 3:
                continue
            c = composition(m.get("ffm"), m.get("fat"), m.get("muscle"))
            if c is None or g["day"] in fat:
                continue
            fat[g["day"]] = round(c["fat_pct"], 2)
            if first_muscle is None and c["muscle_pct"] is not None:
                first_muscle = (g["day"], c["muscle_pct"])
        first_fat = min(fat) if fat else None
        regions.append({
            "key": key, "label": label, "short": SHORT[key],
            "first": {"fat_pct": _iso(first_fat),
                      "muscle_pct": _iso(first_muscle[0]) if first_muscle else None},
            "first_value": {"fat_pct": fat[first_fat] if fat else None,
                            "muscle_pct": round(first_muscle[1], 2) if first_muscle else None},
            "series": dense(fat), "weekly": bucket_mean(fat, 7),
            "long": bucket_mean(fat, LONG_BUCKET_DAYS),
            # Section 4's own range for this region, from all its readings.
            "axis": lane_axis(list(fat.values())),
        })

    out = {"regions": regions, "latest": None, "long_days": LONG_BUCKET_DAYS,
           "ramp": {"anchors": [list(a) for a in FAT_RAMP], "dark": INK_DARK,
                    "light": INK_LIGHT, "switch": INK_SWITCH}}
    if not scans:
        return out

    g = scans[-1]
    rows = []
    for key, label in REGIONS:
        c = region_comp(g, key)
        rows.append({"key": key, "label": label, "short": SHORT[key],
                     "mass": round(c["mass"] * kg, 1),
                     "fat_pct": round(c["fat_pct"], 1), "muscle_pct": round(c["muscle_pct"], 1),
                     "bone_pct": round(c["bone_pct"], 1),
                     "fat": round(c["fat"] * kg, 1), "muscle": round(c["muscle"] * kg, 1),
                     "bone": round(c["bone"] * kg, 2),
                     # The map's shade for this fat %, not the region's colour.
                     "colour": fat_colour(c["fat_pct"]), "ink": fat_ink(c["fat_pct"])})
    segs = [r for r in rows if r["key"] != "whole"]
    hi, lo = max(segs, key=lambda r: r["fat_pct"]), min(segs, key=lambda r: r["fat_pct"])
    t = _local(g["utc"], tz)
    w = g["whole"]
    seg_mass = sum(m["ffm"] + m["fat"] for m in (g["seg"][s] for s in SEGMENTS))
    out["latest"] = {
        "day": _iso(g["day"]), "time": t.strftime("%H:%M") if t else None, "rows": rows,
        # The widest gap between two segments, for the map's one sentence.
        "spread": {"hi": hi["key"], "lo": lo["key"],
                   "points": round(hi["fat_pct"] - lo["fat_pct"], 1)},
        # The five segments add up to the whole body: shown so the reader can
        # see it, not just take it on trust.
        "segments_mass": round(seg_mass * kg, 1),
        "weight": round(w["weight"] * kg, 1) if "weight" in w else None,
        # Withings' own whole-body bone, beside the derived one.
        "bone_measured": round(w["bone"] * kg, 2) if "bone" in w else None}

    per_scan = [(x["day"], x["algo"], {k: region_comp(x, k)["fat_pct"] for k, _ in REGIONS})
                for x in scans]
    steps = algo_steps(per_scan)
    out["era"] = scans[0]["day"].isoformat()
    out["algo"] = steps
    # The whole body's history can cross older scales too, so Section 4
    # marks its own changes.
    whole_days: dict[date, tuple] = {}
    for x in groups:
        c = region_comp(x, "whole")
        if c is not None and x["day"] not in whole_days:
            whole_days[x["day"]] = (x["day"], x["algo"], {"whole": c["fat_pct"]})
    out["algo_whole"] = algo_steps([whole_days[d] for d in sorted(whole_days)])
    out["change"] = {name: fat_change(scans, today, days, steps)
                     for name, days in CHANGE_WINDOWS.items()}
    # Section 3: one axis for all six regions over the segmental era.
    era_vals = [region_comp(x, k)["fat_pct"] for x in scans for k, _ in REGIONS]
    out["shared_axis"] = lane_axis(era_vals)
    spread = {x["day"]: round(max(v for k, v in p.items() if k != "whole")
                              - min(v for k, v in p.items() if k != "whole"), 2)
              for x, (_, _, p) in zip(scans, per_scan)}
    out["spread"] = {"series": dense(spread), "weekly": bucket_mean(spread, 7),
                     "long": bucket_mean(spread, LONG_BUCKET_DAYS),
                     "axis": lane_axis(list(spread.values())),
                     "first": round(spread[scans[0]["day"]], 1), "last": round(spread[g["day"]], 1)}
    bones = [region_comp(x, k)["bone_pct"] for x in scans for k, _ in REGIONS]
    out["bone_range"] = [round(min(bones), 1), round(max(bones), 1)]
    # Section 4's headline: the latest full scan against each region's own
    # first reading, which for the whole body may predate the segments.
    first = {R["key"]: R for R in regions}
    out["since"] = {}
    for key, _ in REGIONS:
        c, R = region_comp(g, key), first[key]
        out["since"][key] = {
            m: {"day": R["first"][m], "first": R["first_value"][m],
                "change": round(c[m] - R["first_value"][m], 1) + 0.0   # never -0.0
                if R["first_value"][m] is not None else None}
            for m in ("fat_pct", "muscle_pct")}
    return out


# ---------------------------------------------------------------- training

def strength_records(conn: sqlite3.Connection, tz: ZoneInfo, lb: float = 1.0) -> list[dict]:
    """Every Tonal workout and every Garmin strength activity, in local time.
    Tonal volume is stored in lb; `lb` is the factor to the display unit."""
    recs = []
    for r in conn.execute(
        "SELECT begin_utc, local_date, title, duration_s, total_volume, set_count "
        "FROM tonal_workout WHERE local_date IS NOT NULL"
    ):
        start = _local(r["begin_utc"], tz)
        volume = r["total_volume"]
        recs.append({"src": "tonal", "day": _day(r["local_date"]), "start": start,
                     "dur_s": r["duration_s"] or 0, "title": r["title"],
                     "volume": None if volume is None else round(volume * lb, 1),
                     "sets": r["set_count"]})
    for r in conn.execute(
        "SELECT start_local, local_date, name, elapsed_s FROM garmin_activity "
        "WHERE sub_sport = ?", (GARMIN_STRENGTH,)
    ):
        start = _local(r["start_local"], tz)
        day = start.date() if start else _day(r["local_date"])
        if day is None:
            continue
        recs.append({"src": "garmin", "day": day, "start": start,
                     "dur_s": r["elapsed_s"] or 0, "title": r["name"]})
    return recs


def merge_sessions(records: list[dict], gap: timedelta = SESSION_GAP) -> list[dict]:
    """Group Tonal workouts and Garmin strength activities into sessions.

    The watch logs most Tonal sessions as well, and one session can be
    several Tonal records, so counting records would double-count. Records
    that overlap, or sit within `gap` of each other, are one session. A
    record with no usable time joins the first session on its day.

    `kind` says what the session is known from: 'tonal' when a Tonal record
    in it has sets or volume, 'garmin' when only the watch recorded strength
    work. A session made only of zero-set Tonal records - a stretch or a
    mobility routine on its own - has kind None and is not counted.
    """
    sessions: list[dict] = []
    for r in sorted((r for r in records if r["start"]), key=lambda r: r["start"]):
        end = r["start"] + timedelta(seconds=r["dur_s"])
        cur = sessions[-1] if sessions else None
        if cur and r["start"] <= cur["end"] + gap:
            cur["records"].append(r)
            cur["end"] = max(cur["end"], end)
        else:
            sessions.append({"day": r["start"].date(), "start": r["start"], "end": end,
                             "records": [r]})
    for r in records:
        if r["start"]:
            continue
        same = next((s for s in sessions if s["day"] == r["day"]), None)
        if same:
            same["records"].append(r)
        else:
            sessions.append({"day": r["day"], "start": None, "end": None, "records": [r]})
    for s in sessions:
        if any(r["src"] == "tonal" and ((r.get("sets") or 0) > 0 or (r.get("volume") or 0) > 0)
               for r in s["records"]):
            s["kind"] = "tonal"
        elif any(r["src"] == "garmin" for r in s["records"]):
            s["kind"] = "garmin"
        else:
            s["kind"] = None
    sessions.sort(key=lambda s: (s["day"], s["start"] or datetime.min))
    return sessions


def training_buckets(sessions: list[dict], records: list[dict], first: date,
                     last: date, days: int = 7) -> dict:
    """Sessions per bucket by provenance, and Tonal volume per bucket. A bucket
    is a Monday-to-Sunday week (days=7) or a single day (days=1).

    Volume is None for a bucket without a Tonal workout. It is never filled
    in from Garmin: watch minutes and Tonal pounds measure different things.
    """
    align = _monday if days == 7 else (lambda d: d)
    b0 = align(first)
    n = (align(last) - b0).days // days + 1
    tonal, garmin = [0] * n, [0] * n
    volume: list[float | None] = [None] * n

    def bucket(d: date) -> int | None:
        i = (align(d) - b0).days // days
        return i if 0 <= i < n else None

    for s in sessions:
        i = bucket(s["day"])
        if s["kind"] and i is not None:
            (tonal if s["kind"] == "tonal" else garmin)[i] += 1
    for r in records:
        i = bucket(r["day"]) if r["src"] == "tonal" and r["day"] else None
        if i is not None and r["volume"] is not None:
            volume[i] = round((volume[i] or 0) + r["volume"], 1)
    return {"start": b0.isoformat(), "days": days, "tonal": tonal, "garmin": garmin,
            "volume": volume}


def training_days(sessions: list[dict]) -> dict[date, int]:
    """1 for a day with a Tonal session, 2 for a day with only watch strength."""
    out: dict[date, int] = {}
    for s in sessions:
        if s["kind"] == "tonal":
            out[s["day"]] = 1
        elif s["kind"] == "garmin":
            out.setdefault(s["day"], 2)
    return out


def day_training(conn: sqlite3.Connection, sessions: list[dict], day: date) -> dict:
    recs = [r for s in sessions if s["day"] == day for r in s["records"]]
    other = [{"sport": r["sport"], "title": r["name"], "dur_s": r["elapsed_s"]}
             for r in conn.execute(
                 "SELECT sport, name, elapsed_s FROM garmin_activity "
                 "WHERE local_date = ? AND COALESCE(sub_sport, '') <> ? ORDER BY start_local",
                 (day.isoformat(), GARMIN_STRENGTH))]
    # What the day counts as, by the same rule the training charts use:
    # 'tonal', 'garmin' (watch-only strength), or None (no strength session).
    kind = {1: "tonal", 2: "garmin"}.get(training_days(sessions).get(day))
    return {"day": day.isoformat(),
            "kind": kind,
            "tonal": [{"title": r["title"], "volume": r["volume"], "sets": r["sets"],
                       "dur_s": r["dur_s"]} for r in recs if r["src"] == "tonal"],
            "garmin": [{"title": r["title"], "dur_s": r["dur_s"]}
                       for r in recs if r["src"] == "garmin"],
            "other": other}


# ------------------------------------------------------------------- tonal

# Two days of one movement more than this far apart are not joined, so a
# month without the movement is a gap in its line, never a segment.
JOIN_DAYS = 31
# Workout titles listed under an unnamed movement, to help recognise it.
UNKNOWN_TITLES = 4
# Each Tonal chart is scaled to its own history. Its y range is never
# narrower than the larger of these, so a movement whose whole history sits
# within a few pounds isn't zoomed until session-to-session noise fills the
# frame: 10 lb, or a quarter of the movement's heaviest load.
AXIS_MIN_SPAN_LB = 10.0
AXIS_MIN_SPAN_FRAC = 0.25
# Room above and below the data, as a share of the range.
AXIS_PAD = 0.08
# A range whose bottom would sit this close to zero (as a share of the
# range) starts at zero instead: there's nothing to gain from a break.
AXIS_ZERO_SNAP = 0.2
# Tonal's motors top out at 100 lb per arm. Real exports bear that out: no
# set's average weight is above 100.0, and sets do pile up exactly on it,
# so a movement's line flattening there is the machine, not the lifter. The
# detail chart draws this ceiling once a movement's loads come within
# CAP_NEAR of it; below that it would only squash the chart.
TONAL_MAX_LB_PER_ARM = 100.0
CAP_NEAR = 0.85


def load_axis(loads: list[float], lb: float = 1.0) -> list[float]:
    """[bottom, top] for one movement's chart, in the display unit.

    Scaled to the movement's own loads, padded, and never narrower than
    AXIS_MIN_SPAN_*. The bottom is usually above zero, which makes a small
    gain look large, so the page marks such an axis as broken and labels
    where it starts. It is never below zero."""
    lo, hi = min(loads), max(loads)
    span = max(hi - lo, AXIS_MIN_SPAN_LB * lb, AXIS_MIN_SPAN_FRAC * hi)
    mid = (lo + hi) / 2
    lo, hi = mid - span / 2, mid + span / 2
    if lo < 0:
        lo, hi = 0.0, hi - lo
    pad = span * AXIS_PAD
    lo, hi = lo - pad, hi + pad
    if lo <= AXIS_ZERO_SNAP * (hi - lo):
        lo = 0.0
    # Whole units, so the labelled bottom of the axis reads cleanly.
    return [float(math.floor(lo)), float(math.ceil(hi))]


def quarter_start(d: date) -> date:
    return date(d.year, 3 * ((d.month - 1) // 3) + 1, 1)


def _next_quarter(q: date) -> date:
    return date(q.year + (q.month == 10), (q.month + 2) % 12 + 1, 1)


def quarterly_load(pts: list[list], through: date | None = None) -> dict | None:
    """The Tonal matrix row for one movement: each calendar quarter's median
    working load, from its first quarter to `through`'s quarter (default: its
    last day's).

    A cell is [median, days, level]. `median` is the median of that
    quarter's day loads (each already the day's median working set);
    `days` is how many days the movement was done. A quarter it wasn't done
    is [None, 0, None] - absence, which the page draws as an outline, never
    as a low value. [None, n, None] means done on n days, none with weight.
    `level` places the median within this row's own range (0 lowest, 1
    highest; 0.5 when the row has one value), so it is not comparable
    between rows."""
    days = [(date.fromisoformat(p[0]), p[1]) for p in pts]
    if not days:
        return None
    q0 = quarter_start(days[0][0])
    last = quarter_start(max(through or days[-1][0], days[-1][0]))
    quarters: list[date] = [q0]
    while quarters[-1] < last:
        quarters.append(_next_quarter(quarters[-1]))
    index = {q: i for i, q in enumerate(quarters)}
    loads: list[list[float]] = [[] for _ in quarters]
    done = [0] * len(quarters)
    for d, load in days:
        i = index[quarter_start(d)]
        done[i] += 1
        if load is not None:
            loads[i].append(load)
    medians = [round(statistics.median(v), 1) if v else None for v in loads]
    present = [m for m in medians if m is not None]
    lo, hi = (min(present), max(present)) if present else (None, None)
    def level(m):
        if m is None:
            return None
        return 0.5 if hi == lo else round((m - lo) / (hi - lo), 3)
    return {"start": q0.isoformat(), "range": [lo, hi],
            "cells": [[m, n, level(m)] for m, n in zip(medians, done)]}


# The page's window choices, which the lanes are scaled to. They must match
# WINS in dashboard.html (the tests check).
WINDOWS = {"30d": 30, "90d": 90, "1y": 365, "3y": 1095, "All": None}
# A lane's range is its readings' 2nd to 98th percentile, nearest rank,
# never trimming fewer than LANE_TRIM_MIN readings from each end once a
# window has LANE_TRIM_FROM of them...
LANE_PCT = 0.02
LANE_TRIM_MIN = 1
LANE_TRIM_FROM = 5
# ...then a trimmed reading within LANE_KEEP of that range's span is taken
# back in, so an ordinary best night isn't pushed off the edge; one further
# out is drawn at the edge as off-scale. What's kept is padded by LANE_PAD.
LANE_KEEP = 1.0
LANE_PAD = 0.10


def _lane_step(span: float) -> float:
    """The unit a lane's ends are rounded out to, so their labels read cleanly."""
    return 0.5 if span < 6 else 1.0 if span < 40 else 5.0


def lane_axis(values: list[float]) -> dict | None:
    """{lo, hi, out} for a Trends lane, from the readings in the window shown.

    Robust, so one bad reading can't stretch the scale: the core is the
    2nd-98th percentile (at least one reading trimmed from each end). A
    trimmed reading within LANE_KEEP of the core still fits, and what fits
    is padded by LANE_PAD;
    `out` counts the readings beyond the final range, which the page draws
    as a marker on the edge rather than letting them set the scale. The
    bottom never goes below zero for a measure that can't. No snap to zero:
    the page labels both ends and marks an axis that doesn't start there."""
    v = sorted(x for x in values if x is not None)
    if not v:
        return None
    n = len(v)
    k = max(LANE_TRIM_MIN, math.floor(LANE_PCT * n)) if n >= LANE_TRIM_FROM else 0
    lo, hi = v[k], v[n - 1 - k]
    span = (hi - lo) or max(abs(hi) * 0.05, 1.0)
    keep_lo, keep_hi = lo - LANE_KEEP * span, hi + LANE_KEEP * span
    lo = min([lo] + [x for x in v[:k] if x >= keep_lo])
    hi = max([hi] + [x for x in v[n - k:] if x <= keep_hi])
    pad = LANE_PAD * ((hi - lo) or span)
    lo, hi = lo - pad, hi + pad
    if v[0] >= 0:
        lo = max(lo, 0.0)
    step = _lane_step(hi - lo)
    lo, hi = math.floor(lo / step) * step, math.ceil(hi / step) * step
    return {"lo": lo, "hi": hi, "out": sum(1 for x in v if x < lo or x > hi)}


def lane_axes(points: dict[date, float], today: date) -> dict:
    """One lane_axis per window, each from the readings in the days that
    window shows (the days ending today; All is every reading)."""
    return {name: lane_axis(list((window(points, today, days) if days else points).values()))
            for name, days in WINDOWS.items()}


def promote_key(m: dict) -> tuple:
    """Sort key for the detail chart's default movement: most sets first,
    then the earliest first day with a weight."""
    return (-m["sets"], next(p[0] for p in m["pts"] if p[1] is not None))


def movement_history(conn: sqlite3.Connection, lb: float = 1.0,
                     today: date | None = None) -> dict | None:
    """The Tonal tab: each movement's working load, one point per day it was
    done in any routine, plus each routine's newest session.

    Working weight is the median avg weight of that day's sets of the
    movement, leaving out warm-up sets and sets with no weight:
    - Warm-ups: Tonal's warmUp flag is rare, but it is accurate where set
      (flagged sets are clearly lighter than the same movement's later sets
      in that workout), and checked against a real history it hides very
      little: unflagged warm-ups are rare enough that inferring them from
      weight would cost more, in wrongly dropped working sets, than it
      buys. So the flag is used as it is. Don't add a rule that guesses
      warm-ups from weight.
    - No weight: off-machine and bodyweight moves are performed sets that
      carry none. In the median they would drag a movement toward zero and
      read as a decline. A movement with no weighted set at all
      gets no line; the page lists it as "no weight recorded".
    Sets come from v_tonal_set_both_sides, so the two sides of a two-sided
    movement are one set at their shared prescribed weight. Side 0 always
    goes first, so a per-side figure would mix the limb with the order;
    none is drawn.

    The load is TOTAL load. Where both arms pull together
    (tonal_movement.arms_together = 1) avg weight is per arm, so it is
    doubled, and reads double the per-arm figure Tonal's screen shows. For a
    movement no file names, arms_together is unknown: it is plotted as
    recorded, undoubled, and the page says so. `lb` converts Tonal's pounds
    to the display unit.

    A point is a day, not a workout, because the chart's axis is days: a
    movement done in two workouts on one day is one point.
    """
    meta = {r["movement_id"]: r for r in conn.execute(
        "SELECT movement_id, name, arms_together FROM tonal_movement")}
    days: dict[str, dict[date, dict]] = {}
    titles_seen: dict[str, dict[str | None, int]] = {}
    workouts: dict[str, dict] = {}
    # Performed sets per movement, in every routine (the view drops sets
    # never begun, and counts a two-sided set once).
    set_count: dict[str, int] = {}
    for r in conn.execute(
        "SELECT b.workout_id, b.day, b.movement_id, b.avg_weight_lb, b.warm_up, "
        "       w.title, w.begin_utc "
        "FROM v_tonal_set_both_sides b JOIN tonal_workout w ON w.workout_id = b.workout_id "
        "WHERE b.movement_id IS NOT NULL "
        "ORDER BY w.begin_utc, w.workout_id, b.begin_utc"
    ):
        d, mid = _day(r["day"]), r["movement_id"]
        if d is None:
            continue
        cell = days.setdefault(mid, {}).setdefault(d, {"weights": [], "title": r["title"]})
        set_count[mid] = set_count.get(mid, 0) + 1
        if (r["avg_weight_lb"] or 0) > 0 and not r["warm_up"]:
            cell["weights"].append(r["avg_weight_lb"])
        wk = workouts.setdefault(r["workout_id"], {"title": r["title"], "begin": r["begin_utc"],
                                                   "day": d, "moves": [], "counted": set()})
        if mid not in wk["moves"]:
            wk["moves"].append(mid)      # in the order first performed
        if mid not in wk["counted"]:
            wk["counted"].add(mid)
            t = titles_seen.setdefault(mid, {})
            t[r["title"]] = t.get(r["title"], 0) + 1
    if not workouts:
        return None

    latest: dict[str | None, dict] = {}
    count: dict[str | None, int] = {}
    for wid, wk in workouts.items():
        key = (wk["begin"] or "", wid)
        count[wk["title"]] = count.get(wk["title"], 0) + 1
        if wk["title"] not in latest or key > latest[wk["title"]]["key"]:
            latest[wk["title"]] = {**wk, "key": key}
    routines = [{"title": t, "last": w["day"].isoformat(), "sessions": count[t],
                 "movements": w["moves"]}
                for t, w in sorted(latest.items(), key=lambda kv: kv[1]["key"], reverse=True)]
    titles = [r["title"] for r in routines]
    index = {t: i for i, t in enumerate(titles)}

    movements: dict[str, dict] = {}
    for mid, by_day in days.items():
        m = meta.get(mid)
        arms = m["arms_together"] if m else None
        factor = (2 if arms == 1 else 1) * lb
        pts, prev = [], None
        for d in sorted(by_day):
            ws = by_day[d]["weights"]
            load = round(statistics.median(ws) * factor, 1) if ws else None
            joined = int(prev is not None and load is not None and prev[1] is not None
                         and (d - prev[0]).days <= JOIN_DAYS)
            pts.append([d.isoformat(), load, len(ws), joined, index[by_day[d]["title"]]])
            prev = (d, load)
        loads = [p[1] for p in pts if p[1] is not None]
        # The machine's ceiling in this chart's terms: total load, so doubled
        # where both arms pull together, and as recorded where that's unknown.
        cap = round(TONAL_MAX_LB_PER_ARM * factor, 1)
        near = bool(loads) and max(loads) >= CAP_NEAR * cap
        newest = next((p for p in reversed(pts) if p[1] is not None), None)
        seen = sorted(titles_seen.get(mid, {}).items(), key=lambda kv: (-kv[1], str(kv[0])))
        movements[mid] = {
            "name": m["name"] if m else None,
            "short": mid[:8],
            "arms": arms,
            "sessions": len(pts),
            "sets": set_count.get(mid, 0),
            "first": pts[0][0], "last": pts[-1][0],
            "weighted": bool(loads),
            # Near the ceiling, the axis reaches it so the line can be drawn.
            "y": (load_axis(loads + [cap] if near else loads, lb) if loads else None),
            "cap": cap if near else None,
            # The newest day with a weight, and that weight.
            "current": [newest[0], newest[1]] if newest else None,
            "quarters": quarterly_load(pts, today) if loads else None,
            # The routines an unnamed movement turns up in, to recognise it by.
            "titles": [t for t, _ in seen[:UNKNOWN_TITLES]] if not m or not m["name"] else [],
            "pts": pts if loads else [],
        }
    # The detail chart opens on the routine's movement with the most sets
    # over its whole history - the one done most is the one cared about -
    # then the earliest first day with a weight. Counted over the whole
    # history, not this routine's sessions: a routine done once ties every
    # movement. The first movement performed is often a new one: one dot.
    for r in routines:
        weighted = [m for m in r["movements"] if movements[m]["weighted"]]
        r["promote"] = min(weighted, key=lambda m: promote_key(movements[m]), default=None)
    return {"routines": routines, "titles": titles, "movements": movements,
            "join_days": JOIN_DAYS}


# ------------------------------------------------------------------ health

def data_health(conn: sqlite3.Connection, cfg, today: date) -> dict:
    """The guard's own staleness verdict per source, plus the last run's status."""
    last = {r["source"]: r for r in conn.execute(
        "SELECT source, status FROM sync_runs "
        "WHERE run_id IN (SELECT MAX(run_id) FROM sync_runs GROUP BY source)")}
    sources = []
    for s in SOURCES:
        limit = int(getattr(cfg, s).get("stale_after_days", guard.STALE_AFTER_DAYS[s]))
        f = guard.freshness(conn, s, today=today, stale_after_days=limit)
        sources.append({"source": s, "newest": _iso(f.newest), "through": _iso(f.through),
                        "age_days": f.age_days, "limit_days": limit, "stale": f.stale,
                        "last_status": last[s]["status"] if s in last else None})
    synced = conn.execute(
        "SELECT MAX(COALESCE(finished_utc, started_utc)) FROM sync_runs").fetchone()[0]
    return {"synced_utc": synced, "synced_local": _fmt_local(synced, cfg.timezone),
            "sources": sources}


# ------------------------------------------------------------------- daily

def moved(rows: list[dict]) -> list[dict]:
    """The rows at least MOVED_SD from their own 30-day mean, largest first.
    Usually empty, and then empty is the answer: nothing is added to fill it.
    Each keeps the `day` its row carries, so a moved reading is never undated."""
    out = [{"key": m["key"], "label": m["label"], "unit": m["unit"], "value": m["value"],
            "z": m["z"], "mean": m["mean"], "day": m.get("day")}
           for m in rows if m["z"] is not None and abs(m["z"]) >= MOVED_SD]
    out.sort(key=lambda m: -abs(m["z"]))
    return out


def deviation(row: dict, cap_sd: float = DEV_CAP_SD) -> dict | None:
    """Where a reading sits on its Today-tab track: its distance from its own
    30-day median, as a share of the half-track (cap_sd of its 30-day SDs).
    `x` is clipped to -1..1 and `clipped` says it was. None without a
    reading, a median or a spread to scale by: the page draws no bar then."""
    v, med, sd = row.get("value"), row.get("median"), row.get("sd")
    if v is None or med is None or not sd:
        return None
    x = (v - med) / (cap_sd * sd)
    return {"x": round(max(-1.0, min(1.0, x)), 4), "clipped": abs(x) > 1}


def strip_axis(hours: list[float | None], median: float | None) -> dict | None:
    """The hours the strip's bars run between: {lo, hi}.

    Nights cluster between about 6 and 8 h, and bars from zero make them all
    look alike. So with a median to draw, the bars start from a floor under
    the usual nights: under every night within STRIP_REACH_H of the median,
    by STRIP_FLOOR of their span, which leaves the shortest of them about a
    third of the height. A night further short than that doesn't drag the
    floor back to zero. If it lands below the floor, the page draws a
    chevron at the bottom instead of a bar (`below` on the night). The top
    always fits the longest night. The span is never under
    STRIP_MIN_SPAN_H, so a quiet fortnight isn't blown up.

    Bars that don't start at zero are readable only against the median line.
    With no median there's no line to read them by, so the floor is zero:
    one never ships without the other."""
    v = [h for h in hours if h is not None]
    if not v:
        return None
    if median is None:
        return {"lo": 0.0, "hi": max(v)}
    hi = max(v + [median])
    low = min([median] + [x for x in v if x >= median - STRIP_REACH_H])
    span = max(hi - low, STRIP_MIN_SPAN_H)
    return {"lo": max(0.0, math.floor((low - STRIP_FLOOR * span) / 0.5) * 0.5), "hi": hi}


def sleep_strip(sleep_h: dict[date, float], trained: dict[date, int], today: date,
                latest: date | None, median: float | None = None,
                nights: int = STRIP_NIGHTS) -> dict:
    """The header's context strip: hours slept on each of the `nights` nights
    ending today, oldest first. A night with no record is None - the page
    leaves a gap there, never a zero bar.

    `trained` is a day with a counted strength session (Tonal or watch-only).
    Its dash goes under the night with the same date. Garmin dates a night by
    the morning it ended, so that is the night BEFORE the session. On real
    data, all but a vanishing few nights end on their own date, and no
    session starts between midnight and dawn, so the pairing holds.

    `latest` is the night the Today tab describes, which the page picks out.
    `median` is that night's 30-day median sleep, the same figure Today shows
    beside it. The page draws it as a line across the strip."""
    days = [today - timedelta(days=k) for k in range(nights - 1, -1, -1)]
    hours = [sleep_h.get(d) for d in days]
    axis = strip_axis(hours, median)
    return {"end": today.isoformat(), "latest": _iso(latest),
            "median_h": round(median, 2) if median is not None and axis else None,
            "lo_h": axis["lo"] if axis else None, "hi_h": axis["hi"] if axis else None,
            "nights": [{"day": d.isoformat(), "h": h, "trained": d in trained,
                        "below": bool(axis and h is not None and h < axis["lo"])}
                       for d, h in zip(days, hours)]}


NIGHT = (("sleep_h", "Sleep", "h"), ("sleep_score", "Sleep score", ""),
         ("rhr", "Resting HR", "bpm"), ("bb_min", "Battery low", ""),
         ("bb_max", "Battery high", ""))
# The night's own readings. The rest of NIGHT are readings of the whole day.
NIGHT_SLEEP = ("sleep_h", "sleep_score")


def night_row_days(g: dict, night_day: date) -> dict[str, date]:
    """The day each NIGHT row reads. Usually the night's own day for every row.

    The mid-morning sleep catch-up loads the night ending this morning hours before
    the rest of that day exists, so the day holds only sleep. Then resting HR
    and body battery come from the day before, the last day the nightly
    loaded, and their rows carry that day. A full day that lacks one of them
    still shows it missing: only a sleep-only day reaches back."""
    day_keys = [k for k, _, _ in NIGHT if k not in NIGHT_SLEEP]
    sleep_only = not any(night_day in g[k] for k in day_keys)
    back = night_day - timedelta(days=1)
    return {k: back if sleep_only and k in day_keys else night_day for k, _, _ in NIGHT}


def daily_view(conn: sqlite3.Connection, today: date, g: dict, w: dict,
               sessions: list[dict], mass_unit: str = "kg") -> dict:
    # Garmin dates sleep by the morning it ended, so last night is today's row.
    night_day = max((d for k in GARMIN_KEYS for d in g[k] if d <= today), default=None)
    night = []
    if night_day:
        row_days = night_row_days(g, night_day)
        for key, label, unit in NIGHT:
            row = {"key": key, "label": label, "unit": unit, "day": row_days[key].isoformat(),
                   **compare(g[key], row_days[key])}
            row["dev"] = deviation(row)
            night.append(row)

    weigh_day = max((d for d in w["weight"] if d <= today), default=None)
    morning = None
    if weigh_day:
        morning = {"day": weigh_day.isoformat(), "time": w["taken"].get(weigh_day), "rows": []}
        for key, label, unit, pts in (("weight", "Weight", mass_unit, w["weight"]),
                                      ("fat", "Body fat", "%", w["fat"])):
            morning["rows"].append({
                "key": key, "label": label, "unit": unit, "day": weigh_day.isoformat(),
                **compare(pts, weigh_day),
                "avg7": average(pts, weigh_day, 7), "avg30": average(pts, weigh_day, 30)})

    rows = night + (morning["rows"] if morning else [])
    return {"today": today.isoformat(),
            "night": {"day": _iso(night_day), "rows": night},
            "training": day_training(conn, sessions, today - timedelta(days=1)),
            "morning": morning,
            "moved": moved(rows),
            # How many readings had a baseline to be tested against. With
            # none, an empty "moved" means nothing could be checked, and the
            # page says that instead of "nothing moved".
            "moved_checked": sum(1 for r in rows if r["z"] is not None),
            "strip": sleep_strip(g["sleep_h"], training_days(sessions), today, night_day,
                                 median=next((r["median"] for r in night if r["key"] == "sleep_h"), None))}


# ----------------------------------------------------------------- payload

def build_payload(conn: sqlite3.Connection, cfg, *, today: date,
                  built_utc: str | None = None) -> dict:
    tz = cfg.timezone
    mass_unit = MASS_UNIT[cfg.units]
    kg = mass_factor("kg", cfg.units)
    g = garmin_series(conn)
    w = weigh_ins(conn, tz)
    w["weight"] = scaled(w["weight"], kg)
    records = strength_records(conn, tz, lb=mass_factor("lb", cfg.units))
    sessions = merge_sessions(records)
    counted = [s for s in sessions if s["kind"]]
    # Converted before balance_stats, so its mean and noise come out in the
    # display unit too.
    seg = {k: scaled(v, kg) for k, v in segmental_balance(conn).items()}
    built_utc = built_utc or db.utc_now()

    training = None
    if counted:
        training = {"weekly": training_buckets(sessions, records, counted[0]["day"], today, 7),
                    "daily": training_buckets(sessions, records, counted[0]["day"], today, 1)}

    annotations = [
        {"source": r["source"], "start": r["start_date"], "end": r["end_date"],
         "note": r["note"],
         # The row's own label, or a generic one: a note is usually too long
         # to draw over the gap.
         "label": r["label"] or f"no {r['source']} data expected"}
        for r in conn.execute(
            "SELECT source, start_date, end_date, note, label FROM annotations "
            "ORDER BY start_date")]

    daily_points = {"sleep_h": g["sleep_h"], "rhr": g["rhr"], "weight": w["weight"],
                    "fat_pct": w["fat"], "arms": seg["arms"], "legs": seg["legs"]}
    return {
        "built": {"utc": built_utc, "local": _fmt_local(built_utc, tz),
                  "today": today.isoformat(), "tz": cfg.tz_name},
        "units": {"system": cfg.units, "mass": mass_unit},
        "health": data_health(conn, cfg, today),
        "daily": daily_view(conn, today, g, w, sessions, mass_unit),
        "moved_sd": MOVED_SD,
        "dev_cap_sd": DEV_CAP_SD,
        "baseline_days": BASELINE_DAYS,
        "series": {**{k: dense(v) for k, v in daily_points.items()},
                   "train_days": dense(training_days(sessions))},
        # The page draws these instead when a window has too many days for
        # one mark each.
        "weekly": {k: weekly_mean(v) for k, v in daily_points.items()},
        "training": training,
        "balance": {"arms": balance_stats(seg["arms"]), "legs": balance_stats(seg["legs"])},
        "bodyscan": body_scan(conn, tz, kg, today),
        "tonal": movement_history(conn, lb=mass_factor("lb", cfg.units), today=today),
        # Each Trends lane's y range for each window, from the readings that
        # window shows: the scale follows the window, robust to a bad reading.
        "lanes": {k: lane_axes(daily_points[k], today) for k in ("sleep_h", "rhr", "weight", "fat_pct")},
        "annotations": annotations,
    }


def render(payload: dict) -> str:
    template = TEMPLATE.read_text(encoding="utf-8")
    if template.count(PLACEHOLDER) != 1:
        raise ValueError(f"{TEMPLATE.name} must contain {PLACEHOLDER} exactly once")
    # Escaping every '<' keeps a workout title like "</script>" from ending
    # the data block early; JSON.parse turns < back into '<'.
    data = json.dumps(payload, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).replace("<", "\\u003c")
    return template.replace(PLACEHOLDER, data)


def snapshot_path(cfg, snapshot: str | Path | None = None) -> Path:
    """The read-only snapshot sync_all writes beside wellness.db."""
    return Path(snapshot).expanduser() if snapshot else cfg.wellness_db.with_name("wellness-ro.db")


def build(cfg, *, today: date | None = None, snapshot: str | Path | None = None,
          out: str | Path | None = None) -> Path:
    """Read the snapshot, write the page. Never opens the live database."""
    snap = snapshot_path(cfg, snapshot)
    if not snap.exists():
        raise SystemExit(f"No snapshot at {snap}. Run: python3 sync_all.py snapshot")
    conn = db.connect(snap, read_only=True)
    try:
        payload = build_payload(conn, cfg, today=today or datetime.now(cfg.timezone).date())
    finally:
        conn.close()
    target = Path(out).expanduser() if out else cfg.dashboard_dir / "index.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    # Written beside the target and swapped in, so a static server never
    # serves half a page.
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(render(payload), encoding="utf-8")
    os.replace(tmp, target)
    return target
