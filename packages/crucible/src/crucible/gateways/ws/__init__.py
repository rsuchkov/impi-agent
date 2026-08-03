"""ws adapter — a duplex WebSocket gateway for custom client services.

A service (the user's own program) dials the engine's WsHub, authenticates
with its service token, and exchanges JSON frames: ``message`` in (addressed
to any allowed agent, keyed by the service's own conversation id), ``reply``/
``notice`` out, ``agents`` for discovery. See docs/ws-gateway.md for the
protocol.

events.py  — pure frame normalization + conversation namespacing
hub.py     — the aiohttp WebSocket server shared by all services/agents
client.py  — ChatClient over the hub (one per agent)
gateway.py — per-agent lifecycle shim (identity + park-until-stop)
"""

from crucible.gateways.ws.client import WsChatClient
from crucible.gateways.ws.gateway import WsGateway
from crucible.gateways.ws.hub import WsHub

# No formatting hint: frames carry Markdown as-is; rendering is the client
# service's concern.
PROMPT_HINT = ""

__all__ = ["PROMPT_HINT", "WsChatClient", "WsGateway", "WsHub"]
