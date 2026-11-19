# The MCP gateway (the engine as an MCP server)

The `mcp` gateway lets **an agent on the outside** — a person's assistant in
their IDE, a workflow in another system, any MCP client — call the agents here
the way the engine calls an MCP server of its own: `tools/list`, `tools/call`,
over streamable HTTP. The tools it sees are the application's; `ask_agent` is
the one every deployment starts with: the agent itself, asked in words,
answering with a turn of its own in a conversation it remembers.

Who calls is a person. Every request carries a bearer token that a person
signed in for and consented to — at one of the application's connection
providers ([connections.md](connections.md)), any of them — and the turn runs
as that person: their memory, their entitlement, their connected accounts if
the application's `owner_for` says so. The client speaks for the person; it
is not them. A tool that needs their yes asks *them*, where the application
puts the card (a direct message in their chat), never the client.

It is a library gateway: `crucible.gateways.mcp`, wired by an application
built on the library ([building-an-app.md](building-an-app.md)). **`impi`
does not run it** — `AGENTS_GATEWAY__<AGENT>=mcp` in an `impi` deployment is
skipped with a word in the log.

## What a client goes through

1. `POST /mcp` without a token → `401`, `WWW-Authenticate: Bearer
   resource_metadata="<public>/.well-known/oauth-protected-resource/mcp"`.
2. The metadata names the authorization server (the same listener) and its
   metadata, `<public>/.well-known/oauth-authorization-server`: `/authorize`,
   `/token`, `/revoke`, `code` with PKCE `S256`, scopes `read` and `act`, no
   dynamic registration (`client_id_metadata_document_supported` is true: a
   client whose id is the URL of its metadata document is let in when the
   operator listed that URL).
3. `GET /authorize?response_type=code&client_id=…&redirect_uri=…&code_challenge=…&code_challenge_method=S256&scope=read act&state=…&resource=<public>/mcp`.
   A client not on the operator's list, or a redirect address not its own, is
   refused on a page and never redirected; anything else wrong goes back to
   the client as `error=` (`invalid_request` without PKCE, `invalid_scope`,
   `invalid_target` for another resource).
4. The person signs in at a connection provider — with several, they pick one
   on a page — through a sign-in connect: the provider names the account,
   nothing is stored, the provider's tokens are revoked at once.
5. The consent page: which client (by the operator's name and its id), as
   whom, for which scopes — `Allow` or `Deny`. Allowed → `302` to the client's
   redirect with `code`, `state` and `iss`; denied → `error=access_denied`.
6. `POST /token` (`grant_type=authorization_code`, `code`, `redirect_uri`,
   `client_id`, `code_verifier`, `resource`) → `access_token`,
   `refresh_token`, `expires_in`, `scope`. The code works once, within a
   minute, for that client, redirect and verifier.
7. `POST /mcp` with `Authorization: Bearer <access_token>`.

Access tokens last an hour (`access_ttl_s`), the grant twelve hours from the
sign-in (`grant_ttl_s`, counted from the code exchange, which follows the
sign-in within minutes): a refresh (`grant_type=refresh_token`) rotates both
tokens and never outlives the grant; after it the person signs in again. A
refresh token presented a second time ends the grant for good — somebody has
a copy. `POST /revoke` with either token ends it on purpose. Tokens are random
and kept as digests; the consent page is bound by a cookie to the browser
that signed in, so the client that started the request — which knows its id —
cannot consent in the person's place; the audit (logger
`crucible.mcp_authorization.audit`) names
clients and subjects and never a token, a code or a state.

## The protocol

Revision 2026-07-28, stateless: every request carries `MCP-Protocol-Version:
2026-07-28` and may carry `Mcp-Method` and `Mcp-Name`, which must say what the
body says (`-32020` otherwise). A client of another era — no version header, or an
older version, as a client that opens with `initialize` sends — is answered
`400` with `-32022` and `{"supported": ["2026-07-28"]}` and is not served
otherwise. One JSON-RPC request per POST
(a batch is `400`); a notification is `202` and nothing; `GET` and `DELETE`
are `405`; a browser `Origin` not in `allowed_origins` is `403`. Bodies up to
1 MiB.

