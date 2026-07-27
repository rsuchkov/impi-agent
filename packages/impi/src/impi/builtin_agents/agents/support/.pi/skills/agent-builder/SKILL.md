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

The human reference for this is `$IMPI_ROOT/docs/creating-agents.md` (and
`configuration.md` for the `.env` keys) — read it if you need more detail than this
skill gives.

## 1. Create the profile

```
$AGENTS_PATH/agents/<name>/
  agent.yaml                 # machine config (below); `name` MUST equal <name>
  .pi/SYSTEM.md              # personality / instructions (any language)
  .pi/skills/<skill>/        # optional per-agent skills (see the skill-authoring skill)
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
  skills: [<name>, ...]      # optional; bare names -> .pi/skills/<name>, or paths
```

Keep `.pi/SYSTEM.md` short and concrete: who the agent is, its scope, and its
house rules. It is the agent's true personality — write it in whatever language
the agent should think in.

## 2. Tool gating

`runtime.tools` is the ONE allowlist over pi's built-ins (`read`, `bash`, `edit`,
`write`, `grep`, `find`, `ls`), the agent's extension tools, and the engine's
typed tools. Naming a tool is the only way to enable it; an empty list = no tools.
**Skills need `read` + `bash`** to function (the model reads `SKILL.md` and runs
its scripts). The engine drops any typed tool whose capability the agent's gateway
lacks (e.g. channel-admin tools on a Slack agent) and logs it.

## 3. Provision the bot + apply

A **new** agent only appears after an engine **restart** (agents are enumerated at
startup).

**With the `create_agent` tool (Mattermost, preferred):** call it with `name`,
`role`, and optionally `display_name`, `description`, `system_prompt`. It creates
the bot account, writes the profile skeleton, and stores the token — the operator
confirms via a button before it runs. Afterwards edit the generated files as
needed and ask the operator to **restart** (`impi restart` in a deployment). If
the tool reports a missing admin token, ask the operator to set
`TOOL_CREATE_AGENT_ADMIN_TOKEN` in the engine `.env` — or use the manual flow.

**Manual flow** (Slack, or no admin token): write the profile files yourself,
then tell the operator to:

1. Create a bot account for the agent on its gateway (Mattermost bot, or a Slack
   app for `GATEWAY=slack`).
2. Put its token in the engine `.env`:
   - Mattermost: `AGENTS_MM_TOKEN__<NAME>` (upper-case, `-`→`_`).
   - Slack: `AGENTS_SLACK_BOT_TOKEN__<NAME>` + `AGENTS_SLACK_APP_TOKEN__<NAME>`,
     and `AGENTS_GATEWAY__<NAME>=slack`.
3. **Restart** the engine.

**Editing an existing** agent (agent.yaml, SYSTEM.md, skills, extensions): change
the files, then apply with a **reload** — run `pkill -HUP -n -f '[i]mpi\.main'`
yourself, or ask the operator to `make reload`. Reload re-reads profiles and drops
idle sessions; conversation memory survives. (A new agent still needs a restart.)

## 4. Toggles the operator has

- `AGENTS_ENABLED` (CSV) — which agents run; empty = all found.
- `AGENTS_SKILLS__<NAME>` (CSV) — override an agent's skills without editing its
  `agent.yaml` (empty = no skills; unset = the agent.yaml list). Useful for the
  bundled agents whose `agent.yaml` lives inside the package.
- Shared extensions live at `$AGENTS_PATH/_extensions/<name>/index.ts` (loaded for
  every agent); per-agent skills live in that agent's `.pi/skills/`.

When you finish, briefly tell the operator what you created/changed and the exact
step to apply it (restart vs reload, and which `.env` keys to set).
