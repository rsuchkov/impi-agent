# Creating agents

An agent is three things: a **profile** (a directory of config), a **bot account**
on a chat platform, and its **token** in the engine's `.env`. You write the
profile; the operator provisions the bot and applies the change.

Profiles live under `AGENTS_PATH` (see [configuration.md](configuration.md)). The
engine's own `support` agent has an `agent-builder` skill that automates the steps
below — this document is its human-facing twin.

## Profile layout

```
$AGENTS_PATH/agents/<name>/
  agent.yaml                    # machine config (schema below); `name` MUST equal <name>
  .pi/SYSTEM.md                 # personality / instructions (any language)
  .pi/skills/<skill>/SKILL.md   # optional per-agent skills (listed in runtime.skills)

$AGENTS_PATH/_extensions/<name>/index.ts   # optional shared tools; loaded for every agent once named in RUNTIME_EXTENSIONS
```

## `agent.yaml`

```yaml
name: <name>              # MUST equal the directory name
display_name: <Display Name>
role: <short-role>
description: <one line>
runtime:
  provider: openai-codex  # OPTIONAL — omit to inherit the engine default (DEFAULT_PROVIDER)
  model: gpt-6.1-sol      # OPTIONAL — omit for the default; agents may run different models.
                          # A provider needs a model, here or in DEFAULT_MODEL
  fallback_models: [other-provider/other-model]  # optional; tried in order when the model is down
  timeout: 180            # seconds per turn (active time: waiting on a person does not count)
  idle_timeout: 120       # optional; seconds without a sign of life from the model — no text,
                          # no reasoning, no tool activity — before the turn is called stalled and
                          # stopped; not counted while a tool runs. 0 = never
  wrap_up_after: 0        # optional; active seconds after which the model is told, once, to stop
                          # calling tools and say what is done and what is left — so a long turn
                          # rounds off before `timeout` cuts it. 0 = never; must be below timeout
  thinking: medium        # optional; how much the model reasons before it answers:
                          # off | minimal | low | medium | high | xhigh | max (the runtime's levels;
                          # omit for its default). An answer that comes back cut short, or not at
                          # all, after long reasoning is the sign to lower it.
  tools: [read, bash]     # the single capability allowlist (see "Tool gating")
  tools_by_gateway:       # optional; a gateway named here gets only these of `tools`
    slack: [read]         #   (a subset); one not named gets them all
  skills: [<name>, ...]   # optional; bare names -> .pi/skills/<name>, or paths
  env: [MYSERVICE_*]      # optional; engine environment variables its process inherits
  confinement: agent      # optional; conversation | agent | off (default: RUNTIME_CONFINEMENT)
  confinement_read: [/srv/checkout]   # optional; more paths its process may read
  confinement_write: [state]          # optional; more it may write (relative = in the profile)
  packages:               # OPTIONAL, and only with agent containers (see below)
    apt: [ffmpeg]
```

Only `name` and `role` are required. Leave `provider`/`model` out unless the agent
needs a different backend or model than the engine default — different agents can
run different models. See [runtime-notes.md](runtime-notes.md) for how
provider/model are resolved.

`fallback_models` lists models, as `provider/model`, that a turn moves on to
when the agent's own model fails the way a service fails: overloaded,
rate-limited (429), 5xx, a connection that never completes. The turn goes on
there without anything being asked again, and the next message starts on the
agent's own model. A spent quota or a refused key is not such a failure, and
still ends the turn. Each model needs a login or a definition the runtime has
where the agent runs (`impi doctor` — `impi health` in a source checkout —
names one that does not). The deployment can set one list for every agent
(`DEFAULT_FALLBACK_MODELS`); `AGENTS_FALLBACK_MODELS__<AGENT>` overrides an agent.

`.pi/SYSTEM.md` is the agent's true personality — who it is, its scope, its house
rules. Write it in whatever language the agent should think in. `pi` loads it
natively (the agent's profile dir is `pi`'s working directory).

