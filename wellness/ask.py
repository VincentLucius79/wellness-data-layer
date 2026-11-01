"""Query catalog: a fixed set of questions the agent can ask about the data.

    python3 sync_all.py ask list
    python3 sync_all.py ask metric-window metric=sleep_hours days=30
    scripts/ask.sh <query> [key=value ...]     # what the agent calls

The agent runs a small local model that is unreliable at SQL and arithmetic,
and a plausible wrong answer about health data is worse than none. So code
computes and the model phrases: it picks a query, and gets back numbers to
quote, never rows to add up.

Rules every answer follows:
  * Every number carries the dates it covers: `from`/`to` on each window,
    a date on each single reading.
  * Missing days are counted and reported, never filled.
  * Too little data is said in words (`insufficient`), not computed anyway.
  * Statistics come from dashboard.py and freshness from guard.py, so an
    answer can't disagree with the page or the brief.
  * Small output: aggregates and short series. It goes into a 33k context.

The caller is assumed hostile: arguments come from an agent that may have
been prompt-injected. parse() takes only an allowlisted query name and
key=value pairs that match this catalog exactly, checks every value's type
and range, and never repeats input back. Nothing a caller passes reaches SQL:
every statement here is a fixed string, and values are compared in Python.
scripts/ask.sh checks the same shape again before it starts Python.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from . import config, dashboard, db, guard

OK, UNAVAILABLE, REJECTED = 0, 2, 64      # 64: EX_USAGE, bad input
MAX_ARGS = 8
EARLIEST = date(2000, 1, 1)
# Statistics need at least this many readings, and at least half the window
# (dashboard.needed). A mean of three nights is not a sleep average.
MIN_POINTS = 5
# Each side of a two-group comparison needs at least this many readings.
MIN_GROUP = 10
# Series longer than this many rows are left out, with a note saying so.
SERIES_MAX = 26
GAPS_MAX = 10
# A day-by-day list is included for windows up to this long.
DAILY_MAX = 14
# Larger answers are refused, rather than flooding the model's context.
MAX_OUTPUT = 8000

KEY_RE = re.compile(r"[a-z_]{1,16}")
VALUE_RE = re.compile(r"[a-z0-9_-]{1,24}")


class Rejected(ValueError):
    """Bad input. The message never contains the input itself."""


# ----------------------------------------------------------------- metrics

@dataclass(frozen=True)
class Metric:
    key: str                # dashboard series key
    source: str
    unit: str | None        # None: the display mass unit
    dp: int
    about: str


METRICS = {
    "sleep_hours": Metric("sleep_h", "garmin", "h", 2,
                          "Garmin total sleep. A night is dated by the morning it ended."),
    "sleep_score": Metric("sleep_score", "garmin", "", 1, "Garmin sleep score, dated like sleep_hours."),
    "resting_hr": Metric("rhr", "garmin", "bpm", 1, "Garmin resting heart rate."),
    "body_battery_low": Metric("bb_min", "garmin", "", 1, "Garmin body battery, lowest of the day."),
    "body_battery_high": Metric("bb_max", "garmin", "", 1, "Garmin body battery, highest of the day."),
    "weight": Metric("weight", "withings", None, 1, "Withings, the first weigh-in of each day."),
    "body_fat": Metric("fat", "withings", "%", 1, "Withings body fat, the first reading of each day."),
}
RECOVERY = ("sleep_hours", "sleep_score", "resting_hr", "body_battery_low", "body_battery_high")


# ------------------------------------------------------------------ params

@dataclass(frozen=True)
class Int:
    lo: int
    hi: int

    def parse(self, raw: str) -> int:
        if not re.fullmatch(r"[1-9][0-9]{0,4}", raw) or not self.lo <= int(raw) <= self.hi:
            raise ValueError
        return int(raw)

    def describe(self) -> str:
        return f"integer {self.lo}-{self.hi}"


@dataclass(frozen=True)
class Choice:
    options: tuple[str, ...]

    def parse(self, raw: str) -> str:
        if raw not in self.options:
            raise ValueError
        return raw

    def describe(self) -> str:
        return "one of " + ", ".join(self.options)


@dataclass(frozen=True)
class Day:
    def parse(self, raw: str) -> date:
        # [0-9], not \d, which also matches other scripts' digits.
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", raw):
            raise ValueError
        d = date.fromisoformat(raw)
        if d < EARLIEST:
            raise ValueError
        return d

    def describe(self) -> str:
        return f"date YYYY-MM-DD, {EARLIEST.isoformat()} to today"


REQUIRED = object()


@dataclass(frozen=True)
class Param:
    kind: Int | Choice | Day
    default: object = REQUIRED
    about: str = ""


@dataclass
class Query:
    fn: object
    does: str
    params: dict[str, Param] = field(default_factory=dict)


METRIC = Param(Choice(tuple(METRICS)), about="which metric")
END = Param(Day(), None, "last day of the window, inclusive; default today")


def days_param(default: int, lo: int = 1) -> Param:
    return Param(Int(lo, 3650), default, "window length in days, ending on `end`")


def parse(argv: list[str]) -> tuple[str, dict]:
    """(query name, typed params) or Rejected."""
    if not argv:
        raise Rejected("no query given; run: ask list")
    name, args = argv[0], argv[1:]
    if name not in QUERIES:
        raise Rejected("unknown query; run: ask list")
    q = QUERIES[name]
    if len(args) > MAX_ARGS:
        raise Rejected(f"too many parameters (at most {MAX_ARGS})")
    params: dict = {}
    for arg in args:
        key, sep, raw = arg.partition("=")
        if not sep or not KEY_RE.fullmatch(key) or not VALUE_RE.fullmatch(raw):
            raise Rejected("parameters must be key=value, using lowercase letters, digits, - and _")
        if key not in q.params:
            allowed = ", ".join(q.params) or "none"
            raise Rejected(f"unknown parameter for {name}; allowed: {allowed}")
        if key in params:
            raise Rejected(f"{key} given twice")
        try:
            params[key] = q.params[key].kind.parse(raw)
        except ValueError:
            raise Rejected(f"{key} must be {q.params[key].kind.describe()}") from None
    for key, p in q.params.items():
        if key not in params:
            if p.default is REQUIRED:
                raise Rejected(f"{name} needs {key}: {p.kind.describe()}")
            params[key] = p.default
    return name, params


# ----------------------------------------------------------------- helpers

def _iso(d: date | None) -> str | None:
    return d.isoformat() if d else None


def _fmt(v: float, dp: int, unit: str) -> str:
    n = f"{v:.{dp}f}"
    return n + unit if unit == "%" else f"{n} {unit}" if unit else n


def _label(metric: str) -> str:
    return metric.replace("_", " ")


class Ctx:
    """One snapshot connection, with each series loaded once."""

    def __init__(self, conn: sqlite3.Connection, cfg, today: date):
        self.conn, self.cfg, self.today = conn, cfg, today
        self.mass = dashboard.MASS_UNIT[cfg.units]
        self._garmin = self._withings = self._training = None
        self._days: dict[str, set[date]] = {}

    def points(self, metric: str) -> dict[date, float]:
        m = METRICS[metric]
        if m.source == "garmin":
            if self._garmin is None:
                self._garmin = dashboard.garmin_series(self.conn)
            return self._garmin[m.key]
        if self._withings is None:
            w = dashboard.weigh_ins(self.conn, self.cfg.timezone)
            w["weight"] = dashboard.scaled(w["weight"], dashboard.mass_factor("kg", self.cfg.units))
            self._withings = w
        return self._withings[m.key]

    def unit(self, metric: str) -> str:
        u = METRICS[metric].unit
        return self.mass if u is None else u

    def training(self) -> tuple[list[dict], list[dict]]:
        if self._training is None:
            records = dashboard.strength_records(
                self.conn, self.cfg.timezone, lb=dashboard.mass_factor("lb", self.cfg.units))
            self._training = (records, dashboard.merge_sessions(records))
        return self._training

    def days(self, source: str) -> set[date]:
        if source not in self._days:
            self._days[source] = {d for (r,) in self.conn.execute(guard.DAYS_SQL[source])
                                  if (d := dashboard._day(r))}
        return self._days[source]

    def annotations(self, start: date, end: date) -> list[dict]:
        return [{"source": r["source"], "from": r["start_date"], "to": r["end_date"], "note": r["note"]}
                for r in self.conn.execute(
                    "SELECT source, start_date, end_date, note FROM annotations ORDER BY start_date")
                if r["start_date"] <= end.isoformat() and r["end_date"] >= start.isoformat()]


def _span(end: date, days: int) -> tuple[date, date]:
    return end - timedelta(days=days - 1), end


def _stats(pts: dict[date, float], start: date, end: date, dp: int) -> dict:
    """Window statistics, or `insufficient` in words when there's too little."""
    days = (end - start).days + 1
    n = len(pts)
    out = {"from": _iso(start), "to": _iso(end), "days": days,
           "days_present": n, "days_missing": days - n}
    need = max(MIN_POINTS, dashboard.needed(days))
    if n < need:
        longer = "; ask for a longer window" if need > days else ""
        out["insufficient"] = (f"{n} of {days} days have a reading; "
                               f"statistics need at least {need}{longer}")
        return out
    s = dashboard.summarize(list(pts.values()))
    out["mean"] = round(s["mean"], dp)
    out["median"] = round(s["median"], dp)
    # The most recent day holding the extreme value.
    for k in ("min", "max"):
        out[k] = {"value": round(s[k], dp),
                  "date": _iso(max(d for d, v in pts.items() if v == s[k]))}
    return out


