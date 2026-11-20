"""Who holds the bearer token on a request to the MCP gateway.

Every request to ``/mcp`` carries a token an authorization server issued to a
client program after a person signed in and consented to it; ``Authorization``
turns the token into a ``McpCaller`` — the person (``subject``), the program
(``client``) and what the person let it do (``scopes``). The gateway answers a
request with no caller 401, pointing the client at the protected-resource
metadata, as the OAuth profile of the protocol wants; a tool the scopes do not
reach is 403. The engine's own authorization server (``crucible.mcp_authorization``)
implements this port; a test or a deployment with an authorization server of
its own implements it otherwise.
"""

from dataclasses import dataclass
from typing import Protocol

from aiohttp import web

from crucible.ports.chat.gateway import MCP_SCOPE_READ


@dataclass(frozen=True)
class McpCaller:
    """One authenticated request, as the http gateway's ``Caller`` is: there
    the program is trusted to say who its user is, here the token says it.
    ``subject`` is the person the token was
    issued for, as the application names people (the same key its other
    gateways use for them, so a card the application puts in front of them
    elsewhere is answered by the same person); ``client`` is the program that
    holds the token — its id, a URL for a client that publishes its metadata
    — and ``client_name`` the operator's name for it, for a person's eyes
    ("" when it has none); ``scopes`` are what the person let it do
    (``ports.chat.gateway.MCP_SCOPES``); ``username`` is the person's name
    for the prompt (``subject`` when empty)."""

    subject: str
    client: str
    scopes: frozenset[str] = frozenset({MCP_SCOPE_READ})
    username: str = ""
    client_name: str = ""

    @property
    def client_words(self) -> str:
        """The client as a person is told of it: its name, else its id."""
        return self.client_name or self.client

    def allows(self, scope: str) -> bool:
        return scope in self.scopes


class Authorization(Protocol):
    async def authenticate(self, request: web.Request) -> McpCaller | None:
        """The caller behind this request's bearer token, or None for 401 —
        no token, an unknown one, one expired or revoked."""
        ...
