# Compose runtime detection + invocation. Sourced after tui.sh.
# Sets: COMPOSE_CMD (e.g. "docker compose"), COMPOSE_RUNTIME (docker|podman),
# COMPOSE_ROOTLESS (0|1). The compose() wrapper always passes the project name,
# the derived -f file list, and the compose.env for ${...} interpolation.

# Consumed by main.sh / preflight after detect_compose runs.
# shellcheck disable=SC2034
COMPOSE_CMD=""
COMPOSE_RUNTIME=""
COMPOSE_ROOTLESS=0
# Whether this deployment runs a Vault for the secret broker. An axis of its own,
# independent of the chat platform.
COMPOSE_VAULT=0
# Whether this deployment runs a browser for the agents. Another such axis.
COMPOSE_BROWSER=0
# Whether the inventory lives on MongoDB rather than in a SQLite file. Another
# axis again — it changes where the engine keeps state, not what it talks to.
COMPOSE_MONGO=0
# Whether each agent gets a container of its own. Another axis again, and the
# only one whose overlay is GENERATED rather than shipped: it names one service
# per agent, so it is written by `impi agent sync` into IMPI_HOME, not the repo.
COMPOSE_AGENT_CONTAINERS=0

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
#
# MODE is the chat-platform axis and stays positional. Anything orthogonal to it
# — rootless, the secret store — reads its own variable instead, the way
# COMPOSE_ROOTLESS does: a second positional would have to be threaded through
# every caller for a choice that has nothing to do with the first.
derive_compose_files() {
    local files="deploy/compose.yaml"
    case "$1" in
        codeploy) files="$files deploy/compose.mattermost.yaml" ;;
        external) files="$files deploy/compose.external-mm.yaml" ;;
        slack) : ;;
        *) die "derive_compose_files: unknown mode $1" ;;
    esac
    [ "${COMPOSE_MONGO:-0}" = 1 ] && files="$files deploy/compose.mongo.yaml"
    [ "${COMPOSE_VAULT:-0}" = 1 ] && files="$files deploy/compose.ward.yaml"
    [ "${COMPOSE_BROWSER:-0}" = 1 ] && files="$files deploy/compose.browser.yaml"
    [ "$COMPOSE_ROOTLESS" = 1 ] && files="$files deploy/compose.podman.yaml"
    # The one file that belongs to both axes: it maps the broker's user, and the
    # broker exists only when the store does.
    [ "$COMPOSE_ROOTLESS" = 1 ] && [ "${COMPOSE_VAULT:-0}" = 1 ] \
        && files="$files deploy/compose.podman-ward.yaml"
    printf '%s\n' "$files"
}

