"""Sleep catch-up: load the night that ended this morning, before the brief.

    python3 sync_all.py sleep-catchup

scripts/sleep_catchup.sh runs this mid-morning, after the nightly sync and
before the brief. The nightly's GarminDB run (`--latest`) downloads only
through yesterday, so without this the newest night anything shows is the
one before last.

A run stops at the first of these that applies:

1. The snapshot already holds today's night. Nothing to do, so running again
   changes nothing.
2. GarminDB holds it but the snapshot doesn't (an earlier run imported it and
   then failed): load it, with no download.
3. GarminDB's access token has expired. The nightly run renews it, so this run
   doesn't try. It logs that and exits 0.
4. Connect has no night for today yet. That's normal on a late morning, so it
   exits 0. The brief dates its night, so it says which night it has.
5. Otherwise GarminDB's own request for today's sleep is saved where GarminDB
   keeps its sleep files and imported by GarminDB's own importer. sync_all
   then loads Garmin and rewrites the snapshot, as the nightly does.

Steps 1, 3 and 4 never open wellness.db, so they leave the freshness state
exactly as the nightly sync left it. Steps 2 and 5 add a row for today that
holds only sleep, and Garmin's freshness doesn't count such a row
(guard.NEWEST_DAY_SQL). So a working catch-up can't hide a nightly download
that has stopped.

Every run appends one JSON line to sleep_catchup.jsonl, beside wellness.db.
It says whether the night was found, when you woke, and how many minutes
after waking the run came. A missed night's wake time is only known once the
next nightly has the night, so each line also completes the night before
(`previous`). Over a few weeks that is the distribution of how long after
waking Connect has the night, measured by the job itself.
"""

from __future__ import annotations

import base64
import json
import sys
import tempfile
import traceback
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import dashboard, db, garmin

LOG_NAME = "sleep_catchup.jsonl"
# A token this close to its expiry could lapse partway through the run.
TOKEN_MARGIN_S = 60

# A run's result. Only ERROR exits non-zero. FOUND means imported into
# GarminDB but not loaded yet. sync_all turns it into LOADED or ERROR.
LOADED, ALREADY, FOUND, NOT_FOUND, TOKEN_EXPIRED, ERROR = (
    "loaded", "already_loaded", "found", "not_found", "token_expired", "error")


@dataclass
class Night:
    """One night as GarminDB holds it."""
    asleep_s: int
    # GarminDB's `end`: naive local wall-clock time, like every GarminDB time
    # the loader reads.
    wake: datetime | None


def log_path(cfg) -> Path:
    return cfg.wellness_db.with_name(LOG_NAME)


def token_expiry(path: Path) -> datetime | None:
    """When GarminDB's cached access token expires, from the token's own `exp`
    claim, or None when that can't be read. Only the claim is decoded. The
    token itself is never logged or printed."""
    try:
        payload = json.loads(Path(path).read_text())["di_token"].split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return datetime.fromtimestamp(float(claims["exp"]), timezone.utc)
    except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError):
        return None


def asleep_in(reply, day: date) -> int | None:
    """Seconds asleep when Connect's reply holds the night ending `day`, or
    None when Connect doesn't have that night yet.

    Until Connect has the night, it answers with the same record and every
    sleep field empty, not with part of a night (seen when probed every 15
    minutes through a morning). A reply that isn't that record, or is for another
    day, is an error."""
    dto = reply.get("dailySleepDTO") if isinstance(reply, dict) else None
    if not isinstance(dto, dict):
        raise ValueError("Connect's sleep reply has no dailySleepDTO")
    if dto.get("calendarDate") != day.isoformat():
        raise ValueError(f"Connect's sleep reply is for {dto.get('calendarDate')}, not {day}")
    secs = dto.get("sleepTimeSeconds")
    # 0 s is how Garmin records a night the watch didn't: a missing night.
    if not secs or not dto.get("sleepStartTimestampGMT") or not dto.get("sleepEndTimestampGMT"):
        return None
    return int(secs)


