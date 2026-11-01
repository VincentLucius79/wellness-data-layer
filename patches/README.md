# patches/

`scripts/nightly.sh` runs toneget from `~/projects/toneget` and depends on a
local modification to it. Stock toneget only prompts for credentials, which
hangs a scheduled run forever, and it does not report how many pages failed,
so a truncated export is indistinguishable from a complete one.

`toneget-env-credentials.patch` is that change, exported from the
`local-patches` branch of the toneget clone (upstream at `ec29fb5`).

To reapply on a fresh clone:

    cd ~/projects/toneget
    git checkout -b local-patches
    git apply ~/projects/wellness-data-layer/patches/toneget-env-credentials.patch
    git commit -am "Read TONAL_EMAIL/TONAL_PASSWORD from env and report failed pages"

Without it the Tonal half of the nightly sync cannot run unattended.

## Attribution

toneget is by curlrequests, at <https://github.com/curlrequests/toneget>, and
is MIT-licensed ("MIT License, Copyright (c) 2025", in its LICENSE file). The
patch quotes a few lines of toneget's code as context for its changes. That
code remains toneget's, under toneget's license.
