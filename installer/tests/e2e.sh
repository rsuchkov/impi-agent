#!/usr/bin/env bash
# Local end-to-end test of the installer (Linux + podman/docker):
#   make e2e-install             # asserts a full zero-touch codeploy install
#   make e2e-install KEEP=1      # keep the stack for inspection
#   make e2e-install BROWSER=1   # also install and exercise the browser axis
#   make e2e-install AGENTS=1    # also give each agent a container of its own
#   make e2e-install STORE=mongo # keep the inventory in MongoDB instead of SQLite
#   make e2e-install UPDATE_FROM=v0.24.0  # install that release, `impi update`
#                                # to the newest tag, then assert the moved stack
#
# The legs that need a real model are opt-in and take their configuration from
# outside the tree, so nothing here has to be edited to run them:
#   EXTRA_ANSWERS=file  appended to the answers, e.g. a real LLM_* or a
#                       subscription mode with a pinned provider
#   E2E_PI_AUTH=file    seed pi's subscription login (~/.pi/agent/auth.json)
#   E2E_LLM=1           ask the agent things and wait for it to answer
#
# Installs the CURRENT WORKING TREE (not a git clone, so uncommitted installer
# changes are covered — except with UPDATE_FROM, which needs a clone with its
# tags) into a temp IMPI_HOME under a dedicated compose project
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
# Which inventory backend to install. `sqlite` is what every other run uses;
# `mongo` adds a database container and moves the engine's state onto it.
STORE=${STORE:-sqlite}
# Off by default: a release to install FIRST, with the wrapper of that release
# then updating to the newest tag of this clone. What is asserted is that the
# stack moved: every rebuilt service recreated, one engine running, the schema
# migrated, the version recorded. The checks after it run against the updated
# stack. Asks for a clone rather than the working tree, so what is installed
# is the tagged release and the update finds the tags at origin.
UPDATE_FROM=${UPDATE_FROM:-}

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

# 1. Stage the working tree as the "clone" (rsync keeps uncommitted changes in)
#    — or, for an update run, a real clone of this repository checked out at the
#    release to start from: `impi update` fetches the tags from origin, which is
#    then this working tree's own repository.
mkdir -p "$E2E_HOME/repo"
if [ -n "$UPDATE_FROM" ]; then
    git clone --quiet "$REPO_DIR" "$E2E_HOME/repo"
    git -C "$E2E_HOME/repo" checkout --quiet "$UPDATE_FROM"
else
    rsync -a --exclude .git --exclude .venv --exclude data --exclude '.*_cache' \
        --exclude .pytest_cache --exclude node_modules "$REPO_DIR/" "$E2E_HOME/repo/"
    git -C "$E2E_HOME/repo" init -q 2>/dev/null || true
fi

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
if [ "$STORE" = mongo ]; then echo "IMPI_MONGO=yes" >>"$ANSWERS"; fi
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

