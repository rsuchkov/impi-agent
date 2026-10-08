# Runtime notes (pi)

The engine drives the [`pi`](https://github.com/earendil-works/pi) coding agent as
its runtime backend. This file records the `pi` flags the engine passes and the
`pi` behaviors it relies on. Verified against `pi` 0.85.1 (the version `deploy/pi/package.json` pins).

## The argv the engine builds

For each turn the driver spawns `pi` roughly like this (see
`crucible/runtimes/pi/runtime.py`):

```
pi --mode rpc --approve
   --session-id <agent>--<conversation>     # or --no-session for a stateless run
   --session-dir <DATA_DIR>/pi-sessions/<agent>/<session-id>  # absent for a stateless run
   -e <engine tool-bridge> [-e <extension> ...] [-e <fallback extension>]
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
| `--session-dir` | the conversation's own session directory under `DATA_DIR` — one per conversation, so a confined process writes its memory and no other. |
| `-e <path>` | loads a TypeScript extension. The engine always loads its tool-bridge first, then the `AGENTS_PATH/_extensions/<name>/index.ts` named in `RUNTIME_EXTENSIONS` — none by default — and, for an agent with fallback models, the runtime's fallback extension (`runtimes/pi/extension/fallback.ts`). An agent's own container loads both of the runtime's extensions every time; the fallback one does nothing without its variable. |
| `--tools <csv>` | the ONE allowlist over built-in, extension, and typed tools. Empty = no tools at all. |
| `--no-skills` + `--skill` | disables ambient skill discovery, then loads exactly the agent's declared skills. |
| `--provider` / `--model` | select the backend and model (omitted when unset). |
| `--append-system-prompt` | appends text to the true system prompt — the engine uses it for gateway-specific formatting rules (e.g. Slack mrkdwn). |

`pi`'s working directory is the agent's profile dir, so it natively loads that
agent's `.pi/*`.

A confined process is started through the launcher instead —
`python -I -S <crucible>/confinement/launcher.py --readable … --writable …
-- pi …` — which applies the Landlock rules and then becomes `pi`. What the
runtime adds to the application's scope: the system paths, where `pi` and
node are installed, the profile, skills and extensions, files the environment
points at, its memory for this conversation; its agent directory
(`PI_CODING_AGENT_DIR`, set explicitly because `HOME` is the process's own)
with `auth.json` and `models-store.json` readable, room for the lock directory
`pi` takes beside them, and nothing writable there. `pi` creates either file
when it is missing, which a confined process cannot do, so the application
creates both, empty, at startup (`crucible.runtime_storage`). `pi` re-reads `auth.json`
before every model request and runs a key that begins with `!`, so a process
that could write it could have a command run in every other one; the
application refreshes an OAuth login instead (`runtimes/pi/auth_refresh.py`:
every 15 minutes, any login that expires within the hour, with `pi`'s own
code). A confined process then never finds a login it would have to refresh.

`impi escape <agent>` starts the same profile for a person in a terminal: the
flags from `--tools` down are built by the same code (`interactive_args`
beside `command_args` in `hosts/local.py`), without `--mode rpc`, the session
id and the tool bridge; `--approve` stays. The one difference in content: the
allowlist loses the tools only the engine could serve (its typed tools and the
bridge's `ask_user_confirm`), since nothing is there to answer them.

## Bumping `pi`

The version lives in one place, `deploy/pi/package.json`; `deploy/pi/package-lock.json`
pins every dependency with its integrity hash, and both images install from
the pair with `npm ci --ignore-scripts` (a mismatch between the two fails the
build). To bump: edit the version in `package.json`, run `make pi-lock`, rebuild
(`impi update` does), then re-verify the facts on this page against the new
version. `impi escape` reads the same file to compare the host's `pi`.

## Provider / model resolution

For each agent, provider and model are resolved in order:

1. the agent's `agent.yaml` `runtime.provider` / `runtime.model`, else
2. the engine default `DEFAULT_PROVIDER` / `DEFAULT_MODEL` (for the `support`
   agent, `SUPPORT_PROVIDER` / `SUPPORT_MODEL` first), else
3. nothing — the flag is omitted and `pi` uses its own configured default.

## Environment forwarded into `pi`

The subprocess env is built, not inherited: a base set taken from the engine's
environment (`PATH`, `HOME`, the user, locale and time zone, proxies, certificate
bundles, `PI_*`), then the names the deployment (`RUNTIME_ENV`,
`RUNTIME_ENV__<AXIS>`) and the profile (`runtime.env`) grant, then the engine's
shared extra env, then the agent's per-agent env, then a per-session value.
Everything else the engine holds stays in the engine. An engine-owned agent
(`support`) inherits the whole environment instead. Every process also gets
`PI_OFFLINE=1`, which stops `pi`'s startup version check, its download of `rg`
and `fd` for its `grep`/`find` tools (the images ship both), and its install of
a package `settings.json` names that is not installed yet.

- `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` (and `NODE_TLS_REJECT_UNAUTHORIZED`
  when `LLM_VERIFY_SSL=false`) — only when a custom endpoint is configured; consumed
  by `pi`'s provider extension.
- `TOOL_URL` / `TOOL_TOKEN` / `TOOL_MANIFEST` — how the tool-bridge extension reaches
  the tool-server as this agent (the token identifies the caller; the manifest lists
  its allowed tools). Plus, per session, `RUNTIME_SESSION_ID` and
  `TOOL_SESSION_PROOF` — the secret that makes the server believe the id, since
  the token alone is the agent's and the same in every conversation's process.
- `RUNTIME_FALLBACK_MODELS` — the agent's fallback models, `provider/model`
  comma-separated, without its own model and repeats; only for an agent that
  names some.

### Fallback models

`pi` retries a failed request by itself — three times, after 2, 4 and 8
seconds (`retry` in its settings) — when the failure is one it judges
transient: overloaded, 429, 5xx, a connection error, a timeout, a stream cut
short. A spent quota, a billing error and a refused key are not retried. The
retry continues the turn with the session's *current* model, and an extension
may change that model between the failure and the retry.

The fallback extension does exactly that. On a run that failed in a way `pi`
will retry (`isRetryableAssistantError`, the same classifier), it calls
`pi.setModel` with the next model of `RUNTIME_FALLBACK_MODELS`, and `pi`'s own
retry goes there. Nothing is prompted again, so the conversation's memory
holds the person's message once; the failed attempt stays in it as an answer
with an error. The next prompt starts on the model the turn started on, and a
new process starts on the profile's `--provider`/`--model` whatever the
session last used. The thinking level is `pi`'s to choose on the switch: the
model's own setting, else the configured default.

The engine logs the switch at INFO (`pi stderr: fallback: <from> failed
(<error>); switching to <to>`), and every turn's line names the model that
answered (`pi turn: … model=<provider/model>`). A model the runtime does not
know, or has no login for, is not switched to; the line says so.
- For engine-owned agents (`support`): `AGENTS_PATH` (their editable workspace) and
  `IMPI_ROOT` (the engine checkout, read-only, so `support` can diagnose the engine).

## Built-in tools & the working directory

`pi`'s built-in tools (`read`, `bash`, `edit`, `write`, `grep`, `find`, `ls`)
operate relative to the process's **current working directory**, and the engine
spawns `pi` with cwd = the agent's **profile directory** — that is what makes
`pi` load the agent's own `.pi/*` (SYSTEM.md, skills) natively. Consequences:

- Relative paths in prompts/skills resolve inside the profile dir; that's the
  right place for per-agent state (e.g. a skill writing `state/*.json`).
- Data that lives OUTSIDE the profile (a mounted repo checkout, a docs folder)
  is still reachable, but only by **absolute paths** — tell the agent the root
  in its SYSTEM.md (this is exactly how the bundled `support` agent works
  against `$AGENTS_PATH`). For tighter control, wrap access in your own typed
  tool with a pinned root.
- Standalone crucible apps can additionally point a whole turn at a checkout
  via the runtime port: `run_stateful(..., cwd=...)`. There is deliberately no
  per-agent `workdir` knob: moving the cwd away from the profile would stop
  `.pi/*` auto-loading and would auto-trust a foreign checkout's own `.pi/` —
  see the tracker before reaching for that.

## `pi` facts the engine relies on

- **Built-in tools** are exactly `read`, `bash`, `edit`, `write`, `grep`, `find`,
  `ls`, operating relative to the cwd (see above). `--tools` is a single
  allowlist over these plus extension tools and the engine's typed tools; naming
  a tool is the only way to enable it, and `--tools ""` yields no tools at all.
- **Skills** are `SKILL.md` capability packages the model reads on demand and drives
  via `bash` — so a skill needs `read` + `bash` in the agent's `tools`. `--skill`
  takes a path; the engine also accepts a bare skill name and resolves it to
  `<profile>/.pi/skills/<name>`.
- **Images ride with the prompt; other files do not.** The RPC `prompt`,
  `steer` and `follow_up` commands take an optional
  `images: [{type: "image", data: <base64>, mimeType}]`, which is how a photo
  someone sent reaches the model. There is no equivalent channel for any other
  file type (`@file` is an argv feature of the CLI, not of RPC), so the engine
  saves those to disk and names the path in the prompt text — see
  [files.md](files.md).
- **No web-search built-in.** `pi` provides web search as a *skill* (e.g. a
  Brave-search skill that needs an API key), not a typed tool. impi does not ship
  one yet.
- **No MCP.** `pi` deliberately has no MCP; the engine's own tools reach it through
  the tool-bridge extension instead.
- **`models.json` env interpolation is limited.** In `~/.pi/agent/models.json`,
  `$VAR` / `${VAR}` interpolation works only in `apiKey` and `headers` — **not**
  in `baseUrl`. A literal `"baseUrl": "$LLM_BASE_URL"` is used verbatim and every
  turn fails with `Invalid URL`; put a real URL there and keep only the secret in
  `apiKey`.
- **Packages.** `pi install <npm:|git:|https:|local>` installs extensions and/or
  skills; the engine does not depend on this, but agents can.
- **Config is baked at spawn.** `pi` reads `.pi/*` and the CLI flags when the
  subprocess starts, so applying a profile change means dropping idle sessions
  (hot-reload) — the on-disk memory persists per session id.

## Interrupting and forgetting a conversation

`PiRuntime.cancel(session_id)` sends pi an `abort` and waits (10 s by default,
`cancel_grace`) for the turn to end on pi's side: pi then emits its `agent_end`
with whatever the turn had produced, the interrupted `run_stateful` returns that
as its result, and the session stays usable — the next turn resumes the
conversation. This is deliberately different from what happens after a turn
*timeout*, where the session is poisoned and replaced: there the engine does not
know what pi will still send; here it asked and waited for the end. If pi does
not end the turn within the grace period, the session is dropped (the turn
fails like a crashed process would) and the memory on disk is left alone, so
the next turn still resumes in a fresh process. (Checked against pi 0.85.1: an
`abort` sent while a tool call waits on the engine ends the turn within seconds,
the tool's abort signal fires so the extension's call to the engine closes, and
the same session answers the next prompt. How pi words the ending changed on
the way: 0.80 ended the final message with `stopReason: aborted`, 0.82 and later
with `stopReason: error` and the text "This operation was aborted" — the driver
reads both as a cancel, never as a provider failure.)

`PiRuntime.reset(agent, session_id)` is the opposite: end the process now and
delete `<session_dir>/<agent>/<session_id>/` — its files and the directory — so
the next turn starts from nothing. `has_memory(agent, session_id)` reads the
same place. The layout lives in `runtimes/pi/spawn` (`conversation_memory_dir`
and the functions beside it), which the cleanup CLI and the retention sweep use
too.
