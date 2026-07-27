# Preflight checks for the impi installer. Sourced after tui.sh + compose.sh.

# port_free PORT -> 0 if nothing listens on 127.0.0.1:PORT
port_free() {
    ! (exec 9<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null
}

# rand_hex BYTES — password/secret material without relying on openssl.
rand_hex() {
    od -An -N"$1" -tx1 /dev/urandom | tr -d ' \n'
}

# preflight MODE — prints a check table; dies on any hard failure.
# MODE: codeploy | external | slack (decides which ports must be free).
preflight() {
    local mode=$1 fail=0

    title "Preflight"
    case "$(uname -s)" in
        Linux|Darwin) ok "OS: $(uname -s)" ;;
        *) bad "OS: $(uname -s) — only Linux and macOS are supported"; fail=1 ;;
    esac
    if command -v git >/dev/null 2>&1; then
        ok "git: $(git --version | head -n 1)"
    else
        bad "git is required"; fail=1
    fi
    if [ -n "$COMPOSE_CMD" ] || detect_compose; then
        ok "compose: $COMPOSE_CMD ($COMPOSE_RUNTIME$([ "$COMPOSE_ROOTLESS" = 1 ] && printf ', rootless'))"
    else
        bad "no compose runtime — install Docker (with compose v2) or podman"; fail=1
    fi
    if command -v curl >/dev/null 2>&1; then
        ok "curl: present"
    else
        bad "curl is required"; fail=1
    fi
    if [ "$mode" = codeploy ]; then
        if port_free "${IMPI_MM_PORT:-8065}"; then
            ok "port ${IMPI_MM_PORT:-8065} (Mattermost): free"
        else
            bad "port ${IMPI_MM_PORT:-8065} is busy — is a Mattermost already running? (choose 'existing server' or set IMPI_MM_PORT)"
            fail=1
        fi
    fi
    if [ "$mode" = external ]; then
        if port_free "${IMPI_INTEGRATIONS_PORT:-8423}"; then
            ok "port ${IMPI_INTEGRATIONS_PORT:-8423} (widget callbacks): free"
        else
            bad "port ${IMPI_INTEGRATIONS_PORT:-8423} is busy (set IMPI_INTEGRATIONS_PORT)"; fail=1
        fi
    fi
    [ "$fail" = 0 ] || die "preflight failed — fix the items above and re-run"
}

# lan_ip — best-effort host LAN address (external-MM widget callbacks).
lan_ip() {
    case "$(uname -s)" in
        Linux)
            ip route get 1.1.1.1 2>/dev/null | awk '{for (i=1;i<NF;i++) if ($i=="src") {print $(i+1); exit}}'
            ;;
        Darwin)
            local iface
            iface=$(route -n get default 2>/dev/null | awk '/interface:/{print $2}')
            [ -n "$iface" ] && ipconfig getifaddr "$iface" 2>/dev/null
            ;;
    esac
}
