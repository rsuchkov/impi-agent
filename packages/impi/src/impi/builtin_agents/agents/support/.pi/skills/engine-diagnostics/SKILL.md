---
name: engine-diagnostics
description: Work out why something in the engine is not working — an agent that never starts, a turn that fails, a tool that is refused, widgets or commands that never arrive, a task that does not fire. Use whenever the operator reports that something is broken, missing, or silently doing nothing.
---

# Diagnosing the engine

Work from evidence, not from a guess. Most reports fall into one of five layers,
and each has a cheap check that tells you which one you are in.

Reference: `$IMPI_ROOT/docs/troubleshooting.md`.

When the question is "can agent X reach Y" or "is this safe" rather than "why
is this broken", the answer is in `$IMPI_ROOT/docs/security.md`: the four
boundaries, what an agent with a shell can reach in the default deployment
versus a container per agent, where data and credentials live, the known
limits. Quote it rather than reasoning it out.

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

The **engine itself refuses to start** on a setting it cannot honour, and says
which in the first lines of `impi logs`:

- on MongoDB, after an update that made `sessions.runtime_session_id` unique:
  Mongo will not change an index in place, so the operator runs
  `db.sessions.dropIndex("runtime_session_id_1")` once, then starts again
  (`$IMPI_ROOT/docs/storage.md`);
- `INTEGRATIONS_UI_TIMEOUT` at 270 or above: the runtime's tool extension gives
  up on a call after 300 s, and a confirmation answered later would run a call
  the model was already told had failed. Lower it.
- on MongoDB, a `DB_COLLECTION_PREFIX` with a character outside letters,
  digits, `_` and `-`: the prefix goes on every collection name and must read
  in a shell without quoting (`$IMPI_ROOT/docs/storage.md`);
- a `RUNTIME_ENV`, `RUNTIME_ENV__<AXIS>` or `AGENTS_ENV__<AGENT>` entry that is
  not a variable name or a family with a real prefix, or that covers the
  engine's own (`AGENTS_*`, `TOOL_*`, `LLM_*`, …) — the error names the key;
- a `RUNTIME_EXTENSIONS` name that is not one (a `/`, `..`);
- `RUNTIME_CONFINEMENT` or `AGENTS_CONFINEMENT__<AGENT>` that is not
  `conversation`, `agent` or `off`;
- an `INTEGRATIONS_SECRET` shorter than 16 characters or with characters
  outside letters, digits, `-` and `_` (it is a segment of a URL).

After that same update a `ws` agent **starts every conversation afresh once**:
its session keys changed shape. That is expected, not a lost volume — the
agent-containers skill's "forgot everything" diagnosis does not apply.

## 2. Turns fail

- "refusing to start the runtime: … holds a value it would run as a command"
  (engine log), or a host refusing a spawn with "replace the file with a fresh
  login" — the model credential holds a key beginning with `!`, which `pi`
  would run. No login writes one; something unconfined did. Nothing starts
  until the operator replaces it with `impi login` — say so, and that what
  wrote it is worth finding.
- Every agent fails to call the model after working for days ("No API key",
  "OAuth refresh failed") — the engine refreshes the shared login (log:
  `refreshing the runtime's login failed: …`); a confined process cannot. A
  fresh `impi login` fixes it.
- "The model's service is not answering right now" — the provider (or the
  gateway in front of it) answered with a 5xx or could not be reached. Not the
  agent, not the request: the engine log's `pi LLM error:` line names it.
  Usually gone in a minute; if not, the endpoint or the provider is down. An
  agent with `runtime.fallback_models` only shows this once every fallback
  failed too.
- Which model answered: every turn's `pi turn: … model=<provider/model>` line in
  the engine log. A move to a fallback is a line of its own, `fallback: <from>
  failed (<error>); switching to <to>` (INFO) — in the engine log, or for an
  agent in a container of its own in `impi agent logs <agent>` (as `runtime
  stderr: fallback: …`). "is not a model this runtime knows" or "cannot
  switch" there means the fallback has no login or definition — `impi health`
  names those.
