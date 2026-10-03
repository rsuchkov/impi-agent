---
name: engine-diagnostics
description: Work out why something in the engine is not working — an agent that never starts, a turn that fails, a tool that is refused, widgets or commands that never arrive, a task that does not fire. Use whenever the operator reports that something is broken, missing, or silently doing nothing.
---

# Diagnosing the engine

Work from evidence, not from a guess. Most reports fall into one of five layers,
and each has a cheap check that tells you which one you are in.

Reference: `$IMPI_ROOT/docs/troubleshooting.md`.

## 0. What you can see from where you are

You run inside the engine's own process tree, so:

- **You can** read `$IMPI_ROOT` (code and docs), `$AGENTS_PATH` (profiles), the
  config file (`$DOTENV_PATH`, `/app/conf/.env` in a container), and run
  `impi health`, `impi task status`, `impi task runs`, `impi sessions list`,
  `impi skill list`, `impi agent list`.
- **You cannot** read the engine log: it goes to the process's stdout, which the
  container runtime captures. Ask the operator for `impi logs` (or
  `impi logs -f`); in a source checkout it is `data/logs/engine.log`.
  `impi doctor` is likewise a host command — it checks compose, the container
  runtime and its version, every container that exists but is not running
  (with the daemon's own error and the fix), file permissions, whether the
  engine reported readiness, and which inventory it actually reached. That
  last one matters when the tasks or conversations an
  operator expects are simply not there: the inventory is a SQLite file by
  default but can be a database on a server (`$IMPI_ROOT/docs/storage.md`), and
  a setting that did not arrive leaves the engine reading an empty one.
- **Where each agent has a container of its own**, that agent's log is its own
  too: `impi agent logs <agent>`, not `impi logs`. The engine's log still has
  the engine's half of the story (a spawn refused, a host unreachable).

- **On the operator's host** (`impi escape`) all of that is yours: `impi logs`,
  `impi doctor`, `impi agent logs <agent>`, `impi ward status` run directly.
  Run them rather than asking, and quote what matters from the output.

So: gather everything you can yourself, then ask for **one specific thing** from
the log rather than "send me the logs".

## 1. An agent does not exist / never answers

```bash
impi agent list          # profiles the engine can see
impi health              # Mattermost reachable + the agents dir
```

An agent is skipped at startup, with a log line, when:

- **no token** — `AGENTS_MM_TOKEN__<NAME>`, or the Slack pair, or the unsuffixed
  `MATTERMOST_TOKEN`/`SLACK_*` for the default agent (`AGENT_NAME`);
- its gateway is `slack` (often because the global `GATEWAY` is) but it has no
  Slack tokens;
- `AGENTS_ENABLED` is set and does not list it.

A **new** agent needs a restart; agents are enumerated once at startup. Profile
edits need only a reload.

The **engine itself refuses to start** in two configurations, and says which in
the first lines of `impi logs`:

- on MongoDB, after an update that made `sessions.runtime_session_id` unique:
  Mongo will not change an index in place, so the operator runs
  `db.sessions.dropIndex("runtime_session_id_1")` once, then starts again
  (`$IMPI_ROOT/docs/storage.md`);
- `INTEGRATIONS_UI_TIMEOUT` at 270 or above: the runtime's tool extension gives
  up on a call after 300 s, and a confirmation answered later would run a call
  the model was already told had failed. Lower it.

After that same update a `ws` agent **starts every conversation afresh once**:
its session keys changed shape. That is expected, not a lost volume — the
agent-containers skill's "forgot everything" diagnosis does not apply.

## 2. Turns fail

- "pi process exited unexpectedly" — the error carries the exit code and the
  last stderr lines; read them. Usual causes: a custom endpoint that is down,
  a provider/model the backend does not have, a missing `models.json`.
- The model says a tool was denied — that is pi's own permission system, not the
  engine's allowlist. See `docs/troubleshooting.md`.
- A conversation that fails on **every** turn after someone sent a picture: the
  session replays its history. Reset just that conversation:
  `impi sessions delete <agent> <conversation>`.
- A turn that ran tools leaves a message above its reply — `Running 3 tools ·
  1 failed →`. Ask the operator to open it and paste the row that failed: it
  names the tool, its arguments and how long it ran. It never shows a result,
  by design (`$IMPI_ROOT/docs/tool-trace.md`), so "what did the tool return"
  still has to come from the log.
- "I am at capacity right now" — every runtime slot (`PI_MAX_CONCURRENT_SESSIONS`,
  default 4) holds a turn in flight. Idle sessions do not cause this any more:
  a full pool drops the longest-unused idle one for a new turn
  (`PI_EVICT_IDLE_ON_PRESSURE`, log line `runtime full: dropping idle session …`),
  and the evicted conversation resumes from disk on its next message — its
  memory is not lost. Only when every slot is **busy** does a turn wait
  `PI_ACQUIRE_TIMEOUT_S` (120) and then say so.
- A confirmation card left waiting no longer ends the turn: the turn's timeout
  stops while the gate waits. A turn that times out while a card is up has some
  other cause.

## 3. A tool is missing or refused

In order:

1. Is it in that agent's `runtime.tools`? It is an allowlist; nothing is ambient.
2. Was it dropped for a missing capability? The startup log says
   `tool … not advertised — gateway lacks …` (chat-admin without an admin
   client, widgets with `INTEGRATIONS_ENABLED=false`, `send_file` with
   attachments off, scheduling with the scheduler off).
3. Is the whole typed-tool server off (`TOOL_ENABLED=false`)?
4. Skills need `read` + `bash` in the same list.

A tool added to a profile applies on **reload**; a `403 forbidden` right after
an edit means the change was not applied yet.

Four other 403s say what happened in the body. From the confirmation gate:

- `declined by the user` — the tool declares `requires_confirmation`, the card
  went out, and the answer was Deny or nobody answered in time. A human can
  answer **Allow for…** instead, which stops the questions for that agent and
  that tool **in that conversation** until the window closes (`TOOL_MAX_GRANT_S`
  caps it); another thread or direct message is asked on its own.
- `cannot be confirmed here` — the same tool reached the server in a
  deployment with no way to ask. Normally it cannot even get there: with
  interactivity off such a tool is left out of the agent's tool list (the
  engine log says `not advertised — gateway lacks confirmation`), so seeing
  this body means something other than the runtime made the call. Turn
  interactivity on, or drop the tool from that agent.
