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
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from aiohttp import web
from aiohttp.typedefs import Middleware

from crucible.approvals import APPROVAL_KEY, ApprovalOutcome
from crucible.attachments import AttachmentStore
from crucible.gateways.dispatch import GatewayDispatcher
from crucible.gateways.http.auth import Caller, CallerAuthenticator, Refusal
from crucible.gateways.http.client import HttpChatClient, wire_action, wire_card
from crucible.gateways.http.journal import EV_ACTIONS_RETIRED, STATUS_RUNNING
from crucible.gateways.http.turns import (
    STARTED_BY_ENGINE,
    HttpTurns,
    Turn,
    TurnInProgress,
)
from crucible.gateways.ws.events import files_error, frame_files
from crucible.interactions.screens import SCREEN_KEY, STATE_KEY
from crucible.ports.agent.runtime import RuntimeControl
from crucible.ports.chat.directory import AgentDirectory
from crucible.ports.chat.flow import StoppableSink, TrackedSink, TurnOutcome
from crucible.ports.chat.gateway import GATEWAY_HTTP
from crucible.ports.chat.interactions import actions_from_json, approval_card_from_json
from crucible.ports.chat.keying import ConversationKey, PlatformKey
from crucible.ports.chat.types import (
    ACTION_BUTTON,
    KIND_DM,
    Action,
    ConversationRef,
    IncomingMessage,
)
from crucible.ports.conversation import EngineTurnsSince
from crucible.ports.engine_turn import DispatchError, TurnRequest, requested_message
from crucible.ports.traces import decode_calls
from crucible.ports.turn import TurnScope

logger = logging.getLogger(__name__)

API_VERSION = "1.0"
_VERSION_HEADER = "X-Engine-Api-Version"
_CLIENT_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
MAX_TEXT = 32_000
# What a request may weigh with no attachment store to size it by: text and a
# little more. With a store, the cap follows ATTACHMENT_MAX_MB (base64 is 4/3
# of the bytes) with room for a few files.
_TEXT_ONLY_BODY_BYTES = 1024 * 1024
_FILES_PER_BODY = 4
_SEP = ":"


# One of the application's own say in readiness: None when it may take
# requests, else why not — a short reason, which the response carries. It
# answers rather than raises; one that raises counts as a "no".
ReadinessProbe = Callable[[], Awaitable[str | None]]
# How long one probe may take before it counts as a "no": a probe that hangs
# reads as a pod that is not ready, never as a hub that stops answering.
READINESS_PROBE_TIMEOUT_S = 2.0


def _expired(expires_at: str) -> bool:
    """Whether a card's ``expires_at`` (ISO-8601 UTC) is past; an unreadable or
    absent one is not an expiry."""
    if not expires_at:
        return False
    try:
        until = datetime.fromisoformat(expires_at)
    except ValueError:
        return False
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    return until <= datetime.now(timezone.utc)


def _conversation_key(caller: Caller, conversation_id: str) -> str:
    """The engine-side conversation: realm-namespaced, like a ws service's."""
    return f"{caller.realm}{_SEP}{conversation_id}"


