"""An MCP server's tool as a tool of the engine.

Named ``mcp__<server>__<tool>``, it stands in the registry beside the engine's
own: advertised to the agents that name it, called through the tool server
with everything that implies — the allowlist, a conversation's entitlement, a
confirmation card unless the operator or a trusted server says the tool is
harmless, the trace — and executed by calling the server. Its result is a
stranger's text and comes wrapped as such.
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
from crucible.ports.chat.types import CallPreview, PreviewRow
from crucible.ports.connections import (
    ConnectionsError,
    NotConnected,
    ProviderUnavailable,
    TokenGrant,
)
from crucible.tools.base import CAP_CONNECTIONS, ToolContext, ToolError

logger = logging.getLogger(__name__)

TOOL_PREFIX = "mcp__"
TOOL_SEP = "__"
# An argument up to this long is a one-line fact; past it, a passage shown whole.
_ARG_LIMIT = 200
# What the model is told when the server could not be called, by error code —
# fixed wording, the server's own message stays in the log.
_FAILED = {
    UNREACHABLE: "{server} could not be reached; try again later",
    REFUSED: "{server} refused the call",
    PROTOCOL: "{server} answered in a way the engine does not accept",
    UNSUPPORTED: "{server} asked for something the engine does not do (more input mid-call)",
}
# The request reached the server and no answer came: it may have run.
_UNANSWERED = "{server} did not answer in time; the call may have run — check before repeating it"
# The server would not take the person's token any more: their connection is over.
_TOKEN_REFUSED = (
    "{server} no longer accepts the user's connection, so it has ended; ask them to "
    "connect their {provider} account again in a direct message with you"
)

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


def destructive(definition: ToolDefinition) -> bool:
    """Whether the server says the tool may destroy something — with the
    protocol's own defaults: a tool is destructive unless it is marked
    read-only or ``destructiveHint`` is explicitly false. Absent annotations
    are a server that said nothing, not a server that said "harmless"."""
    annotations = definition.annotations
    if annotations.get("readOnlyHint") is True:
        return False
    return annotations.get("destructiveHint", True) is not False


def requires_confirmation(server: McpServer, definition: ToolDefinition) -> bool:
    """Whether a person must confirm a call. The operator's word first:
    ``confirm:`` patterns always ask, ``safe:`` patterns never do. Then the
    server's — but only a trusted server's annotations are a decision's basis
    (the protocol says a client must not decide by an untrusted server's
    hints); an untrusted server's tool the operator did not name is asked
    about."""
    name = definition.name
    if any(fnmatchcase(name, pattern) for pattern in server.confirm):
        return True
    if any(fnmatchcase(name, pattern) for pattern in server.safe):
        return False
    return destructive(definition) if server.trusted else True


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
        self.requires_confirmation = requires_confirmation(self.server, definition)

    async def execute(self, ctx: ToolContext, args: dict[str, Any]) -> Any:
        headers, grants = await self._credentials(ctx)
        try:
            result = await self._call(self._definition.name, args, headers)
        except McpError as exc:
            # The engine's own words and the code: the server's message goes to
            # the log, not to the model — an error text is as much a stranger's
            # as a result, and a tool error is read as the tool's own voice.
            logger.warning("mcp %s: %s failed: %s: %s", self.server.name, self._definition.name, exc.code, exc)
            if exc.code == REFUSED and exc.status == 401 and grants:
                # A 401 on a call that carried the person's token is read as
                # the token's: the connection it came from is over, as the
                # connection tools treat a 401 — forgotten, and the person asked
                # to connect again. (A server whose own ${VAR} credential expired
                # answers the same status; from the status alone the two cannot
                # be told apart, so the person's connection goes first.)
                assert ctx.connections is not None
                for grant in grants:
                    await ctx.connections.forget(grant)
                raise ToolError(
                    _TOKEN_REFUSED.format(server=self.server.name, provider=", ".join(g.provider for g in grants))
                ) from exc
            if exc.code == UNREACHABLE and exc.sent:
                raise ToolError(_UNANSWERED.format(server=self.server.name)) from exc
            raise ToolError(_FAILED[exc.code].format(server=self.server.name)) from exc
        if result.is_error:
            # The server's refusal is data of the server's, wrapped as such by
            # the tool server (``returns_untrusted``), never raised as our own.
            return {"error": True, "text": result.text}
        return result.structured if result.structured is not None else result.text

    async def _credentials(self, ctx: ToolContext) -> tuple[dict[str, str], tuple[TokenGrant, ...]]:
        """The person's credentials for this call alone: the headers with their
        tokens filled in, and the grants they came from, for a server that
        turns one down. The server's own headers are the service's, filled from
        the engine's values."""
        headers: dict[str, str] = {}
        grants: list[TokenGrant] = []
        for key, value in self.server.headers.items():
            match = CONNECTION_REF.match(value)
            if match is None:
                continue
            provider = match.group(1)
            if not ctx.direct:
                # Whose turn it is in a shared conversation is not settled
                # enough to act as anyone — the rule the connection tools keep.
                raise ToolError(
                    f"{self.server.name} acts as the person, which works only in a direct message "
                    "with them; tell them to ask there"
                )
            if ctx.connections is None or provider not in ctx.connections.providers:
                raise ToolError(
                    f"{self.server.name} acts as the person at {provider}, which is not a provider "
                    "anyone can connect here"
                )
            try:
                grant = await ctx.connections.grant(provider)
            except NotConnected as exc:
                # What the connection tools say: the model tells the person how
                # to connect, and never asks them for a token.
                raise ToolError(
                    f"the user has not connected their {provider} account, which {self.server.name} acts "
                    "as them at. Ask them to connect it in a direct message with you (the connections "
                    "command); never ask them for a token or a password"
                ) from exc
            except ProviderUnavailable as exc:
                raise ToolError(f"{provider} did not answer; try again in a minute") from exc
            except ConnectionsError as exc:
                raise ToolError(str(exc)) from exc
            grants.append(grant)
            headers[key] = f"Bearer {grant.token.reveal()}"
        return headers, tuple(grants)

    async def describe(self, ctx: ToolContext, args: dict[str, Any]) -> CallPreview | None:
        # A short argument is a fact on one line; a long or multi-line one — a
        # script, a comment's body — is a passage the person reads whole: a
        # card that cut it would ask them to approve what they cannot see.
        rows = tuple(_argument_row(key, _text(value)) for key, value in args.items())
        return CallPreview(
            title=f"{self.display_name} ({self.server.name})",
            rows=rows,
            danger=destructive(self._definition),
        )

    def caption(self, args: Mapping[str, Any]) -> str | None:
        return self.display_name


def _text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _argument_row(key: str, text: str) -> PreviewRow:
    if "\n" in text or len(text) > _ARG_LIMIT:
        return PreviewRow(label=key, value=text, passage=True)
    return PreviewRow(label=key, value=one_line(text, limit=_ARG_LIMIT))
