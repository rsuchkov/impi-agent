#!/usr/bin/env bash
# Local end-to-end test of the installer (Linux + podman/docker):
#   make e2e-install             # asserts a full zero-touch codeploy install
#   make e2e-install KEEP=1      # keep the stack for inspection
#   make e2e-install BROWSER=1   # also install and exercise the browser axis
#   make e2e-install AGENTS=1    # also give each agent a container of its own
#
# The legs that need a real model are opt-in and take their configuration from
# outside the tree, so nothing here has to be edited to run them:
#   EXTRA_ANSWERS=file  appended to the answers, e.g. a real LLM_* or a
#                       subscription mode with a pinned provider
#   E2E_PI_AUTH=file    seed pi's subscription login (~/.pi/agent/auth.json)
#   E2E_LLM=1           ask the agent things and wait for it to answer
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
# Off by default: the browser image is Chrome, which is minutes of build and
# about a gigabyte. Opt in when the axis is what you are changing.
BROWSER=${BROWSER:-0}
# Off by default too: the agent image is Node plus the runtime, which is minutes
# of build. Opt in when the axis is what you are changing.
AGENTS=${AGENTS:-0}

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
#
# Through the answers file rather than the environment, even for the one key
# that varies: an answers file is the interface an unattended install actually
# uses, so a key the whitelist does not know has to fail here rather than in
# somebody's CI.
ANSWERS=$E2E_HOME/answers
cp "$E2E_HOME/repo/installer/tests/e2e.answers" "$ANSWERS"
if [ "$BROWSER" = 1 ]; then echo "IMPI_BROWSER=yes" >>"$ANSWERS"; fi
if [ "$AGENTS" = 1 ]; then echo "IMPI_AGENT_CONTAINERS=yes" >>"$ANSWERS"; fi
# Last wins, so this overrides the checked-in defaults rather than adding to
# them — which is what lets a real model be supplied without editing a file
# that every other run depends on.
if [ -n "${EXTRA_ANSWERS:-}" ]; then cat "$EXTRA_ANSWERS" >>"$ANSWERS"; fi

bash "$E2E_HOME/repo/installer/main.sh" --home "$E2E_HOME" --answers "$ANSWERS"

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

engine_ready() { engine_logged "app built:"; }
ready=1
for _ in $(seq 1 30); do
    if engine_ready; then ready=0; break; fi
    sleep 2
done
check "engine logs 'app built:'" test "$ready" -eq 0
both_agents_in_log() {
    # -c, not -q: a short-circuiting grep SIGPIPEs its producer and pipefail
    # would report that instead of the match (see engine_logged).
    compose logs impi 2>/dev/null | grep "app built:" | grep assistant | grep -c support >/dev/null
}
check "engine sees both agents" both_agents_in_log

# 4. Add one more agent through the container CLI, restart, expect 3 agents.
check "impi agent add tester" \
    compose run --rm -T impi impi agent add --name tester --role "e2e probe" --yes
compose restart impi >/dev/null 2>&1
sleep 3
tester_up() {
    for _ in $(seq 1 30); do
        if compose logs impi 2>/dev/null | grep "app built:" | tail -n 1 | grep -c tester >/dev/null; then
            return 0
        fi
        sleep 2
    done
    return 1
}
check "engine picked up 'tester' after restart" tester_up

