#!/usr/bin/env bats
# `impi escape` (installer/lib/escape.sh): pi on the host with an agent's
# profile, the plan handed over by the engine, and the cleanup afterwards.

setup() {
    . "$BATS_TEST_DIRNAME/../lib/tui.sh"      # die/ok/bad
    . "$BATS_TEST_DIRNAME/../lib/compose.sh"  # compose(), COMPOSE_DROPIN_DIR
    . "$BATS_TEST_DIRNAME/../lib/escape.sh"
    set -euo pipefail                          # what the wrapper runs with
    IMPI_HOME="$BATS_TEST_TMPDIR/home"
    REPO="$IMPI_HOME/repo"
    IMPI_AGENTS_DIR="$IMPI_HOME/agents"
    export IMPI_HOME IMPI_AGENTS_DIR   # the pi stubs below touch files under them
    mkdir -p "$REPO/deploy" "$IMPI_AGENTS_DIR/agents/assistant" "$IMPI_HOME/$COMPOSE_DROPIN_DIR"
    printf 'ARG PI_VERSION=0.80.3\n' > "$REPO/deploy/Dockerfile"
    STUBS="$BATS_TEST_TMPDIR/stubs"
    mkdir -p "$STUBS"
    # Only the stubs and the system: a pi installed on the developer's own
    # machine must not answer for the one under test.
    PATH="$STUBS:/usr/bin:/bin"
    IMPI_COMPOSE_CMD="docker compose"
    CONFIRM_RC=0
    confirm() { return "$CONFIRM_RC"; }
    # The engine's answer: what `impi agent argv --format nul` would print,
    # with a system-prompt note that spans lines.
    compose() {
        if [ "${ESCAPE_STUB_FAIL:-0}" = 1 ]; then return 1; fi
        case "$*" in
            *"--format text"*) printf 'cd %s\npi --tools read\n' "$IMPI_AGENTS_DIR/agents/assistant" ;;
            *) printf 'cwd=%s\0env=LLM_MODEL=m\0arg=--approve\0arg=--tools\0arg=read,bash\0arg=--append-system-prompt\0arg=line one\nline two\0' \
                   "$IMPI_AGENTS_DIR/agents/assistant" ;;
        esac
    }
}

# A pi that writes down how it was started, then does what the test asks.
_stub_pi() { # VERSION [BODY]
    cat > "$STUBS/pi" <<STUB
#!/bin/sh
[ "\$1" = --version ] && { echo "$1"; exit 0; }
pwd > "$BATS_TEST_TMPDIR/pi.cwd"
printf '%s\n' "\$@" > "$BATS_TEST_TMPDIR/pi.args"
env | sort > "$BATS_TEST_TMPDIR/pi.env"
${2:-exit 0}
STUB
    chmod +x "$STUBS/pi"
}

# --- pi itself -----------------------------------------------------------------

@test "the pin is read from the engine's Dockerfile" {
    [ "$(pi_pin)" = "0.80.3" ]
}

@test "without pi, declining the install ends with the command to run by hand" {
    CONFIRM_RC=1
    run ensure_pi
    [ "$status" -ne 0 ]
    [[ "$output" == *"pi is not installed"* ]]
    [[ "$output" == *"npm install -g --ignore-scripts @earendil-works/pi-coding-agent@0.80.3"* ]]
}

@test "a pi of another version is noted with the command that pins it, and not refused" {
    _stub_pi 0.79.0
    run ensure_pi
    [ "$status" -eq 0 ]
    [[ "$output" == *"pi here is 0.79.0, the engine pins 0.80.3"* ]]
    [[ "$output" == *"pi-coding-agent@0.80.3"* ]]
}

