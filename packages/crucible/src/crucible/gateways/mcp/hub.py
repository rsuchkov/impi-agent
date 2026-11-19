"""McpHub: the engine as an MCP server, for an agent on the outside.

Another agent — a person's assistant in their IDE, a workflow in some other
system — reaches the agents here the way the engine reaches an MCP server of
its own: ``tools/list``, ``tools/call``, over streamable HTTP. The tools it
sees are the application's (``McpGatewayTool``), ``ask_agent`` among them:
the agent itself, asked in words, answering with a turn of its own in a
conversation it remembers (``tools.META_CONVERSATION``).

Stateless, as the current revision of the protocol is (2026-07-28): every
request carries its protocol version and method in headers and its ``_meta``,
and any request may reach any replica. A legacy client, which would open a
session with ``initialize``, is told which version to speak and is not served
otherwise.

Who calls is a person: every request carries a bearer token a person signed
in for and consented to (``Authorization``), the turn runs as them, and a
tool that needs their yes asks them — not the client, which speaks for them
and may not answer for them — where the application puts the card
(``ApprovalCards``). The gateway's own tools say which scope they need; a
token without it does not see them.

The turns in flight are kept the http gateway's way (``HttpTurns``): one
running turn per conversation, a journal the answer is read out of when the
turn ends and progress is read from while it runs. The application wires the
hub's ``turns`` as the flow's tracer for ``GATEWAY_MCP``.
"""

import asyncio
import base64
import json
import logging
import re
import secrets
import uuid
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from aiohttp import web

from crucible.gateways.http.journal import (
    EV_FILE,
    EV_MESSAGE,
    EV_NOTICE,
    EV_PROGRESS,
    EV_TOOL_AWAITING,
    EV_TOOL_STARTED,
)
from crucible.gateways.http.turns import HttpTurns, Turn, TurnInProgress
from crucible.gateways.mcp.auth import Authorization, Principal
from crucible.gateways.mcp.client import ApprovalCards, McpChatClient
from crucible.gateways.mcp.tasks import (
    RESULT_WAIT_S,
    TASK_SUPPORT_OPTIONAL,
    TASK_SUPPORT_REQUIRED,
    TERMINAL,
    McpTask,
    McpTasks,
)
from crucible.gateways.mcp.tools import (
    META_CONVERSATION,
    AskAgentTool,
    CallFailed,
    GatewayCall,
    McpGatewayTool,
)
from crucible.mcp.protocol import (
    ERROR_HEADER_MISMATCH,
    ERROR_UNSUPPORTED_VERSION,
    META_PROTOCOL_VERSION,
    META_SERVER_INFO,
    METHOD_HEADER,
    MODERN_VERSION,
    NAME_HEADER,
    PROTOCOL_VERSION_HEADER,
)
from crucible.ports.chat.flow import StoppableSink, TrackedSink, TurnOutcome
from crucible.ports.chat.gateway import (
    GATEWAY_MCP,
    MCP_CLIENT_ATTRIBUTE,
    MCP_SCOPES_ATTRIBUTE,
)
from crucible.ports.chat.types import KIND_DM, ConversationRef, IncomingMessage
from crucible.ports.turn import TurnScope

logger = logging.getLogger(__name__)

MCP_PATH = "/mcp"
# Where the protected-resource metadata is served (the authorization server's
# address, for a client that was answered 401): RFC 9728, under the resource's path.
RESOURCE_METADATA_PATH = "/.well-known/oauth-protected-resource/mcp"
SERVER_INFO: Mapping[str, str] = {"name": "crucible", "version": "1"}
# One request may weigh this much: a question and its arguments, no files.
MAX_BODY_BYTES = 1024 * 1024
# How much of an agent's answer goes back to the client.
ANSWER_MAX = 100_000
# JSON-RPC's own codes, beside the protocol's in ``crucible.mcp.protocol``.
ERROR_PARSE = -32700
ERROR_INVALID_REQUEST = -32600
ERROR_METHOD_NOT_FOUND = -32601
ERROR_INVALID_PARAMS = -32602
ERROR_INTERNAL = -32603
# The server's own: a task's result asked for before the task has ended.
ERROR_TASK_NOT_DONE = -32002
# How the gateway keys a principal's conversation: the subject is in the key,
# so a handle presented for another subject names another conversation.
_SEP = ":"
_HANDLE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_FAILED = frozenset({TurnOutcome.TIMEOUT, TurnOutcome.ERROR})
_PROGRESS_WAIT_S = 1.0


