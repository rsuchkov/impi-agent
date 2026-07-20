"""Mattermost adapter — the ONLY layer that knows the Mattermost API.

events.py    — pure WS-frame normalization into neutral chat types
options.py   — MATTERMOST_URL -> driver options dict
client.py    — ChatClient implementation (posts, reactions, chunking)
gateway.py   — WS loop + respond decision, dispatches into a Flow
callbacks.py — MM interactive-callback wire-shape <-> neutral callbacks
"""

from crucible.gateways.mattermost.callbacks import MattermostCallbackCodec
from crucible.gateways.mattermost.client import MattermostChatClient
from crucible.gateways.mattermost.gateway import MattermostGateway
from crucible.gateways.mattermost.options import driver_options

__all__ = [
    "MattermostCallbackCodec",
    "MattermostChatClient",
    "MattermostGateway",
    "driver_options",
]
