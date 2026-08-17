#!/usr/bin/env bash
# impi installer — the versioned orchestrator. install.sh clones the repo and
# execs this script from the clone, so the questionnaire always matches the
# checked-out release. Interactive TUI by default; --answers FILE drives every
# prompt from variables (see installer/lib/answers.sh) for CI/e2e.
#
# bash 3.2 compatible (macOS). Only writes under $IMPI_HOME, ~/.local/bin,
# and the chosen agents directory.

set -euo pipefail

INSTALLER_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_DIR=$(cd "$INSTALLER_DIR/.." && pwd)

# shellcheck source=lib/tui.sh
. "$INSTALLER_DIR/lib/tui.sh"
# shellcheck source=lib/envfile.sh
. "$INSTALLER_DIR/lib/envfile.sh"
# shellcheck source=lib/files.sh
. "$INSTALLER_DIR/lib/files.sh"
# shellcheck source=lib/compose.sh
. "$INSTALLER_DIR/lib/compose.sh"
# shellcheck source=lib/checks.sh
. "$INSTALLER_DIR/lib/checks.sh"
# shellcheck source=lib/answers.sh
. "$INSTALLER_DIR/lib/answers.sh"
# shellcheck source=lib/mattermost.sh
. "$INSTALLER_DIR/lib/mattermost.sh"

usage() {
    cat <<'EOF'
Usage: main.sh [--home DIR] [--answers FILE]

  --home DIR      installation root (default ~/.impi, or $IMPI_HOME)
  --answers FILE  non-interactive: KEY=VALUE answers for every prompt
EOF
}

ANSWERS_FILE=""
while [ $# -gt 0 ]; do
    case "$1" in
        --home) IMPI_HOME=$2; shift 2 ;;
        --answers) ANSWERS_FILE=$2; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage; exit 2 ;;
    esac
done

[ -n "$ANSWERS_FILE" ] && load_answers "$ANSWERS_FILE"
IMPI_HOME=${IMPI_HOME:-$HOME/.impi}
mkdir -p "$IMPI_HOME"
TUI_LOG="$IMPI_HOME/install.log"
: >"$TUI_LOG"

tui_init || true
VERSION=$(cat "$REPO_DIR/VERSION" 2>/dev/null || echo dev)

bold "impi installer v$VERSION"
dim  "Everything is logged to $TUI_LOG"

# Both runtimes present -> let the user pick (docker recommended: its daemon
# brings the stack back up after a machine reboot; podman needs `impi start`).
if [ -z "${IMPI_RUNTIME:-}" ] && has_docker_compose && has_podman_compose; then
    CHOICE_RT=""
    menu CHOICE_RT "Both Docker and podman are installed — which one runs impi?" \
        "Docker  — auto-starts the stack after a machine reboot (recommended)" \
        "podman  — daemonless; run \`impi start\` yourself after a reboot"
    case "$CHOICE_RT" in
        0) IMPI_RUNTIME=docker ;;
        1) IMPI_RUNTIME=podman ;;
    esac
fi
detect_compose "${IMPI_RUNTIME:-}" || die "no compose runtime found — install Docker (compose v2) or podman"
if [ "$COMPOSE_RUNTIME" = podman ]; then
    dim "Note: podman has no daemon — after a machine reboot bring the stack"
    dim "back with \`impi start\` (install Docker instead if you want auto-start)."
fi

# --- questionnaire -----------------------------------------------------------

title "Chat platform"
if [ -z "${IMPI_GATEWAY:-}" ]; then
    CHOICE_GW=""
    menu CHOICE_GW "Where will your agents live?" \
        "Mattermost  — full experience: buttons, forms, auto bot creation" \
        "Slack       — bring your own Slack app tokens"
    case "$CHOICE_GW" in
        0) IMPI_GATEWAY=mattermost ;;
        1) IMPI_GATEWAY=slack ;;
    esac
fi