@test "the pinned version passes in silence" {
    _stub_pi 0.80.3
    run ensure_pi
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

# --- the plan ------------------------------------------------------------------

@test "the engine's NUL records become cwd, env and arguments, newlines intact" {
    escape_plan assistant
    [ "$ESCAPE_CWD" = "$IMPI_AGENTS_DIR/agents/assistant" ]
    [ "${ESCAPE_ENV[0]}" = "LLM_MODEL=m" ]
    [ "${#ESCAPE_ARGS[@]}" -eq 5 ]
    [ "${ESCAPE_ARGS[4]}" = $'line one\nline two' ]
    rm -f "$ESCAPE_TMP"
}

@test "ids a compose prints while starting things do not pass for the plan" {
    compose() { printf '1f2e3d\nimpi-e2e_db_1\ncwd=%s\0arg=--approve\0' "$IMPI_AGENTS_DIR/agents/assistant"; }
    escape_plan assistant
    [ "$ESCAPE_CWD" = "$IMPI_AGENTS_DIR/agents/assistant" ]
    rm -f "$ESCAPE_TMP"
}

@test "the engine is asked without its dependencies being started" {
    compose() { printf '%s\n' "$*" > "$BATS_TEST_TMPDIR/compose.args"; printf 'cwd=/x\0'; }
    escape_plan assistant
    grep -q -- 'run --rm -T --no-deps impi impi agent argv assistant' "$BATS_TEST_TMPDIR/compose.args"
    rm -f "$ESCAPE_TMP"
}

@test "an engine that cannot answer stops the escape with the reason" {
    ESCAPE_STUB_FAIL=1
    run escape_plan assistant
    [ "$status" -ne 0 ]
    [[ "$output" == *"could not describe assistant"* ]]
}

# --- the run -------------------------------------------------------------------

@test "pi starts in the profile with the plan's flags, the prompt and its own session dir" {
    _stub_pi 0.80.3
    run cmd_escape assistant -- "what host is this"
    [ "$status" -eq 0 ]
    [ "$(cat "$BATS_TEST_TMPDIR/pi.cwd")" = "$IMPI_AGENTS_DIR/agents/assistant" ]
    args=$(cat "$BATS_TEST_TMPDIR/pi.args")
    [[ "$args" == *$'--tools\nread,bash\n'* ]]
    [[ "$args" == *$'line one\nline two\n'* ]]
    [[ "$args" == *"--session-dir"$'\n'"$IMPI_HOME/escape/assistant."* ]]
    [[ "$args" == *$'-p\nwhat host is this'* ]]
    grep -q '^LLM_MODEL=m$' "$BATS_TEST_TMPDIR/pi.env"
}

@test "credentials of the deployment in the operator's shell never reach pi" {
    _stub_pi 0.80.3
    export SECRET_BROKER_URL=https://ward:8425 AGENTS_MM_TOKEN__ASSISTANT=t WARD_ROLE_ID=r MATTERMOST_TOKEN=x
    run cmd_escape assistant
    [ "$status" -eq 0 ]
    # `run` + $status, not `! grep`: a negated command never fails a bats test.
    run grep 'SECRET_BROKER_URL\|AGENTS_MM_TOKEN__ASSISTANT\|WARD_ROLE_ID\|MATTERMOST_TOKEN' "$BATS_TEST_TMPDIR/pi.env"
    [ "$status" -ne 0 ]
    grep -q '^PATH=' "$BATS_TEST_TMPDIR/pi.env"   # the rest of the environment is still there
}

@test "--dry-run shows the engine's rendering and starts nothing" {
    run cmd_escape assistant --dry-run
    [ "$status" -eq 0 ]
    [[ "$output" == *"pi --tools read"* ]]
    [ ! -e "$BATS_TEST_TMPDIR/pi.args" ]
    [ ! -d "$IMPI_HOME/escape" ]
}

@test "--tools and --append are handed to the engine" {
    _stub_pi 0.80.3
    compose() { printf '%s\n' "$*" > "$BATS_TEST_TMPDIR/compose.args"; printf 'cwd=%s\0arg=--approve\0' "$IMPI_AGENTS_DIR/agents/assistant"; }
    run cmd_escape assistant --tools read --append "Be brief."
    [ "$status" -eq 0 ]
    grep -q -- '--tools read --append Be brief.' "$BATS_TEST_TMPDIR/compose.args"
}

# --- cleaning up ---------------------------------------------------------------

_temp_files() { ( ls "${TMPDIR:-/tmp}"/impi-escape.* 2>/dev/null || true ) | wc -l; }

@test "the session directory is gone once pi has exited, and so is the plan's temp file" {
    _stub_pi 0.80.3
    before=$(_temp_files)
    run cmd_escape assistant
    [ "$status" -eq 0 ]
    [ -z "$(ls -A "$IMPI_HOME/escape")" ]
    [ "$(_temp_files)" -eq "$before" ]
}

@test "an engine that cannot answer leaves no temp file behind either" {
    before=$(_temp_files)
    ESCAPE_STUB_FAIL=1
    run escape_plan assistant
    [ "$status" -ne 0 ]
    [ "$(_temp_files)" -eq "$before" ]
}

@test "--keep leaves it, marked, and says how to come back to it" {
    _stub_pi 0.80.3
    run cmd_escape assistant --keep
    [ "$status" -eq 0 ]
    kept=$(ls -d "$IMPI_HOME"/escape/assistant.*)
    [ -e "$kept/.keep" ]
    [[ "$output" == *"session kept: $kept"* ]]
    [[ "$output" == *"impi escape assistant --session-dir $kept"* ]]
}

@test "a directory the operator chose is theirs and is not removed" {
    _stub_pi 0.80.3
    run cmd_escape assistant --session-dir "$BATS_TEST_TMPDIR/mine"
    [ "$status" -eq 0 ]
    [ -d "$BATS_TEST_TMPDIR/mine" ]
    grep -q -- "$BATS_TEST_TMPDIR/mine" "$BATS_TEST_TMPDIR/pi.args"
}

@test "a pi that is killed still gets cleaned up after" {
    _stub_pi 0.80.3 'echo $$ > "$BATS_TEST_TMPDIR/pi.pid"; sleep 30'
    cmd_escape assistant >/dev/null 2>&1 &
    wrapper=$!
    for _ in 1 2 3 4 5 6 7 8 9 10; do [ -s "$BATS_TEST_TMPDIR/pi.pid" ] && break; sleep 0.2; done
    kill -TERM "$(cat "$BATS_TEST_TMPDIR/pi.pid")"
    wait "$wrapper" || true
    [ -z "$(ls -A "$IMPI_HOME/escape")" ]
}

@test "abandoned sessions older than a week are swept, kept ones are not" {
    mkdir -p "$IMPI_HOME/escape/old.abc" "$IMPI_HOME/escape/kept.def" "$IMPI_HOME/escape/new.ghi"
    touch "$IMPI_HOME/escape/kept.def/.keep"
    touch -t 200001010000 "$IMPI_HOME/escape/old.abc" "$IMPI_HOME/escape/kept.def"
    escape_sweep
    [ ! -d "$IMPI_HOME/escape/old.abc" ]
    [ -d "$IMPI_HOME/escape/kept.def" ]
    [ -d "$IMPI_HOME/escape/new.ghi" ]
}

# --- what to apply afterwards --------------------------------------------------

@test "a profile changed during the session earns a reload reminder, an untouched one silence" {
    _stub_pi 0.80.3 'sleep 1; touch "$IMPI_AGENTS_DIR/agents/assistant/SYSTEM.md"'
    run cmd_escape assistant
    [[ "$output" == *"impi reload"* ]]
    [[ "$output" != *"impi start"* ]]
    _stub_pi 0.80.3
    run cmd_escape assistant
    [[ "$output" != *"impi reload"* ]]
}

@test "a drop-in written during the session earns an impi start reminder" {
    _stub_pi 0.80.3 'sleep 1; touch "$IMPI_HOME/compose.d/extra.yaml"'
    run cmd_escape assistant
    [[ "$output" == *"impi start"* ]]
}
