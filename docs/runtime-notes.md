# Runtime notes (pi)

The engine drives the [`pi`](https://github.com/earendil-works/pi) coding agent as
its runtime backend. This file records the `pi` flags the engine passes and the
`pi` behaviors it relies on. Verified against `pi` 0.80.3.

## The argv the engine builds

For each turn the driver spawns `pi` roughly like this (see
`crucible/runtimes/pi/runtime.py`):

```
pi --mode rpc --approve
   --session-id <agent>--<conversation>     # or --no-session for a stateless run
   --session-dir <DATA_DIR>/pi-sessions/<agent>
   -e <engine tool-bridge> [-e <extension> ...]
   --tools <csv>                            # the single allowlist
   --no-skills [--skill <path> ...]
   [--provider <p>] [--model <m>]
   [--append-system-prompt <text>]
```

| Flag | Why |
|---|---|
| `--mode rpc` | line-delimited JSON over stdin/stdout — the engine's transport. |
| `--approve` | RPC mode otherwise ignores the project's `.pi/` resources; approving loads them (`SYSTEM.md`, settings) from the agent's profile dir non-interactively. |
| `--session-id` | resumes the agent's on-disk memory for this conversation; `--no-session` for a memoryless run. |
| `--session-dir` | per-agent session storage under `DATA_DIR`. |
| `-e <path>` | loads a TypeScript extension. The engine always loads its tool-bridge first, then any `AGENTS_PATH/_extensions/*/index.ts`. |
| `--tools <csv>` | the ONE allowlist over built-in, extension, and typed tools. Empty = no tools at all. |
| `--no-skills` + `--skill` | disables ambient skill discovery, then loads exactly the agent's declared skills. |
| `--provider` / `--model` | select the backend and model (omitted when unset). |
| `--append-system-prompt` | appends text to the true system prompt — the engine uses it for gateway-specific formatting rules (e.g. Slack mrkdwn). |

`pi`'s working directory is the agent's profile dir, so it natively loads that
agent's `.pi/*`.

## Provider / model resolution

For each agent, provider and model are resolved in order:

1. the agent's `agent.yaml` `runtime.provider` / `runtime.model`, else
2. the engine default `DEFAULT_PROVIDER` / `DEFAULT_MODEL` (for the `support`
   agent, `SUPPORT_PROVIDER` / `SUPPORT_MODEL` first), else
3. nothing — the flag is omitted and `pi` uses its own configured default.

## Environment forwarded into `pi`

The subprocess env is layered: the process env, then the engine's shared extra env,
then the agent's per-agent env, then a per-session value.

- `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` (and `NODE_TLS_REJECT_UNAUTHORIZED`
  when `LLM_VERIFY_SSL=false`) — only when a custom endpoint is configured; consumed
  by `pi`'s provider extension.
- `TOOL_URL` / `TOOL_TOKEN` / `TOOL_MANIFEST` — how the tool-bridge extension reaches
  the tool-server as this agent (the token identifies the caller; the manifest lists
  its allowed tools). Plus `RUNTIME_SESSION_ID` per session.
- For engine-owned agents (`support`): `AGENTS_PATH` (their editable workspace) and
  `IMPI_ROOT` (the engine checkout, read-only, so `support` can diagnose the engine).

## `pi` facts the engine relies on

- **Built-in tools** are exactly `read`, `bash`, `edit`, `write`, `grep`, `find`,
  `ls`. `--tools` is a single allowlist over these plus extension tools and the
  engine's typed tools; naming a tool is the only way to enable it, and `--tools ""`
  yields no tools at all.
- **Skills** are `SKILL.md` capability packages the model reads on demand and drives
  via `bash` — so a skill needs `read` + `bash` in the agent's `tools`. `--skill`
  takes a path; the engine also accepts a bare skill name and resolves it to
  `<profile>/.pi/skills/<name>`.
- **No web-search built-in.** `pi` provides web search as a *skill* (e.g. a
  Brave-search skill that needs an API key), not a typed tool. impi does not ship
  one yet.
- **No MCP.** `pi` deliberately has no MCP; the engine's own tools reach it through
  the tool-bridge extension instead.
- **Packages.** `pi install <npm:|git:|https:|local>` installs extensions and/or
  skills; the engine does not depend on this, but agents can.
- **Config is baked at spawn.** `pi` reads `.pi/*` and the CLI flags when the
  subprocess starts, so applying a profile change means dropping idle sessions
  (hot-reload) — the on-disk memory persists per session id.
