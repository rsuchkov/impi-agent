"""McpServers: the declared servers, connected, checked against their snapshots
and called.

Built before the tool registry, synchronously, from the snapshots alone: that
is what a tool is at startup, whatever the server says now. ``start`` then
connects every server and compares. An untrusted server whose tools differ
from the snapshot has them withdrawn — the model will not read a description
nobody reviewed — and the operator is told to review and refresh; a trusted
server's snapshot is rewritten and the tools follow it. A server that cannot
be reached keeps its tools advertised and answers each call with the error,
the way any service down does.
"""

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, cast

from crucible.mcp.library import McpLibrary, McpServer, expand
from crucible.mcp.protocol import (
    CallResult,
    HttpTransport,
    McpClient,
    McpError,
    StdioTransport,
    ToolDefinition,
    Transport,
)
from crucible.mcp.snapshot import Snapshot
from crucible.mcp.tools import McpTool
from crucible.tools.base import Tool

logger = logging.getLogger(__name__)

TransportFactory = Callable[[McpServer], Transport]


def default_transport(server: McpServer) -> Transport:
    if server.is_http:
        return HttpTransport(server.url, server.headers)
    return StdioTransport(server.command, server.args, server.env, server.cwd)


@dataclass
class StartReport:
    """What ``start`` found, for the startup log."""

    connected: list[str] = field(default_factory=list)
    unreachable: dict[str, str] = field(default_factory=dict)  # server -> why
    refreshed: list[str] = field(default_factory=list)  # trusted servers whose snapshot moved
    withdrawn: dict[str, tuple[str, ...]] = field(default_factory=dict)  # untrusted server -> tool names
    unsnapshotted: list[str] = field(default_factory=list)  # servers with tools nobody pinned yet

    @property
    def withdrawn_tools(self) -> tuple[str, ...]:
        return tuple(name for names in self.withdrawn.values() for name in names)


class McpServers:
    def __init__(
        self,
        library: McpLibrary,
        values: Mapping[str, str],
        *,
        transport: TransportFactory = default_transport,
    ) -> None:
        self._library = library
        self._values = values  # what ${NAME} in a server entry is filled from
        self._transport = transport
        self._servers: dict[str, McpServer] = {}
        self._clients: dict[str, McpClient] = {}
        self._tools: dict[str, list[McpTool]] = {}
        for server in library.list():
            self._servers[server.name] = server
            snapshot = library.snapshot(server.name)
            self._tools[server.name] = (
                [McpTool(server, definition, self._caller(server.name)) for definition in snapshot.tools]
                if snapshot is not None
                else []
            )

    @property
    def servers(self) -> tuple[McpServer, ...]:
        return tuple(self._servers.values())

    def tools(self) -> tuple[McpTool, ...]:
        """Every pinned tool of every server, for the registry."""
        return tuple(tool for tools in self._tools.values() for tool in tools)

    def registry_tools(self) -> tuple[Tool, ...]:
        """The same, as the registry's port sees them. ``McpTool`` carries a
        definition per instance where the port declares class attributes; the
        registry reads them from the instance either way, and this is the one
        seam where that is said."""
        return tuple(cast(Tool, tool) for tool in self.tools())

    def _caller(self, server: str):
        async def call(tool: str, arguments: Mapping[str, Any], headers: Mapping[str, str]) -> CallResult:
            return await self.call(server, tool, arguments, headers)

        return call

    async def client(self, name: str) -> McpClient:
        client = self._clients.get(name)
        if client is None:
            server = expand(self._servers[name], self._values)
            client = self._clients[name] = McpClient(self._transport(server))
        return client

    async def call(
        self, server: str, tool: str, arguments: Mapping[str, Any], headers: Mapping[str, str]
    ) -> CallResult:
        """Call once; a connection that failed is dropped and the call tried
        once more on a fresh one — a server restarted between calls is the
        ordinary case, not an error worth the model's attention."""
        if headers:
            # A per-call credential: this call's connection carries it, alone.
            spec = expand(self._servers[server], self._values)
            client = McpClient(self._transport(_with_headers(spec, headers)))
            try:
                return await client.call_tool(tool, arguments)
            finally:
                await client.close()
        for attempt in (1, 2):
            client = await self.client(server)
            try:
                return await client.call_tool(tool, arguments)
            except McpError as exc:
                if exc.code != "unreachable" or attempt == 2:
                    raise
                logger.warning("mcp %s: %s; reconnecting", server, exc)
                await self._drop(server)
        raise AssertionError("unreachable")

    async def _drop(self, server: str) -> None:
        client = self._clients.pop(server, None)
        if client is not None:
            try:
                await client.close()
            except Exception:
                logger.debug("mcp %s: close failed", server, exc_info=True)

    async def start(self) -> StartReport:
        """Connect every server and check its tools against the snapshot."""
        report = StartReport()
        for name, server in self._servers.items():
            try:
                client = await self.client(name)
                definitions = await client.list_tools()
            except (McpError, Exception) as exc:
                report.unreachable[name] = str(exc)
                logger.warning("mcp %s: not reached at startup: %s", name, exc)
                await self._drop(name)
                continue
            report.connected.append(name)
            snapshot = self._library.snapshot(name)
            if snapshot is None:
                if server.trusted:
                    self._pin(name, definitions, client.server_info)
                    report.refreshed.append(name)
                else:
                    report.unsnapshotted.append(name)
                    logger.error(
                        "mcp %s: no snapshot of its tools — nothing is advertised until "
                        "they are reviewed and pinned", name,
                    )
                continue
            if not snapshot.differs_from(definitions):
                continue
            if server.trusted:
                self._pin(name, definitions, client.server_info)
                report.refreshed.append(name)
                logger.info("mcp %s: tools changed; the snapshot follows (trusted)", name)
            else:
                withdrawn = tuple(tool.name for tool in self._tools.get(name, ()))
                report.withdrawn[name] = withdrawn
                logger.error(
                    "mcp %s: its tools differ from the reviewed snapshot; %d tool(s) withdrawn "
                    "until they are reviewed and pinned again", name, len(withdrawn),
                )
        return report

    def _pin(self, name: str, definitions: tuple[ToolDefinition, ...], server_info: Mapping[str, Any]) -> None:
        """A trusted server's word, taken: the snapshot and the live tools follow
        it. A tool the server dropped keeps its definition until a restart —
        the registry is what it was at startup; the call will say so."""
        snapshot = Snapshot.taken(name, definitions, _now(), server_info)
        self._library.save_snapshot(snapshot)
        by_name = {d.name: d for d in definitions}
        for tool in self._tools.get(name, ()):
            if tool.definition.name in by_name:
                tool.definition = by_name[tool.definition.name]

    async def stop(self) -> None:
        for name in list(self._clients):
            await self._drop(name)


def _with_headers(server: McpServer, headers: Mapping[str, str]) -> McpServer:
    return replace(server, headers={**server.headers, **headers})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
