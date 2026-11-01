#!/usr/bin/env bash
# Morning brief: a short plain-text digest of wellness-ro.db, for Telegram.
#
# Scheduled by OpenClaw, which runs:
#   sudo -H -u <you> /path/to/wellness-data-layer/scripts/brief.sh
# and sends whatever this prints on stdout to Telegram. So stdout carries the
# digest and nothing else: no log file, no progress lines. Warnings go to
# stderr. A non-zero exit means there is no digest, and the single stdout
# line says why.
#
# It reads only the snapshot nightly.sh writes, so it needs no secrets and
# sources none. Don't move it or make it take arguments: a sudoers rule will
# allow exactly this path, the way it does for nightly.sh.

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# The same interpreter nightly.sh uses, so the brief sees what the sync saw.
PY="${PY:-$HOME/projects/health-venv/bin/python}"
if [[ ! -x "$PY" ]]; then
    echo "WARN: $PY not found; falling back to python3 on PATH" >&2
    PY="$(command -v python3 || true)"
fi
if [[ -z "$PY" ]]; then
    echo "Wellness brief unavailable: no Python interpreter found."
    exit 2
fi

cd "$REPO" || { echo "Wellness brief unavailable: can't enter $REPO."; exit 2; }

# A scheduler's environment may have no locale; the message is UTF-8 either way.
export PYTHONIOENCODING=utf-8
exec "$PY" sync_all.py brief