| method | answers |
|---|---|
| `ping` | `{}` |
| `server/discover` | `supportedVersions`, `capabilities` (`tools`, `tasks`), `serverInfo`, `instructions` |
| `tools/list` | the tools the token's scopes reach, each with `execution.taskSupport` (`required` or `optional`) |
| `tools/call` | the tool's result: `content[0].text`, `isError`, and in `_meta` the conversation (below); with `params.task`, a task (below) |
| `tasks/get`, `tasks/result`, `tasks/cancel`, `tasks/list` | the caller's own tasks |
| anything else | `-32601` |

A call with `_meta.progressToken` and `Accept: text/event-stream` is answered
as an event stream: `notifications/progress` as the turn goes (the model's
remark, the running call's caption, "waiting for the person to confirm …"),
then the result. Without them, plain JSON when the turn is over. The engine's
own client (`crucible.mcp.protocol.McpClient` over `HttpTransport`) speaks to
the gateway as it is.

A tool an insufficient scope does not reach is not in `tools/list`, and a
call to it is `403` with `WWW-Authenticate: Bearer error="insufficient_scope",
scope="act"` and `-32602`.

## Conversations

A call that asks an agent runs in a conversation of the person's, named by a
handle: the result carries it in `_meta["crucible/conversation"]`, and the
next call puts the same key in `params._meta` to go on there — the agent's
memory of the conversation included. A call with no handle opens a new
conversation. The handle is the person's: the same one presented with another
person's token is another conversation, so there is no handle to steal. A
conversation answering one call refuses a second until it is done
(`isError`, "ask again once it is done").

The conversation is a direct message on the `mcp` gateway (kind `dm`); its
key in the session store is `mcp:<subject>:<handle>`. `tools_by_gateway: {mcp:
[...]}` in `agent.yaml` narrows what a conversation there may use, as on any
gateway.

## Tasks

A turn may take minutes and may stop to ask the person. A client that cannot
hold a request open that long asks for the call as a task — `params.task:
{"ttl": <ms>}` — and gets `{"task": {"taskId", "status": "working",
"createdAt", "lastUpdatedAt", "ttl", "pollInterval"}}` at once. `tasks/get`
reads the status off the turn: `working` with its latest word in
`statusMessage`, `input_required` while a card is up for the person
("waiting for the person's yes to deploy" — never the arguments), then
`completed`, `failed` (the tool itself failed) or `cancelled`. `tasks/result`
hands over the result — the same shape a plain call answers with — or waits
up to 25 seconds and says `-32002`, "still working". `tasks/cancel` stops the
turn through the agent's sink and ends the task; `tasks/list` names the
caller's own. A task is the person's and the client's together: another
client of the same person does not see it.

`ttl` is how long an ended task is kept for the client to read (ten minutes
when not asked, an hour at most); a working one is kept until it ends. A tool
that may wait on a person declares `requires_task`; called plainly, it answers
`isError` saying how to call it. Tasks live in the process: after a restart
the task is unknown and the client asks again.

## The two doors

A token's scopes are what the person let the client do:

- **`read`** — ask the agent, and use the tools that ask nobody for a yes.
  The turn runs with the agent's tools minus every one that would ask the
  person (`requires_confirmation`): `McpScopeEntitlements(spec_of, registry)`
  reads the scopes off the turn (`mcp_scopes` on its `TurnScope.attributes`)
  and narrows the entitlement; the gateway's own tools marked `scope = "act"`
  are not offered. A runtime's built-in (`bash`, `write`, `edit`) is not in
  the registry and asks nobody, so it stays: an operator who hands out `read`
  tokens to an agent with a shell narrows it with `tools_by_gateway: {mcp:
  [...]}` — the door takes away what asks, not what acts quietly.
- **`act`** — the whole agent, each confirmation put to the person. The card
  goes where the application delivers cards for this gateway
  (`ApprovalCards`: a direct message on the chat platform, say), with the
  headline saying it was asked through the client; the person answers it
  there, through the application's own gateway; the MCP client, meanwhile,
  sees `input_required` (as a task) or the progress word. The application's
  `PersonKey` must say that the token's subject and the chat user are one
  person, or the click is not theirs to make. Without `ApprovalCards` the
  turn has nobody to ask, and a tool that needs a yes is refused where it
  asks (`NO_APPROVER`).

The grant window a card offers ("allow for a while") is the gateway's own
ceiling: `TOOL_MAX_GRANT_S_BY_GATEWAY=mcp=0` offers none on cards an MCP turn
raises and honours none opened elsewhere (`TOOL_MAX_GRANT_S_BY_GATEWAY` in
[configuration.md](configuration.md)).

## Wiring (crucible)

```python
from crucible.conversation_profiles import GatewayEntitlements, McpScopeEntitlements
from crucible.gateways.mcp import AskAgentTool, McpHub
from crucible.mcp_authorization import AuthorizationServer, ClientRegistration

authorization = AuthorizationServer(
    store, connections, public_url="https://agents.example.com",
    clients={
        "https://ide.example/mcp-client.json": ClientRegistration("The IDE"),      # loopback redirects, any port
        "portal": ClientRegistration("Portal", redirect_uris=("https://portal.example/oauth/done",), loopback=False),
    },
    sign_in_providers=("okta",),           # any of the Connections' providers; all of them when None
    subject_of=lambda connected: (connected.account.email, connected.account.name),
    consent_page=MyConsentPage(),          # DefaultConsentPage when None
)
hub = McpHub(
    "0.0.0.0", 8430, authorization, public_url="https://agents.example.com",
    tools=(AskAgentTool(), MyActTool()), cards=SlackDmCards(...), routes=authorization.routes(),
    allowed_origins=(), instructions="Ask the assistant anything about the deployment.",
)
callback = CallbackServer(connections, sign_ins=authorization)   # the sign-in goes on to the consent page
factory = GatewayFactory(..., mcp_hub=hub)                       # GatewayConfig(kind="mcp") per agent
tracer = TracerByGateway({"http": http_hub.turns, "mcp": hub.turns}, default=...)
entitlements = GatewayEntitlements(spec_of, inner=McpScopeEntitlements(spec_of, registry, inner=...))
```

`store` is the engine's store (`McpGrants`: `mcp_auth_requests`, `mcp_grants`
in SQLite and MongoDB alike). The authorization server is
`crucible.mcp_authorization`, a module over the gateway and the connections
(the gateway itself knows only the `Authorization` port). A tool of the application's is any object with
`name`, `description`, `input_schema`, `scope`, `requires_task` and `async
run(call: GatewayCall) -> str`; `call.caller` says who and which client,
`call.arguments` what, `await call.ask(text, agent=...)` runs an agent's turn
in the call's conversation and answers with what the agent said, `await
call.progress(text)` tells a client that asked, and `CallFailed(text)` is the
answer when the call cannot do what was asked (`isError`). `presenter=`
turns the agent's Markdown into whatever the client should read. `McpHub.turns`
is the flow's tracer for the gateway, the same `HttpTurns` the http gateway
keeps its journals in.

## What it does not do

- No dynamic client registration: the operator lists clients. A client with
  a metadata document is listed by its URL; the document is not fetched.
- No `resources`, `prompts`, sampling or elicitation: tools only.
- No widgets, forms or hand-offs in an MCP turn: nothing on the other end
  can click, fill a form or be a conversation another agent joins, so the
  agent's `ask_user_*`, `open_form`, `open_screen`, `delegate` and
  `ask_agent` tools are not there in an `mcp` conversation (the handle's
  `denied_caps`).
- No session era: a client speaking 2025-11-25 or older is refused.
- Tasks are not kept across a restart; the conversation and its memory are.
- A loopback redirect is accepted on any port and path (RFC 8252); a client
  elsewhere needs its exact address listed.
- The person's answer to a card is relayed by the client to its own model;
  what the model does with the agent's words is the client's business
  ([security.md](security.md#known-limits)).
