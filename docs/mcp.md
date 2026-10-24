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
    --confirm 'delete_*' --confirm 'merge_*' --description 'GitHub issues and pull requests'
```

This connects to the server, lists its tools, and writes two files under
`$AGENTS_PATH/_mcp/` (beside `_skills` and `_extensions`):

```yaml
# _mcp/github.yaml
url: https://mcp.github.example/mcp
headers:
  Authorization: Bearer ${MCP_GITHUB_TOKEN}
trust: untrusted
confirm: [delete_*, merge_*]
description: GitHub issues and pull requests
```

and `_mcp/github.lock.json` — the snapshot: every tool's name, title,
description, input schema and annotations, with a digest over them and when
they were taken. Both files belong in the agents repository; the secret does
not — `${MCP_GITHUB_TOKEN}` is read from the engine's environment when the
server is connected (the `MCP_` family is the engine's own and never reaches
an agent's process).

A server on stdio is a command instead of a URL:

```
impi mcp add stands --command 'uvx jira-stands-mcp' --env 'STANDS_TOKEN=${MCP_STANDS_TOKEN}'
```

The engine starts it as a child of its own — one process per server, shared by
every agent, given `PATH` and the variables the entry names and nothing else of
the engine's environment — and starts it again if it dies. Its runtime (`uvx`,
`npx`, a binary) has to be in the engine's image or on the host the engine
runs on.

`impi mcp list` shows every server, its trust and how many tools are pinned;
`impi mcp show <name>` the tools; `impi mcp refresh <name>` connects again and
pins the tools as they are now; `impi mcp remove <name>` forgets the server.
A new or removed server needs an engine restart; a changed snapshot is read at
the next start too.

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

- **untrusted** — the snapshot is the truth. At every start the engine
  connects, lists the tools and compares the digest; a server whose tools
  differ has **all of them withdrawn** from every agent (the log says so, with
  `ERROR`), until someone reviews the change and runs `impi mcp refresh`. A
  server nobody pinned (`_mcp/<name>.yaml` without its lock) advertises
  nothing.
- **trusted** — the server's word is taken: a changed definition rewrites the
  snapshot at start and the tools follow it. For a server your own team runs.

A server that cannot be reached at start keeps its tools advertised, as any
service that is down would; each call answers with the error
(`<server>: unreachable: …`) and the model is told. The engine reconnects on
the next call.

## Confirmations

A tool is confirmed with the usual card — in every gateway the conversation is
live in, with "allow for…" windows and the ledger — when the server's entry
names it in `confirm:` (patterns with `*`), or when the server itself marks it
`destructiveHint`. The card shows the tool's title, the server and the
arguments as given; a destructive tool gets the warning mark. Where nothing can
confirm (`INTEGRATIONS_ENABLED=false`) such a tool is not advertised at all,
like any confirmed tool.

## Acting as the person

A header can be the token of the person whose turn it is, from their
[connected account](connections.md):

```
impi mcp add gitlab --url https://gitlab.example/mcp --header 'Authorization=${connection:gitlab}'
```

The tool then needs the connections capability (it is advertised only where
connections are configured), the person's token is fetched for each call and
sent on a connection of that call alone, and a person who has not connected
their account is told so by the tool, in words the model repeats. The model
never sees the token.

## What a result is

Whatever a server answers is a stranger's text: every MCP tool is
`returns_untrusted`, so its description carries the note and its result comes
wrapped (`{"untrusted": true, "data": …}`). A text answer is handed over as
text, a `structuredContent` as JSON; a result the server marks `isError` is a
tool error with the server's words quoted.

## Protocol revisions

The engine speaks the current revision of the protocol, **2026-07-28**
(stateless: every request carries its version, the client's identity and its
capabilities in `_meta`, and on HTTP the `MCP-Protocol-Version`, `Mcp-Method`
and `Mcp-Name` headers), and falls back to the handshake era (**2025-11-25**,
`initialize` and a session) when a server turns out to be of it: on HTTP a
modern request answered with a 400 that carries no modern error, on stdio a
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
(`McpServers.registry_tools()`), starts the servers before the tool server and
withdraws what the check says (`ToolWiring.withdraw`); see
[building-an-app.md](building-an-app.md).