- `abandoned by the caller` — the card was answered Allow, but the turn that
  asked had already been cancelled or had hung up. Nothing ran; the ledger
  says `abandoned`. Normal after an operator interrupted a turn.

And from the session check:

- `session not proven` — the call named a conversation without that process's
  own `TOOL_SESSION_PROOF` (log: `named session … without its proof; refused`).
  The engine's own runtime always has it; this is a stale process from before
  a restart, or something in the agent's container calling the tool server by
  hand. Not a configuration problem.

## 4. Widgets, forms or commands never arrive

These come back over HTTP, so the Mattermost server must be able to reach the
receiver: `INTEGRATIONS_PUBLIC_URL` reachable from the server, its subnet in
Mattermost's `AllowedUntrustedInternalConnections`, `INTEGRATIONS_ENABLED=true`.
Slack needs none of this — it uses its socket.

On the `http` gateway there is no chat to deliver to: a confirmation card is an
`actions` event in the caller's journal and the caller's program answers it
through the API; fire-and-forget widgets, forms, slash commands and the
tool-trace widget do not exist there by design (`$IMPI_ROOT/docs/http-gateway.md`).
"Nothing arrives" on http means the program is not polling the turn's events.
Its `/readyz` answers 503 while the engine starts and while every runtime slot
is busy.

For a slash command specifically, the **chat-commands** skill has the log-line
table (token mismatch, unresolvable default, no live presence, nothing at all).

## 5. A scheduled task did not run

`impi task status` and `impi task runs <task>` answer this precisely — the
**scheduled-tasks** skill has the verdicts and the per-status table.

## 6. Reporting back

- Name the layer and the evidence: "the agent isn't running — no
  `AGENTS_MM_TOKEN__X` in the config", not "it seems the token may be missing".
- Give the exact fix: which key, which file, restart or reload.
- If you could not confirm something, say what you would need (one log line, one
  command's output) instead of guessing.
- You may edit profiles under `$AGENTS_PATH`; the engine itself is read-only.
  Config changes are the operator's to make unless one of your tools does it.