IMPI_MM_MODE=${IMPI_MM_MODE:-}
if [ "$IMPI_GATEWAY" = mattermost ] && [ -z "$IMPI_MM_MODE" ]; then
    CHOICE_MM=""
    menu CHOICE_MM "Which Mattermost?" \
        "Deploy one right here (Team Edition, zero-touch)  — recommended" \
        "Connect to an existing server"
    case "$CHOICE_MM" in
        0) IMPI_MM_MODE=codeploy ;;
        1) IMPI_MM_MODE=external ;;
    esac
fi
[ "$IMPI_GATEWAY" = slack ] && IMPI_MM_MODE=slack

preflight "$IMPI_MM_MODE"

HAS_ADMIN=0
if [ "$IMPI_MM_MODE" = codeploy ]; then
    title "Fresh Mattermost"
    dim "The imp will conjure the whole thing: admin account, team, bots. No browser needed."
    case "$(uname -m)" in
        arm64|aarch64)
            dim "Note: Mattermost ships amd64 images only, so on this machine it runs"
            dim "emulated — the first start takes a few minutes. Your runtime needs"
            dim "amd64 emulation on (Docker Desktop: Rosetta; podman: qemu)."
            ;;
    esac
    ask IMPI_MM_ADMIN_USER "Admin username" "admin"
    ask IMPI_MM_ADMIN_EMAIL "Admin email" "admin@impi.local"
    if [ -z "${IMPI_MM_ADMIN_PASSWORD:-}" ]; then
        GENERATED_PW="Impi-$(rand_hex 6)"
        ask IMPI_MM_ADMIN_PASSWORD "Admin password" "$GENERATED_PW"
    fi
    ask IMPI_MM_TEAM "Team name (slug)" "impi"
    IMPI_MM_PORT=${IMPI_MM_PORT:-8065}
    HAS_ADMIN=1
elif [ "$IMPI_MM_MODE" = external ]; then
    title "Existing Mattermost"
    ask IMPI_MM_URL "Mattermost URL (as you reach it from this machine)" "http://localhost:8065"
    if mm_wait_ready "$IMPI_MM_URL" 2; then
        ok "server answers at $IMPI_MM_URL"
    else
        bad "no /api/v4/system/ping answer at $IMPI_MM_URL"
        # shellcheck disable=SC2034  # passed by name into confirm()
        CONFIRM_URL=${IMPI_ASSUME_YES:+yes}
        confirm CONFIRM_URL "Continue anyway?" n || die "aborted"
    fi
    dim "A system-admin personal access token lets impi create bot accounts for"
    dim "you — now and later, straight from chat. Without it you will paste each"
    dim "bot's token by hand."
    ask_opt IMPI_MM_ADMIN_TOKEN "Admin token (Enter to skip)"
    [ -n "${IMPI_MM_ADMIN_TOKEN:-}" ] && HAS_ADMIN=1
    ask_opt IMPI_MM_TEAM "Team (slug) for new bots (Enter = first team)"
else
    title "Slack"
    dim "Create a Slack app with Socket Mode + a bot user, then paste its tokens."
    ask_secret IMPI_SLACK_BOT_TOKEN "Bot token (xoxb-...)"
    ask_secret IMPI_SLACK_APP_TOKEN "App-level token (xapp-...)"
fi

title "Support bot"
if [ "$HAS_ADMIN" = 1 ]; then
    dim "impi ships its own 'support' agent — an agent-builder that lives in your"
    dim "chat: ask it to create new agents, edit their profiles, write skills."
    dim "It only ever touches the agents directory, never the engine."
    IMPI_SUPPORT=${IMPI_SUPPORT:-}
    confirm IMPI_SUPPORT "Enable the support bot? (recommended)" y || true
else
    IMPI_SUPPORT=no
    dim "Support bot needs a Mattermost admin token — enable it later with"
    dim "\`impi provision support\` once you have one."
fi

