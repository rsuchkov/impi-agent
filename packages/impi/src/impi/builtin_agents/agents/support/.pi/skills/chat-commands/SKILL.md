---
name: chat-commands
description: Register a slash command (Mattermost, or Slack outside threads), a Slack message shortcut, or an emoji reaction so it reaches an agent as a command, and diagnose one that does nothing. Use when the operator asks to add a /command, wire a command or a reaction to an agent, or reports that a slash command or a reaction fails or answers as the wrong bot.
---

# Slash commands, shortcuts and reactions

A command is an ordinary turn started by a word instead of a mention: the user
types `/summarize` — or picks a shortcut, or puts a mapped emoji on a message
— and that agent answers in the same conversation. Setup has two halves — one
in the platform, one in the engine `.env` — and both must agree. Reference:
`$IMPI_ROOT/docs/commands.md`.

## 1. Is the word already taken?

Some words are answered by the **engine itself**, with no agent turn: the skill
browser (`SKILLS_COMMAND`, default `skills`), the task browser
(`TASKS_COMMAND`, default `tasks`) and, where a provider is configured, the
connected-accounts panel (`CONNECTIONS_COMMAND`, default `connections`; a
direct message with the agent only — see the **connections** skill).
Registering one of those words gives the screen; registering any other word
gives the agent. If the operator wants a
screen under a different word, they change that setting — the trigger word must
match it exactly.

A command is not the only way in: `open_screen` opens the same panel from an
ordinary turn, so "show me the skills" works without any registration at all.
**You hold that tool** — use it rather than describing what the panel would say. The model only decides to open it — the panel and its
buttons are still the engine's. That is also the only route on Slack, where a
custom slash command cannot run inside a thread.

Slash commands and screens exist on Mattermost and Slack only. An agent on the
`ws` or `http` gateway has no command surface: its callers are programs, and a
program asks for things in the message itself.

## 2. Mattermost

**Create the command** in System Console → Integrations → Slash Commands, or:

```bash
mmctl command create <team> --title Summarize --trigger-word summarize \
    --url <URL> --creator <user> --post
```

Request method must be **POST**. `<URL>` is one of:

- `<INTEGRATIONS_PUBLIC_URL>/<INTEGRATIONS_SECRET>/command/<agent>` — the path
  names the agent, because a Mattermost payload does not say which bot it is
  meant for; the secret segment is what says the request came from Mattermost
  (the receiver answers 403 off it). `impi health` prints the URL (with `<agent>` to fill in); the
  secret is in `$DOTENV_PATH`, minted by the engine on its first start;
- `<INTEGRATIONS_PUBLIC_URL>/<INTEGRATIONS_SECRET>/command/default` — the engine picks: the only agent
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

Two entries, by where the command is typed. **Outside a thread** — a channel
or a direct message — a slash command works as on Mattermost: the operator
declares it in the agent's Slack app (*Slash Commands*, any name, no request
URL — Socket Mode delivers it), and the engine answers `/skills`, `/tasks`
and `/connections` with their panels and anything else as a turn of the agent. **Inside a
thread** Slack does not allow custom slash commands — that is a platform rule
— so the entry is a **message shortcut** whose callback id starts with
`SLACK_COMMAND_PREFIX` (default `crux_`); the rest of the id is the command
word. Neither needs a URL or a token: the socket is already authenticated.

Note the asymmetry: a shortcut never opens a screen. A `crux_tasks` shortcut
on a Slack agent becomes an ordinary turn, and the model will answer about a
task list it cannot read — in a thread, point them at `open_screen` instead.

## 4. Reactions (both platforms)

An emoji on a message runs a command in that message's conversation when the
**agent's own map** names it: `AGENTS_REACTION_COMMANDS__<AGENT>` (CSV of
`emoji=command`, platform short names without colons: `white_check_mark=done`);
`REACTION_COMMANDS` is the default agent's map only. Per agent on purpose —
every agent in a channel sees every reaction there, and one emoji must not
start all of them. The agent reads the command word as the reactor's message,
and the message the reaction was put on as a quoted block under its author's
name — not as the reactor's words. `eyes` is what an agent puts on a message
it is working on; its own reactions, other agents' and other bots' are never
commands, a person's are. A map entry that is not `emoji=command` stops the
engine at start, naming the key.

Platform side: Mattermost needs nothing registered. A Slack app needs the bot
scope `reactions:read` and the `reaction_added` event subscription; the
`*:history` scopes it already has for messages let the engine quote the
message. Both need `INTEGRATIONS_ENABLED=true`, as a shortcut does.

## 5. Networking (Mattermost only)

The command reaches the same receiver as widget callbacks:

- `INTEGRATIONS_ENABLED=true`, receiver on `INTEGRATIONS_PORT` (default 8423);
- `INTEGRATIONS_PUBLIC_URL` must be reachable **from the Mattermost server** —
  `auto` detects the host LAN IP at startup;
- Mattermost blocks outbound calls to internal addresses: the receiver's subnet
  must be in `AllowedUntrustedInternalConnections`.

## 6. When a command does nothing

Read the engine log and match the line:

| Log | Meaning |
|---|---|
| `command <cmd> for <agent>: rejected (token mismatch)` | the token is missing from, or wrong in, `AGENTS_COMMAND_TOKENS__<AGENT>` / `COMMAND_TOKENS` |
| `/command/default resolves to no agent` | several agents run and `AGENT_NAME` names none of them |
| `command for <agent> …` then a normal turn | it worked; the word simply isn't a screen |
| `screen <word> opened for <agent>` | the engine answered it itself |
| `agent <x> has no live presence` | the agent isn't running — no token, or not enumerated |
| `callback to /<?>/… refused: not on the receiver's secret path` | registered without, or with an old, `INTEGRATIONS_SECRET` — re-register under the URL `impi health` prints |
| nothing at all | the request never arrived: URL, port, or the Mattermost outbound block |
| `reaction :<emoji>: by <user> in <conv> runs /<cmd> for <agent>` | a reaction worked; what follows is a normal turn |
| a reaction, and no line at all | the emoji is not in that agent's map (check `AGENTS_REACTION_COMMANDS__<AGENT>`, not only the global key); interactivity is off; on Slack the app lacks `reactions:read` or the `reaction_added` subscription; or the reaction was an agent's or a bot's |
| `could not read the message under a reaction … missing a *:history scope` | Slack only: the command ran without the quote; add the history scopes |

A command posts its answer into the conversation it was invoked from; the
immediate reply the user sees is only an ephemeral receipt.
