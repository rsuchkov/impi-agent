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

## A tool isn't available to an agent

- The agent must **name the tool** in `runtime.tools` — it's an allowlist.
- A typed tool is **dropped** (and logged) when the agent's gateway/config lacks a
  capability it requires — e.g. channel-admin tools on a Slack agent, or widget/form
  tools when `INTEGRATIONS_ENABLED=false`. Check the startup log for
  "tool … not advertised".
- The whole typed-tool server is off if `TOOL_ENABLED=false`.
- Skills need `read` + `bash` in `runtime.tools` to run.

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
uv run python -m crucible.sessions_cli --help
```
