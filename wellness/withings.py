"""Withings loader: the official Health API (BodyScan + BP Vision).

This is the only one of the three sources with a sanctioned, documented API,
and it shows: OAuth2 with refresh tokens, a real `lastupdate` cursor for
incremental pulls, and explicit pagination semantics.

Three things the docs are emphatic about, all handled here:
  1. `value` is an integer and `unit` is a power of ten. The real number is
     value * 10**unit. 7500 with unit -2 is 75.00 kg.
  2. You MUST loop until `more == 0`. Stopping early is silent data loss, so a
     run that cannot finish the walk is rejected rather than committed.
  3. Save the new refresh_token on every refresh - the previous one stops
     working 8 hours after a new one is issued. The token file is written
     atomically for exactly that reason.

Documented rate limit: do not poll more than once every 10 minutes per user.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import guard
from .db import utc_now

AUTHORIZE_URL = "https://account.withings.com/oauth2_user/authorize2"
TOKEN_URL = "https://wbsapi.withings.net/v2/oauth2"
MEASURE_URL = "https://wbsapi.withings.net/measure"
SCOPE = "user.metrics,user.info"
MAX_PAGES = 200
MIN_POLL_SECONDS = 600          # Withings: no more than once per 10 minutes


class WithingsError(RuntimeError):
    pass


# ------------------------------------------------------------------ tokens

def _save_tokens(path: Path, data: dict) -> None:
    """Atomic + 0600. A half-written token file locks you out of the API."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _load_tokens(path: Path) -> dict:
    if not path.exists():
        raise WithingsError(
            f"No Withings tokens at {path}. Run: python sync_all.py withings-auth"
        )
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _post(url: str, params: dict, headers: dict | None = None) -> dict:
    body = urllib.parse.urlencode(
        {k: v for k, v in params.items() if v is not None}
    ).encode()
    req = urllib.request.Request(url, data=body, headers=headers or {}, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise WithingsError(f"HTTP {exc.code} from {url}: {exc.read()[:300]!r}") from exc
    except urllib.error.URLError as exc:
        raise WithingsError(f"Could not reach {url}: {exc.reason}") from exc

    status = payload.get("status")
    if status != 0:
        hint = ""
        if status == 601:
            hint = " (rate limit - wait 10 minutes)"
        elif status in (401, 2555):
            hint = " (token invalid or expired)"
        raise WithingsError(f"Withings API status {status}{hint}: {payload.get('error')}")
    return payload.get("body") or {}


# -------------------------------------------------------------------- auth

def authorize_url(cfg, state: str = "wellness") -> str:
    q = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": cfg.withings["client_id"],
        "scope": SCOPE,
        "redirect_uri": cfg.withings["redirect_uri"],
        "state": state,
    })
    return f"{AUTHORIZE_URL}?{q}"


def exchange_code(cfg, code: str) -> dict:
    body = _post(TOKEN_URL, {
        "action": "requesttoken",
        "grant_type": "authorization_code",
        "client_id": cfg.withings["client_id"],
        "client_secret": cfg.withings["client_secret"],
        "code": code,
        "redirect_uri": cfg.withings["redirect_uri"],
    })
    tokens = {
        "access_token": body["access_token"],
        "refresh_token": body["refresh_token"],
        "userid": body.get("userid"),
        "scope": body.get("scope"),
        "expires_at": int(time.time()) + int(body.get("expires_in", 10800)) - 60,
    }
    _save_tokens(cfg.withings_token, tokens)
    return tokens


def refresh(cfg, tokens: dict) -> dict:
    body = _post(TOKEN_URL, {
        "action": "requesttoken",
        "grant_type": "refresh_token",
        "client_id": cfg.withings["client_id"],
        "client_secret": cfg.withings["client_secret"],
        "refresh_token": tokens["refresh_token"],
    })
    new = {
        "access_token": body["access_token"],
        # Rotated. Persist immediately or the old one expires out from under us.
        "refresh_token": body.get("refresh_token", tokens["refresh_token"]),
        "userid": body.get("userid", tokens.get("userid")),
        "scope": body.get("scope", tokens.get("scope")),
        "expires_at": int(time.time()) + int(body.get("expires_in", 10800)) - 60,
    }
    _save_tokens(cfg.withings_token, new)
    return new


def access_token(cfg) -> str:
    tokens = _load_tokens(cfg.withings_token)
    if tokens.get("expires_at", 0) <= time.time():
        tokens = refresh(cfg, tokens)
    return tokens["access_token"]


# ----------------------------------------------------------------- getmeas