def _diff(a: float, b: float, dp: int) -> float:
    """a - b as the two are shown, so the stated difference matches them."""
    return round(round(a, dp) - round(b, dp), dp)


def _direction(d: float) -> str:
    return "higher" if d > 0 else "lower" if d < 0 else "the same"


def _window_text(metric: str, s: dict, unit: str, dp: int) -> str:
    head = f"{_label(metric)}, {s['from']} to {s['to']}"
    if "insufficient" in s:
        return f"{head}: not enough data ({s['insufficient']})."
    return (f"{head}: mean {_fmt(s['mean'], dp, unit)}, median {_fmt(s['median'], dp, unit)}, "
            f"lowest {_fmt(s['min']['value'], dp, unit)} on {s['min']['date']}, "
            f"highest {_fmt(s['max']['value'], dp, unit)} on {s['max']['date']}, "
            f"from {s['days_present']} of {s['days']} days.")


def _in(records: list[dict], sessions: list[dict], start: date, end: date):
    return ([r for r in records if r["day"] and start <= r["day"] <= end],
            [s for s in sessions if s["day"] and start <= s["day"] <= end])


# ----------------------------------------------------------------- queries

def _describe(p: Param) -> str:
    if p.default is REQUIRED:
        return p.kind.describe() + "; required"
    return p.kind.describe() + f"; default {'today' if p.default is None else p.default}"


