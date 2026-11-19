"""Who holds the bearer token on a request to the MCP gateway.

Every request to ``/mcp`` carries a token an authorization server issued to a
client program after a person signed in and consented to it; ``Authorization``
turns the token into a ``Principal`` — the person (``subject``), the program
(``client``) and what the person let it do (``scopes``). The gateway answers a
request with no principal 401, pointing the client at the protected-resource
metadata, as the OAuth profile of the protocol wants; a tool the scopes do not
reach is 403. The engine's own authorization server (``gateways.mcp.server``)
implements this port; a test or a deployment with an authorization server of
its own implements it otherwise.
"""

from dataclasses import dataclass
from typing import Protocol

from aiohttp import web

# What a token lets the client do. ``read``: tools that change nothing and the
# agent's answer; ``act``: tools that need the person's yes too — the gateway's
# ``act`` tools, and inside a turn the agent's own tools that ask for
# confirmation, the card going to the person where the application puts it.
SCOPE_READ = "read"
SCOPE_ACT = "act"
SCOPES = (SCOPE_READ, SCOPE_ACT)


@dataclass(frozen=True)
class Principal:
    """One authenticated request. ``subject`` is the person the token was
    issued for, as the application names people (the same key its other
    gateways use for them, so a card the application puts in front of them
    elsewhere is answered by the same person); ``client`` is the program that
    holds the token; ``scopes`` are what the person let it do (``SCOPES``);
    ``username`` is the person's name for the prompt (``subject`` when
    empty)."""

    subject: str
    client: str
    scopes: frozenset[str] = frozenset({SCOPE_READ})
    username: str = ""

    def allows(self, scope: str) -> bool:
        return scope in self.scopes


class Authorization(Protocol):
    async def authenticate(self, request: web.Request) -> Principal | None:
        """The principal behind this request's bearer token, or None for 401 —
        no token, an unknown one, one expired or revoked."""
        ...