title "Agents"
ask IMPI_AGENTS_DIR "Agents directory (your agents' profiles live here)" "$IMPI_HOME/agents"
ask IMPI_FIRST_AGENT "First agent name" "assistant"
ask IMPI_FIRST_AGENT_ROLE "Its role (one line)" "personal assistant"
dim "Skills are shared: installed once, given to any agent (\`impi skill\`)."
ask IMPI_SKILLS_DIR "Skill library directory" "$IMPI_HOME/skills"

title "Interactivity"
dim "Widgets let agents ask with buttons and open forms right in the chat."
IMPI_WIDGETS=${IMPI_WIDGETS:-}
if [ "$IMPI_GATEWAY" = mattermost ]; then
    confirm IMPI_WIDGETS "Enable interactive widgets? (recommended)" y || true
else
    IMPI_WIDGETS=${IMPI_WIDGETS:-yes}
fi
IMPI_INTEGRATIONS_PORT=${IMPI_INTEGRATIONS_PORT:-8423}
if [ "$IMPI_MM_MODE" = external ] && [ "$IMPI_WIDGETS" = yes ]; then
    DETECTED_IP=$(lan_ip || true)
    ask IMPI_PUBLIC_URL "Callback URL your Mattermost can reach" \
        "http://${DETECTED_IP:-<host-ip>}:$IMPI_INTEGRATIONS_PORT"
fi

title "Secrets"
dim "A secret store your agents can use but never read: they ask, you approve in"
dim "chat, and the value goes straight into the process — never into the model's"
dim "context. Adds a Vault container (~150 MB) that you unlock after each restart."
IMPI_VAULT=${IMPI_VAULT:-}
confirm IMPI_VAULT "Run a secret store?" n || true
if [ "$IMPI_VAULT" = yes ]; then
    dim "Who may approve a request? Your chat username, or several, comma-separated."
    ask IMPI_SECRET_APPROVERS "Approvers" "${IMPI_MM_ADMIN_USER:-}"
fi

title "Model backend"
if [ -z "${IMPI_LLM_MODE:-}" ]; then
    CHOICE_LLM=""
    menu CHOICE_LLM "How do your agents reach a model?" \
        "OpenAI-compatible endpoint  — base URL + API key" \
        "Subscription login          — pi's own OAuth (ChatGPT/Claude etc.)"
    case "$CHOICE_LLM" in
        0) IMPI_LLM_MODE=endpoint ;;
        1) IMPI_LLM_MODE=subscription ;;
    esac
fi
if [ "$IMPI_LLM_MODE" = endpoint ]; then
    ask IMPI_LLM_BASE_URL "LLM base URL" ""
    ask_secret IMPI_LLM_API_KEY "API key"
    # shellcheck disable=SC2153  # IMPI_LLM_MODEL is a prompt variable, not a typo
    ask IMPI_LLM_MODEL "Model name" ""
else
    dim "After the build you will log in once inside the container (pi's /login);"
    dim "credentials persist in a volume. The provider you pick there is what your"
    dim "agents use — the two answers below are OPTIONAL pins on top of it:"
    dim "left empty, the engine passes no provider/model flag at all and pi follows"
    dim "its own settings. Fill them in only to force one (an agent can still"
    dim "override both in its agent.yaml)."
    ask_opt IMPI_DEFAULT_PROVIDER "Pin a provider, e.g. openai-codex (Enter = the one you log in with)"
    ask_opt IMPI_DEFAULT_MODEL "Pin a model (Enter = that provider's own default)"
fi

# --- summary -------------------------------------------------------------------

title "Summary"
say "  Install root      : $IMPI_HOME"
say "  Compose           : $COMPOSE_CMD"
say "  Gateway           : $IMPI_GATEWAY ($IMPI_MM_MODE)"
case "$IMPI_MM_MODE" in
    codeploy) say "  Mattermost        : Team Edition on port ${IMPI_MM_PORT}, team '${IMPI_MM_TEAM}', admin '@${IMPI_MM_ADMIN_USER}'" ;;
    external) say "  Mattermost        : ${IMPI_MM_URL} (admin token: $([ "$HAS_ADMIN" = 1 ] && echo yes || echo no))" ;;
