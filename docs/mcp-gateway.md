# The MCP gateway (the engine as an MCP server)

The `mcp` gateway lets **an agent on the outside** — a person's assistant in
their IDE, a workflow in another system, any MCP client — call the agents here
the way the engine calls an MCP server of its own: `tools/list`, `tools/call`,
over streamable HTTP. The tools it sees are the application's; `ask_agent` is
the one every deployment starts with: the agent itself, asked in words,
answering with a turn of its own in a conversation it remembers; `wait_agent`
is the way back for an answer that took longer than a call may.

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
   `/token`, `/revoke`, `code` with PKCE `S256`, scopes `read` and `act`, the
   `iss` parameter on the redirect, no dynamic registration
   (`client_id_metadata_document_supported` is true: a client whose id is the
   URL of its metadata document is let in when the operator listed that URL).
3. `GET /authorize?response_type=code&client_id=…&redirect_uri=…&code_challenge=…&code_challenge_method=S256&scope=read act&state=…&resource=<public>/mcp`.
   A client not on the operator's list, or a redirect address not its own, is
   refused on a page and never redirected; anything else wrong goes back to
   the client as `error=` (`invalid_request` without PKCE, `invalid_scope`,
   `invalid_target` for another resource).
4. The person signs in at a connection provider — with several, they pick one
   on a page — through a sign-in connect: the provider names the account,
   nothing is stored, the provider's tokens are revoked at once. The
   application turns the account into the subject the turns run as
   (`subject_of`), or answers the browser with a refusal page of its own.