def fetch_measures(cfg, *, lastupdate: int | None = None,
                   startdate: int | None = None) -> tuple[list[dict], int, int, bool]:
    """Walk getmeas to exhaustion.

    Returns (measuregrps, pages_walked, updatetime, exhausted).
    `meastypes` is deliberately omitted so undocumented BodyScan codes come
    back too - they get recorded as 'discovered' rather than dropped.
    """
    token = access_token(cfg)
    headers = {"Authorization": f"Bearer {token}"}

    base: dict = {"action": "getmeas"}
    if lastupdate is not None:
        base["lastupdate"] = lastupdate
    else:
        base["startdate"] = startdate
        base["enddate"] = int(time.time())
    if cfg.withings.get("meastypes"):
        base["meastypes"] = cfg.withings["meastypes"]

    grps: list[dict] = []
    offset = 0
    pages = 0
    updatetime = 0
    exhausted = False

    while pages < MAX_PAGES:
        params = dict(base)
        if offset:
            params["offset"] = offset
        body = _post(MEASURE_URL, params, headers)
        pages += 1
        grps.extend(body.get("measuregrps") or [])
        updatetime = int(body.get("updatetime") or updatetime)
        if int(body.get("more") or 0) != 1:
            exhausted = True
            break
        offset = int(body.get("offset") or 0)
        if not offset:                 # more=1 but no cursor: refuse to spin
            break
        time.sleep(1)                  # be a polite client

    return grps, pages, updatetime, exhausted


# -------------------------------------------------------------------- sync

