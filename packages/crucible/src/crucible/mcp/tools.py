"""An MCP server's tool as a tool of the engine.

Named ``mcp__<server>__<tool>``, it stands in the registry beside the engine's
own: advertised to the agents that name it, called through the tool server
with everything that implies — the allowlist, a conversation's entitlement, a
confirmation card unless the operator or a trusted server says the tool is
harmless, the trace — and executed by calling the server. Its result is a
stranger's text and comes wrapped as such.
"""

import base64
import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from fnmatch import fnmatchcase
from typing import Any

from crucible.containment import one_line
from crucible.content_types import KNOWN_CONTENT_TYPES, format_size, sniff_content_type
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
from crucible.ports.attachments import (
    ReceivedFile,
    ReceivedFileChanged,
    ReceivedFileGone,
)
from crucible.ports.chat.types import CallPreview, PreviewRow
from crucible.ports.connections import (
    ConnectionsError,
    NotConnected,
    ProviderUnavailable,
    TokenGrant,
)
from crucible.ports.telemetry import McpCalled, Telemetry
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
# A server's schema saying "this string is a file's content" — JSON Schema's
# own word for it, pinned in the snapshot like the rest of the schema.
_CONTENT_ENCODING = "contentEncoding"
_CONTENT_MEDIA_TYPE = "contentMediaType"
_BASE64_ENCODING = "base64"
# What the model is told of such an argument instead of "a base64 string".
_FILE_ARGUMENT_NOTE = (
    "A file the person attached in this conversation: give the path from its [attached] line; "
    "the engine sends the file's content."
)
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
    """Whether a person must confirm a call. A tool that takes a person's file
    always asks — the file leaves the engine — whatever else says. Then the
    operator's word: ``confirm:`` patterns always ask, ``safe:`` patterns never
    do. Then the server's — but only a trusted server's annotations are a
    decision's basis (the protocol says a client must not decide by an
    untrusted server's hints); an untrusted server's tool the operator did not
    name is asked about."""
    if file_arguments(definition):
        return True
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
    posts_to_user = False
    returns_untrusted = True

    def __init__(
        self, server: McpServer, definition: ToolDefinition, call: Caller, *, telemetry: Telemetry | None = None
    ) -> None:
        self.server = server
        self._call = call
        self._telemetry = telemetry
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
        self.file_arguments = file_arguments(definition)
        self.parameters = _parameters_for_model(definition.input_schema, self.file_arguments)
        self.display_name = definition.title or definition.name
        self.requires_confirmation = requires_confirmation(self.server, definition)
        # The server's word on what the tool does, taken only from a trusted
        # server; an untrusted one's tools are all read as destructive.
        self.destructive = destructive(definition) if self.server.trusted else True

    async def execute(self, ctx: ToolContext, args: dict[str, Any]) -> Any:
        started = time.monotonic()
        try:
            return await self._run(ctx, args, started)
        except _Ended as ended:
            self._tell(ctx, ended.outcome, started, ended.file_bytes)
            raise ended.error from ended.error.__cause__

    async def _run(self, ctx: ToolContext, args: dict[str, Any], started: float) -> Any:
        """The call, with every way it can end named for the telemetry."""
        try:
            headers, grants = await self._credentials(ctx)
            args, file_bytes = await self._with_files(ctx, args)
        except _Declined as exc:
            raise _Ended(exc, exc.outcome) from exc
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
                raise _Ended(ToolError(
                    _TOKEN_REFUSED.format(server=self.server.name, provider=", ".join(g.provider for g in grants))
                ), "token_refused", file_bytes) from exc
            if exc.code == UNREACHABLE and exc.sent:
                raise _Ended(ToolError(_UNANSWERED.format(server=self.server.name)), "unanswered", file_bytes) from exc
            raise _Ended(ToolError(_FAILED[exc.code].format(server=self.server.name)), exc.code, file_bytes) from exc
        if result.is_error:
            # The server's refusal is data of the server's, wrapped as such by
            # the tool server (``returns_untrusted``), never raised as our own.
            self._tell(ctx, "is_error", started, file_bytes)
            return {"error": True, "text": result.text}
        self._tell(ctx, "ok", started, file_bytes)
        return result.structured if result.structured is not None else result.text

    def _tell(self, ctx: ToolContext, outcome: str, started: float, file_bytes: int) -> None:
        if self._telemetry is None:
            return
        try:
            self._telemetry.record(McpCalled(
                server=self.server.name, tool=self._definition.name, agent=ctx.agent_name, outcome=outcome,
                duration_s=time.monotonic() - started, file_bytes=file_bytes,
            ))
        except Exception:  # noqa: BLE001 - telemetry must never cost the call
            logger.warning("telemetry failed to record an MCP call", exc_info=True)

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
                raise _Declined(
                    "not_direct",
                    f"{self.server.name} acts as the person, which works only in a direct message "
                    "with them; tell them to ask there"
                )
            if ctx.connections is None or provider not in ctx.connections.providers:
                raise _Declined(
                    "no_provider",
                    f"{self.server.name} acts as the person at {provider}, which is not a provider "
                    "anyone can connect here"
                )
            try:
                grant = await ctx.connections.grant(provider)
            except NotConnected as exc:
                # What the connection tools say: the model tells the person how
                # to connect, and never asks them for a token.
                raise _Declined(
                    "not_connected",
                    f"the user has not connected their {provider} account, which {self.server.name} acts "
                    "as them at. Ask them to connect it in a direct message with you (the connections "
                    "command); never ask them for a token or a password"
                ) from exc
            except ProviderUnavailable as exc:
                raise _Declined("provider_unavailable", f"{provider} did not answer; try again in a minute") from exc
            except ConnectionsError as exc:
                raise _Declined("not_connected", str(exc)) from exc
            grants.append(grant)
            headers[key] = f"Bearer {grant.token.reveal()}"
        return headers, tuple(grants)

    async def describe(self, ctx: ToolContext, args: dict[str, Any]) -> CallPreview | None:
        # A short argument is a fact on one line; a long or multi-line one — a
        # script, a comment's body — is a passage the person reads whole: a
        # card that cut it would ask them to approve what they cannot see. A
        # file argument says which of the person's files leaves, to whom, and
        # its digest — the bytes sent are the ones the record describes.
        rows: list[PreviewRow] = []
        for key, value in args.items():
            if key in self.file_arguments:
                rows.append(await self._file_row(ctx, key, _text(value)))
            else:
                rows.append(_argument_row(key, _text(value)))
        return CallPreview(
            title=f"{self.display_name} ({self.server.name})",
            rows=tuple(rows),
            danger=destructive(self._definition),
        )

    def caption(self, args: Mapping[str, Any]) -> str | None:
        return self.display_name

    # -- a person's file as an argument ------------------------------------------

    async def _with_files(self, ctx: ToolContext, args: dict[str, Any]) -> tuple[dict[str, Any], int]:
        """The arguments the server declared as content, with the person's file
        in place of the path the model gave — read once, checked against the
        record of what arrived, and against the type the schema names — and
        how many bytes of files went in."""
        if not self.file_arguments:
            return args, 0
        sent = 0
        filled = dict(args)
        for key in self.file_arguments:
            if key not in filled:
                continue
            received = await self._record_of(ctx, _text(filled[key]))
            assert ctx.received_files is not None  # _record_of said so
            try:
                data = await ctx.received_files.read(received)
            except ReceivedFileGone as exc:
                raise _Declined(
                    "file_gone",
                    f"{received.name} is no longer here (files are kept for a while, not forever); "
                    "ask the person to attach it again"
                ) from exc
            except ReceivedFileChanged as exc:
                raise _Declined(
                    "file_changed", f"{received.name} is not the file the person attached any more; ask them to attach it again"
                ) from exc
            wanted = str((self._definition.input_schema.get("properties") or {}).get(key, {}).get(_CONTENT_MEDIA_TYPE) or "")
            if wanted:
                _check_type(self.name, key, received, data, wanted)
            filled[key] = base64.b64encode(data).decode("ascii")
            sent += len(data)
        return filled, sent

    async def _record_of(self, ctx: ToolContext, path: str) -> ReceivedFile:
        """The record of the file at ``path`` among what this conversation
        received — the only files a tool may hand on."""
        if ctx.received_files is None:
            raise _Declined("no_file", f"{self.name} takes a file, and this engine keeps none")
        for record in await ctx.received_files.list():
            if record.path == path:
                return record
        raise _Declined(
            "no_file",
            f"{path!r} is not a file the person attached in this conversation; give the path "
            "from an [attached] line of this conversation, and nothing else"
        )

    async def _file_row(self, ctx: ToolContext, key: str, path: str) -> PreviewRow:
        try:
            received = await self._record_of(ctx, path)
        except ToolError as exc:
            return PreviewRow(label=f"{key} (file)", value=one_line(str(exc), limit=_ARG_LIMIT))
        return PreviewRow(
            label=f"{key} (file to {self.server.name})",
            value=f"{received.name} — {received.mime or 'unknown type'}, {format_size(received.size)}, sha256 {received.sha256[:12]}",
        )


