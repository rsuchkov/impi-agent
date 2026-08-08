#!/usr/bin/env bats
# install_executable (installer/lib/files.sh): the wrapper replaces itself while
# bash is still reading it, so the replacement must swap the directory entry
# rather than write into the file.

setup() {
    . "$BATS_TEST_DIRNAME/../lib/files.sh"
    TMP=$(mktemp -d "${TMPDIR:-/tmp}/impi-files.XXXXXX")
}

teardown() {
    rm -rf "$TMP"
}

@test "the new content is installed and executable" {
    printf '#!/bin/sh\necho new\n' >"$TMP/src"
    printf '#!/bin/sh\necho old\n' >"$TMP/dest"

    install_executable "$TMP/src" "$TMP/dest"

    [ "$(cat "$TMP/dest")" = "$(cat "$TMP/src")" ]
    [ -x "$TMP/dest" ]
}

@test "the destination gets a NEW inode, so a running script keeps its own" {
    printf 'old\n' >"$TMP/dest"
    before=$(ls -i "$TMP/dest" | awk '{print $1}')
    printf 'new\n' >"$TMP/src"

    install_executable "$TMP/src" "$TMP/dest"

    after=$(ls -i "$TMP/dest" | awk '{print $1}')
    [ "$before" != "$after" ]
}

@test "a script replaced mid-run finishes on its old content" {
    # The regression: `cp` over a running script makes bash resume at its old
    # byte offset inside the new file (a syntax error after a successful update).
    cat >"$TMP/src" <<'NEW'
#!/usr/bin/env bash
echo replacement
NEW
    cat >"$TMP/runner" <<RUNNER
#!/usr/bin/env bash
set -euo pipefail
. "$BATS_TEST_DIRNAME/../lib/files.sh"
install_executable "$TMP/src" "\$0"
echo "the rest of the old script still runs"
RUNNER
    chmod +x "$TMP/runner"

    run "$TMP/runner"

    [ "$status" -eq 0 ]
    [ "$output" = "the rest of the old script still runs" ]
    [ "$(cat "$TMP/runner")" = "$(cat "$TMP/src")" ]
}

@test "no leftovers beside the destination" {
    printf 'new\n' >"$TMP/src"
    printf 'old\n' >"$TMP/dest"

    install_executable "$TMP/src" "$TMP/dest"

    [ "$(ls -A "$TMP" | tr '\n' ' ')" = "dest src " ]
}