# 3a. The update, when asked for: run by the wrapper the OLD release installed,
#     because that is the wrapper a deployment updates with.
if [ -n "$UPDATE_FROM" ]; then
    LATEST=$(git -C "$REPO_DIR" tag -l 'v*' | sort -V | tail -n 1)
    check "installed $UPDATE_FROM first" \
        grep -q "^IMPI_VERSION_INSTALLED=$UPDATE_FROM\$" "$E2E_HOME/compose.env"
    # Wait for the old engine to be up: a recreate of a container that never
    # started would prove nothing about the one it replaced.
    for _ in $(seq 1 30); do
        if engine_logged "app built:"; then break; fi
        sleep 2
    done
    check "the engine of $UPDATE_FROM came up" engine_logged "app built:"
    # By the runtime's own labels: `compose ps` takes no service here with
    # podman-compose, and the ids are what tell a recreated container from one
    # left running.
    RUNTIME=${IMPI_COMPOSE_CMD%% *}
    svc_ids() { # SERVICE [all]
        "$RUNTIME" ps ${2:+-a} -q --filter "label=com.docker.compose.project=$IMPI_PROJECT" \
            --filter "label=com.docker.compose.service=$1"
    }
    declare -A ids_before=()
    for _svc in $(build_services); do ids_before[$_svc]=$(svc_ids "$_svc"); done
    engine_id_before=${ids_before[impi]}
    check "impi update --yes to $LATEST" \
        env IMPI_HOME="$E2E_HOME" "$HOME/.local/bin/impi" update --yes
    # The release's own helpers from here on: the repo is at the new tag now.
    # shellcheck disable=SC1091
    . "$E2E_HOME/repo/installer/lib/compose.sh"
    # shellcheck disable=SC1091
    . "$E2E_HOME/compose.env"
    check "the repo is checked out at $LATEST" \
        test "v$(cat "$E2E_HOME/repo/VERSION")" = "$LATEST"
    check "compose.env records $LATEST" \
        grep -q "^IMPI_VERSION_INSTALLED=$LATEST\$" "$E2E_HOME/compose.env"
    # Every rebuilt service is a NEW container (the v0.24.0 lesson: a rebuilt
    # image with an unchanged definition left the old ones running), and the
    # old engine is gone rather than running beside the new one.
    all_recreated() {
        local _svc _id
        for _svc in $(build_services); do
            _id=$(svc_ids "$_svc")
            [ -n "$_id" ] && [ "$_id" != "${ids_before[$_svc]:-}" ] || return 1
        done
    }
    check "every rebuilt service runs in a new container" all_recreated
    one_engine() { [ "$(svc_ids impi | grep -c .)" -eq 1 ]; }
    check "exactly one engine container runs" one_engine
    old_engine_gone() { [ "$(svc_ids impi all | grep -c -x "$engine_id_before")" -eq 0 ]; }
    check "the old engine container is gone" old_engine_gone
    for _ in $(seq 1 30); do
        if engine_logged "app built:"; then break; fi
        sleep 2
    done
    check "the updated engine logs 'app built:'" engine_logged "app built:"
    # The schema moved with the code: the columns the newer releases add are
    # there once the engine has opened its store. SQLite only — the file is in
    # the engine's data volume, so it is read from inside the engine.
    if [ "$STORE" = sqlite ]; then
        schema_migrated() {
            compose exec -T impi python3 -c '
import os, sqlite3, sys
# The engine names its store file after itself (impi.STORE_NAME); a wrong path
# here would make an empty database rather than fail, so the file must exist.
path = "/app/data/impi.db"
if not os.path.exists(path):
    sys.exit(f"no store at {path}")
db = sqlite3.connect(path)
cols = lambda t: {r[1] for r in db.execute(f"PRAGMA table_info({t})")}
sys.exit(0 if "last_engine_turn_at" in cols("sessions") and {"watch_key", "expires_at"} <= cols("tasks") else 1)
'
        }
        check "the sqlite schema was migrated on the engine's first start" schema_migrated
    fi
fi

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
check "conf/.env has the receiver secret" grep -q '^INTEGRATIONS_SECRET=.\{16,\}$' "$E2E_HOME/conf/.env"
env_mode() { p=$(stat -c '%a' "$E2E_HOME/conf/.env" 2>/dev/null || stat -f '%Lp' "$E2E_HOME/conf/.env"); [ "$p" = 600 ]; }
check "conf/.env is 0600" env_mode
check "pi's home is a directory on the host" test -d "$E2E_HOME/pi/agent"
check "agent profile scaffolded" test -f "$E2E_HOME/agents/agents/assistant/agent.yaml"
# The box the engine runs in: nothing writable but its volumes and /tmp.
root_is_read_only() { ! compose run --rm -T impi touch /app/probe 2>/dev/null; }
check "the engine's root is read-only" root_is_read_only
check "the engine can write its data volume" \
    compose run --rm -T impi sh -c 'touch /app/data/.probe && rm /app/data/.probe'
check "/tmp is writable in the engine" compose run --rm -T impi sh -c 'touch /tmp/.probe'
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
    agent_up() { compose ps 2>/dev/null | grep -c "agent-assistant" >/dev/null; }
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
    agent_root_is_read_only() { ! in_agent assistant touch /app/probe 2>/dev/null; }
    check "the agent's root is read-only" agent_root_is_read_only
    check "/tmp is writable in the agent" in_agent assistant sh -c 'touch /tmp/.probe'

    # And the engine really did stop forking runtimes for it.
    remote_logged() {
        compose logs impi 2>/dev/null \
            | grep -c "Agents running in containers of their own" >/dev/null
    }
    check "the engine reports the agent as remote" remote_logged
    # A new agent's container does not exist until somebody syncs — the whole
    # point of the trade, so it is asserted rather than assumed.
    tester_has_no_container() { ! compose ps 2>/dev/null | grep -c "agent-tester" >/dev/null; }
    check "a newly added agent has no container until sync" tester_has_no_container
