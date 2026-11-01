#!/usr/bin/env bash
# Query catalog for the OpenClaw agent: one fixed, parameterized question
# about the health data, answered as one line of JSON on stdout.
#
#   sudo -H -u <you> /path/to/wellness-data-layer/scripts/ask.sh <query> [key=value ...]
#   ask.sh list                      # what can be asked, and with which parameters
#
# Unlike brief.sh this takes arguments, and the sudoers rule lets the caller
# pass any. So this script is the security boundary, and the caller is
# assumed hostile: a prompt-injected agent writes these arguments. It
#   - runs a query only if its name is in QUERIES below, exactly,
#   - takes at most 8 arguments, each key=value in a narrow character set,
#   - never evals, never builds a command or SQL from input, and never
#     echoes an argument back,
#   - ignores the caller's environment: a fixed interpreter, python -E -s.
# Then `sync_all.py ask` checks every value's type and range against the
# catalog in wellness/ask.py before it opens anything, and every SQL
# statement there is a fixed string. The two lists of names must match;
# the tests check that they do.
#
# stdout: the answer, or {"error": ...} with exit 64 (bad input) or 2.
# Reads only wellness-ro.db. Needs no secrets and sources none.

set -uo pipefail
export LC_ALL=C          # [a-z] means a to z, in every locale

QUERIES=(list latest day metric-window compare-windows training-frequency
         training-volume recovery-after-training extremes coverage)

# "by" says this script refused, before Python started; the tests rely on it.
reject() { printf '{"error":"%s","by":"ask.sh"}\n' "$1"; exit 64; }

(( $# >= 1 )) || reject "no query given; run: ask.sh list"
name="$1"
shift
known=0
for q in "${QUERIES[@]}"; do
    [[ "$name" == "$q" ]] && known=1
done
(( known )) || reject "unknown query; run: ask.sh list"
(( $# <= 8 )) || reject "too many parameters (at most 8)"
for arg in "$@"; do
    [[ "$arg" =~ ^[a-z_]{1,16}=[a-z0-9_-]{1,24}$ ]] \
        || reject "parameters must be key=value, using lowercase letters, digits, - and _"
done

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)" || exit 2
# The venv beside the repo, as nightly.sh uses; never one named by the caller.
PY="$(dirname "$REPO")/health-venv/bin/python"
[[ -x "$PY" ]] || PY=/usr/bin/python3
if [[ ! -x "$PY" ]]; then
    printf '{"error":"no Python interpreter found"}\n'
    exit 2
fi
cd "$REPO" || { printf '{"error":"cannot enter the repository"}\n'; exit 2; }

# -E ignores PYTHON* variables, -s the user site directory; -X utf8 for output.
exec "$PY" -E -s -X utf8 sync_all.py ask "$name" "$@"
