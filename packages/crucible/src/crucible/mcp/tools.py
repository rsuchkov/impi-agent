"""An MCP server's tool as a tool of the engine.

Named ``mcp__<server>__<tool>``, it stands in the registry beside the engine's
own: advertised to the agents that name it, called through the tool server
with everything that implies — the allowlist, a conversation's entitlement, a
confirmation card when the server's entry or the server's own
``destructiveHint`` says so, the trace — and executed by calling the server.
Its result is a stranger's text and comes wrapped as such.
"""

import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from fnmatch import fnmatchcase
from typing import Any

from crucible.containment import one_line
from crucible.mcp.library import CONNECTION_REF, McpServer
from crucible.mcp.protocol import CallResult, McpError, ToolDefinition
from crucible.ports.chat.types import CallPreview, PreviewRow
from crucible.tools.base import CAP_CONNECTIONS, ToolContext, ToolError

logger = logging.getLogger(__name__)

TOOL_PREFIX = "mcp__"
TOOL_SEP = "__"
# What a call's row shows of one argument.
_ARG_LIMIT = 200

# The server, called: (tool name, arguments, headers for this call) -> result.
Caller = Callable[[str, Mapping[str, Any], Mapping[str, str]], Awaitable[CallResult]]


def tool_name(server: str, tool: str) -> str:
    return f"{TOOL_PREFIX}{server}{TOOL_SEP}{tool}"


def split_tool_name(name: str) -> tuple[str, str] | None:
    """(server, tool) of an engine name for an MCP tool, or None for any other."""
    if not name.startswith(TOOL_PREFIX):
        return None
    server, sep, tool = name[len(TOOL_PREFIX):].partition(TOOL_SEP)
    return (server, tool) if sep and server and tool else None


def is_server_pattern(entry: str) -> str:
    """The server an allowlist entry ``mcp__<server>__*`` names, "" otherwise."""
    parts = split_tool_name(entry)
    return parts[0] if parts is not None and parts[1] == "*" else ""


class McpTool:
    """One definition, as the registry holds it. Not a class per tool: the
    definition is data, so the tool is an instance whose attributes carry it."""

    settings_cls = None
    speaks_to_user = False
    returns_untrusted = True

    def __init__(self, server: McpServer, definition: ToolDefinition, call: Caller) -> None:
        self.server = server
        self._call = call
        self.name = tool_name(server.name, definition.name)
        # The headers that are a person's token are filled per call: the tool
        # then needs the connections capability to be advertised at all.
        self.requires = frozenset({CAP_CONNECTIONS}) if server.connection_providers() else frozenset()
        self.definition = definition  # sets description, parameters, display_name, confirmation

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    @definition.setter
    def definition(self, definition: ToolDefinition) -> None:
        self._definition = definition
        self.description = definition.description or f"{definition.name} on {self.server.name}"
        self.parameters = dict(definition.input_schema)
        self.display_name = definition.title or definition.name
        destructive = bool(definition.annotations.get("destructiveHint", False))
        self.requires_confirmation = destructive or any(
            fnmatchcase(definition.name, pattern) for pattern in self.server.confirm
        )

    async def execute(self, ctx: ToolContext, args: dict[str, Any]) -> Any:
        headers = await self._headers(ctx)
        try:
            result = await self._call(self._definition.name, args, headers)
        except McpError as exc:
            raise ToolError(f"{self.server.name}: {exc.code}: {exc}") from exc
        if result.is_error:
            raise ToolError(f"{self.server.name} refused the call: {one_line(result.text, limit=500)}")
        return result.structured if result.structured is not None else result.text

    async def _headers(self, ctx: ToolContext) -> dict[str, str]:
        """The server's headers with the person's tokens filled in."""
        headers: dict[str, str] = {}
        for key, value in self.server.headers.items():
            match = CONNECTION_REF.match(value)
            if match is None:
                headers[key] = value
                continue
            provider = match.group(1)
            if ctx.connections is None or provider not in ctx.connections.providers:
                raise ToolError(
                    f"{self.server.name} acts as the person at {provider}, and they have not "
                    f"connected a {provider} account here"
                )
            grant = await ctx.connections.grant(provider)
            headers[key] = f"Bearer {grant.token.reveal()}"
        return headers

    async def describe(self, ctx: ToolContext, args: dict[str, Any]) -> CallPreview | None:
        rows = tuple(
            PreviewRow(label=key, value=one_line(_text(value), limit=_ARG_LIMIT)) for key, value in args.items()
        )
        return CallPreview(
            title=f"{self.display_name} ({self.server.name})",
            rows=rows,
            danger=bool(self._definition.annotations.get("destructiveHint", False)),
        )

    def caption(self, args: Mapping[str, Any]) -> str | None:
        return self.display_name


def _text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
