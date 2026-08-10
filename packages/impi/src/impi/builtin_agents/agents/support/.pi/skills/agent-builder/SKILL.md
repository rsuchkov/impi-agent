---
name: agent-builder
description: Scaffold, wire, and reload an impi agent under $AGENTS_PATH. Use when the operator asks to create a new agent, add an agent, or change an existing agent's profile (agent.yaml, SYSTEM.md, tools, skills, provider/model).
---

# Building an impi agent

An agent is a directory under `$AGENTS_PATH/agents/<name>/` plus a chat-platform
bot account and its token in the engine `.env`.

**Preferred path (Mattermost):** call the `create_agent` tool — it provisions the
bot account, scaffolds the profile, and stores the token in one confirmed step
(see section 3). Then refine `.pi/SYSTEM.md` and `agent.yaml` with the file
tools. Fall back to the manual flow below only when `create_agent` is missing
from your tools or reports that no admin token is configured.

Always use **absolute paths** and only write under `$AGENTS_PATH`.

The human reference is `$IMPI_ROOT/docs/creating-agents.md` (and
`configuration.md` for the `.env` keys) — read it when you need more than this
skill gives.

## 1. Create the profile

```
$AGENTS_PATH/agents/<name>/
  agent.yaml                 # machine config (below); `name` MUST equal <name>
  .pi/SYSTEM.md              # personality / instructions (any language)
  .pi/skills/<skill>/        # optional private skills (see skill-authoring)
```

`agent.yaml`:

```yaml
name: <name>                 # MUST equal the directory name
display_name: <Display Name>
role: <short-role>
description: <one line>
runtime:
  provider: openai-codex     # optional; omit to inherit the engine default
  model: gpt-5.5             # optional; omit to inherit the engine default
  timeout: 180               # seconds per turn
  tools: [read, bash]        # the ONE allowlist (see "Tool gating")
  skills: [<name>, registry:<name>]   # private, and/or from the shared library
```

Keep `.pi/SYSTEM.md` short and concrete: who the agent is, its scope, and its
house rules. It is the agent's true personality — write it in whatever language
the agent should think in.

## 2. Tool gating

`runtime.tools` is the ONE allowlist over pi's built-ins (`read`, `bash`, `edit`,
`write`, `grep`, `find`, `ls`), the agent's extension tools, and the engine's
typed tools. Naming a tool is the only way to enable it; an empty list = no tools.
**Skills need `read` + `bash`** to function (the model reads `SKILL.md` and runs
its scripts).

The engine silently drops a typed tool whose capability the agent's setup lacks,
and logs why. Common cases:

| Tool | Needs |
|---|---|
| `create_channel`, `invite_to_channel`, `read_channel`, `send_message`, `get_channel_members` | a gateway with an admin client (Mattermost) |
| `ask_user_buttons`, `ask_user_select`, `open_form`, `open_screen` | `INTEGRATIONS_ENABLED=true` |
| `send_file` | `ATTACHMENTS_ENABLED=true` |
| `send_ephemeral` | a gateway that has ephemeral messages |
| `schedule_task`, `list_tasks`, `cancel_task`, `pause_task` | `SCHEDULER_ENABLED=true` |

If an agent says a tool is missing, check the startup log for
`tool … not advertised` before editing anything.

## 3. Provision the bot + apply

A **new** agent only appears after an engine **restart** (agents are enumerated
at startup).

**With the `create_agent` tool (Mattermost, preferred):** call it with `name`,
`role`, and optionally `display_name`, `description`, `system_prompt`. It creates
the bot account, writes the profile skeleton, and stores the token — the operator
confirms via a button before it runs. Afterwards edit the generated files as
needed and ask the operator to **restart**. If the tool reports a missing admin
token, ask the operator to set `TOOL_CREATE_AGENT_ADMIN_TOKEN` in the engine
`.env` — or use the manual flow.

**Manual flow** (Slack, or no admin token): write the profile files yourself,
then tell the operator to:

1. Create a bot account on the agent's gateway (a Mattermost bot, or a Slack app
   for `GATEWAY=slack`).
2. Put its token in the engine `.env`:
   - Mattermost: `AGENTS_MM_TOKEN__<NAME>` (upper-case, `-`→`_`).
   - Slack: `AGENTS_SLACK_BOT_TOKEN__<NAME>` + `AGENTS_SLACK_APP_TOKEN__<NAME>`,
     and `AGENTS_GATEWAY__<NAME>=slack`.
   - ws (the operator's own programs, no chat platform): `AGENTS_GATEWAY__<NAME>=ws`
     and a service token — see `$IMPI_ROOT/docs/ws-gateway.md`.
3. **Restart** the engine.

An agent with no token is skipped at startup and logged — that, not a broken
profile, is the usual reason a new agent "doesn't exist".

## 4. Applying a change to an existing agent

Editing `agent.yaml`, `SYSTEM.md`, skills or extensions needs only a **reload**:
profiles are re-read and idle sessions dropped; conversation memory survives.

- The operator, on the host: `impi reload` (deployment) or `make reload` (checkout).
- You, from inside the engine: `pkill -HUP -n -f '[i]mpi\.main'`.

A conversation already in flight keeps its current configuration until its
session resets. A **new** agent still needs a restart.

## 5. Toggles the operator has

- `AGENTS_ENABLED` (CSV) — which agents run; empty = all found.
- `AGENTS_SKILLS__<NAME>` (CSV) — replaces an agent's whole skill list without
  editing `agent.yaml` (empty = no skills; unset = the agent.yaml list). While it
  is set, `assign_skill` changes won't show up.
- `AGENTS_GATEWAY__<NAME>` — that agent's gateway.
- Shared extensions live at `$AGENTS_PATH/_extensions/<name>/index.ts` (loaded for
  every agent); private skills live in that agent's `.pi/skills/`.

## 6. After the agent exists

- A skill from the shared library: the **skill-library** skill (`assign_skill`).
- A slash command of its own: the **chat-commands** skill — the command's URL
  and token are what bind it to this agent.
- Recurring work: the **scheduled-tasks** skill.

When you finish, briefly tell the operator what you created or changed and the
exact step to apply it (restart vs reload, and which `.env` keys to set).
