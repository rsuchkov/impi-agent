# Troubleshooting

Task-oriented fixes for common issues. Start with the logs: run in the foreground
(`make run`) or check `data/logs/engine.log` (`make run-bg`); raise detail with
`LOG_LEVEL=DEBUG`. The engine logs a one-line summary per turn and names any agent
it skips and why.

## No agents start

The engine raises "No agents with a gateway token — nothing to run" when nothing
was built. Check, in order:

- **`AGENTS_PATH`** points at a directory that contains `agents/<name>/agent.yaml`.
- **A token is set** for at least one agent. An agent is present only if its token
  is set: `AGENTS_MM_TOKEN__<NAME>` (or the Slack pair), with the default agent
  (`AGENT_NAME`) falling back to `MATTERMOST_TOKEN`. A tokenless profile is skipped
  (logged).
- **`AGENTS_ENABLED`** — if set, only the listed names run. Empty = all found.
- For the bundled `support` agent, set `AGENTS_MM_TOKEN__SUPPORT`.

## `pi` not found / turns fail immediately

`pi` must be on `PATH` (`npm i -g @earendil-works/pi-coding-agent`, needs Node), or
point `PI_BIN` at it. Verify with `pi --help`. If turns start but the model errors,
it's usually authentication — see below.

## Model / provider errors

`pi` owns the model connection. Either:

- a **subscription login** (`pi` authenticates itself; leave `LLM_*` empty), or
- a **custom endpoint** (`LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL`).

Confirm the agent's `provider`/`model` resolve to something the backend accepts
(see [runtime-notes.md](runtime-notes.md)). An agent that omits them inherits
`DEFAULT_PROVIDER`/`DEFAULT_MODEL`.

## "pi process exited unexpectedly"

The `pi` subprocess died mid-turn. The error now carries the **exit code and
the last stderr lines** — read them, they name the actual cause. Typical ones:

- a custom endpoint that is down or misconfigured (`Invalid URL` — check
  `~/.pi/agent/models.json`; note `baseUrl` does NOT interpolate `$VARS`, see
  [runtime-notes.md](runtime-notes.md));
- a provider/model that doesn't exist for the backend (`pi --list-models` with
  the same env shows what pi actually sees);
- a missing `models.json` when the setup expects a custom provider.

To reproduce outside the engine: run
`pi --provider <p> --model <m> -p "ping"` by hand with the same environment.

## The model says a tool was denied / "blocked by security policy"

