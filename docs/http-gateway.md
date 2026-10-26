# The http gateway (a request/turn API)

The `http` gateway lets **a program that cannot hold a socket open** talk to
agents: a chat panel in a browser behind someone else's server, a caller spread
over several nodes, anything that has to work in short independent HTTP calls.
It is the ws gateway's sibling — your program, no chat platform — with a
different shape: post a message, get a turn id, poll the turn's events, answer a
card, come back after a page reload and find the turn still running.

What the engine owns here: running the turn, its journal, busy/idempotency,
resuming, confirmations. What stays yours: who the caller is (a token and
headers by default; your own check on the library), and whether you keep a
transcript of your own — the engine keeps the agent's memory, not a transcript
for your UI.

## Wiring

1. Put an agent on the http gateway: `AGENTS_GATEWAY__<AGENT>=http` (or
   `impi agent add --gateway http`). No per-agent token — access is authorized
   per caller.
2. Register a caller: `impi http add-caller my-app [--agents a,b]` — or by hand:
   ```
   HTTP_CALLER_TOKEN__MY_APP=<secret>
   HTTP_CALLER_AGENTS__MY_APP=helper,scribe   # optional; unset = all http agents
   ```
3. Restart the engine. The hub starts when some agent runs on `http` (or the
   engine is a replica — `REPLICA_URL`, `REPLICA_TOKEN`),
   listening on `HTTP_HOST:HTTP_PORT` (default `0.0.0.0:8428`; 8427 is the agent containers' relay). Like the ws
   hub's port it is not published by the compose files; publish it (or front it
   with your proxy) where your program can reach it.

The program is trusted to say who its user is. That is the right trust when the
program is your own frontend — a server that already authenticated the person
and forwards their identity. When the caller has to *prove* the user (a session
cookie checked upstream, a signed assertion), build on the library instead and
give the hub your own `CallerAuthenticator` — see
[building-an-app.md](building-an-app.md).

## Contract (v1)

Every change to v1 is additive: ignore fields and event types you do not know.
Every response carries `X-Engine-Api-Version: 1.0`.

### Headers on every request

| header | value |
|---|---|
| `Authorization` | `Bearer <caller token>` |
| `X-User-Id` | who is talking — the person's id in **your** system (default `user`) |
| `X-Username` | how to address them (default: the id) |

