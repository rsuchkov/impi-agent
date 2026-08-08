#!/usr/bin/env bats
# engine_logged (installer/lib/compose.sh) under `set -o pipefail`, which is what
# the wrapper and the installer both run with.

# A stand-in for the real thing: keeps writing after the marker, so a reader that
# stops early kills it with SIGPIPE — exactly how `docker compose logs` behaves
# against a live engine.
_noisy_log() {
    local i=0
    while [ "$i" -lt 3000 ]; do
        printf 'line %d\n' "$i"
        i=$((i + 1))
    done
    printf '%s\n' "$MARKER_LINE"
    i=0
    while [ "$i" -lt 3000 ]; do
        printf 'more %d\n' "$i"
        i=$((i + 1))
    done
}

setup() {
    . "$BATS_TEST_DIRNAME/../lib/tui.sh"      # die()
    . "$BATS_TEST_DIRNAME/../lib/compose.sh"
    set -o pipefail
    # AFTER the library: it defines compose() too, and the fake must win.
    compose() { _noisy_log; }
}

@test "a present marker is found (the producer's SIGPIPE is not the answer)" {
    MARKER_LINE="INFO impi.app: app built: agents=[assistant]"
    run engine_logged "app built:"
    [ "$status" -eq 0 ]
}

@test "an absent marker answers no, not an error" {
    MARKER_LINE="INFO impi.app: still starting"
    run engine_logged "app built:"
    [ "$status" -eq 1 ]
}

@test "a marker starting with a dash is a marker, not an option" {
    MARKER_LINE="-- app built: agents=[assistant]"
    run engine_logged "-- app built:"
    [ "$status" -eq 0 ]
}

@test "grep -q would have reported the opposite — the bug this guards" {
    run bash -c '
        set -o pipefail
        producer() {
            i=0; while [ $i -lt 3000 ]; do echo "line $i"; i=$((i + 1)); done
            echo "app built: agents=[assistant]"
            i=0; while [ $i -lt 3000 ]; do echo "more $i"; i=$((i + 1)); done
        }
        producer | grep -q "app built:"
    '
    [ "$status" -ne 0 ]  # the match IS there, yet the pipeline reports failure
}
