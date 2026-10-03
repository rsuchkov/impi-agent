"""HttpHub: a request/turn API over the engine, for a program that cannot hold
a socket open.

The ws gateway gives a service one long-lived connection; a client that lives
in a browser behind someone else's server, or spreads over several nodes, needs
short independent calls instead: post a message, poll the turn's events, answer
a card, come back after a reload and find the turn still running. The hub
serves exactly that, for every agent registered on it, and leaves to the
application what it alone knows — who the caller is (``CallerAuthenticator``)
and whether to keep a transcript of its own.

The contract (``docs/http-gateway.md``) is additive: a client ignores event
types and fields it does not know.
"""

import asyncio
import logging
import re
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from aiohttp import web

from crucible.approvals import APPROVAL_KEY, ApprovalOutcome
from crucible.attachments import AttachmentStore
from crucible.gateways.dispatch import GatewayDispatcher
from crucible.gateways.http.auth import Caller, CallerAuthenticator
from crucible.gateways.http.client import HttpChatClient
from crucible.gateways.http.journal import EV_ACTIONS_RETIRED, STATUS_RUNNING
from crucible.gateways.http.turns import HttpTurns, Turn, TurnInProgress
from crucible.gateways.ws.events import frame_files
from crucible.interactions.screens import SCREEN_KEY, STATE_KEY
from crucible.ports.agent.runtime import RuntimeControl
from crucible.ports.chat.directory import AgentDirectory
from crucible.ports.chat.flow import TrackedSink, TurnOutcome
from crucible.ports.chat.types import (
    KIND_DM,
    PICK_FIELD_BY_KIND,
    ConversationRef,
    IncomingMessage,
)

logger = logging.getLogger(__name__)

API_VERSION = "1.0"
_VERSION_HEADER = "X-Engine-Api-Version"
_CLIENT_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_MAX_TEXT = 32_000
_SEP = ":"


class _ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.extra = extra


def _conversation_key(caller: Caller, conversation_id: str) -> str:
    """The engine-side conversation: realm-namespaced, like a ws service's."""
    return f"{caller.realm}{_SEP}{conversation_id}"


