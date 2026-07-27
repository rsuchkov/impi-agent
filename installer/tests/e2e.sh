#!/usr/bin/env bash
# Local end-to-end test of the installer (Linux + podman/docker):
#   make e2e-install          # asserts a full zero-touch codeploy install
#   make e2e-install KEEP=1   # keep the stack for inspection
#
# Installs the CURRENT WORKING TREE (not a git clone, so uncommitted installer
# changes are covered) into a temp IMPI_HOME under a dedicated compose project
# (impi-e2e) on a non-default MM port, then asserts services, bootstrap, bots,
# config, and the wrapper. The engine's LLM endpoint is a dummy: the engine
# must boot and register bots without ever calling a model.

set -euo pipefail

REPO_DIR=$(cd "$(dirname "$0")/../.." && pwd)
E2E_HOME=$(mktemp -d "${TMPDIR:-/tmp}/impi-e2e.XXXXXX")
export IMPI_PROJECT=impi-e2e
export IMPI_ASSUME_YES=1
MM_PORT=8066

PASS=0
FAIL=0
check() { # "label" cmd...
    local label=$1; shift
    if "$@" >/dev/null 2>&1; then
        printf '✔ %s\n' "$label"; PASS=$((PASS + 1))
    else
        printf '✘ %s\n' "$label" >&2; FAIL=$((FAIL + 1))
    fi
}

cleanup() {
    if [ "${KEEP:-0}" = 1 ]; then
        printf 'KEEP=1 — stack left running; IMPI_HOME=%s\n' "$E2E_HOME"
        return
    fi
    (
        set +eu
        [ -f "$E2E_HOME/compose.env" ] || exit 0
        # shellcheck disable=SC1091
        . "$E2E_HOME/repo/installer/lib/compose.sh" 2>/dev/null || exit 0
        # compose.env is bash-sourceable by contract (quoted where needed).
        # shellcheck disable=SC1091
        . "$E2E_HOME/compose.env"
        IMPI_HOME=$E2E_HOME
        [ -n "${IMPI_COMPOSE_CMD:-}" ] && compose down -v -t 5 >/dev/null 2>&1
        exit 0
    )
    rm -rf "$E2E_HOME"
}
trap cleanup EXIT

echo "== e2e install into $E2E_HOME (project $IMPI_PROJECT, MM port $MM_PORT) =="

# 1. Stage the working tree as the "clone" (rsync keeps uncommitted changes in).
mkdir -p "$E2E_HOME/repo"
rsync -a --exclude .git --exclude .venv --exclude data --exclude '.*_cache' \
    --exclude .pytest_cache --exclude node_modules "$REPO_DIR/" "$E2E_HOME/repo/"
git -C "$E2E_HOME/repo" init -q 2>/dev/null || true

# 2. Run the installer non-interactively.
bash "$E2E_HOME/repo/installer/main.sh" --home "$E2E_HOME" \
    --answers "$E2E_HOME/repo/installer/tests/e2e.answers"

# 3. Assertions.
# shellcheck disable=SC1091
. "$E2E_HOME/repo/installer/lib/compose.sh"
IMPI_HOME=$E2E_HOME
# shellcheck disable=SC1091
. "$E2E_HOME/compose.env"
export IMPI_HOME IMPI_COMPOSE_CMD IMPI_COMPOSE_FILES

ADMIN_PAT=$(grep '^TOOL_CREATE_AGENT_ADMIN_TOKEN=' "$E2E_HOME/conf/.env" | cut -d= -f2-)
BASE="http://localhost:$MM_PORT/api/v4"
auth_get() { curl -sf --max-time 5 -H "Authorization: Bearer $ADMIN_PAT" "$BASE$1"; }

check "Mattermost answers ping" curl -sf --max-time 5 "$BASE/system/ping"
check "admin PAT is valid (users/me)" auth_get /users/me
check "bot 'assistant' exists" auth_get /users/username/assistant
check "bot 'support' exists" auth_get /users/username/support
check "conf/.env has the agent token" grep -q '^AGENTS_MM_TOKEN__ASSISTANT=' "$E2E_HOME/conf/.env"
check "conf/.env has the support token" grep -q '^AGENTS_MM_TOKEN__SUPPORT=' "$E2E_HOME/conf/.env"
check "conf/.env has the widget callback URL" \
    grep -q '^INTEGRATIONS_PUBLIC_URL=http://impi:8423$' "$E2E_HOME/conf/.env"
env_mode() { p=$(stat -c '%a' "$E2E_HOME/conf/.env" 2>/dev/null || stat -f '%Lp' "$E2E_HOME/conf/.env"); [ "$p" = 600 ]; }
check "conf/.env is 0600" env_mode
check "agent profile scaffolded" test -f "$E2E_HOME/agents/agents/assistant/agent.yaml"
check "agents dir is a git repo" test -d "$E2E_HOME/agents/.git"

engine_ready() { compose logs impi 2>/dev/null | grep -q "app built:"; }
ready=1
for _ in $(seq 1 30); do
    if engine_ready; then ready=0; break; fi
    sleep 2
done
check "engine logs 'app built:'" test "$ready" -eq 0
both_agents_in_log() {
    compose logs impi 2>/dev/null | grep "app built:" | grep assistant | grep -q support
}
check "engine sees both agents" both_agents_in_log

# 4. Add one more agent through the container CLI, restart, expect 3 agents.
check "impi agent add tester" \
    compose run --rm -T impi impi agent add --name tester --role "e2e probe" --yes
compose restart impi >/dev/null 2>&1
sleep 3
tester_up() {
    for _ in $(seq 1 30); do
        if compose logs impi 2>/dev/null | grep "app built:" | tail -n 1 | grep -q tester; then
            return 0
        fi
        sleep 2
    done
    return 1
}
check "engine picked up 'tester' after restart" tester_up

# 5. Optional live DM roundtrip (needs a real model): E2E_LLM=1 + real LLM_* in
#    the answers file.
if [ "${E2E_LLM:-0}" = 1 ]; then
    ASSISTANT_ID=$(auth_get /users/username/assistant | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
    ME_ID=$(auth_get /users/me | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
    CHAN=$(curl -sf -H "Authorization: Bearer $ADMIN_PAT" -X POST "$BASE/channels/direct" \
        -d "[\"$ME_ID\",\"$ASSISTANT_ID\"]" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
    curl -sf -H "Authorization: Bearer $ADMIN_PAT" -X POST "$BASE/posts" \
        -d "{\"channel_id\":\"$CHAN\",\"message\":\"ping\"}" >/dev/null
    reply() {
        for _ in $(seq 1 60); do
            if curl -sf -H "Authorization: Bearer $ADMIN_PAT" "$BASE/channels/$CHAN/posts" \
                | python3 -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if any(p['user_id']=='$ASSISTANT_ID' for p in d['posts'].values()) else 1)"; then
                return 0
            fi
            sleep 2
        done
        return 1
    }
    check "assistant replied to a DM" reply
fi

echo "== e2e: $PASS passed, $FAIL failed =="
[ "$FAIL" -eq 0 ]
