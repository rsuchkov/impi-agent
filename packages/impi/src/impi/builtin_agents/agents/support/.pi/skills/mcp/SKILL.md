---
name: mcp
description: "Give an agent the tools of an MCP server, or explain why an MCP tool is missing, refused or withdrawn. Use when the operator wants to connect a server (GitHub, a tracker, an internal service), asks what trust or confirm mean, or reports a server's tools withdrawn, no snapshot, unreachable, or a tool the agent cannot see."
---

# MCP servers

An MCP server is a source of tools for the engine, declared once by the
operator and named by agents like any other tool. The engine is the client:
every `mcp__<server>__<tool>` stands in the registry beside the engine's own
tools and is called through the tool server — with the allowlist, the
confirmation card, the trace and the untrusted-result wrapping. The runtime
never talks to a server (`--no-mcp` stays). Reference: `$IMPI_ROOT/docs/mcp.md`.

You have no MCP tools of your own and do not need any: everything here is the
`impi mcp` CLI through `bash`, plus the files it writes.

## 1. Declare a server

```
impi mcp add github --url https://mcp.github.example/mcp \
    --header 'Authorization=Bearer ${MCP_GITHUB_TOKEN}' \
    --confirm 'delete_*' --safe 'get_*' --safe 'list_*' --description 'GitHub issues and pull requests'
impi mcp add stands --command 'uvx jira-stands-mcp' --env 'TOKEN=${MCP_STANDS_TOKEN}'
```

