"""A server's tool as a tool of the engine."""

from typing import Any

import pytest

from crucible.mcp import (
    McpServer,
    McpTool,
    ToolDefinition,
    is_server_pattern,
    split_tool_name,
    tool_name,
)
from crucible.mcp.protocol import CallResult, McpError
from crucible.ports.connections import AccessToken, ProviderAccount, TokenGrant
from crucible.tools.base import CAP_CONNECTIONS, ToolContext, ToolError
from tests.test_tools import AGENTS, FakeDirectory

ECHO = ToolDefinition("echo", "Echoes.", {"type": "object", "properties": {"text": {"type": "string"}}}, title="Echo")
WIPE = ToolDefinition("wipe", "Wipes.", {"type": "object"}, annotations={"destructiveHint": True})
DELETE = ToolDefinition("delete_repo", "Deletes a repository.", {"type": "object"})


class _Calls:
    def __init__(self, result: CallResult | Exception) -> None:
        self.result = result
        self.seen: list[tuple[str, dict, dict]] = []

    async def __call__(self, tool: str, args, headers) -> CallResult:
        self.seen.append((tool, dict(args), dict(headers)))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _ctx(**over: Any) -> ToolContext:
    return ToolContext(agent_name="assistant", directory=FakeDirectory(AGENTS), **over)  # type: ignore[arg-type]


def test_names_say_the_server_and_the_tool() -> None:
    assert tool_name("github", "create_issue") == "mcp__github__create_issue"
    assert split_tool_name("mcp__github__create_issue") == ("github", "create_issue")
    assert split_tool_name("mcp__github__") is None and split_tool_name("read") is None
    assert is_server_pattern("mcp__github__*") == "github" and is_server_pattern("mcp__github__x") == ""


def test_a_tool_carries_its_definition_and_knows_when_a_person_must_confirm() -> None:
    server = McpServer("github", url="https://x/mcp", confirm=("delete_*",))
    echo, wipe, delete = (McpTool(server, d, _Calls(CallResult("x"))) for d in (ECHO, WIPE, DELETE))
    assert echo.name == "mcp__github__echo" and echo.display_name == "Echo" and echo.description == "Echoes."
    assert echo.parameters == ECHO.input_schema and echo.returns_untrusted is True
    assert echo.requires_confirmation is False
    assert wipe.requires_confirmation is True  # the server says destructive
    assert delete.requires_confirmation is True  # the operator's pattern
    assert echo.requires == frozenset() and echo.caption({}) == "Echo"
    unnamed = McpTool(server, ToolDefinition("x", "", {"type": "object"}), _Calls(CallResult("x")))
    assert unnamed.description == "x on github" and unnamed.display_name == "x"


async def test_a_call_returns_the_text_or_the_structured_content_and_an_error_is_a_tool_error() -> None:
    server = McpServer("github", url="https://x/mcp")
    calls = _Calls(CallResult("echo: hi"))
    tool = McpTool(server, ECHO, calls)
    assert await tool.execute(_ctx(), {"text": "hi"}) == "echo: hi"
    assert calls.seen == [("echo", {"text": "hi"}, {})]
    assert await McpTool(server, WIPE, _Calls(CallResult("wiped", {"wiped": True}))).execute(_ctx(), {}) == {"wiped": True}
    with pytest.raises(ToolError, match="refused the call: no such"):
        await McpTool(server, ECHO, _Calls(CallResult("no such tool", is_error=True))).execute(_ctx(), {})
    with pytest.raises(ToolError, match="github: unreachable"):
        await McpTool(server, ECHO, _Calls(McpError("unreachable", "down"))).execute(_ctx(), {})


class _Connections:
    def __init__(self, tokens: dict[str, str]) -> None:
        self._tokens = tokens
        self.granted: list[str] = []

    @property
    def providers(self) -> tuple[str, ...]:
        return tuple(self._tokens)

    async def grant(self, provider: str) -> TokenGrant:
        self.granted.append(provider)
        return TokenGrant(provider, AccessToken(self._tokens[provider]), ProviderAccount("1", "roman"), "c1", 1)

    async def forget(self, grant: TokenGrant) -> None:
        pass

    async def account(self, provider: str) -> ProviderAccount | None:
        return None


async def test_a_header_that_is_the_persons_token_is_filled_per_call_from_their_connection() -> None:
    server = McpServer("gitlab", url="https://x/mcp", headers={"Authorization": "${connection:gitlab}", "X-Team": "ops"})
    calls = _Calls(CallResult("ok"))
    tool = McpTool(server, ECHO, calls)
    assert tool.requires == frozenset({CAP_CONNECTIONS})  # advertised only where connections exist
    connections = _Connections({"gitlab": "glpat-xyz"})
    assert await tool.execute(_ctx(connections=connections), {"text": "x"}) == "ok"
    assert calls.seen[0][2] == {"Authorization": "Bearer glpat-xyz", "X-Team": "ops"}
    assert connections.granted == ["gitlab"]
    with pytest.raises(ToolError, match="not connected a gitlab account"):
        await tool.execute(_ctx(connections=_Connections({"github": "t"})), {"text": "x"})
    with pytest.raises(ToolError, match="not connected"):
        await tool.execute(_ctx(), {"text": "x"})
    assert len(calls.seen) == 1  # nothing was sent without the token


async def test_the_preview_shows_the_arguments_and_flags_a_destructive_call() -> None:
    server = McpServer("github", url="https://x/mcp")
    preview = await McpTool(server, WIPE, _Calls(CallResult("x"))).describe(_ctx(), {"path": "/", "force": True})
    assert preview is not None and preview.title == "wipe (github)" and preview.danger is True
    assert [(r.label, r.value) for r in preview.rows] == [("path", "/"), ("force", "true")]
    assert (await McpTool(server, ECHO, _Calls(CallResult("x"))).describe(_ctx(), {})).danger is False  # type: ignore[union-attr]
