-- wellness.db - unified schema for Garmin + Tonal + Withings
-- One file. The agent gets read-only access to this and nothing else.
--
-- Design rules:
--   1. Nothing here is ever DELETEd by a sync. Loads are upserts only, so a
--      short or truncated source export can never erase history.
--   2. Every timestamp is stored twice: UTC (authoritative) and local_date
--      (for human/daily joins). tz_source records how we knew the offset.
--   3. raw_json is kept on every row so the DB can be re-derived without
--      re-contacting the source API.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- meta

CREATE TABLE IF NOT EXISTS schema_version (
    version      INTEGER NOT NULL,
    applied_utc  TEXT    NOT NULL
);

-- Audit trail. Every sync attempt lands here, including rejected ones.
CREATE TABLE IF NOT EXISTS sync_runs (
    run_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source         TEXT    NOT NULL,          -- 'tonal' | 'garmin' | 'withings'
    started_utc    TEXT    NOT NULL,
    finished_utc   TEXT,
    status         TEXT    NOT NULL,          -- 'running'|'ok'|'ok_warn'|'rejected'|'error'
    rows_seen      INTEGER,
    rows_upserted  INTEGER,
    source_file    TEXT,
    source_sha256  TEXT,
    anomalies      TEXT,                      -- JSON array of strings
    notes          TEXT
);

CREATE INDEX IF NOT EXISTS ix_sync_runs_source ON sync_runs(source, started_utc DESC);

-- Plain-language column notes, so a local model doing text-to-SQL has a
-- dictionary instead of guessing from column names.
CREATE TABLE IF NOT EXISTS data_dictionary (
    object_name  TEXT NOT NULL,
    column_name  TEXT NOT NULL DEFAULT '',
    description  TEXT NOT NULL,
    PRIMARY KEY (object_name, column_name)
);

-- Known-good gaps. A row says missing data from this source over this date
-- range (inclusive) is expected, so it is acknowledged once instead of
-- warned about on every run. The guard reads it before flagging a history
-- gap or a stale source. A guard that cries wolf is a guard nobody reads.
CREATE TABLE IF NOT EXISTS annotations (
    annotation_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    source         TEXT NOT NULL CHECK (source IN ('tonal', 'garmin', 'withings')),
    start_date     TEXT NOT NULL,         -- YYYY-MM-DD, inclusive
    end_date       TEXT NOT NULL,         -- YYYY-MM-DD, inclusive
    note           TEXT NOT NULL,
    -- A few words for the dashboard to draw over the gap; NULL draws a
    -- generic "no <source> data expected". Added by db.ADDED_COLUMNS on
    -- databases made before it existed.
    label          TEXT,
    recorded_by    TEXT NOT NULL,
    recorded_utc   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    CHECK (start_date <= end_date),
    UNIQUE (source, start_date, end_date)
);


-- ---------------------------------------------------------------- tonal