- Every turn fails from the start with `EACCES` or `EROFS` on `auth.json` —
  look for `the runtime's agent directory …` at ERROR in the engine log, from
  startup. The engine creates the credential files there before any agent
  runs, and the runtime locks beside them on every model call, so that
  directory (`IMPI_HOME/pi/agent` on a compose install) must be writable by
  the engine.
- The engine does not start, or a reload is refused ("profile reload failed;
  keeping running config"), naming a provider without a model:
  `DEFAULT_PROVIDER is set (…) but DEFAULT_MODEL is not` (or `SUPPORT_…`), or
  `<agent.yaml>: runtime.provider is set (…) but runtime.model is not`. The
  runtime refuses a provider without a model on every turn; set the model, or
  clear the provider. One such `agent.yaml` stops the whole engine at start,
  not only that agent. The same for `runtime.tools entry '…': name each tool`
  — a `*` pattern or a `+name`/`-name` entry, which would hand out tools
  nobody named.
- "pi process exited unexpectedly" — the error carries the exit code and the
  last stderr lines; read them. Usual causes: a custom endpoint that is down,
  a provider/model the backend does not have, a missing `models.json`.
- The model says a tool was denied — that is pi's own permission system, not the
  engine's allowlist. See `docs/troubleshooting.md`.
- A conversation that fails on **every** turn after someone sent a picture: the
  session replays its history. Reset just that conversation:
  `impi sessions delete <agent> <conversation>`.
- A turn that ran tools leaves a message — `Running 3 tools · 1 failed →` —
  above the reply in a thread, in a thread under the person's message in a
  direct message or a channel, and beside the reply after a click, a slash
  command or a scheduled run. Ask the operator to open it and paste the row
  that failed: it
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
- **An agent answers but remembers nothing of an old conversation** — besides
  a skipped migration (agent containers), `SESSION_RETENTION_DAYS` may have
  forgotten it at startup: the log says `forgot N conversation(s) idle for D+
  days`, and only idle ones go.

## 3. A tool is missing or refused

- **A skill says something is not configured that plainly is** — a user's
  agent's process inherits only a base set (PATH, HOME, locale, proxies,
  certificates, `PI_*`) plus what is named: `runtime.env` in its `agent.yaml`
  (or `AGENTS_ENV__<NAME>`) and `RUNTIME_ENV` / `RUNTIME_ENV__*` for every agent.
  Name the variable there. You inherit everything yourself, so do not test it
  from your own shell.
- **A skill fails with `Permission denied` (or `EACCES`, `Operation not
  permitted`) on a path that exists** — the agent's process is confined
  (Landlock). It reads its profile, the skill library, the system and the files
  it is handed; it writes only its conversation's memory and files
  (`$AGENT_FILES_DIR`) and its own `$TMPDIR` and `$HOME`. The startup log and
  `impi health` say each agent's level (`confinement: … assistant=conversation`).
  Fixes, narrowest first: write to `$AGENT_FILES_DIR` or `$TMPDIR` instead;
  declare the path in `agent.yaml` (`runtime.confinement_read: [...]` /
  `runtime.confinement_write: [...]`, absolute or relative to the profile) and
  reload; `runtime.confinement: agent` when the agent's conversations must
  share its files; `runtime.confinement: off` (or
  `AGENTS_CONFINEMENT__<NAME>=off`) only for an agent trusted with everything
  the engine's user can reach. You are unconfined yourself, so do not test it
  from your own shell. In a container of its own, declared paths do not apply
  — the level does.
- **An agent "forgot" a file from another conversation** — at the default
  level each conversation has its own files directory. Working as designed;
  `runtime.confinement: agent` shares them.
- **Someone clicked Allow on a tool's card and was told only an approver can
  answer** — the card is addressed to the person whose message started the
  turn, or to `TOOL_APPROVERS` when set (log: `approval … may not answer
  for …`). Working as designed; the card stays live for the right person. If
  colleagues should be able to approve, name them in `TOOL_APPROVERS`.

In order:

1. Is it in that agent's `runtime.tools`? It is an allowlist; nothing is ambient.
2. Was it dropped for a missing capability? The startup log says
   `tool … not advertised — gateway lacks …` (chat-admin without an admin
   client, widgets with `INTEGRATIONS_ENABLED=false`, `send_file` with
   attachments off, scheduling with the scheduler off, `connected_api_*` with
   no provider configured).
