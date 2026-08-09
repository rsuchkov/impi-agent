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
  `impi doctor` is likewise a host command — it checks compose, file
  permissions, and whether the engine reported readiness.

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

## 2. Turns fail

- "pi process exited unexpectedly" — the error carries the exit code and the
  last stderr lines; read them. Usual causes: a custom endpoint that is down,
  a provider/model the backend does not have, a missing `models.json`.
- The model says a tool was denied — that is pi's own permission system, not the
  engine's allowlist. See `docs/troubleshooting.md`.
- A conversation that fails on **every** turn after someone sent a picture: the
  session replays its history. Reset just that conversation:
  `impi sessions delete <agent> <conversation>`.

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

## 4. Widgets, forms or commands never arrive

These come back over HTTP, so the Mattermost server must be able to reach the
receiver: `INTEGRATIONS_PUBLIC_URL` reachable from the server, its subnet in
Mattermost's `AllowedUntrustedInternalConnections`, `INTEGRATIONS_ENABLED=true`.
Slack needs none of this — it uses its socket.

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