esac
say "  Agents dir        : $IMPI_AGENTS_DIR"
say "  Skill library     : $IMPI_SKILLS_DIR"
say "  First agent       : $IMPI_FIRST_AGENT ($IMPI_FIRST_AGENT_ROLE)"
say "  Support bot       : ${IMPI_SUPPORT}"
say "  Widgets           : ${IMPI_WIDGETS:-no}"
if [ "${IMPI_VAULT:-no}" = yes ]; then
    say "  Secret store      : Vault (approvers: ${IMPI_SECRET_APPROVERS:-nobody yet})"
else
    say "  Secret store      : no"
fi
if [ "$IMPI_LLM_MODE" = subscription ]; then
    say "  Model backend     : subscription login (provider: ${IMPI_DEFAULT_PROVIDER:-whatever you log in with}, model: ${IMPI_DEFAULT_MODEL:-its default})"
else
    say "  Model backend     : endpoint ${IMPI_LLM_BASE_URL} (model: ${IMPI_LLM_MODEL:-endpoint default})"
fi
hr
IMPI_CONFIRM=${IMPI_CONFIRM:-${IMPI_ASSUME_YES:+yes}}
confirm IMPI_CONFIRM "Summon the imp?" y || die "aborted — nothing was written"

# --- execution -------------------------------------------------------------------

title "Installing"

IMPI_COMPOSE_CMD=$COMPOSE_CMD
# The compose file list is DERIVED (from the mode + rootless) on every call, not
# stored: a stored list has to be regenerated whenever a release adds an overlay,
# and that regeneration is what used to wipe files a human had added.
export IMPI_HOME IMPI_COMPOSE_CMD IMPI_MM_MODE

mkdir -p "$IMPI_HOME/conf" "$IMPI_AGENTS_DIR" "$IMPI_SKILLS_DIR" \
    "$IMPI_HOME/$COMPOSE_DROPIN_DIR"

COMPOSE_ENV="$IMPI_HOME/compose.env"
env_set IMPI_HOME "$IMPI_HOME" "$COMPOSE_ENV"
env_set IMPI_PROJECT "${IMPI_PROJECT:-impi}" "$COMPOSE_ENV"
env_set IMPI_AGENTS_DIR "$IMPI_AGENTS_DIR" "$COMPOSE_ENV"
env_set IMPI_SKILLS_DIR "$IMPI_SKILLS_DIR" "$COMPOSE_ENV"
# The image is built with these ids so bind-mount writes stay owned by the
# operator. That mapping only exists on Linux: macOS runs the engine in a VM
# (Docker Desktop / podman machine) that translates ownership on its own, and
# its ids are the VM's, not the Mac's — so keep the image defaults there.
case "$(uname -s)" in
    Darwin) env_set IMPI_UID 1000 "$COMPOSE_ENV"; env_set IMPI_GID 1000 "$COMPOSE_ENV" ;;
    *)      env_set IMPI_UID "$(id -u)" "$COMPOSE_ENV"
            env_set IMPI_GID "$(id -g)" "$COMPOSE_ENV" ;;
esac
env_set IMPI_COMPOSE_CMD "$COMPOSE_CMD" "$COMPOSE_ENV"
env_set IMPI_MM_MODE "$IMPI_MM_MODE" "$COMPOSE_ENV"
env_set IMPI_COMPOSE_ROOTLESS "$COMPOSE_ROOTLESS" "$COMPOSE_ENV"
# Its own axis, like rootless: the wrapper sources this file, so `impi …`
# derives the same file list the install used.
env_set IMPI_VAULT "$([ "${IMPI_VAULT:-no}" = yes ] && echo 1 || echo 0)" "$COMPOSE_ENV"
env_set IMPI_MM_PORT "${IMPI_MM_PORT:-8065}" "$COMPOSE_ENV"
env_set IMPI_INTEGRATIONS_PORT "$IMPI_INTEGRATIONS_PORT" "$COMPOSE_ENV"
env_set IMPI_VERSION_INSTALLED "v$VERSION" "$COMPOSE_ENV"
if [ "$IMPI_MM_MODE" = codeploy ] && [ -z "$(env_get IMPI_MM_DB_PASSWORD "$COMPOSE_ENV")" ]; then
    env_set IMPI_MM_DB_PASSWORD "$(rand_hex 16)" "$COMPOSE_ENV"