def garmindb_night(cfg, day: date) -> Night | None:
    """The night ending `day` in GarminDB's sleep table, opened read-only and
    found the way the loader finds it. None when GarminDB has no such night."""
    found = garmin.discover(cfg)
    chosen, _ = garmin.resolve_daily(found)
    if "sleep_total_s" not in chosen:
        raise RuntimeError(f"no GarminDB sleep table found under {cfg.garmin_db_dir}")
    fname, table, day_col, col = chosen["sleep_total_s"]
    cols = f'"{col}" AS asleep' + (', "end" AS wake' if "end" in found[fname][table] else "")
    src = db.connect(Path(cfg.garmin_db_dir) / fname, read_only=True)
    try:
        # GarminDB writes the day as '2026-01-31 00:00:00.000000'.
        row = src.execute(f'SELECT {cols} FROM "{table}" WHERE substr("{day_col}", 1, 10) = ?',
                          (day.isoformat(),)).fetchone()
    finally:
        src.close()
    asleep = garmin.to_seconds(row["asleep"]) if row else None
    if not asleep:
        return None
    wake = None
    if "wake" in row.keys() and row["wake"]:
        try:
            wake = datetime.fromisoformat(str(row["wake"]))
        except ValueError:
            wake = None
    return Night(asleep, wake)


def snapshot_night(cfg, day: date) -> int | None:
    """Seconds asleep for the night ending `day` in the snapshot that the brief
    and the page read, or None. The snapshot, not wellness.db: a night that
    was loaded but never snapshotted hasn't reached anything downstream, so a
    run that finds it there finishes the job."""
    snap = dashboard.snapshot_path(cfg)
    if not snap.is_file():
        return None
    conn = db.connect(snap, read_only=True)
    try:
        row = conn.execute("SELECT sleep_total_s FROM garmin_daily WHERE day = ?",
                           (day.isoformat(),)).fetchone()
    finally:
        conn.close()
    return row[0] if row and row[0] else None


class GarminDB:
    """GarminDB's own login, sleep request and importer, for one day. The
    only code here that imports garmindb, and only when a run needs it, so
    the rest of this module and its tests stay stdlib-only."""

    def __init__(self) -> None:
        import logging

        from garmindb import Download, GarminConnectConfigManager, GarminSleepData

        # garmindb_cli logs INFO to garmindb.log in the working directory.
        # Here only warnings and errors go out, on stderr, which the script logs.
        logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
        self._download, self._importer = Download, GarminSleepData
        self.gc = GarminConnectConfigManager()
        self.token_file = Path(self.gc.get_token_store_file())

    def fetch(self, day: date) -> dict:
        dl = self._download(self.gc)
        dl.garmin.login()           # GarminDB's cached token. Raises if it can't log in.
        # The URL and parameters GarminDB's own sleep download sends
        # (Download.__get_sleep_day, garmindb 3.9.0). When checked, that
        # reply equalled the file GarminDB later saved, field for field.
        # GarminDB's day loop logs a failed request and carries on, so the
        # request is made here, where a failure raises.
        return dl.garmin.connectapi(
            f"{dl.garmin_connect_sleep_daily_url}/{dl.garmin.display_name}",
            params={"date": day.isoformat(), "nonSleepBufferMinutes": 60})

    def save_and_import(self, day: date, reply: dict) -> None:
        name = f"sleep_{day.isoformat()}"
        # Saved where GarminDB's download keeps it, as that download would.
        # The nightly re-downloads its last 3 days, so it will overwrite this.
        self._download.save_json_to_file(f"{self.gc.get_sleep_dir()}/{name}", reply, True)
        # GarminDB's importer reads a directory, so give it one with only this night.
        with tempfile.TemporaryDirectory() as tmp:
            self._download.save_json_to_file(f"{tmp}/{name}", reply, True)
            self._importer(self.gc.get_db_params(), tmp, False, False).process()


def _minutes(later: datetime, earlier: datetime) -> int:
    return round((later - earlier).total_seconds() / 60)