The command connects, lists the tools and writes `$AGENTS_PATH/_mcp/<name>.yaml`
(how to reach the server, `trust`, `confirm`, `safe`, `description`, `timeout`) and
`<name>.lock.json` (the **snapshot**: every tool's definition and a digest).
Both belong in the agents repository. The secret does not: `${MCP_…}` is read
when the server is connected from the engine's process environment or from
`conf/.env` — so `MCP_GITHUB_TOKEN=…` in `conf/.env` is enough. Only the
`MCP_` family may be named in an entry, and it never reaches an agent's
process. `${connection:<provider>}` in a header is the token of the person
whose turn it is (the **connections** skill).

A stdio server (`--command`) runs as a child of the engine, in the engine's
container: its runtime (`uvx`, `npx`, a binary) has to be in that image, or
`impi mcp add` fails with the OS error (`No such file or directory`) and a
call with `<name> could not be reached`.

A new or removed server, a changed entry (`trust`, `confirm`, `safe`,
`timeout`) and a tool a server gained need an engine restart (`impi restart`);
a refreshed snapshot is read at `impi reload`.

## 2. Give an agent the tools

In `agent.yaml`:

```yaml
runtime:
  tools:
    - mcp__github__*             # every pinned tool of the server
    - mcp__stands__list_stands   # or one by name
```

`mcp__<server>__*` is the one pattern `runtime.tools` accepts. Then
`impi reload`. A **confirmed** tool is advertised only where somebody can
confirm it (`INTEGRATIONS_ENABLED=true`) — like `create_agent`. Which tools
are confirmed: everything the entry names in `confirm:`; nothing it names in
`safe:`; of the rest, every tool of an untrusted server (the protocol says a
client must not decide by an untrusted server's hints), and on a trusted
server the tools the server does not mark `readOnlyHint` or
`destructiveHint: false` (absent annotations mean destructive). `impi mcp
show <name>` lists them with `needs confirmation`; `impi mcp add` says how
many. An operator whose reads all raise cards adds `--safe 'get_*'` (or a
`safe:` list in the yaml) after looking at what those tools do.

## 3. Trust

- `untrusted` (default): the snapshot is what the model is shown. The engine
  compares the live server with it — at start, every `MCP_RECHECK_S` seconds
  (300), at once when a stdio server says its tools changed, and at `impi
  reload`; a difference **withdraws every tool of that server** from every
  agent until a person reviews the change and runs `impi mcp refresh <name>`
  then `impi reload`. A server with no snapshot advertises nothing.
- `trusted` (`--trusted`): the server's definitions are taken as they come; the
  snapshot is rewritten at start and at every recheck. For a server your own
  team runs.

A description is text the model reads as an instruction; that is why the
default pins it.

## 4. A person's file

A tool whose argument the server declares as content (`contentEncoding:
base64` in the schema, pinned in the snapshot) takes a file the person
attached: the model gives the path from the `[attached]` line and the engine
sends the content (a top-level string argument; one nested in an object is
passed as written). `impi mcp add`, `show` and `refresh` mark such a tool
`takes a file (<arg>)`; every call of it is confirmed, whatever `safe:` says —
the card names the file, the type the platform claimed, size and digest.
Nothing to configure beyond `ATTACHMENTS_ENABLED` and `INTEGRATIONS_ENABLED`
being on. The type check: PNG, JPEG, GIF, WebP and PDF by their bytes; another
`contentMediaType` is judged by what the platform said of the file. A file
attached before v0.34.0 has no record and is refused — attach it again. A
stdio server may take a plain path and read the file itself instead.
Upload-by-id is not supported.

## 5. Diagnosing

Startup log line: `mcp: github(12 tools, untrusted), stands(3 tools, trusted, unreachable)`.

- `mcp <name>: its tools differ from the reviewed snapshot; N tool(s) withdrawn`
  (ERROR, at start or at a later recheck) — the server changed a tool; the
  tools stay withdrawn until a person reviews: show the operator `impi mcp
  show <name>` (the pinned definitions) and suggest comparing with the
  server's current list; when the change is fine, `impi mcp refresh <name>`
  then `impi reload` (`mcp <name>: its tools match the reviewed snapshot
  again; handed back`). A server that went back to its snapshot by itself
  stays withdrawn — nobody looked — and comes back at the next `impi reload`,
  which is the review.
- `mcp <name>: no snapshot of its tools` (ERROR; `impi mcp list` says
  `UNPINNED`) — the yaml exists without its lock (hand-written, or the lock was
  deleted): `impi mcp refresh <name>` and restart (a reload instead logs `N
  new tool(s) pinned; they appear at the next start` — the registry is built at
  start). A lock deleted while the engine runs withdraws an untrusted server's
  tools at the next reload (`its snapshot is gone; N tool(s) withdrawn`).
- `mcp <name>: not reached at startup` / `not reached` (WARNING, once; `reached
  again` at INFO when it is back) — the tools stay advertised and
  each call answers `<name> could not be reached; try again later`; check the
  URL or the command, the token in `conf/.env`, and for stdio that the runtime
  is in the image. The server's own words are in the engine log, never in the
  agent's answer. From a server that is up, after a minute, `<name> did not
  answer in time; the call may have run — check before repeating it`, with
  `no answer in 60s` (stdio) or `<url>: ReadTimeout` (HTTP) in the log — the
  call outran the server's time (the engine does not repeat a call that reached
  the server; the model is told to check first):
  `timeout: <seconds>` in `_mcp/<name>.yaml` (or `impi mcp add --timeout`)
  gives a slow server more.
- an agent lacks a tool it names — `impi mcp list` shows the server and its
  pinned count; the tool must be in the snapshot under exactly that name
  (`impi mcp show <name>`); a confirmed tool needs `INTEGRATIONS_ENABLED=true`
  — and on an untrusted server every tool not in `safe:` is confirmed, so
  with integrations off such a server advertises only its `safe:` tools, and
  no server a tool that takes a file;
- every call raises a card, reads included — the server is untrusted and the
  entry has no `safe:`; the operator reviews the read-only tools and names
  them (`--safe 'get_*'`), or declares the server `--trusted` when their own
  team runs it;
  a tool whose header is `${connection:…}` needs a connections provider
  configured.
- `<name> answered in a way the engine does not accept`, with `the server
  supports …; this client speaks 2026-07-28, 2025-11-25` in the log — a protocol
  version nobody shares. Report it; the server has to offer one of those.
- `<name> refused the call` — the server answered a JSON-RPC error (a capability
  this client lacks, an unknown tool), a 401/403 on its own credentials, or a
  403 on the person's token (which leaves their connection as it is); the
  message is in the engine log. `<name> no longer accepts the user's connection,
  so it has ended` — a 401 on the *person's* token (`${connection:…}`): the
  connection is forgotten, the person connects again (`/connections`).
- `the user has not connected their <provider> account, which <name> acts as
  them at` — the tool with a `${connection:…}` header in a direct message with
  someone who never connected; they open the connections command and connect.
- `<name> asked for something the engine does not do (more input mid-call)` —
  the server wanted a question answered mid-call (elicitation); not supported,
  by design.
- a tool with a `${connection:…}` header answers "works only in a direct
  message" in a channel or a thread in one — by design, as the connection
  tools; a thread inside the person's DM is a direct message for this.
- `cannot run: … is not a file the person attached in this conversation` — the
  model gave a path that is not from an `[attached]` line of this conversation
  (a file a tool wrote, another conversation's); said before any card is drawn,
  so the person is not asked to approve a file that is not there; `… is no longer here` — the conversation's
  files went (idle for `ATTACHMENT_RETENTION_DAYS`, or the conversation held
  more than `ATTACHMENT_CONVERSATION_MAX_MB` and the oldest made room), the
  person attaches it again; `… is not the file the person
  attached any more` — it was changed on disk since it arrived; `… takes
  application/pdf for document; … is image/png` — the bytes are not the type
  the schema names. All by design.

What is not there: resources, prompts, sampling, OAuth sign-in at a server.
