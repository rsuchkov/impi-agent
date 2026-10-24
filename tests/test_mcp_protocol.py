"""The least of MCP: list and call — over HTTP and over stdio, in both eras."""

import asyncio
import sys
from pathlib import Path

import pytest

from crucible.mcp.protocol import (
    LEGACY_VERSION,
    MODERN_VERSION,
    PROTOCOL,
    REFUSED,
    UNREACHABLE,
    UNSUPPORTED,
    HttpTransport,
    McpClient,
    McpError,
    StdioTransport,
)
from tests.fakes.mcp_http import ASK, ECHO, WIPE, FakeMcpHttp

STDIO_SERVER = str(Path(__file__).parent / "fakes" / "mcp_stdio_server.py")


async def test_http_speaks_the_current_revision_with_metadata_in_the_body_and_the_headers() -> None:
    server = FakeMcpHttp(8580, tools=[ECHO, WIPE, ASK])
    await server.start()
    client = McpClient(HttpTransport(server.url, {"X-Team": "ops"}))
    try:
        tools = await client.list_tools()
        assert [t.name for t in tools] == ["echo", "wipe", "ask"] and tools[0].title == "Echo"
        assert tools[1].annotations == {"destructiveHint": True}
        assert client.version == MODERN_VERSION and client.legacy is False
        assert client.server_info["name"] == "fake-http"  # from the result's _meta
        result = await client.call_tool("echo", {"text": "hi"})
        assert result.text == "echo: hi" and result.structured is None and result.is_error is False
        assert (await client.call_tool("wipe", {})).structured == {"wiped": True}
        assert (await client.call_tool("nothing", {})).is_error is True
        name, args, headers = server.calls[0]
        assert (name, args) == ("echo", {"text": "hi"})
        assert headers["X-Team"] == "ops" and headers["MCP-Protocol-Version"] == MODERN_VERSION
        assert headers["Mcp-Method"] == "tools/call" and headers["Mcp-Name"] == "echo"
        assert "Mcp-Session-Id" not in headers and server.sessions == 0  # no handshake, no session
        meta = server.requests[-1]["params"]["_meta"]
        assert meta["io.modelcontextprotocol/protocolVersion"] == MODERN_VERSION
        assert meta["io.modelcontextprotocol/clientInfo"]["name"] == "crucible"
        assert meta["io.modelcontextprotocol/clientCapabilities"] == {}
        # A server that wants more from the person or the model is told no.
        with pytest.raises(McpError) as caught:
            await client.call_tool("ask", {})
        assert caught.value.code == UNSUPPORTED
    finally:
        await client.close()
        await server.stop()


async def test_http_falls_back_to_the_handshake_for_a_legacy_server_and_keeps_its_session() -> None:
    server = FakeMcpHttp(8581, era="legacy")
    await server.start()
    client = McpClient(HttpTransport(server.url))
    try:
        assert [t.name for t in await client.list_tools()] == ["echo", "wipe"]
        assert client.legacy is True and client.version == LEGACY_VERSION
        assert client.server_info == {"name": "fake-http", "version": "1"}  # from initialize
        assert (await client.call_tool("echo", {"text": "hi"})).text == "echo: hi"
        assert server.sessions == 1 and server.calls[0][2]["Mcp-Session-Id"] == "s1"
        methods = [m["method"] for m in server.requests]
        assert methods[:3] == ["tools/list", "initialize", "notifications/initialized"]  # modern first, then the handshake
        assert "_meta" not in server.requests[-1]["params"]  # legacy requests carry none
    finally:
        await client.close()
        await server.stop()