class HttpHub:
    def __init__(
        self,
        host: str,
        port: int,
        callers: CallerAuthenticator,
        *,
        turns: HttpTurns | None = None,
        directory: AgentDirectory | None = None,
        attachments: AttachmentStore | None = None,
        dispatcher: GatewayDispatcher | None = None,
        control: RuntimeControl | None = None,
        max_wait_s: float = 8.0,
    ) -> None:
        self._host = host
        self._port = port
        self._callers = callers
        # Shared with the agents' chat clients and handed to their flows as the
        # tracer: one object knows every running turn on this gateway.
        self.turns = turns or HttpTurns()
        self._directory = directory
        self._attachments = attachments
        self._dispatcher = dispatcher
        self._control = control
        # Every waiting poll holds a connection (and, behind a proxy, one of its
        # workers), so the wait is capped here whatever the client asks for.
        self._max_wait_s = max_wait_s
        self._agents: dict[str, tuple[TrackedSink, HttpChatClient]] = {}
        self._settling: set[asyncio.Task[None]] = set()
        self._runner: web.AppRunner | None = None
        self._started = False

    def register_agent(self, agent: str, sink: TrackedSink, chat: HttpChatClient) -> None:
        """Called by the gateway factory for every agent living on this hub."""
        self._agents[agent] = (sink, chat)

    # -- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        app = web.Application(middlewares=[self._envelope])
        app.router.add_get("/healthz", self._healthz)
        app.router.add_get("/readyz", self._readyz)
        app.router.add_get("/v1/agents", self._list_agents)
        app.router.add_post("/v1/agents/{agent}/conversations/{conversation}/messages", self._post_message)
        app.router.add_get("/v1/agents/{agent}/conversations/{conversation}", self._get_conversation)
        app.router.add_post("/v1/agents/{agent}/conversations/{conversation}/cancel", self._cancel)
        app.router.add_get("/v1/turns/{turn}/events", self._events)
        app.router.add_post("/v1/turns/{turn}/actions", self._action)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        await web.TCPSite(self._runner, self._host, self._port).start()
        self._started = True
        logger.info(
            "http hub on http://%s:%d/v1 (%d agent(s))", self._host, self._port, len(self._agents)
        )

    async def stop(self) -> None:
        self._started = False
        for task in list(self._settling):
            task.cancel()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # -- plumbing -------------------------------------------------------------

    @web.middleware
    async def _envelope(self, request: web.Request, handler) -> web.StreamResponse:
        """One shape for every error, the version on every answer."""
        try:
            response = await handler(request)
        except _ApiError as exc:
            response = web.json_response(
                {"error": {"code": exc.code, "message": str(exc)}, **exc.extra}, status=exc.status
            )
        except web.HTTPException:
            raise
        except Exception:
            logger.exception("http hub: %s %s failed", request.method, request.path)
            response = web.json_response(
                {"error": {"code": "internal", "message": "the engine could not handle this"}},
                status=500,
            )
        response.headers[_VERSION_HEADER] = API_VERSION
        return response

    async def _caller(self, request: web.Request, body: dict[str, Any] | None) -> Caller:
        caller = await self._callers.authenticate(request, body)
        if caller is None:
            raise _ApiError(401, "unauthorized", "the request carries no accepted credentials")
        return caller

    async def _body(self, request: web.Request) -> dict[str, Any]:
        try:
            data = await request.json()
        except ValueError as exc:
            raise _ApiError(422, "validation", "the body is not valid JSON") from exc
        if not isinstance(data, dict):
            raise _ApiError(422, "validation", "the body must be a JSON object")
        return data

    def _agent(self, request: web.Request, caller: Caller) -> tuple[str, TrackedSink, HttpChatClient]:
        agent = request.match_info["agent"]
        entry = self._agents.get(agent)
        if entry is None or (caller.agents is not None and agent not in caller.agents):
            raise _ApiError(403, "forbidden", f"no agent {agent!r} for this caller")
        return agent, entry[0], entry[1]

    def _owned_turn(self, request: web.Request, caller: Caller) -> Turn:
        turn = self.turns.get(request.match_info["turn"])
        if turn is None or turn.owner != caller.owner:
            raise _ApiError(404, "not_found", "no such turn")
        return turn

    def _wait(self, request: web.Request) -> float:
        raw = request.query.get("wait", "0")
        try:
            wait = float(raw)
        except ValueError as exc:
            raise _ApiError(422, "validation", "`wait` must be a number of seconds") from exc
        return max(0.0, min(wait, self._max_wait_s))

    @staticmethod
    def _after(request: web.Request) -> int:
        raw = request.query.get("after", "0")
        try:
            return max(0, int(raw))
        except ValueError as exc:
            raise _ApiError(422, "validation", "`after` must be an integer") from exc

    # -- handlers -------------------------------------------------------------

    async def _healthz(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def _readyz(self, request: web.Request) -> web.Response:
        if not self._started:
            return web.json_response({"ready": False, "reason": "starting"}, status=503)
        if self._control is not None:
            stats = self._control.stats()
            if stats.busy >= stats.capacity:
                return web.json_response(
                    {"ready": False, "reason": "every runtime slot is busy", "runtime": stats.__dict__},
                    status=503,
                )
            return web.json_response({"ready": True, "runtime": stats.__dict__})
        return web.json_response({"ready": True})

    async def _list_agents(self, request: web.Request) -> web.Response:
        caller = await self._caller(request, None)
        infos = {a.name: a for a in self._directory.list_agents()} if self._directory else {}
        agents = [
            {"name": name, "role": infos[name].role if name in infos else "",
             "description": infos[name].description if name in infos else ""}
            for name in sorted(self._agents)
            if caller.agents is None or name in caller.agents
        ]
        return web.json_response({"agents": agents})

    async def _post_message(self, request: web.Request) -> web.Response:
        body = await self._body(request)
        caller = await self._caller(request, body)
        agent, sink, chat = self._agent(request, caller)
        conversation_id = request.match_info["conversation"]
        client_message_id = str(body.get("clientMessageId") or "")
        if not _CLIENT_ID.match(client_message_id):
            raise _ApiError(422, "validation", "`clientMessageId` is required: 1-128 url-safe characters")
        text = body.get("text")
        if not isinstance(text, str) or len(text) > _MAX_TEXT:
            raise _ApiError(422, "validation", f"`text` must be a string of at most {_MAX_TEXT} characters")
        try:
            files = frame_files(body)
        except ValueError as exc:
            raise _ApiError(422, "validation", str(exc)) from exc
        if not text.strip() and not files:
            raise _ApiError(422, "validation", "`text` must not be empty unless files are attached")

        conversation = _conversation_key(caller, conversation_id)
        self.turns.sweep(asyncio.get_running_loop().time())
        try:
            turn, created = self.turns.start(
                agent=agent, conversation=conversation, owner=caller.owner,
                client_message_id=client_message_id,
            )
        except TurnInProgress as exc:
            raise _ApiError(
                409, "turn_in_progress", "this conversation already has a turn running",
                turnId=exc.turn_id,
            ) from exc
        answer = {"turnId": turn.id, "conversationId": conversation_id, "cursor": 0}
        if not created:
            return web.json_response(answer, status=200)  # the retry of a message we have

        message = IncomingMessage(
            ref=ConversationRef(
                channel_id=conversation, conversation_id=conversation,
                # Namespaced like the conversation, so the store's dedup of a
                # replayed id cannot collide across realms.
                message_id=f"{caller.realm}{_SEP}{client_message_id}", thread_root_id="",
            ),
            text=text,
            user_id=caller.user_id,
            username=caller.username or caller.user_id,
            kind=KIND_DM,
            is_dm=True,
            mentioned=True,
            turn=caller.turn,
        )
        if files and self._attachments is not None:
            stored = await self._attachments.save_many(agent, conversation, files)
            message = replace(message, attachments=stored)
        try:
            outcome = sink.submit_tracked(message, chat)
        except Exception:
            self.turns.abandon(turn)
            raise
        task = asyncio.ensure_future(self._settle(turn, outcome))
        self._settling.add(task)
        task.add_done_callback(self._settling.discard)
        return web.json_response(answer, status=202)

    async def _settle(self, turn: Turn, outcome) -> None:
        try:
            result = await outcome
        except asyncio.CancelledError:
            self.turns.abandon(turn)  # the engine is stopping: nobody will answer
            raise
        except Exception:
            logger.exception("turn %s: the flow raised", turn.id)
            result = TurnOutcome.ERROR
        self.turns.finish(turn, result)

    async def _events(self, request: web.Request) -> web.Response:
        caller = await self._caller(request, None)
        turn = self._owned_turn(request, caller)
        after = self._after(request)
        found = await turn.journal.wait(after, self._wait(request))
        return web.json_response({
            "turnId": turn.id,
            "status": turn.journal.status,
            "events": [e.to_wire() for e in found],
            "cursor": found[-1].seq if found else after,
        })

    async def _get_conversation(self, request: web.Request) -> web.Response:
        caller = await self._caller(request, None)
        self._agent(request, caller)
        conversation_id = request.match_info["conversation"]
        turn = self.turns.active(_conversation_key(caller, conversation_id))
        active = None
        if turn is not None and turn.owner == caller.owner:
            active = {"turnId": turn.id, "status": turn.journal.status, "cursor": turn.journal.cursor}
        return web.json_response({"conversationId": conversation_id, "activeTurn": active})

    async def _cancel(self, request: web.Request) -> web.Response:
        caller = await self._caller(request, None)
        self._agent(request, caller)
        turn = self.turns.active(_conversation_key(caller, request.match_info["conversation"]))
        if turn is None or turn.owner != caller.owner:
            raise _ApiError(404, "not_found", "no turn is running in this conversation")
        if self._control is None or not turn.runtime_session_id:
            raise _ApiError(409, "not_cancellable", "this turn cannot be interrupted here")
        cancelled = await self._control.cancel(turn.runtime_session_id)
        return web.json_response({"turnId": turn.id, "cancelled": cancelled})

    async def _action(self, request: web.Request) -> web.Response:
        body = await self._body(request)
        caller = await self._caller(request, body)
        turn = self._owned_turn(request, caller)
        if self._dispatcher is None:
            raise _ApiError(404, "not_found", "interactivity is off on this engine")
        post_id = str(body.get("postId") or "")
        action_id = str(body.get("actionId") or "")
        value = str(body.get("value") or "")
        posted = turn.posted.get(post_id)
        action = next((a for a in posted or () if a.id == action_id), None)
        if action is None:
            raise _ApiError(404, "not_found", "no such control; it may have been retired")
        context: Mapping[str, Any] = action.context
        if context.get(APPROVAL_KEY):
            outcome = self._dispatcher.resolve_approval(str(context[APPROVAL_KEY]), value, caller.user_id)
            if outcome is ApprovalOutcome.NOT_MINE:
                self._retire(turn, post_id, "These controls are no longer active.")
            return web.json_response({"outcome": outcome.name.lower()})
        if context.get(SCREEN_KEY):
            await self._dispatcher.redraw_screen(
                str(context.get(STATE_KEY, "")), value, post_id=post_id, user_id=caller.user_id
            )
            return web.json_response({"outcome": "redrawn"})
        if context.get("form"):
            raise _ApiError(409, "not_available", "forms cannot be opened on this gateway")
        token = str(context.get("token", ""))
        if not self._dispatcher.resolve_pending(token, value):
            await self._dispatcher.consume_action(
                token, value, caller.user_id, pick=PICK_FIELD_BY_KIND.get(action.kind, "")
            )
        self._retire(turn, post_id, f"You chose: {value}" if value else "Answered.")
        return web.json_response({"outcome": "resolved"})

    def _retire(self, turn: Turn, post_id: str, text: str) -> None:
        turn.posted.pop(post_id, None)
        turn.journal.emit(EV_ACTIONS_RETIRED, postId=post_id, text=text)
        if not turn.posted:
            turn.journal.set_status(STATUS_RUNNING)
