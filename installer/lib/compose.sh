# Compose runtime detection + invocation. Sourced after tui.sh.
# Sets: COMPOSE_CMD (e.g. "docker compose"), COMPOSE_RUNTIME (docker|podman),
# COMPOSE_ROOTLESS (0|1). The compose() wrapper always passes the project name,
# the derived -f file list, and the compose.env for ${...} interpolation.

# Consumed by main.sh / preflight after detect_compose runs.
# shellcheck disable=SC2034
COMPOSE_CMD=""
COMPOSE_RUNTIME=""
COMPOSE_ROOTLESS=0

has_docker_compose() { docker compose version >/dev/null 2>&1; }
has_podman_compose() { podman compose version >/dev/null 2>&1; }

# detect_compose [docker|podman] — optional preference wins when available;
# otherwise docker is preferred: its daemon restores `restart: unless-stopped`
# containers on boot, while daemonless podman needs a manual `impi start`
# after a machine restart.
detect_compose() {
    local pref=${1:-}
    if [ "$pref" = podman ] && has_podman_compose; then
        COMPOSE_CMD="podman compose"
        COMPOSE_RUNTIME=podman
    elif [ "$pref" = docker ] && has_docker_compose; then
        COMPOSE_CMD="docker compose"
        COMPOSE_RUNTIME=docker
    elif has_docker_compose; then
        COMPOSE_CMD="docker compose"
        COMPOSE_RUNTIME=docker
    elif has_podman_compose; then
        COMPOSE_CMD="podman compose"
        COMPOSE_RUNTIME=podman
    elif docker-compose version >/dev/null 2>&1; then
        case "$(docker-compose version --short 2>/dev/null)" in
            1.*) die "docker-compose v1 is too old (no BuildKit) — install docker compose v2 or podman" ;;
        esac
        COMPOSE_CMD="docker-compose"
        COMPOSE_RUNTIME=docker
    else
        return 1
    fi
    if [ "$COMPOSE_RUNTIME" = podman ]; then
        [ "$(podman info --format '{{.Host.Security.Rootless}}' 2>/dev/null)" = true ] && COMPOSE_ROOTLESS=1
    fi
    return 0
}

# derive_compose_files MODE -> space-separated repo-relative file list.
# MODE: codeploy | external | slack. Re-run on update so overlays added in
# newer releases activate automatically.
derive_compose_files() {
    local files="deploy/compose.yaml"
    case "$1" in
        codeploy) files="$files deploy/compose.mattermost.yaml" ;;
        external) files="$files deploy/compose.external-mm.yaml" ;;
        slack) : ;;
        *) die "derive_compose_files: unknown mode $1" ;;
    esac
    [ "$COMPOSE_ROOTLESS" = 1 ] && files="$files deploy/compose.podman.yaml"
    printf '%s\n' "$files"
}

# compose ARGS... — run the configured compose against $IMPI_HOME's deployment.
# Reads IMPI_COMPOSE_CMD / IMPI_COMPOSE_FILES / IMPI_HOME from the environment
# (main.sh exports them; the wrapper sources compose.env).
compose() {
    local _f _args=""
    for _f in $IMPI_COMPOSE_FILES; do
        _args="$_args -f $IMPI_HOME/repo/$_f"
    done
    # shellcheck disable=SC2086  # word splitting is the point here
    $IMPI_COMPOSE_CMD --project-name "${IMPI_PROJECT:-impi}" $_args \
        --env-file "$IMPI_HOME/compose.env" "$@"
}
