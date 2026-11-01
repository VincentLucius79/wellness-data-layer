# CLAUDE.md

Loads Garmin (GarminDB's SQLite files), Tonal (toneget JSON exports) and
Withings (official Health API) into one SQLite file, `wellness.db`. The agent
only ever reads `wellness-ro.db`, a `VACUUM INTO` snapshot. README.md covers
setup and the agent contract. This file covers the rules for changing the code.

## Commands

- Tests: `python3 tests/test_pipeline.py`. It's a plain script, not pytest.
  It uses no network and builds its fixtures in a temp dir. It must end with
  `N passed, 0 failed` (447 checks as of 2026-11-01).
  For anything date-dependent, pass `today=` explicitly. The fixtures are
  dated September 2026.
- `python3 sync_all.py init | all | garmin | tonal [file] | withings [--full] | status | snapshot | dashboard [out] | brief | sleep-catchup | demo [--out DIR] | fetch-catalog | ask <query> [key=value ...]`
- Leak check: `scripts/leakcheck.sh` before a commit, and with `--history`
  before a push anywhere new. It exits non-zero on anything that looks like
  data, a literal credential, or a string from the gitignored
  `.leakcheck-terms`. `.githooks/pre-commit` runs it once you opt in with
  `git config core.hooksPath .githooks`. Keep `.gitignore` and the check's
  rules in step: the tests check both.
- Stdlib only, Python 3.11+ (`tomllib`). Don't add dependencies. The one
  import from outside the stdlib is garmindb, inside `sleep_catchup.GarminDB`,
  and only when a run needs it. The venv has it for nightly.sh already, and
  the tests never load it.

## Invariants

Each invariant below is the rule. Each one also lists where the code falls
short today (verified 2026-10-14). New code must follow the rule. Don't widen
a gap, and remove a gap from this list once it's fixed.

### 1. Nothing is ever DELETEd by a sync

Loads are upserts, so a short or truncated source can never erase history.
When an upsert updates a column, write `COALESCE(excluded.col, table.col)` so
that a value missing from a new payload can't NULL a stored one.
`garmin.py` does this for every column. Raw payloads under `archive_dir` are
never deleted.

Rebuilt every run, and fine: `garmin_column_map` holds the *current* mapping
(`garmin.py:192`). `data_dictionary` is rebuilt at `schema.sql:814`.
`tonal_movement` is reference data, replaced from `reference/` on every
Tonal run. `schema.sql` deletes the `withings_meastype` row for code 140,
a seeding mistake, only while no measurement refers to it.

Gaps:
- `UPSERT_WORKOUT` (`tonal.py:654`) overwrites `begin_utc`, `begin_epoch`,
  `local_date`, `tz_source`, `workout_type`, `is_custom`, `set_count` and
  `raw_json` without COALESCE. If a later export has an unparseable
  timestamp, it NULLs a stored good one.

### 2. `position` is part of the Withings measure key

`measure_uid = grpid:meastype:position`, with `-` for a NULL position
(`withings.py:297`). BodyScan reports types 173/174/175 once per body segment,
and only `position` tells them apart. Without it, four of every five rows
overwrite each other.

- The sync counts uid collisions within a run. A collision means the API is
  sending another discriminator that isn't in the key. Today a collision is a
  post-commit warning (`ok_warn`), not a rejection.
- `withings_position` is inconsistent on purpose: arms are 2=right, 3=left and
  legs are 10=left, 11=right. This was confirmed in Health Mate. Don't "tidy" it.

### 3. Every timestamp gets a `tz_source`

Store UTC as the authoritative value, plus `local_date` in the configured
timezone, plus `tz_source` saying how the offset was known. `tonal.parse_ts`
is the reference implementation. It emits `offset`, `z`, `epoch_s`,
`epoch_ms`, `assumed_local`, `unknown` and `zero_time` (Go's
0001-01-01 zero time, which Tonal writes on a set never performed). The
comment at `schema.sql:77` lists only four of these. `v_tonal_day.tz_uncertain` flags `assumed_local`
and `unknown`.

Gaps: only `tonal_workout` and `tonal_set` have a `tz_source` column.
- `withings_measure`: converting the epoch to UTC is exact, but nothing records that.
- `garmin_activity`: there is no UTC column. `start_local` is GarminDB's
  string copied verbatim, and `local_date = start[:10]` without any
  conversion. On the real install (checked 2026-10-14) the string is naive
  local wall-clock time: it trails Tonal's UTC by exactly the CST/CDT offset.
  So the name and `local_date` are right there. If a GarminDB version ever
  writes an offset or `Z`, the loader copies it as-is: a start at
  `2026-09-04T03:10:00Z` would land on 09-04, though it was 09-03 in Chicago.
  `dashboard._local` reads both forms. (Called `start_utc` until 2026-10-14.)
- `garmin_daily.day` is Garmin's own local day.
- `tonal_strength_score.observed_date` and `tonal_muscle_score.snapshot_date`
  are both `str(x)[:10]`.

### 4. A bad export is rejected whole, never partially absorbed

`wellness/guard.py` gives its verdict before any data row is written, and an
accepted load commits in a single transaction.

- **Tonal** history is append-only. An export is rejected if it's missing any
  workout already stored, if the workout count dropped, or if it's shorter than
  `pagination.declaredTotal`. The archived copy and the `tonal_export_archive`
  ledger row are written even on rejection. That's intended.
- **Tonal sets** follow the same rule, keyed by each set's own `id`
  (`setId` repeats and is never a key). An export is rejected if it's missing
  any stored set, or if a set has no `id`, repeats an `id`, or repeats a
  workout + `setId` + side. A set field that no set carries is a warning
  (`ok_warn`): the stored values are kept by COALESCE.
- **Withings** is rejected if the `more`/`offset` walk didn't finish. The
  `lastupdate` cursor only advances in the same transaction as the data.
- **Garmin** has no guard. It's a read-only copy, protected only by
  upsert + COALESCE.
- Statuses are `ok` / `ok_warn` / `rejected` / `error`. Exit codes come from
  `sync_all.exit_code`: 0 clean, 1 rejected, 2 errored, 3 stale. With more
  than one, the first of 2, 1, 3 wins. `ok_warn` exits 0 unless the source is
  stale. `v_data_health.last_good_sync_utc` counts both `ok` and `ok_warn`,
  so the agent trusts any run reported as `ok`. Return `ok` only when data
  really loaded.
- **Staleness** is separate from load success. After every load,
  `sync_all._run` calls `guard.evaluate_freshness`, which compares the newest
  stored day with today. A stale source turns `ok` into `ok_warn` but never
  rejects, so one stale source can't block the others. It sets
  `result["stale"]`, and that flag alone makes the run exit 3. Every other
  warning is advisory and exits 0, because the OpenClaw cron job alerts on any
  non-zero exit. An alert that fires every night stops being read. The limits
  come from `stale_after_days` in each config section, defaulting to
  `guard.STALE_AFTER_DAYS` (garmin 2, withings 2, tonal 14). Garmin is the
  source that can go stale invisibly, because GarminDB sits between the API
  and the loader. So a Garmin day counts toward freshness only if it holds
  something besides sleep (`guard.NEWEST_DAY_SQL`). The sleep catch-up loads
  this morning's night hours before the rest of the day exists, and that
  sleep-only row must not keep a stopped nightly looking fresh.
- **Annotations:** a row in `annotations` (source, inclusive date range,
  note, recorded_by, and an optional short `label` for the chart) marks a
  gap as expected. The guard treats annotated days
  as accounted for, both in the Tonal history-gap check and in staleness.
  Record a known gap there instead of loosening a threshold. A warning that
  fires on every run stops being read. None is seeded: they are each
  install's own record, not schema.

Gaps:
- Re-running an export whose hash is already in the ledger returns `ok`, even
  when its verdict was `rejected` (`tonal.py:712`). That advances
  `last_good_sync_utc`. The same thing happens when toneget fails and the
  nightly run picks up the previous file again.
- Notes from `normalize()` (no sets field, no volume field, synthetic ids) are
  added to the anomalies but never change the status. A lost sets field is
  caught anyway once sets are stored, because the guard refuses an export
  missing any stored set. On a first load it is still only a note.
- Withings silently skips groups with no `date` (`withings.py:278`) and
  measures with no type or value (`withings.py:286`).

## Other rules

- Schema changes: `sync_all.py` re-applies `schema.sql` before every command,
  so everything in it must be idempotent. There's no migration mechanism:
  `CREATE TABLE IF NOT EXISTS` won't add a column to an existing table, and
  `schema_version` just gains another `version=1` row on every apply. A new
  column needs an explicit `ALTER TABLE` path. A column rename goes in
  `db.RENAMES`, which `db.apply_schema` runs before the script, only while the
  old name exists. `garmin_daily.weight_kg -> weight_lb` and
  `garmin_activity.start_utc -> start_local` went in that way: their old
  names lied, and the agent reads column names at face value.
- A table whose shape changes under the same name goes in `db.RETIRED`:
  `db.retire_tables` renames the old one aside (never drops it) before the
  script runs. The position-keyed `tonal_set` became `tonal_set_v1` that way
  on 2026-10-22, and `tonal.migrate` refilled the new one from its
  `raw_json`.
- GarminDB files are opened `read_only=True`. Never write to them. The sleep
  catch-up doesn't either: GarminDB's own importer stores today's night, as
  it does in nightly.sh's garmindb_cli run.
- Garmin column resolution takes the first table, in file order and then
  `sqlite_master` order, that has the right column name. A name can mean
  something else in another table: `sleep.avg_stress` is not all-day stress.
  Use the table hint in `DAILY_TARGETS`, and check `garmin_column_map`
  against the real install (`sync_all.py inspect-garmin`).
- Remapping a Garmin column: the upsert's COALESCE keeps the old values on
  any day the new source lacks, which leaves two sources mixed in one column.
  Before remapping, check that no such day exists, or clear the column.
- Units stay as the source reported them: Tonal in lb, Withings in kg,
  `garmin_daily.weight_lb` in lb. Never convert in storage. A column name
  that carries a unit must carry the one the values are in.
- Withings: `value = raw_value * 10**raw_unit`. Keep the raw pair.
  `category = 2` rows are user objectives, and every analysis view filters to
  `category = 1`.
- On schema apply, the seeded `withings_meastype` rows overwrite only rows with
  `origin='discovered'`, and never update a row once written. A name the user
  confirms needs `origin='confirmed'`. Renaming a code is a mapping change,
  not a column rename: names live in `withings_meastype.metric` and its copy
  on `withings_measure`, and no table or view has a column per code. The nine
  BodyScan codes 155-227 are `confirmed`, named from the aiowithings enum
  (source and checks in `schema.sql`). There is no code 140: an old seed
  named it `vascular_age` by mistake, and `schema.sql` removes that row while
  nothing refers to it. Vascular age is 155.
- BodyScan's five segments partition the body: per weigh-in, 173/174/175
  summed over positions 2, 3, 10, 11 and 12 equal whole-body 5/8/76, and
  whole-body 5 − 76 = 88 (bone). `guard.withings_partition` checks every
  synced group, with a 0.1 kg tolerance (real scans agree to rounding). A
  failure is a warning (`ok_warn`), not a rejection.
- The Withings token file is written atomically with mode 0600, because
  refresh tokens rotate and the old one dies 8h after rotation. Polls are
  limited to one per 600s, and `withings.sync` enforces that.
- No secrets go in `config.toml` or the repo. Environment variables win
  (`~/.secrets/sync.env` for scheduled runs).
- The dashboard (`wellness/dashboard.py` builds the payload,
  `wellness/dashboard.html` is the page) reads only `wellness-ro.db`. It never
  interpolates: series are day-indexed with None for a missing day. It shows
  no composite scores. It never combines Tonal lb with Garmin minutes. Garmin's
  0 s sleep is a missing night. Every mass it shows (weight, segmental R−L
  and its noise band, Tonal volume) is converted once, in `dashboard.py`, to
  the unit `[display] units` names (default imperial), so no screen mixes lb
  and kg. Anything computed for display goes in `dashboard.py`, where the
  tests can reach it, not in the page's JS. That includes the weekly means
  the page draws when a Trends window has more days than pixels, and the
  Tonal tab's working load (`dashboard.movement_history`: median of the day's
  sets without warm-ups or weightless sets, doubled where `arms_together`),
  the header's 14-night sleep strip (`dashboard.sleep_strip`: a missing night
  is None and gets no bar. Its bars start from a floor under the usual nights,
  `dashboard.strip_axis`, only when there is a 30-day median to draw as a
  line across them. With no median the floor is zero. A night below the floor
  is a chevron, not a bar cut to fit. A session's dash sits under the night
  with the same date, the night BEFORE it, and the caption says so) and the Today tab's distance-from-median bars
  (`dashboard.deviation`: clipped at `DEV_CAP_SD` and flagged, never longer
  than the track). Trends lanes are scaled per window (`dashboard.lane_axes`:
  robust 2nd-98th percentile, both ends labelled); a reading beyond the
  range is an edge marker, never a stretched scale. Don't exclude a suspect
  reading in chart logic: there is no mechanism for that yet (`annotations`
  only marks expected gaps). The Tonal
  matrix cells come from `dashboard.quarterly_load`: a quarter not done is
  `[None, 0, None]` and is drawn as an outline, never as a low shade, and a
  shade is relative to its own row only. The detail chart's cap line is
  Tonal's 100 lb-per-arm ceiling (`TONAL_MAX_LB_PER_ARM`), in total-load
  terms per movement: 200 where `arms_together`. The detail chart opens on
  the routine's movement with the most sets over its whole history, then the
  earliest first weighted day (`dashboard.promote_key`). The Body Scan tab's
  percentages come from `dashboard.composition`: fat, muscle and bone over
  the region's OWN fat-free plus fat mass. A segment's percentage over the
  whole body's fat-free mass is the trap: a torso that is 76% muscle reads
  51%. For the whole body that mass is the weight. Bone is never measured per
  segment. It is fat-free minus muscle, labelled derived wherever shown.
  Only fat % is drawn over time: fat, muscle and bone make 100% of a region
  and bone barely moves, so a muscle % line is the fat line flipped. Muscle %
  stays a number. The tab has two colour scales and never mixes them. On the
  body map, each region is shaded off `FAT_RAMP` (darker = more fat, never
  darker than #A46634; anchors invented, 10% to 30%, never fitted to anyone's
  readings). The page reads the ramp from the payload and never
  types a colour of it. The ramp stays inside `renderMap`: the figure, its
  legend and the composition bars beside it. Everywhere else a region is a
  series and wears its identity colour. Those are the page's `--rg-<key>`
  tokens, one set per theme: one hue per body part (torso orange, arms blue,
  legs teal), with the lighter shade of a pair for the left side. The dark
  set is the one specified. The light set is the same hues darker, so the
  left shades clear 3:1 on the card. The rule is said once, in the key at
  the top of "What changed". No identity colour is a ramp colour. Torso
  orange still sits near the ramp's middle (OKLab dE 2.7), and L arm / L leg
  are the closest pair (dE 8.9, the same in greyscale), so every chart also
  names its regions in text. The map, "What changed" and the shared axis use
  full scans only (`dashboard.full_scans`: all six regions in one weigh-in),
  so they cover the segmental era. "What changed" (`dashboard.fat_change`)
  compares two actual scans, never a fitted trend. It names an endpoint
  `END_FLAG` points off the median of the `END_SCANS` scans beside it, and
  any algorithm change in between that moved a region `ALGO_STEP_NOTE` points
  (`dashboard.algo_steps`). One update has been seen to move both legs' fat
  % several points overnight, so a window across it isn't like for like for
  the legs. Region detail carries each region's own fat % history, and only
  the whole body's can reach back before the segments; don't crop it to the
  segments'. Long spans
  draw 28-day means on the `BUCKET_ANCHOR` grid, which `dashboard.html`
  shares. Muscle balance lives on Body Scan, not Trends.
  Don't add a weight-based guess at warm-ups. Its docstring has the evidence
  that Tonal's flag hides almost nothing.
