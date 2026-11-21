"""McpHub: the engine as an MCP server, for an agent on the outside.

Another agent — a person's assistant in their IDE, a workflow in some other
system — reaches the agents here the way the engine reaches an MCP server of
its own: ``tools/list``, ``tools/call``, over streamable HTTP. The tools it
sees are the application's (``McpGatewayTool``), ``ask_agent`` among them:
the agent itself, asked in words, answering with a turn of its own in a
conversation it remembers (``tools.CONVERSATION_KEY``).

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

A call that runs an agent's turn may take minutes. For a client that declared
the tasks extension it is a task: answered at once with a task id, polled
with ``tasks/get`` until it ends, read there. For any other client the call
waits as long as a call may (``call_wait_s``) and then answers with where the
turn stands — the client comes back with ``wait_agent``. Either way the turn
runs on the replica the call landed on and is written down as it goes
(``McpTurns``, in the store), so the next call about it may land anywhere:
a poll, a wait or a cancel reads the record, and the replica that runs the
turn reads the cancel. The turn's own journal (``HttpTurns``) stays with the
replica that runs it, for progress on the call's own stream.
"""

import asyncio
import base64
import hashlib
import json
import logging
import re
import secrets
import uuid
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

from aiohttp import web

from crucible.gateways.http.journal import (
    EV_FILE,
    EV_MESSAGE,
    EV_NOTICE,
    EV_PROGRESS,
    EV_TOOL_AWAITING,
    EV_TOOL_STARTED,
    STATUS_AWAITING_INPUT,
)
from crucible.gateways.http.turns import HttpTurns, Turn, TurnInProgress
from crucible.gateways.mcp.auth import (
    KEY_SEP,
    Authorization,
    McpCaller,
    conversation_key,
)
from crucible.gateways.mcp.client import ApprovalCards, McpChatClient
from crucible.gateways.mcp.tools import (
    META_CONVERSATION,
    AskAgentTool,
    CallFailed,
    GatewayCall,
    McpGatewayTool,
    TurnStanding,
    WaitAgentTool,
    standing_words,
)
from crucible.mcp.protocol import (
    ERROR_HEADER_MISMATCH,
    ERROR_MISSING_CAPABILITY,
    ERROR_UNSUPPORTED_VERSION,
    META_CLIENT_CAPABILITIES,
    META_PROTOCOL_VERSION,
    META_SERVER_INFO,
    METHOD_HEADER,
    MODERN_VERSION,
    NAME_HEADER,
    PROTOCOL_VERSION_HEADER,
    RESULT_COMPLETE,
)
from crucible.ports.chat.flow import StoppableSink, TrackedSink, TurnOutcome
from crucible.ports.chat.gateway import (
    GATEWAY_MCP,
    MCP_CLIENT_ATTRIBUTE,
    MCP_CLIENT_NAME_ATTRIBUTE,
    MCP_SCOPE_READ,
    MCP_SCOPES_ATTRIBUTE,
    mcp_scopes_attribute,
)
from crucible.ports.chat.types import KIND_DM, ConversationRef, IncomingMessage
from crucible.ports.mcp_turns import (
    TURN_AWAITING_APPROVAL,
    TURN_CANCELLED,
    TURN_DONE,
    TURN_ENDED,
    TURN_FAILED,
    TURN_WORKING,
    McpTurnRecord,
    McpTurns,
)
from crucible.ports.replica import Replica, Replicas
from crucible.ports.turn import TurnScope

logger = logging.getLogger(__name__)

