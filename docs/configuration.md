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

The human-approved secret broker: an agent asks for a credential, you approve it
in chat, and the value is injected into the process it named — never into the
model's context. Off unless you turn it on, because it needs a backend to talk
to. Full guide, including what this does and does not protect against:
[secrets.md](secrets.md).

| Variable | Default | Purpose |
|---|---|---|
| `SECRETS_ENABLED` | `false` | run the broker at all |
| `SECRETS_VAULT_ADDR` | `http://vault:8200` | where the store lives |
| `SECRETS_VAULT_MOUNT` | `secrets` | the KV v2 mount the engine owns |
| `SECRETS_ROLE_ID` | — | the engine's AppRole; written by `impi secret init` |
| `SECRETS_UNSEAL_KEY_FILE` | — | unattended unlock: the unseal key, mounted as a file |
| `SECRETS_SECRET_ID_FILE` | — | unattended unlock: the AppRole secret, mounted as a file |
| `SECRETS_APPROVERS` | — | CSV of usernames or user ids that may answer a request |
| `SECRETS_APPROVAL_CHANNEL` | — | where requests are posted (empty = DM the first approver) |
| `SECRETS_APPROVAL_TIMEOUT_S` | `120` | how long a request waits before it is refused |
| `SECRETS_MAX_GRANT_S` | `3600` | ceiling over every policy's own window ceiling |

The AppRole **secret** has no variable on purpose: it is the credential that
opens the store, and anything in `.env` is readable by every agent that can read
`.env`. It reaches the engine either interactively (`impi secret unlock`, held
only in memory) or through `SECRETS_SECRET_ID_FILE` — which is the convenient
option and the weaker one, for exactly that reason.

## Logging

| Variable | Default | Purpose |
|---|---|---|
| `LOG_LEVEL` | `INFO` | root log level |
