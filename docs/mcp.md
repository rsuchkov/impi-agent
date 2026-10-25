# MCP servers

An agent's tools can come from an [MCP](https://modelcontextprotocol.io)
server — GitHub, a tracker, a service a colleague's team runs — without a line
of engine code. The operator declares the server once; an agent names its
tools in `runtime.tools` like any other tool, and gets them with everything
the engine gives a tool: the allowlist, a conversation's entitlement, a
confirmation card, the trace, the result marked as a stranger's words.

The engine is the MCP client. The runtime never talks to a server (`pi` keeps
`--no-mcp`): a tool named `mcp__<server>__<tool>` is a tool of the engine's
registry that, when called through the tool server, calls the server. What a
server says about its tools is **pinned** when the server is declared and
advertised to the model from that snapshot, not from whatever the server says
today — a description is text the model reads as an instruction.

## Declare a server

```
impi mcp add github --url https://mcp.github.example/mcp \
    --header 'Authorization=Bearer ${MCP_GITHUB_TOKEN}' \
    --confirm 'delete_*' --confirm 'merge_*' --safe 'get_*' --safe 'list_*' --safe 'search_*' \
    --description 'GitHub issues and pull requests'
```

This connects to the server, lists its tools, and writes two files under
`$AGENTS_PATH/_mcp/` (beside `_extensions`, and `_skills` when the library lives there):

```yaml
# _mcp/github.yaml
url: https://mcp.github.example/mcp
headers:
  Authorization: Bearer ${MCP_GITHUB_TOKEN}
trust: untrusted
confirm: [delete_*, merge_*]
safe: [get_*, list_*, search_*]
description: GitHub issues and pull requests
timeout: 60        # seconds one call may take; 60 when omitted
```

and `_mcp/github.lock.json` — the snapshot: every tool's name, title,
description, input schema and annotations, with a digest over them and when
they were taken. Both files belong in the agents repository; the secret does
not — `${MCP_GITHUB_TOKEN}` is read when the server is connected from the
engine's process environment or, failing that, from `conf/.env`, the two
places every secret setting is read from. Only the `MCP_` family may be named:
a file in the agents directory is not the way the engine's other secrets
travel to an address of the file's choosing (and the family never reaches an
agent's process).

A server on stdio is a command instead of a URL:

```
impi mcp add stands --command 'uvx jira-stands-mcp' --env 'STANDS_TOKEN=${MCP_STANDS_TOKEN}'
```

The engine starts it as a child of its own — one process per server, shared by
every agent, given `PATH` and the variables the entry names and nothing else of
the engine's environment. A child that dies fails the call in flight as
unreachable; the next call opens a new one. Its runtime (`uvx`,
`npx`, a binary) has to be in the engine's image or on the host the engine
runs on.

`impi mcp list` shows every server, its trust and how many tools are pinned;
`impi mcp show <name>` the tools; `impi mcp refresh <name>` connects again and
pins the tools as they are now; `impi mcp remove <name>` forgets the server.
A refreshed snapshot is read at `impi reload`; a new or removed server, a
changed entry (`trust`, `confirm`, `safe`, `timeout`) and a tool a server
gained need an engine restart (the servers and the registry are read at start).

## Give an agent the tools

```yaml
runtime:
  tools:
    - read
    - mcp__github__*             # every pinned tool of the server
    - mcp__stands__list_stands   # or one at a time
```

`mcp__<server>__*` is the one pattern `runtime.tools` accepts: it stands for
the server's pinned tools, which the operator looked at when they declared it.
Everything else in the list is named one by one as before.

## Trust

Every server has a trust level, `untrusted` unless `--trusted` was given:

- **untrusted** — the snapshot is the truth. The engine connects, lists the
  tools and compares the digest — at start and again in the life of the
  process (below); a server whose tools differ has **all of them withdrawn**
  from every agent (the log says so, with `ERROR`), until someone reviews the
  change, runs `impi mcp refresh` and then `impi reload`, which hands them
  back. A server back to its snapshot on its own stays withdrawn: nobody
  looked. A server nobody pinned (`_mcp/<name>.yaml` without its lock)
  advertises nothing.
- **trusted** — the server's word is taken: a changed definition rewrites the
  snapshot and the tools follow it, at start and at every recheck. For a
  server your own team runs.

The check runs again every `MCP_RECHECK_S` seconds (300; 0 for no timer), not
more often than a server's own `ttlMs` on its listing allows; at once when a
server on stdio sends `notifications/tools/list_changed`; and at `impi
reload`, which is the one check that reads the snapshots from disk again — the
operator's review. A server that could not be reached is said once in the log,
and once more when it is reached again.