def q_list(ctx: Ctx | None) -> dict:
    return {
        "usage": "ask.sh <query> [key=value ...]",
        "rules": ["Quote numbers exactly as given; do not compute new ones.",
                  "Windows end on `end` (default today) and include it.",
                  "Missing days are counted, never filled in.",
                  "`insufficient` means there was too little data to answer."],
        "queries": {name: {"does": q.does,
                           "params": {k: _describe(p) for k, p in q.params.items()}}
                    for name, q in QUERIES.items()},
        "metrics": {name: {"unit": ctx.unit(name) if ctx else
                           (m.unit if m.unit is not None else "lb or kg, per [display] units"),
                           "about": m.about}
                    for name, m in METRICS.items()},
    }


def q_latest(ctx: Ctx) -> dict:
    rows, dates = [], []
    for metric, m in METRICS.items():
        pts = ctx.points(metric)
        day = max((d for d in pts if d <= ctx.today), default=None)
        if day is None:
            rows.append({"metric": metric, "value": None, "note": "no readings stored"})
            continue
        c = dashboard.compare(pts, day)
        dates.append(day)
        rows.append({"metric": metric, "date": _iso(day), "days_old": (ctx.today - day).days,
                     "value": round(c["value"], m.dp), "unit": ctx.unit(metric),
                     "median_30d": None if c["median"] is None else round(c["median"], m.dp)})
    return {"from": _iso(min(dates, default=None)), "to": _iso(max(dates, default=None)),
            "readings": rows,
            "note": "median_30d is the median of the 30 days before each reading's date; "
                    "null means fewer than 15 of those days have a reading."}