class _Declined(ToolError):
    """A tool error raised where the engine declined the call itself — the
    server never saw it; ``REFUSED`` is the server's word — with the outcome
    word the telemetry gets for it, named where the reason is known, never
    read back from the message."""

    def __init__(self, outcome: str, message: str) -> None:
        super().__init__(message)
        self.outcome = outcome


class _Ended(Exception):
    """How a call ended short of a result: the error the model gets, the
    outcome word the telemetry gets, and the bytes of files that went in."""

    def __init__(self, error: ToolError, outcome: str, file_bytes: int = 0) -> None:
        super().__init__(str(error))
        self.error = error
        self.outcome = outcome
        self.file_bytes = file_bytes


def file_arguments(definition: ToolDefinition) -> tuple[str, ...]:
    """The top-level arguments the server's schema declares as a file's content
    (``contentEncoding: base64`` on a string), in schema order."""
    properties = definition.input_schema.get("properties") or {}
    return tuple(
        name for name, schema in properties.items()
        if isinstance(schema, Mapping) and schema.get(_CONTENT_ENCODING) == _BASE64_ENCODING and _is_string(schema)
    )


def _is_string(schema: Mapping[str, Any]) -> bool:
    """Whether the schema admits a string: ``"type": "string"`` or a list that
    names it (``["string", "null"]`` — an optional argument); no type at all
    admits anything."""
    declared = schema.get("type", "string")
    types = declared if isinstance(declared, list) else [declared]
    return "string" in types