def sync(conn: sqlite3.Connection, cfg, *, full: bool = False,
         ignore_rate_limit: bool = False) -> dict:
    tz: ZoneInfo = cfg.timezone
    anomalies: list[str] = []

    if not cfg.withings.get("client_id"):
        return {"status": "error", "rows_seen": 0, "rows_upserted": 0,
                "anomalies": ["No Withings client_id in config.toml."]}

    # Respect the documented 10-minute poll limit.
    row = conn.execute(
        "SELECT started_utc FROM sync_runs WHERE source = 'withings' "
        "AND status IN ('ok','ok_warn') ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    if row and not ignore_rate_limit:
        try:
            last = datetime.strptime(row[0], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - last).total_seconds()
            if age < MIN_POLL_SECONDS:
                return {"status": "ok", "rows_seen": 0, "rows_upserted": 0,
                        "anomalies": [f"Skipped: last successful poll was {int(age)}s ago; "
                                      f"Withings asks for no more than one per {MIN_POLL_SECONDS}s."]}
        except ValueError:
            pass

    cursor = conn.execute(
        "SELECT value FROM withings_cursor WHERE key = 'lastupdate'"
    ).fetchone()

    if full or cursor is None:
        # Withings' first connected scale shipped in 2009, so the default
        # reaches back past any account's first weigh-in.
        start_date = cfg.withings.get("initial_start_date", "2009-01-01")
        startdate = int(datetime.fromisoformat(start_date)
                        .replace(tzinfo=timezone.utc).timestamp())
        grps, pages, updatetime, exhausted = fetch_measures(cfg, startdate=startdate)
        anomalies.append(f"Full pull from {start_date}.")
    else:
        grps, pages, updatetime, exhausted = fetch_measures(cfg, lastupdate=int(cursor[0]))
    # Lines so far, and the verdict's page count below, are context, not
    # warnings: a full pull that loaded cleanly is `ok`.
    informational = len(anomalies) + 1

    measures_seen = sum(len(g.get("measures") or []) for g in grps)

    # Archive the raw payload BEFORE judging it. Cheap, and the only way to
    # rebuild without re-polling an API that rate-limits you - and a rejected
    # payload is exactly the one you want to be able to inspect later.
    cfg.withings_archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    archive = cfg.withings_archive_dir / f"getmeas_{stamp}.json"
    archive.write_text(json.dumps(grps, separators=(",", ":")), encoding="utf-8")

    verdict = guard.evaluate_withings(
        pages, exhausted, measures_seen,
        strict=cfg.withings.get("strict_pagination", True),
    )
    anomalies.extend(verdict.anomalies)

    if not verdict.accept:
        return {"status": "rejected", "rows_seen": measures_seen, "rows_upserted": 0,
                "anomalies": anomalies, "notes": f"archived {archive.name}"}

    # Advisory: a BodyScan weigh-in whose segments stop adding up to the
    # whole body still loads, but the run says so (ok_warn).
    anomalies.extend(guard.withings_partition(grps))

    known = {r[0] for r in conn.execute("SELECT meastype FROM withings_meastype")}
    names = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT meastype, metric, unit_label FROM withings_meastype")}

    now = utc_now()
    upserted = 0
    discovered: set[int] = set()
    # Two measurements that land on the same key mean the key is missing a
    # discriminator the API is sending. That is how the `position` field on
    # segmental data was found - silently, four of every five rows were
    # overwriting each other. Never let it be silent again.
    seen_uids: set[str] = set()
    collisions = 0

    # Known bad estimate, kept on purpose. One real weigh-in has a good
    # weight, in line with its neighbours, but its fat-free mass
    # is about half theirs, so its body fat reads more than double. The row
    # is internally consistent (fat-free + fat = weight), so this is the
    # scale's model failing on one reading, not corrupted data, and it is
    # stored as the API sent it.
    # It is deliberately NOT annotated or excluded. The dashboard's Trends
    # lanes trim to the 2nd-98th percentile, so it can't set a lane's scale,
    # and one bad estimate among years of readings doesn't justify a schema
    # change: `annotations` only marks expected gaps, and has no way to flag
    # one reading's fields. Don't "fix" it by deleting the row, filtering it
    # here, or hard-coding it into chart logic.
    with conn:
        for grp in grps:
            grpid = str(grp.get("grpid"))
            epoch = int(grp.get("date") or 0)
            if not epoch:
                continue
            dt = datetime.fromtimestamp(epoch, timezone.utc)
            taken_utc = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            local_date = dt.astimezone(tz).date().isoformat()

            for m in grp.get("measures") or []:
                mtype = m.get("type")
                if mtype is None or m.get("value") is None:
                    continue
                mtype = int(mtype)
                raw_value = int(m["value"])
                raw_unit = int(m.get("unit") or 0)
                value = raw_value * (10 ** raw_unit)
                # Segmental measures repeat the same type once per body
                # segment, distinguished only by `position`. It belongs in
                # the key, or four of every five rows overwrite each other.
                pos = m.get("position")
                pos = int(pos) if pos is not None else None
                uid = f"{grpid}:{mtype}:{pos if pos is not None else '-'}"
                if uid in seen_uids:
                    collisions += 1
                seen_uids.add(uid)

                if mtype not in known:
                    discovered.add(mtype)
                    conn.execute(
                        "INSERT OR IGNORE INTO withings_meastype "
                        "(meastype, metric, unit_label, origin) VALUES (?,?,?,'discovered')",
                        (mtype, f"unknown_{mtype}", None),
                    )
                    known.add(mtype)
                    names[mtype] = (f"unknown_{mtype}", None)

                metric, unit_label = names.get(mtype, (None, None))
                conn.execute(
                    "INSERT INTO withings_measure (measure_uid, grpid, taken_utc, taken_epoch, "
                    "local_date, meastype, position, metric, value, unit_label, raw_value, "
                    "raw_unit, category, attrib, algo, fm, device_model, loaded_utc) VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(measure_uid) DO UPDATE SET "
                    "value = excluded.value, raw_value = excluded.raw_value, "
                    "raw_unit = excluded.raw_unit, metric = excluded.metric, "
                    "category = excluded.category, algo = excluded.algo, "
                    "fm = excluded.fm, loaded_utc = excluded.loaded_utc",
                    (uid, grpid, taken_utc, epoch, local_date, mtype, pos,
                     metric, value, unit_label, raw_value, raw_unit,
                     grp.get("category"), grp.get("attrib"), m.get("algo"), m.get("fm"),
                     grp.get("modelid"), now),
                )
                upserted += 1

        if updatetime:
            conn.execute(
                "INSERT INTO withings_cursor (key, value, updated_utc) VALUES "
                "('lastupdate', ?, ?) ON CONFLICT(key) DO UPDATE SET "
                "value = excluded.value, updated_utc = excluded.updated_utc",
                (str(updatetime), now),
            )

    if collisions:
        anomalies.append(
            f"{collisions} measurement(s) collided on an existing key, so only the last "
            "of each was kept. The API is sending a field we are not keying on - inspect "
            "the archived payload for a key beyond type/position before trusting this run."
        )

    if discovered:
        anomalies.append(
            "Undocumented measurement type(s) returned by your account: "
            + ", ".join(str(d) for d in sorted(discovered))
            + ". Compare SELECT * FROM v_withings_unknown_types against the Health Mate "
            "app and name them in withings_meastype."
        )

    status = "ok_warn" if len(anomalies) > informational else "ok"
    return {"status": status, "rows_seen": measures_seen, "rows_upserted": upserted,
            "anomalies": anomalies, "notes": f"archived {archive.name}"}
