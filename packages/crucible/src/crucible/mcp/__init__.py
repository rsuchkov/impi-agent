"""MCP servers as a source of tools for the engine (``docs/mcp.md``).

library.py  — the servers a deployment declares (``_mcp/<name>.yaml``) and their snapshots
protocol.py — the least of MCP: tools/list and tools/call over HTTP or stdio, in both eras of the protocol
snapshot.py — a server's tool definitions, pinned and digested
tools.py    — a server's tool as a tool of the engine (``mcp__<server>__<tool>``)
service.py  — the servers connected, checked against their snapshots, called
"""

from crucible.mcp.library import (
    MCP_DIR,
    TRUST_LEVELS,
    TRUST_TRUSTED,
    TRUST_UNTRUSTED,
    VALUES_FAMILY,
    McpConfigError,
    McpLibrary,
    McpServer,
    server_from_yaml,
    with_values,
)
from crucible.mcp.protocol import CallResult, McpClient, McpError, ToolDefinition
from crucible.mcp.service import McpServers, StartReport
from crucible.mcp.snapshot import Snapshot
from crucible.mcp.tools import McpTool, mcp_tool_name, split_tool_name

__all__ = [
    "MCP_DIR",
    "TRUST_LEVELS",
    "TRUST_TRUSTED",
    "TRUST_UNTRUSTED",
    "CallResult",
    "McpClient",
    "McpConfigError",
    "McpError",
    "McpLibrary",
    "McpServer",
    "McpServers",
    "McpTool",
    "Snapshot",
    "StartReport",
    "ToolDefinition",
    "VALUES_FAMILY",
    "mcp_tool_name",
    "server_from_yaml",
    "split_tool_name",
    "with_values",
]
