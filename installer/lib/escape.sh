# `impi escape`: an agent's own pi on this host, for a person. Sourced after
# tui.sh (die/ok/bad/confirm) and compose.sh (compose, COMPOSE_DROPIN_DIR).
#
# The engine describes how it would start the agent (`impi agent argv`, run
# in a throwaway container, with this host's paths for the container's); this
# side makes sure pi is here, gives the run a place for its session, starts it,
# and cleans up after it — the session it wrote holds the conversation, and a
# conversation held on the host had the host's command output in it.

# Where escape sessions live, under IMPI_HOME. Ephemeral unless --keep.
ESCAPE_DIR="escape"
ESCAPE_KEEP_MARK=".keep"
# An escape that never came back (a crash, a lost terminal) leaves its
# directory; anything this old with no keep mark is dropped at the next start.
ESCAPE_STALE_DAYS=7
PI_PACKAGE="@earendil-works/pi-coding-agent"
PI_INSTALL_URL="https://pi.dev/install.sh"

# Filled by escape_plan.
ESCAPE_CWD=""
ESCAPE_ENV=()
ESCAPE_ARGS=()
ESCAPE_TMP=""

# pi_pin -> the pi version the engine's image pins. One source: the Dockerfile.
pi_pin() {
    sed -n 's/^ARG PI_VERSION=//p' "$REPO/deploy/Dockerfile" | head -1
}

# ensure_pi -> 0 with a usable pi on PATH. Offers the install when there is
# none; a version other than the engine's is noted, not refused — most days it
# does not matter, and the note says what to do on the day it does.
ensure_pi() {
    local _pin _have
    _pin=$(pi_pin)
    if ! command -v pi >/dev/null 2>&1; then
        bad "pi is not installed on this host"
        printf 'The engine runs pi %s in its container; a session in this terminal needs it here too.\n' "${_pin:-?}"
        if confirm "Install it now (curl -fsSL $PI_INSTALL_URL | sh)?" y; then
            curl -fsSL "$PI_INSTALL_URL" | sh || die "the pi installer failed"
            hash -r
            command -v pi >/dev/null 2>&1 \
                || die "pi is installed but not on PATH in this shell yet — open a new one and run this again"
        else
            die "install it yourself: npm install -g --ignore-scripts $PI_PACKAGE@${_pin:-latest}"
        fi
    fi
    _have=$(pi --version 2>/dev/null | head -1)
    if [ -n "$_pin" ] && [ "$_have" != "$_pin" ]; then
        printf 'note: pi here is %s, the engine pins %s. If the agent behaves differently, match it:\n  npm install -g --ignore-scripts %s@%s\n' \
            "${_have:-?}" "$_pin" "$PI_PACKAGE" "$_pin" >&2
    fi
    return 0
}

# escape_plan AGENT [FLAGS...] — asks the engine how it would start the agent
# and fills ESCAPE_CWD / ESCAPE_ENV / ESCAPE_ARGS. FLAGS go to `impi agent argv`
# (--tools, --append). NUL records through a file: a command substitution
# cannot hold a NUL, and the system-prompt note has newlines in it. --no-deps:
# the answer needs no chat server, and a compose that starts dependencies may
# print their ids to stdout — ahead of the records. Anything before the first
# `cwd=` is treated as such noise.
escape_plan() {
    local _agent=$1 _rec
    shift
    ESCAPE_CWD=""; ESCAPE_ENV=(); ESCAPE_ARGS=()
    ESCAPE_TMP=$(mktemp "${TMPDIR:-/tmp}/impi-escape.XXXXXX") || die "cannot create a temp file"
    compose run --rm -T --no-deps impi impi agent argv "$_agent" --format nul \
        --map "/app/agents=$IMPI_AGENTS_DIR" \
        --map "/app/skills=${IMPI_SKILLS_DIR:-$IMPI_HOME/skills}" \
        --map "/app=$REPO" "$@" >"$ESCAPE_TMP" || {
        rm -f "$ESCAPE_TMP"
        die "the engine could not describe $_agent — is its image built and the stack up? (impi start)"
    }
    while IFS= read -r -d '' _rec; do
        case "$_rec" in
            *$'\n'cwd=*) _rec=cwd=${_rec##*$'\n'cwd=} ;;  # noise a compose printed first
        esac
        case "$_rec" in
            cwd=*) ESCAPE_CWD=${_rec#cwd=} ;;
            env=*) ESCAPE_ENV+=("${_rec#env=}") ;;
            arg=*) ESCAPE_ARGS+=("${_rec#arg=}") ;;
        esac
    done <"$ESCAPE_TMP"
    [ -n "$ESCAPE_CWD" ] || die "the engine answered nothing for $_agent"
}

# escape_scrub -> `-u NAME` for every variable of THIS shell that is a
# credential for the deployment's own services. The engine grants those to a
# process it runs; on the host the agent is the operator, and the one thing it
# must not be handed is an identity to present from outside a container.
escape_scrub() {
    env | sed -n 's/^\(SECRET_BROKER_[A-Z_]*\|AGENTS_MM_TOKEN__[A-Z0-9_]*\|AGENTS_SLACK_[A-Z0-9_]*\|WARD_[A-Z_]*\|TOOL_TOKEN\|TOOL_URL\|MATTERMOST_TOKEN\|SLACK_[A-Z_]*TOKEN\)=.*/-u \1/p'
}

