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
from crucible.gateways.leased import LEASE_RENEW_S, LEASE_TTL_S, LeasedGateway

__all__ = [
    "LEASE_RENEW_S",
    "LEASE_TTL_S",
    "GatewayConfig",
    "GatewayFactory",
    "GatewayHandle",
    "LeasedGateway",
    "needs_http_receiver",
]
