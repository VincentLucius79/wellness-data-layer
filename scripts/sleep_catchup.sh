#!/usr/bin/env bash
# Sleep catch-up: load the night that ended this morning, before the brief.
#
# The nightly's GarminDB run downloads only through yesterday, so the night
# ending this morning would otherwise wait a day. Scheduled mid-morning,
# between the nightly sync and the brief, through sudo:
#   sudo -H -u <you> /path/to/wellness-data-layer/scripts/sleep_catchup.sh
# As for nightly.sh, a sudoers rule allows exactly that path with no
# arguments, so don't move it or make it take any.
#
# Exit 0 is every normal morning: the night loaded, the night already loaded,
# no night in Garmin Connect yet (you woke late), or GarminDB's token expired
# (the nightly renews it). The last three never open wellness.db, so Garmin's
# freshness stays exactly as the nightly left it. Exit 2 is a real failure:
# the download, GarminDB's import, the load or the snapshot.
#
# Each run appends one JSON line to ~/HealthData/sleep_catchup.jsonl: whether
# the night was found, when you woke, and the minutes in between.
# wellness/sleep_catchup.py has the details.

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HEALTH_DIR="${HEALTH_DIR:-$HOME/HealthData}"
LOG="$HEALTH_DIR/sync.log"

# The nightly's interpreter: GarminDB is installed in it.
PY="${PY:-$HOME/projects/health-venv/bin/python}"
[[ -x "$PY" ]] || PY="$(command -v python3)"

mkdir -p "$HEALTH_DIR"

# As in nightly.sh: the detail goes to the log, and this run's part of it
# goes back up fd 3 at the end, for the scheduler's run record.
start=0
[[ -f "$LOG" ]] && start="$(wc -l < "$LOG")"
exec 3>&1
exec >> "$LOG" 2>&1

echo "=============================================================="
echo "$(date -Is) starting sleep catch-up"
echo "interpreter: $PY"

# No secrets are sourced: GarminDB signs in with its own cached token, and
# nothing else here talks to a service. stdin is /dev/null so that a login
# that falls back to asking for an MFA code fails at once instead of hanging.
cd "$REPO" || exit 1
"$PY" sync_all.py sleep-catchup < /dev/null
rc=$?

# ---------------------------------------------------- dashboard and publish
# The same two steps as nightly.sh, taken only when there is something new:
# a page older than the snapshot, a published copy older than the page. A run
# that loaded nothing rebuilds and copies nothing. Neither step changes rc.
SNAPSHOT="$HEALTH_DIR/wellness-ro.db"
DASH_SRC="$HEALTH_DIR/dashboard/index.html"

echo "--- dashboard"
if [[ "$SNAPSHOT" -nt "$DASH_SRC" ]]; then
    "$PY" sync_all.py dashboard \
        || echo "WARN: dashboard build failed; the previous page stays up"
else
    echo "dashboard: no newer snapshot, nothing to rebuild"
fi

# Optional. PUBLISH_DIR is a folder another machine serves the page from:
# on WSL2, say, a Windows folder that Tailscale serves to a phone. It comes
# from the environment, or from a PUBLISH_DIR= line in $LOCAL_ENV (default:
# local.env in the repo, gitignored). That line is read, never sourced.
# Unset or empty skips this step.
echo "--- publish"
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
elif [[ ! -f "$DASH_SRC" ]]; then
    echo "WARN: $DASH_SRC missing; nothing to publish"
elif [[ -n "$PUBLISH_MOUNT" ]] && ! mountpoint -q "$PUBLISH_MOUNT"; then
    echo "WARN: $PUBLISH_MOUNT is not mounted; dashboard not published to $PUBLISH_DIR"
elif [[ -f "$PUBLISH_DIR/index.html" && ! "$DASH_SRC" -nt "$PUBLISH_DIR/index.html" ]]; then
    echo "publish: $PUBLISH_DIR/index.html is already the newest page"
else
    { mkdir -p "$PUBLISH_DIR" && cp "$DASH_SRC" "$PUBLISH_DIR/index.html" \
        && echo "dashboard published to $PUBLISH_DIR/index.html"; } \
        || echo "WARN: dashboard publish to $PUBLISH_DIR failed; the previous copy stays"
fi

echo "$(date -Is) sleep catch-up finished with exit code $rc"

{
    echo "sleep catch-up finished rc=$rc  log=$LOG"
    tail -n +"$((start + 1))" "$LOG"
} >&3

exit $rc
