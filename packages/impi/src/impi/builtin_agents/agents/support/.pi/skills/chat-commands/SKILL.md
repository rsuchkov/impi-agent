---
name: chat-commands
description: Register a slash command (Mattermost) or message shortcut (Slack) so it reaches an agent, and diagnose one that does nothing. Use when the operator asks to add a /command, wire a command to an agent, or reports that a slash command fails or answers as the wrong bot.
---

# Slash commands and shortcuts

A command is an ordinary turn started by a word instead of a mention: the user
types `/summarize` and that agent answers in the same conversation. Setup has
two halves — one in the platform, one in the engine `.env` — and both must
agree. Reference: `$IMPI_ROOT/docs/commands.md`.

## 1. Is the word already taken?

Some words are answered by the **engine itself**, with no agent turn: the skill
browser (`SKILLS_COMMAND`, default `skills`) and the task browser
(`TASKS_COMMAND`, default `tasks`). Registering `/skills` or `/tasks` gives the
screen; registering any other word gives the agent. If the operator wants a
screen under a different word, they change that setting — the trigger word must
match it exactly.

A command is not the only way in: an agent holding the `open_screen` tool opens
the same panel from an ordinary turn, so "show me the skills" works without any
registration at all. The model only decides to open it — the panel and its
buttons are still the engine's. That is also the only route on Slack, where a
custom slash command cannot run inside a thread.

## 2. Mattermost

**Create the command** in System Console → Integrations → Slash Commands, or:

```bash
mmctl command create <team> --title Summarize --trigger-word summarize \
    --url <URL> --creator <user> --post
```

Request method must be **POST**. `<URL>` is one of:

- `<INTEGRATIONS_PUBLIC_URL>/command/<agent>` — the path names the agent, because
  a Mattermost payload does not say which bot it is meant for;
- `<INTEGRATIONS_PUBLIC_URL>/command/default` — the engine picks: the only agent
  running, or the one `AGENT_NAME` names when several run. The startup log line
  `/command/default -> <agent>` says which.

**Put the token it issues in `.env`.** Mattermost generates one token per
command:

```
AGENTS_COMMAND_TOKENS__<AGENT>=<token>,<token2>    # CSV, one per command
COMMAND_TOKENS=<token>                             # the default agent only
```

An agent with no tokens refuses every command with 403 — that check is the only
thing between the receiver's open port and running a turn as that agent, so
never suggest removing it. `/command/default` is no exception: resolving an
agent is not authorising a command.

**Restart** the engine afterwards (`.env` is read at startup).

## 3. Slack

Slack does not allow custom slash commands inside threads — that is a platform
rule. Use a **message shortcut** whose callback id starts with
`SLACK_COMMAND_PREFIX` (default `crux_`); the rest of the id is the command
word. No URL and no token: the socket is already authenticated.

Note the asymmetry: **the engine's screens are Mattermost-only.** A `crux_tasks`
shortcut on a Slack agent becomes an ordinary turn, and the model will answer
about a task list it cannot read.

## 4. Networking (Mattermost only)

The command reaches the same receiver as widget callbacks:

- `INTEGRATIONS_ENABLED=true`, receiver on `INTEGRATIONS_PORT` (default 8423);
- `INTEGRATIONS_PUBLIC_URL` must be reachable **from the Mattermost server** —
  `auto` detects the host LAN IP at startup;
- Mattermost blocks outbound calls to internal addresses: the receiver's subnet
  must be in `AllowedUntrustedInternalConnections`.

## 5. When a command does nothing

Read the engine log and match the line:

| Log | Meaning |
|---|---|
| `command <cmd> for <agent>: rejected (token mismatch)` | the token is missing from, or wrong in, `AGENTS_COMMAND_TOKENS__<AGENT>` / `COMMAND_TOKENS` |
| `/command/default resolves to no agent` | several agents run and `AGENT_NAME` names none of them |
| `command for <agent> …` then a normal turn | it worked; the word simply isn't a screen |
| `screen <word> opened for <agent>` | the engine answered it itself |
| `agent <x> has no live presence` | the agent isn't running — no token, or not enumerated |
| nothing at all | the request never arrived: URL, port, or the Mattermost outbound block |

A command posts its answer into the conversation it was invoked from; the
immediate reply the user sees is only an ephemeral receipt.