async def test_http_takes_the_version_a_modern_server_offers_or_says_none_is_shared() -> None:
    older = FakeMcpHttp(8582, supported=("2025-11-25",))  # modern server that only speaks the handshake era
    await older.start()
    client = McpClient(HttpTransport(older.url))
    try:
        assert [t.name for t in await client.list_tools()] == ["echo", "wipe"]
        assert client.legacy is True and older.sessions == 1
        assert [m["method"] for m in older.requests][:2] == ["tools/list", "initialize"]
    finally:
        await client.close()
        await older.stop()
    newer = FakeMcpHttp(8583, supported=("2027-01-01",))
    await newer.start()
    client = McpClient(HttpTransport(newer.url))
    try:
        with pytest.raises(McpError, match="2027-01-01") as caught:
            await client.list_tools()
        assert caught.value.code == PROTOCOL
    finally:
        await client.close()
        await newer.stop()


async def test_a_modern_servers_refusal_is_reported_not_taken_for_a_legacy_server() -> None:
    """A 400 with a modern error in it (a capability this client lacks) is the
    answer; only a 400 with no such error means the handshake era."""
    server = FakeMcpHttp(8585, reject=-32021)
    await server.start()
    client = McpClient(HttpTransport(server.url))
    try:
        with pytest.raises(McpError) as caught:
            await client.list_tools()
        assert caught.value.rpc_code == -32021 and caught.value.code == REFUSED
        assert client.legacy is False and [m["method"] for m in server.requests] == ["tools/list"]
    finally:
        await client.close()
        await server.stop()


async def test_http_reads_a_one_message_event_stream_and_refuses_a_refusal() -> None:
    server = FakeMcpHttp(8584, sse=True, token="s3cret")
    await server.start()
    try:
        client = McpClient(HttpTransport(server.url, {"Authorization": "Bearer s3cret"}))
        assert [t.name for t in await client.list_tools()] == ["echo", "wipe"]
        await client.close()
        stranger = McpClient(HttpTransport(server.url))
        with pytest.raises(McpError) as caught:
            await stranger.list_tools()
        assert caught.value.code == REFUSED
        await stranger.close()
        nobody = McpClient(HttpTransport("http://127.0.0.1:8599/mcp"))
        with pytest.raises(McpError) as caught:
            await nobody.list_tools()
        assert caught.value.code == UNREACHABLE
        await nobody.close()
    finally:
        await server.stop()


async def test_stdio_probes_the_servers_era_and_runs_it_with_only_the_declared_environment() -> None:
    client = McpClient(StdioTransport(sys.executable, [STDIO_SERVER], env={"FAKE_VERSION": "7"}))
    try:
        tools = await client.list_tools()
        assert [t.name for t in tools] == ["echo", "wipe"]
        assert client.legacy is False
        assert client.server_info == {"name": "fake-stdio", "version": "7"}  # the env reached the child
        assert (await client.call_tool("echo", {"text": "yo"})).text == "echo: yo"
        assert (await client.call_tool("wipe", {})).structured == {"wiped": True}
    finally:
        await client.close()
    legacy = McpClient(StdioTransport(sys.executable, [STDIO_SERVER], env={"FAKE_ERA": "legacy"}))
    try:
        assert [t.name for t in await legacy.list_tools()] == ["echo", "wipe"]
        assert legacy.legacy is True and legacy.version == LEGACY_VERSION
        assert (await legacy.call_tool("echo", {"text": "old"})).text == "echo: old"
    finally:
        await legacy.close()


async def test_a_stdio_server_that_cannot_start_or_died_is_said_so() -> None:
    client = McpClient(StdioTransport("/no/such/binary"))
    with pytest.raises(McpError) as caught:
        await client.list_tools()
    assert caught.value.code == UNREACHABLE
    await client.close()

    transport = StdioTransport(sys.executable, [STDIO_SERVER], timeout_s=1.0)
    client = McpClient(transport)
    try:
        await client.list_tools()
        process = transport._process
        assert process is not None
        process.kill()
        await process.wait()
        await asyncio.sleep(0)
        # The next request starts a new child rather than waiting on the dead one.
        assert (await client.call_tool("echo", {"text": "again"})).text == "echo: again"
        assert transport._process is not process
    finally:
        await client.close()
