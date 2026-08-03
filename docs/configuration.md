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
| `AGENT_NAME` | `assistant` | the agent that `MATTERMOST_TOKEN`/`SLACK_*` fall back to |
| `DOTENV_PATH` | `.env` | file the per-agent keys below are read from |

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

## pi runtime

| Variable | Default | Purpose |
|---|---|---|
| `PI_BIN` | `pi` | path to the `pi` CLI (assumed on `PATH`) |
| `PI_SESSION_DIR` | `""` | `pi` session storage; default `{DATA_DIR}/pi-sessions` (per-agent subdirs) |
| `PI_TIMEOUT` | `180` | per-turn timeout (s) when `agent.yaml` omits `runtime.timeout` |
| `PI_MAX_CONCURRENT_SESSIONS` | `4` | max concurrent `pi` subprocesses |
| `PI_SESSION_IDLE_TTL` | `1800` | reap a `pi` subprocess after this many idle seconds |

## Tool server

| Variable | Default | Purpose |
|---|---|---|
| `TOOL_ENABLED` | `true` | master switch for the typed-tool server |
| `TOOL_SERVER_HOST` | `127.0.0.1` | tool-server bind host |
| `TOOL_SERVER_PORT` | `8422` | tool-server port |

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

See [troubleshooting.md](troubleshooting.md) if callbacks don't arrive.

## Inter-agent messaging (impi)

| Variable | Default | Purpose |
|---|---|---|
| `AGENTS_REPLY_TO_AGENTS` | `true` | agents answer other agents' mentions |
| `AGENT_MAX_HOPS` | `4` | refuse an agent-triggered turn past this depth from a human |
| `AGENT_RATE_LIMIT_TURNS` | `6` | max agent-triggered turns per conversation… |
| `AGENT_RATE_WINDOW_S` | `60` | …within this sliding window (seconds) |

## Logging

| Variable | Default | Purpose |
|---|---|---|
| `LOG_LEVEL` | `INFO` | root log level |