def _minute(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M")


def _with_night(rec: dict, result: str, night: Night | None, now: datetime, cfg) -> dict:
    rec.update(result=result, found=True)
    if night and night.wake:
        rec["wake_local"] = _minute(night.wake)
        rec["gap_min"] = _minutes(now, night.wake.replace(tzinfo=cfg.timezone))
    return rec


def attempt(cfg, now: datetime, source=None) -> tuple[dict, int | None]:
    """Everything before the load. Returns the run's record and, when the
    result is FOUND, the seconds asleep that the load must then hold."""
    day = now.date()
    rec: dict = {"run_local": now.isoformat(timespec="seconds"), "night": day.isoformat(),
                 "result": None, "found": None, "wake_local": None, "gap_min": None}
    try:
        if snapshot_night(cfg, day):
            return _with_night(rec, ALREADY, garmindb_night(cfg, day), now, cfg), None
        night = garmindb_night(cfg, day)
        if night:
            return _with_night(rec, FOUND, night, now, cfg), night.asleep_s

        source = source or GarminDB()
        expires = token_expiry(source.token_file)
        if expires is None:
            raise RuntimeError(f"can't read the expiry of GarminDB's token in {source.token_file}")
        if expires <= now + timedelta(seconds=TOKEN_MARGIN_S):
            rec.update(result=TOKEN_EXPIRED, token_expires_local=expires.astimezone(
                cfg.timezone).isoformat(timespec="minutes"))
            return rec, None

        reply = source.fetch(day)
        asleep = asleep_in(reply, day)
        if asleep is None:
            rec.update(result=NOT_FOUND, found=False)
            return rec, None
        rec["found"] = True
        # When probed, the first version Connect served was already final.
        # Recorded so the log shows whether an early fetch ever isn't.
        rec["confirmation"] = reply["dailySleepDTO"].get("sleepWindowConfirmationType")
        source.save_and_import(day, reply)
        # GarminDB's importer logs a failure and carries on, so check what it stored.
        night = garmindb_night(cfg, day)
        if not night or night.asleep_s != asleep:
            raise RuntimeError(
                f"GarminDB's import didn't store the night: Connect has {asleep} s asleep, "
                f"GarminDB holds {night.asleep_s if night else 'nothing'}")
        return _with_night(rec, FOUND, night, now, cfg), asleep
    except Exception as exc:  # noqa: BLE001 - recorded, and the run exits 2
        return failed(rec, exc), None


def failed(rec: dict, exc: BaseException) -> dict:
    traceback.print_exc()       # into the log. The record keeps one line.
    rec.update(result=ERROR, error=f"{type(exc).__name__}: {str(exc)[:300]}")
    return rec


def read_log(cfg) -> list[dict]:
    try:
        lines = log_path(cfg).read_text().splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict):
            out.append(r)
    return out


def previous(cfg, day: date) -> dict:
    """The night before `day`, now that the nightly sync has it: when you woke,
    and what the first catch-up run that checked that morning found. A night
    the catch-up missed gets its wake time and gap here, a day late. A
    negative gap means the run came before you woke."""
    prev = (day - timedelta(days=1)).isoformat()
    night = garmindb_night(cfg, day - timedelta(days=1))
    wake = night.wake if night else None
    first = next((r for r in read_log(cfg)
                  if r.get("night") == prev and r.get("found") is not None), None)
    out = {"night": prev, "wake_local": _minute(wake) if wake else None,
           "found": first["found"] if first else None,
           "run_local": first["run_local"] if first else None, "gap_min": None}
    if first and wake:
        out["gap_min"] = _minutes(datetime.fromisoformat(first["run_local"]),
                                  wake.replace(tzinfo=cfg.timezone))
    return out


def record(cfg, rec: dict) -> Path:
    """Complete the night before, then append the run's line to the log."""
    day = date.fromisoformat(rec["night"])
    try:
        rec["previous"] = previous(cfg, day)
    except Exception as exc:  # noqa: BLE001 - the run's own result stands
        rec["previous"] = {"night": (day - timedelta(days=1)).isoformat(),
                           "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    path = log_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(rec) + "\n")
    return path


def describe(rec: dict) -> str:
    """The run in one plain sentence, for the log and the scheduler's record."""
    d = date.fromisoformat(rec["night"])
    night = f"the night ending {d:%a} {d.day} {d:%b}"
    woke = (f" You woke at {rec['wake_local'][11:]}, {rec['gap_min']} min before this run."
            if rec.get("wake_local") else "")
    result = rec["result"]
    if result == LOADED:
        return f"Loaded {night}.{woke}"
    if result == ALREADY:
        return f"{night[0].upper()}{night[1:]} is already loaded. Nothing to do."
    if result == NOT_FOUND:
        return (f"Garmin Connect doesn't have {night} yet. That's normal on a late morning. "
                "Nothing was loaded, and the brief shows the newest night it has, under its date.")
    if result == TOKEN_EXPIRED:
        return (f"Skipped: GarminDB's access token expired at {rec['token_expires_local'][11:16]}, "
                "and renewing it is the nightly sync's job. Nothing was loaded.")
    return f"FAILED: {rec.get('error')}"