def q_day(ctx: Ctx, **p) -> dict:
    day = p["date"]
    rows, missing = {}, []
    for metric, m in METRICS.items():
        pts = ctx.points(metric)
        if day not in pts:
            missing.append(metric)
            continue
        c = dashboard.compare(pts, day)
        rows[metric] = {"value": round(c["value"], m.dp), "unit": ctx.unit(metric),
                        "median_30d": None if c["median"] is None else round(c["median"], m.dp)}
    _, sessions = ctx.training()
    t = dashboard.day_training(ctx.conn, sessions, day)
    vols = [w["volume"] for w in t["tonal"] if w["volume"] is not None]
    training = {"kind": t["kind"] or "none"}
    if t["kind"] == "tonal":
        training.update(tonal_volume=round(sum(vols)) if vols else None,
                        tonal_sets=sum(w["sets"] or 0 for w in t["tonal"]), unit=ctx.mass)
    elif t["kind"] == "garmin":
        training["garmin_strength_min"] = round(sum(w["dur_s"] or 0 for w in t["garmin"]) / 60)
    return {"from": _iso(day), "to": _iso(day), "readings": rows, "missing": missing,
            "training": training,
            "note": "Sleep on a date is the night that ended that morning."}


def q_metric_window(ctx: Ctx, metric: str, days: int, end: date | None) -> dict:
    m, unit = METRICS[metric], ctx.unit(metric)
    start, end = _span(end or ctx.today, days)
    pts = dashboard.window(ctx.points(metric), end, days)
    s = _stats(pts, start, end, m.dp)
    out = {"metric": metric, "unit": unit, **s}
    if days <= DAILY_MAX:
        out["daily"] = [[_iso(d), None if d not in pts else round(pts[d], m.dp)]
                        for d in (start + timedelta(days=i) for i in range(days))]
    out["summary"] = _window_text(metric, s, unit, m.dp)
    return out


def q_compare_windows(ctx: Ctx, metric: str, recent: int, prior: int, end: date | None) -> dict:
    m, unit = METRICS[metric], ctx.unit(metric)
    end = end or ctx.today
    r_start, r_end = _span(end, recent)
    p_start, p_end = _span(r_start - timedelta(days=1), prior)
    pts = ctx.points(metric)
    r = _stats(dashboard.window(pts, r_end, recent), r_start, r_end, m.dp)
    p = _stats(dashboard.window(pts, p_end, prior), p_start, p_end, m.dp)
    out = {"metric": metric, "unit": unit, "from": _iso(p_start), "to": _iso(r_end),
           "recent": r, "prior": p}
    if "insufficient" in r or "insufficient" in p:
        out["difference"] = None
        out["summary"] = (f"{_label(metric)}: no comparison, too little data in the "
                          + " and ".join(w for w, s in (("recent", r), ("prior", p))
                                         if "insufficient" in s) + " window.")
        return out
    dm, dmed = _diff(r["mean"], p["mean"], m.dp), _diff(r["median"], p["median"], m.dp)
    out["difference"] = {"mean": dm, "median": dmed, "recent_is": _direction(dm)}
    than = "as" if dm == 0 else f"by {_fmt(abs(dm), m.dp, unit)} than"
    out["summary"] = (f"{_label(metric)}: mean {_fmt(r['mean'], m.dp, unit)} from {r['from']} to "
                      f"{r['to']}, {_direction(dm)} {than} the mean {_fmt(p['mean'], m.dp, unit)} "
                      f"from {p['from']} to {p['to']}.")
    return out


