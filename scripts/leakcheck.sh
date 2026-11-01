#!/usr/bin/env bash
# Leak check: is anything about to be committed, or already in history, that
# would leak personal data or a secret?
#
#   scripts/leakcheck.sh             # what is staged, plus history's file names
#   scripts/leakcheck.sh --history   # also every blob and message in history
#
# Run it before any commit (.githooks/pre-commit can do that for you) and
# with --history before the repo is made public or pushed anywhere new.
#
# Checks:
#   1. Tracked files that look like data: databases and their journals, logs,
#      run records, exports, archives, a generated dashboard, any JSON not on
#      the allowlist, config and token files, or any file over 1 MB.
#   2. Files ever added in history that look like data, by the same rules,
#      and any blob in history over 1 MB. A file deleted today is still in
#      every commit that had it.
#   3. Literal credentials in tracked files: token formats (JWT, GitHub, AWS,
#      Slack, Telegram bot, private keys, API keys) and quoted values assigned
#      to a secret-looking name. Test placeholders are allowed (see
#      PLACEHOLDER below), and so is any line marked `leakcheck: allow`.
#   4. Personal strings, if .leakcheck-terms exists: one per line, matched
#      as fixed text, case-insensitive. That file is gitignored, because a
#      list of your name, paths and ids is itself the leak.
#
# Exit 0 clean, 1 at least one hit, 2 it couldn't run. It reads the index
# (what `git commit` would record), not the working tree, so a staged file
# is checked as staged.

set -uo pipefail
# Run the last command of a pipeline in this shell, so `... | report` can
# count its hits. (Scripts run without job control, which lastpipe needs.)
shopt -s lastpipe

usage() { sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; }

HISTORY=0
for arg in "$@"; do
    case "$arg" in
        --history) HISTORY=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "leakcheck: unknown argument (try --help)" >&2; exit 2 ;;
    esac
done

top="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel 2>/dev/null)" \
    || { echo "leakcheck: not inside a git repository" >&2; exit 2; }
cd "$top" || exit 2

# Tracked files that match a data rule but are meant to be there. Exact paths.
ALLOW_FILES=(
    "wellness/dashboard.html"           # the page template, no data in it
)
MAX_BYTES=$((1024 * 1024))

# Paths that look like data. Matched case-insensitively against the path.
DATA_PATH_RE='\.(db|sqlite3?|fit|tcx|gpx|zip|gz|tgz|bz2|xz|7z|bak|orig|log|jsonl|ndjson|csv|tsv|pkl|parquet|xlsx?|html?|json)$'
DATA_PATH_RE+='|\.(db|sqlite3?)-[a-z]+$|-(journal|wal|shm)$'
DATA_PATH_RE+='|(^|/)(healthdata|tonal_exports|raw|dbs|fitfiles|dashboard|\.secrets)/'
DATA_PATH_RE+='|(^|/)config\.toml$|(^|/)([^/]*\.)?env$|tonal_workouts_'

# Token formats that are a secret wherever they appear.
TOKEN_RE='eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.'               # JWT
TOKEN_RE+='|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}'  # GitHub
TOKEN_RE+='|AKIA[0-9A-Z]{16}'                                        # AWS
TOKEN_RE+='|-----BEGIN [A-Z ]*PRIVATE KEY-----'
TOKEN_RE+='|xox[abprs]-[A-Za-z0-9-]{10,}'                            # Slack
TOKEN_RE+='|[0-9]{8,10}:AA[A-Za-z0-9_-]{30,}'                        # Telegram bot
TOKEN_RE+='|AIza[0-9A-Za-z_-]{35}'                                   # Google
TOKEN_RE+='|sk-(ant-)?[A-Za-z0-9_-]{20,}'                            # OpenAI, Anthropic
# A quoted value assigned to a secret-looking name.
ASSIGN_RE='(client_secret|client_id|password|passwd|api_?key|access_token|refresh_token|di_token|secret|token|auth)["'"'"']?[[:space:]]*[:=][[:space:]]*["'"'"'][^"'"'"'$]{6,}["'"'"']'
# Values that are obviously not secrets: test fixtures, placeholders, the
# names of environment variables, and file paths (a token's location).
PLACEHOLDER='[:=][[:space:]]*["'"'"']([~/{.][^"'"'"']*|test-[^"'"'"']*|fake[^"'"'"']*|dummy[^"'"'"']*|example[^"'"'"']*|changeme|your[-_ ][^"'"'"']*|<[^"'"'"']*>|[A-Z][A-Z0-9_]+|[A-Za-z0-9-]*NOT-TO-LOG|not a token)["'"'"']'

hits=0
LIMIT=25

