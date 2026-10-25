"""McpServers: the declared servers, connected, checked against their snapshots
and called.

Built before the tool registry, synchronously, from the snapshots alone: that
is what a tool is at startup, whatever the server says now. ``start`` then
connects every server and compares; ``recheck`` compares again — on a timer,
when a server says its tools changed, and when the operator reloads after a
review. An untrusted server whose tools differ from the snapshot has them
withdrawn — the model will not read a description nobody reviewed — and the
operator is told to review and refresh; a trusted server's snapshot is
rewritten and the tools follow it. A server that cannot be reached keeps its
tools advertised and answers each call with the error, the way any service
down does.
"""

import asyncio
import logging
import time
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, cast

from crucible.mcp.library import (
    CONNECTION_REF,
    McpConfigError,
    McpLibrary,
    McpServer,
    with_values,
)
from crucible.mcp.protocol import (
    UNREACHABLE,
    CallResult,
    HttpTransport,
    McpClient,
    McpError,
    StdioTransport,
    ToolDefinition,
    Transport,
)
from crucible.mcp.snapshot import Snapshot, digest
from crucible.mcp.tools import McpTool
from crucible.tools.base import Tool

logger = logging.getLogger(__name__)

# A server saying its tools changed, unasked (a legacy server on stdio does).
TOOLS_CHANGED = "notifications/tools/list_changed"

TransportFactory = Callable[[McpServer], Transport]


def default_transport(server: McpServer) -> Transport:
    if server.is_http:
        return HttpTransport(server.url, server.headers, timeout_s=server.timeout_s)
    return StdioTransport(server.command, server.args, server.env, server.cwd, timeout_s=server.timeout_s)


@dataclass
class CheckReport:
    """What a check of the servers found — at start or on a recheck — for the
    log and for the tool wiring: which tools to withdraw, which a review
    handed back, whose definitions moved."""

    connected: list[str] = field(default_factory=list)
    unreachable: dict[str, str] = field(default_factory=dict)  # server -> why
    refreshed: list[str] = field(default_factory=list)  # servers whose definitions moved (a trusted pin, an adopted snapshot)
    withdrawn: dict[str, tuple[str, ...]] = field(default_factory=dict)  # untrusted server -> tool names
    restored: dict[str, tuple[str, ...]] = field(default_factory=dict)  # server -> tool names handed back
    unpinned: list[str] = field(default_factory=list)  # servers whose tools nobody pinned yet
    skipped: list[str] = field(default_factory=list)  # servers not asked: fresh by their own ttl

    @property
    def withdrawn_tools(self) -> tuple[str, ...]:
        return tuple(name for names in self.withdrawn.values() for name in names)

    @property
    def restored_tools(self) -> tuple[str, ...]:
        return tuple(name for names in self.restored.values() for name in names)


