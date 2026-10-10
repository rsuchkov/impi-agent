"""The vocabulary of connected accounts: who someone is at a provider, what a
provider answers with, and what a tool is handed.

Values a tool must never repeat — an access token — travel as ``AccessToken``,
which shows nothing of itself in a log line, a traceback or a pickle.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import NoReturn


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
class Grant:
    """What a tool reads with: the token, whose it is, and which credential it
    was — ``forget`` takes it back to end exactly that one after a 401."""

    provider: str
    token: AccessToken
    account: ProviderAccount
    connect_id: str
    version: int


@dataclass(frozen=True)
class Connected:
    """A connect that finished: whose, where, as whom."""

    owner: str
    provider: str
    account: ProviderAccount


@dataclass(frozen=True)
class Disconnection:
    """A connection ended. ``revoked`` False = the provider did not confirm the
    revoke, and the person should revoke the application there themselves."""

    account: ProviderAccount
    revoked: bool