def q_training_frequency(ctx: Ctx, weeks: int, end: date | None) -> dict:
    end = end or ctx.today
    first = dashboard._monday(end) - timedelta(weeks=weeks - 1)
    recs, sess = _in(*ctx.training(), first, end)
    b = dashboard.training_buckets(sess, recs, first, end, 7)
    watch = ctx.days("garmin")
    rows = []
    for i in range(weeks):
        wk = first + timedelta(weeks=i)
        we = min(wk + timedelta(days=6), end)
        rows.append([_iso(wk), b["tonal"][i], b["garmin"][i],
                     sum(1 for k in range((we - wk).days + 1) if wk + timedelta(days=k) in watch)])
    full = rows if end.weekday() == 6 else rows[:-1]
    out = {"from": _iso(first), "to": _iso(end), "weeks": weeks,
           "columns": ["week_of", "tonal", "garmin_strength_only", "days_with_watch_data"],
           "rows": rows,
           "totals": {"tonal": sum(b["tonal"]), "garmin_strength_only": sum(b["garmin"])},
           "note": "A session the watch also logged counts once, as Tonal. "
                   "garmin_strength_only is strength the watch recorded with no Tonal record. "
                   "A week with 0 sessions and few days_with_watch_data may be missing data, "
                   "not rest."}
    if end.weekday() != 6:
        out["partial_week"] = {"week_of": rows[-1][0], "through": _iso(end)}
    if full:
        per = dashboard.summarize([r[1] + r[2] for r in full])
        out["mean_sessions_per_full_week"] = round(per["mean"], 1)
    ann = ctx.annotations(first, end)
    if ann:
        out["annotations"] = ann
    return out


def q_training_volume(ctx: Ctx, days: int, end: date | None) -> dict:
    start, end = _span(end or ctx.today, days)
    recs, sess = _in(*ctx.training(), start, end)
    daily = dashboard.training_buckets(sess, recs, start, end, 1)
    vols = [v for v in daily["volume"] if v is not None]
    sessions, garmin_only = sum(daily["tonal"]), sum(daily["garmin"])
    out = {"from": _iso(start), "to": _iso(end), "days": days, "unit": ctx.mass,
           "source": "Tonal only. Garmin records strength in minutes, not pounds, so it has no volume.",
           "tonal_sessions": sessions,
           "tonal_volume_total": round(sum(vols)) if sessions and vols else None,
           "garmin_strength_only_sessions": garmin_only}
    if not sessions:
        out["insufficient"] = "no Tonal sessions in this window"
    elif not vols:
        out["insufficient"] = "Tonal sessions in this window carry no volume"
    weekly = dashboard.training_buckets(sess, recs, start, end, 7)
    # +1: a window of SERIES_MAX weeks can touch one more Monday-to-Sunday week.
    if len(weekly["volume"]) <= SERIES_MAX + 1:
        w0 = date.fromisoformat(weekly["start"])
        out["columns"] = ["from", "to", "tonal_volume", "tonal_sessions"]
        out["weekly"] = [[_iso(max(w0 + timedelta(weeks=i), start)),
                          _iso(min(w0 + timedelta(weeks=i, days=6), end)),
                          None if v is None else round(v), weekly["tonal"][i]]
                         for i, v in enumerate(weekly["volume"])]
    else:
        out["weekly"] = f"left out: over {SERIES_MAX} weeks; ask for {SERIES_MAX * 7} days or fewer"
    if garmin_only:
        out["note"] = (f"{garmin_only} Garmin-only strength session(s) in this window "
                       "have no volume and are not in the total.")
    ann = ctx.annotations(start, end)
    if ann:
        out["annotations"] = ann
    return out


