"""The vocabulary of connected accounts the machinery speaks: what a provider
answers with, a connect that finished, a connection ended.

Who someone is at a provider (``ProviderAccount``) and what a tool is handed
(``TokenGrant``, ``AccessToken``) are the tool layer's vocabulary too, and live
in ``crucible.ports.connections``.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from crucible.ports.connections import ProviderAccount


@dataclass(frozen=True)
class OAuthTokens:
    """What a provider's token endpoint answered. ``refresh_token`` "" = it
    issued none (a refresh then keeps the one it had); ``expires_in`` None =
    the access token does not expire."""

    access_token: str = field(repr=False)
    refresh_token: str = field(default="", repr=False)
    expires_in: int | None = None
    scope: str = ""

    def expires_at(self, now: datetime) -> datetime | None:
        return now + timedelta(seconds=self.expires_in) if self.expires_in is not None else None


@dataclass(frozen=True)
class Connected:
    """A connect that finished: whose, where, as whom — and the ``return_to``
    the application gave ``begin``. ``started_by`` is whom ``begin`` started it
    for, which ``owner`` differs from when the application's ``ConnectionOwner``
    named somebody else once the provider had named the account."""

    owner: str
    provider: str
    account: ProviderAccount
    return_to: str = ""
    started_by: str = ""


@dataclass(frozen=True)
class Disconnection:
    """A connection ended. ``revoked`` False = the provider did not confirm the
    revoke, and the person should revoke the application there themselves."""

    account: ProviderAccount
    revoked: bool