async def _probe_reason(probe: ReadinessProbe) -> str | None:
    """A probe's reason, or a reason of its own when it raised or hung. What it
    raised is logged, not answered: a readiness response is no place for an
    exception's text."""
    name = getattr(probe, "__name__", "readiness probe")
    try:
        return await asyncio.wait_for(probe(), READINESS_PROBE_TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.warning("readiness: %s took over %.0fs", name, READINESS_PROBE_TIMEOUT_S)
        return f"{name}: timed out"
    except Exception:
        logger.warning("readiness: %s failed", name, exc_info=True)
        return f"{name}: failed"


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
        routes: Sequence[web.RouteDef] = (),
        middlewares: Sequence[Middleware] = (),
        readiness_probes: Sequence[ReadinessProbe] = (),
        engine_turns: EngineTurnsSince | None = None,
        keying: ConversationKey | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._callers = callers
        self._keying: ConversationKey = keying or PlatformKey()  # the conversation a file belongs to
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
        self._max_body = (
            int(attachments.max_bytes * 4 / 3) * _FILES_PER_BODY + _TEXT_ONLY_BODY_BYTES
            if attachments is not None
            else _TEXT_ONLY_BODY_BYTES
        )
        # An application's own routes, on this port: the contract's envelope and
        # version header apply to them too, and `caller()`/`conversation_key()`
        # give them what the hub's handlers have. (Users pinned to a pod by a
        # consistent hash of one ip:port must find the application's routes on
        # the same port as the engine's.)
        self._routes = list(routes)
        self._middlewares = list(middlewares)
        # The application's say in /readyz: what it cannot serve without (its
        # own store, say). Whether a dependency belongs here is its choice — a
        # pod taken out of rotation moves its users, which a store outage
        # every pod shares does not cure.
        self._readiness_probes = list(readiness_probes)
        # Where `startedSince` is answered from: the conversations the engine
        # started a turn in by itself, from the session inventory
        # (``crucible.conversation_files.engine_turns_lookup``). None = the
        # listing is not offered.
        self._engine_turns = engine_turns
        self._settling: set[asyncio.Task[TurnOutcome]] = set()
        self._runner: web.AppRunner | None = None
        self._started = False
        self._draining = False

    def register_agent(self, agent: str, sink: TrackedSink, chat: HttpChatClient) -> None:
        """Called by the gateway factory for every agent living on this hub."""
        self._agents[agent] = (sink, chat)

    def serves(self, agent: str) -> bool:
        """Whether this agent's conversations live on this hub — where a
        composition root decides which dispatcher an engine-started turn for
        the agent goes through."""
        return agent in self._agents

    # -- TurnDispatcher: a turn the engine starts on this gateway ---------------

    async def run_turn(self, request: TurnRequest) -> TurnOutcome:
        """Open a turn in the request's conversation the way a caller's message
        would — a journal the client reads, `activeTurn` while it runs — and
        answer with how it ended. ``conversation_id`` is the engine's key
        (realm-namespaced, as ``conversation_key`` spells it); the turn belongs
        to ``user_id`` in that realm, who is the one to read its journal."""
        entry = self._agents.get(request.agent)
        if entry is None:
            raise DispatchError(f"agent {request.agent!r} is not on this gateway")
        if self._draining:
            raise DispatchError("this engine is shutting down")
        sink, chat = entry
        realm, _, _ = request.conversation_id.partition(_SEP)
        self._sweep()
        try:
            turn, created = self.turns.start(
                agent=request.agent, conversation=request.conversation_id,
                owner=f"{realm}/{request.user_id}", client_message_id=request.message_id,
                started_by=STARTED_BY_ENGINE,
            )
        except TurnInProgress as exc:
            raise DispatchError("the conversation already has a turn running") from exc
        if not created:
            raise DispatchError(f"a turn for message {request.message_id!r} was already started")
        message = requested_message(
            request,
            ConversationRef(
                channel_id=request.conversation_id, conversation_id=request.conversation_id,
                message_id=f"{realm}{_SEP}{request.message_id}", thread_root_id="",
            ),
            turn=TurnScope(turn_id=turn.id),
        )
        message = replace(message, is_dm=True, timestamp=datetime.now(timezone.utc))
        try:
            outcome = sink.submit_tracked(message, chat)
        except Exception:
            self.turns.abandon(turn)
            raise
        return await self._settle(turn, outcome)

    # -- for an application's own routes ----------------------------------------

    @property
    def max_body(self) -> int:
        """The request-body cap the hub was built with, in bytes."""
        return self._max_body

    @staticmethod
    def conversation_key(caller: Caller, conversation_id: str) -> str:
        """The engine-side key of a caller's conversation — what `HttpTurns`
        and the session store know it by."""
        return _conversation_key(caller, conversation_id)

    @staticmethod
    def refuse(status: int, code: str, message: str, **extra: Any) -> Refusal:
        """The exception a route raises to answer in the hub's error envelope."""
        return Refusal(status, code, message, **extra)

    # -- draining ---------------------------------------------------------------

    def drain(self) -> None:
        """Stop taking new messages (503 `draining`; polls, answers and cancels
        still work) and report not ready, so a load balancer moves new users
        elsewhere while the turns already running here finish."""
        self._draining = True
        logger.info("http hub draining: no new turns")

    @property
    def draining(self) -> bool:
        return self._draining

    async def idle(self, timeout: float) -> bool:
        """Wait until no turn runs on this hub; False if some still do after
        ``timeout`` seconds."""
        try:
            await asyncio.wait_for(self.turns.idle.wait(), timeout)
        except asyncio.TimeoutError:
            return False
        return True

    # -- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        self._draining = False  # a hub started again takes messages again
        app = web.Application(
            middlewares=[self._envelope, *self._middlewares], client_max_size=self._max_body
        )
        app.router.add_get("/healthz", self._healthz)
        app.router.add_get("/readyz", self._readyz)
        app.router.add_get("/stats", self._stats)
        app.router.add_get("/v1/agents", self._list_agents)
        app.router.add_post("/v1/agents/{agent}/conversations/{conversation}/messages", self._post_message)
        app.router.add_get("/v1/agents/{agent}/conversations", self._list_conversations)
        app.router.add_get("/v1/agents/{agent}/conversations/{conversation}", self._get_conversation)
        app.router.add_post("/v1/agents/{agent}/conversations/{conversation}/cancel", self._cancel)
        app.router.add_post("/v1/agents/{agent}/conversations/{conversation}/actions", self._conversation_action)
        app.router.add_get("/v1/turns/{turn}/events", self._events)
        app.router.add_get("/v1/traces/{token}", self._trace)
        app.router.add_post("/v1/turns/{turn}/actions", self._action)
        app.add_routes(self._routes)
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
        except Refusal as exc:
            # The hub's own refusals and an authenticator's CallerRefused alike.
            response = web.json_response(
                {"error": {"code": exc.code, "message": str(exc)}, **exc.extra}, status=exc.status
            )
        except web.HTTPRequestEntityTooLarge:
            response = web.json_response(
                {"error": {"code": "too_large",
                           "message": f"the request body may not exceed {self._max_body} bytes"}},
                status=413,
            )
        except web.HTTPException as exc:
            # aiohttp's own answers (an unknown route, a wrong method) still say
            # which API they come from.
            exc.headers[_VERSION_HEADER] = API_VERSION
            raise
        except Exception:
            logger.exception("http hub: %s %s failed", request.method, request.path)
            response = web.json_response(
                {"error": {"code": "internal", "message": "the engine could not handle this"}},
                status=500,
            )
        response.headers[_VERSION_HEADER] = API_VERSION
        return response

    async def caller(self, request: web.Request, body: dict[str, Any] | None) -> Caller:
        caller = await self._callers.authenticate(request, body)
        if caller is None:
            raise Refusal(401, "unauthorized", "the request carries no accepted credentials")
        return caller

    async def body(self, request: web.Request) -> dict[str, Any]:
        # Nothing of a stranger's is parsed: no credentials at all is answered
        # before the body is read. (Whether they are GOOD credentials is the
        # authenticator's call, made after parsing — a credential may travel in
        # the body.)
        if not request.headers.get("Authorization", "").strip():
            raise Refusal(401, "unauthorized", "the request carries no credentials")
        try:
            data = await request.json()
        except ValueError as exc:
            raise Refusal(422, "validation", "the body is not valid JSON") from exc
        if not isinstance(data, dict):
            raise Refusal(422, "validation", "the body must be a JSON object")
        return data

    def _agent(self, request: web.Request, caller: Caller) -> tuple[str, TrackedSink, HttpChatClient]:
        agent = request.match_info["agent"]
        entry = self._agents.get(agent)
        if entry is None or (caller.agents is not None and agent not in caller.agents):
            raise Refusal(403, "forbidden", f"no agent {agent!r} for this caller")
        return agent, entry[0], entry[1]

    def _owned_turn(self, request: web.Request, caller: Caller) -> Turn:
        turn = self.turns.get(request.match_info["turn"])
        if turn is None or turn.owner != caller.owner:
            raise Refusal(404, "not_found", "no such turn")
        return turn

    def _wait(self, request: web.Request) -> float:
        raw = request.query.get("wait", "0")
        try:
            wait = float(raw)
        except ValueError as exc:
            raise Refusal(422, "validation", "`wait` must be a number of seconds") from exc
        return max(0.0, min(wait, self._max_wait_s))

    @staticmethod
    def _after(request: web.Request) -> int:
        raw = request.query.get("after", "0")
        try:
            return max(0, int(raw))
        except ValueError as exc:
            raise Refusal(422, "validation", "`after` must be an integer") from exc

    # -- handlers -------------------------------------------------------------

    async def _healthz(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def _readyz(self, request: web.Request) -> web.Response:
        """Ready means able to take a request — not idle. A full runtime pool is
        a busy pod, not a broken one: a new turn waits for a slot or evicts an
        idle session, and a load balancer that took the pod out would move its
        users away from the turns running on it. Load is on /stats."""
        if not self._started:
            return web.json_response({"ready": False, "reason": "starting"}, status=503)
        if self._draining:
            return web.json_response({"ready": False, "reason": "draining"}, status=503)
        for probe in self._readiness_probes:
            reason = await _probe_reason(probe)
            if reason is not None:
                return web.json_response({"ready": False, "reason": reason}, status=503)
        return web.json_response({"ready": True})

    async def _stats(self, request: web.Request) -> web.Response:
        """How loaded this hub is, for whoever scales or watches it: the
        runtime's slots and the turns in flight here."""
        runtime = self._control.stats().__dict__ if self._control is not None else None
        return web.json_response({"runtime": runtime, "turns": {"active": self.turns.active_count()}})

    async def _list_agents(self, request: web.Request) -> web.Response:
        caller = await self.caller(request, None)
        infos = {a.name: a for a in self._directory.list_agents()} if self._directory else {}
        agents = [
            {"name": name, "role": infos[name].role if name in infos else "",
             "description": infos[name].description if name in infos else ""}
            for name in sorted(self._agents)
            if caller.agents is None or name in caller.agents
        ]
        return web.json_response({"agents": agents})

    async def _post_message(self, request: web.Request) -> web.Response:
        body = await self.body(request)
        caller = await self.caller(request, body)
        agent, sink, chat = self._agent(request, caller)
        conversation_id = request.match_info["conversation"]
        client_message_id = str(body.get("clientMessageId") or "")
        if not _CLIENT_ID.match(client_message_id):
            raise Refusal(422, "validation", "`clientMessageId` is required: 1-128 url-safe characters")
        conversation = self.conversation_key(caller, conversation_id)
        # Draining refuses NEW turns. The retry of a message this hub already
        # answered is not one: it gets its turn id back, as it would have
        # without the drain, so the client follows the turn here instead of
        # starting it again elsewhere.
        if self._draining and self.turns.known(conversation, client_message_id) is None:
            raise Refusal(503, "draining", "this engine is shutting down; send the message elsewhere")
        text = body.get("text")
        if not isinstance(text, str) or len(text) > MAX_TEXT:
            raise Refusal(422, "validation", f"`text` must be a string of at most {MAX_TEXT} characters")
        files_problem = files_error(body)
        if files_problem is not None:
            raise Refusal(422, "validation", files_problem)
        try:
            files = frame_files(body)
        except ValueError as exc:
            raise Refusal(422, "validation", str(exc)) from exc
        if not text.strip() and not files:
            raise Refusal(422, "validation", "`text` must not be empty unless files are attached")

        self._sweep()
        if (running := self.turns.active(conversation)) is not None and \
                running.client_message_id != client_message_id:
            raise Refusal(
                409, "turn_in_progress", "this conversation already has a turn running",
                turnId=running.id,
            )
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
            gateway=GATEWAY_HTTP,
        )
        # Saved before the turn exists — a failure here must leave no turn behind
        # that is active and will never finish — under the conversation as the
        # application keys it, where a tool's files directory is.
        stored = ()
        if files and self._attachments is not None:
            stored = await self._attachments.save_many(agent, self._keying.key(agent, message), files)
        # Checked again past the only await above: a drain that began while the
        # files were saved must not be followed by a turn `idle()` never saw.
        if self._draining and self.turns.known(conversation, client_message_id) is None:
            raise Refusal(503, "draining", "this engine is shutting down; send the message elsewhere")
        try:
            turn, created = self.turns.start(
                agent=agent, conversation=conversation, owner=caller.owner,
                client_message_id=client_message_id,
            )
        except TurnInProgress as exc:  # another request of this conversation won the race
            raise Refusal(
                409, "turn_in_progress", "this conversation already has a turn running",
                turnId=exc.turn_id,
            ) from exc
        answer = {"turnId": turn.id, "conversationId": conversation_id, "cursor": 0}
        if not created:
            return web.json_response(answer, status=200)  # the retry of a message we have

        message = replace(
            message,
            # When the hub accepted it: the speaker line's time is how the
            # model knows what "now" is.
            timestamp=datetime.now(timezone.utc),
            # The hub's turn id travels on the scope: a flow or a tool that keeps
            # a transcript of its own then shares a key with the journal. Fresh
            # flags: `replace` would hand the authenticator's own set to the
            # turn, and a flag a tool left would then outlive it.
            turn=replace(caller.turn, turn_id=turn.id, flags=set()) if caller.turn is not None
            else TurnScope(turn_id=turn.id),
            attachments=tuple(stored),
        )
        try:
            outcome = sink.submit_tracked(message, chat)
        except Exception:
            self.turns.abandon(turn)
            raise
        task = asyncio.ensure_future(self._settle(turn, outcome))
        self._settling.add(task)
        task.add_done_callback(self._settling.discard)
        return web.json_response(answer, status=202)

    async def _settle(self, turn: Turn, outcome) -> TurnOutcome:
        try:
            result = await outcome
        except asyncio.CancelledError:
            self.turns.abandon(turn)  # the engine is stopping: nobody will answer
            raise
        except Exception:
            logger.exception("turn %s: the flow raised", turn.id)
            result = TurnOutcome.ERROR
        self.turns.finish(turn, result)
        return result

    def _sweep(self) -> None:
        self.turns.sweep(asyncio.get_running_loop().time())

    async def _events(self, request: web.Request) -> web.Response:
        caller = await self.caller(request, None)
        self._sweep()
        turn = self._owned_turn(request, caller)
        after = self._after(request)
        found = await turn.journal.wait(after, self._wait(request))
        return web.json_response({
            "turnId": turn.id,
            "status": turn.journal.status,
            "events": [e.to_wire() for e in found],
            "cursor": found[-1].seq if found else after,
        })

    async def _trace(self, request: web.Request) -> web.Response:
        """The calls of a turn that has ended, by the token its `turn.finished`
        carried: what the journal said while it ran, kept past the journal's
        retention. The token came to the caller in their own journal; the
        record is answered to a caller of that conversation's realm."""
        caller = await self.caller(request, None)
        record = await self.turns.trace(request.match_info["token"])
        if record is None or not record.conversation_id.startswith(f"{caller.realm}{_SEP}"):
            raise Refusal(404, "not_found", "no such trace")
        return web.json_response({
            "token": record.token,
            "agent": record.agent,
            "conversationId": record.conversation_id.removeprefix(f"{caller.realm}{_SEP}"),
            "createdAt": record.created_at,
            "finishedAt": record.finished_at,
            "calls": [
                {
                    "index": call.index, "callId": call.call_id, "tool": call.tool, "caption": call.caption,
                    "args": call.args, "cut": call.cut, "state": call.state, "durationS": call.duration_s,
                }
                for call in decode_calls(record.calls)
            ],
        })

    async def _list_conversations(self, request: web.Request) -> web.Response:
        """The caller's conversations the engine started a turn in since a
        moment: one request for a badge, instead of polling every conversation
        the program knows. Only the caller's own — their realm, their user."""
        caller = await self.caller(request, None)
        agent, _, _ = self._agent(request, caller)
        if self._engine_turns is None:
            raise Refusal(404, "not_found", "this engine does not list engine-started turns")
        since = request.query.get("startedSince", "")
        try:
            datetime.fromisoformat(since)
        except ValueError as exc:
            raise Refusal(422, "validation", "`startedSince` must be an ISO-8601 time") from exc
        prefix = f"{caller.realm}{_SEP}"
        found = [
            {"conversationId": turn.conversation_id.removeprefix(prefix), "startedAt": turn.at}
            for turn in await self._engine_turns(agent, since)
            if turn.conversation_id.startswith(prefix) and turn.user_id == caller.user_id
        ]
        return web.json_response({"conversations": found})

    async def _get_conversation(self, request: web.Request) -> web.Response:
        caller = await self.caller(request, None)
        self._agent(request, caller)
        self._sweep()
        conversation_id = request.match_info["conversation"]
        key = self.conversation_key(caller, conversation_id)
        turn = self.turns.active(key)
        active = None
        if turn is not None and turn.owner == caller.owner:
            active = {"turnId": turn.id, "status": turn.journal.status, "cursor": turn.journal.cursor}
        # Cards the conversation holds outside any turn of this gateway's:
        # raised by a turn on another gateway, forwarded here. Shown to the
        # caller only when the approvals would take their answer: a card's
        # arguments are the asker's business, as a turn's journal is its owner's.
        pending = []
        for held in await self.turns.held_by(key):
            card = approval_card_from_json(held.card)
            if _expired(card.expires_at):
                # The question is over whatever became of the pod that asked
                # it; a card nobody can close any more is let go here.
                await self.turns.release(held.post_id)
                continue
            actions = actions_from_json(held.actions)
            if await self._caller_may_answer(actions, caller):
                pending.append({
                    "postId": held.post_id, **wire_card(card), "actions": [wire_action(a) for a in actions],
                })
        return web.json_response({"conversationId": conversation_id, "activeTurn": active, "pendingCards": pending})

    async def _caller_may_answer(self, actions: tuple[Action, ...], caller: Caller) -> bool:
        if self._dispatcher is None:
            return False
        tokens = {str(a.context.get(APPROVAL_KEY) or "") for a in actions} - {""}
        for token in tokens:
            if await self._dispatcher.may_answer(token, caller.user_id, gateway=GATEWAY_HTTP):
                return True
        return False

    async def _conversation_action(self, request: web.Request) -> web.Response:
        """An answer to a card the conversation holds outside a turn — a
        confirmation raised on another gateway. The same body as a turn's
        actions; the card's own token says what is answered."""
        body = await self.body(request)
        caller = await self.caller(request, body)
        self._agent(request, caller)
        if self._dispatcher is None:
            raise Refusal(404, "not_found", "interactivity is off on this engine")
        key = self.conversation_key(caller, request.match_info["conversation"])
        post_id = str(body.get("postId") or "")
        held = await self.turns.held(post_id)
        actions = actions_from_json(held.actions) if held is not None and held.conversation == key else ()
        action_id = str(body.get("actionId") or "")
        action = next((a for a in actions if a.id == action_id), None)
        if action is None:
            raise Refusal(404, "not_found", "no such control; the card may have been answered")
        token = str(action.context.get(APPROVAL_KEY) or "")
        if not token:
            raise Refusal(409, "not_available", "this control is not a confirmation's")
        value = action.value if action.kind == ACTION_BUTTON else str(body.get("value") or "")
        outcome = await self._dispatcher.resolve_approval(token, value, caller.user_id, gateway=GATEWAY_HTTP)
        if outcome is ApprovalOutcome.NOT_MINE:
            await self.turns.release(post_id)
        return web.json_response({"outcome": outcome.name.lower()})

    async def _cancel(self, request: web.Request) -> web.Response:
        caller = await self.caller(request, None)
        _, sink, _ = self._agent(request, caller)
        key = self.conversation_key(caller, request.match_info["conversation"])
        turn = self.turns.active(key)
        if turn is None or turn.owner != caller.owner:
            raise Refusal(404, "not_found", "no turn is running in this conversation")
        if isinstance(sink, StoppableSink):
            # The flow ends the turn as stopped: the reply it had, a `stopped`
            # notice, a note for the next turn — the same as a /stop in chat.
            cancelled = await sink.stop(key)
        elif self._control is not None and turn.runtime_session_id:
            cancelled = await self._control.cancel(turn.runtime_session_id)
        else:
            raise Refusal(409, "not_cancellable", "this turn cannot be interrupted here")
        return web.json_response({"turnId": turn.id, "cancelled": cancelled})

    async def _action(self, request: web.Request) -> web.Response:
        body = await self.body(request)
        caller = await self.caller(request, body)
        turn = self._owned_turn(request, caller)
        if self._dispatcher is None:
            raise Refusal(404, "not_found", "interactivity is off on this engine")
        post_id = str(body.get("postId") or "")
        action_id = str(body.get("actionId") or "")
        posted = turn.posted.get(post_id)
        action = next((a for a in posted or () if a.id == action_id), None)
        if action is None:
            raise Refusal(404, "not_found", "no such control; it may have been retired")
        # The value is the control's, not the caller's: a button carries its
        # own, a menu offers a fixed set. What the card showed is what is answered.
        if action.kind == ACTION_BUTTON:
            value = action.value
        else:
            value = str(body.get("value") or "")
            if action.options and value not in {c.value for c in action.options}:
                raise Refusal(422, "validation", "`value` is not one of the control's options")
        context: Mapping[str, Any] = action.context
        if context.get(APPROVAL_KEY):
            outcome = await self._dispatcher.resolve_approval(
                str(context[APPROVAL_KEY]), value, caller.user_id, gateway=GATEWAY_HTTP
            )
            if outcome is ApprovalOutcome.NOT_MINE:
                self._retire(turn, post_id, "These controls are no longer active.")
            return web.json_response({"outcome": outcome.name.lower()})
        if context.get(SCREEN_KEY):
            redrawn = await self._dispatcher.redraw_screen(
                str(context.get(STATE_KEY, "")), value, post_id=post_id, user_id=caller.user_id
            )
            # The caller is the clicker: what they alone are told is theirs to show.
            note = redrawn.private_note if redrawn is not None else ""
            return web.json_response({"outcome": "redrawn", **({"privateNote": note} if note else {})})
        if context.get("form"):
            raise Refusal(409, "not_available", "forms cannot be opened on this gateway")
        # What remains is a request the turn is blocked on — a confirmation, a
        # question the runtime asked. Widgets the agent fires and forgets are
        # not advertised on this gateway, so a token nobody waits on is a card
        # that has already been answered or withdrawn.
        token = str(context.get("token", ""))
        if not await self._dispatcher.resolve_pending(token, value):
            self._retire(turn, post_id, "These controls are no longer active.")
            raise Refusal(404, "not_found", "nothing is waiting for this answer any more")
        self._retire(turn, post_id, f"You chose: {action.label or value}" if value else "Answered.")
        return web.json_response({"outcome": "resolved"})

    def _retire(self, turn: Turn, post_id: str, text: str) -> None:
        turn.posted.pop(post_id, None)
        turn.blocking.discard(post_id)
        turn.journal.emit(EV_ACTIONS_RETIRED, postId=post_id, text=text)
        if not turn.blocking:
            turn.journal.set_status(STATUS_RUNNING)