- Tonal sets: a two-sided movement has one `tonal_set` row per side.
  Tonal prescribes both sides the same weight, so collapse weights across
  sides (`v_tonal_set_both_sides`) rather than plot them per side. Side 0 is
  always performed first, so side and order are confounded: a per-side
  difference, in power say, can't be read as a limb difference. Say so in a comment wherever
  per-side data is handled. Weights are per arm on movements with
  `tonal_movement.arms_together = 1`.
- `reference/tonal-movements.json` is downloaded by `sync_all.py
  fetch-catalog` from one pinned revision, installed only if its hash
  matches (`tonal.CATALOG_URL`, `tonal.CATALOG_SHA256`, also recorded in
  `reference/README.md`), and never committed: it is a dump of Tonal's own
  text with no licence to redistribute. It is gitignored. Everything works
  without it. Names for movements it lacks go in
  `reference/tonal-movement-names.toml`.
- `scripts/nightly.sh` is run by OpenClaw cron through a sudoers rule. The rule
  allows exactly that path, with no arguments, so don't move or rename the
  script or make it need arguments. Its optional last step copies the page
  to `PUBLISH_DIR`, from the environment or a `PUBLISH_DIR=` line in the
  gitignored `local.env` (read, never sourced); sleep_catchup.sh does the
  same. Nothing about one machine's folders goes in the scripts.