fi
[ -f "$IMPI_HOME/$COMPOSE_DROPIN_DIR/README.md" ] \
    || cp "$INSTALLER_DIR/dropin-README.md" "$IMPI_HOME/$COMPOSE_DROPIN_DIR/README.md"
ok "compose.env written"

ENV_FILE="$IMPI_HOME/conf/.env"
env_set GATEWAY "$IMPI_GATEWAY" "$ENV_FILE"
env_set AGENT_NAME "$IMPI_FIRST_AGENT" "$ENV_FILE"
env_set LOG_LEVEL INFO "$ENV_FILE"
case "$IMPI_MM_MODE" in
    codeploy)
        env_set MATTERMOST_URL "http://mattermost:8065" "$ENV_FILE"
        env_set TOOL_CREATE_AGENT_TEAM "$IMPI_MM_TEAM" "$ENV_FILE"
        env_set TOOL_CREATE_CHANNEL_OWNER_USERNAME "$IMPI_MM_ADMIN_USER" "$ENV_FILE"
        ;;
    external)
        env_set MATTERMOST_URL "$(mm_container_url "$IMPI_MM_URL" "$COMPOSE_RUNTIME")" "$ENV_FILE"
        [ -n "${IMPI_MM_TEAM:-}" ] && env_set TOOL_CREATE_AGENT_TEAM "$IMPI_MM_TEAM" "$ENV_FILE"
        ;;
    slack)
        env_set SLACK_BOT_TOKEN "$IMPI_SLACK_BOT_TOKEN" "$ENV_FILE"
        env_set SLACK_APP_TOKEN "$IMPI_SLACK_APP_TOKEN" "$ENV_FILE"
        ;;
esac
if [ "$IMPI_LLM_MODE" = endpoint ]; then
    env_set LLM_BASE_URL "$IMPI_LLM_BASE_URL" "$ENV_FILE"
    env_set LLM_API_KEY "$IMPI_LLM_API_KEY" "$ENV_FILE"
    # shellcheck disable=SC2153  # IMPI_LLM_MODEL is a prompt variable, not a typo
    env_set LLM_MODEL "$IMPI_LLM_MODEL" "$ENV_FILE"
else
    [ -n "${IMPI_DEFAULT_PROVIDER:-}" ] && env_set DEFAULT_PROVIDER "$IMPI_DEFAULT_PROVIDER" "$ENV_FILE"
    [ -n "${IMPI_DEFAULT_MODEL:-}" ] && env_set DEFAULT_MODEL "$IMPI_DEFAULT_MODEL" "$ENV_FILE"
fi
if [ "${IMPI_WIDGETS:-no}" = yes ] && [ "$IMPI_GATEWAY" = mattermost ]; then
    env_set INTEGRATIONS_PORT 8423 "$ENV_FILE"
    if [ "$IMPI_MM_MODE" = codeploy ]; then
        env_set INTEGRATIONS_PUBLIC_URL "http://impi:8423" "$ENV_FILE"
    else
        env_set INTEGRATIONS_PUBLIC_URL "$IMPI_PUBLIC_URL" "$ENV_FILE"
    fi
else
    env_set INTEGRATIONS_ENABLED false "$ENV_FILE"
fi
if [ "${IMPI_VAULT:-no}" = yes ]; then
    env_set SECRETS_ENABLED true "$ENV_FILE"
    env_set SECRETS_VAULT_ADDR "http://vault:8200" "$ENV_FILE"
    env_set SECRETS_APPROVERS "$IMPI_SECRET_APPROVERS" "$ENV_FILE"
