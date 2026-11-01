"""Integrity guard.

The problem this exists to solve: toneget paginates the workout download and,
if a page fails, logs it and carries on. The export still succeeds - it is just
missing workouts, with nothing in the file to say so. A pipeline that trusts
the export will quietly shrink your training history.

So we treat Tonal history as append-only and verify the export against what we
already hold. An export that has lost workouts is rejected whole; we never
upsert part of a suspect file.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta

from .garmin import DAILY_TARGETS


@dataclass
class Verdict:
    accept: bool
    status: str                                  # 'ok' | 'ok_warn' | 'rejected'
    anomalies: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        head = {"ok": "ACCEPT", "ok_warn": "ACCEPT (with warnings)", "rejected": "REJECT"}[self.status]
        if not self.anomalies:
            return head
        return head + "\n" + "\n".join(f"  - {a}" for a in self.anomalies)


def _parse_day(s: str | None) -> date | None:
    try:
        return date.fromisoformat(s) if s else None
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------ annotations

def acknowledged_spans(conn: sqlite3.Connection, source: str) -> list[tuple[date, date]]:
    """Date ranges recorded in `annotations` as a known-good absence of data."""
    spans = []
    for start, end in conn.execute(
        "SELECT start_date, end_date FROM annotations WHERE source = ?", (source,)
    ):
        s, e = _parse_day(start), _parse_day(end)
        if s and e and s <= e:
            spans.append((s, e))
    return spans


def _merge(spans: list[tuple[date, date]]) -> list[tuple[date, date]]:
    """Merge overlapping or adjacent (start, end) day ranges."""
    merged: list[tuple[date, date]] = []
    for s, e in sorted(spans):
        if merged and s <= merged[-1][1] + timedelta(days=1):
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def evaluate_tonal(
    conn: sqlite3.Connection,
    incoming: list[dict],
    *,
    declared_total: int | None = None,
    reject_on_missing: bool = True,
    reject_on_count_drop: bool = True,
    gap_warn_days: int = 21,
    set_field_drift: list[str] | tuple = (),
) -> Verdict:
    """Decide whether a parsed Tonal export may be loaded.

    `incoming` is the normalized workout list: dicts with at least
    'workout_id' and 'local_date'.

    `declared_total` is how many workouts the source API said exist. Stock
    toneget does not record it; the README has a short patch that does, and
    with it even a FIRST run can detect a dropped page. Without it we fall
    back to comparing against what we already hold.
    """
    anomalies: list[str] = []   # real warnings
    info: list[str] = []        # context, never promotes the status
    fatal: list[str] = []

    incoming_ids = [w["workout_id"] for w in incoming]
    unique_ids = set(incoming_ids)

    if not incoming:
        return Verdict(False, "rejected", ["Export contains zero workouts."])

    if len(unique_ids) != len(incoming_ids):
        anomalies.append(
            f"Export contains {len(incoming_ids) - len(unique_ids)} duplicate workout id(s); "
            "duplicates were collapsed."
        )

    # The strongest check available, when the exporter recorded it: the API
    # told us how many workouts exist, so a short file is provably short.
    if declared_total:
        if len(unique_ids) < int(declared_total):
            fatal.append(
                f"Export holds {len(unique_ids)} workout(s) but the source declared "
                f"{int(declared_total)}. {int(declared_total) - len(unique_ids)} were lost "
                "in transit."
            )
        else:
            info.append(f"Source-declared total ({int(declared_total)}) matched.")
    else:
        info.append(
            "Export carries no source-declared total, so completeness can only be "
            "checked against stored history (see README: toneget pagination patch)."
        )

    existing_ids = {r[0] for r in conn.execute("SELECT workout_id FROM tonal_workout")}
    existing_count = len(existing_ids)

    # The core check. Tonal history does not shrink, so a workout we have seen
    # before and is now absent means the export is incomplete.
    missing = existing_ids - unique_ids
    if missing:
        sample = ", ".join(sorted(missing)[:5])
        msg = (
            f"Export is missing {len(missing)} workout(s) already in the database "
            f"(e.g. {sample}). This is the signature of a dropped page."
        )
        (fatal if reject_on_missing else anomalies).append(msg)

    if existing_count and len(unique_ids) < existing_count:
        msg = (
            f"Workout count fell from {existing_count} to {len(unique_ids)}."
        )
        (fatal if reject_on_count_drop else anomalies).append(msg)

    _check_sets(conn, incoming, fatal, anomalies, info,
                reject_on_missing=reject_on_missing, field_drift=set_field_drift)

    # Soft checks.
    bad_ts = [w for w in incoming if not w.get("local_date")]
    if bad_ts:
        anomalies.append(
            f"{len(bad_ts)} workout(s) had an unparseable timestamp and will not join "
            "to any daily view."
        )

    assumed = [w for w in incoming if w.get("tz_source") == "assumed_local"]
    if assumed:
        anomalies.append(
            f"{len(assumed)} workout(s) had no timezone offset in the source; the "
            "configured local timezone was applied. Spot-check one against the Tonal app."
        )

    days = sorted({d for d in (_parse_day(w.get("local_date")) for w in incoming) if d})
    if len(days) >= 10 and gap_warn_days > 0:
        # Days inside an annotated span count as accounted for, so a known
        # break is acknowledged once instead of warned about on every run.
        spans = [
            (max(s, days[0]), min(e, days[-1]))
            for s, e in acknowledged_spans(conn, "tonal")
            if s <= days[-1] and e >= days[0]
        ]
        runs = _merge([(d, d) for d in days] + spans)
        worst = max(
            (((b[0] - a[1]).days, a[1], b[0]) for a, b in zip(runs, runs[1:])),
            default=(0, None, None),
        )
        if worst[0] > gap_warn_days:
            anomalies.append(
                f"{worst[0]}-day gap in history between {worst[1]} and {worst[2]}. "
                "Expected if you took time off; suspicious if you did not."
            )

    new_count = len(unique_ids - existing_ids)
    info.append(f"{new_count} new workout(s), {len(unique_ids)} total in export.")

    if fatal:
        return Verdict(False, "rejected", fatal + anomalies + info)
    return Verdict(True, "ok_warn" if anomalies else "ok", anomalies + info)


def _check_sets(conn, incoming, fatal, anomalies, info, *,
                reject_on_missing: bool, field_drift) -> None:
    """Sets are held to the workouts' rule: history doesn't shrink. A set
    we hold that is absent now means the export lost it (a renamed or
    trimmed sets field looks exactly like this), so the export is refused
    whole. So is a set the loader can't key."""
    sets = [s for w in incoming for s in w.get("_sets") or ()]
    ids = [s["set_uid"] for s in sets if s.get("set_uid") is not None]
    unique = set(ids)

    if len(ids) < len(sets):
        fatal.append(f"{len(sets) - len(ids)} set(s) have no `id`, so they can't be "
                     "keyed. toneget's set fields may have changed.")
    if len(unique) < len(ids):
        fatal.append(f"{len(ids) - len(unique)} set id(s) appear more than once in the export.")
    natural = Counter((s["workout_id"], s.get("prescribed_set_id"), s.get("side_number"))
                      for s in sets if s.get("prescribed_set_id") is not None)
    clashes = sum(n - 1 for n in natural.values() if n > 1)
    if clashes:
        fatal.append(f"{clashes} set(s) repeat another set's workout, setId and side, "
                     "which have always been unique together.")

    stored = {r[0] for r in conn.execute("SELECT set_uid FROM tonal_set")}
    lost = stored - unique
    if lost:
        msg = (f"Export is missing {len(lost)} set(s) already in the database "
               f"(e.g. {', '.join(sorted(lost)[:3])}). A renamed or trimmed sets field, "
               "or a dropped page, looks like this.")
        (fatal if reject_on_missing else anomalies).append(msg)

    elsewhere = sum(1 for s in sets
                    if s.get("_claimed_workout") not in (None, s["workout_id"]))
    if elsewhere:
        anomalies.append(f"{elsewhere} set(s) name a different workout in workoutActivityID "
                         "than the one they are nested in; they were kept under the one "
                         "they are nested in.")
    if field_drift:
        anomalies.append(f"Set field(s) no set carries: {', '.join(field_drift)}. toneget's "
                         "field names may have changed; those columns load as NULL for new "
                         "sets, and stored values are kept.")
    info.append(f"{len(unique - stored)} new set(s), {len(unique)} total in export.")


