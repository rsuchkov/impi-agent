#!/usr/bin/env bats
# Unit tests for installer/lib/checks.sh helpers.

setup() {
    . "$BATS_TEST_DIRNAME/../lib/tui.sh"
    . "$BATS_TEST_DIRNAME/../lib/compose.sh"
    . "$BATS_TEST_DIRNAME/../lib/checks.sh"
}

@test "rand_hex yields the requested number of hex bytes" {
    out=$(rand_hex 16)
    [ "${#out}" -eq 32 ]
    [[ "$out" =~ ^[0-9a-f]+$ ]]
}

@test "rand_hex output varies" {
    [ "$(rand_hex 8)" != "$(rand_hex 8)" ]
}

@test "port_free is true for an unused high port" {
    port_free 45923
}

@test "port_free is false for a listening port" {
    command -v python3 >/dev/null || skip "python3 not available"
    # Spin a throwaway listener with python (available on dev/CI machines).
    python3 -c '
import socket, sys, time
s = socket.socket()
s.bind(("127.0.0.1", 45924))
s.listen(1)
sys.stdout.write("ready\n"); sys.stdout.flush()
time.sleep(5)
' &
    listener=$!
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        if ! port_free 45924; then break; fi
        sleep 0.3
    done
    ! port_free 45924
    kill "$listener" 2>/dev/null || true
}
