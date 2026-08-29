# Configuration

The engine is configured from the environment / `.env` via pydantic-settings
(`crucible.config.Settings`, extended by `impi.config.ImpiSettings`). Copy
`.env.example` to `.env` and fill it in. A field's env var is its name upper-cased
(`mattermost_url` → `MATTERMOST_URL`). The real `.env` is git-ignored — never
commit secrets.

## LLM endpoint (optional)

Only needed when `pi` talks to a custom OpenAI-compatible endpoint. Empty when
`pi` uses a subscription login (it authenticates itself). These are forwarded into
`pi`'s subprocess env, not consumed by Python.

| Variable | Default | Purpose |
|---|---|---|
| `LLM_BASE_URL` | `""` | OpenAI-compatible base URL |
| `LLM_API_KEY` | `""` | API key |
| `LLM_MODEL` | `""` | model name for the endpoint |
| `LLM_VERIFY_SSL` | `true` | set `false` to skip TLS verification |

## Provider / model defaults

Used when an agent's `agent.yaml` omits `provider`/`model`. Empty = pass no flag,
letting `pi` use its own default. See [runtime-notes.md](runtime-notes.md) for the
full resolution order.

| Variable | Default | Purpose |
|---|---|---|
| `DEFAULT_PROVIDER` | `""` | `pi` backend for agents that don't set one (e.g. `openai-codex`) |
| `DEFAULT_MODEL` | `""` | model within that backend |
| `SUPPORT_PROVIDER` | `""` | override for the bundled `support` agent (falls back to `DEFAULT_PROVIDER`) |
| `SUPPORT_MODEL` | `""` | same, for the model |

## Mattermost gateway

| Variable | Default | Purpose |
|---|---|---|
| `MATTERMOST_URL` | `http://localhost:8065` | server URL |
| `MATTERMOST_TOKEN` | `""` | bot token for the default agent (`AGENT_NAME`) |
| `MM_MAX_POST_CHARS` | `16000` | reply chunking threshold |

## Slack gateway (Socket Mode)

Used by agents with `GATEWAY=slack`. Interactivity arrives over the socket, so no
HTTP receiver is needed.

| Variable | Default | Purpose |
|---|---|---|
| `SLACK_BOT_TOKEN` | `""` | `xoxb-…` bot token for the default agent |
| `SLACK_APP_TOKEN` | `""` | `xapp-…` app-level token (scope `connections:write`) |
| `SLACK_COMMAND_PREFIX` | `crux_` | message shortcuts whose callback id starts with this are commands; the rest of the id is the command name (empty = every shortcut is one). See [commands.md](commands.md) |

## Gateway selection

| Variable | Default | Purpose |
|---|---|---|
| `GATEWAY` | `mattermost` | which gateway agents run on (`mattermost` \| `slack` \| `ws`); override per agent below |

Gateway kinds mix freely in one engine process — agent A on Slack, agent B on
Mattermost, agent C on ws, each with its own connection, supervised
independently. One constraint: all Mattermost agents share the single
`MATTERMOST_URL` (one MM server per process).

## ws gateway (custom client services)

A duplex WebSocket hub for your own programs — see
[ws-gateway.md](ws-gateway.md). Started only when some agent has
`AGENTS_GATEWAY__<AGENT>=ws`.

| Variable | Default | Purpose |
|---|---|---|
| `WS_HOST` | `0.0.0.0` | hub bind host |
| `WS_PORT` | `8424` | hub port (`ws://host:port/ws`) |

Client services are dynamic keys (register with `impi ws add-service`):
`WS_SERVICE_TOKEN__<NAME>` — the service's bearer token;
`WS_SERVICE_AGENTS__<NAME>` — CSV allowlist of agents it may address
(unset = every ws agent).

## Agents