def evaluate_withings(
    pages_walked: int,
    exhausted: bool,
    measures: int,
    *,
    strict: bool = True,
) -> Verdict:
    """Same principle for the Withings loop: a hole is a rejection, not a shrug.

    getmeas returns `more = 1` plus an `offset` until the data is exhausted,
    and the docs are explicit that you must loop until `more = 0`. Stopping
    early - on a safety cap or an error - leaves a hole, so we refuse to
    commit the run rather than write a partial window.
    """
    note = f"Walked {pages_walked} page(s), {measures} measurement(s)."
    if exhausted:
        return Verdict(True, "ok", [note])
    msg = (
        f"Pagination did not complete: stopped after {pages_walked} page(s) while the "
        "API still reported more data."
    )
    if strict:
        return Verdict(False, "rejected", [msg, note])
    return Verdict(True, "ok_warn", [msg, note])


# BodyScan's five segments partition the body. In one weigh-in, each segmental
# type summed over the five positions equals its whole-body counterpart, and
# whole-body fat-free mass minus muscle mass equals bone mass. On a real
# scan history both held on every full scan, to rounding. The Body Scan
# tab leans on both identities. It draws segments as parts of the whole, and
# derives a segment's bone as fat-free minus muscle, because Withings reports
# bone for the whole body only. A payload where either stops holding is
# flagged. That's a warning, not a rejection, because the readings themselves
# are still what the scale sent.
SEGMENT_POSITIONS = (2, 3, 10, 11, 12)
SEGMENT_WHOLE = {173: 5, 174: 8, 175: 76}   # segmental type -> whole-body type
PARTITION_TOL_KG = 0.1