fi
[ -n "${IMPI_MM_ADMIN_TOKEN:-}" ] && env_set TOOL_CREATE_AGENT_ADMIN_TOKEN "$IMPI_MM_ADMIN_TOKEN" "$ENV_FILE"
ok "conf/.env written (chmod 600)"

# Both directories hold things worth reviewing in a diff: your agents, and
# whatever skill code you installed from elsewhere.
if command -v git >/dev/null 2>&1; then
    for _dir in "$IMPI_AGENTS_DIR" "$IMPI_SKILLS_DIR"; do
        [ -d "$_dir/.git" ] && continue
        (cd "$_dir" && git init -q) && ok "$(basename "$_dir") dir: git repository initialized"
    done
fi

run_step "Building the impi image (a few minutes on first run)" compose build impi || die "build failed"

if [ "$IMPI_MM_MODE" = codeploy ]; then
    run_step "Starting Mattermost + Postgres" compose up -d db mattermost || die "could not start Mattermost"
    run_step "Waiting for Mattermost to come up" mm_wait_ready "http://localhost:${IMPI_MM_PORT:-8065}" 90 \
        || die "Mattermost did not become healthy — see $TUI_LOG"
    if ! run_step "Creating the admin account (@$IMPI_MM_ADMIN_USER)" \
        mm_bootstrap_admin "$IMPI_MM_ADMIN_USER" "$IMPI_MM_ADMIN_EMAIL" "$IMPI_MM_ADMIN_PASSWORD"; then
        if [ -n "${IMPI_ASSUME_YES:-}" ]; then die "admin bootstrap failed"; fi
        bad "Automatic admin creation failed. Manual fallback:"
        say "  1. Open http://localhost:${IMPI_MM_PORT:-8065} and create the FIRST account"
        say "     (username '$IMPI_MM_ADMIN_USER', your password — the first user becomes admin)."
        # shellcheck disable=SC2034  # passed by name into confirm()
        CONFIRM_MANUAL=""
        confirm CONFIRM_MANUAL "Done? Continue" y || die "aborted"
        ask_secret IMPI_MM_ADMIN_PASSWORD "The password you just set"
    fi
    run_step "Creating team '$IMPI_MM_TEAM'" \
        mm_bootstrap_team "$IMPI_MM_TEAM" "$IMPI_MM_TEAM" "$IMPI_MM_ADMIN_USER" || die "team bootstrap failed"
    say "… Minting an admin token"
    IMPI_MM_ADMIN_TOKEN=$(mm_admin_token "http://mattermost:8065" "$IMPI_MM_ADMIN_USER" "$IMPI_MM_ADMIN_PASSWORD") \
        || die "could not mint the admin token — see $TUI_LOG"
    [ -n "$IMPI_MM_ADMIN_TOKEN" ] || die "empty admin token"
    env_set TOOL_CREATE_AGENT_ADMIN_TOKEN "$IMPI_MM_ADMIN_TOKEN" "$ENV_FILE"
    HAS_ADMIN=1
    ok "admin token stored (TOOL_CREATE_AGENT_ADMIN_TOKEN)"
fi

# Bots — always through the container CLI: one provisioning implementation.
if [ "$IMPI_GATEWAY" = mattermost ]; then
    if [ "$HAS_ADMIN" = 1 ]; then
        run_step "Creating agent '$IMPI_FIRST_AGENT' (bot + profile)" \
            compose run --rm -T impi impi agent add \
                --name "$IMPI_FIRST_AGENT" --role "$IMPI_FIRST_AGENT_ROLE" --yes \
            || die "agent provisioning failed"
    else
        dim "No admin token: create a bot named '$IMPI_FIRST_AGENT' in the System"
        dim "Console -> Integrations -> Bot Accounts, and paste its token."
        ask_secret IMPI_FIRST_AGENT_BOT_TOKEN "Bot token for '$IMPI_FIRST_AGENT'"
        run_step "Creating agent '$IMPI_FIRST_AGENT' (profile + token)" \
            compose run --rm -T impi impi agent add \
                --name "$IMPI_FIRST_AGENT" --role "$IMPI_FIRST_AGENT_ROLE" \
                --bot-token "$IMPI_FIRST_AGENT_BOT_TOKEN" --yes \
            || die "agent provisioning failed"
    fi
    if [ "${IMPI_SUPPORT:-no}" = yes ]; then
        run_step "Provisioning the support bot" \
            compose run --rm -T impi impi provision support --yes \
            || die "support provisioning failed"
    fi