| Variable | Default | Purpose |
|---|---|---|
| `AGENTS_PATH` | `""` | directory holding `agents/<name>/agent.yaml` + `.pi/`. A plain directory (may be a git repo, but nothing requires it). |
| `AGENTS_ENABLED` | `""` | CSV of agent names to run; empty = all found in `AGENTS_PATH` |
| `SKILLS_COMMAND` | `skills` | trigger word of the library browser: the platform's slash command must use this exact word, or it goes to an agent instead |
| `SKILLS_PATH` | `""` | the shared skill library (its own directory, ideally its own git repo); empty = `_skills` beside the agents. See [skills.md](skills.md) |
| `AGENT_NAME` | `assistant` | the default agent: the one `MATTERMOST_TOKEN`/`SLACK_*`/`COMMAND_TOKENS` fall back to, and the one `/command/default` resolves to when several agents run |
| `COMMAND_TOKENS` | `""` | slash-command tokens of the default agent (CSV); the per-agent key wins where both are set |
| `DOTENV_PATH` | `.env` | file the per-agent keys below are read from |

`AGENT_NAME` answers the same question at two scopes, so be clear which one you
are reading. Set **here**, it names the deployment's default agent. Inside the
environment of an agent's **own process** the engine overwrites it with that
agent's name, so a program the agent runs can tell who it is running as — which
is how `secret-exec` finds the right identity. A program run by hand in the
engine's container sees the deployment value, not an agent's.

### Per-agent keys (dynamic)

These are keyed by the upper-cased agent name (`-`→`_`) and read from the
environment or `.env`. They are dynamic (not fixed fields), so they don't appear
as defaults — set only the ones you need.

| Pattern | Purpose |
|---|---|
| `AGENTS_MM_TOKEN__<AGENT>` | that agent's Mattermost bot token |
| `AGENTS_SLACK_BOT_TOKEN__<AGENT>` | that agent's Slack bot token |
| `AGENTS_SLACK_APP_TOKEN__<AGENT>` | that agent's Slack app-level token |
| `AGENTS_GATEWAY__<AGENT>` | that agent's gateway (`mattermost` \| `slack`) |
| `AGENTS_SKILLS__<AGENT>` | override that agent's skills (CSV of names; empty = none; unset = its `agent.yaml`) |

An agent is present only if its token is set; a tokenless profile is skipped.

## State

| Variable | Default | Purpose |
|---|---|---|
| `DATA_DIR` | `data` | base dir for the SQLite inventory, `pi` session dirs, logs |
| `DB_PATH` | `""` | SQLite path; default `{DATA_DIR}/impi.db` |
| `DOTENV_PATH` | `.env` | where the `.env` file itself lives — containers mount the config directory and point this at `/app/conf/.env` |

## Files and photos

See [files.md](files.md) for what an agent gets and what each platform needs.

| Variable | Default | Purpose |
|---|---|---|
| `ATTACHMENTS_ENABLED` | `true` | master switch: off = attachments are ignored |
| `ATTACHMENTS_DIR` | `""` | where attachments land; default `{DATA_DIR}/attachments` |
| `ATTACHMENT_MAX_MB` | `20` | per-file cap; a bigger file is skipped, the message still arrives |
| `ATTACHMENT_RETENTION_DAYS` | `14` | delete attachments older than this; `0` = keep forever |
| `INLINE_IMAGE_MAX_MB` | `4` | above this a picture is not shown to the model, only named by path |

## Scheduled work

See [tasks.md](tasks.md) for what a task is and how a missed run is handled.

| Variable | Default | Purpose |
|---|---|---|
| `SCHEDULER_ENABLED` | `true` | master switch: off = no ticker, and no agent is offered the scheduling tools |
| `SCHEDULER_TICK_S` | `20` | how often the engine looks for due work |
| `SCHEDULER_TIMEZONE` | `UTC` | the zone a task's schedule is read in when it names none (the container runs in UTC) |
| `SCHEDULER_MAX_CONCURRENT` | `2` | scheduled runs at once; the runtime allows `PI_MAX_CONCURRENT_SESSIONS` in total |
| `SCHEDULER_RUN_DEADLINE_S` | `900` | stop waiting on a run (the turn is not cancelled) |
| `SCHEDULER_STARTUP_GRACE_S` | `60` | wait this long after a start before catching anything up |
| `SCHEDULER_MAX_FAILURES` | `5` | failures in a row before a task is paused |
| `SCHEDULER_MAX_TASKS_PER_AGENT` | `50` | cap on tasks one agent may create |
| `TASKS_COMMAND` | `tasks` | the slash command the task browser binds to |

## pi runtime