- `scripts/brief.sh` (`sync_all.py brief`, `wellness/brief.py`) is meant for
  the same kind of sudoers rule, so the same constraints apply. Its stdout is
  sent to Telegram as is: nothing but the digest goes there, warnings go to
  stderr, and it never redirects to a log file. Every number in it comes from
  `dashboard.py`. Word them in `brief.py`, but don't compute a statistic there.
- `scripts/sleep_catchup.sh` (`sync_all.py sleep-catchup`,
  `wellness/sleep_catchup.py`) runs mid-morning under the same kind of sudoers
  rule, so the same constraints apply. It loads the night that ended this
  morning, which the nightly's GarminDB run (through yesterday) leaves for
  the next day. Finding no night yet, a night already loaded, and an expired
  GarminDB token are normal mornings: exit 0, and none of them opens
  wellness.db, so a no-op run leaves the freshness state byte for byte as the
  nightly left it. Only a real failure (download, GarminDB's import, the load,
  the snapshot) exits non-zero. GarminDB's download loop and importer both
  log a failure and carry on, so the catch-up makes the request itself and
  reads GarminDB back to check the import. Every run appends one JSON line to
  `sleep_catchup.jsonl` beside wellness.db (found or not, wake time, minutes
  between), and each line completes the night before. That log is the
  measurement of whether its run time is the right one. Don't drop a field from it.
  The row it loads holds only sleep until the next nightly, so the brief and
  the Today tab take resting HR and body battery from the day before, each
  under that day (`dashboard.night_row_days`), instead of showing them
  missing. A full day that lacks one still shows it missing.
