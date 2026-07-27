#!/usr/bin/env bats
# Unit tests for derive_compose_files (installer/lib/compose.sh).

setup() {
    . "$BATS_TEST_DIRNAME/../lib/tui.sh"      # die()
    . "$BATS_TEST_DIRNAME/../lib/compose.sh"
}

@test "codeploy adds the mattermost overlay" {
    COMPOSE_ROOTLESS=0
    run derive_compose_files codeploy
    [ "$output" = "deploy/compose.yaml deploy/compose.mattermost.yaml" ]
}

@test "external adds the external-mm overlay" {
    COMPOSE_ROOTLESS=0
    run derive_compose_files external
    [ "$output" = "deploy/compose.yaml deploy/compose.external-mm.yaml" ]
}

@test "slack is the base file only" {
    COMPOSE_ROOTLESS=0
    run derive_compose_files slack
    [ "$output" = "deploy/compose.yaml" ]
}

@test "rootless podman appends the podman overlay" {
    COMPOSE_ROOTLESS=1
    run derive_compose_files codeploy
    [ "$output" = "deploy/compose.yaml deploy/compose.mattermost.yaml deploy/compose.podman.yaml" ]
}

@test "unknown mode dies" {
    COMPOSE_ROOTLESS=0
    run derive_compose_files nonsense
    [ "$status" -ne 0 ]
}

_stub_runtimes() { # creates fake docker/podman honoring "compose version"
    STUBS="$BATS_TEST_TMPDIR/stubs"
    mkdir -p "$STUBS"
    for rt in docker podman; do
        cat > "$STUBS/$rt" <<'EOF'
#!/bin/sh
case "$1 $2" in
    "compose version") exit 0 ;;
    "info --format") echo true ;;
esac
exit 0
EOF
        chmod +x "$STUBS/$rt"
    done
    PATH="$STUBS:$PATH"
}

@test "detect_compose prefers docker when both runtimes exist" {
    _stub_runtimes
    detect_compose
    [ "$COMPOSE_RUNTIME" = docker ]
    [ "$COMPOSE_CMD" = "docker compose" ]
}

@test "detect_compose honors an explicit podman preference" {
    _stub_runtimes
    detect_compose podman
    [ "$COMPOSE_RUNTIME" = podman ]
    [ "$COMPOSE_CMD" = "podman compose" ]
}

@test "detect_compose falls back when the preferred runtime is missing" {
    _stub_runtimes
    rm "$STUBS/podman"
    detect_compose podman   # asked for podman, only docker exists
    [ "$COMPOSE_RUNTIME" = docker ]
}

@test "every derived file exists in the repo" {
    COMPOSE_ROOTLESS=1
    for mode in codeploy external slack; do
        for f in $(derive_compose_files "$mode"); do
            [ -f "$BATS_TEST_DIRNAME/../../$f" ]
        done
    done
}
