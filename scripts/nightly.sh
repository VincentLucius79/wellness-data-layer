#!/usr/bin/env bash
# Nightly sync: Garmin -> Tonal -> Withings -> wellness.db -> read-only snapshot
# -> dashboard page -> an optional published copy (PUBLISH_DIR).
#
# Written to be scheduled through sudo by an agent runtime (OpenClaw here),
# which runs:
#   sudo -H -u <you> /path/to/wellness-data-layer/scripts/nightly.sh
# The runtime's own user is permitted, by a sudoers rule, to run exactly that
# one path, with no arguments and no shell.
#
# Each source may fail without taking the others down. The exit code is
# non-zero if anything was rejected or errored, so the job records an error
# and `sync_all.py status` says which source.

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HEALTH_DIR="${HEALTH_DIR:-$HOME/HealthData}"
TONEGET_DIR="${TONEGET_DIR:-$HOME/projects/toneget}"
LOG="$HEALTH_DIR/sync.log"

# One interpreter for the whole stack, so a scheduled run behaves exactly
# like a hand-run one regardless of which venv happens to be active.
PY="${PY:-$HOME/projects/health-venv/bin/python}"
[[ -x "$PY" ]] || PY="$(command -v python3)"

mkdir -p "$HEALTH_DIR" "$HEALTH_DIR/tonal_exports"
cd "$HEALTH_DIR" || exit 1

# Keep the real stdout on fd 3. Everything verbose goes to the log; a short
# summary goes back up fd 3 at the end, so the scheduler's run record is
# useful on its own instead of just pointing at a file.
exec 3>&1
exec >> "$LOG" 2>&1

echo "=============================================================="
echo "$(date -Is) starting nightly sync"

# --------------------------------------------------------------- secrets
# Primary source: ~/.secrets/sync.env - mode 0600, owned by this user,
# outside every directory any agent can reach. Sourced, never echoed.
#
# `pass` remains the fallback for hand-runs, but is consulted ONLY when a
# terminal is attached. Without one, gpg blocks on a pinentry prompt that
# nobody can answer and the run stalls for minutes before failing anyway.
SECRETS_ENV="${SECRETS_ENV:-$HOME/.secrets/sync.env}"
SECRET_VARS=(WITHINGS_CLIENT_ID WITHINGS_CLIENT_SECRET TONAL_EMAIL TONAL_PASSWORD)

if [[ -r "$SECRETS_ENV" ]]; then
    mode="$(stat -c '%a' "$SECRETS_ENV" 2>/dev/null || echo '?')"
    [[ "$mode" == "600" ]] || echo "WARN: $SECRETS_ENV is mode $mode, expected 600"
    set -a
    . "$SECRETS_ENV"
    set +a
    echo "secrets: loaded $SECRETS_ENV"
else
    echo "WARN: $SECRETS_ENV missing or unreadable"
fi

for v in "${SECRET_VARS[@]}"; do
    printf -v "$v" '%s' "${!v:-}"
    export "$v"
done

if [[ -t 0 ]] && command -v pass >/dev/null 2>&1; then
    [[ -n "$WITHINGS_CLIENT_ID" ]]     || WITHINGS_CLIENT_ID="$(pass show withings/client_id 2>/dev/null)"
    [[ -n "$WITHINGS_CLIENT_SECRET" ]] || WITHINGS_CLIENT_SECRET="$(pass show withings/client_secret 2>/dev/null)"
    [[ -n "$TONAL_EMAIL" ]]            || TONAL_EMAIL="$(pass show tonal/email 2>/dev/null)"
    [[ -n "$TONAL_PASSWORD" ]]         || TONAL_PASSWORD="$(pass show tonal/password 2>/dev/null)"
    echo "secrets: terminal attached, pass consulted for any gaps"
fi

missing=0
for v in "${SECRET_VARS[@]}"; do
    [[ -n "${!v}" ]] || { echo "WARN: $v is empty"; missing=1; }
done
(( missing == 0 )) || echo "WARN: credentials incomplete - expect authentication failures below"
echo "interpreter: $PY"