def q_recovery(ctx: Ctx, metric: str, days: int, end: date | None) -> dict:
    m, unit = METRICS[metric], ctx.unit(metric)
    start, end = _span(end or ctx.today, days)
    pts = ctx.points(metric)
    train = dashboard.training_days(ctx.training()[1])
    groups = {"after_training_day": [], "after_rest_day": []}
    counts = dict.fromkeys(groups, 0)
    # Day d's recovery is read the next morning: the night that ended then,
    # and that day's resting HR and body battery are all dated d + 1.
    for i in range(days - 1):
        d = start + timedelta(days=i)
        g = "after_training_day" if d in train else "after_rest_day"
        counts[g] += 1
        if d + timedelta(days=1) in pts:
            groups[g].append(pts[d + timedelta(days=1)])
    out = {"metric": metric, "unit": unit, "from": _iso(start), "to": _iso(end),
           "definition": "Training day: a Tonal or Garmin strength session. Rest day: neither "
                         "(walks and runs count as rest). Each day is matched with the next "
                         "day's reading. This compares two groups of days; it does not show cause."}
    for g, vals in groups.items():
        need = max(MIN_GROUP, dashboard.needed(counts[g]))
        block = {"days": counts[g], "readings": len(vals), "missing": counts[g] - len(vals)}
        if len(vals) < need:
            block["insufficient"] = f"{len(vals)} readings; a group needs at least {need}"
        else:
            s = dashboard.summarize(vals)
            block.update(mean=round(s["mean"], m.dp), median=round(s["median"], m.dp))
        out[g] = block
    t, r = out["after_training_day"], out["after_rest_day"]
    if "mean" in t and "mean" in r:
        dm = _diff(t["mean"], r["mean"], m.dp)
        out["difference"] = {"mean": dm, "median": _diff(t["median"], r["median"], m.dp),
                             "after_training_is": _direction(dm)}
        out["summary"] = (f"{_label(metric)}, {out['from']} to {out['to']}: mean "
                          f"{_fmt(t['mean'], m.dp, unit)} after {t['readings']} training days, "
                          f"{_fmt(r['mean'], m.dp, unit)} after {r['readings']} rest days.")
    else:
        out["difference"] = None
        out["summary"] = f"{_label(metric)}: no comparison, too little data in one group."
    return out


def q_extremes(ctx: Ctx, metric: str, days: int, n: int, end: date | None) -> dict:
    m, unit = METRICS[metric], ctx.unit(metric)
    start, end = _span(end or ctx.today, days)
    pts = dashboard.window(ctx.points(metric), end, days)
    out = {"metric": metric, "unit": unit, "from": _iso(start), "to": _iso(end), "days": days,
           "days_present": len(pts),
           "note": "Highest and lowest, not best and worst: which is better depends on the "
                   "metric. Ranked among the days that have a reading."}
    # Each entry is a real, dated reading, not an estimate, so the half-filled
    # rule for averages doesn't apply. The floor still does.
    if len(pts) < MIN_POINTS:
        out["insufficient"] = f"{len(pts)} days have a reading; ranking needs at least {MIN_POINTS}"
        return out
    # Ties go to the most recent day.
    ranked = sorted(pts.items(), key=lambda kv: (kv[1], kv[0].toordinal()))
    out["highest"] = [[_iso(d), round(v, m.dp)] for d, v in reversed(ranked[-n:])]
    out["lowest"] = [[_iso(d), round(v, m.dp)]
                     for d, v in sorted(pts.items(), key=lambda kv: (kv[1], -kv[0].toordinal()))[:n]]
    return out


def q_coverage(ctx: Ctx, days: int, min_gap: int, end: date | None) -> dict:
    start, end = _span(end or ctx.today, days)
    health = {s["source"]: s for s in dashboard.data_health(ctx.conn, ctx.cfg, ctx.today)["sources"]}
    out = {"from": _iso(start), "to": _iso(end), "days": days, "min_gap_days": min_gap,
           "sources": {}}
    for source in dashboard.SOURCES:
        have = ctx.days(source)
        h = health[source]
        block = {"oldest": _iso(min(have, default=None)), "newest": h["newest"],
                 "stale": h["stale"], "days_old": h["age_days"],
                 "days_with_data": sum(1 for d in have if start <= d <= end)}
        spans = guard.acknowledged_spans(ctx.conn, source)
        gaps: list[dict] = []
        if have and min(have) <= end:
            scan = max(start, min(have))
            if scan > start:
                block["no_data_before"] = _iso(scan)
            run = None
            for k in range((end - scan).days + 2):
                d = scan + timedelta(days=k)
                if d <= end and d not in have:
                    run = run or d
                    continue
                if run and (d - run).days >= min_gap:
                    last = d - timedelta(days=1)
                    gap = {"from": _iso(run), "to": _iso(last), "days": (d - run).days}
                    if any(s <= run and last <= e for s, e in spans):
                        gap["annotated"] = True
                    gaps.append(gap)
                run = None
        block["gaps_total"] = len(gaps)
        block["gaps"] = gaps[-GAPS_MAX:]
        if len(gaps) > GAPS_MAX:
            block["gaps_note"] = f"showing the {GAPS_MAX} most recent"
        out["sources"][source] = block
    out["note"] = ("Tonal has data only on workout days, so its gaps include rest days. "
                   "annotated: the gap is recorded as expected in annotations.")
    ann = ctx.annotations(start, end)
    if ann:
        out["annotations"] = ann
    return out