A server that cannot be reached at start keeps its tools advertised, as any
service that is down would; each call answers with the error
(`<server>: unreachable: …`) and the model is told. The engine reconnects on
the next call. A call that outlasts the server's `timeout` (60 s unless the
entry says otherwise — `impi mcp add --timeout 600` for a server that runs
reports) is the same failure.

## Confirmations

A tool is confirmed with the usual card — in every gateway the conversation is
live in, with "allow for…" windows and the ledger — unless somebody the engine
believes said it is harmless. Who may say so:

- the operator, in the server's entry: a tool matching `confirm:` (patterns
  with `*`) always asks, one matching `safe:` never does; `confirm:` wins when
  both match;
- a **trusted** server, through its annotations, read with the protocol's own
  defaults: a tool is destructive unless it is marked `readOnlyHint: true` or
  `destructiveHint: false` — a server that says nothing about a tool has said
  "may destroy", not "harmless";
- nobody else. An **untrusted** server's annotations are shown (the warning
  mark, `impi mcp show`) but decide nothing: the protocol tells a client never
  to decide by hints from a server it does not trust. So every tool of an
  untrusted server asks until the operator names it in `safe:`.

`impi mcp add`, `show` and `refresh` say which tools are confirmed and how
many; a server whose reads all ask is one `--safe 'get_*'` (and a restart)
away. The card shows the
tool's title, the server and the arguments: a short one as a fact on one line,
a long or multi-line one — a script, a comment's body — whole, as a passage,
so the person approves what they can read; a destructive tool gets the warning
mark. Where nothing can confirm (`INTEGRATIONS_ENABLED=false`) a confirmed
tool is not advertised at all — for an untrusted server that is everything
outside `safe:`.

## Acting as the person

A header can be the token of the person whose turn it is, from their
[connected account](connections.md):

```
impi mcp add gitlab --url https://gitlab.example/mcp --header 'Authorization=${connection:gitlab}'
```

The header's value is exactly `${connection:<provider>}` and goes out as
`Bearer <token>`; the URL must be `https`. The tool then needs the connections
capability (it is advertised only where connections are configured) and works
in a direct message with the person only, as the connection tools do; the
person's token is fetched for each call and sent on a connection of that call
alone — the server's standing connection, the one that lists its tools, never
carries it — and a person who has not connected their account is told so by
the tool, in words the model repeats. The model never sees the token.

## What a result is

Whatever a server answers is a stranger's text: every MCP tool is
`returns_untrusted`, so its description carries the note and its result comes
wrapped (`{"untrusted": true, "note": …, "data": …}`). A text answer is handed
over as text, a `structuredContent` as JSON; a result the server marks
`isError` is handed over the same way, as `{"error": true, "text": …}` — the
server's words stay data. When the server could not be called at all, the tool
fails in the engine's own words (`<server> could not be reached; try again
later`) and the server's message goes to the log, not to the model.

## Protocol revisions

The engine speaks the current revision of the protocol, **2026-07-28**
(stateless: every request carries its version, the client's identity and its
capabilities in `_meta`, and on HTTP the `MCP-Protocol-Version`, `Mcp-Method`
and `Mcp-Name` headers), and falls back to the handshake era (**2025-11-25**,
`initialize` and a session) when a server turns out to be of it: on HTTP a
modern request answered with a 400, 404 or 405 that carries no modern error, on stdio a
`server/discover` probe the server does not answer. A modern server that
names other supported versions (`-32022`) is spoken to in the newest one both
sides share; one that shares none is reported.

## What is not supported

Only `tools/list` and `tools/call`. Resources, prompts, sampling (a server
asking the model to generate) and elicitation (a server asking the person a
question) are not offered to a server: a request a legacy server sends back is
answered "method not found", and a result that asks for more input
(`input_required`) is a tool error. OAuth sign-in at a server is not supported
either: authenticate with a header, from the engine's environment or from the
person's connection.

## Where the pieces are

`crucible.mcp`: `protocol` (the client, HTTP and stdio), `library` (the `_mcp`
directory), `snapshot` (the pinned definitions and their digest), `tools` (a
definition as a tool of the engine), `service` (the servers connected, checked
and called). The composition root builds the registry from the snapshots
(`McpServers.registry_tools()`), starts the servers before the tool server,
rechecks them on a timer and at a reload (`McpServers.run`, `recheck`), and
applies what each check says: withdraws (`ToolWiring.withdraw`), hands back
what a review pinned again (`ToolWiring.restore`) and rewrites the manifests
when definitions moved (`ToolWiring.refresh_manifests`); see "Tools from MCP
servers" in [building-an-app.md](building-an-app.md).
