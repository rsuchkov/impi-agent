"""The replicas' own routes on the http hub, and the client that calls them.

What one replica hands another (``ports.replica``): a batch of messages for a
conversation the other owns, answered with the turn's outcome once it ran;
and a click on a card the other posted, answered with what it did with it.
Both under one shared secret (``Authorization: Bearer``), on the hub's port —
behind the same balancer as the public routes, which is why the secret is a
must and not a hardening.

A message goes over the wire whole but for two things: an attachment's file
travels as its bytes and is written into the owner's attachment store, since
the path it had is this replica's disk; and a turn's secrets stay behind — the
internal route is plain http between pods, and nothing forwarded through it
needs them (a credential a caller sent rides a request pinned to its replica,
which is never forwarded).
"""

import asyncio
import base64
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import aiohttp
from aiohttp import web

from crucible.attachments import AttachmentStore, IncomingFile
from crucible.constant_time import matches_any
from crucible.gateways.dispatch import GatewayDispatcher
from crucible.gateways.http.auth import Refusal
from crucible.ports.chat.client import ChatClient
from crucible.ports.chat.flow import MessageSink, TrackedSink, TurnOutcome
from crucible.ports.chat.types import (
    Attachment,
    ConversationRef,
    EventReport,
    IncomingMessage,
    QuotedMessage,
)
from crucible.ports.replica import ClickKind, ForwardedClick
from crucible.ports.turn import TurnScope

logger = logging.getLogger(__name__)

TURNS_PATH = "/v1/internal/turns"
CLICKS_PATH = "/v1/internal/clicks"
# A forwarded turn is awaited whole: a turn waiting on a confirmation can take
# minutes, and the caller has nothing else to do with the connection.
FORWARD_TIMEOUT_S = 900.0


class ForwardFailed(Exception):
    """The owner did not take what was forwarded: unreachable, or it refused."""


class TurnTarget(Protocol):
    """An agent as it runs here: where its messages go, and its chat client on
    a gateway (``interactions.dispatcher.AgentSink`` has this shape)."""

    @property
    def sink(self) -> MessageSink: ...

    def chat_for(self, gateway: str) -> ChatClient: ...


class TurnTargets(Protocol):
    """The agents running here, by name (``interactions.presence.AgentPresence``
    has this shape)."""

    def sink(self, agent: str) -> TurnTarget | None: ...


# -- the message on the wire -----------------------------------------------------


def message_to_wire(msg: IncomingMessage) -> dict[str, Any]:
    """The message as JSON, attachments as bytes, secrets left out."""
    files = []
    for a in msg.attachments:
        try:
            data = Path(a.path).read_bytes()
        except OSError:
            logger.warning("forwarding %s: attachment %s not readable; left out", msg.ref.message_id, a.name)
            continue
        files.append({"name": a.name, "mime": a.mime, "data": base64.b64encode(data).decode("ascii")})
    turn = (
        {
            "turn_id": msg.turn.turn_id,
            "attributes": dict(msg.turn.attributes),
            "flags": sorted(msg.turn.flags),
        }
        if msg.turn is not None
        else None
    )
    try:
        raw = json.loads(json.dumps(msg.raw))
    except (TypeError, ValueError):
        raw = {}
    return {
        "ref": {
            "channel_id": msg.ref.channel_id,
            "conversation_id": msg.ref.conversation_id,
            "message_id": msg.ref.message_id,
            "thread_root_id": msg.ref.thread_root_id,
        },
        "text": msg.text,
        "user_id": msg.user_id,
        "username": msg.username,
        "timestamp": msg.timestamp.isoformat() if msg.timestamp is not None else None,
        "kind": msg.kind,
        "is_dm": msg.is_dm,
        "mentioned": msg.mentioned,
        "is_from_bot": msg.is_from_bot,
        "gateway": msg.gateway,
        "hop_depth": msg.hop_depth,
        "attachments": files,
        "synthetic": msg.synthetic,
        "turn": turn,
        "quoted": {"author": msg.quoted.author, "text": msg.quoted.text} if msg.quoted else None,
        "engine_started": msg.engine_started,
        "event": {"source": msg.event.source, "text": msg.event.text} if msg.event else None,
        "raw": raw,
    }


def message_from_wire(data: dict[str, Any], attachments: tuple[Attachment, ...] = ()) -> IncomingMessage:
    """The message back, with the attachments as saved here."""
    ref = data["ref"]
    turn = data.get("turn")
    quoted = data.get("quoted")
    event = data.get("event")
    stamp = data.get("timestamp")
    return IncomingMessage(
        ref=ConversationRef(
            channel_id=str(ref["channel_id"]),
            conversation_id=str(ref["conversation_id"]),
            message_id=str(ref["message_id"]),
            thread_root_id=str(ref.get("thread_root_id", "")),
        ),
        text=str(data.get("text", "")),
        user_id=str(data.get("user_id", "")),
        username=str(data.get("username", "")),
        timestamp=datetime.fromisoformat(stamp) if stamp else None,
        kind=str(data.get("kind", "")),
        is_dm=bool(data.get("is_dm", False)),
        mentioned=bool(data.get("mentioned", False)),
        is_from_bot=bool(data.get("is_from_bot", False)),
        gateway=str(data.get("gateway", "")),
        hop_depth=int(data.get("hop_depth", 0)),
        attachments=attachments,
        synthetic=bool(data.get("synthetic", False)),
        turn=(
            TurnScope(
                turn_id=str(turn["turn_id"]),
                attributes={str(k): str(v) for k, v in dict(turn.get("attributes", {})).items()},
                flags=set(turn.get("flags", [])),
            )
            if turn
            else None
        ),
        quoted=QuotedMessage(author=str(quoted["author"]), text=str(quoted["text"])) if quoted else None,
        engine_started=bool(data.get("engine_started", False)),
        event=EventReport(source=str(event["source"]), text=str(event["text"])) if event else None,
        raw=dict(data.get("raw") or {}),
    )