The agent answers that it is not allowed to use its tools, or logs show
`User denied tool '<name>'`. That denial comes from **pi's permission system**,
not from the engine (the engine's own gate is the `--tools` allowlist). On a
headless bot a permission "ask" cannot be answered by a human, so it resolves
to a denial. Check, in order:

1. **Local pi config**: a third-party permissions module/extension in
   `~/.pi/agent/settings.json` applies to every pi the engine spawns — remove
   it for headless use, or add a per-agent allow policy (next item).
2. **Per-agent policy**: ship `.pi/agent/pi-permissions.jsonc` inside the
   agent's profile dir allowing exactly its tools.
3. **pi version drift**: behavior differs across pi versions — align the local
   `pi --version` with the version the engine was verified against
   (see [runtime-notes.md](runtime-notes.md)).

## Widget/form clicks do nothing

Clicks reach the engine through the interactions receiver. For a **Slack** agent
this is the socket — no networking to configure. For **Mattermost**, the server
must be able to POST back to the receiver:

- The receiver binds `INTEGRATIONS_HOST` (default `0.0.0.0`) on `INTEGRATIONS_PORT`
  (default `8423`).
- `INTEGRATIONS_PUBLIC_URL` is the URL Mattermost calls back to. The default,
  `http://host.containers.internal:{port}`, works when Mattermost runs in a
  container that can route to the host by that name. If it can't, set
  `INTEGRATIONS_PUBLIC_URL=auto` (the engine detects the host LAN IP at startup) or
  a fixed `http://IP:port`.
- Mattermost blocks outbound calls to internal addresses by default — add the
  receiver's subnet to **`AllowedUntrustedInternalConnections`** in the Mattermost
  config.

Confirm `INTEGRATIONS_ENABLED=true`.

**The modal never opens on Mattermost.** The log carries
`Mattermost refused the dialog (field types: …)`. Usually the server is older
than a control the form uses: `multiselect` needs **11.0**, `date`/`datetime`
need **11.1** (see the type table in
[creating-agents.md](creating-agents.md)). Drop those types or upgrade the
server. The co-deployed Mattermost is new enough.

Related Slack failure: `ui bridge: failed to post widget (...)` in the log —
posting interactive components needs **Interactivity enabled** on the Slack app
(and the usual bot scopes); the log message includes the Slack error code. A
widget-free bot (read-only Q&A, no `ask_user_*`/forms) can simply set
`INTEGRATIONS_ENABLED=false` — then interactive runtime requests are declined
locally instead of attempting a post.

## A tool isn't available to an agent

- The agent must **name the tool** in `runtime.tools` — it's an allowlist.
- A typed tool is **dropped** (and logged) when the agent's gateway/config lacks a
  capability it requires — e.g. channel-admin tools on a Slack agent, or widget/form
  tools when `INTEGRATIONS_ENABLED=false`. Check the startup log for
  "tool … not advertised".
- The whole typed-tool server is off if `TOOL_ENABLED=false`.
- Skills need `read` + `bash` in `runtime.tools` to run.

## A conversation keeps failing after someone sent a picture

A model backend that refuses an image refuses it **on every later turn too**: the
runtime session replays its history, so the bad picture is in every request. The
engine only shows the model files whose bytes really are a PNG/JPEG/GIF/WebP (a
corrupt or renamed file travels as a path instead), so this should not happen —
if it does, the log says so and names the way out:

```bash
impi sessions delete <agent> <conversation>
```

That forgets the conversation (inventory row + the runtime's memory for it) and
the next message starts fresh. See [files.md](files.md).

## A scheduled task didn't run

Ask the scheduler before guessing — an idle ticker and a dead one look the same
from outside:

```bash
impi task status          # alive / stale / never / absent, and the next wake-up
impi task runs <task>     # every occurrence, with the reason it ended that way
```

- **`absent`** — `SCHEDULER_ENABLED=false`. Off on purpose, not broken.
- **`stale` or `never`** — the loop is not ticking; `impi logs` will have the
  reason, and the heartbeat carries the last error it hit.
- **the run says `missed`** — it was later than its grace window (half the
  period, 2 min…2 h) or the task has `on_missed: skip`. A catch-up happens once,
  not once per missed interval.
- **`no_agent`** — the agent isn't running: no profile, or no token.
- **the task is `paused`** — five failures in a row pause a task; it said so in
  the conversation. `impi task resume <task>`.

Times are read in the task's own zone (`impi task show` prints it). The container
runs in UTC, so a schedule written without a zone means UTC — set
`SCHEDULER_TIMEZONE`. See [tasks.md](tasks.md).

## Changes to a profile don't take effect

- **Editing** an agent (`agent.yaml`, `SYSTEM.md`, skills) applies with a **reload**:
  `make reload`. Reload re-reads profiles and drops idle sessions; conversation
  memory survives.
- Adding a **new** agent requires a **restart** — agents are enumerated at startup.

## Stopping / stray subprocesses

`make stop` signals the engine (SIGTERM) so it can close its `pi` children, then
sweeps any orphaned `pi` processes left by a previous hard kill.

## Inspecting or clearing session state

The SQLite inventory maps conversations to `pi` session ids (it is inventory, not
the source of truth — `pi`'s on-disk memory is). To list or clean up sessions:

```bash
impi sessions list                       # every conversation, with its file count
impi sessions delete <agent> <conv>      # forget one
impi sessions purge-idle --days 30       # forget everything idle that long
```

The library ships the same three commands as `python -m crucible.sessions_cli`,
but that entry point resolves the database with `crucible`'s own default filename
— against an impi deployment it opens a file nobody writes and reports an empty
stand. Use `impi sessions`, or pass `--db`.
