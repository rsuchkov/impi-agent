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
letting `pi` use its own default. A provider needs a model: `pi` refuses a
provider without one, so the engine does not start with `DEFAULT_PROVIDER`
set and `DEFAULT_MODEL` empty (nor with `SUPPORT_PROVIDER` and neither model,
nor with an `agent.yaml` naming a provider and no model anywhere). A model
alone is fine — `pi` finds its provider. See [runtime-notes.md](runtime-notes.md)
for the full resolution order.

| Variable | Default | Purpose |
|---|---|---|
| `DEFAULT_PROVIDER` | `""` | `pi` backend for agents that don't set one (e.g. `openai-codex`) |
| `DEFAULT_MODEL` | `""` | model within that backend; required when `DEFAULT_PROVIDER` is set |
| `DEFAULT_FALLBACK_MODELS` | `""` | CSV of `provider/model` every agent moves on to, in order, when its own model fails the way a service fails (overloaded, rate-limited/429, 5xx, unreachable) — not on a spent quota or a refused key. An agent's `runtime.fallback_models` replaces it; the agent's own model in the list is skipped |
| `AGENTS_FALLBACK_MODELS__<AGENT>` | — | that agent's fallback models, overriding its profile and the default; set and empty = none |
| `SUPPORT_PROVIDER` | `""` | override for the bundled `support` agent (falls back to `DEFAULT_PROVIDER`) |
| `SUPPORT_MODEL` | `""` | same, for the model (falls back to `DEFAULT_MODEL`); one of the two is required when `SUPPORT_PROVIDER` is set |

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

## Reactions as commands