# Print a section's hits (stdin), at most LIMIT lines, and count them.
report() {
    local title="$1" lines n
    lines="$(cat)"
    if [[ -z "$lines" ]]; then
        echo "ok    $title"
        return
    fi
    n="$(printf '%s\n' "$lines" | wc -l)"
    echo "HIT   $title ($n)"
    printf '%s\n' "$lines" | head -n "$LIMIT" | sed 's/^/        /'
    (( n > LIMIT )) && echo "        ... and $((n - LIMIT)) more"
    hits=$((hits + n))
}

not_allowed() {
    local f re=""
    for f in "${ALLOW_FILES[@]}"; do re+="${re:+|}^${f//./\\.}\$"; done
    grep -v -E "$re"
}

data_paths() { grep -i -E "$DATA_PATH_RE" | not_allowed; }

# ------------------------------------------------------------------ 1
git ls-files | data_paths | report "1. tracked files that look like data"

git ls-files -s | while IFS=$' \t' read -r _mode oid _stage path; do
    size="$(git cat-file -s "$oid")"
    (( size > MAX_BYTES )) && echo "$path ($size bytes)"
done | not_allowed | report "1. tracked files over $((MAX_BYTES / 1024 / 1024)) MB"

# A built page committed over the template would carry readings: the
# template keeps its placeholder, the built page doesn't.
if git ls-files --error-unmatch wellness/dashboard.html >/dev/null 2>&1; then
    # Not grep -q: it stops reading early, and under pipefail git's SIGPIPE
    # would read as a missing placeholder.
    git show :wellness/dashboard.html | grep -F '__DASHBOARD_DATA__' >/dev/null \
        || echo "wellness/dashboard.html has no __DASHBOARD_DATA__ placeholder: a built page?"
fi | report "1. the dashboard template is still a template"

# ------------------------------------------------------------------ 2
git log --all --diff-filter=A --name-only --format= | sort -u | data_paths \
    | report "2. files ever added in history that look like data"

git rev-list --objects --all \
    | git cat-file --batch-check='%(objecttype) %(objectsize) %(rest)' \
    | awk -v max="$MAX_BYTES" '$1 == "blob" && $2 > max { $1 = ""; sub(/^ /, ""); print }' \
    | awk '{ s = $1; $1 = ""; sub(/^ /, ""); print $0 " (" s " bytes)" }' \
    | not_allowed | report "2. blobs in history over $((MAX_BYTES / 1024 / 1024)) MB"

# ------------------------------------------------------------------ 3
# creds --cached (the index) or creds REV... (those revisions).
creds() {
    local where=() revs=()
    if [[ "$1" == --cached ]]; then where=(--cached); else revs=("$@"); fi
    { git grep "${where[@]}" -n -I -E "$TOKEN_RE" "${revs[@]}" \
          -- . ':!reference/tonal-movements.json'
      git grep "${where[@]}" -n -I -i -E "$ASSIGN_RE" "${revs[@]}" \
          -- . ':!reference/tonal-movements.json' | grep -v -E "$PLACEHOLDER"
    } | grep -v 'leakcheck: allow' | sort -u | cut -c1-200
}
creds --cached | report "3. literal credentials in tracked files"
if (( HISTORY )); then
    # shellcheck disable=SC2046  # one argument per revision
    creds $(git rev-list --all) | report "3. literal credentials anywhere in history"
fi

# ------------------------------------------------------------------ 4
TERMS="$top/.leakcheck-terms"
if [[ -f "$TERMS" ]]; then
    terms="$(mktemp)"
    trap 'rm -f "$terms"' EXIT
    grep -v -E '^[[:space:]]*(#|$)' "$TERMS" > "$terms"
    if [[ -s "$terms" ]]; then
        git grep -n -I -i -F -f "$terms" --cached -- . | cut -c1-200 \
            | report "4. personal strings in tracked files"
        if (( HISTORY )); then
            # shellcheck disable=SC2046
            git grep -l -I -i -F -f "$terms" $(git rev-list --all) -- . \
                | sed 's/^[0-9a-f]\{40\}://' | sort | uniq -c | sort -rn \
                | awk '{ print $2 " (in " $1 " commits)" }' \
                | report "4. personal strings in files in history"
            git log --all --format='%h %s%n%b' | grep -i -F -f "$terms" | cut -c1-200 \
                | report "4. personal strings in commit messages"
            git log --all --format='%an <%ae>%n%cn <%ce>' | sort -u | grep -i -F -f "$terms" \
                | report "4. personal strings in commit authors"
        fi
    else
        echo "skip  4. personal strings: .leakcheck-terms has no terms"
    fi
else
    echo "skip  4. personal strings: no .leakcheck-terms (see the header of this script)"
fi

if (( hits )); then
    echo "leakcheck: $hits hit(s)"
    exit 1
fi
echo "leakcheck: clean"
exit 0