# 4b. Per-agent containers, when they were installed (AGENTS=1).
#
# The claim being tested is isolation, so the checks are about what an agent
# CANNOT see as much as what it can: its own profile at the engine's own path,
# its own session volume, and no sight of the other agent's anything.
if [ "$AGENTS" = 1 ]; then
    in_agent() { compose exec -T "agent-$1" "${@:2}"; }

    check "compose.env records the agent-container axis" \
        grep -q '^IMPI_AGENT_CONTAINERS=1$' "$E2E_HOME/compose.env"
    check "the overlay was generated" test -f "$E2E_HOME/conf/agents.compose.yaml"
    check "the engine knows the agent's host token" \
        grep -q '^AGENTS_HOST_TOKEN__ASSISTANT=' "$E2E_HOME/conf/.env"
    agent_up() { compose ps 2>/dev/null | grep -q "agent-assistant"; }
    check "the assistant's container is running" agent_up
    check "its host answers its own health endpoint" \
        in_agent assistant python -c \
        'import urllib.request as u; u.urlopen("http://127.0.0.1:8427/health", timeout=5)'
    check "the agent sees its own profile at the engine's path" \
        in_agent assistant test -f /app/agents/agents/assistant/agent.yaml
    check "the agent has a session volume of its own" \
        in_agent assistant test -d /app/sessions
    # The isolation, stated as an absence: another agent's profile is not mounted.
    no_other_profile() { ! in_agent assistant test -e /app/agents/agents/tester; }
    check "the agent cannot see another agent's profile" no_other_profile
    # The property that broke when the agents' containers had no user mapping of
    # their own: agent and engine share a volume, so a file one writes the other
    # has to read. Nothing here asserts an owner — only that it works, which is
    # the part an operator would notice.
    shared_files_work() {
        compose exec -T agent-assistant \
            sh -c 'echo e2e-probe > /app/files/assistant/probe.txt' \
            && compose run --rm -T impi grep -q e2e-probe /app/files/assistant/probe.txt
    }
    check "the agent can write a file the engine can read" shared_files_work

    # And the engine really did stop forking runtimes for it.
    remote_logged() {
        compose logs impi 2>/dev/null \
            | grep -c "Agents running in containers of their own" >/dev/null
    }
    check "the engine reports the agent as remote" remote_logged
    # A new agent's container does not exist until somebody syncs — the whole
    # point of the trade, so it is asserted rather than assumed.
    tester_has_no_container() { ! compose ps 2>/dev/null | grep -q "agent-tester"; }
    check "a newly added agent has no container until sync" tester_has_no_container
fi

# 5. The browser axis, when it was installed (BROWSER=1).
#
# Every check runs FROM THE ENGINE, which is the whole point: the browser
# publishes no host port and sits on a network of its own, so a probe from here
# would prove nothing an agent can use. This walks the same path an agent walks
# — engine, browser network, relay, Chrome — and then drives the CLI the skill
# tells the agent to run.
if [ "$BROWSER" = 1 ]; then
    in_engine() { compose run --rm -T impi "$@"; }

    check "compose.env records the browser axis" \
        grep -q '^IMPI_BROWSER=1$' "$E2E_HOME/compose.env"
    browser_up() { compose ps 2>/dev/null | grep -q "${IMPI_PROJECT}_browser"; }
    check "the browser container is running" browser_up
    check "the engine is told where the browser is" \
        in_engine sh -c 'test -n "$BROWSER_CDP_URL"'

    # httpx, not curl: the engine's image has none. Chrome starts on this call —
    # the relay launches it for the first client — so the timeout is generous.
    cdp_answers() {
        in_engine python3 -c '
import os, sys, httpx
info = httpx.get(os.environ["BROWSER_CDP_URL"] + "/json/version", timeout=60).json()
name = info.get("Browser", "")
agent = info.get("User-Agent", "")
sys.stderr.write(f"{name} / {agent}\n")
# HeadlessChrome in either field is the automation tell the image rebuilds the
# product token to avoid; a plain "Chrome/NNN" is what should come back.
sys.exit(0 if name.startswith("Chrome/") and "Headless" not in agent else 1)
'
    }
    check "Chrome answers CDP from the engine, and does not say Headless" cdp_answers

    # The rewrite is load-bearing: playwright attaches by reading this URL out of
    # /json/version, so a webSocketDebuggerUrl still pointing at the browser
    # container's own loopback would send the client back to itself.
    ws_rewritten() {
        in_engine python3 -c '
import os, sys, httpx
url = os.environ["BROWSER_CDP_URL"]
ws = httpx.get(url + "/json/version", timeout=60).json().get("webSocketDebuggerUrl", "")
sys.stderr.write(ws + "\n")
sys.exit(0 if ws.startswith("ws://browser:9222/") else 1)
'
    }
    check "webSocketDebuggerUrl points back at the relay" ws_rewritten

    skill_installed() { in_engine impi skill list 2>/dev/null | grep -q web-browsing; }
    check "the web-browsing skill is installed" skill_installed
    # By reference, not by copy: `skill assign` writes the library entry into
    # the profile, which is what makes one installed skill servable to many.
    check "the first agent has it" \
        grep -q 'registry:web-browsing' "$E2E_HOME/agents/agents/assistant/agent.yaml"

    # What the skill tells an agent to run, in the order it tells them to.
    #
    # One `bash -c`, deliberately: playwright-cli keeps its session in the
    # container it ran in, so a sequence split across `compose run` invocations
    # would find no session on the second command. An agent is not split that
    # way — every bash call it makes lands in the same running engine.
    browse() {
        in_engine bash -c '
set -e
playwright-cli attach --cdp="$BROWSER_CDP_URL" >/dev/null
playwright-cli goto https://example.com >/dev/null
# Inline, as a fenced block — this is the command that does NOT write a file.
out=$(playwright-cli snapshot)
printf "%s\n" "$out" >&2
printf "%s" "$out" | grep -qi "example domain"
# And a ref out of that tree drives the page, which is the whole loop.
ref=$(printf "%s" "$out" | grep -oE "\[ref=[a-z0-9]+\]" | head -n1 | tr -d "[]" | cut -d= -f2)
test -n "$ref"
playwright-cli click "$ref" >/dev/null
playwright-cli detach >/dev/null
'
    }
    check "attach -> goto -> snapshot -> click drives the page" browse

    # Through the wrapper, because `impi doctor` is where an operator looks
    # first and its browser probe has its own copy of the reasoning. A probe
    # that never runs is a probe nobody finds out is wrong.
    doctor_sees_chrome() {
        IMPI_HOME=$E2E_HOME "$HOME/.local/bin/impi" doctor 2>&1 | grep -q "browser: Chrome/"
    }
    check "impi doctor reports the browser" doctor_sees_chrome

    # The boundary, not a guardrail: the browser is on a network the chat server
    # is not on, so a page cannot be used as a hop into it.
    no_hop() {
        ! compose exec -T browser \
            timeout 5 bash -c 'exec 3<>/dev/tcp/mattermost/8065' 2>/dev/null
    }
    check "the browser cannot reach Mattermost" no_hop