| Variable | Default | Purpose |
|---|---|---|
| `PI_BIN` | `pi` | path to the `pi` CLI (assumed on `PATH`) |
| `PI_SESSION_DIR` | `""` | `pi` session storage; default `{DATA_DIR}/pi-sessions` (per-agent subdirs) |
| `PI_TIMEOUT` | `180` | per-turn timeout (s) when `agent.yaml` omits `runtime.timeout` |
| `PI_MAX_CONCURRENT_SESSIONS` | `4` | max concurrent `pi` subprocesses |
| `PI_MAX_SESSIONS_PER_AGENT` | `0` | a second bound, per agent; `0` = only the global one |
| `PI_SESSION_IDLE_TTL` | `1800` | reap a `pi` subprocess after this many idle seconds |

## Agent containers

Each agent's runtime in a container of its own — see
[agent-containers.md](agent-containers.md). Off by default; the installer asks,
and `impi agent sync` writes everything below except the first key.

| Variable | Default | Purpose |
|---|---|---|
| `AGENT_HOSTS_ENABLED` | `false` | ask each agent's own host for a runtime instead of forking one |
| `AGENT_HOST_URL` | `http://agent-{agent}:8427` | where a host is; `{agent}` is the name |
| `AGENT_HOST_TIMEOUT` | `30` | seconds to wait for a host to accept and answer a spawn |
| `AGENTS_HOST_TOKEN__<AGENT>` | — | the secret that agent's host shares with the engine |
| `AGENTS_HOST_URL__<AGENT>` | — | override the address for one agent |

An agent with no `AGENTS_HOST_TOKEN__…` runs in the engine, whatever the URL
would be — a host that would accept anybody is not one the engine will talk to.
The engine names such agents in its log at boot.

Note the deployment-level key `IMPI_AGENT_CONTAINERS` in `~/.impi/compose.env`,
which is separate: it decides whether the generated overlay is merged at all.

## Tool server

| Variable | Default | Purpose |
|---|---|---|
| `TOOL_ENABLED` | `true` | master switch for the typed-tool server |
| `TOOL_SERVER_HOST` | `127.0.0.1` | tool-server bind host |
| `TOOL_SERVER_PORT` | `8422` | tool-server port |
| `TOOL_PUBLIC_URL` | `""` | what agents should CALL, when that is not where it binds (set with agent containers) |

Individual tools read their own settings from `TOOL_<TOOL>_*` keys (loaded by the
registry, not declared centrally), e.g. `TOOL_CREATE_CHANNEL_OWNER_USERNAME` — the
human owner auto-added to private channels an agent creates.

### Agent provisioning (`create_agent` tool + `impi agent add`)

