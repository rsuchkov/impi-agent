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
3. Restart the engine. The hub starts only when some agent runs on `http`,
   listening on `HTTP_HOST:HTTP_PORT` (default `0.0.0.0:8427`). Like the ws
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
| `not_found` | 404 | unknown turn, another person's turn, a retired control, nothing running to cancel |
| `turn_in_progress` | 409 | this conversation already has a turn running; `turnId` names it |
| `not_cancellable` | 409 | the turn cannot be interrupted here (no runtime behind it yet) |
| `not_available` | 409 | a form was asked to open; forms are chat-only |
| `validation` | 422 | a bad body or query value |

### `POST /v1/agents/{agent}/conversations/{conversation}/messages`

```json
{"clientMessageId": "uuid-or-anything-stable", "text": "…",
 "files": [{"name": "photo.jpg", "mime": "image/jpeg", "data": "<base64>"}]}
```

- `202 {"turnId", "conversationId", "cursor": 0}` — a new turn started.
- `200` with the same body — `clientMessageId` was already accepted: a retry
  returns the turn it started, not a second one.
- `409 turn_in_progress` with `turnId` — follow that turn instead.

`clientMessageId` is required (1–128 url-safe characters) and is what makes a
submit idempotent; generate one per message, reuse it on retry. `text` may be
empty only when files are attached — a photo is a message.

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
| `turn.started` | — | the message was accepted |
| `tool.started` | `callId`, `tool` | a tool began |
| `tool.finished` | `callId`, `tool`, `ok` | it ended |
| `message` | `messageId`, `format: "markdown"`, `text` | the agent's answer |
| `notice` | `code`, `text` | the engine speaking: `timeout`, `busy`, `quota`, `credentials`, `context`, `runtime_unavailable`, `agent_error`, `empty_answer`; `text` is a sentence for a person, `code` is for you |
| `actions` | `postId`, `text`, `actions[]` | a message with controls — a confirmation card, a question; the turn waits (`awaiting_input`) |
| `actions.retired` | `postId`, `text` | its controls are gone; show `text` in their place |
| `cards` | `postId`, `cards[{text, accent, actions[]}]` | a message built from cards; the same `postId` again means redraw it |
| `file` | `name`, `mime`, `data` (base64), `text` | a file the agent sent (the caption rides with the first) |
| `turn.finished` | `outcome` | the last event: `replied`, `acted` (a tool answered for it), `empty`, `duplicate`, `timeout`, `error` |

An action: `{id, label, value, style, kind, options[{label, value}]}`. `kind` is
`button`, `select`, `user_select` or `channel_select`; a button echoes its
`value`, a select echoes the chosen option's value. Render the text with
`textContent` — a card's content is partly the agent's, and the agent reads
what other people wrote.

### `POST /v1/turns/{turnId}/actions`

```json
{"postId": "…", "actionId": "yes", "value": "Allow"}
```

`200 {"outcome": "resolved" | "redrawn" | "not_mine" | "not_allowed"}`. A click
on a confirmation card for a tool call is answered by whoever is in the
conversation — here, the caller's user; a request for a *credential* is
addressed to named approvers and refuses everyone else (`not_allowed`). After a
click the control is retired (`actions.retired`); clicking it again is `404`.

### `GET /v1/agents/{agent}/conversations/{conversation}`

`200 {"conversationId", "activeTurn": {"turnId", "status", "cursor"} | null}` —
for resuming after a page reload: poll the active turn from its cursor.

### `POST /v1/agents/{agent}/conversations/{conversation}/cancel`

Interrupts the running turn; `200 {"turnId", "cancelled"}`. The turn ends with
what it had (its `turn.finished` follows) and the conversation keeps its
memory. `404` when nothing is running.

### `GET /v1/agents`

The agents this caller may address: `{"agents": [{"name", "role", "description"}]}`.

### `GET /healthz`, `GET /readyz`

Unauthenticated. `readyz` is `503` while the engine starts and while every
runtime slot is busy (`runtime: {alive, busy, capacity, waiting}` says which).

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
  person *saw* is yours to keep if you want to show it again.
- **Confirmations work.** A tool that needs confirming puts an `actions` card
  in the journal and the turn waits — for up to `INTEGRATIONS_UI_TIMEOUT`, and
  the wait does not count against the turn's own timeout. Answer through the
  actions endpoint. Nobody answering in time refuses the call; cancelling the
  turn withdraws the card, and a click on a withdrawn card runs nothing.
- **Chat-only things stay chat-only.** Forms (`open_form`) cannot open here —
  the tool is advertised when interactivity is on, and a call is refused with a
  logged warning. `send_ephemeral` and the channel-admin tools are not
  advertised to an http agent. The tool-trace widget under a reply has no
  message to sit under: the same tool events go to the journal instead.
- **Identity is the caller's claim.** `X-User-Id` is whatever the program says.
  Keep the token where only your server can read it, and never let a browser
  talk to the hub directly.

## A minimal client (Python + aiohttp)

```python
import asyncio, uuid, aiohttp

ENGINE = "http://localhost:8427"
HEADERS = {"Authorization": "Bearer …", "X-User-Id": "u-42", "X-Username": "vasya"}

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
        print(await ask(session, "привет!"))

asyncio.run(main())
```
