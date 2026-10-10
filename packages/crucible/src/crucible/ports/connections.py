"""What a tool sees of a person's connected accounts: the account, a token to
call with, and the port that hands them out — nothing about whose they are or
where they came from.

The vocabulary and the port only, so the tool layer depends on this and not on
the machinery behind it (``crucible.connections``, which needs the store).
Values a tool must never repeat — an access token — travel as ``AccessToken``,
which shows nothing of itself in a log line, a traceback or a pickle.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import NoReturn, Protocol

from crucible.ports.turn import TurnScope


class AccessToken:
    """A provider's access token, for one call. ``reveal()`` is the only way to
    the value: ``repr``, ``str`` and pickling give nothing away."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __bool__(self) -> bool:
        return bool(self._value)

    def __repr__(self) -> str:
        return "AccessToken(***)"

    __str__ = __repr__

    def __reduce__(self) -> NoReturn:
        raise TypeError("an access token cannot be pickled")


@dataclass(frozen=True)
class ProviderAccount:
    """Who the person is at the provider."""

    id: str
    username: str
    name: str = ""
    email: str = ""


@dataclass(frozen=True)
class TokenGrant:
    """What a tool reads with: the token, whose it is, and which credential it
    was — ``forget`` takes it back to end exactly that one after a 401."""

    provider: str
    token: AccessToken
    account: ProviderAccount
    connect_id: str
    version: int


class ConnectionAccess(Protocol):
    """One person's connected accounts. ``grant`` raises ``NotConnected`` when
    there is no usable connection to that provider — the tool tells the model,
    and the model tells the person how to connect; it never sees a token or a
    connect link."""

    @property
    def providers(self) -> tuple[str, ...]:
        """The providers the person could connect to, connected or not."""
        ...

    async def grant(self, provider: str) -> TokenGrant: ...

    async def forget(self, grant: TokenGrant) -> None:
        """The provider answered a call made with ``grant`` with 401."""
        ...

    async def account(self, provider: str) -> ProviderAccount | None: ...


# (agent, the user the turn acts for, the turn's scope) -> that person's
# connections, or None when the turn acts for nobody the application can name.
ConnectionAccessFactory = Callable[[str, str, TurnScope | None], ConnectionAccess | None]
