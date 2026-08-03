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

$AGENTS_PATH/_extensions/<name>/index.ts   # optional shared tools, loaded for every agent
```

## `agent.yaml`

```yaml
name: <name>              # MUST equal the directory name
display_name: <Display Name>
role: <short-role>
description: <one line>
runtime:
  provider: openai-codex  # OPTIONAL — omit to inherit the engine default (DEFAULT_PROVIDER)
  model: gpt-5.5          # OPTIONAL — omit for the default; agents may run different models
  timeout: 180            # seconds per turn
  tools: [read, bash]     # the single capability allowlist (see "Tool gating")
  skills: [<name>, ...]   # optional; bare names -> .pi/skills/<name>, or paths
```

Only `name` and `role` are required. Leave `provider`/`model` out unless the agent
needs a different backend or model than the engine default — different agents can
run different models. See [runtime-notes.md](runtime-notes.md) for how
provider/model are resolved.

`.pi/SYSTEM.md` is the agent's true personality — who it is, its scope, its house
rules. Write it in whatever language the agent should think in. `pi` loads it
natively (the agent's profile dir is `pi`'s working directory).

## Tool gating

`runtime.tools` is the **one allowlist** over three kinds of tools at once:

- `pi`'s built-ins: `read`, `bash`, `edit`, `write`, `grep`, `find`, `ls`.
- tools registered by an extension (`_extensions/<name>/index.ts`).
- the engine's typed tools (e.g. the multi-agent choreography tools).

Naming a tool is the only way to enable it; an **empty list means the agent gets
no tools at all**. Two rules to remember:

- **Skills need `read` + `bash`** to function (the agent reads `SKILL.md` and runs
  its scripts via `bash`).
- A typed tool is **dropped** (and logged) if the agent's gateway/config doesn't
  provide a capability it requires — e.g. channel-admin tools on a Slack agent, or
  widget tools when interactivity is disabled.
- **Built-ins work relative to the profile directory** (the runtime's cwd) — so
  relative state like a skill's `state/*.json` lands there. To read data that
  lives elsewhere (a mounted repo, a docs folder), give the agent the **absolute
  path** in its SYSTEM.md, or wrap access in your own typed tool with a pinned
  root. See [runtime-notes.md](runtime-notes.md).

## Skills

A skill is a `SKILL.md` capability package the agent reads on demand. List a
skill in `runtime.skills` by **bare name** (resolved to `.pi/skills/<name>`) or by
path. To author one, see the `support` agent's `skill-authoring` skill; the format
is the [Agent Skills](https://agentskills.io) standard (a `SKILL.md` with a `name`
and `description` in front matter, plus optional `scripts/`).

Skills are **toggleable per agent without editing `agent.yaml`** via the
`AGENTS_SKILLS__<AGENT>` environment variable (CSV of names): a set value replaces
the profile's list, empty disables all, unset keeps the profile's list. This is
how you turn a bundled agent's skills on or off.

## Choosing a gateway

By default an agent runs on `GATEWAY` (Mattermost). Override per agent with
`AGENTS_GATEWAY__<AGENT>=slack` or `=ws`. A Slack agent needs Slack tokens
instead of a Mattermost token (see [configuration.md](configuration.md)); the
engine appends Slack formatting rules to that agent's system prompt
automatically. A `ws` agent talks to your own programs over the engine's
WebSocket hub — no per-agent token; access is authorized by service tokens
(see [ws-gateway.md](ws-gateway.md)). Kinds mix freely in one engine.

## Provisioning and applying

**Shortcut:** `impi agent add` (the CLI; in a deployment: the `impi agent add`
wrapper command) does all of the below in one go — with a Mattermost
system-admin token (`TOOL_CREATE_AGENT_ADMIN_TOKEN`) it creates the bot account
itself; without one it asks for a manually created bot token. In chat, the
bundled `support` agent can do the same via its `create_agent` tool. The manual
steps:

1. **Create the bot account** on the platform and copy its token:
   - Mattermost: System Console → Integrations → Bot Accounts.
   - Slack: create an app with Socket Mode + a bot user.
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

An agent is present only if its token is set; a tokenless profile is skipped.

## Engine-owned vs. user agents

- **User agents** live under `AGENTS_PATH` — yours to create and edit.
- **Engine-owned agents** ship with `impi` (bundled in the package) and are
  privileged. The `support` agent is the first: it builds/maintains other agents
  and helps diagnose the engine. It is present only if `AGENTS_MM_TOKEN__SUPPORT`
  is set, and its provider/model come from `SUPPORT_PROVIDER`/`SUPPORT_MODEL`
  (falling back to the engine default). Because its `agent.yaml` lives inside the
  package, toggle its skills with `AGENTS_SKILLS__SUPPORT` rather than editing it.
