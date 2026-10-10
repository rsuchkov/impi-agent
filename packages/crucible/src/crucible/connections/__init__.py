"""People's own accounts at OAuth providers, connected once and used by tools
acting as them — the token never reaching the model.

``OAuthConnections`` is the service; the stores are ``crucible.store`` slices,
sealing at rest is ``connections.sealing`` (``crucible[connections]``), and
what a tool sees of it all is ``crucible.ports.connections``. See
``service.py`` for how a connect, a grant and a refresh go.
"""

from crucible.connections.audit import AUDIT_LOGGER, LoggingAudit
from crucible.connections.errors import (
    ACCOUNT_REFUSED,
    LINK_DENIED,
    LINK_EXPIRED,
    LINK_FOREIGN,
    LINK_REJECTED,
    ConnectionsError,
    LinkRefused,
    NotConnected,
    ProviderRejected,
    ProviderUnavailable,
)
from crucible.connections.ports import (
    AccountPolicy,
    ConnectionsAudit,
    OAuthProvider,
    Sealer,
)
from crucible.connections.service import OAuthConnections
from crucible.connections.types import Connected, Disconnection, OAuthTokens
from crucible.ports.connections import AccessToken, ProviderAccount, TokenGrant

__all__ = [
    "ACCOUNT_REFUSED",
    "AUDIT_LOGGER",
    "LINK_DENIED",
    "LINK_EXPIRED",
    "LINK_FOREIGN",
    "LINK_REJECTED",
    "AccessToken",
    "AccountPolicy",
    "Connected",
    "ConnectionsAudit",
    "ConnectionsError",
    "Disconnection",
    "LinkRefused",
    "LoggingAudit",
    "NotConnected",
    "OAuthConnections",
    "OAuthProvider",
    "OAuthTokens",
    "ProviderAccount",
    "ProviderRejected",
    "ProviderUnavailable",
    "TokenGrant",
    "Sealer",
]
