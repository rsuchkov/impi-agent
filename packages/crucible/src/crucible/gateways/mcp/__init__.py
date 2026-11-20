"""mcp adapter — the engine as an MCP server, for an agent on the outside.

An MCP client with a token a person signed in for lists the gateway's tools
and calls them; ``ask_agent`` runs a turn of the agent's as that person, in a
conversation the client names by a handle. See docs/mcp-gateway.md.

auth.py    — who holds the token: the Authorization port and McpCaller (the scopes are
             ``ports.chat.gateway.MCP_SCOPES``; the engine's authorization server is
             ``crucible.mcp_authorization``, over this package and the connections)
tools.py   — the McpGatewayTool port, GatewayCall, TurnState, the bundled AskAgentTool and WaitAgentTool
client.py  — ChatClient over the journal, cards forwarded to the application (ApprovalCards)
hub.py     — the aiohttp server shared by every agent on the gateway (McpHub)
gateway.py — per-agent lifecycle shim (identity + park-until-stop)
"""

from crucible.gateways.mcp.auth import Authorization, McpCaller
from crucible.gateways.mcp.client import ApprovalCards, McpChatClient
from crucible.gateways.mcp.gateway import McpGateway
from crucible.gateways.mcp.hub import (
    MCP_PATH,
    RESOURCE_METADATA_PATH,
    TASKS_EXTENSION,
    McpHub,
    conversation_key,
)
from crucible.gateways.mcp.tools import (
    CONVERSATION_KEY,
    META_CONVERSATION,
    AskAgentTool,
    CallFailed,
    GatewayCall,
    McpGatewayTool,
    TurnState,
    WaitAgentTool,
)

# No formatting hint: replies carry Markdown as-is; the client's model reads it.
PROMPT_HINT = ""

__all__ = [
    "CONVERSATION_KEY",
    "MCP_PATH",
    "META_CONVERSATION",
    "PROMPT_HINT",
    "RESOURCE_METADATA_PATH",
    "TASKS_EXTENSION",
    "ApprovalCards",
    "AskAgentTool",
    "Authorization",
    "CallFailed",
    "GatewayCall",
    "McpChatClient",
    "McpGateway",
    "McpGatewayTool",
    "McpHub",
    "McpCaller",
    "TurnState",
    "WaitAgentTool",
    "conversation_key",
]
