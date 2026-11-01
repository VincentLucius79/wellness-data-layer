"""Morning brief: a short plain-text digest of the read-only snapshot.

    python3 sync_all.py brief

scripts/brief.sh runs this for the OpenClaw scheduler, which sends stdout to
Telegram. So stdout carries the digest and nothing else, and a failure is one
line on stdout plus a non-zero exit, which the scheduler alerts on.

Every figure comes from dashboard.py: the 30-day medians, the 7- and 30-day
averages, sessions, what moved, and the guard's freshness verdict. The brief
only words them, so the brief and the page can't disagree about a number.

Like the page, it reports the newest night and the newest weigh-in, each
under its own date. The date is never dropped, even when the reading is from
this morning: an undated number would read as current. Past OLD_DAYS, the
age is the story, so the brief says how old the reading is and leaves it out
of "What moved".

No LLM, no composite score, no interpolation.
"""

from __future__ import annotations

import sqlite3
import sys
import traceback
from datetime import date, datetime
from pathlib import Path

from . import dashboard, db

# The same code sync_all uses for "errored".
UNAVAILABLE = 2
# Decimal places per metric, as the page shows them.
DP = {"sleep_h": 1, "sleep_score": 0, "rhr": 0, "bb_min": 0, "bb_max": 0,
      "weight": 1, "fat": 1}
LABEL = {"bb_min": "Body battery low", "bb_max": "Body battery high"}
# How a difference is written, per unit. Body fat moves in percentage
# points: "0.3%" would read as a relative change.
DELTA_UNIT = {"h": "h", "bpm": " bpm", "lb": " lb", "kg": " kg", "%": " pts", "": ""}
TITLE_CHARS = 40
# A reading older than this many days is reported as old, not just dated.
OLD_DAYS = 3


def _day(d: date) -> str:
    return f"{d:%a} {d.day} {d:%b}"


def _label(r: dict) -> str:
    return LABEL.get(r["key"], r["label"])


def _value(r: dict) -> str:
    v = f"{r['value']:.{DP[r['key']]}f}"
    unit = r["unit"]
    return v + unit if unit in ("h", "%") else f"{v} {unit}" if unit else v


def _vs(r: dict, ref: float | None, what: str) -> str:
    """'1.1h below your median', 'at your 7-day avg'. Both sides are rounded
    as shown first, so the words always match the numbers on screen."""
    dp = DP[r["key"]]
    diff = round(round(r["value"], dp) - round(ref, dp), dp)
    if diff == 0:
        return f"at your {what}"
    return (f"{abs(diff):.{dp}f}{DELTA_UNIT.get(r['unit'], '')} "
            f"{'above' if diff > 0 else 'below'} your {what}")


def _header(health: dict, today: date, tz) -> str:
    problems = []
    for s in health["sources"]:
        name = s["source"]
        if s["newest"] is None:
            problems.append(f"no {name} data stored")
        elif s["stale"]:
            age = (f"{s['age_days']} days old" if s["through"] == s["newest"]
                   else f"{s['age_days']} days past its annotated gap")
            problems.append(f"{name} STALE, {age} (limit {s['limit_days']})")
        if s["last_status"] in ("rejected", "error"):
            problems.append(f"{name} last sync {s['last_status']}")
    state = "; ".join(problems) if problems else "all sources fresh"
    synced = dashboard._local(health["synced_utc"], tz)
    when = ("never synced" if synced is None
            else f"synced {synced:%H:%M}" if synced.date() == today
            else f"synced {_day(synced.date())} {synced:%H:%M}")
    return f"{_day(today)} {today.year}: {state} ({when})"


def _age(day: date, today: date) -> int | None:
    """Days old, or None when it's recent enough to just carry its date."""
    age = (today - day).days
    return age if age > OLD_DAYS else None


def _night(night: dict, today: date) -> tuple[list[str], list[dict]]:
    """The newest night under its own date, and the rows that may count as moved."""
    if not night["day"]:
        return ["No Garmin nights stored."], []
    day = date.fromisoformat(night["day"])
    age = _age(day, today)
    # Garmin dates a night by the morning it ended.
    lines = [f"Newest night ended {_day(day)}, {age} days ago" if age
             else f"Night ending {_day(day)}"]
    for r in night["rows"]:
        # Resting HR and body battery are from the day before when the sleep
        # catch-up has loaded only the night (dashboard.night_row_days). A
        # reading from another day than the night's names its day.
        on = "" if r["day"] == night["day"] else f" on {_day(date.fromisoformat(r['day']))}"
        if r["value"] is None:
            lines.append(f"{_label(r)}{on}: missing")
        elif r["median"] is None:
            lines.append(f"{_label(r)} {_value(r)}{on}, no {dashboard.BASELINE_DAYS}-day median yet")
        else:
            lines.append(f"{_label(r)} {_value(r)}{on}, {_vs(r, r['median'], 'median')}")
    return lines, [] if age else night["rows"]


