"""Interactivity: widgets/forms and the callback machinery behind them.

A click on an agent's button is delivered by the gateway (an HTTP callback for
Mattermost, the socket for Slack), normalized by that gateway's ``CallbackCodec``,
and handed to the transport-neutral ``InteractionDispatcher``, which either
resolves a blocking mid-turn request or feeds the choice back into the right
agent's conversation as a new message. ``InteractionsServer`` is the HTTP-callback
receiver for gateways that deliver callbacks over HTTP.
"""

from crucible.interactions.dispatcher import (
    ActionResult,
    AgentSink,
    FormOpen,
    InteractionDispatcher,
)
from crucible.interactions.presence import AgentPresence, MappingPresence
from crucible.interactions.server import InteractionsServer
from crucible.interactions.wiring import InteractionWiring

__all__ = [
    "ActionResult",
    "AgentPresence",
    "AgentSink",
    "FormOpen",
    "InteractionDispatcher",
    "InteractionsServer",
    "InteractionWiring",
    "MappingPresence",
]
