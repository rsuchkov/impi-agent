"""What the connections service needs from outside it.

The stores are ``crucible.store`` slices (``ConnectionStore``,
``ConnectionLinkStore``). The rest is here: the provider, the sealing of what
is stored, the application's rule for which account may be bound to whom, and
where the audit trail goes.
"""

from collections.abc import Mapping
from typing import Protocol

from crucible.connections.types import OAuthTokens
from crucible.ports.connections import ProviderAccount


class OAuthProvider(Protocol):
    """One OAuth 2.0 provider, configured for this application (its client,
    its redirect, its scope). A code or refresh token the provider refuses
    raises ``ProviderRejected``; anything else that goes wrong raises
    ``ProviderUnavailable``."""

    def authorize_url(self, *, state: str, code_challenge: str) -> str:
        """Where to send the person: the provider's consent page for this
        client, with the state and the PKCE challenge (S256)."""
        ...

    async def exchange(self, code: str, code_verifier: str) -> OAuthTokens: ...

    async def refresh(self, refresh_token: str) -> OAuthTokens: ...

    async def revoke(self, token: str) -> None:
        """End the grant the token belongs to. Raises unless the provider
        confirmed it."""
        ...

    async def identify(self, access_token: str) -> ProviderAccount:
        """Who the token belongs to."""
        ...


class Sealer(Protocol):
    """Seals what is kept at rest. ``open`` answers None for a value it cannot
    open — sealed under a key it no longer has, or not sealed at all."""

    def seal(self, value: str) -> str: ...

    def open(self, sealed: str) -> str | None: ...


class AccountPolicy(Protocol):
    """The application's rule for which provider account may be bound to whom
    — the same email, a membership. None allows it; a reason refuses it, and
    the tokens just issued are revoked. ``facts`` is what the application
    knew about the person when it finished the connect (the email of the
    session that brought the code, say) — plain strings, never a secret —
    handed through ``finish(facts=…)`` so the policy need not reach for it
    out of band."""

    async def refusal(
        self, owner: str, provider: str, account: ProviderAccount, facts: Mapping[str, str]
    ) -> str | None: ...


class ConnectionOwner(Protocol):
    """The application's word on whom a connection belongs to, once the
    provider has named the account — for an application that keys a person by
    that account rather than by the chat user who started the connect (one
    person, several chats, one connection). ``started_by`` is whom ``begin``
    started it for; the answer is the owner the connection is saved under —
    ``started_by`` itself to keep it. ``facts`` as for ``AccountPolicy``."""

    async def owner_of(
        self, started_by: str, provider: str, account: ProviderAccount, facts: Mapping[str, str]
    ) -> str: ...


class ConnectionsAudit(Protocol):
    """Where each event of a connection is recorded: one call per event, with
    an outcome and plain fields — never a token, a code or a state."""

    def record(self, event: str, result: str, **fields: str) -> None: ...
