# Mattermost bootstrap helpers (co-deploy path). Zero-touch: the first admin,
# the team, and team membership come from mmctl in LOCAL Mode inside the
# mattermost container (no web-UI signup). Tokens and bots CANNOT be created in
# local mode — the admin PAT is minted over REST by the container CLI
# (`impi mm bootstrap-token`), and bots by `impi agent add` with that PAT.

# mm_wait_ready URL [TRIES] — poll /api/v4/system/ping from the host.
mm_wait_ready() {
    local url=$1 tries=${2:-60} i=0
    while [ "$i" -lt "$tries" ]; do
        if curl -sf --max-time 3 "${url%/}/api/v4/system/ping" >/dev/null 2>&1; then
            return 0
        fi
        sleep 2
        i=$((i + 1))
    done
    return 1
}

_mmctl() {
    compose exec -T mattermost mmctl --local "$@"
}

# mm_bootstrap_admin USER EMAIL PASSWORD — create the first system admin.
# Preferred: mmctl local mode. Fallback: first-user signup over the API (the
# first account on an empty server becomes system admin).
mm_bootstrap_admin() {
    local user=$1 email=$2 password=$3
    if _mmctl user create --email "$email" --username "$user" \
        --password "$password" --system-admin >>"${TUI_LOG:-/dev/null}" 2>&1; then
        return 0
    fi
    dim "mmctl local mode unavailable — trying first-user signup"
    curl -sf --max-time 10 -X POST \
        -H 'Content-Type: application/json' \
        -d "{\"email\":\"$email\",\"username\":\"$user\",\"password\":\"$password\"}" \
        "http://localhost:${IMPI_MM_PORT:-8065}/api/v4/users" \
        >>"${TUI_LOG:-/dev/null}" 2>&1
}

# mm_bootstrap_team TEAM DISPLAY ADMIN_USER — create the team + add the admin.
mm_bootstrap_team() {
    local team=$1 display=$2 admin=$3
    _mmctl team create --name "$team" --display-name "$display" \
        >>"${TUI_LOG:-/dev/null}" 2>&1 || true  # may already exist on re-run
    _mmctl team users add "$team" "$admin" >>"${TUI_LOG:-/dev/null}" 2>&1
}

# mm_admin_token CONTAINER_URL ADMIN_USER PASSWORD — prints a fresh admin PAT.
# Runs inside the impi container so the single Python implementation is the
# only place that speaks the login/token API.
mm_admin_token() {
    local url=$1 user=$2 password=$3
    # tail -1: some compose implementations chat on stdout before the command
    # output; the CLI prints exactly one line (the token) last.
    printf '%s\n' "$password" | \
        compose run --rm -T impi impi mm bootstrap-token \
            --url "$url" --login-id "$user" --password-stdin | tail -n 1
}

# mm_container_url USER_URL RUNTIME — rewrite a host-centric URL so the impi
# CONTAINER can reach it: localhost/127.0.0.1 become the host gateway alias
# (the external-mm overlay adds both spellings via extra_hosts).
mm_container_url() {
    local url=$1 runtime=$2 host_alias
    case "$runtime" in
        podman) host_alias="host.containers.internal" ;;
        *) host_alias="host.docker.internal" ;;
    esac
    printf '%s\n' "$url" | sed -e "s|//localhost|//$host_alias|" -e "s|//127\.0\.0\.1|//$host_alias|"
}