CREATE TABLE IF NOT EXISTS tonal_workout (
    workout_id     TEXT PRIMARY KEY,
    begin_utc      TEXT,           -- ISO8601 Z; NULL if source timestamp unparseable
    begin_epoch    INTEGER,
    local_date     TEXT,           -- YYYY-MM-DD in configured local timezone
    tz_source      TEXT,           -- 'offset' | 'z' | 'assumed_local' | 'unknown'
    workout_type   TEXT,
    title          TEXT,
    template_id    TEXT,
    is_custom      INTEGER NOT NULL DEFAULT 0,
    duration_s     INTEGER,
    total_volume   REAL,           -- source units (lb) as reported
    total_reps     INTEGER,
    set_count      INTEGER,
    raw_json       TEXT NOT NULL,
    first_seen_utc TEXT NOT NULL,
    last_seen_utc  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_tonal_workout_date ON tonal_workout(local_date);
CREATE INDEX IF NOT EXISTS ix_tonal_workout_tz   ON tonal_workout(tz_source);

-- One row per set, keyed by the set's own `id`. Tonal's `setId` is NOT
-- unique: it names a prescribed set within a program and repeats across
-- workouts and across sides, so it is kept as prescribed_set_id and never
-- used as a key. (workout_id, prescribed_set_id, side_number) is unique.
--
-- Upserted, never deleted, and every column is COALESCEd on update (tonal.py).
-- This replaced a table keyed by workout_id:position that was deleted and
-- re-inserted on every load. db.RETIRED renames that one to tonal_set_v1.
--
-- Sides: a two-sided movement has one row per side. Tonal prescribes the
-- same weight to both sides (the recorded averages differ by a rounding
-- error), so collapse weights across sides (v_tonal_set_both_sides) rather
-- than compare them. Side 0 is performed first in every pair where both
-- sides have times, so side and order are confounded: if one side shows
-- more power, nothing here can say whether that is the limb or the order.
-- Don't read a per-side difference as a strength asymmetry.
--
-- Weights are lb as Tonal reports them. Where both arms pull together
-- (tonal_movement.arms_together = 1) they are per arm, and volume is about
-- 2 x avg_weight_lb x rep_count. Elsewhere, volume is about avg_weight_lb x reps.
--
-- Tonal writes Go's zero time (0001-01-01T00:00:00Z) on a set that was
-- prescribed but not performed. Such a row has begin_utc NULL,
-- tz_source 'zero_time', and 0 in every measure.
CREATE TABLE IF NOT EXISTS tonal_set (
    set_uid              TEXT PRIMARY KEY NOT NULL,  -- the set's own `id`
    workout_id           TEXT NOT NULL REFERENCES tonal_workout(workout_id),
    prescribed_set_id    TEXT,               -- `setId`: repeats, not a key
    movement_id          TEXT,               -- tonal_movement.movement_id when named
    original_movement_id TEXT,               -- the movement it replaced, if swapped
    side_number          INTEGER,            -- 0, or 1 for a two-sided movement's second side
    block_number         INTEGER,
    set_group            INTEGER,
    round                INTEGER,
    sort_order           INTEGER,
    repetition           INTEGER,            -- this is set `repetition` of `repetition_total`
    repetition_total     INTEGER,
    begin_utc            TEXT,               -- NULL for a set never performed (zero time)
    end_utc              TEXT,
    begin_epoch          INTEGER,
    local_date           TEXT,
    tz_source            TEXT,               -- as tonal_workout, plus 'zero_time'
    duration_s           INTEGER,
    prescribed_reps      INTEGER,            -- NULL on time-based sets
    prescribed_duration_s INTEGER,           -- NULL on rep-based sets
    rep_count            INTEGER,
    reps_in_reserve      REAL,
    base_weight_lb       REAL,
    avg_weight_lb        REAL,
    max_weight_lb        REAL,
    min_weight_lb        REAL,
    suggested_weight_lb  REAL,
    eccentric_weight_lb  REAL,
    chains_weight_lb     REAL,
    one_rep_max_lb       REAL,
    volume               REAL,               -- lb x reps; sums to tonal_workout.total_volume
    concentric_work      REAL,               -- newer sets only; 8.85 x total_concentric_work (in-lb per J?)
    total_concentric_work REAL,              -- likely joules
    max_con_power        REAL,
    vel_at_max_con_power REAL,
    weight_at_max_con_power_lb REAL,
    avg_velocity         REAL,               -- 0 on older sets: not measured, not slow
    rom                  REAL,               -- range of motion per rep, inches
    avg_rom              REAL,               -- 0 on older sets, like avg_velocity
    rom_length_in        REAL,               -- total travel over the set, inches
    struggling_score     REAL,
    inconsistency_score  REAL,
    warm_up              INTEGER,            -- booleans: 1 / 0
    drop_set             INTEGER,
    final_set            INTEGER,
    burnout              INTEGER,
    practice             INTEGER,
    calibration          INTEGER,
    progressive          INTEGER,
    spotter              INTEGER,
    eccentric            INTEGER,
    chains               INTEGER,
    prs                  TEXT,               -- JSON array: "Power", "Strength", "BlockVolume"
    raw_json             TEXT NOT NULL,      -- the whole set as exported, all ~94 fields
    first_seen_utc       TEXT NOT NULL,
    last_seen_utc        TEXT NOT NULL,
    UNIQUE (workout_id, prescribed_set_id, side_number)
);

CREATE INDEX IF NOT EXISTS ix_tonal_set_workout  ON tonal_set(workout_id);
CREATE INDEX IF NOT EXISTS ix_tonal_set_movement ON tonal_set(movement_id);

-- Movement names. Tonal's export carries only movement UUIDs. This is filled
-- from reference/tonal-movements.json (a vendored public catalog) and
-- reference/tonal-movement-names.toml (your own names, which win). It is
-- reference data: rows are replaced from those files on every Tonal load,
-- and a movement in neither file has no row (v_tonal_unknown_movements).
CREATE TABLE IF NOT EXISTS tonal_movement (
    movement_id    TEXT PRIMARY KEY,
    name           TEXT,
    short_name     TEXT,
    muscle_groups  TEXT,           -- JSON array, as the catalog lists them
    body_region    TEXT,
    is_two_sided   INTEGER,        -- one tonal_set row per side
    is_alternating INTEGER,
    arms_together  INTEGER,        -- onMachineInfo.trainerArmsPulledAtSameTime: weights are per arm
    on_machine     INTEGER,
    skill_level    INTEGER,
    accessory      TEXT,           -- onMachineInfo.accessory; NULL off the machine
    origin         TEXT NOT NULL,  -- 'catalog' | 'manual'
    loaded_utc     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tonal_strength_score (
    observed_date TEXT NOT NULL,
    scope         TEXT NOT NULL,   -- 'overall' | 'upper' | 'lower' | 'core'
    score         REAL,
    PRIMARY KEY (observed_date, scope)
);

CREATE TABLE IF NOT EXISTS tonal_muscle_score (
    snapshot_date TEXT NOT NULL,
    muscle        TEXT NOT NULL,
    score         REAL,
    PRIMARY KEY (snapshot_date, muscle)
);

-- Hash ledger of every raw export we have accepted or rejected.
CREATE TABLE IF NOT EXISTS tonal_export_archive (
    sha256         TEXT PRIMARY KEY,
    archived_path  TEXT NOT NULL,
    exported_at    TEXT,
    workout_count  INTEGER,
    verdict        TEXT NOT NULL,
    seen_utc       TEXT NOT NULL
);

-- ---------------------------------------------------------------- garmin
-- Copied out of GarminDB's own SQLite files. GarminDB remains the owner of
-- those; we only ever read them. Columns are filled by introspection, so any
-- one of them may be NULL if the installed GarminDB version doesn't expose it.

CREATE TABLE IF NOT EXISTS garmin_daily (
    day                   TEXT PRIMARY KEY,   -- YYYY-MM-DD, local day per Garmin
    rhr                   INTEGER,            -- resting heart rate
    hr_min                INTEGER,
    hr_max                INTEGER,
    steps                 INTEGER,
    intensity_time_s      INTEGER,
    moderate_activity_s   INTEGER,
    vigorous_activity_s   INTEGER,
    calories_total        INTEGER,
    stress_avg            INTEGER,
    bb_min                INTEGER,            -- body battery
    bb_max                INTEGER,
    sleep_total_s         INTEGER,
    sleep_deep_s          INTEGER,
    sleep_rem_s           INTEGER,
    sleep_light_s         INTEGER,
    sleep_score           INTEGER,
    weight_lb             REAL,               -- POUNDS: GarminDB's unit setting, not kg
    loaded_utc            TEXT NOT NULL
);

-- start_local is GarminDB's activities.start_time as written: local
-- wall-clock time with no offset, so there is no UTC column here. It was
-- called start_utc until 2026-10-14; db.RENAMES renames it on old databases.
CREATE TABLE IF NOT EXISTS garmin_activity (
    activity_id    TEXT PRIMARY KEY,
    start_local    TEXT,           -- local wall-clock time, no offset; NOT UTC
    local_date     TEXT,
    sport          TEXT,
    sub_sport      TEXT,
    name           TEXT,
    elapsed_s      INTEGER,
    moving_s       INTEGER,
    calories       INTEGER,
    avg_hr         INTEGER,
    max_hr         INTEGER,
    training_load  REAL,
    loaded_utc     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_garmin_activity_date ON garmin_activity(local_date);

-- Which GarminDB table/column each target column actually came from on this
-- machine. Written on every garmin sync; read when a number looks wrong.
CREATE TABLE IF NOT EXISTS garmin_column_map (
    target_table   TEXT NOT NULL,
    target_column  TEXT NOT NULL,
    source_db      TEXT,
    source_table   TEXT,
    source_column  TEXT,
    mapped         INTEGER NOT NULL,
    resolved_utc   TEXT NOT NULL,
    PRIMARY KEY (target_table, target_column)
);

-- -------------------------------------------------------------- withings
-- From the official Withings Health API (getmeas). Unlike the other two
-- sources this one is sanctioned, versioned and has real refresh tokens.
--
-- The API returns each number as value * 10^unit. We store the computed
-- float in `value` and keep raw_value/raw_unit so it can be re-derived.

CREATE TABLE IF NOT EXISTS withings_measure (
    -- grpid || ':' || meastype || ':' || position.
    -- `position` is what makes segmental data work: a BodyScan weigh-in
    -- reports the same meastype five times, once per body segment. Keying on
    -- grpid+meastype alone silently keeps one of the five.
    measure_uid   TEXT PRIMARY KEY,
    grpid         TEXT NOT NULL,        -- measurement group: one scale step or one BP reading
    taken_utc     TEXT NOT NULL,
    taken_epoch   INTEGER NOT NULL,
    local_date    TEXT NOT NULL,
    meastype      INTEGER NOT NULL,
    position      INTEGER,              -- body segment; NULL for whole-body measures
    metric        TEXT,                 -- friendly name; NULL means an undocumented code
    value         REAL NOT NULL,        -- already raw_value * 10^raw_unit
    unit_label    TEXT,
    raw_value     INTEGER,
    raw_unit      INTEGER,
    category      INTEGER,              -- 1 = real measure, 2 = user objective
    attrib        INTEGER,              -- device/attribution flag from the API
    algo          INTEGER,              -- algorithm version; a change here can shift a trend
    fm            INTEGER,              -- fat-method identifier
    device_model  INTEGER,
    loaded_utc    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_withings_day  ON withings_measure(local_date);
CREATE INDEX IF NOT EXISTS ix_withings_type ON withings_measure(meastype);
CREATE INDEX IF NOT EXISTS ix_withings_grp  ON withings_measure(grpid);

-- Seeded with the documented codes; anything else the account returns is
-- inserted as 'discovered' so BodyScan metrics the docs describe only loosely
-- still land in the DB instead of being dropped.
CREATE TABLE IF NOT EXISTS withings_meastype (
    meastype    INTEGER PRIMARY KEY,
    metric      TEXT NOT NULL,
    unit_label  TEXT,
    origin      TEXT NOT NULL           -- 'seeded' | 'discovered' | 'confirmed'
);

-- Upsert, not INSERT OR IGNORE: a type the loader already registered as
-- 'discovered' must pick up its real name when we learn it. Rows you have
-- renamed yourself (origin 'confirmed') are never touched.
INSERT INTO withings_meastype (meastype, metric, unit_label, origin) VALUES
 (1,   'weight',                  'kg',         'seeded'),
 (4,   'height',                  'm',          'seeded'),
 (5,   'fat_free_mass',           'kg',         'seeded'),
 (6,   'fat_ratio',               'percent',    'seeded'),
 (8,   'fat_mass',                'kg',         'seeded'),
 (9,   'bp_diastolic',            'mmHg',       'seeded'),
 (10,  'bp_systolic',             'mmHg',       'seeded'),
 (11,  'heart_pulse',             'bpm',        'seeded'),
 (12,  'temperature',             'C',          'seeded'),
 (54,  'spo2',                    'percent',    'seeded'),
 (71,  'body_temperature',        'C',          'seeded'),
 (73,  'skin_temperature',        'C',          'seeded'),
 (76,  'muscle_mass',             'kg',         'seeded'),
 (77,  'hydration',               'kg',         'seeded'),
 (88,  'bone_mass',               'kg',         'seeded'),
 (91,  'pulse_wave_velocity',     'm/s',        'seeded'),
 (123, 'vo2_max',                 'ml/min/kg',  'seeded'),
 (130, 'afib_qrs',                '',           'seeded'),
 (135, 'qrs_interval',            'ms',         'seeded'),
 (136, 'pr_interval',             'ms',         'seeded'),
 (137, 'qt_interval',             'ms',         'seeded'),
 (138, 'qtc_interval',            'ms',         'seeded'),
 (139, 'afib_ppg',                '',           'seeded'),
 -- Segmental. Each of these arrives five times per weigh-in, once per
 -- `position`. Named from Withings' published health-data list and confirmed
 -- against the magnitudes in real data (fat-free > muscle > fat per segment).
 (173, 'fat_free_mass_segmental', 'kg',         'seeded'),
 (174, 'fat_mass_segmental',      'kg',         'seeded'),
 (175, 'muscle_mass_segmental',   'kg',         'seeded')
ON CONFLICT(meastype) DO UPDATE SET
    metric     = excluded.metric,
    unit_label = excluded.unit_label,
    origin     = 'seeded'
WHERE withings_meastype.origin = 'discovered';

-- The nine BodyScan codes the docs above didn't name, which the loader had
-- stored as unknown_NNN. Named on 2026-10-23 from the MeasurementType enum in
-- aiowithings, the third-party Withings client that Home Assistant uses
-- (https://github.com/joostlek/python-withings, src/aiowithings/models.py),
-- because Withings' own reference renders its table in JavaScript. Each was
-- then checked against a real account's data:
--   155  starts the same day as pulse_wave_velocity, which it is derived
--        from, and nearly always arrives in the same group. Values in a
--        plausible range of ages.
--   168  + 169 equal hydration (77) to 0.01 kg on every weigh-in; 168 is
--        the usual extracellular share of it.
--   226  tracks fat-free mass closely, and Garmin's resting_metabolic_rate
--        less closely, running somewhat higher than it.
--   227  whole numbers, in a plausible range of ages.
--   196  tracks the mean of 197 and 198 almost exactly, a little above it.
-- Units for 170 and 196-198 are left NULL: nothing documents them.
-- 'confirmed', so no later reseed overwrites them. Like the block above,
-- this only replaces the loader's 'discovered' rows.
INSERT INTO withings_meastype (meastype, metric, unit_label, origin) VALUES
 (155, 'vascular_age',                      'years', 'confirmed'),
 (168, 'extracellular_water',               'kg',    'confirmed'),
 (169, 'intracellular_water',               'kg',    'confirmed'),
 (170, 'visceral_fat',                      NULL,    'confirmed'),
 (196, 'electrodermal_activity_feet',       NULL,    'confirmed'),
 (197, 'electrodermal_activity_left_foot',  NULL,    'confirmed'),
 (198, 'electrodermal_activity_right_foot', NULL,    'confirmed'),
 (226, 'basal_metabolic_rate',              'kcal',  'confirmed'),
 (227, 'metabolic_age',                     'years', 'confirmed')
ON CONFLICT(meastype) DO UPDATE SET
    metric     = excluded.metric,
    unit_label = excluded.unit_label,
    origin     = 'confirmed'
WHERE withings_meastype.origin = 'discovered';

-- Until 2026-10-23 the seed named 140 'vascular_age'. That was a mistake:
-- aiowithings has no 140 (it goes from 139 to 155), no Withings response
-- here has ever carried it, and vascular age is 155. The seed upsert never
-- touches a 'seeded' row, so the wrong one is removed here. Only while
-- nothing refers to it: a 140 that does arrive is registered as
-- 'discovered', like any other unnamed code.
DELETE FROM withings_meastype
WHERE meastype = 140 AND origin = 'seeded' AND metric = 'vascular_age'
  AND NOT EXISTS (SELECT 1 FROM withings_measure WHERE meastype = 140);

-- Keep the denormalised name on each measurement in step with the lookup.
UPDATE withings_measure SET
    metric     = (SELECT t.metric     FROM withings_meastype t WHERE t.meastype = withings_measure.meastype),
    unit_label = (SELECT t.unit_label FROM withings_meastype t WHERE t.meastype = withings_measure.meastype)
WHERE meastype IN (SELECT meastype FROM withings_meastype WHERE origin <> 'discovered');

-- Body segments, from the `position` field on a segmental measurement.
-- Confirmed against the Health Mate app on a real BodyScan account.
--
-- Note the ordering is NOT consistent between limbs: arms are numbered
-- right-then-left, legs left-then-right. Do not "tidy" this.
CREATE TABLE IF NOT EXISTS withings_position (
    position  INTEGER PRIMARY KEY,
    segment   TEXT NOT NULL,
    origin    TEXT NOT NULL             -- 'inferred' | 'confirmed'
);

INSERT INTO withings_position (position, segment, origin) VALUES
 (2,  'arm_right', 'confirmed'),
 (3,  'arm_left',  'confirmed'),
 (10, 'leg_left',  'confirmed'),
 (11, 'leg_right', 'confirmed'),
 (12, 'torso',     'confirmed')
ON CONFLICT(position) DO UPDATE SET
    segment = excluded.segment,
    origin  = excluded.origin
WHERE withings_position.origin = 'inferred';

-- Incremental cursor. getmeas supports `lastupdate`, so after the first run
-- we only ask for what changed.
CREATE TABLE IF NOT EXISTS withings_cursor (
    key          TEXT PRIMARY KEY,
    value        TEXT NOT NULL,
    updated_utc  TEXT NOT NULL
);

-- ---------------------------------------------------------------- views

DROP VIEW IF EXISTS v_tonal_day;
CREATE VIEW v_tonal_day AS
SELECT
    local_date                        AS day,
    COUNT(*)                          AS workouts,
    SUM(COALESCE(total_volume, 0))    AS volume,
    SUM(COALESCE(total_reps, 0))      AS reps,
    SUM(COALESCE(set_count, 0))       AS sets,
    SUM(COALESCE(duration_s, 0))      AS duration_s,
    MAX(CASE WHEN tz_source IN ('assumed_local', 'unknown')
             THEN 1 ELSE 0 END)       AS tz_uncertain
FROM tonal_workout
WHERE local_date IS NOT NULL
GROUP BY local_date;

-- What you did, next to what it cost you on the same day.
DROP VIEW IF EXISTS v_training_day;
CREATE VIEW v_training_day AS
SELECT
    d.day,
    t.workouts,
    t.volume,
    t.reps,
    t.sets,
    t.duration_s          AS tonal_duration_s,
    d.rhr,
    d.stress_avg,
    d.bb_min,
    d.bb_max,
    d.sleep_total_s,
    d.sleep_score,
    d.steps,
    d.intensity_time_s
FROM garmin_daily d
LEFT JOIN v_tonal_day t ON t.day = d.day;

-- The interesting one: a lift day joined to the FOLLOWING night's recovery.
DROP VIEW IF EXISTS v_next_day_recovery;
CREATE VIEW v_next_day_recovery AS
SELECT
    t.day                 AS workout_day,
    t.volume,
    t.sets,
    t.reps,
    d0.rhr                AS rhr_same_day,
    d1.day                AS next_day,
    d1.rhr                AS rhr_next_day,
    d1.rhr - d0.rhr       AS rhr_delta,
    d1.sleep_total_s      AS sleep_total_s_next,
    d1.sleep_deep_s       AS sleep_deep_s_next,
    d1.sleep_score        AS sleep_score_next,
    d1.bb_max             AS bb_max_next,
    d1.stress_avg         AS stress_avg_next
FROM v_tonal_day t
LEFT JOIN garmin_daily d0 ON d0.day = t.day
LEFT JOIN garmin_daily d1 ON d1.day = date(t.day, '+1 day');

-- One row per performed set, with the two sides of a two-sided movement
-- collapsed into one. Tonal prescribes both sides the same weight, so the
-- weights are averaged across sides. Reps and volume are summed, because
-- each side's work is real work. Per-side power is left out on purpose:
-- side 0 always goes first, so a side difference can't be told apart from
-- an order effect. Sets never performed (zero time) are excluded.
DROP VIEW IF EXISTS v_tonal_set_both_sides;
CREATE VIEW v_tonal_set_both_sides AS
SELECT
    s.workout_id,
    w.local_date                       AS day,
    COALESCE(s.prescribed_set_id, s.set_uid) AS set_key,
    s.movement_id,
    m.name                             AS movement_name,
    m.arms_together,
    COUNT(*)                           AS sides,
    MIN(s.begin_utc)                   AS begin_utc,
    MIN(s.block_number)                AS block_number,
    MIN(s.repetition)                  AS repetition,
    SUM(s.rep_count)                   AS reps,
    AVG(s.avg_weight_lb)               AS avg_weight_lb,   -- per arm when arms_together = 1
    AVG(s.one_rep_max_lb)              AS one_rep_max_lb,
    SUM(s.volume)                      AS volume,
    MAX(s.warm_up)                     AS warm_up,
    MAX(s.drop_set)                    AS drop_set,
    MAX(s.burnout)                     AS burnout,
    MAX(s.final_set)                   AS final_set
FROM tonal_set s
JOIN tonal_workout w ON w.workout_id = s.workout_id
LEFT JOIN tonal_movement m ON m.movement_id = s.movement_id
WHERE s.tz_source IS NOT 'zero_time'
GROUP BY s.workout_id, COALESCE(s.prescribed_set_id, s.set_uid), s.movement_id;

-- Per day and movement. movement_name is NULL for a movement no file names
-- yet. top_weight_lb is the heaviest set's side-averaged weight.
DROP VIEW IF EXISTS v_movement_history;
CREATE VIEW v_movement_history AS
SELECT
    day,
    movement_id,
    movement_name,
    COUNT(*)            AS sets,
    SUM(reps)           AS reps,
    SUM(volume)         AS volume,
    MAX(avg_weight_lb)  AS top_weight_lb
FROM v_tonal_set_both_sides
WHERE movement_id IS NOT NULL
GROUP BY day, movement_id;

-- Movement ids in the sets that neither reference file names. Name them in
-- reference/tonal-movement-names.toml. two_sided_observed is what the sets
-- show, the same test the loader uses against the catalog's isTwoSided.
DROP VIEW IF EXISTS v_tonal_unknown_movements;
CREATE VIEW v_tonal_unknown_movements AS
SELECT
    s.movement_id,
    COUNT(*)                                     AS sets,
    SUM(s.tz_source IS NOT 'zero_time')          AS performed_sets,
    MAX(s.side_number) >= 1                      AS two_sided_observed,
    MIN(w.local_date)                            AS first_day,
    MAX(w.local_date)                            AS last_day,
    MAX(w.title)                                 AS example_workout
FROM tonal_set s
JOIN tonal_workout w ON w.workout_id = s.workout_id
LEFT JOIN tonal_movement m ON m.movement_id = s.movement_id
WHERE s.movement_id IS NOT NULL AND m.name IS NULL
GROUP BY s.movement_id;

-- Read this before trusting any answer. One row per source.
DROP VIEW IF EXISTS v_data_health;
CREATE VIEW v_data_health AS
SELECT
    source,
    MAX(CASE WHEN status IN ('ok', 'ok_warn') THEN started_utc END) AS last_good_sync_utc,
    MAX(started_utc)                                                AS last_attempt_utc,
    SUM(CASE WHEN status = 'rejected' THEN 1 ELSE 0 END)            AS rejected_runs,
    SUM(CASE WHEN status = 'error'    THEN 1 ELSE 0 END)            AS errored_runs
FROM sync_runs
GROUP BY source;

-- Days where Tonal says you lifted but Garmin has no row at all, or vice
-- versa. A growing count here usually means one sync has quietly stalled.
DROP VIEW IF EXISTS v_coverage_gaps;
CREATE VIEW v_coverage_gaps AS
SELECT t.day, 'tonal_only' AS gap_kind
FROM v_tonal_day t
LEFT JOIN garmin_daily d ON d.day = t.day
WHERE d.day IS NULL
UNION ALL
SELECT d.day, 'garmin_no_lift' AS gap_kind
FROM garmin_daily d
LEFT JOIN v_tonal_day t ON t.day = d.day
WHERE t.day IS NULL;

-- ------------------------------------------------- withings-derived views

-- One row per weigh-in. Grouped by grpid so a scale step and a BP reading
-- taken the same morning never blend into each other.
DROP VIEW IF EXISTS v_body_composition;
CREATE VIEW v_body_composition AS
SELECT
    grpid,
    MIN(taken_utc)  AS taken_utc,
    MIN(local_date) AS day,
    MAX(CASE WHEN meastype = 1   THEN value END) AS weight_kg,
    MAX(CASE WHEN meastype = 6   THEN value END) AS fat_ratio,
    MAX(CASE WHEN meastype = 8   THEN value END) AS fat_mass_kg,
    MAX(CASE WHEN meastype = 5   THEN value END) AS fat_free_mass_kg,
    MAX(CASE WHEN meastype = 76  THEN value END) AS muscle_mass_kg,
    MAX(CASE WHEN meastype = 88  THEN value END) AS bone_mass_kg,
    MAX(CASE WHEN meastype = 77  THEN value END) AS hydration_kg,
    MAX(CASE WHEN meastype = 11  THEN value END) AS standing_pulse,
    -- BodyScan sends pulse wave velocity and vascular age (155) as a group
    -- of their own, without a weight, so for BodyScan these two columns are
    -- NULL. v_withings_day has vascular age by day.
    MAX(CASE WHEN meastype = 155 THEN value END) AS vascular_age,
    MAX(CASE WHEN meastype = 91  THEN value END) AS pulse_wave_velocity
FROM withings_measure
WHERE category = 1
GROUP BY grpid
HAVING MAX(CASE WHEN meastype = 1 THEN value END) IS NOT NULL;

-- One row per blood-pressure reading from the BP Vision.
DROP VIEW IF EXISTS v_blood_pressure;
CREATE VIEW v_blood_pressure AS
SELECT
    grpid,
    MIN(taken_utc)  AS taken_utc,
    MIN(local_date) AS day,
    MAX(CASE WHEN meastype = 10 THEN value END) AS systolic,
    MAX(CASE WHEN meastype = 9  THEN value END) AS diastolic,
    MAX(CASE WHEN meastype = 11 THEN value END) AS pulse,
    MAX(CASE WHEN meastype = 10 THEN value END)
      - MAX(CASE WHEN meastype = 9 THEN value END) AS pulse_pressure
FROM withings_measure
WHERE category = 1 AND meastype IN (9, 10, 11)
GROUP BY grpid
HAVING MAX(CASE WHEN meastype = 10 THEN value END) IS NOT NULL;

-- Daily roll-up. Body composition takes the LAST reading of the day; blood
-- pressure takes the day's average, because multiple readings are normal and
-- a single max would just report your worst moment.
DROP VIEW IF EXISTS v_withings_day;
CREATE VIEW v_withings_day AS
WITH ranked AS (
    SELECT
        local_date,
        meastype,
        value,
        ROW_NUMBER() OVER (PARTITION BY local_date, meastype
                           ORDER BY taken_epoch DESC)              AS rn,
        AVG(value)   OVER (PARTITION BY local_date, meastype)      AS avg_v,
        COUNT(*)     OVER (PARTITION BY local_date, meastype)      AS n_v
    FROM withings_measure
    WHERE category = 1
)
SELECT
    local_date AS day,
    MAX(CASE WHEN meastype = 1   AND rn = 1 THEN value END) AS weight_kg,
    MAX(CASE WHEN meastype = 6   AND rn = 1 THEN value END) AS fat_ratio,
    MAX(CASE WHEN meastype = 76  AND rn = 1 THEN value END) AS muscle_mass_kg,
    MAX(CASE WHEN meastype = 88  AND rn = 1 THEN value END) AS bone_mass_kg,
    MAX(CASE WHEN meastype = 77  AND rn = 1 THEN value END) AS hydration_kg,
    MAX(CASE WHEN meastype = 155 AND rn = 1 THEN value END) AS vascular_age,
    MAX(CASE WHEN meastype = 10  THEN avg_v END)            AS bp_systolic_avg,
    MAX(CASE WHEN meastype = 9   THEN avg_v END)            AS bp_diastolic_avg,
    MAX(CASE WHEN meastype = 10  THEN n_v   END)            AS bp_readings
FROM ranked
GROUP BY local_date;

-- --------------------------------------------------------- the wide table
-- Spine of every day any source knows about, so a day missing from one
-- source still appears instead of vanishing from an inner join.
DROP VIEW IF EXISTS v_day_spine;
CREATE VIEW v_day_spine AS
SELECT day FROM garmin_daily
UNION
SELECT day FROM v_tonal_day
UNION
SELECT day FROM v_withings_day;

DROP VIEW IF EXISTS v_day_all;
CREATE VIEW v_day_all AS
SELECT
    s.day,
    -- tonal
    t.workouts            AS lift_sessions,
    t.volume              AS lift_volume,
    t.sets                AS lift_sets,
    t.reps                AS lift_reps,
    t.tz_uncertain        AS lift_tz_uncertain,
    -- garmin
    g.rhr,
    g.steps,
    g.stress_avg,
    g.bb_min,
    g.bb_max,
    g.sleep_total_s,
    g.sleep_deep_s,
    g.sleep_score,
    g.intensity_time_s,
    -- withings
    w.weight_kg,
    w.fat_ratio,
    w.muscle_mass_kg,
    w.hydration_kg,
    w.vascular_age,
    w.bp_systolic_avg,
    w.bp_diastolic_avg,
    w.bp_readings
FROM v_day_spine s
LEFT JOIN v_tonal_day   t ON t.day = s.day
LEFT JOIN garmin_daily  g ON g.day = s.day
LEFT JOIN v_withings_day w ON w.day = s.day;

-- Lift day -> next-morning blood pressure and resting HR. This is the one
-- question none of the three apps can answer on its own.
DROP VIEW IF EXISTS v_load_vs_pressure;
CREATE VIEW v_load_vs_pressure AS
SELECT
    t.day                 AS workout_day,
    t.volume              AS lift_volume,
    t.sets                AS lift_sets,
    w0.bp_systolic_avg    AS systolic_same_day,
    w1.day                AS next_day,
    w1.bp_systolic_avg    AS systolic_next_day,
    w1.bp_diastolic_avg   AS diastolic_next_day,
    g1.rhr                AS rhr_next_day,
    g1.sleep_score        AS sleep_score_next_day
FROM v_tonal_day t
LEFT JOIN v_withings_day w0 ON w0.day = t.day
LEFT JOIN v_withings_day w1 ON w1.day = date(t.day, '+1 day')
LEFT JOIN garmin_daily   g1 ON g1.day = date(t.day, '+1 day');

-- Per-segment body composition, one row per weigh-in per body part.
-- Types 173/174/175 are Withings' segmental fat-free / fat / muscle mass.
DROP VIEW IF EXISTS v_segmental;
CREATE VIEW v_segmental AS
SELECT
    m.local_date                                   AS day,
    m.grpid,
    m.position,
    COALESCE(p.segment, 'position_' || m.position) AS segment,
    MAX(CASE WHEN m.meastype = 173 THEN m.value END) AS fat_free_mass_kg,
    MAX(CASE WHEN m.meastype = 174 THEN m.value END) AS fat_mass_kg,
    MAX(CASE WHEN m.meastype = 175 THEN m.value END) AS muscle_mass_kg,
    MAX(m.algo)                                    AS algo
FROM withings_measure m
LEFT JOIN withings_position p ON p.position = m.position
WHERE m.category = 1
  AND m.position IS NOT NULL
  AND m.meastype IN (173, 174, 175)
GROUP BY m.grpid, m.position;

-- Left/right balance per weigh-in. A positive delta means the RIGHT side
-- carries more. Judge it over weeks, not one day: a single weigh-in's
-- difference wobbles from day to day, more for the legs than the arms.
-- A steady sign can't separate the body from a fixed electrode bias.
--
-- muscle_asymmetry_pct is the difference as a share of the pair's average,
-- which is the comparable number - 0.3 kg between arms means far more than
-- 0.3 kg between legs.
DROP VIEW IF EXISTS v_segmental_balance;
CREATE VIEW v_segmental_balance AS
SELECT
    day,
    CASE WHEN segment LIKE 'arm%' THEN 'arms' ELSE 'legs' END AS pair,
    MAX(CASE WHEN segment LIKE '%\_left'  ESCAPE '\' THEN muscle_mass_kg END) AS left_muscle_kg,
    MAX(CASE WHEN segment LIKE '%\_right' ESCAPE '\' THEN muscle_mass_kg END) AS right_muscle_kg,
    ROUND(MAX(CASE WHEN segment LIKE '%\_right' ESCAPE '\' THEN muscle_mass_kg END)
        - MAX(CASE WHEN segment LIKE '%\_left'  ESCAPE '\' THEN muscle_mass_kg END), 3)
        AS muscle_delta_kg,
    ROUND(100.0
        * (MAX(CASE WHEN segment LIKE '%\_right' ESCAPE '\' THEN muscle_mass_kg END)
         - MAX(CASE WHEN segment LIKE '%\_left'  ESCAPE '\' THEN muscle_mass_kg END))
        / NULLIF((MAX(CASE WHEN segment LIKE '%\_right' ESCAPE '\' THEN muscle_mass_kg END)
                + MAX(CASE WHEN segment LIKE '%\_left'  ESCAPE '\' THEN muscle_mass_kg END)) / 2.0, 0)
        , 2) AS muscle_asymmetry_pct,
    ROUND(MAX(CASE WHEN segment LIKE '%\_right' ESCAPE '\' THEN fat_mass_kg END)
        - MAX(CASE WHEN segment LIKE '%\_left'  ESCAPE '\' THEN fat_mass_kg END), 3)
        AS fat_delta_kg
FROM v_segmental
WHERE segment IN ('arm_left', 'arm_right', 'leg_left', 'leg_right')
GROUP BY day, pair;

-- Measurement types your account actually returned that we have no name for.
-- BodyScan segmental metrics, if your account exposes them, show up here.
DROP VIEW IF EXISTS v_withings_unknown_types;
CREATE VIEW v_withings_unknown_types AS
SELECT
    m.meastype,
    COUNT(*)            AS observations,
    MIN(m.local_date)   AS first_day,
    MAX(m.local_date)   AS last_day,
    MIN(m.value)        AS min_value,
    MAX(m.value)        AS max_value
FROM withings_measure m
LEFT JOIN withings_meastype t ON t.meastype = m.meastype
WHERE t.meastype IS NULL OR t.origin = 'discovered'
GROUP BY m.meastype;

-- ---------------------------------------------------------------- dictionary

DELETE FROM data_dictionary;
INSERT INTO data_dictionary (object_name, column_name, description) VALUES
 ('tonal_workout', '',             'One row per Tonal workout session. Append-only; never deleted by a sync.'),
 ('tonal_workout', 'total_volume', 'Volume in pounds as Tonal reported it. Not recomputed from sets.'),
 ('tonal_workout', 'tz_source',    'How the UTC time was determined. "assumed_local" means the source gave no offset and the configured timezone was applied - treat cross-source joins on those rows with suspicion.'),
 ('tonal_set',     '',             'One row per set, keyed by the set''s own id. A two-sided movement has one row per side. Rows with tz_source = zero_time were prescribed but never performed. Prefer v_tonal_set_both_sides for anything about weight.'),
 ('tonal_set',     'side_number',  'Which side of a two-sided movement. Side 0 is always performed first, so side and order are confounded: a per-side difference (side 1 shows higher power) cannot be read as a left/right strength difference. Tonal prescribes both sides the same weight.'),
 ('tonal_set',     'prescribed_set_id', 'Tonal''s setId. NOT unique: it names a set within a program and repeats across workouts and sides. Use set_uid as the key.'),
 ('tonal_set',     'avg_weight_lb','Average weight in pounds. PER ARM when tonal_movement.arms_together = 1 (volume is then about 2 x avg_weight_lb x rep_count).'),
 ('tonal_set',     'volume',       'Pounds x reps as Tonal reported it. The sets of a workout sum to tonal_workout.total_volume.'),
 ('tonal_set',     'warm_up',      'Tonal''s warm-up flag. Set on about 1% of sets; where it is set, the weight is about 65% of the same movement''s later sets.'),
 ('tonal_set',     'avg_velocity', 'Populated only from 2026. A 0 before then means not measured, not zero velocity. The same applies to avg_rom.'),
 ('tonal_set_v1',  '',             'Retired 2026-10-22: the old set table, keyed by workout_id:position. Kept because nothing is deleted. Read tonal_set instead.'),
 ('tonal_movement','',             'Movement names for tonal_set.movement_id, from a vendored public catalog plus your own names (origin = manual). A movement in neither has no row: see v_tonal_unknown_movements.'),
 ('tonal_movement','arms_together','1 when both of Tonal''s arms pull at once (bars, two-handle presses). Weights on those movements are per arm.'),
 ('v_tonal_set_both_sides','',     'One row per performed set with the two sides of a two-sided movement collapsed: weights averaged across sides, reps and volume summed. The view to use for weight and volume per movement.'),
 ('v_movement_history','',         'Per day and movement, from v_tonal_set_both_sides. movement_name is NULL for a movement nobody has named yet.'),
 ('v_tonal_unknown_movements','',  'Movement ids in tonal_set that have no name yet, with how many sets they cover.'),
 ('garmin_daily',  '',             'One row per calendar day, copied from GarminDB. Any column may be NULL if the installed GarminDB version does not expose it - check garmin_column_map.'),
 ('garmin_daily',  'bb_max',       'Body battery high point for the day.'),
 ('garmin_daily',  'stress_avg',   'All-day average stress (0-100) from GarminDB''s daily summary. Not stress during sleep. Garmin''s -1/-2 "could not measure" codes are stored as NULL.'),
 ('garmin_daily',  'weight_lb',    'Body weight in POUNDS, as GarminDB stores it for an imperial Garmin account. Not kilograms. Withings weight (v_body_composition, v_withings_day, v_day_all) is in kg; divide pounds by 2.20462 before comparing the two.'),
 ('annotations',   '',             'Date ranges where missing data from a source is expected, with a note saying why (e.g. training at a gym instead of on Tonal). Read the note before calling a gap an outage.'),
 ('garmin_activity','',            'Garmin-recorded activities (runs, rides, walks). Tonal lifts do NOT appear here unless separately recorded on the watch.'),
 ('garmin_activity','start_local', 'Start time as LOCAL wall-clock time in the configured timezone, with no offset, copied from GarminDB. Not UTC: do not compare it with a *_utc column without converting.'),
 ('garmin_activity','local_date',  'Local calendar date of start_local.'),
 ('v_training_day','',             'Daily join: Tonal training load next to the same day Garmin metrics.'),
 ('v_next_day_recovery','',        'Each lift day joined to the FOLLOWING day resting HR, sleep and body battery. Use this for cost-of-training questions.'),
 ('v_data_health', '',             'Freshness per source. Query this first; if last_good_sync_utc is stale, decline to answer trend questions.'),
 ('v_coverage_gaps','',            'Days present in one source but missing in the other.'),
 ('sync_runs',     'status',       'ok = loaded, ok_warn = loaded with anomalies, rejected = integrity guard refused the export, error = crash.'),
 ('garmin_column_map','',          'Which GarminDB table and column each garmin_daily/garmin_activity column was actually read from on this machine.'),
 ('withings_measure','',           'One row per single Withings measurement from the official Health API. Rows sharing a grpid were taken in the same session (one weigh-in, or one BP reading).'),
 ('withings_measure','value',      'The real number, already computed as raw_value * 10^raw_unit. Units are per withings_meastype.unit_label - weight and masses are KILOGRAMS, not pounds.'),
 ('withings_measure','category',   '1 = an actual measurement. 2 = a user-entered target/objective. Always filter to category = 1 for analysis.'),
 ('withings_meastype','',          'Lookup of measurement type codes. origin = seeded means documented by Withings; confirmed means named by the user (the BodyScan codes 155-227, from the aiowithings library, checked against the data); discovered means the account returned a code nothing names yet.'),
 ('v_body_composition','',         'One row per BodyScan weigh-in. Masses in kg.'),
 ('v_blood_pressure','',           'One row per BP Vision reading, with pulse pressure precomputed. Multiple readings per day are normal.'),
 ('v_withings_day','',             'Daily roll-up: body composition is the LAST reading of the day, blood pressure is the day AVERAGE.'),
 ('v_day_all',     '',             'The main table for most questions. One row per day with Tonal load, Garmin recovery and Withings body/BP side by side. NULL means that source has no data for that day, which is not the same as a zero.'),
 ('v_load_vs_pressure','',         'Lift day joined to the NEXT day blood pressure, resting HR and sleep score. Use for cost-of-training and cardiovascular-response questions.'),
 ('v_withings_unknown_types','',   'Measurement codes the account returned that have no documented name.'),
 ('withings_measure','position',   'Body segment for segmental measurements; NULL for whole-body ones. A BodyScan weigh-in reports types 173/174/175 once per segment, so position is part of the primary key.'),
 ('withings_measure','algo',       'Withings algorithm version for this value. If a trend steps discontinuously, check whether algo changed on that date before believing the step.'),
 ('withings_position','',          'Maps position codes to body segments, confirmed against Health Mate. Arms are numbered right-then-left and legs left-then-right - the inconsistency is Withings own.'),
 ('v_segmental','',                'Per-segment body composition, one row per weigh-in per body part. Masses in kg.'),
 ('v_segmental_balance','',        'Left/right muscle difference per weigh-in for arms and legs. A POSITIVE delta means the right side is larger. Use muscle_asymmetry_pct to compare arms against legs, and trust a multi-week trend rather than any single day.');

INSERT INTO schema_version (version, applied_utc) VALUES (1, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'));
