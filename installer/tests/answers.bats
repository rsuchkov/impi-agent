#!/usr/bin/env bats
# Unit tests for installer/lib/answers.sh.

setup() {
    . "$BATS_TEST_DIRNAME/../lib/tui.sh"      # die()
    . "$BATS_TEST_DIRNAME/../lib/answers.sh"
    WORK="$BATS_TEST_TMPDIR/work"
    mkdir -p "$WORK"
}

@test "load_answers exports whitelisted keys and turns on answers mode" {
    printf 'IMPI_GATEWAY=mattermost\n# comment\n\nIMPI_MM_MODE=codeploy\n' > "$WORK/a"
    load_answers "$WORK/a"
    [ "$IMPI_GATEWAY" = mattermost ]
    [ "$IMPI_MM_MODE" = codeploy ]
    [ "$IMPI_ASSUME_YES" = 1 ]
}

@test "load_answers rejects unknown keys" {
    printf 'IMPI_EVIL=1\n' > "$WORK/a"
    run load_answers "$WORK/a"
    [ "$status" -ne 0 ]
    [[ "$output" == *"unknown key IMPI_EVIL"* ]]
}

@test "load_answers rejects lines without =" {
    printf 'IMPI_GATEWAY\n' > "$WORK/a"
    run load_answers "$WORK/a"
    [ "$status" -ne 0 ]
    [[ "$output" == *"expected KEY=VALUE"* ]]
}

@test "load_answers dies on an unreadable file" {
    run load_answers "$WORK/missing"
    [ "$status" -ne 0 ]
}

@test "answers mode makes default-less prompts fail instead of hanging" {
    export IMPI_ASSUME_YES=1
    TUI_FANCY=0
    unset UNSET_PROMPT_VAR || true
    run ask UNSET_PROMPT_VAR "Label"
    [ "$status" -ne 0 ]
    [[ "$output" == *"UNSET_PROMPT_VAR"* ]]
}

@test "answers mode takes the default when one exists" {
    export IMPI_ASSUME_YES=1
    TUI_FANCY=0
    unset DEFAULTED_VAR || true
    ask DEFAULTED_VAR "Label" "fallback"
    [ "$DEFAULTED_VAR" = fallback ]
    unset DEFAULTED_CONFIRM || true
    confirm DEFAULTED_CONFIRM "Q?" y
    [ "$DEFAULTED_CONFIRM" = yes ]
}

@test "preset variables short-circuit every prompt kind" {
    export IMPI_ASSUME_YES=1
    TUI_FANCY=0
    PRESET_A=hello
    ask PRESET_A "Label"
    [ "$PRESET_A" = hello ]
    PRESET_B=yes
    confirm PRESET_B "Q?" n
    [ "$PRESET_B" = yes ]
    PRESET_C=1
    menu PRESET_C "T" "a" "b"
    [ "$PRESET_C" = 1 ]
    PRESET_D=""
    ask_opt PRESET_D "Label"
    [ -z "$PRESET_D" ]
}
