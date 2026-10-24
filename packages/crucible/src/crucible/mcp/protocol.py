"""The least of MCP the engine needs: a JSON-RPC client over streamable HTTP or
stdio that lists a server's tools and calls one.

Two eras of the protocol are spoken. The current one (revision 2026-07-28) is
stateless: every request carries its protocol version, the client's identity
and its capabilities in ``_meta``, and on HTTP the same in headers, so any
request may reach any instance of a server. The earlier ones (2025-11-25 and
before) open a session with an ``initialize`` handshake. The client speaks the
current era first and finds out which one the server is: a modern server
answers, or refuses with a modern error (an unsupported version names the ones
it supports and the client retries with one of them); anything else — a 400
with no modern error body on HTTP, a failed ``server/discover`` probe on stdio —
is a legacy server, and the client falls back to the handshake for it. The
era, once found, is kept for the server.

Nothing else the protocol lets a server do reaches the engine: a request the
server sends back (sampling, elicitation, roots) is answered "method not
found" on stdio, a result that asks for more input is an error, resources and
prompts are never asked for. A server is a provider of tools whose definitions
the engine pins and whose results are strangers' words (``mcp.tools``).
Answers are capped in size and time, and an HTTP answer is a JSON body or a
single-message event stream — never a stream the client keeps.
"""

import asyncio
import base64
import json
import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

logger = logging.getLogger(__name__)

# The revisions this client speaks, preferred first. The first is the modern
# era (per-request metadata); the rest open a session with ``initialize``.
PROTOCOL_VERSIONS = ("2026-07-28", "2025-11-25")
MODERN_VERSION = PROTOCOL_VERSIONS[0]
LEGACY_VERSION = PROTOCOL_VERSIONS[1]
CLIENT_INFO = {"name": "crucible", "version": "1"}
# What this client declares it can do for a server: nothing. A server that needs
# a capability answers -32021 and the call fails, which is the point.
CLIENT_CAPABILITIES: dict[str, Any] = {}
META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"
# Modern JSON-RPC error codes, the ones that say "a modern server answered".
ERROR_HEADER_MISMATCH = -32020
ERROR_MISSING_CAPABILITY = -32021
ERROR_UNSUPPORTED_VERSION = -32022
MODERN_ERROR_CODES = frozenset({ERROR_HEADER_MISMATCH, ERROR_MISSING_CAPABILITY, ERROR_UNSUPPORTED_VERSION})
RESULT_COMPLETE = "complete"
RESULT_INPUT_REQUIRED = "input_required"
# An answer past this is a stranger's text nobody asked for in that quantity.
MAX_ANSWER_BYTES = 2 * 1024 * 1024
CALL_TIMEOUT_S = 60.0
CONNECT_TIMEOUT_S = 10.0
# The stdio probe for the server's era waits this long for a legacy server that
# answers nothing to an unknown method.
PROBE_TIMEOUT_S = 5.0
SESSION_HEADER = "Mcp-Session-Id"  # legacy era only
PROTOCOL_VERSION_HEADER = "MCP-Protocol-Version"
METHOD_HEADER = "Mcp-Method"
NAME_HEADER = "Mcp-Name"

# What went wrong, as a code the model can be told and a log can group by.
UNREACHABLE = "unreachable"  # no connection, or none that answered in time
REFUSED = "refused"  # 401/403, or a JSON-RPC error from the server
PROTOCOL = "protocol"  # not JSON-RPC, not JSON, a stream, too large, a version nobody shares
UNSUPPORTED = "unsupported"  # the server asked for something this client does not do