| Variable | Default | Purpose |
|---|---|---|
| `TOOL_CREATE_AGENT_ADMIN_TOKEN` | `""` | Mattermost **system-admin** PAT; enables automatic bot creation (the support agent's `create_agent` tool and the `impi agent add` CLI) |
| `TOOL_CREATE_AGENT_TEAM` | `""` | team new bots join; empty = the server's first team |
| `TOOL_CREATE_AGENT_MATTERMOST_URL` | `""` | override; falls back to `MATTERMOST_URL` |
| `TOOL_CREATE_AGENT_AGENTS_PATH` | `""` | override; falls back to `AGENTS_PATH` |
| `TOOL_CREATE_AGENT_DOTENV_PATH` | `""` | override; falls back to `DOTENV_PATH` |

## Interactivity (widget/form callbacks)

| Variable | Default | Purpose |
|---|---|---|
| `INTEGRATIONS_ENABLED` | `true` | master switch for widgets/forms |
| `INTEGRATIONS_HOST` | `0.0.0.0` | receiver bind host |
| `INTEGRATIONS_PORT` | `8423` | receiver port |
| `INTEGRATIONS_PUBLIC_URL` | `""` | URL a containerized Mattermost calls back to; default `http://host.containers.internal:{port}`; `auto` = detect the host LAN IP at startup |
| `INTEGRATIONS_UI_TIMEOUT` | `90` | seconds to await a human on a blocking confirm/select before default-reject |

Commands (slash commands) reach the same receiver at
`POST {INTEGRATIONS_PUBLIC_URL}/command/<agent>` — register that URL with the
platform's command and put the token it issues in the dynamic key
`AGENTS_COMMAND_TOKENS__<AGENT>` (CSV, one per command). An agent with no tokens
refuses every command. `/command/default` in place of the agent's name lets the
engine pick: the only agent running, else `AGENT_NAME`; paired with the
unsuffixed `COMMAND_TOKENS`, a single-agent deployment names no agent anywhere.
Slack needs no URL — it uses `crux_*` message shortcuts over its socket. See
[commands.md](commands.md).

See [troubleshooting.md](troubleshooting.md) if callbacks don't arrive.

## Inter-agent messaging (impi)

| Variable | Default | Purpose |
|---|---|---|
| `AGENTS_REPLY_TO_AGENTS` | `true` | agents answer other agents' mentions |
| `AGENT_MAX_HOPS` | `4` | refuse an agent-triggered turn past this depth from a human |
| `AGENT_RATE_LIMIT_TURNS` | `6` | max agent-triggered turns per conversation… |
| `AGENT_RATE_WINDOW_S` | `60` | …within this sliding window (seconds) |

## Secrets

An agent asks for a credential, you approve it in chat, and the value is
injected into the process it named — never into the model's context. Full guide:
[secrets.md](secrets.md).

**None of it is configured in this file.** Secrets are a tool that ships beside
the engine rather than a part of it: the broker runs in its own container, and
its two clients — `secret-exec`, which an agent runs, and `ward-admin`, which
`impi ward …` runs — read their settings from the container's environment, which
the compose overlay declares. The engine has no field for any of this, and a
value here would reach nothing.

| Variable | Where | Purpose |
|---|---|---|
| `SECRET_BROKER_URL` | the overlay, on the engine's container | where the broker answers |
| `SECRET_BROKER_CERTS_DIR` | the same | where the identities are mounted |
| `SECRET_BROKER_CERT` / `_KEY` / `_CA` | optional | explicit paths, for a deployment that mounts them elsewhere |

An identity is otherwise derived: an agent presents `<certs>/<its own
name>.crt`, the operator presents `<certs>/operator.crt`, and both verify the
broker against `<certs>/ca.crt`. The engine's whole contribution is `AGENT_NAME`,
a generic fact it gives every agent about itself. The authority's key is in none
of these directories — it lives with the broker, so nothing on this side can mint
an identity for an agent that does not exist.

The broker's own settings — the store's address, the approvers, the timeouts —
are `WARD_*` and live in its container's environment, not here.

| Variable | Default | Purpose |
|---|---|---|
| `WARD_VAULT_ADDR` | `http://127.0.0.1:8200` | the store, on the loopback it shares with the broker |
| `WARD_ROLE_ID` | — | the broker's AppRole; written by `ward init` |
| `WARD_UNSEAL_KEY_FILE` / `WARD_SECRET_ID_FILE` | — | unattended unlock, mounted as files |
| `WARD_MATTERMOST_TOKEN` | — | the bot approval cards are posted as |
| `WARD_APPROVERS` | — | CSV of usernames or ids that may answer |
| `WARD_APPROVAL_CHANNEL` | — | where cards go (empty = a DM to the first approver) |
| `WARD_APPROVAL_TIMEOUT_S` | `120` | how long a request waits before it is refused |
| `WARD_MAX_GRANT_S` | `3600` | ceiling over every policy's own window ceiling |
| `WARD_NOTICE_FOLD_S` | `900` | how long a run of automatic grants folds into one notice rather than posting again |
| `WARD_COMMAND_TOKENS` | — | the `/ward` slash command's tokens (CSV). Empty = no operator surface in chat. Operator-grade: anything reaching the receiver with one of these can claim to be any user, and only the approver check that follows decides |
| `WARD_OPERATOR_DIR` | `/var/lib/ward/operator` | where `ward init` puts the operator's identity — a different directory from the agents', because that one is mounted where the agents run |

## Tool confirmations

A tool that declares `requires_confirmation` is asked about before it runs, by
the engine itself and not only by the runtime — a call that reaches the tool
server directly is gated too.

| Variable | Default | Purpose |
|---|---|---|
| `TOOL_MAX_GRANT_S` | `900` | longest window a human may open for a gated tool (`0` = ask every time) |

## Logging

| Variable | Default | Purpose |
|---|---|---|
| `LOG_LEVEL` | `INFO` | root log level |