else
    run_step "Creating agent '$IMPI_FIRST_AGENT' (profile + tokens)" \
        compose run --rm -T impi impi agent add \
            --name "$IMPI_FIRST_AGENT" --role "$IMPI_FIRST_AGENT_ROLE" \
            --gateway slack --slack-bot-token "$IMPI_SLACK_BOT_TOKEN" \
            --slack-app-token "$IMPI_SLACK_APP_TOKEN" --yes \
        || die "agent provisioning failed"
fi

if [ "$IMPI_LLM_MODE" = subscription ]; then
    if [ -n "${IMPI_ASSUME_YES:-}" ]; then
        bad "subscription login is interactive — run \`impi login\` afterwards"
    else
        title "Model login"
        say "A pi session will open inside the container. Type /login, pick your"
        say "provider, follow the flow, then exit pi (Ctrl+C or /quit)."
        CONFIRM_LOGIN=""
        confirm CONFIRM_LOGIN "Open it now?" y || true
        if [ "$CONFIRM_LOGIN" = yes ]; then
            # 1455: the openai-codex OAuth flow runs a fixed localhost callback
            # server inside the container; the browser lives on the host.
            compose run --rm -p 1455:1455 impi pi </dev/tty || true
        fi
        if compose run --rm -T impi test -s /home/impi/.pi/agent/auth.json >>"$TUI_LOG" 2>&1; then
            ok "pi credentials saved (pi-auth volume)"
        else
            bad "no pi credentials yet — the engine will fail model calls until you"
            bad "run \`impi login\` (or \`impi login --copy-auth\` from a logged-in machine)"
        fi
    fi
fi

run_step "Starting the engine" compose up -d || die "engine start failed"

engine_ready() {
    local i=0
    while [ "$i" -lt 30 ]; do
        if engine_logged "app built:"; then return 0; fi
        sleep 2
        i=$((i + 1))
    done
    return 1
}
run_step "Waiting for the engine" engine_ready || {
    bad "engine did not report readiness — check \`impi logs\`"
}

mkdir -p "$HOME/.local/bin"
install_executable "$INSTALLER_DIR/bin/impi" "$HOME/.local/bin/impi"
ok "wrapper installed: ~/.local/bin/impi"
case ":$PATH:" in
    *:"$HOME/.local/bin":*) : ;;
    *) bad "\$HOME/.local/bin is not on your PATH — add: export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac

title "Done — the imp is loose"
if [ "$IMPI_MM_MODE" = codeploy ]; then
    say "  Mattermost : http://localhost:${IMPI_MM_PORT:-8065}"
    say "  Login      : @$IMPI_MM_ADMIN_USER / $IMPI_MM_ADMIN_PASSWORD"
    say "               (shown only once — store it now)"
fi
say "  Try it     : DM @$IMPI_FIRST_AGENT"
[ "${IMPI_SUPPORT:-no}" = yes ] && say "  Build more : DM @support — 'create an agent that ...'"
if [ "$COMPOSE_RUNTIME" = podman ]; then
    say ""
    say "  Reboots    : podman does not auto-start containers — run \`impi start\`"
    say "               after a machine restart (data survives either way)."
fi
say ""
say "  impi status | logs -f | restart | agent add | update | doctor | uninstall"