def withings_partition(grps: list[dict], tol: float = PARTITION_TOL_KG) -> list[str]:
    """Anomalies for weigh-ins whose segments don't add up to the whole body,
    or whose whole-body bone isn't fat-free minus muscle. Raw API groups in,
    so it runs before anything is written. A group with no segmental
    measures isn't checked; one with some positions missing is flagged."""
    bad: list[str] = []
    for g in grps:
        seg: dict[tuple[int, int], float] = {}
        whole: dict[int, float] = {}
        for m in g.get("measures") or []:
            if m.get("type") is None or m.get("value") is None:
                continue
            t, v = int(m["type"]), int(m["value"]) * 10 ** int(m.get("unit") or 0)
            pos = m.get("position")
            if t in SEGMENT_WHOLE and pos is not None:
                seg[(t, int(pos))] = v
            elif pos is None or int(pos) == 7:      # 7 is BodyScan's whole body
                whole[t] = v
        where = f"group {g.get('grpid')}"
        if seg:
            for t, wt in SEGMENT_WHOLE.items():
                missing = [p for p in SEGMENT_POSITIONS if (t, p) not in seg]
                if missing:
                    bad.append(f"{where}: type {t} has no segment at position(s) "
                               + ", ".join(map(str, missing)))
                    continue
                total = sum(seg[(t, p)] for p in SEGMENT_POSITIONS)
                if wt in whole and abs(total - whole[wt]) > tol:
                    bad.append(f"{where}: type {t} segments sum to {total:.2f} kg, "
                               f"whole-body type {wt} is {whole[wt]:.2f} kg")
        if all(k in whole for k in (5, 76, 88)) and abs(whole[5] - whole[76] - whole[88]) > tol:
            bad.append(f"{where}: fat-free {whole[5]:.2f} - muscle {whole[76]:.2f} kg "
                       f"isn't bone {whole[88]:.2f} kg")
    if not bad:
        return []
    shown = "; ".join(bad[:3]) + (f"; and {len(bad) - 3} more" if len(bad) > 3 else "")
    return [f"{len(bad)} BodyScan check(s) failed beyond {tol} kg: the segments should add up "
            f"to the whole body and fat-free minus muscle should be bone. {shown}. The Body "
            "Scan tab's derived segment bone and its segments-as-parts reading assume both."]


