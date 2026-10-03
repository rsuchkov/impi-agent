"""Who is calling the HTTP gateway, as the application decides it.

The hub knows how to run turns and serve their journals; it does not know what
a caller is — a service token and a user header, a session cookie checked
against an upstream, a signed assertion. ``CallerAuthenticator`` is where the
application says so, per request. ``TokenCallers`` is the engine's own: the
same shape as the ws gateway's services, a bearer token per calling program
and the person's identity in headers that program is trusted to fill.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from aiohttp import web

from crucible.ports.turn import TurnScope

USER_HEADER = "X-User-Id"
USERNAME_HEADER = "X-Username"


@dataclass(frozen=True)
class Caller:
    """One authenticated request. ``realm`` namespaces everything the caller
    names — conversations, message ids — so two calling programs can never
    reach each other's turns however they number things. ``agents`` is the
    allowlist of agents this caller may address (None = all on this gateway).
    ``turn`` is what belongs to this request's turn alone (a credential sent
    with it), for the tools the turn calls; the application builds it."""

    realm: str
    user_id: str
    username: str = ""
    agents: tuple[str, ...] | None = None
    turn: TurnScope | None = None

    @property
    def owner(self) -> str:
        """Who may read the turns this caller starts."""
        return f"{self.realm}/{self.user_id}"


class CallerAuthenticator(Protocol):
    async def authenticate(self, request: web.Request, body: dict[str, Any] | None) -> Caller | None:
        """The caller behind this request, or None for 401. ``body`` is the
        parsed JSON of a POST (None on GET), so a credential travelling in it
        can become part of the turn's scope."""
        ...


class TokenCallers:
    """Bearer token → calling program; the person from the program's headers.

    ``callers`` maps a name to (token, agent allowlist or None). The program
    is trusted to say who its user is — the same trust the ws gateway places in
    a service, and the right one when the program is the application's own
    frontend. A deployment where the caller must prove the user's identity
    itself (a session cookie, a signed token) writes its own authenticator."""

    def __init__(self, callers: Mapping[str, tuple[str, tuple[str, ...] | None]]) -> None:
        self._by_token = {token: (name, allow) for name, (token, allow) in callers.items() if token}

    async def authenticate(self, request: web.Request, body: dict[str, Any] | None) -> Caller | None:
        token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        entry = self._by_token.get(token)
        if entry is None:
            return None
        realm, allow = entry
        user_id = request.headers.get(USER_HEADER, "").strip() or "user"
        username = request.headers.get(USERNAME_HEADER, "").strip() or user_id
        return Caller(realm=realm, user_id=user_id, username=username, agents=allow)
