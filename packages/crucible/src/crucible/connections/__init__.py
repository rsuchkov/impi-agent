"""People's own accounts at a provider — an OAuth one, or a system that takes
a credential the person enters — connected once and used by tools acting as
them, the token never reaching the model.

``Connections`` is the service; the stores are ``crucible.store`` slices,
sealing at rest is ``connections.sealing`` (``crucible[connections]``), and
what a tool sees of it all is ``crucible.ports.connections``. See
``service.py`` for how a connect, a grant and a refresh go.
"""

from crucible.connections.audit import AUDIT_LOGGER, LoggingAudit
from crucible.connections.errors import (
    ACCOUNT_REFUSED,
    CREDENTIAL_REJECTED,
    LINK_DENIED,
    LINK_EXPIRED,
    LINK_FOREIGN,
    LINK_FORGED,
    LINK_REJECTED,
    LinkRefused,
    ProviderRejected,
)
from crucible.connections.ports import (
    AccountPolicy,
    ConnectionsAudit,
    CredentialProvider,
    OAuthProvider,
    Sealer,
)
from crucible.connections.service import Connections
from crucible.connections.types import (
    Connected,
    CredentialForm,
    Disconnection,
    OAuthTokens,
)
from crucible.ports.connections import (
    AccessToken,
    ConnectionsError,
    NotConnected,
    ProviderAccount,
    ProviderUnavailable,
    TokenGrant,
)

__all__ = [
    "ACCOUNT_REFUSED",
    "AUDIT_LOGGER",
    "AccessToken",
    "AccountPolicy",
    "CREDENTIAL_REJECTED",
    "Connected",
    "Connections",
    "ConnectionsAudit",
    "ConnectionsError",
    "CredentialForm",
    "CredentialProvider",
    "Disconnection",
    "LINK_DENIED",
    "LINK_EXPIRED",
    "LINK_FOREIGN",
    "LINK_FORGED",
    "LINK_REJECTED",
    "LinkRefused",
    "LoggingAudit",
    "NotConnected",
    "OAuthProvider",
    "OAuthTokens",
    "ProviderAccount",
    "ProviderRejected",
    "ProviderUnavailable",
    "Sealer",
    "TokenGrant",
]
