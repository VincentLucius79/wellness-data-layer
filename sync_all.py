#!/usr/bin/env python3
"""Orchestrator for the wellness data layer.

    python sync_all.py init                  # create / migrate wellness.db
    python sync_all.py all                   # garmin + tonal + withings
    python sync_all.py garmin
    python sync_all.py tonal [export.json.gz]
    python sync_all.py withings [--full]
    python sync_all.py withings-auth         # one-time OAuth2 handshake
    python sync_all.py inspect-garmin        # print GarminDB's real schema
    python sync_all.py verify-day 2026-10-01 # raw source values for one day
    python sync_all.py status                # freshness + row counts
    python sync_all.py dashboard [out.html]  # static page from wellness-ro.db
    python sync_all.py brief                 # plain-text digest from wellness-ro.db
    python sync_all.py sleep-catchup         # the night ending this morning (sleep_catchup.sh)
    python sync_all.py ask <query> [k=v ...] # one catalog query, JSON (ask list)
    python sync_all.py demo [--out DIR]      # an invented person's dashboard, no accounts
    python sync_all.py fetch-catalog         # download the Tonal movement catalog

Exit codes: 0 all sources ok (advisory warnings included), 1 at least one
rejected, 2 at least one errored, 3 everything loaded but at least one
source's data is stale. With more than one, the first of 2, 1, 3 wins.
sleep-catchup exits 0 unless something failed (2). demo exits 0 only when
every source loaded clean (`ok`).
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime
from pathlib import Path

from wellness import (ask, brief, config, dashboard, db, demo, garmin, guard, sleep_catchup,
                      tonal, withings)

SOURCES = ("garmin", "tonal", "withings")


def _report(source: str, result: dict) -> None:
    icon = {"ok": "ok   ", "ok_warn": "warn ", "rejected": "REJECT", "error": "ERROR"}
    print(f"[{icon.get(result['status'], '?')}] {source}: "
          f"{result.get('rows_upserted') or 0} row(s) written, "
          f"{result.get('rows_seen') or 0} seen")
    if result.get("notes"):
        print(f"         {result['notes']}")
    for note in result.get("anomalies") or []:
        print(f"         - {note}")


def _today(cfg) -> date:
    return datetime.now(cfg.timezone).date()


def _stale_after(cfg, source: str) -> int:
    return int(getattr(cfg, source).get("stale_after_days", guard.STALE_AFTER_DAYS[source]))


def _run(conn, cfg, source: str, *, today: date | None = None, **kwargs) -> dict:
    fn = {"garmin": garmin.sync, "tonal": tonal.sync, "withings": withings.sync}[source]
    with db.sync_run(conn, source) as run:
        try:
            result = fn(conn, cfg, **kwargs)
        except Exception as exc:  # noqa: BLE001 - one bad source must not kill the rest
            result = {"status": "error", "rows_seen": 0, "rows_upserted": 0,
                      "anomalies": [f"{type(exc).__name__}: {exc}"]}
        # A load can succeed while the data behind it has stopped advancing.
        # Stale demotes ok to ok_warn; it never fails this or any other source.
        fresh = guard.evaluate_freshness(conn, source, today=today or _today(cfg),
                                         stale_after_days=_stale_after(cfg, source))
        # evaluate_freshness warns only past the limit, so this is staleness
        # alone. exit_code() needs it: ok_warn also covers advisory warnings.
        result["stale"] = fresh.status != "ok"
        if fresh.anomalies:
            result["anomalies"] = list(result.get("anomalies") or []) + fresh.anomalies
            if result["status"] == "ok":
                result["status"] = fresh.status
        run["status"] = result["status"]
        run["rows_seen"] = result.get("rows_seen")
        run["rows_upserted"] = result.get("rows_upserted")
        run["anomalies"] = result.get("anomalies") or []
        run["notes"] = result.get("notes")
        return result


def exit_code(results: dict[str, dict]) -> int:
    """2 errored, 1 rejected, 3 stale, 0 otherwise.

    Stale gets its own code because the load worked: the data just stopped
    advancing upstream, which nothing else would surface to the scheduler.
    Every other warning (an unannotated history gap, an assumed timezone)
    is advisory and stays 0, so an alert always means something to act on.
    """
    statuses = {r["status"] for r in results.values()}
    if "error" in statuses:
        return 2
    if "rejected" in statuses:
        return 1
    if any(r.get("stale") for r in results.values()):
        return 3
    return 0


def cmd_status(conn, cfg, today: date | None = None) -> None:
    # "last good" says the load worked; "newest data" says the source is
    # still advancing. Only the second one catches a stale upstream.
    print("Freshness")
    health = {r["source"]: r for r in conn.execute("SELECT * FROM v_data_health")}
    for source in SOURCES:
        r = health.get(source)
        f = guard.freshness(conn, source, today=today or _today(cfg),
                            stale_after_days=_stale_after(cfg, source))
        newest = f"{f.newest} ({f.age_days}d)" if f.newest else "none"
        if f.through != f.newest:
            newest += f" annotated to {f.through}"
        print(f"  {source:<9} last good: {(r['last_good_sync_utc'] if r else None) or 'never':<21} "
              f"rejected: {r['rejected_runs'] if r else 0}  errors: {r['errored_runs'] if r else 0}  "
              f"newest data: {newest}"
              + (f"  STALE (limit {f.limit_days}d)" if f.stale else ""))

    print("\nRow counts")
    for table in ("tonal_workout", "tonal_set", "tonal_movement", "garmin_daily",
                  "garmin_activity", "withings_measure"):
        n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table:<18} {n}")

    gaps = conn.execute(
        "SELECT gap_kind, COUNT(*) n FROM v_coverage_gaps GROUP BY gap_kind"
    ).fetchall()
    if gaps:
        print("\nCoverage gaps")
        for g in gaps:
            print(f"  {g['gap_kind']:<16} {g['n']} day(s)")

    unknown = conn.execute("SELECT * FROM v_withings_unknown_types").fetchall()
    if unknown:
        print("\nUnnamed Withings measurement types")
        for u in unknown:
            print(f"  type {u['meastype']:<5} {u['observations']} obs, "
                  f"{u['first_day']}..{u['last_day']}, "
                  f"range {u['min_value']}..{u['max_value']}")

    unnamed = conn.execute(
        "SELECT * FROM v_tonal_unknown_movements ORDER BY sets DESC").fetchall()
    if unnamed:
        print("\nUnnamed Tonal movements (name them in reference/tonal-movement-names.toml)")
        for u in unnamed:
            print(f"  {u['movement_id']}  {u['sets']} sets, "
                  f"{'two-sided' if u['two_sided_observed'] else 'one side'}, "
                  f"{u['first_day']}..{u['last_day']}")

    unmapped = conn.execute(
        "SELECT target_column FROM garmin_column_map WHERE mapped = 0"
    ).fetchall()
    if unmapped:
        print("\nGarmin columns not found in your GarminDB install")
        print("  " + ", ".join(r[0] for r in unmapped))


def cmd_snapshot(conn, cfg, dest: str | None = None) -> Path:
    """Write a clean, consistent copy for the agent to read.

    The agent gets this file, not the one the syncs write to. VACUUM INTO takes
    a transactionally consistent snapshot, and the swap is atomic, so a query
    running mid-sync can never see a half-loaded database.
    """
    target = Path(dest).expanduser() if dest else cfg.wellness_db.with_name("wellness-ro.db")
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.unlink(missing_ok=True)
    conn.execute("VACUUM INTO ?", (str(tmp),))
    os.replace(tmp, target)
    os.chmod(target, 0o444)
    return target


def _open_live(cfg):
    """wellness.db with the schema applied and any pending migration run, as
    every command that writes it needs it."""
    conn = db.connect(cfg.wellness_db)
    db.apply_schema(conn)
    for line in tonal.migrate(conn, cfg):
        print(line)
    return conn


def cmd_sleep_catchup(cfg, *, now: datetime | None = None, source=None) -> int:
    """Load the night that ended this morning, hours before the nightly would.

    0 unless something failed: no night yet, a night already loaded and an
    expired GarminDB token are all normal mornings. The steps and the log
    line are in wellness/sleep_catchup.py.
    """
    now = now or datetime.now(cfg.timezone)
    rec, asleep = sleep_catchup.attempt(cfg, now, source)
    if rec["result"] == sleep_catchup.FOUND:
        try:
            conn = _open_live(cfg)
            try:
                result = _run(conn, cfg, "garmin", today=now.date())
                _report("garmin", result)
                # Stale is left alone: the night just loaded doesn't count
                # toward Garmin's freshness, and the nightly run has already
                # exited 3 for it.
                if result["status"] not in ("ok", "ok_warn"):
                    raise RuntimeError(f"the Garmin load ended {result['status']}: "
                                       + "; ".join(result.get("anomalies") or []))
                held = conn.execute("SELECT sleep_total_s FROM garmin_daily WHERE day = ?",
                                    (rec["night"],)).fetchone()
                if not held or held[0] != asleep:
                    raise RuntimeError(f"the loader holds {held[0] if held else 'nothing'} s "
                                       f"asleep for {rec['night']}, not {asleep}")
                print(f"Snapshot for the agent: {cmd_snapshot(conn, cfg)}")
            finally:
                conn.close()
            rec["result"] = sleep_catchup.LOADED
        except Exception as exc:  # noqa: BLE001 - recorded, then exit 2
            sleep_catchup.failed(rec, exc)
    path = sleep_catchup.record(cfg, rec)
    print(f"Sleep catch-up: {sleep_catchup.describe(rec)}")
    print(f"Logged to {path}")
    return 2 if rec["result"] == sleep_catchup.ERROR else 0


def cmd_demo(out: str | None = None, *, today: date | None = None,
             now: datetime | None = None, forbidden: list[Path] | None = None,
             seed: int = demo.SEED, catalog: list | None | str = "auto") -> int:
    """Generate the invented dataset, load it through the real loaders and
    guard, and build the snapshot and the dashboard from it. Exit 0 only if
    every source came back `ok`: a warning here is a generator bug.
    `catalog` "auto" uses the fetched movement catalog when there is one."""
    from zoneinfo import ZoneInfo
    target = Path(out).expanduser() if out else demo.DEFAULT_OUT
    demo.prepare(target, forbidden=demo.real_data_paths() if forbidden is None else forbidden)
    tz = ZoneInfo(demo.TZ_NAME)
    now = now or datetime.now(tz)
    today = today or now.date()
    if catalog == "auto":
        catalog = demo.real_catalog()
    ds = demo.generate(today, now, seed=seed, catalog=catalog)
    cfg = config.load(demo.write_sources(ds, target))
    print(f"Demo data: invented, {len(ds.withings)} weigh-ins, {len(ds.sleeps)} nights, "
          f"{len(ds.tonal['workouts'])} Tonal workouts; movement names "
          + ("from the fetched catalog" if catalog else "invented") + ".")
    conn = _open_live(cfg)
    try:
        a = ds.annotation
        conn.execute("INSERT INTO annotations (source, start_date, end_date, note, recorded_by, "
                     "label) VALUES (:source, :start_date, :end_date, :note, :recorded_by, :label)",
                     a)
        conn.commit()
        results = {}
        real_fetch = withings.fetch_measures
        withings.fetch_measures = lambda cfg, **kw: (ds.withings, 1, int(now.timestamp()), True)
        try:
            for source in SOURCES:
                kwargs = ({"full": True, "ignore_rate_limit": True} if source == "withings"
                          else {})
                results[source] = _run(conn, cfg, source, today=today, **kwargs)
                _report(source, results[source])
        finally:
            withings.fetch_measures = real_fetch
        print(f"\nSnapshot: {cmd_snapshot(conn, cfg)}")
    finally:
        conn.close()
    page = dashboard.build(cfg, today=today)
    print(f"Dashboard: {page}")
    unclean = [s for s, r in results.items() if r["status"] != "ok"]
    if unclean:
        print(f"Demo FAILED: {', '.join(unclean)} did not load clean. The generator "
              "broke an invariant; the warnings above say which.")
        return 2
    print("Every source loaded clean. Open the page in a browser; it needs no server.")
    return 0


def cmd_withings_auth(cfg) -> None:
    missing = [k for k, v in cfg.secret_sources.items() if v == "missing"]
    if missing:
        raise SystemExit(
            "Missing Withings credential(s): " + ", ".join(missing) + ".\n"
            "Either set them in config.toml, or export WITHINGS_CLIENT_ID and "
            "WITHINGS_CLIENT_SECRET in your shell (see README)."
        )
    # Says where each credential came from, never what it is.
    print("Credentials: " + ", ".join(
        f"{k}={v}" for k, v in sorted(cfg.secret_sources.items())) + "\n")
    print("1. Open this URL and approve access:\n")
    print("   " + withings.authorize_url(cfg) + "\n")
    print("2. Withings redirects to your callback URL. It will not load - that's fine.")
    print("   Copy the whole URL out of the address bar and paste it here.\n")
    pasted = input("Redirected URL (or just the code): ").strip()
    code = pasted
    if "code=" in pasted:
        from urllib.parse import parse_qs, urlparse
        code = parse_qs(urlparse(pasted).query).get("code", [""])[0]
    if not code:
        raise SystemExit("No authorization code found in that input.")
    tokens = withings.exchange_code(cfg, code)
    print(f"\nTokens saved to {cfg.withings_token} (userid {tokens.get('userid')}).")
    print("Now run: python sync_all.py withings --full")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    # The agent's entry point. Its arguments may be hostile, so they never
    # reach argparse, where `--config=...` would be read as an option.
    if argv[:1] == ["ask"]:
        return ask.main(argv[1:])

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=[
        "init", "all", "garmin", "tonal", "withings", "withings-auth",
        "inspect-garmin", "verify-day", "status", "snapshot", "dashboard", "brief",
        "sleep-catchup", "demo", "fetch-catalog",
    ])
    ap.add_argument("arg", nargs="?",
                    help="export path, a date for verify-day, or the dashboard output path")
    ap.add_argument("--config", help="path to config.toml")
    ap.add_argument("--full", action="store_true", help="withings: full pull, ignore cursor")
    ap.add_argument("--force", action="store_true", help="tonal: reprocess a seen export")
    ap.add_argument("--ignore-rate-limit", action="store_true")
    ap.add_argument("--out", help="demo: the folder to build it in (default ./demo)")
    args = ap.parse_args(argv)

    # Neither needs a config.toml, so both work in a fresh clone.
    if args.command == "demo":
        return cmd_demo(args.out)
    if args.command == "fetch-catalog":
        print(tonal.fetch_catalog())
        return 0

    cfg = config.load(args.config)

    if args.command == "withings-auth":
        cmd_withings_auth(cfg)
        return 0

    if args.command == "inspect-garmin":
        found = garmin.discover(cfg)
        if not found:
            print(f"No GarminDB files under {cfg.garmin_db_dir}")
            return 2
        for fname, tables in found.items():
            print(f"\n=== {fname}")
            for tname, cols in sorted(tables.items()):
                print(f"  {tname}: {', '.join(cols)}")
        chosen, unmapped = garmin.resolve_daily(found)
        print("\n=== chosen mapping")
        for target, (f, t, d, c) in sorted(chosen.items()):
            print(f"  {target:<20} <- {f}:{t}.{c}  (keyed on {d})")
        if unmapped:
            print("  unmapped: " + ", ".join(sorted(unmapped)))
        return 0

    if args.command == "verify-day":
        if not args.arg:
            raise SystemExit("verify-day needs a date, e.g. 2026-10-01")
        print(garmin.verify_day(cfg, args.arg))
        return 0

    # Reads only the snapshot, so it is handled before anything opens
    # (or re-applies the schema to) the live database.
    if args.command == "dashboard":
        print(f"Dashboard written to {dashboard.build(cfg, out=args.arg)}")
        return 0
    # stdout is the Telegram message: the digest, or one line saying why not.
    if args.command == "brief":
        text, rc = brief.run(cfg)
        print(text)
        return rc
    # Opens wellness.db only when there is a new night to load, so a run that
    # finds nothing leaves it, and Garmin's freshness, exactly as they were.
    if args.command == "sleep-catchup":
        return cmd_sleep_catchup(cfg)

    conn = _open_live(cfg)

    if args.command == "init":
        print(f"Schema applied to {cfg.wellness_db}")
        return 0

    if args.command == "status":
        cmd_status(conn, cfg)
        return 0

    if args.command == "snapshot":
        print(f"Snapshot written to {cmd_snapshot(conn, cfg, args.arg)}")
        return 0

    todo = SOURCES if args.command == "all" else (args.command,)
    results = {}
    for source in todo:
        kwargs: dict = {}
        if source == "tonal":
            kwargs = {"export_path": Path(args.arg) if args.arg else None,
                      "force": args.force}
        elif source == "withings":
            kwargs = {"full": args.full, "ignore_rate_limit": args.ignore_rate_limit}
        results[source] = _run(conn, cfg, source, **kwargs)
        _report(source, results[source])

    print()
    cmd_status(conn, cfg)
    if args.command == "all":
        print(f"\nSnapshot for the agent: {cmd_snapshot(conn, cfg)}")
    conn.close()
    return exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
