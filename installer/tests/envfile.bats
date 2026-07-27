#!/usr/bin/env bats
# Unit tests for installer/lib/envfile.sh (run: make installer-test).

setup() {
    load_lib() { . "$BATS_TEST_DIRNAME/../lib/$1"; }
    load_lib envfile.sh
    WORK="$BATS_TEST_TMPDIR/work"
    mkdir -p "$WORK"
}

@test "env_set creates the file with mode 600" {
    env_set KEY value "$WORK/.env"
    [ "$(cat "$WORK/.env")" = "KEY=value" ]
    perms=$(stat -c '%a' "$WORK/.env" 2>/dev/null || stat -f '%Lp' "$WORK/.env")
    [ "$perms" = 600 ]
}

@test "env_set updates in place and preserves other lines" {
    printf 'A=1\nB=2\n# comment\n' > "$WORK/.env"
    env_set B new "$WORK/.env"
    run cat "$WORK/.env"
    [ "${lines[0]}" = "A=1" ]
    [ "${lines[1]}" = "B=new" ]
    [ "${lines[2]}" = "# comment" ]
}

@test "env_set appends a missing key" {
    printf 'A=1\n' > "$WORK/.env"
    env_set B 2 "$WORK/.env"
    grep -q '^B=2$' "$WORK/.env"
    grep -q '^A=1$' "$WORK/.env"
}

@test "env_set does not match keys by prefix" {
    printf 'GATEWAY_EXTRA=x\n' > "$WORK/.env"
    env_set GATEWAY mattermost "$WORK/.env"
    grep -q '^GATEWAY_EXTRA=x$' "$WORK/.env"
    grep -q '^GATEWAY=mattermost$' "$WORK/.env"
}

@test "env_set keeps the file inode (mount-safe truncate, not rename)" {
    printf 'A=1\n' > "$WORK/.env"
    before=$(ls -i "$WORK/.env" | awk '{print $1}')
    env_set A 2 "$WORK/.env"
    after=$(ls -i "$WORK/.env" | awk '{print $1}')
    [ "$before" = "$after" ]
}

@test "env_set quotes values with spaces so the file stays bash-sourceable" {
    env_set IMPI_COMPOSE_FILES "deploy/compose.yaml deploy/compose.mattermost.yaml" "$WORK/.env"
    env_set PLAIN token-123 "$WORK/.env"
    grep -q '^IMPI_COMPOSE_FILES="deploy/compose.yaml deploy/compose.mattermost.yaml"$' "$WORK/.env"
    grep -q '^PLAIN=token-123$' "$WORK/.env"
    ( . "$WORK/.env"
      [ "$IMPI_COMPOSE_FILES" = "deploy/compose.yaml deploy/compose.mattermost.yaml" ]
      [ "$PLAIN" = token-123 ] )
}

@test "env_set escapes shell-special characters in quoted values" {
    env_set PW 'a $b `c` "d" \e f' "$WORK/.env"
    ( . "$WORK/.env"
      [ "$PW" = 'a $b `c` "d" \e f' ] )
}

@test "env_get returns the value and strips quotes" {
    printf "A=plain\nB='quoted'\nC=\"double\"\n" > "$WORK/.env"
    [ "$(env_get A "$WORK/.env")" = plain ]
    [ "$(env_get B "$WORK/.env")" = quoted ]
    [ "$(env_get C "$WORK/.env")" = double ]
    [ -z "$(env_get MISSING "$WORK/.env")" ]
    [ -z "$(env_get A "$WORK/nonexistent")" ]
}