# -------------------------------------------------------------- freshness

# How many days the newest data may trail today before a source is stale.
# Garmin and Withings record daily; lifting is intermittent. Override per
# source with `stale_after_days` in config.toml.
STALE_AFTER_DAYS = {"garmin": 2, "withings": 2, "tonal": 14}

# A Garmin day counts only if it holds something besides sleep. The mid-morning
# sleep catch-up (scripts/sleep_catchup.sh) loads the night ending this
# morning hours before the rest of that day exists. A sleep-only row must not
# stand in for the nightly download, the one that goes stale invisibly: a
# working catch-up would otherwise keep a stopped nightly looking fresh.
_GARMIN_DAY = "COALESCE({}) IS NOT NULL".format(
    ", ".join(t for t, (_, table) in DAILY_TARGETS.items() if table != "sleep"))

# Every day a source holds data for, by the same definition NEWEST_DAY_SQL
# takes the newest of.
DAYS_SQL = {
    "garmin": f"SELECT DISTINCT day FROM garmin_daily WHERE {_GARMIN_DAY}",
    "tonal": "SELECT DISTINCT local_date FROM tonal_workout",
    "withings": "SELECT DISTINCT local_date FROM withings_measure WHERE category = 1",
}

NEWEST_DAY_SQL = {
    "garmin": f"SELECT MAX(day) FROM garmin_daily WHERE {_GARMIN_DAY}",
    "tonal": "SELECT MAX(local_date) FROM tonal_workout",
    "withings": "SELECT MAX(local_date) FROM withings_measure WHERE category = 1",
}


@dataclass
class Freshness:
    source: str
    newest: date | None         # newest day the source holds data for (Garmin: besides sleep)
    through: date | None        # newest, carried forward by an annotation that continues it
    age_days: int | None        # days from `through` to today
    limit_days: int

    @property
    def stale(self) -> bool:
        return self.age_days is None or self.age_days > self.limit_days


def freshness(conn: sqlite3.Connection, source: str, *, today: date,
              stale_after_days: int) -> Freshness:
    newest = _parse_day(conn.execute(NEWEST_DAY_SQL[source]).fetchone()[0])
    if newest is None:
        return Freshness(source, None, None, None, stale_after_days)
    through = newest
    for s, e in _merge(acknowledged_spans(conn, source) + [(newest, newest)]):
        if s <= newest <= e:
            through = e
    return Freshness(source, newest, through, (today - through).days, stale_after_days)


def evaluate_freshness(conn: sqlite3.Connection, source: str, *, today: date,
                       stale_after_days: int) -> Verdict:
    """Is the data itself new, as opposed to: did the load work?

    The two diverge when something upstream stops advancing while the loader
    keeps succeeding - GarminDB's files going stale is the case that hid for
    days. Stale is a warning, never a rejection: one stale source must
    not stop the others loading.
    """
    f = freshness(conn, source, today=today, stale_after_days=stale_after_days)
    if not f.stale:
        return Verdict(True, "ok")
    if f.newest is None:
        return Verdict(True, "ok_warn", [f"No {source} data stored at all."])
    if f.through == f.newest:
        age = f"{f.age_days} day(s) old"
    else:
        age = f"{f.age_days} day(s) past the annotation ending {f.through}"
    return Verdict(True, "ok_warn", [
        f"Newest {source} data is from {f.newest}, {age} (limit {f.limit_days}). "
        "The load succeeded but the data behind it has stopped advancing - "
        "check the upstream download."
    ])