`runtime.packages` is read only by a deployment that gives each agent [a
container of its own](agent-containers.md), where it becomes that agent's image.
It is ignored otherwise — with one shared container there is no per-agent image
to put a package in. The same goes for a `Dockerfile.include` beside
`agent.yaml`, which is how an agent asks for something a package list cannot say.

## Tool gating

`runtime.tools` is the **one allowlist** over three kinds of tools at once:

- `pi`'s built-ins: `read`, `bash`, `edit`, `write`, `grep`, `find`, `ls`.
- tools registered by an extension (`_extensions/<name>/index.ts`) the
  deployment enabled: an extension is code in every agent's process, so being
  present in the directory is not enough — `RUNTIME_EXTENSIONS=<name>,…` in
  `.env` names what loads, and the engine logs what is present but not named.
  `impi doctor` lists both.
- the engine's typed tools (e.g. the multi-agent choreography tools).
- the tools of an MCP server the deployment declared (`impi mcp add`, see
  [mcp.md](mcp.md)): `mcp__<server>__<tool>`, or every pinned tool of a server
  as `mcp__<server>__*` — the one pattern the list accepts.

Naming a tool is the only way to enable it; an **empty list means the agent gets
no tools at all**.

An agent on several gateways may be allowed less on one of them:
`runtime.tools_by_gateway` maps a gateway kind (`mattermost`, `slack`, `ws`,
`http`) to the names of `tools` a conversation there may use — a subset,
checked when the profile loads; a gateway not named gets the agent's whole
list, `[]` gives a gateway no tool at all. A conversation on such a gateway
runs with a profile narrowed to that list — the runtime's allowlist, the
manifest the process advertises and the tool server's door all agree — and a
tool outside it is refused there whatever the process claims. The engine does
this through the same entitlement mechanism an application uses for its own
rules ([building-an-app.md](building-an-app.md)).

Two rules to remember:

- **Skills need `read` + `bash`** to function (the agent reads `SKILL.md` and runs
  its scripts via `bash`).