On Mattermost and Slack alike, an emoji a person puts on a message can run a
command in that message's conversation, as a slash command or a shortcut would
([commands.md](commands.md#reactions-as-commands)). Needs
`INTEGRATIONS_ENABLED=true`; on Slack also the `reactions:read` scope and the
`reaction_added` event subscription.

| Variable | Default | Purpose |
|---|---|---|
| `REACTION_COMMANDS` | `""` | the **default agent's** emoji → command map: CSV of `emoji=command` or a bare `emoji` (the command is then the emoji's name); platform short names without colons. Every other agent's map is `AGENTS_REACTION_COMMANDS__<AGENT>` — per agent, because every agent in a channel sees every reaction there. An entry that is not `emoji=command` stops the engine at start, naming the key. Empty = reactions are not commands |
| `REACTION_QUOTE_LIMIT` | `1500` | how much of the reacted message the agent is shown, quoted as its author's words |

## Gateway selection

| Variable | Default | Purpose |
|---|---|---|
| `GATEWAY` | `mattermost` | the gateways agents are reached through (`mattermost` \| `slack` \| `ws` \| `http`), as a CSV when several; override per agent with `AGENTS_GATEWAY__<AGENT>` |

Gateway kinds mix freely in one engine process — agent A on Slack, agent B on
Mattermost, agent C on ws, each with its own connection, supervised
independently. One constraint: all Mattermost agents share the single
`MATTERMOST_URL` (one MM server per process).

**One agent, several gateways.** `AGENTS_GATEWAY__ASSISTANT=mattermost,slack`
reaches the same agent through both: one profile, one runtime session per
conversation, a reply through the gateway the message came in by. The first
in the list is the agent's **default gateway** — where a turn the engine
starts by itself (a scheduled task, a reported event) goes, when the
conversation is live there (or nowhere yet); otherwise it goes where the
conversation last spoke. A tool's confirmation card is posted in every
gateway the conversation is live in; the first answer, from whichever copy,
settles it and every copy shows the verdict. A gateway whose tokens are
missing is left out with a logged reason; the agent runs on the rest. What a tool call may do is
decided per call by the gateway the conversation is in: a channel-admin tool
asked for from Slack, which has no channel administration, is refused with
`not available through this gateway` even though the agent has the tool
through Mattermost. Which conversation a Slack thread and an http dialog are
the same one is the application's to say (`ConversationKey`); `impi` keeps the
platform's own ids, so a conversation stays within its gateway.

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

## http gateway (request/turn API)

A request/turn HTTP API for programs that cannot hold a socket open — a browser
behind someone else's server, a caller spread over several nodes; see
[http-gateway.md](http-gateway.md). Started when some agent has
`AGENTS_GATEWAY__<AGENT>=http`, or the engine is a replica (below).

| Variable | Default | Purpose |
|---|---|---|
| `HTTP_HOST` | `0.0.0.0` | hub bind host |
| `HTTP_PORT` | `8428` | hub port (`http://host:port/v1`) |
| `HTTP_MAX_WAIT_S` | `8` | the longest a poll may wait for new events, whatever the caller asks — every waiting poll holds a connection |
| `HTTP_DRAIN_S` | `20` | on shutdown, how long the hub lets its running turns finish after it stopped taking new messages; keep the orchestrator's grace period above it |

Callers are dynamic keys (register with `impi http add-caller`):
`HTTP_CALLER_TOKEN__<NAME>` — the program's bearer token;
`HTTP_CALLER_AGENTS__<NAME>` — CSV allowlist of agents it may address
(unset = every http agent). The program is trusted to say who its user is
(`X-User-Id`, `X-Username`); a deployment that needs the caller to prove it
writes its own authenticator on the library.

## Several engine processes (replicas)

Two or more engines on one store — pods behind a balancer. Each process is a
replica with an address the others reach it at, and all share one secret for
what they hand each other over the http hub's port: a turn for a conversation
another replica owns (the one whose runtime remembers it), a click on a card
another replica posted. The hub runs for these routes even with no http agent.
Both set = conversations have owners and are forwarded; either empty = one
process, nothing is forwarded (two replicas then each serve whatever reaches
them, and a card can be answered only where it was posted).

| Variable | Default | Purpose |
|---|---|---|
| `REPLICA_URL` | `""` | this process's address on the hub's port, as the other replicas reach it (`http://10.0.3.7:8428`; in Kubernetes the pod IP from the downward API) |
| `REPLICA_TOKEN` | `""` | the secret every replica presents on the internal routes; one value for the whole deployment |

The replicas share one configuration apart from `REPLICA_URL`: the same
`INTEGRATIONS_SECRET` and `INTEGRATIONS_PUBLIC_URL` above all, since a card one
replica posts is clicked through whichever replica the callback address reaches,
and that replica answers 403 to a secret that is not its own. A deployment that
mounts one `.env` on every pod has this by construction.

A message through a gateway whose connection one replica holds (Mattermost,
Slack) is forwarded to the owner; one through the http or ws gateway is served
where it arrived — a balancer pinned the caller there, a socket is open there —
and the conversation moves with it, the owner's older memory caught up from the
application's history on the next turn.

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
| `AGENTS_GATEWAY__<AGENT>` | that agent's gateway (`mattermost` \| `slack` \| `ws` \| `http`), or several as a CSV (`mattermost,slack`); the first is its default |
| `AGENTS_SKILLS__<AGENT>` | override that agent's skills (CSV of names; empty = none; unset = its `agent.yaml`) |
| `AGENTS_ENV__<AGENT>` | override the engine environment variables that agent's process inherits (CSV of names, `*` allowed; empty = none beyond the base set; unset = its `agent.yaml` `runtime.env`) |
| `AGENTS_FALLBACK_MODELS__<AGENT>` | override that agent's fallback models (CSV of `provider/model`; empty = none; unset = its `agent.yaml`, else `DEFAULT_FALLBACK_MODELS`) |
| `AGENTS_REACTION_COMMANDS__<AGENT>` | that agent's emoji → command map (CSV of `emoji=command`); the default agent falls back to `REACTION_COMMANDS`, every other agent to none |

An agent is present only if its token is set; a tokenless profile is skipped.

## State

| Variable | Default | Purpose |
|---|---|---|
| `DATA_DIR` | `data` | base dir for the inventory file, `pi` session dirs, logs |
| `STORE_BACKEND` | `sqlite` | what kind of database holds the inventory: `sqlite` or `mongo` — see [storage.md](storage.md) |
| `DB_NAME` | `""` | which one: a file path on `sqlite` (default `{DATA_DIR}/impi.db`), a database name on `mongo` (default `impi`) |
| `DB_URL` | `""` | where the server is; required on `mongo`, unused on `sqlite`. Connection options (replica set, TLS, credentials) go in this URL |
| `DB_COLLECTION_PREFIX` | `""` | on `mongo`, a prefix for every collection name, for a database shared with another engine or an application; letters, digits, `_` and `-`. Unused on `sqlite` |

`DB_PATH` is the name `DB_NAME` had when SQLite was the only option and still
works, so a deployment that set it keeps opening the same file.
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
| `SCHEDULER_TICK_S` | `20` | the longest the ticker waits between passes; it wakes sooner for work falling due sooner (see [tasks.md](tasks.md)) |
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
| `RUNTIME_CONFINEMENT` | `conversation` | how far an agent's runtime process is confined with Landlock: `conversation`, `agent` or `off`. A confined process reads its profile and the skill library and writes only its own files, memory and scratch space (`TMPDIR`, `HOME`); everything else — the engine's configuration, other agents, other processes' environments — is out of its reach. At `conversation` "its own" is one conversation's: a process sees neither another conversation's memory and attachments nor its process. At `agent` an agent's conversations share them. At `conversation` a conversation's scratch space is cleared a day after its last process ended, and a scheduled run's when its process ends. Per agent: `runtime.confinement` in `agent.yaml` or `AGENTS_CONFINEMENT__<AGENT>`. The bundled `support` agent is `off` by default. A kernel without Landlock runs every process unconfined, with a warning at startup. |
| `AGENTS_CONFINEMENT__<AGENT>` | — | that agent's confinement level, overriding its profile |
| `RUNTIME_EXTENSIONS` | `""` | which `AGENTS_PATH/_extensions/<name>/index.ts` load into every agent's runtime (CSV of directory names). Nothing loads for merely being present: the directory is writable by any agent with `write`, and an extension is code in every agent's process. The engine logs what is present but not named; `impi doctor` lists both; with agent containers, `impi agent sync` passes the names to each host |
| `RUNTIME_ENV`, `RUNTIME_ENV__<AXIS>` | `""` | names (CSV) of the engine's own environment variables every agent's process inherits beside the base set (`PATH`, `HOME`, locale, proxies, certificate bundles, `PI_*`). Each compose overlay that puts a variable into the engine's environment for the agents declares it under its own `RUNTIME_ENV__` key (`RUNTIME_ENV__BROWSER=BROWSER_CDP_URL`, `RUNTIME_ENV__SECRETS=SECRET_BROKER_*`); nothing else the engine holds reaches a process. A family is `PREFIX_*` with a literal prefix of three characters or more, and may not cover the engine's own (`AGENTS_`, `MATTERMOST_`, `SLACK_`, `TOOL_`, `INTEGRATIONS_`, `HTTP_CALLER_`, `WS_SERVICE_`, `COMMAND_TOKENS`, `DB_`, `LLM_`, `REPLICA_`, `MCP_`) — the engine refuses to start on one that does. The value must be in the engine's process environment (compose `environment:`, a `compose.d/` drop-in) — a line in `conf/.env` does not reach an agent. The bundled `support` agent inherits everything. Every process also gets `PI_OFFLINE=1`: no version check, no download of `rg`/`fd`, no install of a package `settings.json` names |
| `PI_SESSION_DIR` | `""` | `pi` session storage; default `{DATA_DIR}/pi-sessions` (a directory per agent, one per conversation in it) |
| `PI_TIMEOUT` | `180` | per-turn timeout (s) when `agent.yaml` omits `runtime.timeout` |
| `PI_MAX_CONCURRENT_SESSIONS` | `4` | max concurrent `pi` subprocesses |
| `PI_MAX_SESSIONS_PER_AGENT` | `0` | a second bound, per agent; `0` = only the global one |
| `PI_SESSION_IDLE_TTL` | `1800` | reap a `pi` subprocess after this many idle seconds |
| `SESSION_RETENTION_DAYS` | `0` | forget, at startup, every conversation idle for this many days or more — its record and the runtime's files (what `impi sessions purge-idle --days N` does by hand); `0` = never, memory is kept until deleted |
| `PROCESSED_POSTS_RETENTION_DAYS` | `7` | how long the engine remembers having answered a message (so a reconnect replaying it is not answered twice); pruned at startup; `0` = keep forever |
| `PI_EVICT_IDLE_ON_PRESSURE` | `true` | when every slot is held, drop the longest-unused idle subprocess to make room for a new turn instead of making it wait; memory is on disk, so the evicted conversation resumes on its next turn |
| `PI_ACQUIRE_TIMEOUT_S` | `120` | how long a turn waits for a free slot before giving up with "the engine is full" — reached only when every slot is busy, or eviction is off |

## Agent containers

Each agent's runtime in a container of its own — see
[agent-containers.md](agent-containers.md). Off by default; the installer asks,
and `impi agent sync` writes everything below except the first key.

| Variable | Default | Purpose |
|---|---|---|
| `AGENT_HOSTS_ENABLED` | `false` | ask each agent's own host for a runtime instead of forking one |
| `AGENT_HOST_URL` | `http://agent-{agent}:8428` | where a host is; `{agent}` is the name |
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

## Connected accounts

People's own accounts at an OAuth provider, used by tools acting as them — see
[connections.md](connections.md). Off until a provider is configured; then
`CONNECTIONS_KEYS` and `CONNECTIONS_PUBLIC_URL` are required, and the engine
does not start without them. Agents on the ws and http gateways are offered no
connected account whatever these say: their user ids are the calling
program's word. Neither family may be handed to an agent by a pattern in
`runtime.env`, `AGENTS_ENV__*` or `RUNTIME_ENV*`.

| Variable | Default | Purpose |
|---|---|---|
| `CONNECTIONS_KEYS` | `""` | Fernet keys, CSV: the first seals, every one opens. `impi connections key` puts a new one first |
| `CONNECTIONS_PUBLIC_URL` | `""` | where a person's browser reaches the callback listener; each provider is given `<this>/oauth/callback` as its redirect URI. Use `https://` |
| `CONNECTIONS_HOST` | `0.0.0.0` | callback listener bind host |
| `CONNECTIONS_PORT` | `8429` | callback listener port — its own, serving `GET /oauth/callback` and nothing else |
| `CONNECTIONS_COMMAND` | `connections` | the command that opens the panel (a direct message only) |

### Per-provider keys (dynamic)

One family per field, the provider's name last; the name is the suffix
lower-cased, `_` read as `-` (`__SELF_HOSTED` is `self-hosted`). Environment
over `.env`, like every other key.

| Key | Default | Purpose |
|---|---|---|
| `CONNECTION_CLIENT_ID__<NAME>` | — | the OAuth application's ID; its presence configures the provider |
| `CONNECTION_CLIENT_SECRET__<NAME>` | — | required |
| `CONNECTION_KIND__<NAME>` | `gitlab` | `gitlab` or `oauth2` |
| `CONNECTION_BASE_URL__<NAME>` | — | GitLab's address; required for `gitlab` |
| `CONNECTION_AUTHORIZE_URL__<NAME>`, `CONNECTION_TOKEN_URL__<NAME>`, `CONNECTION_USERINFO_URL__<NAME>` | — | required for `oauth2` |
| `CONNECTION_REVOKE_URL__<NAME>` | `""` | `oauth2`: RFC 7009 revocation, when the provider has it |
| `CONNECTION_SCOPE__<NAME>` | gitlab: `read_api` | scopes asked for at consent |
| `CONNECTION_API_URL__<NAME>` | gitlab: `<base>/api/v4` | where the tools call; a path never leaves it |
| `CONNECTION_MATCH_EMAIL__<NAME>` | `true` | the account's email must be the chat profile's |
| `CONNECTION_AGENTS__<NAME>` | unset = every agent | CSV of agents that may use it; empty = none |

## MCP servers

Declared as files under `$AGENTS_PATH/_mcp/` (`impi mcp add`, see
[mcp.md](mcp.md)), not as settings. Their secrets are: a `${NAME}` in a
server's headers or environment is read from the engine's process environment
when the server is connected; name them in the `MCP_` family
(`MCP_GITHUB_TOKEN`), which is the engine's own and never reaches an agent's
process. `${connection:<provider>}` in a header is the token of the person
whose turn it is ([connections.md](connections.md)).

| Variable | Default | Purpose |
|---|---|---|
| `MCP_<ANYTHING>` | — | whatever a server entry's `${…}` names; by convention `MCP_<SERVER>_TOKEN` |

## Interactivity (widget/form callbacks)

| Variable | Default | Purpose |
|---|---|---|
| `INTEGRATIONS_ENABLED` | `true` | master switch for widgets/forms |
| `INTEGRATIONS_HOST` | `0.0.0.0` | receiver bind host |
| `INTEGRATIONS_PORT` | `8423` | receiver port |
| `INTEGRATIONS_PUBLIC_URL` | `""` | URL a containerized Mattermost calls back to; default `http://host.containers.internal:{port}`; `auto` = detect the host LAN IP at startup |
| `INTEGRATIONS_SECRET` | minted on first start | a segment of every callback path (`<public url>/<secret>/interact`, `/dialog`, `/command/<agent>`): the receiver answers 403 off it, so only the Mattermost that was handed the URL can click, submit or run a command. At least 16 of `[A-Za-z0-9_-]`. Changing it means re-registering every slash command — see [commands.md](commands.md) |
| `INTEGRATIONS_UI_TIMEOUT` | `90` | seconds to await a human on a blocking confirm/select (and on a tool's confirmation card) before default-reject. Must stay below 270: the runtime's tool extension gives up on a call after 300 s, and an answer arriving later would run a call the model was already told had failed |
| `TOOL_TRACE_ENABLED` | `true` | the message listing the tools a turn ran — in the thread, or in a thread under the message the turn answered — see [tool-trace.md](tool-trace.md); needs interactivity |
| `TOOL_TRACE_RETENTION_DAYS` | `14` | drop traces older than this at startup; `0` keeps them all |

Commands (slash commands) reach the same receiver at
`POST {INTEGRATIONS_PUBLIC_URL}/{INTEGRATIONS_SECRET}/command/<agent>` — register that URL with the
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
| `WARD_UNSEAL_KEY_FILE` / `WARD_SECRET_ID_FILE` | — | unattended unlock, mounted as files; the broker waits up to two minutes for the store before using them |
| `WARD_GATEWAY` | `mattermost` | which chat platform the broker's account lives on: `mattermost` or `slack` |
| `WARD_MATTERMOST_URL` | `http://mattermost:8065` | Mattermost only: the server |
| `WARD_MATTERMOST_TOKEN` | — | Mattermost only: the bot approval cards are posted as |
| `WARD_SLACK_BOT_TOKEN` / `WARD_SLACK_APP_TOKEN` | — | Slack only: the app the cards are posted as, and the app-level token its socket is opened with |
| `WARD_APPROVERS` | — | CSV of usernames or ids that may answer |
| `WARD_APPROVAL_CHANNEL` | — | where cards go (empty = a DM to the first approver) |
| `WARD_APPROVAL_TIMEOUT_S` | `120` | how long a request waits before it is refused |
| `WARD_MAX_GRANT_S` | `3600` | ceiling over every policy's own window ceiling |
| `WARD_NOTICE_FOLD_S` | `900` | how long a run of automatic grants folds into one notice rather than posting again |
| `WARD_COMMAND_TOKENS` | — | Mattermost only: the `/ward` slash command's tokens (CSV). Empty = no operator surface in chat. Operator-grade: anything reaching the receiver with one of these can claim to be any user, and only the approver check that follows decides. On Slack the command is the app's own and needs no token |
| `WARD_CALLBACK_PUBLIC_URL` | `http://ward:8426` | Mattermost only: where its clicks, dialog submissions and the slash command are posted back to |
| `WARD_CALLBACK_SECRET` | written by the installer | Mattermost only: the segment under which the broker's receiver answers (`http://ward:8426/<secret>/…`); off it, 403. The broker cannot write its own env file, so `impi start` mints a missing one and says so. Same alphabet and length as `INTEGRATIONS_SECRET` |
| `WARD_OPERATOR_DIR` | `/var/lib/ward/operator` | where `ward init` puts the operator's identity — a different directory from the agents', because that one is mounted where the agents run |

## Tool confirmations

A tool that declares `requires_confirmation` is asked about before it runs — by
the engine, inside the tool server, so a call that skips the runtime and reaches
the server directly is gated all the same. The question is asked once, as a card
in the conversation, and the time a person takes to answer it does not count
against the turn's timeout (`PI_TIMEOUT` / `runtime.timeout`) — that timeout is
for a stuck runtime, not a slow human. It needs interactivity
(`INTEGRATIONS_ENABLED=true`): with
it off there is nobody to ask, and such a tool is left out of the agent's tool
list rather than offered and refused.

A call that is not allowed does not run, and the model is told which of three
things happened, because they call for different next steps: a person said
no (`declined by the user`); nobody answered before the card expired
(`INTEGRATIONS_UI_TIMEOUT`) — not a refusal, so the agent is told to ask the
person for their approval when they are back, or to go on with what needs
none, rather than retry; or there was nobody to ask. A scheduled turn with
nobody at the screen is the usual case of the second.

| Variable | Default | Purpose |
|---|---|---|
| `TOOL_APPROVERS` | `""` | who may answer a tool's confirmation card (CSV of platform user ids). Empty = the person whose message the turn answers: in a burst merged into one turn the newest one's author, for a scheduled task the person who scheduled it; a conversation that names no person is answered by anyone in it. One set for every agent and gateway — name ids from each platform in use (an application with a `PersonKey` names persons). A click from anyone else is told so privately and the card stays live |
| `TOOL_MAX_GRANT_S` | `900` | longest window a human may open for a gated tool (`0` = ask every time). A window covers that tool in the conversation where it was opened — another thread or direct message with the same agent is asked afresh |

## Logging

| Variable | Default | Purpose |
|---|---|---|
| `LOG_LEVEL` | `INFO` | root log level |