fi

# 4c. The inventory axis, when it was installed (STORE=mongo).
#
# The engine boots and registers bots whatever the backend, so a stack that
# comes up proves nothing here. What has to be shown is that the state really
# moved: the collections fill, and the CLI — a SEPARATE process, the one that
# used to open a SQLite file and report an empty stand — reads the same data
# back.
if [ "$STORE" = mongo ]; then
    check "compose.env records the inventory axis" \
        grep -q '^IMPI_MONGO=1$' "$E2E_HOME/compose.env"
    mongo_up() { compose ps 2>/dev/null | grep -q "${IMPI_PROJECT}_mongo"; }
    check "the mongo container is running" mongo_up
    check "the engine is pointed at it" \
        compose run --rm -T impi sh -c 'test "$STORE_BACKEND" = mongo'

    # The engine registers its agents at boot, so this collection is the first
    # thing it writes. An empty one means the engine wrote its inventory
    # somewhere else — which is exactly the failure this axis has to rule out.
    agents_recorded() {
        compose exec -T mongo mongosh --quiet impi \
            --eval 'db.agents.countDocuments({}) > 0' | grep -q true
    }
    check "the engine wrote its agents to mongo" agents_recorded
    check "no SQLite inventory was created" \
        sh -c '! compose run --rm -T impi test -f /app/data/impi.db'
    # The CLI is another container with its own connection: this is the case
    # that used to answer "no sessions" from a file nobody wrote.
    check "the CLI reads the same inventory" \
        sh -c 'compose run --rm -T impi impi task list 2>&1 | grep -qv "^✘"'
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
        # -c, not -q: doctor is still printing after the browser line, and a
        # grep that quits early SIGPIPEs it — pipefail then fails a match.
        IMPI_HOME=$E2E_HOME "$HOME/.local/bin/impi" doctor 2>&1 | grep -c "browser: Chrome/" >/dev/null
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

    # 6a. A reaction as a command. The map is per agent and read at start, so
    #     it goes into conf/.env and the engine is restarted for it. The message
    #     under the emoji says what the command means, so an answer proves the
    #     word and the quote both reached the model; the engine's log line
    #     proves it read the reaction as a command — the check that does not
    #     depend on what the model chose to say.
    printf 'AGENTS_REACTION_COMMANDS__ASSISTANT=eyes=echo\n' >>"$E2E_HOME/conf/.env"
    built_before=$(engine_log_count "app built:")
    ws_before=$(engine_log_count "Websocket authentication OK")
    compose restart impi >/dev/null 2>&1
    for _ in $(seq 1 45); do
        if [ "$(engine_log_count 'app built:')" -gt "$built_before" ] \
            && [ "$(engine_log_count 'Websocket authentication OK')" -gt "$ws_before" ]; then break; fi
        sleep 2
    done
    export ME_ID
    ask "A rule for later: when you get the command /echo, answer with exactly one word: pelican. For now reply with the single word: noted."
    took_the_rule() { waits_for "noted" 60; }
    check "the agent took the rule" took_the_rule
    # The person's reaction on their own message: a bot's never counts.
    react_to_last() { EMOJI=$1 python3 -c '
import json, os, urllib.request
def call(path, body=None):
    request = urllib.request.Request(os.environ["BASE"] + path, data=body, method="POST" if body else "GET")
    request.add_header("Authorization", "Bearer " + os.environ["ADMIN_PAT"])
    request.add_header("Content-Type", "application/json")
    return json.load(urllib.request.urlopen(request, timeout=10))
posts = call("/channels/" + os.environ["CHAN"] + "/posts")["posts"].values()
mine = sorted((p for p in posts if p["user_id"] == os.environ["ME_ID"]), key=lambda p: p["create_at"])
call("/reactions", json.dumps({"user_id": os.environ["ME_ID"], "post_id": mine[-1]["id"],
                               "emoji_name": os.environ["EMOJI"]}).encode())
'; }
    ASK_STAMP=$(python3 -c 'import time; print(int(time.time() * 1000))')
    export ASK_STAMP
    check "a reaction is put on the person's message" react_to_last eyes
    read_as_command() {
        for _ in $(seq 1 30); do
            if engine_logged "runs /echo for assistant"; then return 0; fi
            sleep 2
        done
        return 1
    }
    check "the engine read :eyes: as /echo for the assistant" read_as_command
    echoed() { waits_for "pelican" 90; }
    check "the agent answered the command the reaction ran" echoed

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
        # The page's title, not its H1: example.com dropped its <h1> (the page is
        # now paragraphs in several languages), and a model answering truthfully
        # "there is no H1" failed this check for being right.
        ask "Open https://example.com in your browser and reply with the page's exact title and nothing else."
        # A string that is on the page. Asking for something it could recite
        # from training would pass without a browser ever opening, so the run
        # afterwards checks the relay saw a client too.
        browsed() { waits_for "example domain" 90; }
        check "the agent browsed a page when asked" browsed
    fi
fi

# 7. Connected accounts: a person connects their own account at an OAuth
#    provider from a direct message, and an agent acts as them.
#
#    The provider is a stand-in run inside the engine's container
#    (e2e_fake_oauth.py), and so is the "person" (e2e_connect.py): engine,
#    browser and provider then meet on 127.0.0.1 and nothing is published. The
#    person presses buttons through Mattermost's API, gets the consent link the
#    way a person does — privately, over the websocket — and opens it.
FAKE_PORT=9431
FAKE=http://127.0.0.1:$FAKE_PORT
ENV_FILE=$E2E_HOME/conf/.env
ADMIN_EMAIL=$(grep '^IMPI_MM_ADMIN_EMAIL=' "$E2E_HOME/repo/installer/tests/e2e.answers" | cut -d= -f2-)
check "impi connections key writes a sealing key" compose run --rm -T impi impi connections key
check "the key is in conf/.env" grep -q '^CONNECTIONS_KEYS=.\{40,\}$' "$ENV_FILE"
cat >>"$ENV_FILE" <<EOF
CONNECTION_CLIENT_ID__FAKE=e2e-client
CONNECTION_CLIENT_SECRET__FAKE=e2e-secret
CONNECTION_KIND__FAKE=oauth2
CONNECTION_AUTHORIZE_URL__FAKE=$FAKE/oauth/authorize
CONNECTION_TOKEN_URL__FAKE=$FAKE/oauth/token
CONNECTION_USERINFO_URL__FAKE=$FAKE/user
CONNECTION_REVOKE_URL__FAKE=$FAKE/oauth/revoke
CONNECTION_API_URL__FAKE=$FAKE/api
CONNECTION_AGENTS__FAKE=assistant
CONNECTIONS_PUBLIC_URL=http://127.0.0.1:8429
EOF
# The panel's slash command, registered the way docs/commands.md says.
SECRET=$(grep '^INTEGRATIONS_SECRET=' "$ENV_FILE" | cut -d= -f2-)
TEAM_ID=$(auth_get /teams/name/impi | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
COMMAND_TOKEN=$(curl -sf -H "Authorization: Bearer $ADMIN_PAT" -X POST "$BASE/commands" -d "{
    \"team_id\": \"$TEAM_ID\", \"trigger\": \"connections\", \"method\": \"P\",
    \"url\": \"http://impi:8423/$SECRET/command/assistant\", \"display_name\": \"connections\"
}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')
echo "AGENTS_COMMAND_TOKENS__ASSISTANT=$COMMAND_TOKEN" >>"$ENV_FILE"
sed -i 's/^    - bash$/    - bash\n    - connected_accounts\n    - connected_api_read\n    - connected_api_write/' \
    "$E2E_HOME/agents/agents/assistant/agent.yaml"

built_before=$(engine_log_count "app built:")
ws_before=$(engine_log_count "Websocket authentication OK")
compose restart impi >/dev/null 2>&1
engine_back() {
    [ "$(engine_log_count 'app built:')" -gt "$built_before" ] \
        && [ "$(engine_log_count 'Websocket authentication OK')" -gt "$ws_before" ]
}
for _ in $(seq 1 45); do
    engine_back && break
    sleep 2
done
check "the engine came back with connections" engine_back
check "the startup log names the provider and its agent" engine_logged "connections: fake (agents: assistant)"
check "impi health names it too" \
    compose run --rm -T impi sh -c 'impi health 2>&1 | grep -c "connections: fake" >/dev/null'
callback_listens() {
    compose exec -T impi python -c '
import urllib.error, urllib.request
try:
    urllib.request.urlopen("http://127.0.0.1:8429/oauth/callback", timeout=5)
except urllib.error.HTTPError as exc:
    raise SystemExit(0 if exc.code == 400 else 1)
raise SystemExit(1)'
}
check "the callback listener answers on its own port" callback_listens

compose exec -T impi sh -c 'cat > /tmp/e2e_fake_oauth.py' <"$E2E_HOME/repo/installer/tests/e2e_fake_oauth.py"
compose exec -T impi sh -c 'cat > /tmp/e2e_connect.py' <"$E2E_HOME/repo/installer/tests/e2e_connect.py"
compose exec -T impi sh -c \
    "nohup python /tmp/e2e_fake_oauth.py $FAKE_PORT $ADMIN_EMAIL >/tmp/e2e_fake_oauth.log 2>&1 &"
fake_up() { compose exec -T impi python -c "import urllib.request as u; u.urlopen('$FAKE/log', timeout=2)"; }
for _ in $(seq 1 15); do fake_up && break; sleep 1; done
check "the stand-in provider is up" fake_up

ASSISTANT_ID=$(auth_get /users/username/assistant | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
ME_ID=$(auth_get /users/me | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
DM=$(curl -sf -H "Authorization: Bearer $ADMIN_PAT" -X POST "$BASE/channels/direct" \
    -d "[\"$ME_ID\",\"$ASSISTANT_ID\"]" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
TOWN=$(auth_get /teams/"$TEAM_ID"/channels/name/town-square | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
# person MODE CHANNEL -> its OK/FAIL lines in $E2E_HOME/person.<mode>.
person() {
    compose exec -T impi python /tmp/e2e_connect.py "$1" http://mattermost:8065 "$ADMIN_PAT" "$TEAM_ID" "$2" \
        "$ASSISTANT_ID" fake >"$E2E_HOME/person.$1" 2>&1 || true
    sed 's/^/    /' "$E2E_HOME/person.$1"
}
# Anchored at the end only: podman-compose prints its banner, colour codes and
# all, onto the line before the script's first one.
stepped() { grep -q "OK $2\$" "$E2E_HOME/person.$1"; }
provider_saw() { # python predicate over the provider's log
    compose exec -T impi python -c "
import json, urllib.request
log = json.load(urllib.request.urlopen('$FAKE/log', timeout=5))
raise SystemExit(0 if ($1) else 1)"
}

person refused "$TOWN"
check "/connections in a channel is refused privately" stepped refused "refused outside a direct message"
check "and nothing is posted there" stepped refused "nothing posted in the channel"

person panel "$DM"
check "/connections in the DM posts the panel" stepped panel "the panel offers Connect"
check "Connect hands the link to the person alone" stepped panel "the link came privately"
check "the link is not in the message" stepped panel "the link is not in the post"
check "the callback page says connected" stepped panel "the callback page says connected"
check "the panel says the connect worked" stepped panel "the same message says it worked"
check "the provider saw PKCE asked and verified" \
    provider_saw "any(e['event'] == 'authorize' and e['pkce'] for e in log) and any(e['event'] == 'token' and e['verified'] for e in log)"
check "Disconnect redraws the panel" stepped panel "the panel says disconnected"
check "and revokes at the provider" provider_saw "any(e['event'] == 'revoke' for e in log)"

# With a live model: the agent reaches for the provider, finds the person not
# connected, and the engine offers Connect in the conversation itself; once
# connected, the same question gets an answer read as them.
if [ "${E2E_LLM:-0}" = 1 ]; then
    CHAN=$DM
    export CHAN
    QUESTION="Use connected_api_read with provider fake and path projects, then tell me the name of the project it returns."
    ask "$QUESTION"
    person offer "$DM"
    check "a tool that found no connection posted the Connect offer" stepped offer "the offer offers Connect"
    check "the offer's link came privately" stepped offer "the link came privately"
    check "the offer says the connect worked" stepped offer "the same message says it worked"
    ask "$QUESTION"
    read_as_them() { waits_for "e2e-marker-project" 90; }
    check "asked again, the agent read the provider as the person" read_as_them
    check "with the person's token" provider_saw "any(e['event'] == 'api' and e['authorized'] for e in log)"
fi

echo "== e2e: $PASS passed, $FAIL failed =="
[ "$FAIL" -eq 0 ]