# build_services -> the services this deployment builds from source, space
# separated. The engine always; the broker too when the secret store is on, or
# an update would leave it running the image of the release before it; and every
# agent that has a container of its own, for the same reason. Bare
# `compose build` would also build whatever a drop-in adds, which is not this
# script's decision to make.
build_services() {
    local _services="impi" _dir
    [ -n "${IMPI_VAULT:-}" ] && COMPOSE_VAULT=$IMPI_VAULT
    [ -n "${IMPI_BROWSER:-}" ] && COMPOSE_BROWSER=$IMPI_BROWSER
    [ -n "${IMPI_AGENT_CONTAINERS:-}" ] && COMPOSE_AGENT_CONTAINERS=$IMPI_AGENT_CONTAINERS
    [ "${COMPOSE_VAULT:-0}" = 1 ] && _services="$_services ward"
    [ "${COMPOSE_BROWSER:-0}" = 1 ] && _services="$_services browser"
    if [ "${COMPOSE_AGENT_CONTAINERS:-0}" = 1 ]; then
        for _dir in "$IMPI_HOME"/conf/agents/*/; do
            [ -f "$_dir/Dockerfile" ] || continue
            _services="$_services agent-$(basename "$_dir")"
        done
    fi
    printf '%s\n' "$_services"
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
    # Same shape: recorded in compose.env for an installed deployment, set by the
    # installer's own question during an install.
    [ -n "${IMPI_VAULT:-}" ] && COMPOSE_VAULT=$IMPI_VAULT
    [ -n "${IMPI_BROWSER:-}" ] && COMPOSE_BROWSER=$IMPI_BROWSER
    [ -n "${IMPI_MONGO:-}" ] && COMPOSE_MONGO=$IMPI_MONGO
    [ -n "${IMPI_AGENT_CONTAINERS:-}" ] && COMPOSE_AGENT_CONTAINERS=$IMPI_AGENT_CONTAINERS
    for _f in $(derive_compose_files "$1"); do
        printf '%s\n' "$IMPI_HOME/repo/$_f"
    done
    # The per-agent overlay: generated into IMPI_HOME rather than shipped, so it
    # cannot come from derive_compose_files with the rest. After the engine's
    # files, because it adds to the engine service; before the drop-ins, because
    # a deployment's own overrides are still the last word.
    if [ "${COMPOSE_AGENT_CONTAINERS:-0}" = 1 ] \
        && [ -f "$IMPI_HOME/conf/agents.compose.yaml" ]; then
        printf '%s\n' "$IMPI_HOME/conf/agents.compose.yaml"
    fi
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

# migrate_volume AGENT VOLUME SOURCE WHAT -> 0 when the copy landed.
#
# Fills a per-agent volume from the engine's data volume, and answers honestly.
# The first version swallowed stderr, forced the exit code to 0 and printed
# "copied" unconditionally, so four agents were reported migrated while every
# volume stayed empty — the one failure the command exists to prevent, announced
# as a success.
#
# The destination is mounted at /app/migrate, which the ENGINE IMAGE creates and
# owns. That is not cosmetic: a named volume takes its owner from the image
# directory it is first mounted on, and one mounted where the image made nothing
# belongs to root, which the engine's user cannot write. Mounting it at an
# invented path is what made the copy fail in the first place.
migrate_volume() {
    local _agent=$1 _volume=$2 _source=$3 _what=$4 _out _count
    _out=$(compose run --rm -T -v "$_volume:/app/migrate" impi sh -c \
        "cp -a '$_source/.' /app/migrate/ && printf 'MIGRATED=%s\n' \"\$(ls -A /app/migrate | wc -l)\"" 2>&1) || {
        printf '%s\n' "$_out" >&2
        bad "$_agent: $_what did not copy — nothing was removed, the originals are still there"
        return 1
    }
    _count=$(printf '%s\n' "$_out" | sed -n 's/^MIGRATED=//p' | tr -d '[:space:]')
    if [ "${_count:-0}" -gt 0 ] 2>/dev/null; then
        ok "$_agent: $_what copied ($_count item(s))"
        return 0
    fi
    printf '%s\n' "$_out" >&2
    bad "$_agent: $_what copied nothing — the volume is still empty"
    return 1
}

# engine_log_count MARKER -> how many lines carry it. The log is cumulative, so
# "has it ever said X" cannot answer "has it said X since the restart" — the
# count before and after can.
engine_log_count() {
    compose logs impi 2>/dev/null | grep -c -- "$1" || true
}

# container_runtime -> docker | podman: what is under the compose command.
# For the one build compose cannot order (see cmd_agent_sync in the wrapper),
# and for the questions compose itself cannot answer — see stopped_containers.
container_runtime() {
    case "${IMPI_COMPOSE_CMD:-}" in
        podman*) printf 'podman\n' ;;
        *) printf 'docker\n' ;;
    esac
}

# runtime_version -> "docker 28.3.2" / "podman 5.8.2"; empty when the runtime
# does not answer. Docker's is the DAEMON's version, not the client's: the
# daemon is what restores the stack after a reboot, and the two can differ.
runtime_version() {
    local _rt _v=""
    _rt=$(container_runtime)
    case "$_rt" in
        docker) _v=$(docker version --format '{{.Server.Version}}' 2>/dev/null || true) ;;
        podman) _v=$(podman version --format '{{.Client.Version}}' 2>/dev/null || true) ;;
    esac
    [ -n "$_v" ] && printf '%s %s\n' "$_rt" "$_v"
    return 0
}

# restore_is_ordered "RUNTIME VERSION" -> 0 when that runtime brings a container
# that lives in another's network namespace back AFTER its owner.
#
# The broker lives in the store's namespace, and a daemon that restores the two
# in no particular order fails the broker for good when it reaches it first:
# the failure is at creation, before any process a restart policy could watch,
# so nothing retries. Docker Engine 29.0 waits for the owner (moby #50326);
# older daemons do not. podman starts a container's dependencies itself. An
# unknown runtime is given the benefit of the doubt — the container probe says
# what actually happened.
restore_is_ordered() {
    local _major
    case "$1" in
        docker\ *)
            _major=${1#docker }
            _major=${_major%%.*}
            [ "$_major" -ge 29 ] 2>/dev/null
            ;;
        *) return 0 ;;
    esac
}

# stopped_containers -> one line per container of this project that exists but
# is not running: SERVICE|STATUS|ERROR.
#
# The runtime is asked directly rather than compose: `docker compose ps` hides
# stopped containers unless told `-a`, and podman-compose's `ps` shows them
# always but refuses `-a` — one flag, two opposite meanings. Both runtimes
# label every container they create with its compose service, and both answer
# the same `inspect` template, so the runtime is the one interface they share.
#
# A container nothing expects to be running is skipped: a one-off (`compose
# run`, which docker labels as such), and any container with no restart policy
# — the operator's ward-admin, which `compose up` starts once and which exits
# at once. Every service that matters carries `restart: unless-stopped`.
stopped_containers() {
    local _rt _id _line _svc _oneoff _policy _rest
    _rt=$(container_runtime)
    for _id in $("$_rt" ps -a --filter "label=com.docker.compose.project=${IMPI_PROJECT:-impi}" \
            --format '{{.ID}}' 2>/dev/null); do
        _line=$("$_rt" inspect --format \
            '{{index .Config.Labels "com.docker.compose.service"}}|{{index .Config.Labels "com.docker.compose.oneoff"}}|{{.HostConfig.RestartPolicy.Name}}|{{.State.Status}}|{{.State.Error}}' \
            "$_id" 2>/dev/null) || continue
        _svc=${_line%%|*}; _rest=${_line#*|}
        _oneoff=${_rest%%|*}; _rest=${_rest#*|}
        _policy=${_rest%%|*}; _rest=${_rest#*|}
        case "$_oneoff" in True|true) continue ;; esac
        case "$_policy" in ""|no) continue ;; esac
        case "$_rest" in running\|*) continue ;; esac
        printf '%s|%s\n' "$_svc" "$_rest"
    done
    return 0
}

# explain_stopped SERVICE STATUS ERROR — say why a container of this project is
# not running, in words that name the fix. Everything goes where `bad` goes.
#
# The one failure recognised by its text is the daemon's own — see
# restore_is_ordered. It reads as "some containers came back and some did not"
# and is diagnosed as a broken broker unless something names the order.
explain_stopped() {
    local _svc=$1 _status=$2 _error=$3
    case "$_error" in
        *"cannot join network"*)
            bad "$_svc: $_status — $_error"
            printf '  the daemon restored it before the container whose network it lives in.\n' >&2
            # shellcheck disable=SC2016  # the backticks are message text
            printf '  Docker older than 29.0 does not order that; `impi start` does. Upgrading\n' >&2
            printf '  Docker removes the step.\n' >&2
            ;;
        *)
            bad "$_svc: $_status — ${_error:-not running}; \`impi start\` brings it back"
            ;;
    esac
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