MCP_PATH = "/mcp"
# Where the protected-resource metadata is served (the authorization server's
# address, for a client that was answered 401): RFC 9728, under the resource's path.
RESOURCE_METADATA_PATH = "/.well-known/oauth-protected-resource/mcp"
SERVER_INFO: Mapping[str, str] = {"name": "crucible", "version": "1"}
# The tasks extension, as a client declares it and the server offers it, and
# the result type that says "a task, not the answer".
TASKS_EXTENSION = "io.modelcontextprotocol/tasks"
RESULT_TASK = "task"
# One request may weigh this much: a question and its arguments, no files.
MAX_BODY_BYTES = 1024 * 1024
# How much of an agent's answer goes back to the client.
ANSWER_MAX = 100_000
# How long a call that runs a turn is held open for a client without tasks
# before it is answered with where the turn stands: under the minute some
# clients cut a call at.
CALL_WAIT_S = 45.0
# How long an ended turn is kept for the client to read; what a task's ttlMs says.
TASK_TTL_MS = 10 * 60 * 1000
POLL_INTERVAL_MS = 2000
# The same request again by the same caller within this many seconds, with no
# conversation named, joins the turn it opened: a client whose stream broke
# sends the call again, and a second turn would answer it twice.
REPEAT_WINDOW_S = 120.0
# A record of a turn still running is kept at least this long; once it has
# ended, the task's ttl.
_RUNNING_RETENTION_S = 24 * 3600
# JSON-RPC's own codes, beside the protocol's in ``crucible.mcp.protocol``.
ERROR_PARSE = -32700
ERROR_INVALID_REQUEST = -32600
ERROR_METHOD_NOT_FOUND = -32601
ERROR_INVALID_PARAMS = -32602
ERROR_INTERNAL = -32603
_HANDLE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TURN_FAILED = frozenset({TurnOutcome.TIMEOUT, TurnOutcome.ERROR})
_POLL_S = 1.0
_RESTARTED = "the engine running this turn restarted; ask again"
# A task's status for each of the gateway's own words.
_TASK_STATUS = {
    TURN_WORKING: "working", TURN_AWAITING_APPROVAL: "working",
    TURN_DONE: "completed", TURN_FAILED: "failed", TURN_CANCELLED: "cancelled",
}




def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    # Milliseconds: a record's retention is compared as text, and a short one
    # rounded to the second would be over before it began.
    return moment.isoformat(timespec="milliseconds")


class _RpcError(Exception):
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


def _declares_tasks(meta: Mapping[str, Any]) -> bool:
    capabilities = meta.get(META_CLIENT_CAPABILITIES)
    extensions = capabilities.get("extensions") if isinstance(capabilities, Mapping) else None
    return isinstance(extensions, Mapping) and TASKS_EXTENSION in extensions


