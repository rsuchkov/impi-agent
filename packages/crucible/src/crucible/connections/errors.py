"""What can go wrong with a connected account, by kind — the kind decides what
the person is told: connect again, try later, or nothing they can do. The
errors a tool meets through the port (``grant``) are the port's own and live
with it; they are named here too, for the machinery that raises them."""

from crucible.ports.connections import (
    ConnectionsError,
    NotConnected,
    ProviderUnavailable,
)

# Why a connect that came back was refused.
LINK_EXPIRED = "expired"  # unknown, already used, or past its time
LINK_FOREIGN = "foreign"  # started by somebody else
LINK_DENIED = "denied"  # the person declined at the provider
LINK_REJECTED = "rejected"  # the provider would not exchange the code
ACCOUNT_REFUSED = "account_refused"  # the application's rule refused that account


class LinkRefused(ConnectionsError):
    """A connect that came back and was refused, with why (``LINK_*``,
    ``ACCOUNT_REFUSED``) and, when known, the provider account it named."""

    def __init__(self, reason: str, *, provider: str = "", account: str = "", detail: str = "") -> None:
        super().__init__(f"connect refused: {reason}" + (f" ({detail})" if detail else ""))
        self.reason = reason
        self.provider = provider
        self.account = account
        self.detail = detail


class ProviderRejected(ConnectionsError):
    """The provider refused a code or a refresh token: the grant is over."""


__all__ = [
    "ACCOUNT_REFUSED", "LINK_DENIED", "LINK_EXPIRED", "LINK_FOREIGN", "LINK_REJECTED",
    "ConnectionsError", "LinkRefused", "NotConnected", "ProviderRejected", "ProviderUnavailable",
]