# ---------------------------------------------------------------- 1. Garmin
echo "--- garmindb"
GARMINDB="$(dirname "$PY")/garmindb_cli.py"
[[ -x "$GARMINDB" ]] || GARMINDB="$(command -v garmindb_cli.py || true)"
if [[ -n "$GARMINDB" ]]; then
    "$GARMINDB" --all --download --import --analyze --latest \
        || echo "WARN: garmindb download/import failed; continuing with what's on disk"
else
    echo "WARN: garmindb_cli.py not found; skipping download"
fi

# ----------------------------------------------------------------- 2. Tonal
# --full matters: toneget's default trimming DELETES the per-set `reps`
# field. Bigger file, complete data.
# Requires the local patch that reads TONAL_EMAIL / TONAL_PASSWORD; stock
# toneget only prompts, which would hang a scheduled run forever.
echo "--- toneget"
if [[ -f "$TONEGET_DIR/sync_workouts.py" ]]; then
    (
        cd "$HEALTH_DIR/tonal_exports" || exit 1
        "$PY" "$TONEGET_DIR/sync_workouts.py" --full --no-gzip < /dev/null \
            || echo "WARN: toneget export failed; the guard will keep the last good data"
    )
else
    echo "WARN: toneget not found at $TONEGET_DIR; skipping"
fi

# -------------------------------------------------- 3. load everything + snapshot
echo "--- wellness-data-layer"
cd "$REPO" || exit 1
"$PY" sync_all.py all
rc=$?

# ------------------------------------------------------------ 4. dashboard
# Built from the snapshot `all` just wrote, never from wellness.db. A failed
# build leaves the previous page up; its header says how old it is. It does
# not change rc, which reports the sources.
echo "--- dashboard"
"$PY" sync_all.py dashboard \
    || echo "WARN: dashboard build failed; the previous page stays up"

# -------------------------------------------------------------- 5. publish
# Optional. PUBLISH_DIR is a folder another machine serves the page from:
# on WSL2, say, a Windows folder that Tailscale serves to a phone. It comes
# from the environment, or from a PUBLISH_DIR= line in $LOCAL_ENV (default:
# local.env in the repo, gitignored). That line is read, never sourced.
# Unset or empty skips this step.
# Runs whatever rc is (a stale run still builds a valid page and says so on
# it) and, like the build, never changes rc.
echo "--- publish"
DASH_SRC="$HEALTH_DIR/dashboard/index.html"
LOCAL_ENV="${LOCAL_ENV:-$REPO/local.env}"
if [[ -z "${PUBLISH_DIR:-}" && -r "$LOCAL_ENV" ]]; then
    PUBLISH_DIR="$(sed -n 's/^PUBLISH_DIR=//p' "$LOCAL_ENV" | tail -n 1)"
    PUBLISH_DIR="${PUBLISH_DIR%\"}"; PUBLISH_DIR="${PUBLISH_DIR#\"}"
fi
PUBLISH_DIR="${PUBLISH_DIR:-}"
# A WSL drive mount (/mnt/c/...) is checked first: unmounted, it is usually
# still an empty directory, so mkdir -p + cp would "succeed" onto the Linux disk.
PUBLISH_MOUNT=""
[[ "$PUBLISH_DIR" =~ ^(/mnt/[a-z])(/|$) ]] && PUBLISH_MOUNT="${BASH_REMATCH[1]}"
if [[ -z "$PUBLISH_DIR" ]]; then
    echo "publish: no PUBLISH_DIR set; skipped"
elif [[ -n "$PUBLISH_MOUNT" ]] && ! mountpoint -q "$PUBLISH_MOUNT"; then
    echo "WARN: $PUBLISH_MOUNT is not mounted; dashboard not published to $PUBLISH_DIR"
elif [[ ! -f "$DASH_SRC" ]]; then
    echo "WARN: $DASH_SRC missing; nothing to publish"
else
    { mkdir -p "$PUBLISH_DIR" && cp "$DASH_SRC" "$PUBLISH_DIR/index.html" \
        && echo "dashboard published to $PUBLISH_DIR/index.html"; } \
        || echo "WARN: dashboard publish to $PUBLISH_DIR failed; the previous copy stays"
fi

echo "$(date -Is) finished with exit code $rc"

# Short summary back to the caller; the scheduler captures this.
{
    echo "nightly sync finished rc=$rc  log=$LOG"
    tail -n 40 "$LOG"
} >&3

exit $rc