class McpError(Exception):
    def __init__(
        self, code: str, message: str, *, rpc_code: int | None = None, data: Any = None,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.rpc_code = rpc_code  # the JSON-RPC error code, when the server answered one
        self.data = data
        self.status = status  # the HTTP status, when that is all the server answered

    @property
    def legacy_sign(self) -> bool:
        """Whether this is how a legacy HTTP server meets a modern request: a
        client error status with no JSON-RPC error in its body."""
        return self.status in (400, 404, 405) and self.rpc_code is None


@dataclass(frozen=True)
class ToolDefinition:
    """One tool as a server declares it (``tools/list``)."""

    name: str
    description: str
    input_schema: Mapping[str, Any]
    title: str = ""
    annotations: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_wire(cls, data: Mapping[str, Any]) -> "ToolDefinition":
        return cls(
            name=str(data.get("name", "")),
            description=str(data.get("description", "")),
            input_schema=dict(data.get("inputSchema") or {"type": "object", "properties": {}}),
            title=str(data.get("title", "")),
            annotations=dict(data.get("annotations") or {}),
        )

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {
            "name": self.name, "description": self.description, "inputSchema": dict(self.input_schema),
        }
        if self.title:
            wire["title"] = self.title
        if self.annotations:
            wire["annotations"] = dict(self.annotations)
        return wire


@dataclass(frozen=True)
class CallResult:
    """What ``tools/call`` answered: the text parts joined, the structured
    content when the server gave one, and whether the server calls it an error."""

    text: str
    structured: Any = None
    is_error: bool = False

    @classmethod
    def from_wire(cls, data: Mapping[str, Any]) -> "CallResult":
        parts = [
            str(item.get("text", ""))
            for item in data.get("content") or ()
            if isinstance(item, Mapping) and item.get("type") == "text"
        ]
        return cls(
            text="\n".join(parts), structured=data.get("structuredContent"),
            is_error=bool(data.get("isError", False)),
        )


def request_meta(version: str) -> dict[str, Any]:
    """The per-request metadata of the modern era."""
    return {
        META_PROTOCOL_VERSION: version,
        META_CLIENT_INFO: dict(CLIENT_INFO),
        META_CLIENT_CAPABILITIES: dict(CLIENT_CAPABILITIES),
    }


class Transport(Protocol):
    """One JSON-RPC connection: a request awaits its answer; a notification does
    not. ``version`` is the protocol version the request speaks, for whatever
    the binding mirrors into its envelope; ``close`` ends the connection, and
    the next request opens a new one."""

    async def request(self, method: str, params: Mapping[str, Any], *, version: str) -> Any: ...
    async def notify(self, method: str, params: Mapping[str, Any], *, version: str) -> None: ...
    async def close(self) -> None: ...


def _answer_of(message: Mapping[str, Any]) -> Any:
    if "error" in message:
        error = message["error"] or {}
        code = error.get("code")
        raise McpError(
            REFUSED, f"{error.get('message', 'error')} ({code if code is not None else '?'})",
            rpc_code=code if isinstance(code, int) else None, data=error.get("data"),
        )
    return message.get("result")


def _header_value(value: str) -> str:
    """A header value as the transport may carry it: plain when it is safe
    ASCII, otherwise the Base64 sentinel form the revision defines."""
    plain = value.isascii() and value.isprintable() and value == value.strip() and not (
        value.startswith("=?base64?") and value.endswith("?=")
    )
    if plain:
        return value
    return f"=?base64?{base64.b64encode(value.encode('utf-8')).decode('ascii')}?="


# -- streamable HTTP -------------------------------------------------------------


class HttpTransport:
    def __init__(
        self,
        url: str,
        headers: Mapping[str, str] | None = None,
        *,
        http: httpx.AsyncClient | None = None,
        timeout_s: float = CALL_TIMEOUT_S,
    ) -> None:
        self._url = url
        self._headers = dict(headers or {})
        self._http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s, connect=CONNECT_TIMEOUT_S), follow_redirects=False
        )
        self._next_id = 0
        self.session_id = ""  # a legacy server's, once it gave one

    async def request(self, method: str, params: Mapping[str, Any], *, version: str) -> Any:
        self._next_id += 1
        message = await self._post(
            {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": dict(params)},
            expect=self._next_id, version=version,
        )
        if message is None:
            raise McpError(PROTOCOL, f"{method}: the server answered nothing")
        return _answer_of(message)

    async def notify(self, method: str, params: Mapping[str, Any], *, version: str) -> None:
        await self._post({"jsonrpc": "2.0", "method": method, "params": dict(params)}, expect=None, version=version)

    async def _post(
        self, body: dict[str, Any], *, expect: int | None, version: str
    ) -> Mapping[str, Any] | None:
        headers = {
            **self._headers,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            PROTOCOL_VERSION_HEADER: version,
            METHOD_HEADER: body["method"],
        }
        params = body.get("params") or {}
        if (name := params.get("name") or params.get("uri")) and isinstance(name, str):
            headers[NAME_HEADER] = _header_value(name)
        if self.session_id:
            headers[SESSION_HEADER] = self.session_id
        try:
            response = await self._http.post(self._url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise McpError(UNREACHABLE, f"{self._url}: {exc.__class__.__name__}") from exc
        if response.status_code in (401, 403):
            raise McpError(REFUSED, f"{self._url}: {response.status_code}")
        if response.status_code >= 400:
            # A modern server puts a JSON-RPC error in the body of its 400/404
            # (an unsupported version, a header mismatch, an unknown method);
            # that error is the answer. A bare status is the status.
            error = _rpc_error_in(response)
            if error is not None:
                _answer_of(error)
            raise McpError(PROTOCOL, f"{self._url}: {response.status_code}", status=response.status_code)
        if session := response.headers.get(SESSION_HEADER):
            self.session_id = session
        if expect is None or response.status_code == 202 or not response.content:
            return None
        if len(response.content) > MAX_ANSWER_BYTES:
            raise McpError(PROTOCOL, f"{self._url}: answer over {MAX_ANSWER_BYTES} bytes")
        content_type = response.headers.get("Content-Type", "").split(";")[0].strip()
        if content_type == "text/event-stream":
            return _message_from_events(response.text, expect)
        try:
            data = response.json()
        except ValueError as exc:
            raise McpError(PROTOCOL, f"{self._url}: not JSON") from exc
        if not isinstance(data, Mapping):
            raise McpError(PROTOCOL, f"{self._url}: not a JSON-RPC message")
        return data

    async def close(self) -> None:
        if self.session_id:
            try:
                await self._http.delete(self._url, headers={SESSION_HEADER: self.session_id})
            except httpx.HTTPError:
                pass
            self.session_id = ""


def _rpc_error_in(response: httpx.Response) -> Mapping[str, Any] | None:
    """The JSON-RPC error message in an HTTP error's body, when there is one."""
    if len(response.content) > MAX_ANSWER_BYTES:
        return None
    try:
        data = response.json()
    except ValueError:
        return None
    if isinstance(data, Mapping) and isinstance(data.get("error"), Mapping):
        return data
    return None


def _message_from_events(text: str, expect: int) -> Mapping[str, Any]:
    """The answer to ``expect`` out of a server-sent-events body that has ended:
    the one shape of stream this client accepts."""
    for chunk in text.replace("\r\n", "\n").split("\n\n"):
        data_lines = [line[5:].lstrip() for line in chunk.split("\n") if line.startswith("data:")]
        if not data_lines:
            continue
        try:
            message = json.loads("\n".join(data_lines))
        except ValueError:
            continue
        if isinstance(message, Mapping) and message.get("id") == expect:
            return message
    raise McpError(PROTOCOL, "the event stream carried no answer")


# -- stdio ---------------------------------------------------------------------------


class StdioTransport:
    """A server as a child process of the engine, one line of JSON-RPC each way.
    The child gets PATH and the variables the server's entry names — not the
    engine's environment. A child that died is started again on the next request."""

    def __init__(
        self,
        command: str,
        args: Sequence[str] = (),
        env: Mapping[str, str] | None = None,
        cwd: str = "",
        *,
        timeout_s: float = CALL_TIMEOUT_S,
    ) -> None:
        self._command = command
        self._args = list(args)
        self._env = {"PATH": os.environ.get("PATH", ""), **dict(env or {})}
        self._cwd = cwd or None
        self._timeout_s = timeout_s
        self._process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._waiting: dict[int, asyncio.Future[Mapping[str, Any]]] = {}
        self._next_id = 0

    async def _ensure(self) -> asyncio.subprocess.Process:
        if self._process is not None and self._process.returncode is None:
            return self._process
        try:
            self._process = await asyncio.create_subprocess_exec(
                self._command, *self._args, env=self._env, cwd=self._cwd,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            raise McpError(UNREACHABLE, f"{self._command}: {exc}") from exc
        self._reader = asyncio.ensure_future(self._read(self._process))
        return self._process

    async def _read(self, process: asyncio.subprocess.Process) -> None:
        assert process.stdout is not None
        try:
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                if len(line) > MAX_ANSWER_BYTES:
                    logger.warning("mcp %s: a line over %d bytes dropped", self._command, MAX_ANSWER_BYTES)
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(message, Mapping):
                    continue
                if "method" in message and "id" in message:
                    # A request of a legacy server's (sampling, elicitation): not here.
                    await self._write(
                        process,
                        {"jsonrpc": "2.0", "id": message["id"],
                         "error": {"code": -32601, "message": "method not found"}},
                    )
                    continue
                rid = message.get("id")
                waiting = self._waiting.pop(rid, None) if isinstance(rid, int) else None
                if waiting is not None and not waiting.done():
                    waiting.set_result(message)
        finally:
            for future in self._waiting.values():
                if not future.done():
                    future.set_exception(McpError(UNREACHABLE, f"{self._command}: the server exited"))
            self._waiting.clear()

    @staticmethod
    async def _write(process: asyncio.subprocess.Process, message: Mapping[str, Any]) -> None:
        assert process.stdin is not None
        try:
            process.stdin.write(json.dumps(message, ensure_ascii=False).encode() + b"\n")
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise McpError(UNREACHABLE, "the server closed its input") from exc

    async def request(
        self, method: str, params: Mapping[str, Any], *, version: str, timeout_s: float | None = None
    ) -> Any:
        process = await self._ensure()
        self._next_id += 1
        future: asyncio.Future[Mapping[str, Any]] = asyncio.get_running_loop().create_future()
        self._waiting[self._next_id] = future
        await self._write(
            process, {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": dict(params)}
        )
        wait = self._timeout_s if timeout_s is None else timeout_s
        try:
            message = await asyncio.wait_for(future, timeout=wait)
        except TimeoutError as exc:
            self._waiting.pop(self._next_id, None)
            raise McpError(UNREACHABLE, f"{method}: no answer in {wait:.0f}s") from exc
        return _answer_of(message)

    async def notify(self, method: str, params: Mapping[str, Any], *, version: str) -> None:
        process = await self._ensure()
        await self._write(process, {"jsonrpc": "2.0", "method": method, "params": dict(params)})

    async def close(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                process.kill()
        if self._reader is not None:
            self._reader.cancel()
            self._reader = None


# -- the client ----------------------------------------------------------------------


class McpClient:
    """One server, over one transport. Speaks the modern era until the server
    shows it is a legacy one; then opens a session and speaks that."""

    def __init__(self, transport: Transport) -> None:
        self._transport = transport
        self.version = MODERN_VERSION  # what the server is spoken to in
        self.legacy = False  # found to need the initialize handshake
        self._initialized = False  # the legacy session is open
        self.server_info: Mapping[str, Any] = {}

    async def list_tools(self) -> tuple[ToolDefinition, ...]:
        tools: list[ToolDefinition] = []
        cursor: str | None = None
        for _ in range(100):  # pages; a server that never ends is not followed forever
            params: dict[str, Any] = {"cursor": cursor} if cursor else {}
            result = await self._call("tools/list", params)
            tools += [ToolDefinition.from_wire(t) for t in result.get("tools") or () if isinstance(t, Mapping)]
            cursor = result.get("nextCursor") or None
            if not cursor:
                break
        return tuple(tools)

    async def call_tool(self, name: str, arguments: Mapping[str, Any]) -> CallResult:
        result = await self._call("tools/call", {"name": name, "arguments": dict(arguments)})
        if result.get("resultType", RESULT_COMPLETE) != RESULT_COMPLETE:
            # A server asking the person or the model for more (elicitation,
            # sampling) is asking what this client does not do.
            raise McpError(UNSUPPORTED, f"{name}: the server asked for more input ({result.get('resultType')})")
        return CallResult.from_wire(result)

    async def close(self) -> None:
        self._initialized = False
        await self._transport.close()

    # -- eras -------------------------------------------------------------------

    async def _call(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        if self.legacy:
            return await self._legacy_call(method, params)
        if isinstance(self._transport, StdioTransport) and not self.server_info:
            # No status code on stdio to tell the eras apart: probe first.
            await self._probe_stdio()
            if self.legacy:
                return await self._legacy_call(method, params)
        try:
            result = await self._transport.request(
                method, {**params, "_meta": request_meta(self.version)}, version=self.version
            )
        except McpError as exc:
            if exc.rpc_code == ERROR_UNSUPPORTED_VERSION:
                self.version = self._agree(exc)
                if self.version == LEGACY_VERSION:
                    self.legacy = True
                    return await self._legacy_call(method, params)
                result = await self._transport.request(
                    method, {**params, "_meta": request_meta(self.version)}, version=self.version
                )
            elif exc.legacy_sign:
                # An HTTP 4xx with no modern error in it: a legacy server.
                logger.info("mcp: the server answered a modern request with %s; speaking %s", exc, LEGACY_VERSION)
                self.legacy = True
                return await self._legacy_call(method, params)
            else:
                raise
        return self._complete(result, method)

    def _agree(self, exc: McpError) -> str:
        supported = (exc.data or {}).get("supported") if isinstance(exc.data, Mapping) else None
        offered = [v for v in (supported or ()) if isinstance(v, str)]
        for version in PROTOCOL_VERSIONS:
            if version in offered:
                return version
        raise McpError(
            PROTOCOL, f"the server supports {', '.join(offered) or 'no known version'}; "
            f"this client speaks {', '.join(PROTOCOL_VERSIONS)}",
        )

    async def _probe_stdio(self) -> None:
        """``server/discover`` tells a modern server from a legacy one: a result
        or a modern error is modern; any other error, or silence, is legacy."""
        assert isinstance(self._transport, StdioTransport)
        try:
            result = await self._transport.request(
                "server/discover", {"_meta": request_meta(self.version)},
                version=self.version, timeout_s=PROBE_TIMEOUT_S,
            )
        except McpError as exc:
            if exc.rpc_code == ERROR_UNSUPPORTED_VERSION:
                self.version = self._agree(exc)
                self.legacy = self.version == LEGACY_VERSION
                return
            if exc.rpc_code in MODERN_ERROR_CODES:
                raise
            logger.info("mcp: the server did not answer server/discover (%s); speaking %s", exc, LEGACY_VERSION)
            self.legacy = True
            return
        if not isinstance(result, Mapping):
            raise McpError(PROTOCOL, "server/discover answered no object")
        self._complete(result, "server/discover")
        offered = [v for v in result.get("supportedVersions") or () if isinstance(v, str)]
        if offered and self.version not in offered:
            for version in PROTOCOL_VERSIONS:
                if version in offered:
                    self.version = version
                    break
            else:
                raise McpError(PROTOCOL, f"the server supports {', '.join(offered)}; this client speaks {', '.join(PROTOCOL_VERSIONS)}")
            self.legacy = self.version == LEGACY_VERSION

    def _complete(self, result: Any, method: str) -> Mapping[str, Any]:
        if not isinstance(result, Mapping):
            raise McpError(PROTOCOL, f"{method} answered no object")
        meta = result.get("_meta")
        if isinstance(meta, Mapping) and isinstance(meta.get(META_SERVER_INFO), Mapping):
            self.server_info = dict(meta[META_SERVER_INFO])
        elif not self.server_info:
            self.server_info = {"name": "?"}  # answered, so known; nameless
        return result

    async def _legacy_call(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        if not self._initialized:
            await self._initialize()
        result = await self._transport.request(method, params, version=self.version)
        if not isinstance(result, Mapping):
            raise McpError(PROTOCOL, f"{method} answered no object")
        return result

    async def _initialize(self) -> None:
        self.version = LEGACY_VERSION
        result = await self._transport.request(
            "initialize",
            {"protocolVersion": LEGACY_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO},
            version=LEGACY_VERSION,
        )
        if not isinstance(result, Mapping):
            raise McpError(PROTOCOL, "initialize answered no object")
        self.server_info = dict(result.get("serverInfo") or {})
        await self._transport.notify("notifications/initialized", {}, version=LEGACY_VERSION)
        self._initialized = True
