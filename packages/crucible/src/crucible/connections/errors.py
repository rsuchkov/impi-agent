"""What can go wrong with a connected account, by kind — the kind decides what
the person is told: connect again, try later, or nothing they can do."""

# Why a connect that came back was refused.
LINK_EXPIRED = "expired"  # unknown, already used, or past its time
LINK_FOREIGN = "foreign"  # started by somebody else
LINK_DENIED = "denied"  # the person declined at the provider
LINK_REJECTED = "rejected"  # the provider would not exchange the code
ACCOUNT_REFUSED = "account_refused"  # the application's rule refused that account


class ConnectionsError(Exception):
    """Base for every failure of a connected account."""


class NotConnected(ConnectionsError):
    """The person has no usable connection to the provider — never had one, or
    it ended (disconnected, revoked at the provider, unreadable with the key)."""

    def __init__(self, owner: str, provider: str) -> None:
        super().__init__(f"{owner} is not connected to {provider}")
        self.owner = owner
        self.provider = provider


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


class ProviderUnavailable(ConnectionsError):
    """The provider could not be asked, or did not answer as it should: try later."""
