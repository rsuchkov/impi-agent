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

@test "the secret store is its own axis, independent of the chat platform" {
    COMPOSE_ROOTLESS=0
    COMPOSE_VAULT=1
    run derive_compose_files slack
    [ "$output" = "deploy/compose.yaml deploy/compose.ward.yaml" ]
    run derive_compose_files codeploy
    [ "$output" = "deploy/compose.yaml deploy/compose.mattermost.yaml deploy/compose.ward.yaml" ]
}

@test "both extra axes can apply at once, podman last" {
    COMPOSE_ROOTLESS=1
    COMPOSE_VAULT=1
    run derive_compose_files external
    [ "$output" = "deploy/compose.yaml deploy/compose.external-mm.yaml deploy/compose.ward.yaml deploy/compose.podman.yaml deploy/compose.podman-ward.yaml" ]
}

@test "no secret store means no vault overlay" {
    COMPOSE_ROOTLESS=0
    COMPOSE_VAULT=0
    run derive_compose_files codeploy
    [ "$output" = "deploy/compose.yaml deploy/compose.mattermost.yaml" ]
}

@test "the browser is its own axis too" {
    COMPOSE_ROOTLESS=0
    COMPOSE_VAULT=0
    COMPOSE_BROWSER=1
    run derive_compose_files codeploy
    [ "$output" = "deploy/compose.yaml deploy/compose.mattermost.yaml deploy/compose.browser.yaml" ]
}

@test "the browser and the secret store are independent of each other" {
    COMPOSE_ROOTLESS=0
    COMPOSE_VAULT=1
    COMPOSE_BROWSER=1
    run derive_compose_files codeploy
    [ "$output" = "deploy/compose.yaml deploy/compose.mattermost.yaml deploy/compose.ward.yaml deploy/compose.browser.yaml" ]
}

@test "every axis at once still puts podman last" {
    COMPOSE_ROOTLESS=1
    COMPOSE_VAULT=1
    COMPOSE_BROWSER=1
    run derive_compose_files external
    [ "$output" = "deploy/compose.yaml deploy/compose.external-mm.yaml deploy/compose.ward.yaml deploy/compose.browser.yaml deploy/compose.podman.yaml deploy/compose.podman-ward.yaml" ]
}

@test "no browser means no browser overlay" {
    COMPOSE_ROOTLESS=0
    COMPOSE_BROWSER=0
    run derive_compose_files codeploy
    [ "$output" = "deploy/compose.yaml deploy/compose.mattermost.yaml" ]
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
    COMPOSE_VAULT=1
    for mode in codeploy external slack; do
        for f in $(derive_compose_files "$mode"); do
            [ -f "$BATS_TEST_DIRNAME/../../$f" ]
        done
    done
}

# --- compose_files: engine files + the deployment's own drop-ins ---------------

setup_home() {
    IMPI_HOME="$BATS_TEST_TMPDIR/home"
    mkdir -p "$IMPI_HOME/repo/deploy" "$IMPI_HOME/$COMPOSE_DROPIN_DIR"
    COMPOSE_ROOTLESS=0
    COMPOSE_VAULT=0
    unset IMPI_COMPOSE_ROOTLESS
    unset IMPI_VAULT
}

@test "compose_files reads the secret store back out of compose.env" {
    setup_home
    IMPI_VAULT=1
    run compose_files slack
    [ "${lines[1]}" = "$IMPI_HOME/repo/deploy/compose.ward.yaml" ]
}

@test "compose_files returns the engine's files as absolute paths" {
    setup_home
    run compose_files codeploy
    [ "${lines[0]}" = "$IMPI_HOME/repo/deploy/compose.yaml" ]
    [ "${lines[1]}" = "$IMPI_HOME/repo/deploy/compose.mattermost.yaml" ]
    [ "${#lines[@]}" -eq 2 ]
}

@test "drop-ins are merged after the engine's files, in alphabetical order" {
    setup_home
    : >"$IMPI_HOME/$COMPOSE_DROPIN_DIR/zebra.yaml"
    : >"$IMPI_HOME/$COMPOSE_DROPIN_DIR/cloudflared.yaml"
    : >"$IMPI_HOME/$COMPOSE_DROPIN_DIR/notes.txt"   # not a compose file
    run compose_files slack
    [ "${lines[0]}" = "$IMPI_HOME/repo/deploy/compose.yaml" ]
    [ "${lines[1]}" = "$IMPI_HOME/$COMPOSE_DROPIN_DIR/cloudflared.yaml" ]
    [ "${lines[2]}" = "$IMPI_HOME/$COMPOSE_DROPIN_DIR/zebra.yaml" ]
    [ "${#lines[@]}" -eq 3 ]
}

@test "an empty or missing drop-in directory is fine" {
    setup_home
    run compose_files slack
    [ "$status" -eq 0 ]
    [ "${#lines[@]}" -eq 1 ]

    rmdir "$IMPI_HOME/$COMPOSE_DROPIN_DIR"
    run compose_files slack
    [ "$status" -eq 0 ]
    [ "${#lines[@]}" -eq 1 ]
}

@test "rootless recorded in compose.env adds the podman overlay" {
    setup_home
    IMPI_COMPOSE_ROOTLESS=1
    run compose_files slack
    [ "${lines[1]}" = "$IMPI_HOME/repo/deploy/compose.podman.yaml" ]
}

# --- reading the mode back out of a legacy list --------------------------------