5. The consent page: which client (by the operator's name and its id), as
   whom, for which scopes — `Allow` or `Deny`. The page is bound by a cookie
   to the browser that signed in: the client that started the request knows
   its id and cannot consent in the person's place. Allowed → `302` to the
   client's redirect with `code`, `state` and `iss`; denied →
   `error=access_denied`.
6. `POST /token` (`grant_type=authorization_code`, `code`, `redirect_uri`,
   `client_id`, `code_verifier`, `resource`) → `access_token`,
   `refresh_token`, `expires_in`, `scope`. The code works once, within a
   minute, for that client, redirect and verifier.
7. `POST /mcp` with `Authorization: Bearer <access_token>`.

Access tokens last an hour (`access_ttl_s`). The grant stands while the
client keeps using it — twelve hours from the last refresh (`grant_idle_s`) —
and a week from the sign-in at most (`grant_max_s`); a refresh
(`grant_type=refresh_token`) rotates both tokens and never outlives the
grant; after it the person signs in again. At every refresh the application
is asked whether the person still holds what the grant gives (`GrantPolicy`:
still employed, still in the group) — `ENDED` ends the grant, `UNAVAILABLE`
answers `503 temporarily_unavailable` and the client tries again. A refresh
token presented a second time ends the grant for good — somebody has a copy.
`POST /revoke` with either token ends it on purpose. Tokens are random and
kept as digests; the audit (logger `crucible.mcp_authorization.audit`) names
clients and subjects and never a token, a code or a state.

## The protocol

Revision 2026-07-28, stateless: every request carries `MCP-Protocol-Version:
2026-07-28` and may carry `Mcp-Method` and `Mcp-Name`, which must say what the
body says (`-32020` otherwise; a task's id is its name). A client of another
era — no version header, or an older version, as a client that opens with
`initialize` sends — is answered `400` with `-32022` and `{"supported":
["2026-07-28"]}` and is not served otherwise. One JSON-RPC request per POST
(a batch is `400`); a notification is `202` and nothing; `GET` and `DELETE`
are `405`; a browser `Origin` not in `allowed_origins` is `403`. Bodies up to
1 MiB. Every result carries `resultType` (`"complete"`, or `"task"` below).

| method | answers |
|---|---|
| `server/discover` | `supportedVersions`, `capabilities` (`tools`, and `extensions` with `io.modelcontextprotocol/tasks`), `serverInfo`, `instructions` |
| `tools/list` | the tools the token's scopes reach |
| `tools/call` | the tool's result — `content[0].text`, `isError`, the conversation in `structuredContent` and `_meta` (below) — or, for a long call, where its turn stands, or a task |
| `tasks/get`, `tasks/update`, `tasks/cancel` | the caller's own tasks, for a client that declared the extension (`-32021` otherwise) |
| anything else (`ping` among them) | `-32601` |

A call with `_meta.progressToken` and `Accept: text/event-stream` is answered
as an event stream: `notifications/progress` as the turn goes (the model's
remark, the running call's caption, "waiting for the person to confirm …"),
then the result. Without them, plain JSON. The engine's own client
(`crucible.mcp.protocol.McpClient` over `HttpTransport`) speaks to the
gateway as it is.

A tool an insufficient scope does not reach is not in `tools/list`, and a
call to it is `403` with `WWW-Authenticate: Bearer error="insufficient_scope",
scope="act"` and `-32602`.

## Conversations

A call that asks an agent runs in a conversation of the person's, named by a
handle. The result says it where the client's model reads — the last line of
the text, `Conversation: <handle>` — and as data, `structuredContent.conversation`
(and `_meta["crucible/conversation"]`); the next call puts the same handle in
the tool's `conversation` argument to go on there, the agent's memory of the
conversation included. A call with no handle opens a new conversation. The
handle is the person's: the same one presented with another person's token is
another conversation, so there is no handle to steal.

The conversation is a direct message on the `mcp` gateway (kind `dm`); its
key in the session store is `mcp:<subject>:<handle>`. `tools_by_gateway: {mcp:
[...]}` in `agent.yaml` narrows what a conversation there may use, as on any
gateway.

## A call that runs a turn

A turn may take minutes and may stop to ask the person; a call cannot be held
open that long by every client. A tool that runs a turn says so
(`long_running`; `ask_agent` does), and the gateway answers such a call one of
two ways, by what the client declared on the request:

- **A client with the tasks extension** (`io.modelcontextprotocol/tasks` in
  `_meta["io.modelcontextprotocol/clientCapabilities"].extensions`) gets a
  task at once: `{"resultType": "task", "taskId", "status": "working",
  "createdAt", "lastUpdatedAt", "ttlMs", "pollIntervalMs"}`. It polls
  `tasks/get` (`{"taskId"}`): `working` with the latest word in
  `statusMessage` — a card up for the person in their chat is `working` too,
  saying so, since the client has nothing to answer — then `completed` with
  the call's `result` (the same shape a plain call answers with, `isError`
  the tool's), `failed` with an `error`, or `cancelled`. `tasks/cancel` stops
  the turn, cooperatively; `tasks/update` is acknowledged and ignored, since
  no task here asks the client for input. An ended task is kept `ttlMs`
  (ten minutes); a working one until it ends.
- **Any other client** gets the answer when the turn ends within
  `call_wait_s` (45 seconds, under the minute some clients cut a call at) —
  else where the turn stands, as an ordinary result: "The agent is still
  working (now: …). Call wait_agent with conversation <handle> to get the
  answer." or "The agent is waiting for the person's yes in their chat (…)",
  with `structuredContent` `{conversation, state, step}` (`state` one of
  `working`, `awaiting_approval`, `done`, `failed`, `cancelled`). The client
  comes back with `wait_agent(conversation)`, which waits as long again and
  answers with the agent's words once the turn is done. A call into a
  conversation whose turn still runs joins that wait rather than failing, and
  the same request again with no conversation named, within two minutes
  (`repeat_window_s`), joins the turn it opened — a client whose stream broke
  asks again, and must not get a second turn.

Either way the turn runs on the replica the call landed on and is written
down as it goes (`mcp_turns` in the store: its state, its step, its answer),
so the next call about it may land on any replica: a poll, a wait or a cancel
reads the record, and the replica that runs the turn reads the cancel. A turn
whose replica is gone is `failed` ("the engine running this turn restarted"),
not waited for; a replica coming back closes what it was running.

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
  headline saying it was asked through the client — by the operator's name
  for it (`ClientRegistration.name`), else its id; the person answers it
  there, through the application's own gateway; the MCP client, meanwhile,
  sees `working` with the word on the wait, or `awaiting_approval`. The
  application's `PersonKey` must say that the token's subject and the chat
  user are one person, or the click is not theirs to make. Without
  `ApprovalCards` the turn has nobody to ask, and a tool that needs a yes is
  refused where it asks (`NO_APPROVER`).

The grant window a card offers ("allow for a while") is the gateway's own
ceiling: `TOOL_MAX_GRANT_S_BY_GATEWAY=mcp=0` offers none on cards an MCP turn
raises and honours none opened elsewhere (`TOOL_MAX_GRANT_S_BY_GATEWAY` in
[configuration.md](configuration.md)).

## Wiring (crucible)

```python
from crucible.conversation_profiles import GatewayEntitlements, McpScopeEntitlements
from crucible.gateways.mcp import AskAgentTool, McpHub, WaitAgentTool
from crucible.mcp_authorization import AuthorizationServer, ClientRegistration, Subject

async def subject_of(connected, request):
    return Subject(connected.account.email, connected.account.name)   # or a refusal page of your own

authorization = AuthorizationServer(
    store, connections, public_url="https://agents.example.com",
    clients={
        "https://ide.example/mcp-client.json": ClientRegistration("The IDE"),      # loopback redirects, any port
        "portal": ClientRegistration("Portal", redirect_uris=("https://portal.example/oauth/done",), loopback=False),
    },
    sign_in_providers=("okta",),           # any of the Connections' providers; all of them when None
    subject_of=subject_of, policy=MyGrantPolicy(),                      # asked at every refresh
    consent_page=MyConsentPage(),          # DefaultConsentPage when None
    grant_idle_s=12 * 3600, grant_max_s=7 * 24 * 3600,
)
hub = McpHub(
    "0.0.0.0", 8430, authorization, store=store, public_url="https://agents.example.com",
    tools=(AskAgentTool(), WaitAgentTool(), MyActTool()), cards=SlackDmCards(...), routes=authorization.routes(),
    replica=replica, replicas=store,       # several pods: who runs a turn, who is alive
    call_wait_s=45.0, instructions="Ask the assistant anything about the deployment.",
)
callback = CallbackServer(connections, sign_ins=authorization)   # the sign-in goes on to the consent page
factory = GatewayFactory(..., mcp_hub=hub)                       # GatewayConfig(kind="mcp") per agent
tracer = TracerByGateway({"http": http_hub.turns, "mcp": hub.turns}, default=...)
entitlements = GatewayEntitlements(spec_of, inner=McpScopeEntitlements(spec_of, registry, inner=...))
```

`store` is the engine's store: `McpGrants` (`mcp_auth_requests`, `mcp_grants`)
for the authorization server and `McpTurns` (`mcp_turns`) for the hub, in
SQLite and MongoDB alike. The authorization server is
`crucible.mcp_authorization`, a module over the gateway and the connections
(the gateway itself knows only the `Authorization` port). A tool of the
application's is any object with `name`, `description`, `input_schema`,
`scope`, `long_running`, `conversation_argument` and `async run(call:
GatewayCall) -> str`; `call.caller` says who and which client,
`call.arguments` what, `await call.ask(text, agent=...)` runs an agent's turn
in the call's conversation and answers with what the agent said, `await
call.wait(handle)` waits for a conversation's turn (`TurnState`), `await
call.progress(text)` tells a client that asked, and `CallFailed(text)` is the
answer when the call cannot do what was asked (`isError`). `presenter=`
turns the agent's Markdown into whatever the client should read. `McpHub.turns`
is the flow's tracer for the gateway, the same `HttpTurns` the http gateway
keeps its journals in.

## What it does not do

- No dynamic client registration: the operator lists clients. A client with
  a metadata document is listed by its URL; the document is not fetched.
- No `resources`, `prompts`, sampling or elicitation: tools only. No
  `notifications/tasks` or `subscriptions/listen`: a task is polled.
- No `input_required` task or result: a card is answered by the person in
  their chat, never by the client.
- No widgets, forms or hand-offs in an MCP turn: nothing on the other end
  can click, fill a form or be a conversation another agent joins, so the
  agent's `ask_user_*`, `open_form`, `open_screen`, `delegate` and
  `ask_agent` tools are not there in an `mcp` conversation (the handle's
  `denied_caps`).
- No session era: a client speaking 2025-11-25 or older is refused, and so
  is the tasks form of that revision (`params.task`, `tasks/result`).
- A loopback redirect is accepted on any port and path (RFC 8252); a client
  elsewhere needs its exact address listed.
- The person's answer to a card is relayed by the client to its own model;
  what the model does with the agent's words is the client's business
  ([security.md](security.md#known-limits)).