def _training(t: dict, unit: str) -> list[str]:
    head = f"Yesterday ({_day(date.fromisoformat(t['day']))})"
    if t["kind"] == "tonal":
        work = [w for w in t["tonal"] if (w["sets"] or 0) > 0 or (w["volume"] or 0) > 0]
        vols = [w["volume"] for w in t["tonal"] if w["volume"] is not None]
        sets = sum(w["sets"] or 0 for w in t["tonal"])
        titles = ", ".join((w["title"] or "untitled")[:TITLE_CHARS] for w in work)
        volume = f"{round(sum(vols)):,} {unit}" if vols else "volume missing"
        return [head, f"Tonal: {titles}, {volume}, {sets} sets"]
    if t["kind"] == "garmin":
        mins = round(sum(w["dur_s"] or 0 for w in t["garmin"]) / 60)
        return [head, f"Garmin strength: {mins} min (watch only, no volume)"]
    return [head, "No session."]


def _morning(m: dict | None, today: date) -> tuple[list[str], list[dict]]:
    """The newest weigh-in under its own date, and the rows that may count as moved."""
    if not m:
        return ["No weigh-ins stored."], []
    day = date.fromisoformat(m["day"])
    age = _age(day, today)
    when = f"{_day(day)}, {m['time']}" if m["time"] else _day(day)
    lines = [f"Newest weigh-in {when}, {age} days ago" if age else f"Weigh-in {when}"]
    for r in m["rows"]:
        if r["value"] is None:
            lines.append(f"{r['label']}: missing")
            continue
        parts = [_vs(r, avg, f"{n}-day avg") if avg is not None else f"{n}-day avg: too few readings"
                 for avg, n in ((r["avg7"], 7), (r["avg30"], 30))]
        lines.append(f"{r['label']} {_value(r)}: " + ", ".join(parts))
    return lines, [] if age else m["rows"]


def compose(conn: sqlite3.Connection, cfg, *, today: date) -> str:
    p = dashboard.build_payload(conn, cfg, today=today)
    daily = p["daily"]
    night, night_rows = _night(daily["night"], today)
    morning, morning_rows = _morning(daily["morning"], today)
    blocks = [[_header(p["health"], today, cfg.timezone)], night,
              _training(daily["training"], p["units"]["mass"]), morning]
    # Each moved reading carries its own date too: the row's, which for
    # resting HR and body battery can be the day before the night's. Readings
    # past OLD_DAYS were left out above: they moved days ago, and old is the
    # news now.
    moved = dashboard.moved(night_rows + morning_rows)
    if moved:
        blocks.append(["What moved"] + [
            f"{_label(m)} {_value(m)} on {_day(date.fromisoformat(m['day']))}, "
            f"{abs(m['z']):.1f} SD {'above' if m['z'] > 0 else 'below'} "
            f"your {dashboard.BASELINE_DAYS}-day mean of {m['mean']:.{DP[m['key']]}f}"
            for m in moved])
    return "\n\n".join("\n".join(b) for b in blocks)


def run(cfg, *, today: date | None = None,
        snapshot: str | Path | None = None) -> tuple[str, int]:
    """(text, exit code). On failure the text is one line saying why."""
    snap = dashboard.snapshot_path(cfg, snapshot)
    if not snap.is_file():
        return f"Wellness brief unavailable: no snapshot at {snap}.", UNAVAILABLE
    try:
        conn = db.connect(snap, read_only=True)
        try:
            text = compose(conn, cfg, today=today or datetime.now(cfg.timezone).date())
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return (f"Wellness brief unavailable: can't read the snapshot at {snap} ({exc}).",
                UNAVAILABLE)
    except Exception as exc:  # noqa: BLE001 - one line to the phone, the trace to stderr
        traceback.print_exc(file=sys.stderr)
        return f"Wellness brief failed: {type(exc).__name__}: {exc}", UNAVAILABLE
    return text, 0