fi

# 6. Optional live model legs (E2E_LLM=1, with a real model in the answers).
#    Everything above proves the plumbing; these two ask whether an agent
#    actually uses it.
#
#    E2E_PI_AUTH=<path to ~/.pi/agent/auth.json> seeds a subscription login into
#    the stack. The installer cannot: that login is interactive, and an
#    unattended run has no terminal to do it in — which would otherwise put the
#    only legs that exercise a model out of reach of an unattended run.
if [ -n "${E2E_PI_AUTH:-}" ]; then
    seed_auth() {
        compose run --rm -T impi sh -c \
            'mkdir -p /home/impi/.pi/agent && cat > /home/impi/.pi/agent/auth.json
             chmod 600 /home/impi/.pi/agent/auth.json' <"$E2E_PI_AUTH"
    }
    check "pi subscription login seeded" seed_auth
    built_before=$(engine_log_count "app built:")
    ws_before=$(engine_log_count "Websocket authentication OK")
    compose restart impi >/dev/null 2>&1
    # Two conditions, and the old code had neither. `engine_logged "app built:"`
    # matches the line from BEFORE the restart — the log is cumulative — so it
    # returned at once and waited for nothing. And readiness is logged before the
    # chat gateway has authenticated its websocket, so a question asked in that
    # gap is not answered late, it is never seen at all.
    engine_back() {
        [ "$(engine_log_count 'app built:')" -gt "$built_before" ] \
            && [ "$(engine_log_count 'Websocket authentication OK')" -gt "$ws_before" ]
    }
    for _ in $(seq 1 45); do
        engine_back && break
        sleep 2
    done
fi