QUERIES: dict[str, Query] = {
    "list": Query(q_list, "This catalog: every query, its parameters and the metrics."),
    "latest": Query(q_latest, "The newest reading of every metric, dated, against its 30-day median."),
    "day": Query(q_day, "Every metric and the training on one date.",
                 {"date": Param(Day(), about="the date")}),
    "metric-window": Query(
        q_metric_window, "One metric over a window: mean, median, lowest and highest with "
                         "dates, and how many days have a reading.",
        {"metric": METRIC, "days": days_param(30), "end": END}),
    "compare-windows": Query(
        q_compare_windows, "One metric over a recent window and the window just before it, "
                           "with the difference.",
        {"metric": METRIC, "recent": Param(Int(1, 365), 7, "recent window, days"),
         "prior": Param(Int(1, 3650), 30, "the window before it, days"), "end": END}),
    "training-frequency": Query(
        q_training_frequency, "Strength sessions per week, Tonal and Garmin-only labelled apart.",
        {"weeks": Param(Int(1, 52), 12, "weeks, ending with the week holding `end`"), "end": END}),
    "training-volume": Query(
        q_training_volume, "Tonal volume over a window. Tonal only: Garmin has no volume.",
        {"days": days_param(90), "end": END}),
    "recovery-after-training": Query(
        q_recovery, "A recovery metric the day after training days versus the day after rest days.",
        {"metric": Param(Choice(RECOVERY), about="which metric"), "days": days_param(90, lo=14),
         "end": END}),
    "extremes": Query(
        q_extremes, "The highest and lowest days of a metric in a window, with dates.",
        {"metric": METRIC, "days": days_param(90), "n": Param(Int(1, 10), 3, "days each way"),
         "end": END}),
    "coverage": Query(
        q_coverage, "Per source: oldest and newest data, staleness, and gaps in a window.",
        {"days": days_param(90), "min_gap": Param(Int(1, 365), 4, "shortest gap to list, days"),
         "end": END}),
}


# --------------------------------------------------------------------- run

def run(argv: list[str], *, cfg=None, today: date | None = None,
        snapshot=None) -> tuple[dict, int]:
    """(answer, exit code). Input is checked before anything is opened."""
    try:
        name, params = parse(argv)
    except Rejected as exc:
        return {"error": str(exc)}, REJECTED
    if name == "list":
        return {"query": "list", **q_list(None)}, OK
    try:
        cfg = cfg or config.load()
    except SystemExit:
        return {"error": "configuration could not be loaded"}, UNAVAILABLE
    today = today or datetime.now(cfg.timezone).date()
    for key, v in params.items():
        if isinstance(v, date) and v > today:
            return {"error": f"{key} must not be after today"}, REJECTED
    snap = dashboard.snapshot_path(cfg, snapshot)
    if not snap.is_file():
        return {"error": "no snapshot; run sync_all.py snapshot"}, UNAVAILABLE
    try:
        conn = db.connect(snap, read_only=True)
        try:
            answer = QUERIES[name].fn(Ctx(conn, cfg, today), **params)
        finally:
            conn.close()
    except sqlite3.Error:
        return {"error": "the snapshot could not be read"}, UNAVAILABLE
    except Exception as exc:  # noqa: BLE001 - one JSON line to the agent, the trace to stderr
        traceback.print_exc(file=sys.stderr)
        return {"error": f"internal error ({type(exc).__name__})"}, UNAVAILABLE
    return {"query": name, "as_of": today.isoformat(), **answer}, OK


def render(answer: dict, rc: int) -> tuple[str, int]:
    """One line of compact JSON. An answer too big for the model's context
    is refused rather than truncated."""
    text = json.dumps(answer, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(text) > MAX_OUTPUT:
        return json.dumps({"error": "answer too large; ask for a shorter window"}), UNAVAILABLE
    return text, rc


def main(argv: list[str]) -> int:
    text, rc = render(*run(argv))
    print(text)
    return rc
