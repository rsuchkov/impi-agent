"""A tiny MCP server over streamable HTTP for the tests (aiohttp), speaking the
era the test asks for: ``modern`` (per-request ``_meta``, revision 2026-07-28),
``legacy`` (an ``initialize`` handshake and a session, 2025-11-25) or ``dual``.
The tools it serves and the calls it saw are the test's to set and read."""

import json
from typing import Any

from aiohttp import web

ECHO = {
    "name": "echo",
    "title": "Echo",
    "description": "Echoes the text back.",
    "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
}
WIPE = {
    "name": "wipe",
    "description": "Wipes everything.",
    "inputSchema": {"type": "object", "properties": {}},
    "annotations": {"destructiveHint": True},
}
ASK = {"name": "ask", "description": "Needs more input.", "inputSchema": {"type": "object", "properties": {}}}
META_VERSION = "io.modelcontextprotocol/protocolVersion"
SERVER_INFO = {"name": "fake-http", "version": "1"}


class FakeMcpHttp:
    def __init__(
        self, port: int, *, tools: list[dict] | None = None, token: str = "", sse: bool = False,
        era: str = "modern", supported: tuple[str, ...] = ("2026-07-28",), reject: int = 0,
    ) -> None:
        self.port = port
        self.tools = tools if tools is not None else [ECHO, WIPE]
        self.token = token  # when set, the Authorization header must carry it
        self.sse = sse  # answer as a one-message event stream instead of JSON
        self.era = era
        self.supported = supported  # what a modern server says it speaks
        self.reject = reject  # a modern error code every modern request is refused with
        self.calls: list[tuple[str, dict, dict[str, str]]] = []  # (tool, arguments, headers)
        self.requests: list[dict] = []  # every JSON-RPC message received
        self.sessions = 0  # legacy initializes
        self._runner: web.AppRunner | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post("/mcp", self._post)
        app.router.add_delete("/mcp", self._delete)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        await web.TCPSite(self._runner, "127.0.0.1", self.port).start()

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    async def _delete(self, request: web.Request) -> web.Response:
        return web.Response(status=204)

    def _rpc_error(self, rid: Any, code: int, message: str, status: int = 400, **data: Any) -> web.Response:
        error: dict[str, Any] = {"code": code, "message": message}
        if data:
            error["data"] = data
        return web.json_response({"jsonrpc": "2.0", "id": rid, "error": error}, status=status)

    async def _post(self, request: web.Request) -> web.Response:
        if self.token and request.headers.get("Authorization") != f"Bearer {self.token}":
            return web.Response(status=401)
        message = await request.json()
        self.requests.append(message)
        rid, method = message.get("id"), message.get("method")
        params = message.get("params") or {}
        meta = params.get("_meta") or {}
        modern_request = META_VERSION in meta
        if rid is None:
            return web.Response(status=202)
        headers: dict[str, str] = {}
        if modern_request:
            if self.era == "legacy":
                # A legacy server knows nothing of _meta: no session, a bare 400.
                return web.Response(status=400, text="Bad Request: no session")
            if request.headers.get("MCP-Protocol-Version") != meta[META_VERSION]:
                return self._rpc_error(rid, -32020, "Header mismatch")
            if request.headers.get("Mcp-Method") != method:
                return self._rpc_error(rid, -32020, "Header mismatch: Mcp-Method")
            if method == "tools/call" and request.headers.get("Mcp-Name") != params.get("name"):
                return self._rpc_error(rid, -32020, "Header mismatch: Mcp-Name")
            if self.reject:
                return self._rpc_error(rid, self.reject, "Missing required client capability", requiredCapabilities=["sampling"])
            if meta[META_VERSION] not in self.supported:
                return self._rpc_error(
                    rid, -32022, "Unsupported protocol version", supported=list(self.supported),
                    requested=meta[META_VERSION],
                )
        elif self.era == "modern" and "2025-11-25" not in self.supported and method != "initialize":
            # Modern-only: a request without _meta is malformed. A dual-era server
            # (one that also lists the handshake revision) serves it the legacy way.
            return self._rpc_error(rid, -32602, "Invalid params: missing _meta")
        result: Any
        if method == "initialize":
            if self.era == "modern" and "2025-11-25" not in self.supported:
                # A modern-only server; one that also lists the handshake era is dual-era.
                return self._rpc_error(rid, -32601, "Method not found (supported: 2026-07-28)", status=404)
            self.sessions += 1
            headers["Mcp-Session-Id"] = f"s{self.sessions}"
            result = {"protocolVersion": params.get("protocolVersion"), "capabilities": {"tools": {}},
                      "serverInfo": SERVER_INFO}
        elif method == "server/discover":
            result = {"supportedVersions": list(self.supported), "capabilities": {"tools": {}}}
        elif method == "tools/list":
            result = {"tools": self.tools}
        elif method == "tools/call":
            self.calls.append((params["name"], dict(params.get("arguments") or {}), dict(request.headers)))
            if params["name"] == "echo":
                result = {"content": [{"type": "text", "text": f"echo: {params['arguments'].get('text', '')}"}]}
            elif params["name"] == "wipe":
                result = {"content": [{"type": "text", "text": "wiped"}], "structuredContent": {"wiped": True}}
            elif params["name"] == "ask":
                result = {"resultType": "input_required", "inputRequests": {"q": {"method": "elicitation/create"}}}
            else:
                result = {"content": [{"type": "text", "text": "no such tool"}], "isError": True}
        else:
            return self._rpc_error(rid, -32601, "Method not found", status=404)
        if modern_request:
            result = {"resultType": "complete", **result, "_meta": {"io.modelcontextprotocol/serverInfo": SERVER_INFO}}
        answer = {"jsonrpc": "2.0", "id": rid, "result": result}
        if self.sse:
            return web.Response(
                text=f"event: message\ndata: {json.dumps(answer)}\n\n", content_type="text/event-stream", headers=headers
            )
        return web.json_response(answer, headers=headers)