3. Is the whole typed-tool server off (`TOOL_ENABLED=false`)?
4. Skills need `read` + `bash` in the same list.

A tool added to a profile applies on **reload**; a `403 forbidden` right after
an edit means the change was not applied yet.

Five other 403s say what happened in the body. From the confirmation gate:

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

- `session required` — the call named no session at all. Every process the
  engine starts names one (a scheduled, memoryless run under a one-off id), so
  this is something else calling the tool server by hand with a token it found.
  Not a configuration problem; worth telling the operator which agent's token
  it was (the log line names the agent).
- `session not proven` — the call named a conversation without that process's
  own `TOOL_SESSION_PROOF` (log: `named session … without its proof; refused`).
  The engine's own runtime always has it; this is a stale process from before
  a restart, or something in the agent's container calling the tool server by
  hand. Not a configuration problem.

### What `impi health` may add

- `! agents X, Y run unconfined in the engine's own process space with a shell …`
  — not a bug: those agents run at confinement level `off` (or the kernel has
  no Landlock) and can read the environment of every process in the engine's
  container. The answer is confinement (drop `off`), a container per agent, or
  no shell for them (`$IMPI_ROOT/docs/security.md`).
- `confinement: Landlock ABI N; a=conversation, b=agent; unconfined: support`
  — each agent's level. `! confinement: this kernel has no Landlock` — every
  agent runs unconfined; the host's kernel (Linux 5.13+ with Landlock in its
  LSM list) decides, not the engine.
- `receiver secret missing (INTEGRATIONS_SECRET)` — the engine mints one on its
  next start; slash commands are then re-registered under the URL health prints.
- `connections: gitlab (agents: all) — register https://…/oauth/callback …`
  — people may connect accounts; `!` lines after it say what keeps a connect
  or a tool from working (no TLS, interactivity or the tool server off, an
  agent named that does not run, a provider allowed to agents on ws/http, where
  nobody may connect). The
  **connections** skill has the rest.
- `extensions in …: a, b; enabled (RUNTIME_EXTENSIONS): none` — an extension
  in `_extensions/` loads only when named in `RUNTIME_EXTENSIONS`.
- `fallback models: a: p/m → q/n; …` — the models each running agent moves on
  to when its own fails the way a service fails, as configured.
- `! fallback models with no login or definition for their provider where the
  agent runs: a → q/n …` — the runtime will not switch to that model. A login
  in `auth.json` (`impi login`) or, for an agent the engine runs, a definition
  in `models.json` fixes it; an agent in its own container is handed
  `auth.json` alone. A key the runtime takes from the environment is not seen
  here, so with `LLM_*` this may be a false alarm.

## 4. Widgets, forms or commands never arrive

These come back over HTTP, so the Mattermost server must be able to reach the
receiver: `INTEGRATIONS_PUBLIC_URL` reachable from the server, its subnet in
Mattermost's `AllowedUntrustedInternalConnections`, `INTEGRATIONS_ENABLED=true`.
Slack needs none of this — it uses its socket.

A click or command that **arrives and is answered 403** with the log line
`not on the receiver's secret path` is the other case: the receiver answers
only under `<INTEGRATIONS_PUBLIC_URL>/<INTEGRATIONS_SECRET>/…`, and the URL
the platform holds predates the secret. Slash commands must be re-registered
with the URL `impi health` prints; cards posted before the secret existed stay
dead. The broker's receiver has the same rule under `WARD_CALLBACK_SECRET`.

On the `http` gateway there is no chat to deliver to: a confirmation card is a
`confirmation` event in the caller's journal (the runtime's own yes/no questions
are `actions` events) and the caller's program answers it through the API; fire-and-forget widgets, forms, slash commands and the
tool-trace widget do not exist there by design (`$IMPI_ROOT/docs/http-gateway.md`).
"Nothing arrives" on http means the program is not polling the turn's events.
Its `/readyz` answers 503 only while the engine starts; how loaded it is —
runtime slots, turns in flight — is on `/stats`.

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