def conversation_key(subject: str, handle: str) -> str:
    """The engine-side key of a principal's conversation — what the session
    store and ``HttpTurns`` know it by."""
    return f"{GATEWAY_MCP}{_SEP}{subject}{_SEP}{handle}"


class _Refused(Exception):
    """A JSON-RPC error with the HTTP status it travels under."""

    def __init__(self, code: int, message: str, *, status: int = 200, data: Any = None, headers: Mapping[str, str] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.data = data
        self.headers = dict(headers or {})


def _header_text(value: str) -> str:
    """A header's value as the client meant it: the Base64 sentinel form the
    revision defines for a value a header cannot carry plainly, decoded."""
    if value.startswith("=?base64?") and value.endswith("?="):
        try:
            return base64.b64decode(value[9:-2], validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return value
    return value


def _word_of(event_type: str, payload: Mapping[str, Any]) -> str:
    """What a journal event says to a client watching the call."""
    if event_type == EV_TOOL_STARTED:
        return str(payload.get("caption") or payload.get("tool") or "")
    if event_type == EV_PROGRESS:
        return str(payload.get("text") or "")
    if event_type == EV_TOOL_AWAITING:
        return f"waiting for the person to confirm {payload.get('tool', 'a call')}"
    return ""


class McpHub:
    def __init__(
        self,
        host: str,
        port: int,
        authorization: Authorization,
        *,
        public_url: str = "",
        tools: Sequence[McpGatewayTool] | None = None,
        cards: ApprovalCards | None = None,
        presenter: Callable[[str], str] | None = None,
        instructions: str = "",
        allowed_origins: Collection[str] = (),
        turns: HttpTurns | None = None,
        server_info: Mapping[str, str] = SERVER_INFO,
        max_body: int = MAX_BODY_BYTES,
        routes: Iterable[web.AbstractRouteDef] = (),
    ) -> None:
        self._host = host
        self._port = port
        self._authorization = authorization
        # Where clients reach this gateway from outside, for the metadata address
        # a 401 names; "" names it relative to the request.
        self._public_url = public_url.rstrip("/")
        offered = list(tools) if tools is not None else [AskAgentTool()]
        self._tools: dict[str, McpGatewayTool] = {tool.name: tool for tool in offered}
        # Where the application puts a card in front of the person; None = a
        # tool that needs a yes is refused.
        self.cards = cards
        # The agent's answer as the client gets it (Markdown as-is when None).
        self._present = presenter or (lambda text: text)
        self._instructions = instructions
        # Browsers name their origin; one not listed is refused (DNS rebinding).
        # A program names none and is not asked to.
        self._allowed_origins = frozenset(allowed_origins)
        self.turns = turns if turns is not None else HttpTurns()
        self._server_info = dict(server_info)
        self._max_body = max_body
        # Routes served beside /mcp on the same listener: the authorization
        # server's (``AuthorizationServer.routes()``), an application's own.
        self._routes = list(routes)
        self._agents: dict[str, tuple[TrackedSink, McpChatClient]] = {}
        # conversation key -> the principal whose call runs there now.
        self._askers: dict[str, Principal] = {}
        self.tasks = McpTasks()
        self._runner: web.AppRunner | None = None

    # -- agents -------------------------------------------------------------------

    def register_agent(self, agent: str, sink: TrackedSink, chat: McpChatClient) -> None:
        self._agents[agent] = (sink, chat)

    def serves(self, agent: str) -> bool:
        return agent in self._agents

    @property
    def agents(self) -> tuple[str, ...]:
        return tuple(sorted(self._agents))

    def asker_of(self, conversation: str) -> Principal | None:
        """The principal whose call is running in ``conversation``, or None."""
        return self._askers.get(conversation)

    @property
    def tools(self) -> tuple[McpGatewayTool, ...]:
        return tuple(self._tools.values())

    # -- lifecycle --------------------------------------------------------------

    async def start(self) -> None:
        app = web.Application(client_max_size=self._max_body)
        app.router.add_post(MCP_PATH, self._mcp)
        app.add_routes(self._routes)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        await web.TCPSite(self._runner, self._host, self._port).start()
        logger.info(
            "mcp gateway on http://%s:%d%s (%d agent(s), %d tool(s))",
            self._host, self._port, MCP_PATH, len(self._agents), len(self._tools),
        )

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # -- the one route --------------------------------------------------------------

    async def _mcp(self, request: web.Request) -> web.StreamResponse:
        origin = request.headers.get("Origin")
        if origin is not None and origin not in self._allowed_origins:
            return web.Response(status=403, text="origin not allowed")
        principal = await self._authorization.authenticate(request)
        if principal is None:
            metadata = f"{self._public_url}{RESOURCE_METADATA_PATH}"
            return web.Response(status=401, headers={"WWW-Authenticate": f'Bearer resource_metadata="{metadata}"'})
        rid: Any = None
        try:
            try:
                body = await request.json()
            except ValueError as exc:
                raise _Refused(ERROR_PARSE, "the body is not JSON", status=400) from exc
            if isinstance(body, list):
                raise _Refused(ERROR_INVALID_REQUEST, "one request per POST; batches are not supported", status=400)
            if not isinstance(body, Mapping) or body.get("jsonrpc") != "2.0" or not isinstance(body.get("method"), str):
                raise _Refused(ERROR_INVALID_REQUEST, "not a JSON-RPC 2.0 request", status=400)
            rid = body.get("id")
            method: str = body["method"]
            params = body.get("params") or {}
            if not isinstance(params, Mapping):
                raise _Refused(ERROR_INVALID_PARAMS, "params must be an object", status=400)
            meta = params.get("_meta") or {}
            if not isinstance(meta, Mapping):
                raise _Refused(ERROR_INVALID_PARAMS, "_meta must be an object", status=400)
            self._check_envelope(request, method, params, meta)
            if rid is None:
                return web.Response(status=202)  # a notification: taken, nothing to say
            return await self._dispatch(request, principal, method, params, meta, rid)
        except _Refused as exc:
            return self._error(rid, exc)
        except web.HTTPException:
            raise
        except Exception:
            logger.exception("mcp gateway: %s failed", request.path)
            return self._error(rid, _Refused(ERROR_INTERNAL, "the engine could not handle this", status=500))

    @staticmethod
    def _check_envelope(request: web.Request, method: str, params: Mapping[str, Any], meta: Mapping[str, Any]) -> None:
        """The modern era's envelope: the version in the header, mirrored in
        ``_meta``; the method and the name in headers, when the client sent
        them, saying what the body says. A client of another era — no version,
        an ``initialize`` — is told what this server speaks."""
        version = request.headers.get(PROTOCOL_VERSION_HEADER, "")
        if version != MODERN_VERSION:
            raise _Refused(
                ERROR_UNSUPPORTED_VERSION, f"this server speaks {MODERN_VERSION}",
                status=400, data={"supported": [MODERN_VERSION]},
            )
        if META_PROTOCOL_VERSION in meta and meta[META_PROTOCOL_VERSION] != version:
            raise _Refused(ERROR_HEADER_MISMATCH, "the protocol version in _meta differs from the header", status=400)
        if (named := request.headers.get(METHOD_HEADER)) is not None and named != method:
            raise _Refused(ERROR_HEADER_MISMATCH, f"{METHOD_HEADER} says {named!r}, the body {method!r}", status=400)
        if (named := request.headers.get(NAME_HEADER)) is not None:
            in_body = params.get("name") or params.get("uri") or ""
            if _header_text(named) != in_body:
                raise _Refused(ERROR_HEADER_MISMATCH, f"{NAME_HEADER} differs from the body's name", status=400)

    async def _dispatch(
        self, request: web.Request, principal: Principal, method: str, params: Mapping[str, Any],
        meta: Mapping[str, Any], rid: Any,
    ) -> web.StreamResponse:
        if method == "ping":
            return self._result(rid, {})
        if method == "server/discover":
            return self._result(rid, {
                "supportedVersions": [MODERN_VERSION],
                "capabilities": {"tools": {}, "tasks": {"requests": {"tools": {"call": {}}}, "list": {}, "cancel": {}}},
                "serverInfo": dict(self._server_info),
                "instructions": self._instructions,
                "_meta": {META_SERVER_INFO: dict(self._server_info)},
            })
        if method == "tools/list":
            return self._result(rid, {
                "tools": [self._declare(tool) for tool in self._tools.values() if principal.allows(tool.scope)]
            })
        if method == "tools/call":
            return await self._call(request, principal, params, meta, rid)
        if method in ("tasks/get", "tasks/result", "tasks/cancel"):
            return await self._task(principal, method, params, rid)
        if method == "tasks/list":
            self.tasks.sweep(asyncio.get_running_loop().time())
            owner = _task_owner(principal)
            return self._result(rid, {"tasks": [self._task_wire(task) for task in self.tasks.owned_by(owner)]})
        raise _Refused(ERROR_METHOD_NOT_FOUND, f"{method}: not a method of this server")

    @staticmethod
    def _declare(tool: McpGatewayTool) -> dict[str, Any]:
        support = TASK_SUPPORT_REQUIRED if tool.requires_task else TASK_SUPPORT_OPTIONAL
        return {
            "name": tool.name, "description": tool.description, "inputSchema": dict(tool.input_schema),
            "execution": {"taskSupport": support},
        }

    # -- tools/call ----------------------------------------------------------------

    async def _call(
        self, request: web.Request, principal: Principal, params: Mapping[str, Any], meta: Mapping[str, Any], rid: Any,
    ) -> web.StreamResponse:
        name = params.get("name")
        tool = self._tools.get(name) if isinstance(name, str) else None
        if tool is None:
            raise _Refused(ERROR_INVALID_PARAMS, f"unknown tool {name!r}")
        if not principal.allows(tool.scope):
            raise _Refused(
                ERROR_INVALID_PARAMS, f"{tool.name} needs the scope {tool.scope!r}", status=403,
                headers={"WWW-Authenticate": f'Bearer error="insufficient_scope", scope="{tool.scope}"'},
            )
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, Mapping):
            raise _Refused(ERROR_INVALID_PARAMS, "arguments must be an object")
        handle = meta.get(META_CONVERSATION, "")
        if handle and not (isinstance(handle, str) and _HANDLE.match(handle)):
            raise _Refused(ERROR_INVALID_PARAMS, f"{META_CONVERSATION} is not a conversation handle of this gateway's")
        asked_task = params.get("task")
        if asked_task is not None and not isinstance(asked_task, Mapping):
            raise _Refused(ERROR_INVALID_PARAMS, "task must be an object")
        if asked_task is not None:
            return self._result(rid, {"task": self._start_task(principal, tool, arguments, handle, asked_task)})
        if tool.requires_task:
            return self._result(rid, {
                "content": [{"type": "text", "text": f"{tool.name} runs as a task: call it with task: {{ttl}} and read tasks/get"}],
                "isError": True,
            })
        progress_token = meta.get("progressToken")
        streaming = progress_token is not None and "text/event-stream" in request.headers.get("Accept", "")
        if not streaming:
            call = GatewayCall(principal, arguments, handle, asker=self._ask, progress=_no_progress)
            return self._result(rid, await self._run(tool, call))

        # The client asked to be told of progress: the answer is an event
        # stream, progress notifications first, the result last.
        response = web.StreamResponse(
            status=200, headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"}
        )
        await response.prepare(request)
        count = 0

        async def progress(text: str) -> None:
            nonlocal count
            count += 1
            await _event(response, {
                "jsonrpc": "2.0", "method": "notifications/progress",
                "params": {"progressToken": progress_token, "progress": count, "message": text},
            })

        call = GatewayCall(principal, arguments, handle, asker=self._ask, progress=progress, wants_progress=True)
        result = await self._run(tool, call)
        await _event(response, {"jsonrpc": "2.0", "id": rid, "result": result})
        await response.write_eof()
        return response

    async def _run(self, tool: McpGatewayTool, call: GatewayCall) -> dict[str, Any]:
        try:
            text, is_error = await tool.run(call), False
        except CallFailed as exc:
            text, is_error = str(exc), True
        except Exception:
            logger.exception("mcp gateway: tool %s failed", tool.name)
            text, is_error = f"{tool.name} failed; the engine's log says how", True
        result: dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": is_error}
        if call.handle:
            result["_meta"] = {META_CONVERSATION: call.handle}
        return result

    # -- tasks ------------------------------------------------------------------------------

    def _start_task(
        self, principal: Principal, tool: McpGatewayTool, arguments: Mapping[str, Any], handle: str, asked: Mapping[str, Any],
    ) -> dict[str, Any]:
        self.tasks.sweep(asyncio.get_running_loop().time())
        task_id = uuid.uuid4().hex
        holder: list[McpTask] = []

        async def progress(text: str) -> None:
            if holder:
                holder[0].progress(text)

        call = GatewayCall(principal, arguments, handle, asker=self._ask, progress=progress, wants_progress=True)
        runner = asyncio.ensure_future(self._run(tool, call))
        task = self.tasks.create(task_id, _task_owner(principal), call, runner, asked)
        holder.append(task)
        return self._task_wire(task)

    def _task_wire(self, task: McpTask) -> dict[str, Any]:
        call = task.call
        conversation = conversation_key(call.principal.subject, call.handle) if call.handle else ""
        status, message = task.status(self.turns, conversation)
        return task.wire(status, message)

    async def _task(self, principal: Principal, method: str, params: Mapping[str, Any], rid: Any) -> web.Response:
        self.tasks.sweep(asyncio.get_running_loop().time())
        task_id = params.get("taskId")
        task = self.tasks.get(_task_owner(principal), task_id) if isinstance(task_id, str) else None
        if task is None:
            raise _Refused(ERROR_INVALID_PARAMS, f"unknown task {task_id!r}")
        if method == "tasks/get":
            return self._result(rid, self._task_wire(task))
        if method == "tasks/cancel":
            status, _ = task.status(self.turns, "")
            if status in TERMINAL or task.cancelled:
                raise _Refused(ERROR_INVALID_PARAMS, f"task {task_id} has already ended")
            await self._cancel(task)
            return self._result(rid, self._task_wire(task))
        # tasks/result: the call's result, waited for a while.
        try:
            result = await asyncio.wait_for(asyncio.shield(task.runner), RESULT_WAIT_S)
        except asyncio.TimeoutError as exc:
            raise _Refused(ERROR_TASK_NOT_DONE, f"task {task_id} is still working; read tasks/get") from exc
        except asyncio.CancelledError as exc:
            raise _Refused(ERROR_INVALID_PARAMS, f"task {task_id} was cancelled") from exc
        except Exception as exc:
            raise _Refused(ERROR_INTERNAL, f"task {task_id} failed") from exc
        return self._result(rid, result)

    async def _cancel(self, task: McpTask) -> None:
        """End the task: the turn it runs, when one runs, and the call."""
        task.cancelled = True
        call = task.call
        if call.handle:
            conversation = conversation_key(call.principal.subject, call.handle)
            turn = self.turns.active(conversation)
            entry = self._agents.get(turn.agent) if turn is not None else None
            if entry is not None and isinstance(entry[0], StoppableSink):
                try:
                    await entry[0].stop(conversation, gateway=GATEWAY_MCP, user_id=call.principal.subject)
                except Exception:
                    logger.warning("mcp gateway: could not stop the turn of task %s", task.id, exc_info=True)
        task.runner.cancel()

    # -- a turn for a call ------------------------------------------------------------

    def _agent_for(self, agent: str) -> tuple[str, TrackedSink, McpChatClient]:
        if not self._agents:
            raise CallFailed("no agent is on this gateway")
        if not agent:
            if len(self._agents) > 1:
                raise CallFailed(f"name the agent: one of {', '.join(self.agents)}")
            agent = next(iter(self._agents))
        entry = self._agents.get(agent)
        if entry is None:
            raise CallFailed(f"agent {agent!r} is not on this gateway; the agents here: {', '.join(self.agents)}")
        return agent, *entry

    async def _ask(self, call: GatewayCall, text: str, agent: str) -> str:
        agent, sink, chat = self._agent_for(agent)
        principal = call.principal
        handle = call.handle or secrets.token_urlsafe(16)
        conversation = conversation_key(principal.subject, handle)
        message_id = uuid.uuid4().hex
        try:
            turn, _ = self.turns.start(
                agent=agent, conversation=conversation, owner=f"{GATEWAY_MCP}/{principal.subject}",
                client_message_id=message_id,
            )
        except TurnInProgress as exc:
            raise CallFailed("the conversation is still answering an earlier call; ask again once it is done") from exc
        call.handle = handle
        self._askers[conversation] = principal
        message = IncomingMessage(
            ref=ConversationRef(
                channel_id=conversation, conversation_id=conversation,
                message_id=f"{GATEWAY_MCP}{_SEP}{message_id}", thread_root_id="",
            ),
            text=text, user_id=principal.subject, username=principal.username or principal.subject,
            timestamp=datetime.now(timezone.utc), kind=KIND_DM, is_dm=True, mentioned=True, gateway=GATEWAY_MCP,
            turn=TurnScope(
                turn_id=turn.id,
                attributes={
                    MCP_CLIENT_ATTRIBUTE: principal.client,
                    MCP_SCOPES_ATTRIBUTE: " ".join(sorted(principal.scopes)),
                },
            ),
        )
        try:
            pending = asyncio.ensure_future(sink.submit_tracked(message, chat))
        except Exception:
            self._askers.pop(conversation, None)
            self.turns.abandon(turn)
            raise
        try:
            outcome = await self._follow(turn, pending, call)
        except BaseException:
            # The call is gone (the client hung up); the turn still runs and is
            # closed when it ends, so the conversation is not busy for ever.
            pending.add_done_callback(lambda done: self._close(turn, done))
            raise
        finally:
            self._askers.pop(conversation, None)
        self.turns.finish(turn, outcome)
        answer = self._answer(turn)
        if outcome in _FAILED:
            raise CallFailed(answer or f"the agent's turn ended: {outcome.name.lower()}")
        return self._present(answer)

    async def _follow(self, turn: Turn, pending: "asyncio.Future[TurnOutcome]", call: GatewayCall) -> TurnOutcome:
        """Wait for the turn, telling the client of its steps when it asked."""
        cursor = 0
        while call.wants_progress and not pending.done():
            waiter = asyncio.ensure_future(turn.journal.wait(cursor, _PROGRESS_WAIT_S))
            await asyncio.wait({pending, waiter}, return_when=asyncio.FIRST_COMPLETED)
            if not waiter.done():
                waiter.cancel()
            for event in turn.journal.since(cursor):
                cursor = event.seq
                if word := _word_of(event.type, event.payload):
                    await call.progress(word)
        try:
            return await pending
        except Exception:
            logger.exception("mcp gateway: the turn of %s failed", turn.conversation)
            return TurnOutcome.ERROR

    def _close(self, turn: Turn, done: "asyncio.Future[TurnOutcome]") -> None:
        if self.turns.active(turn.conversation) is not turn:
            return
        outcome = done.result() if not done.cancelled() and done.exception() is None else TurnOutcome.ERROR
        self.turns.finish(turn, outcome)

    @staticmethod
    def _answer(turn: Turn) -> str:
        """What the turn said, out of its journal: its replies, else its
        notices (how it ended, when it said nothing else); the files it sent,
        by name."""
        replies: list[str] = []
        notices: list[str] = []
        files: list[str] = []
        for event in turn.journal.since(0):
            if event.type == EV_MESSAGE:
                replies.append(str(event.payload.get("text", "")))
            elif event.type == EV_NOTICE:
                notices.append(str(event.payload.get("text", "")))
            elif event.type == EV_FILE:
                if caption := event.payload.get("text"):
                    replies.append(str(caption))
                files.append(str(event.payload.get("name", "")))
        parts = replies or notices
        if files:
            parts.append("Files the agent produced (kept in its conversation): " + ", ".join(files))
        return "\n\n".join(part for part in parts if part)[:ANSWER_MAX]

    # -- answers ----------------------------------------------------------------------

    @staticmethod
    def _result(rid: Any, result: Mapping[str, Any]) -> web.Response:
        return web.json_response({"jsonrpc": "2.0", "id": rid, "result": dict(result)})

    @staticmethod
    def _error(rid: Any, refused: _Refused) -> web.Response:
        error: dict[str, Any] = {"code": refused.code, "message": str(refused)}
        if refused.data is not None:
            error["data"] = refused.data
        return web.json_response(
            {"jsonrpc": "2.0", "id": rid, "error": error}, status=refused.status, headers=refused.headers
        )


async def _no_progress(text: str) -> None:
    return


def _task_owner(principal: Principal) -> str:
    """Whose a task is: the person and the program together — another client
    of the same person does not see it."""
    return f"{principal.subject}\n{principal.client}"


async def _event(response: web.StreamResponse, message: Mapping[str, Any]) -> None:
    await response.write(f"event: message\ndata: {json.dumps(message, ensure_ascii=False)}\n\n".encode("utf-8"))