if [ "${E2E_LLM:-0}" = 1 ]; then
    ASSISTANT_ID=$(auth_get /users/username/assistant | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
    ME_ID=$(auth_get /users/me | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
    CHAN=$(curl -sf -H "Authorization: Bearer $ADMIN_PAT" -X POST "$BASE/channels/direct" \
        -d "[\"$ME_ID\",\"$ASSISTANT_ID\"]" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
    export CHAN BASE ADMIN_PAT ASSISTANT_ID

    # Both helpers take their arguments through the environment: a prompt is
    # prose, and prose in a shell argument is a quoting accident waiting to
    # rewrite the test.
    # When the question was asked, so an answer can be told from something the
    # bot said earlier. Mattermost posts an automatic greeting the moment a DM
    # channel with a bot is created, and a check that accepts "any message from
    # the assistant" accepts THAT — which is how a leg meant to prove a model
    # answered passed on a stack whose model never answered once.
    ASK_STAMP=0
    ask() {
        ASK_STAMP=$(python3 -c 'import time; print(int(time.time() * 1000))')
        export ASK_STAMP
        MSG=$1 python3 -c '
import json, os, urllib.request
body = json.dumps({"channel_id": os.environ["CHAN"], "message": os.environ["MSG"]}).encode()
request = urllib.request.Request(os.environ["BASE"] + "/posts", data=body, method="POST")
request.add_header("Authorization", "Bearer " + os.environ["ADMIN_PAT"])
request.add_header("Content-Type", "application/json")
urllib.request.urlopen(request, timeout=10).read()
'; }

    # Has the agent said anything containing $WANT SINCE the question? An empty
    # WANT matches any answer at all, which is what the bare roundtrip asks — but
    # never something posted before we asked, and never the engine's own fallback
    # text, which is what it posts when the turn failed.
    said() { WANT=${1:-} python3 -c '
import json, os, sys, urllib.request
FALLBACKS = ("temporarily unavailable", "broke on my side")
request = urllib.request.Request(os.environ["BASE"] + "/channels/" + os.environ["CHAN"] + "/posts")
request.add_header("Authorization", "Bearer " + os.environ["ADMIN_PAT"])
posts = json.load(urllib.request.urlopen(request, timeout=10))["posts"].values()
since = int(os.environ.get("ASK_STAMP") or 0)
mine = [
    p["message"] for p in posts
    if p["user_id"] == os.environ["ASSISTANT_ID"] and p["create_at"] > since
]
answers = [m for m in mine if m.strip() and not any(f in m for f in FALLBACKS)]
sys.stderr.write(repr(answers) + chr(10))
sys.exit(0 if any(os.environ["WANT"].lower() in m.lower() for m in answers) else 1)
'; }

    waits_for() { # WANT tries
        for _ in $(seq 1 "$2"); do
            said "$1" 2>/dev/null && return 0
            sleep 2
        done
        return 1
    }

    ask "ping"
    answered() { waits_for "" 60; }
    check "the assistant ANSWERED a DM (not a greeting, not a fallback)" answered

    # With a live model AND containers, the two things the dummy model cannot
    # show: a turn the engine counts as complete, and the memory of it landing in
    # the agent's own volume rather than the engine's. The second is what
    # `impi agent migrate` exists to move, so a deployment that got it wrong
    # would look exactly like an agent that forgot everybody.
    if [ "$AGENTS" = 1 ]; then
        turn_completed() { compose logs impi 2>/dev/null | grep -c "pi turn: session=" >/dev/null; }
        check "the engine counted a completed turn" turn_completed
        memory_is_the_agents() {
            compose exec -T agent-assistant sh -c 'ls -A /app/sessions | grep -q .' \
                && ! compose run --rm -T impi \
                    sh -c 'ls -A /app/data/pi-sessions/assistant 2>/dev/null | grep -q .'
        }
        check "its memory is in its own volume, not the engine's" memory_is_the_agents
    fi

    # The one leg nothing else can cover: whether the MODEL, handed the skill,
    # reaches for the browser on its own. Every other browser check drives
    # playwright-cli directly, which proves the plumbing and says nothing about
    # whether an agent ever finds it.
    if [ "$BROWSER" = 1 ]; then
        ask "Open https://example.com in your browser and reply with the page's exact H1 heading and nothing else."
        # A string that is on the page. Asking for something it could recite
        # from training would pass without a browser ever opening, so the run
        # afterwards checks the relay saw a client too.
        browsed() { waits_for "example domain" 90; }
        check "the agent browsed a page when asked" browsed
    fi
fi

echo "== e2e: $PASS passed, $FAIL failed =="
[ "$FAIL" -eq 0 ]
