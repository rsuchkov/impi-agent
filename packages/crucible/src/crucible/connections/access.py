"""What a tool sees of connected accounts: those of the person its turn acts
for, and nothing about whose they are or where they came from.

The port only — the tool layer imports it and nothing behind it. The service
side (``OAuthConnections.for_owner``, ``connection_access``) implements it.
"""

from collections.abc import Callable
from typing import Protocol

from crucible.connections.types import Grant, ProviderAccount
from crucible.ports.turn import TurnScope


class ConnectionAccess(Protocol):
    """One person's connected accounts. ``grant`` raises ``NotConnected`` when
    there is no usable connection to that provider — the tool tells the model,
    and the model tells the person how to connect; it never sees a token or a
    connect link."""

    @property
    def providers(self) -> tuple[str, ...]:
        """The providers the person could connect to, connected or not."""
        ...

    async def grant(self, provider: str) -> Grant: ...

    async def forget(self, grant: Grant) -> None:
        """The provider answered a call made with ``grant`` with 401."""
        ...

    async def account(self, provider: str) -> ProviderAccount | None: ...


# (agent, the user the turn acts for, the turn's scope) -> that person's
# connections, or None when the turn acts for nobody the application can name.
ConnectionAccessFactory = Callable[[str, str, TurnScope | None], ConnectionAccess | None]
