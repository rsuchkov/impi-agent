"""WsHub: one WebSocket endpoint for every client service.

A client service dials ``GET /ws`` with its bearer token and keeps the duplex
socket: it pushes ``message`` frames at any allowed agent and receives that
agent's ``reply``/``notice`` frames on the same connection — no inbound port
on the service side. One hub serves all services and all ws agents; per-agent
state (sink + chat client) is registered by the gateway factory, per-service
connections and outbound buffers live here.

Offline services do not lose replies outright: frames land in a bounded
per-service buffer and are flushed on reconnect (in-memory only — a restart
drops them).
"""

import base64
import json
import logging
from collections import deque
from collections.abc import Mapping
from dataclasses import replace

from aiohttp import WSMsgType, web

from crucible.attachments import AttachmentStore
from crucible.gateways.ws.events import (
    frame_error,
    frame_files,
    frame_to_incoming,
    split_conversation,
)
from crucible.ports.chat.client import ChatClient
from crucible.ports.chat.directory import AgentDirectory
from crucible.ports.chat.flow import MessageSink
from crucible.ports.chat.types import OutgoingFile

logger = logging.getLogger(__name__)

_BUFFER_LIMIT = 200


class WsHub:
    def __init__(
        self,
        host: str,
        port: int,
        services: Mapping[str, tuple[str, tuple[str, ...] | None]],
        *,
        directory: AgentDirectory | None = None,
        attachments: AttachmentStore | None = None,
    ) -> None:
        # services: name -> (bearer token, agent allowlist or None = all ws agents)
        self._host = host
        self._port = port
        self._attachments = attachments
        self._by_token: dict[str, tuple[str, tuple[str, ...] | None]] = {
            token: (name, allow) for name, (token, allow) in services.items() if token
        }
        self._directory = directory
        self._agents: dict[str, tuple[MessageSink, ChatClient]] = {}
        self._conns: dict[str, web.WebSocketResponse] = {}
        self._buffers: dict[str, deque[dict]] = {}
        self._runner: web.AppRunner | None = None

    def register_agent(self, agent: str, sink: MessageSink, chat: ChatClient) -> None:
        """Called by the gateway factory for every agent living on this hub."""
        self._agents[agent] = (sink, chat)

    # -- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/ws", self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        await web.TCPSite(self._runner, self._host, self._port).start()
        logger.info(
            "ws hub on ws://%s:%d/ws (%d service(s), %d agent(s))",
            self._host, self._port, len(self._by_token), len(self._agents),
        )

    async def stop(self) -> None:
        for ws in list(self._conns.values()):
            await ws.close()
        self._conns.clear()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # -- outbound (called by WsChatClient) --------------------------------------

    async def send(self, agent: str, internal_conv: str, event: str, text: str) -> None:
        """Deliver an agent's message to the service owning the conversation —
        over its live socket, else into its reconnect buffer."""
        service, client_conv = split_conversation(internal_conv)
        await self._deliver(
            service,
            {
                "type": event,
                "agent": agent,
                "conversation_id": client_conv,
                "text": text,
            },
        )

    async def send_file(
        self, agent: str, internal_conv: str, file: OutgoingFile, *, text: str = ""
    ) -> None:
        """Deliver a file the agent sent. Bytes travel base64-encoded inside the
        frame, the same way they arrive — the service may share no filesystem."""
        service, client_conv = split_conversation(internal_conv)
        await self._deliver(
            service,
            {
                "type": "file",
                "agent": agent,
                "conversation_id": client_conv,
                "name": file.name,
                "mime": file.mime,
                "data": base64.b64encode(file.data).decode("ascii"),
                "text": text,
            },
        )

    async def _deliver(self, service: str, frame: dict) -> None:
        ws = self._conns.get(service)
        if ws is not None and not ws.closed:
            try:
                await ws.send_json(frame)
                return
            except ConnectionError:
                logger.debug("ws send to %s failed mid-flight; buffering", service)
        buffer = self._buffers.setdefault(service, deque(maxlen=_BUFFER_LIMIT))
        if len(buffer) == _BUFFER_LIMIT:
            logger.warning(
                "ws service %s: outbound buffer full (%d) — dropping the oldest frame",
                service, _BUFFER_LIMIT,
            )
        buffer.append(frame)

    # -- inbound ---------------------------------------------------------------

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        entry = self._by_token.get(token)
        if entry is None:
            return web.Response(status=401, text="unknown service token")
        service, allow = entry

        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        # One active connection per service: a reconnect supersedes the old one.
        old = self._conns.get(service)
        self._conns[service] = ws
        if old is not None and not old.closed:
            await old.close()
        logger.info("ws service connected: %s", service)

        buffer = self._buffers.get(service)
        while buffer:
            await ws.send_json(buffer.popleft())

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    # Frame handling must never kill the connection.
                    try:
                        await self._on_frame(service, allow, ws, msg.data)
                    except Exception:
                        logger.exception("ws frame handling failed (service %s)", service)
                elif msg.type == WSMsgType.ERROR:
                    break
        finally:
            if self._conns.get(service) is ws:
                del self._conns[service]
            logger.info("ws service disconnected: %s", service)
        return ws

    async def _on_frame(
        self,
        service: str,
        allow: tuple[str, ...] | None,
        ws: web.WebSocketResponse,
        raw: str,
    ) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            await self._error(ws, "frame is not valid JSON")
            return
        if not isinstance(data, dict):
            await self._error(ws, "frame must be a JSON object")
            return
        kind = data.get("type")
        if kind == "agents":
            await ws.send_json({"type": "agents", "agents": self._agents_payload(allow)})
        elif kind == "message":
            error = frame_error(data)
            if error is not None:
                await self._error(ws, error)
                return
            agent = data["agent"].strip()
            if agent not in self._agents or not self._allowed(agent, allow):
                await self._error(ws, f"unknown or not allowed agent {agent!r}")
                return
            sink, chat = self._agents[agent]
            try:
                files = frame_files(data)
            except ValueError as exc:
                await self._error(ws, str(exc))
                return
            msg = frame_to_incoming(service, data)
            if files and self._attachments is not None:
                stored = await self._attachments.save_many(
                    agent, msg.conversation_id, files
                )
                msg = replace(msg, attachments=stored)
            # Fire-and-forget: a long agent turn must never block the socket.
            sink.submit(msg, chat)
        else:
            await self._error(ws, f"unsupported frame type {kind!r}")

    def _agents_payload(self, allow: tuple[str, ...] | None) -> list[dict]:
        infos = {a.name: a for a in self._directory.list_agents()} if self._directory else {}
        payload = []
        for name in sorted(self._agents):
            if not self._allowed(name, allow):
                continue
            info = infos.get(name)
            payload.append({
                "name": name,
                "role": info.role if info else "",
                "description": info.description if info else "",
            })
        return payload

    @staticmethod
    def _allowed(agent: str, allow: tuple[str, ...] | None) -> bool:
        return allow is None or agent in allow

    @staticmethod
    async def _error(ws: web.WebSocketResponse, detail: str) -> None:
        await ws.send_json({"type": "error", "detail": detail})


__all__ = ["WsHub"]