# escape_sweep — drop abandoned session directories: older than
# ESCAPE_STALE_DAYS and not marked to keep.
escape_sweep() {
    local _d
    for _d in "$IMPI_HOME/$ESCAPE_DIR"/*/; do
        [ -d "$_d" ] || continue
        [ -e "$_d$ESCAPE_KEEP_MARK" ] && continue
        [ -n "$(find "$_d" -maxdepth 0 -mtime +"$ESCAPE_STALE_DAYS" 2>/dev/null)" ] && rm -rf "$_d"
    done
    return 0
}

# escape_reminder MARKER — after the session: what to apply, if the agent
# changed things the engine only reads at its own pace.
escape_reminder() {
    if _changed_since "$1" "$IMPI_AGENTS_DIR"; then
        printf 'profiles changed during this session — apply with: impi reload\n'
    fi
    if _changed_since "$1" "$IMPI_HOME/$COMPOSE_DROPIN_DIR" "$IMPI_HOME/compose.env"; then
        printf 'the deployment changed during this session — apply with: impi start\n'
    fi
    return 0
}

# _changed_since MARKER PATH... -> 0 if anything under the paths is newer than
# the marker. Counted rather than `-quit`: not every find has that primary.
_changed_since() {
    local _marker=$1 _n
    shift
    _n=$(find "$@" -newer "$_marker" 2>/dev/null | grep -c .) || true
    [ "${_n:-0}" -gt 0 ]
}

_escape_cleanup() {
    rm -f "$ESCAPE_TMP"
    [ "${_ESCAPE_DROP:-}" = 1 ] && rm -rf "$_ESCAPE_SESSION"
    return 0
}

# cmd_escape AGENT [--tools CSV] [--append TEXT] [--keep] [--session-dir DIR]
#            [--dry-run] [-- PROMPT...]
cmd_escape() {
    local _agent="" _keep=0 _dry=0 _session="" _prompt="" _status _marker
    local _flags=()
    while [ $# -gt 0 ]; do
        case "$1" in
            --keep) _keep=1 ;;
            --dry-run) _dry=1 ;;
            --session-dir) _session=${2:-}; shift ;;
            --session-dir=*) _session=${1#*=} ;;
            --tools) _flags+=(--tools "${2:-}"); shift ;;
            --tools=*) _flags+=(--tools "${1#*=}") ;;
            --append) _flags+=(--append "${2:-}"); shift ;;
            --) shift; _prompt="$*"; break ;;
            -*) die "unknown option: $1" ;;
            *) [ -z "$_agent" ] || die "one agent at a time"; _agent=$1 ;;
        esac
        shift
    done
    [ -n "$_agent" ] || die 'usage: impi escape <agent> [--tools csv] [--keep] [--session-dir DIR] [--dry-run] [-- "prompt"]'
    if [ "$_dry" = 1 ]; then
        # What would run, for a person: the engine's own rendering, keys masked.
        compose run --rm -T --no-deps impi impi agent argv "$_agent" --format text \
            --map "/app/agents=$IMPI_AGENTS_DIR" \
            --map "/app/skills=${IMPI_SKILLS_DIR:-$IMPI_HOME/skills}" \
            --map "/app=$REPO" ${_flags[@]+"${_flags[@]}"}
        return
    fi
    ensure_pi
    escape_sweep
    if [ -n "$_session" ]; then
        # The operator's own directory: theirs to keep, whatever --keep says.
        mkdir -p "$_session" || die "cannot create $_session"
        _ESCAPE_SESSION=$_session
        _ESCAPE_DROP=0
    else
        mkdir -p "$IMPI_HOME/$ESCAPE_DIR" && chmod 700 "$IMPI_HOME/$ESCAPE_DIR"
        _ESCAPE_SESSION=$(mktemp -d "$IMPI_HOME/$ESCAPE_DIR/$_agent.XXXXXX") || die "cannot create a session directory"
        _ESCAPE_DROP=$([ "$_keep" = 1 ] && echo 0 || echo 1)
        [ "$_keep" = 1 ] && touch "$_ESCAPE_SESSION/$ESCAPE_KEEP_MARK"
    fi
    # Cleanup runs however this ends: pi exits, pi dies, the terminal goes away
    # (HUP) or somebody kills the wrapper (TERM). Ctrl-C is pi's to handle —
    # the shell waits for it and then follows whatever it did.
    trap _escape_cleanup EXIT
    trap 'exit 143' TERM HUP
    escape_plan "$_agent" ${_flags[@]+"${_flags[@]}"}
    _marker=$ESCAPE_TMP  # written just now: anything newer changed during the session
    set +e
    (
        cd "$ESCAPE_CWD" || exit 1
        # shellcheck disable=SC2046  # escape_scrub prints `-u NAME` pairs on purpose
        env $(escape_scrub) ${ESCAPE_ENV[@]+"${ESCAPE_ENV[@]}"} \
            pi ${ESCAPE_ARGS[@]+"${ESCAPE_ARGS[@]}"} --session-dir "$_ESCAPE_SESSION" \
            ${_prompt:+-p "$_prompt"}
    )
    _status=$?
    set -e
    escape_reminder "$_marker"
    if [ "$_ESCAPE_DROP" = 0 ] && [ -z "$_session" ]; then
        printf 'session kept: %s\n  continue it with: impi escape %s --session-dir %s\n' \
            "$_ESCAPE_SESSION" "$_agent" "$_ESCAPE_SESSION"
    fi
    return "$_status"
}