- A typed tool is **dropped** (and logged) if none of the agent's gateways or
  its config provides a capability it requires — e.g. widget tools when
  interactivity is disabled, or `send_ephemeral` on a gateway that can't post
  ephemeral messages. An agent on several gateways keeps a tool one of them can
  serve; a call from a gateway without the capability is refused with
  `not available through this gateway`.
  Capabilities per gateway: Mattermost and Slack provide channel administration
  and ephemeral messages; the ws and http gateways provide neither. Widgets
  depend on interactivity being enabled; on ws they degrade to text, and the
  http gateway withholds the fire-and-forget ones and forms altogether (a
  program polling a journal cannot follow the turn a click would start — only
  what blocks the turn, a confirmation, is answered there). So a tool listed by
  an agent whose gateway lacks the capability simply isn't advertised to it.
  Tools that need confirming (`requires_confirmation`) are withheld when
  interactivity is off: there is nobody to ask.
  - **`read_channel` and `get_channel_members` return other people's text**,
    wrapped as `{"untrusted": true, "note": …, "data": …}` with one standard
    sentence telling the model it is information, not instructions. Your
    own tool declares `returns_untrusted = True` for the same treatment.
  - **`send_ephemeral`** posts a message only one user sees (the turn's user by
    default, or a given `@username`). **Mattermost gates this behind the
    `create_post_ephemeral` permission**, which a bot account lacks by default —
    grant it to the bot's role (e.g. `mmctl permissions add system_user
    create_post_ephemeral`, or via the System Console) or the tool returns a
    permission error. Slack bots can post ephemeral messages out of the box.
- **Built-ins work relative to the profile directory** (the runtime's cwd) — so
  relative state like a skill's `state/*.json` lands there. To read data that
  lives elsewhere (a mounted repo, a docs folder), give the agent the **absolute
  path** in its SYSTEM.md, or wrap access in your own typed tool with a pinned
  root. See [runtime-notes.md](runtime-notes.md).
- **An agent that receives files needs `read` (and usually `bash`)**: a picture
  is shown to the model on its own, but a PDF or an archive arrives as an
  absolute path it has to open itself. See [files.md](files.md).
  - **`schedule_task`** / `list_tasks` / `pause_task` / `cancel_task` schedule
    work for later in this conversation — a reminder, a daily digest. Creating
    one answers with the next few fire times; read them back to the person. See
    [tasks.md](tasks.md).
  - **`send_file`** posts a file back into the conversation. It reads only from
    the agent's own profile directory, its attachment directory and `/tmp`, so
    an agent that generates something must write it in one of those first.

## Asking with controls (widgets and forms)

Four tools let an agent ask with real controls instead of prose. Each **declares
that it speaks to the user**: what it posts IS the agent's message, so the turn
can end on the call, and the answer arrives later as a new message.

- **`ask_user_buttons`** — 2–5 buttons.
- **`ask_user_select`** — a dropdown of 2–20 options, or, with
  `source: users` / `source: channels`, a picker fed by the workspace itself.
- **`open_form`** — a button that opens a modal collecting up to 15 fields at
  once. Its wording is the agent's to choose (`open_label`, e.g. "Report a bug";
  the default is "📝 Fill in…"). The button survives a closed modal (so it can be
  reopened) and is struck off its message once the form is submitted — the same
  on both platforms.
- **`open_screen`** — one of the engine's own panels (the same one its slash
  command opens). The engine renders it and answers every click, so what the
  user sees is always live.

The agent is told this once, in wording the engine generates from the
declaration rather than each tool describing it in its own words — which is what
let one of them go untold and answer with the widget *and* a message repeating
it. Two things follow, and they are worth knowing when writing a `SYSTEM.md`:

- **Say it in the tool's own text.** What the person reads is `open_form`'s
  `intro` and the `prompt` of `ask_user_buttons` / `ask_user_select`
  (`open_screen` has none — the engine renders the panel). A turn's own reply is
  delivered when the turn ends — that is, *after* the message the tool posted —
  so a lead-in written before the call lands underneath the thing it announces.
- **A message afterwards is allowed, and is the agent's call.** It should add
  what the posted message does not say. The engine posts whatever the agent
  writes; nothing is dropped.

Separately from what an agent asks, the engine itself posts a message for
every turn that ran tools — in a thread, above the reply; in a direct message
or a channel, in a thread under the message the turn answers — a live counter
that settles into a button listing the calls. Nothing in a profile turns it on; see
[tool-trace.md](tool-trace.md).

### Form field types

One neutral vocabulary; each platform renders its own control. Nothing in an
agent's profile mentions Mattermost or Slack.

`password` masks the characters as they are typed, where the platform can. That
is all it does: the value still travels through the chat platform like any
other, so the field is about the person behind you, not about secrecy.

| `type` | Mattermost | Slack | The agent gets back |
|---|---|---|---|
| `text` / `textarea` | text / textarea | plain-text input (multiline for textarea) | the text |
| `number`, `email`, `url`, `tel` | text with that subtype | number / email / URL input (`tel` → plain text) | the text |
| `password` | text, masked while typing | *(no masked input — plain text)* | the text |
| `select` | dropdown | static select | the chosen option |
| `multiselect` | multi-select dropdown | multi static select | `a, b` |
| `radio` | radio group | radio buttons | the chosen option |
| `bool` | checkbox | one checkbox | `yes` / `no` |
| `user` / `users` | people picker (single / multi) | users select (single / multi) | `@name (id)` |
| `channel` / `channels` | channel picker (single / multi) | channels select (single / multi) | `~name (id)` |
| `date` | date picker | date picker | `YYYY-MM-DD` |
| `datetime` | datetime picker | datetime picker | ISO 8601 |
| `time` | *(no picker — a text field hinting HH:MM)* | time picker | `HH:MM` |
| `label` | folded into the modal's intro text | a text block | nothing — it's static text |

`select`, `multiselect` and `radio` require `options`; the pickers must not
carry any (the platform supplies them). Every field takes `optional`,
`placeholder` and `help_text` (a hint under the control).

Picker answers arrive as **name and id together** — the name is what the model
reasons about, the id is what it passes to another tool afterwards. If a lookup
fails, the raw id is shown rather than nothing.

**Mattermost server floors:** `multiselect` needs 11.0, `date`/`datetime` need
11.1 (the installer's co-deployed server is newer). On an older server the modal
refuses to open and the engine logs the field types it tried to use.

**Widgets in a message** are single-value by nature: buttons, a dropdown, or a
user/channel picker. Multi-select exists only inside a form — Mattermost's
message attachments have no such control.

## Skills

A skill is a `SKILL.md` capability package the agent reads on demand. Three ways
to name one in `runtime.skills`:

| Reference | Resolves to | Use for |
|---|---|---|
| `vocabulary-trainer` | `<profile>/.pi/skills/<name>` | a skill only this agent has |
| `registry:greek-tutor` | the shared library (`SKILLS_PATH`) | a skill several agents share, or one installed from a repository |
| `../shared/thing` | a path, relative to the profile | anything else on disk |

The library is what `impi skill` and `/skills` manage — installing, updating and
handing skills to agents. See **[skills.md](skills.md)**. To author one, see the
`support` agent's `skill-authoring` skill; the format is the
[Agent Skills](https://agentskills.io) standard (a `SKILL.md` with a `name` and
`description` in front matter, plus optional `scripts/`) — the same one Claude
Code and Hermes use, so their skills work here unchanged.

Skills are **toggleable per agent without editing `agent.yaml`** via the
`AGENTS_SKILLS__<AGENT>` environment variable (CSV of names): a set value replaces
the profile's list, empty disables all, unset keeps the profile's list. This is
how you turn a bundled agent's skills on or off.

## What the agent's process inherits

An agent's runtime is started with the environment the engine **grants** it,
not the environment the engine happens to hold — the difference is every chat
token and caller token in `conf/.env`. Three sources, in order:

- a base set every process gets: `PATH`, `HOME`, the user, locale and time
  zone, the proxy variables, the certificate bundles (`SSL_CERT_FILE`,
  `NODE_EXTRA_CA_CERTS`) and the runtime's own `PI_*` knobs;
- what the deployment names for every agent: `RUNTIME_ENV` (CSV) and every
  `RUNTIME_ENV__<AXIS>` key in the engine's environment — the browser overlay
  declares `BROWSER_CDP_URL` that way, the secret-store overlay
  `SECRET_BROKER_*`;
- what the profile names in `runtime.env` (a list of names, `*` allowed),
  replaceable from `.env` with `AGENTS_ENV__<AGENT>` exactly as
  `AGENTS_SKILLS__<AGENT>` replaces skills.

On top of that come the engine's own grants: the tool server's address and
token, the session id and its proof, `AGENT_NAME`, `AGENT_FILES_DIR`, and the
model settings (`LLM_*`) when a custom endpoint is configured. A skill that
reads a variable of its own from the engine's environment names it in
`runtime.env`, or the operator names it once in `RUNTIME_ENV`; otherwise the
skill sees it unset. A name or a family `PREFIX_*` (a literal prefix of three
characters or more, never one of the engine's own families such as `AGENTS_`,
`TOOL_` or `LLM_`). The value has to be in the engine's process environment —
a compose `environment:` entry or a `compose.d/` drop-in; a line in
`conf/.env` does not reach the agent. The bundled `support` agent is part of the engine and
inherits everything, because the engine's own CLI it runs needs the engine's
settings.

## Choosing a gateway

By default an agent runs on `GATEWAY` (Mattermost). Override per agent with
`AGENTS_GATEWAY__<AGENT>=slack`, `=ws` or `=http` — or several at once,
`=mattermost,slack` (the first is the default; see
[configuration.md](configuration.md), *One agent, several gateways*). A Slack agent needs Slack
tokens instead of a Mattermost token (see [configuration.md](configuration.md));
the engine appends Slack formatting rules to that agent's system prompt
automatically. A `ws` agent talks to your own programs over the engine's
WebSocket hub, an `http` agent over a request/turn API for programs that cannot
hold a socket — neither has a per-agent token; access is authorized by the
callers' own tokens (see [ws-gateway.md](ws-gateway.md),
[http-gateway.md](http-gateway.md)). Kinds mix freely in one engine.

## Commands (slash commands / shortcuts)

An agent can also be invoked by a command — `/summarize` in a Mattermost thread,
or a `crux_*` message shortcut in Slack. It arrives as an ordinary message and
is answered in that conversation, so nothing needs enabling in `agent.yaml`:
just say what the command means in `.pi/SYSTEM.md` and register it on the
platform. (For a command whose result must stay private or be produced
deterministically, handle it beside the agent instead.) Full guide:
[commands.md](commands.md).

## Provisioning and applying

**Shortcut:** `impi agent add` (the CLI; in a deployment: the `impi agent add`
wrapper command) does all of the below in one go — with a Mattermost
system-admin token (`TOOL_CREATE_AGENT_ADMIN_TOKEN`) it creates the bot account
itself; without one it asks for a manually created bot token. In chat, the
bundled `support` agent can do the same via its `create_agent` tool. The manual
steps:

1. **Create the bot account** on the platform and copy its token:
   - Mattermost: System Console → Integrations → Bot Accounts.
   - Slack: create an app with Socket Mode + a bot user. For the bot to be an
     agent in Slack's Agents panel — a working indicator and a Stop button
     while it works — the manifest needs `features.agent_view`, the scopes
     `assistant:write` and `chat:write`, and the events `message.im`,
     `app_home_opened`, `app_context_changed`, `agent_session_stopped` and
     `agent_session_title_changed` (`reaction_added` too, for reactions as
     commands). The engine takes all of them; the Stop button ends the turn
     as `/stop` does ([commands.md](commands.md#stopping-a-turn)).
2. **Add the token** to the engine `.env`, keyed by the upper-cased agent name
   (`-`→`_`):
   - Mattermost: `AGENTS_MM_TOKEN__<NAME>`.
   - Slack: `AGENTS_SLACK_BOT_TOKEN__<NAME>` + `AGENTS_SLACK_APP_TOKEN__<NAME>`
     and `AGENTS_GATEWAY__<NAME>=slack`.
3. **Apply:**
   - A **new** agent appears only after an engine **restart** (agents are
     enumerated at startup).
   - **Editing** an existing agent (`agent.yaml`, `SYSTEM.md`, skills) applies with
     a **reload** — `make reload` (re-reads profiles, drops idle sessions;
     conversation memory survives).
   - To try a prompt or a skill from a terminal, without the chat round-trip:
     `impi escape <agent>` starts the same profile on the host — the engine's
     own tools excepted (see [installation.md](installation.md#impi-escape-an-agents-pi-in-your-terminal)).

An agent is present only if its token is set; a tokenless profile is skipped.

## Engine-owned vs. user agents

- **User agents** live under `AGENTS_PATH` — yours to create and edit.
- **Engine-owned agents** ship with `impi` (bundled in the package) and are
  privileged. The `support` agent is the first: it builds/maintains other agents
  and helps diagnose the engine. It is present only if `AGENTS_MM_TOKEN__SUPPORT`
  is set, and its provider/model come from `SUPPORT_PROVIDER`/`SUPPORT_MODEL`
  (falling back to the engine default). Because its `agent.yaml` lives inside the
  package, toggle its skills with `AGENTS_SKILLS__SUPPORT` rather than editing it.
