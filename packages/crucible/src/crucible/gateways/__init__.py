"""Gateway adapters. Mattermost (mattermostautodriver) first; others may follow.

Each gateway normalizes its platform events into neutral message/action types and
routes them to the agent runtime. ``GatewayFactory`` builds an agent's client +
gateway from a neutral ``GatewayConfig``; an application resolves that config from
its own settings.
"""

from crucible.gateways.factory import (
    GatewayConfig,
    GatewayFactory,
    GatewayHandle,
    needs_http_receiver,
)

__all__ = ["GatewayConfig", "GatewayFactory", "GatewayHandle", "needs_http_receiver"]