class McpServers:
    def __init__(
        self,
        library: McpLibrary,
        values: Mapping[str, str],
        *,
        transport_factory: TransportFactory = default_transport,
    ) -> None:
        self._library = library
        self._values = values  # what ${NAME} in a server entry is filled from
        self._transport_factory = transport_factory
        self._servers: dict[str, McpServer] = {}
        self._clients: dict[str, McpClient] = {}
        self._tools: dict[str, list[McpTool]] = {}
        self._digests: dict[str, str] = {}  # the snapshot each server's tools stand on
        self._withdrawn: set[str] = set()  # untrusted servers whose tools are withdrawn
        self._unreachable: set[str] = set()  # servers the last check could not reach
        self._unpinned: set[str] = set()  # untrusted servers said to have no snapshot
        self._listed_at: dict[str, float] = {}  # when each server last listed its tools (monotonic)
        self._ttl_s: dict[str, float] = {}  # how long that listing is fresh, by the server's word
        self._changed: set[str] = set()  # servers that said their tools changed, not yet rechecked
        self._nudge = asyncio.Event()
        self._checked = False
        for server in library.list():
            self._servers[server.name] = server
            snapshot = library.snapshot(server.name)
            self._tools[server.name] = (
                [McpTool(server, definition, self._caller(server.name)) for definition in snapshot.tools]
                if snapshot is not None
                else []
            )
            if snapshot is not None:
                self._digests[server.name] = snapshot.digest

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
        """The server's standing connection: its own headers, never a person's
        — a call with a person's token rides a connection of its own."""
        client = self._clients.get(name)
        if client is None:
            server = without_connection_headers(with_values(self._servers[name], self._values))
            client = self._clients[name] = McpClient(self._transport_factory(server))
            client.on_notification = lambda method, name=name: self._notified(name, method)
        return client

    def _notified(self, name: str, method: str) -> None:
        if method == TOOLS_CHANGED:
            self._changed.add(name)
            self._nudge.set()

    async def call(
        self, server: str, tool: str, arguments: Mapping[str, Any], headers: Mapping[str, str]
    ) -> CallResult:
        """Call once; a connection that failed before the request left is
        dropped and the call tried once more on a fresh one — a server
        restarted between calls is the ordinary case, not an error worth the
        model's attention. A request that did leave is not tried again: the
        server may have run it, and a second run under one card is a second
        action nobody confirmed."""
        if headers:
            # A per-call credential: this call's connection carries it, alone.
            spec = with_values(self._servers[server], self._values)
            client = McpClient(self._transport_factory(_with_headers(spec, headers)))
            try:
                return await client.call_tool(tool, arguments)
            finally:
                await client.close()
        client = await self.client(server)
        try:
            return await client.call_tool(tool, arguments)
        except McpError as exc:
            if exc.code != UNREACHABLE or exc.sent:
                raise
            logger.warning("mcp %s: %s; reconnecting", server, exc)
            await self._drop(server)
        return await (await self.client(server)).call_tool(tool, arguments)

    async def _drop(self, server: str) -> None:
        client = self._clients.pop(server, None)
        if client is not None:
            try:
                await client.close()
            except Exception:
                logger.debug("mcp %s: close failed", server, exc_info=True)

    async def start(self) -> CheckReport:
        """Connect every server, all at once, and check each one's tools
        against its snapshot — the first review."""
        return await self.recheck(reviewed=True)

    async def recheck(self, names: Collection[str] | None = None, *, reviewed: bool = False) -> CheckReport:
        """Ask the servers for their tools again and compare with the snapshot.
        Every server when ``names`` is None, else those; a server still fresh
        by the ttl its last listing carried is skipped unless named or
        ``reviewed``. ``reviewed`` says a person looked (the operator's reload
        after pinning anew): the snapshots are read from disk again, one that
        changed is adopted, and a withdrawn server whose tools match it is
        handed back. Without a review a withdrawn server stays withdrawn, its
        tools back to the snapshot or not."""
        report = CheckReport()
        due: list[str] = []
        for name in self._servers:
            if names is not None and name not in names:
                continue
            if names is None and not reviewed and self._fresh(name):
                report.skipped.append(name)
                continue
            if reviewed and not self._adopt(name, report):
                continue
            due.append(name)
        listed = await asyncio.gather(*(self._list(name) for name in due), return_exceptions=True)
        for name, outcome in zip(due, listed, strict=True):
            server = self._servers[name]
            if isinstance(outcome, BaseException):
                report.unreachable[name] = str(outcome)
                if name not in self._unreachable:
                    logger.warning(
                        "mcp %s: not reached%s: %s", name, "" if self._checked else " at startup", outcome
                    )
                self._unreachable.add(name)
                await self._drop(name)
                continue
            client, definitions = outcome
            self._listed_at[name] = time.monotonic()
            self._ttl_s[name] = client.list_ttl_s or 0.0
            if name in self._unreachable:
                self._unreachable.discard(name)
                logger.info("mcp %s: reached again", name)
            report.connected.append(name)
            if name not in self._digests:
                if server.trusted:
                    self._pin(name, definitions, client.server_info)
                    report.refreshed.append(name)
                else:
                    report.unpinned.append(name)
                    if name not in self._unpinned:
                        self._unpinned.add(name)
                        logger.error(
                            "mcp %s: no snapshot of its tools — nothing is advertised until "
                            "they are reviewed and pinned", name,
                        )
                continue
            if digest(definitions) == self._digests[name]:
                if reviewed and name in self._withdrawn:
                    self._withdrawn.discard(name)
                    report.restored[name] = self._tool_names(name)
                    logger.info("mcp %s: its tools match the reviewed snapshot again; handed back", name)
                continue
            if server.trusted:
                self._pin(name, definitions, client.server_info)
                report.refreshed.append(name)
                logger.info("mcp %s: tools changed; the snapshot follows (trusted)", name)
            elif name not in self._withdrawn:
                self._withdrawn.add(name)
                report.withdrawn[name] = self._tool_names(name)
                logger.error(
                    "mcp %s: its tools differ from the reviewed snapshot; %d tool(s) withdrawn "
                    "until they are reviewed and pinned again", name, len(report.withdrawn[name]),
                )
        self._checked = True
        return report

    async def run(self, interval_s: float, apply: Callable[[CheckReport], None]) -> None:
        """Recheck on a timer (``interval_s``; 0 for no timer) and at once when
        a server says its tools changed; ``apply`` gets what each check found.
        Runs until cancelled."""
        while True:
            try:
                await asyncio.wait_for(self._nudge.wait(), timeout=interval_s or None)
            except TimeoutError:
                pass
            self._nudge.clear()
            changed, self._changed = self._changed, set()
            apply(await self.recheck(names=changed or None))

    def _fresh(self, name: str) -> bool:
        """Whether the server's last listing is still good by its own ttl."""
        listed_at = self._listed_at.get(name)
        return listed_at is not None and time.monotonic() - listed_at < self._ttl_s.get(name, 0.0)

    def _tool_names(self, server: str) -> tuple[str, ...]:
        """The server's tools as the registry names them."""
        return tuple(tool.name for tool in self._tools.get(server, ()))

    def _adopt(self, name: str, report: CheckReport) -> bool:
        """Read the server's snapshot from disk again: a different one than the
        tools stand on is what a review wrote, and the tools follow it; one that
        is gone takes an untrusted server's tools with it. False when the file
        cannot be read (said in the log; the server is left as it was)."""
        try:
            snapshot = self._library.snapshot(name)
        except McpConfigError as exc:
            logger.error("mcp %s: %s", name, exc)
            return False
        if snapshot is None:
            if self._digests.pop(name, None) is not None and not self._servers[name].trusted and name not in self._withdrawn:
                self._withdrawn.add(name)
                report.withdrawn[name] = self._tool_names(name)
                logger.error(
                    "mcp %s: its snapshot is gone; %d tool(s) withdrawn until they are pinned again",
                    name, len(report.withdrawn[name]),
                )
        elif snapshot.digest != self._digests.get(name):
            self._digests[name] = snapshot.digest
            self._unpinned.discard(name)
            self._follow(name, snapshot.tools)
            report.refreshed.append(name)
        return True

    async def _list(self, name: str) -> tuple[McpClient, tuple[ToolDefinition, ...]]:
        client = await self.client(name)
        return client, await client.list_tools()

    def _pin(self, name: str, definitions: tuple[ToolDefinition, ...], server_info: Mapping[str, Any]) -> None:
        """A trusted server's word, taken: the snapshot and the live tools follow it."""
        snapshot = Snapshot.taken(name, definitions, _now(), server_info)
        self._library.save_snapshot(snapshot)
        self._digests[name] = snapshot.digest
        self._unpinned.discard(name)
        self._follow(name, snapshot.tools)

    def _follow(self, name: str, definitions: tuple[ToolDefinition, ...]) -> None:
        """The live tools take these definitions, by name. A tool the registry
        does not have is not added — the registry is what it was at startup —
        and a dropped one keeps its definition until a restart; the call will
        say so."""
        by_name = {d.name: d for d in definitions}
        for tool in self._tools.get(name, ()):
            if tool.definition.name in by_name:
                tool.definition = by_name.pop(tool.definition.name)
        if by_name:
            logger.info("mcp %s: %d new tool(s) pinned; they appear at the next start", name, len(by_name))

    async def stop(self) -> None:
        for name in list(self._clients):
            await self._drop(name)


def _with_headers(server: McpServer, headers: Mapping[str, str]) -> McpServer:
    return replace(server, headers={**without_connection_headers(server).headers, **headers})


def without_connection_headers(server: McpServer) -> McpServer:
    """The server's headers less the ones that are a person's token: a listing,
    or a call for nobody in particular, carries none."""
    return replace(server, headers={k: v for k, v in server.headers.items() if not CONNECTION_REF.match(v)})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
