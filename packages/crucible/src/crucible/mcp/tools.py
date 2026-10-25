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
from crucible.mcp.protocol import (
    PROTOCOL,
    REFUSED,
    UNREACHABLE,
    UNSUPPORTED,
    CallResult,
    McpError,
    ToolDefinition,
)
from crucible.ports.chat.types import KIND_DM, CallPreview, PreviewRow
from crucible.tools.base import CAP_CONNECTIONS, ToolContext, ToolError

logger = logging.getLogger(__name__)

TOOL_PREFIX = "mcp__"
TOOL_SEP = "__"
# What a call's row shows of one argument.
_ARG_LIMIT = 200
# What the model is told when the server could not be called, by error code —
# fixed wording, the server's own message stays in the log.
_FAILED = {
    UNREACHABLE: "{server} could not be reached; try again later",
    REFUSED: "{server} refused the call",
    PROTOCOL: "{server} answered in a way the engine does not accept",
    UNSUPPORTED: "{server} asked for something the engine does not do (more input mid-call)",
}

# The server, called: (tool name, arguments, headers for this call) -> result.
Caller = Callable[[str, Mapping[str, Any], Mapping[str, str]], Awaitable[CallResult]]


def mcp_tool_name(server: str, tool: str) -> str:
    """The engine's name for a server's tool."""
    return f"{TOOL_PREFIX}{server}{TOOL_SEP}{tool}"


def split_tool_name(name: str) -> tuple[str, str] | None:
    """(server, tool) of an engine name for an MCP tool, or None for any other."""
    if not name.startswith(TOOL_PREFIX):
        return None
    server, sep, tool = name[len(TOOL_PREFIX):].partition(TOOL_SEP)
    return (server, tool) if sep and server and tool else None


class McpTool:
    """One definition, as the registry holds it. Not a class per tool: the
    definition is data, so the tool is an instance whose attributes carry it."""

    settings_cls = None
    speaks_to_user = False
    returns_untrusted = True

    def __init__(self, server: McpServer, definition: ToolDefinition, call: Caller) -> None:
        self.server = server
        self._call = call
        self.name = mcp_tool_name(server.name, definition.name)
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
            # The engine's own words and the code: the server's message goes to
            # the log, not to the model — an error text is as much a stranger's
            # as a result, and a tool error is read as the tool's own voice.
            logger.warning("mcp %s: %s failed: %s: %s", self.server.name, self._definition.name, exc.code, exc)
            raise ToolError(_FAILED[exc.code].format(server=self.server.name)) from exc
        if result.is_error:
            # The server's refusal is data of the server's, wrapped as such by
            # the tool server (``returns_untrusted``), never raised as our own.
            return {"error": True, "text": result.text}
        return result.structured if result.structured is not None else result.text

    async def _headers(self, ctx: ToolContext) -> dict[str, str]:
        """The headers of this call alone: the person's tokens, filled in. The
        server's own headers are the service's, filled from the engine's values."""
        headers: dict[str, str] = {}
        for key, value in self.server.headers.items():
            match = CONNECTION_REF.match(value)
            if match is None:
                continue
            provider = match.group(1)
            if ctx.conversation_kind != KIND_DM:
                # Whose turn it is in a shared conversation is not settled
                # enough to act as anyone — the rule the connection tools keep.
                raise ToolError(
                    f"{self.server.name} acts as the person, which works only in a direct message "
                    "with them; tell them to ask there"
                )
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