@test "infer_mode_from_files recognizes each deployment shape" {
    run infer_mode_from_files "deploy/compose.yaml deploy/compose.mattermost.yaml"
    [ "$output" = codeploy ]
    run infer_mode_from_files "deploy/compose.yaml deploy/compose.external-mm.yaml"
    [ "$output" = external ]
    run infer_mode_from_files "deploy/compose.yaml"
    [ "$output" = slack ]
    # A hand-added file must not change what the mode is read as.
    run infer_mode_from_files "deploy/compose.yaml deploy/compose.mattermost.yaml ../compose.cloudflared.yaml"
    [ "$output" = codeploy ]
}

# --- build_services: what an update rebuilds -----------------------------------

@test "without a secret store, only the engine is built" {
    COMPOSE_VAULT=0
    unset IMPI_VAULT
    run build_services
    [ "$output" = "impi" ]
}

@test "with a secret store, the broker is built too — an update must not leave the two on different releases" {
    COMPOSE_VAULT=0
    IMPI_VAULT=1
    run build_services
    [ "$output" = "impi ward" ]
}

@test "the broker's rootless mapping is merged only when both axes are on" {
    COMPOSE_ROOTLESS=1
    COMPOSE_VAULT=1
    run derive_compose_files slack
    [ "$output" = "deploy/compose.yaml deploy/compose.ward.yaml deploy/compose.podman.yaml deploy/compose.podman-ward.yaml" ]
}

@test "rootless without a secret store never names the broker" {
    COMPOSE_ROOTLESS=1
    COMPOSE_VAULT=0
    run derive_compose_files slack
    # A service with a userns_mode and no image is not a service.
    [ "$output" = "deploy/compose.yaml deploy/compose.podman.yaml" ]
}

# --- per-agent containers: the one overlay that is generated -------------------

@test "the generated per-agent overlay is merged after the engine's files and before the drop-ins" {
    setup_home
    mkdir -p "$IMPI_HOME/conf"
    : >"$IMPI_HOME/conf/agents.compose.yaml"
    : >"$IMPI_HOME/$COMPOSE_DROPIN_DIR/zebra.yaml"
    IMPI_AGENT_CONTAINERS=1
    run compose_files slack
    [ "${lines[0]}" = "$IMPI_HOME/repo/deploy/compose.yaml" ]
    [ "${lines[1]}" = "$IMPI_HOME/conf/agents.compose.yaml" ]
    [ "${lines[2]}" = "$IMPI_HOME/$COMPOSE_DROPIN_DIR/zebra.yaml" ]
    [ "${#lines[@]}" -eq 3 ]
}

@test "with the axis off the generated overlay is ignored even when it exists" {
    setup_home
    mkdir -p "$IMPI_HOME/conf"
    : >"$IMPI_HOME/conf/agents.compose.yaml"
    IMPI_AGENT_CONTAINERS=0
    run compose_files slack
    [ "${#lines[@]}" -eq 1 ]
}

@test "the axis on but nothing rendered yet is not an error" {
    setup_home
    IMPI_AGENT_CONTAINERS=1
    run compose_files slack
    [ "$status" -eq 0 ]
    [ "${#lines[@]}" -eq 1 ]
}

@test "an update rebuilds every agent that has an image of its own" {
    IMPI_HOME="$BATS_TEST_TMPDIR/home"
    mkdir -p "$IMPI_HOME/conf/agents/assistant" "$IMPI_HOME/conf/agents/researcher"
    : >"$IMPI_HOME/conf/agents/assistant/Dockerfile"
    : >"$IMPI_HOME/conf/agents/researcher/Dockerfile"
    mkdir -p "$IMPI_HOME/conf/agents/half-written"   # no Dockerfile yet
    COMPOSE_VAULT=0
    unset IMPI_VAULT
    IMPI_AGENT_CONTAINERS=1
    run build_services
    [ "$output" = "impi agent-assistant agent-researcher" ]
}

# --- migrate_volume: the copy has to answer honestly ---------------------------
#
# The version this replaces swallowed stderr, forced the exit code to zero and
# printed "copied" regardless, so four agents were reported migrated while every
# destination volume stayed empty. These stub `compose` and assert the three
# outcomes it has to tell apart.

setup_migrate() {
    ok()  { printf 'OK %s\n' "$*"; }
    bad() { printf 'BAD %s\n' "$*" >&2; }
}

@test "a copy that fails is reported as a failure, not as success" {
    setup_migrate
    compose() { echo "cp: can't create '/app/migrate/x': Permission denied" >&2; return 1; }
    run migrate_volume assistant vol /src "session files"
    [ "$status" -ne 0 ]
    [[ "$output" != *"OK"* ]]
    [[ "$output" == *"did not copy"* ]]
    [[ "$output" == *"originals are still there"* ]]
}

@test "a copy that exits clean but moves nothing is still a failure" {
    setup_migrate
    # The exact shape of the bug on docker: cp says nothing, the volume is empty.
    compose() { echo "MIGRATED=0"; return 0; }
    run migrate_volume assistant vol /src "session files"
    [ "$status" -ne 0 ]
    [[ "$output" == *"copied nothing"* ]]
}

@test "a copy that lands says how much landed" {
    setup_migrate
    compose() { echo "MIGRATED=106"; return 0; }
    run migrate_volume companion vol /src "session files"
    [ "$status" -eq 0 ]
    [[ "$output" == *"OK companion: session files copied (106 item(s))"* ]]
}

@test "noise around the count does not confuse the verdict" {
    setup_migrate
    # podman-compose prints its own lines around the container's output.
    compose() { echo ">>>> Executing external compose provider"; echo "MIGRATED=2"; return 0; }
    run migrate_volume profiler vol /src attachments
    [ "$status" -eq 0 ]
    [[ "$output" == *"(2 item(s))"* ]]
}