def _request_digest(tool: str, arguments: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps({"tool": tool, "arguments": arguments}, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()


def _standing_of(record: McpTurnRecord) -> TurnStanding:
    return TurnStanding(
        handle=record.handle, state=record.state, step=record.step, answer=record.answer, is_error=record.is_error,
    )


class McpHub:
    def __init__(
        self,
        host: str,
        port: int,
        authorization: Authorization,
        *,
        store: McpTurns,
        public_url: str = "",
        tools: Sequence[McpGatewayTool] | None = None,
        cards: ApprovalCards | None = None,
        presenter: Callable[[str], str] | None = None,
        instructions: str = "",
        allowed_origins: Collection[str] = (),
        turns: HttpTurns | None = None,
        replica: Replica | None = None,
        replicas: Replicas | None = None,
        call_wait_s: float = CALL_WAIT_S,
        task_ttl_ms: int = TASK_TTL_MS,
        repeat_window_s: float = REPEAT_WINDOW_S,
        server_info: Mapping[str, str] = SERVER_INFO,
        max_body: int = MAX_BODY_BYTES,
        routes: Iterable[web.AbstractRouteDef] = (),
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._host = host
        self._port = port
        self._authorization = authorization
        # The turns' records, for every replica to read (the engine's store).
        self._store = store
        # Where clients reach this gateway from outside, for the metadata address
        # a 401 names; "" names it relative to the request.
        self._public_url = public_url.rstrip("/")
        offered = list(tools) if tools is not None else [AskAgentTool(), WaitAgentTool()]
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
        # This process among the replicas sharing the store, and the registry
        # that says which are alive: a record of a turn whose replica is gone
        # is a turn that will never end on its own.
        self._replica_id = replica.id if replica is not None else ""
        self._replicas = replicas
        self._call_wait_s = call_wait_s
        self._task_ttl_ms = task_ttl_ms
        self._repeat_window_s = repeat_window_s
        self._server_info = dict(server_info)
        self._max_body = max_body
        # Routes served beside /mcp on the same listener: the authorization
        # server's (``AuthorizationServer.routes()``), an application's own.
        self._routes = list(routes)
        self._clock = clock
        self._agents: dict[str, tuple[TrackedSink, McpChatClient]] = {}
        # conversation key -> the caller whose call runs there now (this replica's).
        self._askers: dict[str, McpCaller] = {}
        # turn id -> the call running it here.
        self._running: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._stopping = False
        self._runner: web.AppRunner | None = None

    # -- agents -------------------------------------------------------------------

    def register_agent(self, agent: str, sink: TrackedSink, chat: McpChatClient) -> None:
        self._agents[agent] = (sink, chat)

    @property
    def agents(self) -> tuple[str, ...]:
        return tuple(sorted(self._agents))

    def asker_of(self, conversation: str) -> McpCaller | None:
        """The caller whose call is running in ``conversation`` here, or None."""
        return self._askers.get(conversation)

    @property
    def tools(self) -> tuple[McpGatewayTool, ...]:
        return tuple(self._tools.values())

    # -- lifecycle --------------------------------------------------------------

    async def start(self) -> None:
        # Whatever this replica was running when it last stopped is over.
        self._stopping = False
        now = self._clock()
        failed = await self._store.fail_mcp_turns(self._replica_id, _RESTARTED, _iso(now), self._ended_until(now))
        gone = await self._store.purge_mcp_turns(_iso(now))
        if failed or gone:
            logger.info("mcp gateway: %d turn(s) of an earlier run closed, %d record(s) purged", failed, gone)
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
        # A turn cut by the engine stopping is not a client's cancel: its
        # record says the engine restarted, as it would had it crashed.
        self._stopping = True
        for runner in list(self._running.values()):
            runner.cancel()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # -- the one route --------------------------------------------------------------

    async def _mcp(self, request: web.Request) -> web.StreamResponse:
        origin = request.headers.get("Origin")
        if origin is not None and origin not in self._allowed_origins:
            return web.Response(status=403, text="origin not allowed")
        caller = await self._authorization.authenticate(request)
        if caller is None:
            metadata = f"{self._public_url}{RESOURCE_METADATA_PATH}"
            return web.Response(status=401, headers={"WWW-Authenticate": f'Bearer resource_metadata="{metadata}"'})
        rid: Any = None
        try:
            try:
                body = await request.json()
            except ValueError as exc:
                raise _RpcError(ERROR_PARSE, "the body is not JSON", status=400) from exc
            if isinstance(body, list):
                raise _RpcError(ERROR_INVALID_REQUEST, "one request per POST; batches are not supported", status=400)
            if not isinstance(body, Mapping) or body.get("jsonrpc") != "2.0" or not isinstance(body.get("method"), str):
                raise _RpcError(ERROR_INVALID_REQUEST, "not a JSON-RPC 2.0 request", status=400)
            rid = body.get("id")
            method: str = body["method"]
            params = body.get("params") or {}
            if not isinstance(params, Mapping):
                raise _RpcError(ERROR_INVALID_PARAMS, "params must be an object", status=400)
            meta = params.get("_meta") or {}
            if not isinstance(meta, Mapping):
                raise _RpcError(ERROR_INVALID_PARAMS, "_meta must be an object", status=400)
            self._check_envelope(request, method, params, meta)
            if rid is None:
                return web.Response(status=202)  # a notification: taken, nothing to say
            return await self._dispatch(request, caller, method, params, meta, rid)
        except _RpcError as exc:
            return self._error(rid, exc)
        except web.HTTPException:
            raise
        except Exception:
            logger.exception("mcp gateway: %s failed", request.path)
            return self._error(rid, _RpcError(ERROR_INTERNAL, "the engine could not handle this", status=500))

    @staticmethod
    def _check_envelope(request: web.Request, method: str, params: Mapping[str, Any], meta: Mapping[str, Any]) -> None:
        """The modern era's envelope: the version in the header, mirrored in
        ``_meta``; the method and the name in headers, when the client sent
        them, saying what the body says (a task's id is its name). A client
        of another era — no version, an older one — is told what this server
        speaks."""
        version = request.headers.get(PROTOCOL_VERSION_HEADER, "")
        if version != MODERN_VERSION:
            raise _RpcError(
                ERROR_UNSUPPORTED_VERSION, f"this server speaks {MODERN_VERSION}",
                status=400, data={"supported": [MODERN_VERSION]},
            )
        if META_PROTOCOL_VERSION in meta and meta[META_PROTOCOL_VERSION] != version:
            raise _RpcError(ERROR_HEADER_MISMATCH, "the protocol version in _meta differs from the header", status=400)
        if (named := request.headers.get(METHOD_HEADER)) is not None and named != method:
            raise _RpcError(ERROR_HEADER_MISMATCH, f"{METHOD_HEADER} says {named!r}, the body {method!r}", status=400)
        if (named := request.headers.get(NAME_HEADER)) is not None:
            in_body = params.get("name") or params.get("uri") or params.get("taskId") or ""
            if _header_text(named) != in_body:
                raise _RpcError(ERROR_HEADER_MISMATCH, f"{NAME_HEADER} differs from the body's name", status=400)

    async def _dispatch(
        self, request: web.Request, caller: McpCaller, method: str, params: Mapping[str, Any],
        meta: Mapping[str, Any], rid: Any,
    ) -> web.StreamResponse:
        tasks = _declares_tasks(meta)
        if method == "server/discover":
            return self._result(rid, {
                "supportedVersions": [MODERN_VERSION],
                "capabilities": {"tools": {}, "extensions": {TASKS_EXTENSION: {}}},
                "serverInfo": dict(self._server_info),
                "instructions": self._instructions,
                "_meta": {META_SERVER_INFO: dict(self._server_info)},
            })
        if method == "tools/list":
            return self._result(rid, {
                "tools": [self._declare(tool) for tool in self._tools.values() if caller.allows(tool.scope)]
            })
        if method == "tools/call":
            return await self._call(request, caller, params, meta, rid, tasks=tasks)
        if method in ("tasks/get", "tasks/update", "tasks/cancel"):
            if not tasks:
                raise _RpcError(
                    ERROR_MISSING_CAPABILITY, f"{method} needs the tasks extension declared on the request",
                    data={"requiredCapabilities": {"extensions": {TASKS_EXTENSION: {}}}},
                )
            return await self._serve_task(caller, method, params, rid)
        raise _RpcError(ERROR_METHOD_NOT_FOUND, f"{method}: not a method of this server")

    @staticmethod
    def _declare(tool: McpGatewayTool) -> dict[str, Any]:
        """The tool as ``tools/list`` says it: with its title when it has one,
        and its annotations — the ones it declares, else what its scope
        implies, so a client tells a reading tool from one that may act."""
        declared: dict[str, Any] = {"name": tool.name, "description": tool.description, "inputSchema": dict(tool.input_schema)}
        if tool.title:
            declared["title"] = tool.title
        annotations = tool.annotations
        if annotations is None:
            annotations = {"readOnlyHint": True} if tool.scope == MCP_SCOPE_READ else {"destructiveHint": True}
        declared["annotations"] = dict(annotations)
        return declared

    # -- tools/call ----------------------------------------------------------------

    async def _call(
        self, request: web.Request, caller: McpCaller, params: Mapping[str, Any], meta: Mapping[str, Any], rid: Any,
        *, tasks: bool,
    ) -> web.StreamResponse:
        name = params.get("name")
        tool = self._tools.get(name) if isinstance(name, str) else None
        if tool is None:
            raise _RpcError(ERROR_INVALID_PARAMS, f"unknown tool {name!r}")
        if not caller.allows(tool.scope):
            raise _RpcError(
                ERROR_INVALID_PARAMS, f"{tool.name} needs the scope {tool.scope!r}", status=403,
                headers={"WWW-Authenticate": f'Bearer error="insufficient_scope", scope="{tool.scope}"'},
            )
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, Mapping):
            raise _RpcError(ERROR_INVALID_PARAMS, "arguments must be an object")
        handle = self._handle_of(tool, arguments, meta)
        progress_token = meta.get("progressToken")
        streaming = progress_token is not None and "text/event-stream" in request.headers.get("Accept", "")
        if tool.long_running:
            # A call that runs a turn: a task for a client that follows tasks,
            # a bounded wait for any other, both reading the turn's record.
            # Another request into a conversation whose turn still runs is
            # told where that turn stands, and gets no result of a call it
            # did not make.
            record, same = await self._turn_to_join(caller, tool, arguments, handle)
            if record is not None and not same:
                return self._result(rid, self._standing_wire(_standing_of(record)))
            if record is None:
                record = await self._open_turn(caller, tool, arguments, handle)
            if tasks:
                return self._result(rid, self._task_wire(record, created=True))
            if not streaming:
                return self._result(rid, self._standing_wire(await self._await_turn(record.turn_id, self._call_wait_s)))
        elif not streaming:
            call = GatewayCall(
                caller, arguments, handle, ask_agent=self._ask, wait_turn=self._wait, progress=_no_progress,
            )
            return self._result(rid, self._wire(await self._run(tool, call), call.handle))

        # The client asked to be told of progress: an event stream, progress
        # notifications while the call lasts, the result last.
        response = web.StreamResponse(
            status=200, headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"}
        )
        await response.prepare(request)
        count = 0

        async def progress(word: str) -> None:
            nonlocal count
            count += 1
            await _event(response, {
                "jsonrpc": "2.0", "method": "notifications/progress",
                "params": {"progressToken": progress_token, "progress": count, "message": word},
            })

        if tool.long_running:
            result = self._standing_wire(await self._await_turn(record.turn_id, self._call_wait_s, progress=progress))
        else:
            call = GatewayCall(
                caller, arguments, handle, ask_agent=self._ask, wait_turn=self._wait, progress=progress,
            )
            result = self._wire(await self._run(tool, call), call.handle)
        await _event(response, {"jsonrpc": "2.0", "id": rid, "result": result})
        await response.write_eof()
        return response

    @staticmethod
    def _handle_of(tool: McpGatewayTool, arguments: Mapping[str, Any], meta: Mapping[str, Any]) -> str:
        """The conversation the call names: in the tool's own argument, else
        in ``_meta``; "" when it names none."""
        handle = arguments.get(tool.conversation_argument) if tool.conversation_argument else None
        if handle in (None, ""):
            handle = meta.get(META_CONVERSATION, "")
        if not handle:
            return ""
        if not (isinstance(handle, str) and _HANDLE.match(handle)):
            raise _RpcError(ERROR_INVALID_PARAMS, f"{handle!r} is not a conversation handle of this gateway's")
        return handle

    async def _run(self, tool: McpGatewayTool, call: GatewayCall) -> tuple[str, bool]:
        """The tool's answer and whether it is an error, whatever the tool did."""
        try:
            return await tool.run(call), False
        except CallFailed as exc:
            return str(exc), True
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("mcp gateway: tool %s failed", tool.name)
            return f"{tool.name} failed; the engine's log says how", True

    @staticmethod
    def _wire(answer: tuple[str, bool], handle: str) -> dict[str, Any]:
        """A call's result as the client reads it: the text, with the
        conversation named at its end where the client's model reads it, and
        the handle in ``_meta`` for a program. Nothing in ``structuredContent``:
        a client that has one hands its model that alone, and the answer with
        it would go unread."""
        text, is_error = answer
        result: dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": is_error}
        if handle:
            result["content"][0]["text"] = f"{text}\n\nConversation: {handle}" if text else f"Conversation: {handle}"
            result["_meta"] = {META_CONVERSATION: handle}
        return result

    def _standing_wire(self, state: TurnStanding) -> dict[str, Any]:
        """Where the turn stands, as a call's result: its answer once it has
        ended, else the word on what it is doing and how to come back."""
        if state.state in TURN_ENDED:
            return self._wire((state.answer, state.is_error or state.state != TURN_DONE), state.handle)
        return self._wire((standing_words(state), False), state.handle)

    # -- a turn for a long call --------------------------------------------------------

    async def _turn_to_join(
        self, caller: McpCaller, tool: McpGatewayTool, arguments: Mapping[str, Any], handle: str,
    ) -> tuple[McpTurnRecord | None, bool]:
        """The turn this call joins instead of opening one, and whether it is
        the same request: the conversation's turn still running (the same
        request again, or another one, which is only told where that turn
        stands), or — with no conversation named — the turn the same request
        opened a moment ago (a client whose stream broke asks again)."""
        digest = _request_digest(tool.name, arguments)
        if handle:
            latest = await self._store.latest_mcp_turn(conversation_key(caller.subject, handle))
            if latest is None or not await self._still_running(latest):
                return None, False
            return latest, latest.request_digest == digest
        recent = await self._store.find_mcp_turn(caller.subject, caller.client, digest)
        if recent is None or not await self._still_running(recent):
            return None, False
        started = datetime.fromisoformat(recent.started_at)
        if self._clock() - started > timedelta(seconds=self._repeat_window_s):
            return None, False
        return recent, True

    async def _open_turn(
        self, caller: McpCaller, tool: McpGatewayTool, arguments: Mapping[str, Any], handle: str,
    ) -> McpTurnRecord:
        """Start the call as a turn of its own: the record written — by the
        runner, which is known here before anything is written, so a look
        from this replica finds it running — then the tool."""
        handle = handle or secrets.token_urlsafe(16)
        now = self._clock()
        record = McpTurnRecord(
            turn_id=uuid.uuid4().hex, conversation=conversation_key(caller.subject, handle), handle=handle,
            subject=caller.subject, client=caller.client, agent="", replica=self._replica_id, state=TURN_WORKING,
            started_at=_iso(now), updated_at=_iso(now), expires_at=self._running_until(now),
            step="starting", request_digest=_request_digest(tool.name, arguments),
        )

        async def progress(word: str) -> None:
            # The tool's own word on what it is doing is the record's step —
            # what a poll reads — while no agent's turn says more.
            current = await self._store.get_mcp_turn(record.turn_id)
            if current is not None and current.state == TURN_WORKING and not current.agent:
                await self._store.save_mcp_turn(replace(current, step=word, updated_at=_iso(self._clock())))

        call = GatewayCall(
            caller, arguments, handle, ask_agent=self._ask, wait_turn=self._wait, progress=progress,
            turn_id=record.turn_id,
        )
        written: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        runner = asyncio.ensure_future(self._run_turn(tool, call, record, written))
        self._running[record.turn_id] = runner
        runner.add_done_callback(lambda _: self._running.pop(record.turn_id, None))
        await written
        return record

    async def _run_turn(
        self, tool: McpGatewayTool, call: GatewayCall, record: McpTurnRecord, written: "asyncio.Future[None]",
    ) -> dict[str, Any]:
        """The record, the tool, and the record again when it ends: what it
        answered, or that it was stopped."""
        try:
            await self._store.save_mcp_turn(record)
        finally:
            if not written.done():
                written.set_result(None)
        state, text, is_error = TURN_DONE, "", False
        try:
            text, is_error = await self._run(tool, call)
        except asyncio.CancelledError:
            if self._stopping:
                state, text, is_error = TURN_FAILED, _RESTARTED, True
            else:
                state, text, is_error = TURN_CANCELLED, "cancelled by the client", True
        except Exception:
            # Past the tool's own failure, which _run answers: the store, say.
            logger.exception("mcp gateway: the turn %s failed", record.turn_id)
            state, text, is_error = TURN_FAILED, "the call failed; the engine's log says how", True
        latest = await self._store.get_mcp_turn(record.turn_id) or record
        if latest.cancel_requested and is_error and state == TURN_DONE:
            # Asked to stop, and the turn ended without an answer: stopped.
            # An answer given despite the ask stands — the agent acted.
            state = TURN_CANCELLED
        now = self._clock()
        await self._store.save_mcp_turn(replace(
            latest, state=state, step="", answer=text, is_error=is_error, updated_at=_iso(now),
            expires_at=self._ended_until(now),
        ))
        await self._store.purge_mcp_turns(_iso(now))  # whatever is past its time goes with this one's end
        return self._wire((text, is_error), call.handle)

    def _running_until(self, now: datetime) -> str:
        """Until when a record of a turn still running is kept — moved on at
        every write, so a long turn is never purged under itself."""
        return _iso(now + timedelta(seconds=_RUNNING_RETENTION_S))

    def _ended_until(self, now: datetime) -> str:
        """Until when a record of a turn that ended is kept: the task's ttl."""
        return _iso(now + timedelta(milliseconds=self._task_ttl_ms))

    async def _await_turn(
        self, turn_id: str, wait_s: float, *, progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> TurnStanding:
        """Where the turn stands after waiting up to ``wait_s`` for it to end.
        ``progress`` is told what the turn is on as it goes: from the turn's
        own journal when it runs here, from its record's steps when it runs
        on another replica."""
        deadline = asyncio.get_running_loop().time() + wait_s
        cursor = 0
        seen: tuple[str, str] = ("", "")

        async def flush(turn: Turn | None) -> None:
            # The journal's words since the last look, for a client that listens.
            nonlocal cursor
            for event in turn.journal.since(cursor) if turn is not None else ():
                cursor = event.seq
                if progress is not None and (word := _word_of(event.type, event.payload)):
                    await progress(word)

        while True:
            record = await self._store.get_mcp_turn(turn_id)
            if record is None:
                return TurnStanding(handle="", state=TURN_FAILED, answer=_RESTARTED, is_error=True)
            if record.state in TURN_ENDED:
                await flush(self.turns.get(turn_id))
                return _standing_of(record)
            if not await self._still_running(record):
                return _standing_of(await self._fail_stale(record))
            left = deadline - asyncio.get_running_loop().time()
            if left <= 0:
                return _standing_of(record)
            runner = self._running.get(turn_id)
            turn = self.turns.get(turn_id) if runner is not None else None
            if runner is not None and turn is not None:
                # This replica runs it: its journal says each step as it happens.
                waiter = asyncio.ensure_future(turn.journal.wait(cursor, min(left, _POLL_S)))
                await asyncio.wait([runner, waiter], return_when=asyncio.FIRST_COMPLETED)
                if not waiter.done():
                    waiter.cancel()
                await flush(turn)
            elif runner is not None:
                # Ours, but no turn opened yet (the tool has not asked an agent):
                # a short wait, then another look.
                await asyncio.wait({runner}, timeout=min(left, 0.05))
            else:
                if progress is not None and (record.state, record.step) != seen and record.step:
                    seen = (record.state, record.step)
                    await progress(
                        standing_words(_standing_of(record)) if record.state == TURN_AWAITING_APPROVAL else record.step
                    )
                await asyncio.sleep(min(left, _POLL_S))

    async def _still_running(self, record: McpTurnRecord) -> bool:
        """Whether the record's turn is still being run by a live replica."""
        if record.state in TURN_ENDED:
            return False
        if record.replica == self._replica_id:
            return record.turn_id in self._running
        if self._replicas is None:
            return True  # nothing to ask: taken at its word
        return await self._replicas.replica_url(record.replica) is not None

    async def _fail_stale(self, record: McpTurnRecord) -> McpTurnRecord:
        """A turn whose replica is gone will never end on its own: say so."""
        now = self._clock()
        failed = replace(
            record, state=TURN_FAILED, step="", answer=_RESTARTED, is_error=True, updated_at=_iso(now),
            expires_at=self._ended_until(now),
        )
        await self._store.save_mcp_turn(failed)
        return failed

    async def _wait(self, call: GatewayCall, handle: str) -> TurnStanding:
        """``GatewayCall.wait``: the conversation's latest turn, waited for."""
        if not _HANDLE.match(handle):
            raise CallFailed(f"{handle!r} is not a conversation handle of this gateway's")
        latest = await self._store.latest_mcp_turn(conversation_key(call.caller.subject, handle))
        if latest is None:
            raise CallFailed(f"no conversation {handle} of yours is known here")
        return await self._await_turn(latest.turn_id, self._call_wait_s)

    # -- tasks ------------------------------------------------------------------------------

    def _task_wire(self, record: McpTurnRecord, *, created: bool = False) -> dict[str, Any]:
        """The turn as a task — ``CreateTaskResult`` when the call is answered
        with it (``created``), the detailed task on a poll: its status, the
        latest word, and once it has ended its result or its error."""
        status = _TASK_STATUS[record.state]
        task: dict[str, Any] = {
            "resultType": RESULT_TASK if created else RESULT_COMPLETE,
            "taskId": record.turn_id, "status": status, "createdAt": record.started_at,
            "lastUpdatedAt": record.updated_at, "ttlMs": self._task_ttl_ms, "pollIntervalMs": POLL_INTERVAL_MS,
        }
        if record.state == TURN_AWAITING_APPROVAL:
            task["statusMessage"] = standing_words(_standing_of(record))
        elif record.state in (TURN_WORKING, TURN_CANCELLED) and record.step:
            task["statusMessage"] = record.step
        if record.state == TURN_DONE:
            task["result"] = {"resultType": RESULT_COMPLETE, **self._wire((record.answer, record.is_error), record.handle)}
        elif record.state == TURN_FAILED:
            task["error"] = {"code": ERROR_INTERNAL, "message": record.answer}
            task["statusMessage"] = record.answer
        return task

    async def _serve_task(self, caller: McpCaller, method: str, params: Mapping[str, Any], rid: Any) -> web.Response:
        """``tasks/get``, ``tasks/update``, ``tasks/cancel``: the caller's own
        turn — the person's, as the conversation is — by its id."""
        await self._store.purge_mcp_turns(_iso(self._clock()))  # a task past its ttl is gone
        task_id = params.get("taskId")
        record = await self._store.get_mcp_turn(task_id) if isinstance(task_id, str) and task_id else None
        if record is None or record.subject != caller.subject:
            raise _RpcError(ERROR_INVALID_PARAMS, f"unknown task {task_id!r}")
        if method == "tasks/get":
            if not await self._still_running(record) and record.state not in TURN_ENDED:
                record = await self._fail_stale(record)
            return self._result(rid, self._task_wire(record))
        if method == "tasks/update":
            # Nothing of a task here asks the client for input: a response to
            # no request is taken and ignored, as the extension says.
            return self._result(rid, {})
        if record.state not in TURN_ENDED:
            await self._store.request_mcp_cancel(record.turn_id)
            if record.turn_id in self._running:
                await self._cancel(record)
        return self._result(rid, {})

    async def _cancel(self, record: McpTurnRecord) -> None:
        """Stop the turn this replica runs for the record, and the call."""
        turn = self.turns.active(record.conversation)
        entry = self._agents.get(turn.agent) if turn is not None else None
        if entry is not None and isinstance(entry[0], StoppableSink):
            try:
                await entry[0].stop(record.conversation, gateway=GATEWAY_MCP, user_id=record.subject)
            except Exception:
                logger.warning("mcp gateway: could not stop the turn %s", record.turn_id, exc_info=True)
        runner = self._running.get(record.turn_id)
        if runner is not None:
            runner.cancel()

    # -- a turn for a call ------------------------------------------------------------

    def _agent(self, agent: str) -> tuple[str, TrackedSink, McpChatClient]:
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
        agent, sink, chat = self._agent(agent)
        caller = call.caller
        handle = call.handle or secrets.token_urlsafe(16)
        conversation = conversation_key(caller.subject, handle)
        message_id = uuid.uuid4().hex
        try:
            turn, _ = self.turns.start(
                agent=agent, conversation=conversation, owner=f"{GATEWAY_MCP}/{caller.subject}",
                client_message_id=message_id, turn_id=call.turn_id or None,
            )
        except TurnInProgress as exc:
            raise CallFailed("the conversation is still answering an earlier call; ask again once it is done") from exc
        call.handle = handle
        self._askers[conversation] = caller
        record = await self._store.get_mcp_turn(turn.id)
        if record is not None:
            record = replace(record, agent=agent, conversation=conversation, handle=handle, updated_at=_iso(self._clock()))
            await self._store.save_mcp_turn(record)
        message = IncomingMessage(
            ref=ConversationRef(
                channel_id=conversation, conversation_id=conversation,
                message_id=f"{GATEWAY_MCP}{KEY_SEP}{message_id}", thread_root_id="",
            ),
            text=text, user_id=caller.subject, username=caller.username or caller.subject,
            timestamp=self._clock(), kind=KIND_DM, is_dm=True, mentioned=True, gateway=GATEWAY_MCP,
            turn=TurnScope(
                turn_id=turn.id,
                attributes={
                    MCP_CLIENT_ATTRIBUTE: caller.client,
                    MCP_CLIENT_NAME_ATTRIBUTE: caller.client_name,
                    MCP_SCOPES_ATTRIBUTE: mcp_scopes_attribute(caller.scopes),
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
            outcome = await self._follow(turn, pending, record)
        except BaseException:
            # The call is gone (cancelled, the hub stopping); the turn still
            # runs and is closed when it ends, so the conversation is not busy
            # for ever.
            pending.add_done_callback(lambda done: self._close(turn, done))
            raise
        finally:
            self._askers.pop(conversation, None)
        self.turns.finish(turn, outcome)
        answer = self._answer(turn)
        if outcome in _TURN_FAILED:
            raise CallFailed(answer or f"the agent's turn ended: {outcome.name.lower()}")
        if outcome is TurnOutcome.STOPPED:
            raise CallFailed(answer or "the turn was stopped")
        return self._present(answer)

    async def _follow(
        self, turn: Turn, pending: "asyncio.Future[TurnOutcome]", record: McpTurnRecord | None,
    ) -> TurnOutcome:
        """Wait for the turn, writing where it stands as it changes — the
        step, a card up for the person — and stopping it when a cancel reached
        another replica. Every write starts from the record as the store has
        it, so what another replica wrote is read, never wiped."""
        written = ("", "")
        cursor = 0
        while not pending.done():
            waiter = asyncio.ensure_future(turn.journal.wait(cursor, _POLL_S))
            await asyncio.wait({pending, waiter}, return_when=asyncio.FIRST_COMPLETED)
            if not waiter.done():
                waiter.cancel()
            cursor = turn.journal.cursor
            if record is None:
                continue
            fresh = await self._store.get_mcp_turn(record.turn_id)
            if fresh is None:
                continue
            awaiting = turn.journal.status == STATUS_AWAITING_INPUT
            state = TURN_AWAITING_APPROVAL if awaiting else TURN_WORKING
            waiting = turn.calls.awaiting
            step = (waiting.tool if waiting is not None else "") if awaiting else turn.calls.latest
            if (state, step) != written:
                written = (state, step)
                now = self._clock()
                fresh = replace(fresh, state=state, step=step, updated_at=_iso(now), expires_at=self._running_until(now))
                await self._store.save_mcp_turn(fresh)
            if fresh.cancel_requested and not pending.done():
                await self._cancel(fresh)
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
        # Every result says what kind it is; a plain one is complete.
        return web.json_response({"jsonrpc": "2.0", "id": rid, "result": {"resultType": RESULT_COMPLETE, **result}})

    @staticmethod
    def _error(rid: Any, refused: _RpcError) -> web.Response:
        error: dict[str, Any] = {"code": refused.code, "message": str(refused)}
        if refused.data is not None:
            error["data"] = refused.data
        return web.json_response(
            {"jsonrpc": "2.0", "id": rid, "error": error}, status=refused.status, headers=refused.headers
        )


async def _no_progress(text: str) -> None:
    return


async def _event(response: web.StreamResponse, message: Mapping[str, Any]) -> None:
    await response.write(f"event: message\ndata: {json.dumps(message, ensure_ascii=False)}\n\n".encode("utf-8"))
