# The ws gateway (custom client services)

The `ws` gateway lets **your own program** talk to agents without Slack or
Mattermost: the service dials the engine's WebSocket hub, authenticates with
its service token, and exchanges JSON frames over one duplex socket. Typical
shape: a bridge that watches some other chat under your personal account and
forwards each correspondent's messages to an agent that answers on your
behalf — every correspondent in their own, strictly isolated conversation.

The mental model mirrors Mattermost: a **service** is like your user account
(one connection, one token), **agents** are like bots — the service picks the
addressee per message, and every agent keeps its own profile/personality.

## Wiring

1. Put an agent on the ws gateway: `AGENTS_GATEWAY__<AGENT>=ws` (or
   `impi agent add --gateway ws`). No per-agent token — access is authorized
   per service.
2. Register a service: `impi ws add-service my-bridge [--agents a,b]` — or by
   hand:
   ```
   WS_SERVICE_TOKEN__MY_BRIDGE=<secret>
   WS_SERVICE_AGENTS__MY_BRIDGE=helper,scribe   # optional; unset = all ws agents
   ```
3. Restart the engine. The hub starts only when some agent runs on `ws`,
   listening on `WS_HOST:WS_PORT` (default `0.0.0.0:8424`).

The service connects OUT to the engine, so it needs no inbound port of its
own — it works from behind NAT, from a laptop, from anywhere that can reach
the engine.

## Protocol

Connect: `GET ws://<engine>:8424/ws` with header
`Authorization: Bearer <service token>`. Bad token → HTTP 401. One active
connection per service — a reconnect supersedes the previous socket.

All frames are JSON text messages.

**Service → engine:**

```jsonc
// deliver a message to an agent; conversation_id is YOUR key (e.g. the
// correspondent's id) — distinct ids never share agent memory
{"type": "message", "agent": "helper", "conversation_id": "user-42",
 "text": "привет",
 "user_id": "u42",        // optional
 "username": "vasya",     // optional
 "message_id": "m-1",     // optional; supply stable ids to dedupe redeliveries
 "kind": "dm",            // optional: dm (default) | thread | channel
 // optional attachments, bytes inline (your service may share no filesystem
 // with the engine). With files, "text" may be empty — a photo is a message.
 "files": [{"name": "photo.jpg", "mime": "image/jpeg", "data": "<base64>"}]}

// discovery: which agents can this service address?
{"type": "agents"}
```

**Engine → service:**

```jsonc
{"type": "reply",  "agent": "helper", "conversation_id": "user-42", "text": "…"}
{"type": "notice", "agent": "helper", "conversation_id": "user-42", "text": "…"}  // status/fallback
{"type": "agents", "agents": [{"name": "helper", "role": "…", "description": "…"}]}
{"type": "error",  "detail": "…"}   // bad frame / unknown agent; the socket stays open

// a file the agent sent (one frame per file; the caption rides with the first)
{"type": "file", "agent": "helper", "conversation_id": "user-42",
 "name": "chart.png", "mime": "image/png", "data": "<base64>", "text": "caption"}
```

Replies arrive whenever the agent's turn finishes (seconds to minutes) — the
`message` frame is fire-and-forget, correlate by `conversation_id`. Text is
Markdown as-is; rendering is the service's business.

## Semantics worth knowing

- **Isolation.** Sessions key on `(agent, conversation_id)`; internally the
  key is namespaced with the service name, so even two services reusing the
  same conversation id at the same agent never share history. Replies are
  routed back to the owning service and the namespace is stripped.
- **Offline services.** If the socket is down when a reply lands, it goes to
  a bounded per-service buffer (200 frames, in-memory) and is flushed on
  reconnect. An engine restart drops the buffer.
- **Dedupe.** Redelivered frames with the same `message_id` are processed
  once (per agent). Without `message_id` every frame is a fresh message.
- **No widgets.** Don't give ws agents `ask_user_*`/`open_form` tools; the
  widget verbs degrade to a plain-text notice.
- **Files.** Inline bytes are saved like any other attachment and named in the
  agent's prompt (pictures also go to the model directly) — see
  [files.md](files.md). A file past `ATTACHMENT_MAX_MB` is skipped; undecodable
  base64 answers with an `error` frame and the message is not delivered. In the
  other direction, `send_file` produces a `file` frame; like replies, it is
  buffered while the service is offline.

## A minimal client (Python + aiohttp)

```python
import asyncio, json, aiohttp

ENGINE = "ws://localhost:8424/ws"
TOKEN = "…"  # from `impi ws add-service`

async def main():
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(
            ENGINE, headers={"Authorization": f"Bearer {TOKEN}"}
        ) as ws:
            await ws.send_json({"type": "agents"})
            await ws.send_json({
                "type": "message", "agent": "helper",
                "conversation_id": "user-42", "text": "привет!",
            })
            async for msg in ws:
                frame = json.loads(msg.data)
                if frame["type"] == "reply":
                    print(f"[{frame['conversation_id']}] {frame['text']}")

asyncio.run(main())
```

Production clients add reconnect-with-backoff around `ws_connect` — buffered
frames arrive right after the handshake.