def _check_type(tool: str, key: str, received: ReceivedFile, data: bytes, wanted: str) -> None:
    """The schema names a media type: the bytes decide where the engine can
    tell one (an image, a PDF); where it cannot, the type the platform
    claimed for the file stands in."""
    found = sniff_content_type(data)
    if found:
        if found != wanted:
            raise _Declined("file_type", f"{tool} takes {wanted} for {key}; {received.name} is {found}")
        return
    if wanted in KNOWN_CONTENT_TYPES:
        raise _Declined("file_type", f"{tool} takes {wanted} for {key}; {received.name} is not that by its bytes")
    if received.mime and received.mime != wanted:
        raise _Declined(
            "file_type", f"{tool} takes {wanted} for {key}; {received.name} is {received.mime}, the platform says"
        )


def _parameters_for_model(schema: Mapping[str, Any], files: tuple[str, ...]) -> dict[str, Any]:
    """The schema as the model is shown it: a content argument asks for the
    path of an attached file, since content is what the engine supplies."""
    if not files:
        return dict(schema)
    properties = {}
    for name, prop in (schema.get("properties") or {}).items():
        if name in files and isinstance(prop, Mapping):
            shown = {k: v for k, v in prop.items() if k not in (_CONTENT_ENCODING, _CONTENT_MEDIA_TYPE)}
            described = str(shown.get("description") or "").strip()
            shown["description"] = f"{described} {_FILE_ARGUMENT_NOTE}".strip()
            properties[name] = shown
        else:
            properties[name] = prop
    return {**schema, "properties": properties}



def _text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _argument_row(key: str, text: str) -> PreviewRow:
    if "\n" in text or len(text) > _ARG_LIMIT:
        return PreviewRow(label=key, value=text, passage=True)
    return PreviewRow(label=key, value=one_line(text, limit=_ARG_LIMIT))