- No personal data in the repo: not readings, not counts, not proportions
  from anyone's dataset. A comment keeps the reasoning and drops the figure
  ("rare enough in practice", not "1 in 2,909"). A test's values are
  invented or come from the demo generator.
- `sync_all.py demo` (`wellness/demo.py`) generates an invented person and
  loads them through the real loaders and guard, then builds the snapshot
  and the page, in its own folder (default `demo/`, gitignored). It exits 0
  only if every source loads clean `ok`: a warning there is a generator bug,
  so fix the generator, never the check. Every constant in it is published
  (cited where used: segment shares from Dempster via Winter) or invented
  and labelled so. Don't tune one toward a real person's readings. It
  refuses a folder that is, holds or sits inside a real data folder, or
  the repo, and a non-empty folder it didn't make. The README's screenshots
  come from it.
- `scripts/ask.sh` (`sync_all.py ask`, `wellness/ask.py`) is the agent's only
  way into the data. Its sudoers rule allows arguments, so the script is the
  security boundary, and the caller is assumed to be a prompt-injected agent.
  Keep these rules when you add a query:
  - The query name must be in both `QUERIES` lists, the one in `ask.sh` and
    the one in `ask.py`. The tests check that the lists match.
  - Every parameter needs a typed spec (`Int`, `Choice` or `Day`) with a range.
  - No input goes into SQL, a shell command or an error message.
  - Every number leaves with the dates it covers.
  - Statistics come from `dashboard.py`.
  - `ask` is dispatched before argparse, so an argument can never be read as
    an option.