A conversation id is yours (the person's id, a page, a ticket); a different id
is a different conversation with its own memory. Both are namespaced by the
caller, so two programs never reach each other's conversations.

### Errors

```json
{"error": {"code": "turn_in_progress", "message": "…"}, "turnId": "…"}
```

| code | HTTP | when |
|---|---|---|
| `unauthorized` | 401 | no accepted token |
| `forbidden` | 403 | the agent does not exist on this hub, or this caller may not address it |
| `not_found` | 404 | unknown turn, another person's turn, a retired control, a question nobody waits on any more, nothing running to cancel, interactivity off, an engine not wired to list engine-started turns |
| `turn_in_progress` | 409 | this conversation already has a turn running; `turnId` names it |
| `not_cancellable` | 409 | the turn cannot be interrupted here (no runtime behind it yet) |
| `not_available` | 409 | a form was asked to open; forms are chat-only |
| `draining` | 503 | the engine is shutting down and takes no new messages; polls and answers still work |
| `validation` | 422 | a bad body or query value; `text` over 32 000 characters; a menu answer that is not one of its options |
| *(the authenticator's own)* | any | an application's `CallerAuthenticator` may answer with a status and code of its own (`CallerRefused`) — a session that expired, an identity that does not match, a caller over its rate; same envelope |
| `too_large` | 413 | the request body is over the cap (text-only: 1 MiB; with attachments on, a few files of `ATTACHMENT_MAX_MB` each) |

### `POST /v1/agents/{agent}/conversations/{conversation}/messages`

```json
{"clientMessageId": "uuid-or-anything-stable", "text": "…",
 "files": [{"name": "photo.jpg", "mime": "image/jpeg", "data": "<base64>"}]}
```

- `202 {"turnId", "conversationId", "cursor": 0}` — a new turn started.
- `200` with the same body — `clientMessageId` was already accepted: a retry
  returns the turn it started, not a second one.
- `409 turn_in_progress` with `turnId` — follow that turn instead.

`clientMessageId` is required (1–128 characters of `A-Za-z0-9_.:-`) and is what makes a
submit idempotent; generate one per message, reuse it on retry. `text` may be
empty only when files are attached — a photo is a message. The message is
stamped with the time the hub accepted it, and the model reads that time in
the prompt — it is how the agent knows what "today" is.

Files travel inline, base64, like on the ws gateway, and are handled like any
other attachment ([files.md](files.md)): saved under the agent's files, named
by path in the prompt, and a **picture is also shown to the model directly**
(up to `INLINE_IMAGE_MAX_MB` each, a few per message). A file over
`ATTACHMENT_MAX_MB` is logged and skipped; undecodable base64 is a `422`. All
of it needs `ATTACHMENTS_ENABLED=true` — with attachments off, files in a
request are ignored and the agent has no `send_file`. In the other direction,
`send_file` becomes a `file` event, one per file.

### `GET /v1/turns/{turnId}/events?after=N&wait=S`

`after` — return events with `seq > after` (default 0). `wait` — long-poll
seconds when nothing new is there yet; **capped by `HTTP_MAX_WAIT_S`** (8),
whatever you ask, because every waiting poll holds a connection.

`200 {"turnId", "status", "events": [...], "cursor"}` — `cursor` is the last
`seq` returned (or `after` when none). Poll with `after=cursor`. The journal
answers for a retention window after the turn ends (10 minutes).

Status: `running` | `awaiting_input` (a card is up) | `done` | `failed`.

### Events

Every event has `seq` (strictly increasing within a turn, from 1), `at`
(ISO-8601 UTC) and `type`:

| type | payload | meaning |
|---|---|---|
| `turn.started` | `startedBy` | the turn opened: `caller` for a message you posted, `engine` for one the engine started by itself (a scheduled task, an event the application reported) |
| `tool.started` | `callId`, `tool`, `caption` | a tool began; `caption` is its one line for a person ("Reading PROJ-123"), written by the tool, `""` when it has none |
| `tool.finished` | `callId`, `tool`, `ok` | it ended |
| `progress` | `text` | what the model said on its way to the next call ("Found the workflow, now listing transitions") — a step's heading, not the answer; the answer is the `message` after the last call |
| `message` | `messageId`, `format: "markdown"`, `text` | the agent's answer |
| `notice` | `code`, `text` | the engine speaking: `timeout`, `busy`, `quota`, `credentials`, `context`, `model_unavailable`, `runtime_unavailable`, `agent_error`, `empty_answer`, `stalled` (the model stopped responding mid-turn and the turn was stopped), `truncated` (the answer hit the model's length limit: the `message` before it stops where the limit fell, or there was none) — or a flag a tool left on the turn (`session_expired`, whatever the application defines), after the answer; `text` is a sentence for a person, `code` is for you |
| `actions` | `postId`, `text`, `actions[]` | a question with controls — what the runtime asks mid-turn (`ask_user_confirm`); the turn waits (`awaiting_input`) |
| `confirmation` | `postId`, `agent`, `icon`, `headline`, `title`, `tool`, `fields[{label, value, before, passage}]`, `block {label, text} \| null`, `danger`, `expiresAt`, `verdict`, `grantS`, `actions[]` | a request for a yes, as data — a tool that needs confirming, a credential an agent asks for. Nothing is rendered: draw the headline, `title` (the one-line account of the call, "" when the card is not about a call) and `tool` (the id of the tool being confirmed, "" for a credential), the facts (a `before` means a change; `passage` means the value is text to show whole, with its lines), the verbatim block (a command line, raw arguments — show it as written), a warning when `danger`, a countdown to `expiresAt`; the turn waits (`awaiting_input`) |
| `confirmation.updated` | `postId`, the same fields as `confirmation`, `actions[]` | that card rewritten — the verdict once it is answered, has expired or was withdrawn (a tool call or a credential alike): in `headline` as a sentence, and in `verdict` as a code to draw your own line from — `allowed_once`, `allowed_for` (for `grantS` seconds), `denied`, `expired`, `withdrawn`; `""` while the card still asks; a notice folding another occurrence into itself (a `Times` field). Redraw the post; empty `actions` means nothing is left to click and the turn no longer waits. A `confirmation` posted with empty `actions` in the first place is a notice: told, not asked, and the turn does not wait |
| `actions.retired` | `postId`, `text` | the controls of an `actions` post are gone; show `text` in their place |
| `cards` | `postId`, `cards[{text, accent, actions[]}]` | a message built from cards; the same `postId` again means redraw it |
| `file` | `name`, `mime`, `data` (base64), `text` | a file the agent sent (the caption rides with the first) |
| `turn.finished` | `outcome` | the last event: `replied`, `acted` (a speaking tool — a widget, a form — answered for it), `truncated` (the answer hit the model's length limit; what there was came as `message`, then a `notice`), `empty`, `duplicate`, `timeout`, `error` |

An action: `{id, label, value, style, kind, options[{label, value}]}`. `kind` is
`button`, `select`, `user_select` or `channel_select`; a button echoes its
`value`, a select echoes the chosen option's value. Render the text with
`textContent` — a card's content is partly the agent's, and the agent reads
what other people wrote.

### `POST /v1/turns/{turnId}/actions`

```json
{"postId": "…", "actionId": "yes", "value": "Allow"}
```

`200 {"outcome": "resolved" | "redrawn" | "not_mine" | "not_allowed"}` (a card the conversation holds outside a turn is answered by the conversation's `actions`, below), and on
a screen's control that tells the clicker something alone, `"privateNote"` with
it — show it to this caller only; it is in no event. A
button answers with its own value (the body's `value` is ignored for it); a menu
answers with one of its options, anything else is `422`. A click on a
confirmation card for a tool call is answered by the user whose message
started the turn (the `X-User-Id` that message carried), or by the people
`TOOL_APPROVERS` names — anyone else gets `not_allowed`; a request for a *credential* is
addressed to named approvers by **user id**, which on this gateway is what the
caller put in `X-User-Id` — so the program's claim about its user decides who
may approve a credential, and refuses everyone else (`not_allowed`, the card
stays live for them). After a click the control is retired — an `actions`
post with `actions.retired`, a `confirmation` with `confirmation.updated`
carrying the verdict and no actions; clicking it again is `404`, as is a click on a question nobody waits on any
more (answered elsewhere, timed out, withdrawn) and any click on a turn that has
ended.

### `GET /v1/agents/{agent}/conversations/{conversation}`

`200 {"conversationId", "activeTurn": {"turnId", "status", "cursor"} | null,
"pendingCards": [...]}` — for resuming after a page reload: poll the active
turn from its cursor. `pendingCards` are the confirmation cards the
conversation holds **outside any turn of this gateway's**: a conversation live
on this gateway and on a chat platform too (one agent, several gateways) has
its turn on the platform ask for a yes, and the card is forwarded here as to
every gateway the conversation is live in. Each is the `confirmation` event's
payload (`postId`, `headline`, `title`, `tool`, `fields`, `block`, `danger`,
`expiresAt`, `actions[]`, …) and stays until its verdict comes, it is
withdrawn or its `expiresAt` passes; listed oldest first. The cards are kept in the engine's store, so with
several pods every pod lists them and takes an answer — the question itself
waits on the pod whose turn raised it, and that pod is asked whether the
caller may answer before the card is shown. Listed only to a caller the card is
addressed to — the person who asked, as the approvals judge `X-User-Id` on
this gateway — another user of the realm sees the conversation and no card:
its arguments are the asker's. A card raised by a turn running *here* is in
that turn's journal instead, as before. Poll this when the program shows a
conversation that may be live elsewhere too.

### `POST /v1/agents/{agent}/conversations/{conversation}/actions`

```json
{"postId": "…", "actionId": "yes"}
```

The answer to a pending card of the conversation — the same body as a turn's
actions. `200 {"outcome": "resolved" | "not_mine" | "not_allowed"}` with the
same meaning as there: who may answer is the person the card is addressed to,
as the approvals judge the caller's `X-User-Id` on this gateway; `not_mine`
means it was answered elsewhere first (the card is let go). `404` when the
conversation holds no such card or control — answered, expired, withdrawn —
`409` for a control that is not a confirmation's.

### `GET /v1/agents/{agent}/conversations?startedSince=<ISO-8601>`

`200 {"conversations": [{"conversationId", "startedAt"}]}` — the caller's
conversations (their realm, their user) in which the **engine** started a turn
at or after `startedSince`, newest first: one request for a badge or an unread
mark, instead of polling every conversation the program knows. Answered from
the engine's session inventory, so it survives a restart. `404` when the
engine is not wired to answer it (`HttpHub(engine_turns=…)`), `422` for a
time that does not parse.

### `POST /v1/agents/{agent}/conversations/{conversation}/cancel`

Interrupts the running turn; `200 {"turnId", "cancelled"}`. The turn ends with
what it had (its `turn.finished` follows) and the conversation keeps its
memory. `404` when nothing is running.

### `GET /v1/agents`

The agents this caller may address: `{"agents": [{"name", "role", "description"}]}`.

### `GET /healthz`, `GET /readyz`, `GET /stats`

Unauthenticated. `readyz` is `503` while the engine starts or drains, or while
one of the application's own readiness probes says it cannot serve — **not** while
every runtime slot is busy. A full pool is a busy pod, not a broken one: a new
turn waits for a slot or evicts an idle session, and a load balancer that took
the pod out of rotation (a Kubernetes readiness probe does exactly that) would
move its users away from the turns running on it — with a per-user consistent
hash, onto a pod that has none of their state. Load belongs to whoever scales
or watches: `stats` answers `{"runtime": {alive, busy, capacity, waiting} |
null, "turns": {"active"}}`.

An application adds its own probes with `HttpHub(readiness_probes=[…])`: each
is `async () -> str | None`, `None` when it may take requests, else a short
reason that the `503` carries as `reason`. A probe that raises or takes over two
seconds counts as a "no" (`"<name>: failed"`, `"<name>: timed out"`); what it
raised goes to the log, not into the response. Whether a dependency belongs
there is a choice: taking a pod out of rotation moves its users, which cures
nothing when every pod shares the dependency that is down.

## Routes of your own

An application usually has a few endpoints beside the engine's — the transcript
its panel draws after a reload, a "new dialog" button. They belong on the
**same port**: a load balancer that pins a user to a pod does it per
`ip:port`, so a second port would send the same user to a different pod,
away from their running turn. Hand them to the hub:

```python
async def dialog(request):
    caller = await hub.caller(request, None)          # 401 in the envelope when unknown
    key = hub.conversation_key(caller, request.match_info["conversation"])
    ...
    raise hub.refuse(404, "no_dialog", "nothing stored yet")   # your own code, same envelope

hub = HttpHub(..., routes=[web.get("/v1/me/{conversation}/dialog", dialog)],
              middlewares=[served_by])
```

Your routes get the contract's error envelope and version header; your
middlewares run on every route, the engine's included; `hub.max_body` is the
request-size cap in force. Your middlewares run *inside* the envelope: a
refusal raised by a handler (401, 404, 409, 503, your own `hub.refuse`) passes
through them as an exception and becomes a response only in the envelope, so
code after `await handler(request)` in your middleware sees successes only —
catch `Refusal` there if an error response needs your header too.

## Several pods

The hub also carries three routes of the engine's own, `POST
/v1/internal/turns`, `POST /v1/internal/clicks` and `POST
/v1/internal/may-answer`, under `REPLICA_TOKEN` (see
[configuration.md](configuration.md), "Several engine processes"): what one
engine process hands another — a turn for a conversation the other owns, a
click on a card the other posted, the question whether a person may answer a
card the other asked. They answer only that token; a caller's
token is refused there. A caller's own turns are never forwarded: the pod a
balancer pins a user to is where their conversation runs, and the conversation
moves there when it was elsewhere.

## Draining a pod

Before a pod stops, `hub.drain()` makes it refuse new messages (`503
draining`) and report not ready, while polls, answers and cancels keep working
and the turns already running finish; `await hub.idle(timeout)` waits for
them (False when some still run). A retry of a message this pod already
answered is not a new message: it gets its turn id back as usual, so a client
that lost the first response follows its turn here instead of starting it
again elsewhere. The engine does this itself on shutdown — on SIGTERM, which is
what a container runtime sends, as on Ctrl+C — for `HTTP_DRAIN_S` seconds (20);
keep the orchestrator's grace period above that — a turn waiting on a
confirmation can take minutes.

## Semantics worth knowing

- **One turn at a time, per conversation.** Chat gateways merge messages that
  arrive mid-turn into the next turn; here a second message is refused with the
  running turn's id, because a program can follow a turn and a merged answer
  would surprise it.
- **Idempotency survives the hub's memory, not a restart.** The hub remembers
  `clientMessageId`s for a while; a retry after an engine restart becomes a new
  turn — which the engine's own replay check ends at once with `outcome:
  duplicate`, since the message id was answered before. Nothing runs twice.
- **The journal is not a transcript.** It lives ten minutes past the turn. The
  agent's memory of the conversation is the runtime's and survives; what the
  person *saw* is yours to keep if you want to show it again — and to hand back
  to the engine as a `HistorySource` when the runtime has lost its copy (see
  "Replaying a conversation the runtime forgot" in
  [building-an-app.md](building-an-app.md)).
- **Confirmations work.** A tool that needs confirming puts a `confirmation`
  event in the journal and the turn waits — for up to `INTEGRATIONS_UI_TIMEOUT`, and
  the wait does not count against the turn's own timeout. Answer through the
  actions endpoint. Nobody answering in time refuses the call, and the model
  is told it was silence rather than a no — it is expected to ask the person
  for their approval when they are back instead of retrying; cancelling the
  turn withdraws the card, and a click on a withdrawn card runs nothing.
- **Chat-only things stay chat-only.** An http agent is not offered the
  widgets it would fire and forget (`ask_user_buttons`, `ask_user_select`,
  `open_screen`), nor forms (`open_form`): on a chat platform a click on such a
  card starts a new turn, and a program polling a journal has no turn to follow
  it in. What a caller answers is what blocks the turn — a confirmation card,
  a yes/no the runtime asks (`ask_user_confirm`). `send_ephemeral` and the
  channel-admin tools are not advertised either. The tool-trace widget under a
  reply has no message to sit under: the same tool events go to the journal
  instead. The journal is swept on every call, not only on new messages.
- **The engine can open a turn too.** A scheduled task in `turn` mode, or an
  event the application reports (`HttpHub.run_turn(TurnRequest)`, see
  [building-an-app.md](building-an-app.md) "Turns the engine starts"), opens a
  turn in the conversation exactly as a posted message would: `activeTurn`
  shows it while it runs, its journal reads the same, its first event says
  `startedBy: "engine"`, and it belongs to the user the request names — in
  that realm — who is the one to read it. A message posted meanwhile is
  refused with `turn_in_progress`, like any other; `startedSince` is how a
  client learns it happened without being told.
- **Identity is the caller's claim.** `X-User-Id` is whatever the program says.
  Keep the token where only your server can read it, and never let a browser
  talk to the hub directly.

## A minimal client (Python + aiohttp)

```python
import asyncio, uuid, aiohttp

ENGINE = "http://localhost:8428"
HEADERS = {"Authorization": "Bearer …", "X-User-Id": "u-42", "X-Username": "alice"}

async def ask(session, text):
    body = {"clientMessageId": uuid.uuid4().hex, "text": text}
    async with session.post(
        f"{ENGINE}/v1/agents/helper/conversations/u-42/messages", json=body, headers=HEADERS
    ) as r:
        started = await r.json()
        if r.status == 409:              # follow the turn already running
            started = {"turnId": started["turnId"], "cursor": 0}
    after = started["cursor"]
    while True:
        async with session.get(
            f"{ENGINE}/v1/turns/{started['turnId']}/events?after={after}&wait=8", headers=HEADERS
        ) as r:
            page = await r.json()
        for event in page["events"]:
            if event["type"] == "message":
                print(event["text"])
            elif event["type"] == "turn.finished":
                return event["outcome"]
        after = page["cursor"]

async def main():
    async with aiohttp.ClientSession() as session:
        print(await ask(session, "hello!"))

asyncio.run(main())
```
