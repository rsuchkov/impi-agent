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

# Where a deployment keeps ITS OWN compose overlays. Anything *.yaml in here is
# merged after the engine's files (so it can override them) — and it is never
# derived, written or read from config, which is what makes it survive updates.
COMPOSE_DROPIN_DIR="compose.d"

# derive_compose_files MODE -> space-separated repo-relative file list of the
# ENGINE's own compose files. MODE: codeploy | external | slack. Derived on every
# call, never stored: a stored list would have to be rewritten whenever a release
# adds an overlay, taking anything a human added with it.
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

# infer_mode_from_files FILES -> codeploy | external | slack. Reads the mode back
# out of a legacy IMPI_COMPOSE_FILES list, for installations made before the mode
# itself was recorded.
infer_mode_from_files() {
    case " $1 " in
        *compose.mattermost.yaml*) printf 'codeploy\n' ;;
        *compose.external-mm.yaml*) printf 'external\n' ;;
        *) printf 'slack\n' ;;
    esac
}

# compose_files MODE -> absolute paths, in merge order: the engine's files, then
# the deployment's own drop-ins (sorted, so the order is predictable).
compose_files() {
    local _f _dropin
    # Whether this deployment needs the rootless overlay: recorded in compose.env
    # for an installed deployment, detected by detect_compose during install.
    [ -n "${IMPI_COMPOSE_ROOTLESS:-}" ] && COMPOSE_ROOTLESS=$IMPI_COMPOSE_ROOTLESS
    for _f in $(derive_compose_files "$1"); do
        printf '%s\n' "$IMPI_HOME/repo/$_f"
    done
    for _dropin in "$IMPI_HOME/$COMPOSE_DROPIN_DIR"/*.yaml; do
        [ -f "$_dropin" ] && printf '%s\n' "$_dropin"
    done
    return 0  # an empty compose.d leaves the glob unmatched; that is fine
}

# engine_logged MARKER -> 0 if the engine's log contains MARKER.
#
# NOT `grep -q`: it stops at the first match and closes the pipe, the compose
# process writing into it dies of SIGPIPE (255), and `set -o pipefail` makes THAT
# the pipeline's status — so the check would answer "no" exactly when the answer
# is yes, and "no" when it is no. `grep -c` drains the stream, so compose exits
# normally and the status is grep's own (0 found / 1 not found).
engine_logged() {
    compose logs impi 2>/dev/null | grep -c -- "$1" >/dev/null
}

# compose ARGS... — run the configured compose against $IMPI_HOME's deployment.
# Reads IMPI_COMPOSE_CMD / IMPI_MM_MODE / IMPI_HOME from the environment (main.sh
# exports them; the wrapper sources compose.env).
compose() {
    local _f _args=""
    for _f in $(compose_files "${IMPI_MM_MODE:-slack}"); do
        _args="$_args -f $_f"
    done
    # shellcheck disable=SC2086  # word splitting is the point here
    $IMPI_COMPOSE_CMD --project-name "${IMPI_PROJECT:-impi}" $_args \
        --env-file "$IMPI_HOME/compose.env" "$@"
}