def wire_files(data: dict[str, Any]) -> list[IncomingFile]:
    files = []
    for f in data.get("attachments") or []:
        try:
            payload = base64.b64decode(str(f.get("data", "")), validate=True)
        except (ValueError, TypeError):
            continue
        files.append(IncomingFile(name=str(f.get("name", "file")), data=payload, mime=str(f.get("mime", ""))))
    return files


# -- the server side -------------------------------------------------------------


class InternalRoutes:
    """The two routes, for ``HttpHub(routes=internal.routes)``."""

    def __init__(
        self,
        token: str,
        presence: TurnTargets,
        *,
        dispatcher: GatewayDispatcher | None = None,
        attachments: AttachmentStore | None = None,
    ) -> None:
        if not token:
            raise ValueError("the internal routes need a shared secret")
        self._token = token
        self._presence = presence
        self._dispatcher = dispatcher
        self._attachments = attachments

    @property
    def routes(self) -> list[web.RouteDef]:
        return [web.post(TURNS_PATH, self._turns), web.post(CLICKS_PATH, self._clicks)]

    async def _body(self, request: web.Request) -> dict[str, Any]:
        presented = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not matches_any([self._token], presented):
            raise Refusal(401, "unauthorized", "the request carries no accepted credentials")
        try:
            data = await request.json()
        except ValueError as exc:
            raise Refusal(422, "validation", "the body is not valid JSON") from exc
        if not isinstance(data, dict):
            raise Refusal(422, "validation", "the body must be a JSON object")
        return data

    async def _turns(self, request: web.Request) -> web.Response:
        body = await self._body(request)
        agent = str(body.get("agent", ""))
        sink = self._presence.sink(agent)
        if sink is None or not isinstance(sink.sink, TrackedSink):
            raise Refusal(404, "unknown_agent", f"agent {agent!r} does not run here")
        wire = body.get("messages")
        if not isinstance(wire, list) or not wire:
            raise Refusal(422, "validation", "`messages` must be a non-empty list")
        waits = []
        for data in wire:
            if not isinstance(data, dict):
                raise Refusal(422, "validation", "every message must be a JSON object")
            saved: tuple[Attachment, ...] = ()
            files = wire_files(data)
            if files and self._attachments is not None:
                saved = await self._attachments.save_many(agent, str(data["ref"]["conversation_id"]), files)
            msg = message_from_wire(data, saved)
            waits.append(sink.sink.submit_tracked(msg, sink.chat_for(msg.gateway)))
        outcomes: list[TurnOutcome] = list(await asyncio.gather(*waits))
        return web.json_response({"outcome": outcomes[-1].name})

    async def _clicks(self, request: web.Request) -> web.Response:
        body = await self._body(request)
        if self._dispatcher is None:
            raise Refusal(404, "not_found", "interactivity is off on this engine")
        token, value, user_id = (str(body.get(k, "")) for k in ("token", "value", "user_id"))
        kind = str(body.get("kind", ""))
        if kind == ClickKind.APPROVAL.name:
            outcome = await self._dispatcher.resolve_approval(token, value, user_id)
            answer = ForwardedClick[outcome.name]
        elif kind == ClickKind.PENDING.name:
            resolved = await self._dispatcher.resolve_pending(token, value)
            answer = ForwardedClick.RESOLVED if resolved else ForwardedClick.NOT_MINE
        else:
            raise Refusal(422, "validation", "`kind` must name a click kind")
        return web.json_response({"outcome": answer.name})


# -- the client side -------------------------------------------------------------


class HubForwarder:
    """Calls another replica's internal routes (``TurnForwarder`` and
    ``ClickForwarder``). One client session, opened on first use."""

    def __init__(self, token: str, *, timeout_s: float = FORWARD_TIMEOUT_S) -> None:
        self._token = token
        self._timeout_s = timeout_s
        self._session: aiohttp.ClientSession | None = None

    async def forward_turn(self, url: str, agent: str, msgs: list[IncomingMessage]) -> TurnOutcome:
        wire = [await asyncio.to_thread(message_to_wire, m) for m in msgs]
        answer = await self._post(url + TURNS_PATH, {"agent": agent, "messages": wire})
        return TurnOutcome[str(answer.get("outcome", ""))]

    async def forward_click(
        self, url: str, kind: ClickKind, token: str, value: str, user_id: str
    ) -> ForwardedClick:
        answer = await self._post(
            url + CLICKS_PATH, {"kind": kind.name, "token": token, "value": value, "user_id": user_id}
        )
        return ForwardedClick[str(answer.get("outcome", ""))]

    async def _post(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._timeout_s, sock_connect=5.0),
                headers={"Authorization": f"Bearer {self._token}"},
            )
        try:
            async with self._session.post(url, json=body) as response:
                if response.status != 200:
                    raise ForwardFailed(f"{url}: {response.status} {await response.text()}")
                data = await response.json()
        except aiohttp.ClientError as exc:
            raise ForwardFailed(f"{url}: {exc}") from exc
        except TimeoutError as exc:
            raise ForwardFailed(f"{url}: timed out") from exc
        if not isinstance(data, dict):
            raise ForwardFailed(f"{url}: not a JSON object")
        return data

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
