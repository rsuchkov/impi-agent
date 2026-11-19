"""What the MCP gateway's authorization server issues (``gateways.mcp``):
an authorization a client asked for and a person signed in and consented to,
and the grant — the tokens — it came to. Vocabulary over nothing: the store
keeps the records, the server reads and writes them."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class McpAuthRequest:
    """One authorization in progress at the MCP gateway (``gateways.mcp``):
    what the client asked for at ``/authorize``, who signed in for it once they
    did, and the code it was answered with once they consented. Short-lived
    and single use: the code is taken with the request. ``code_hash`` is a
    digest of the code, never the code — a reader of the store cannot finish
    an authorization; ``secret`` is the consent form's own, handed back by the
    form that approves; ``browser_hash`` is a digest of the cookie the browser
    that signed in was given, which the consent page asks for."""

    id: str
    client: str
    redirect_uri: str
    code_challenge: str
    scope: str
    resource: str
    state: str
    expires_at: str
    secret: str
    subject: str = ""  # who signed in; "" until they did
    username: str = ""
    code_hash: str = ""  # "" until they consented
    code_expires_at: str = ""
    browser_hash: str = ""  # "" until they signed in


@dataclass(frozen=True)
class McpGrant:
    """A token the MCP gateway's authorization server issued to a client for a
    person: whose, which client, what scopes, when the sign-in was and until
    when it stands. The tokens themselves are kept as digests: the access
    token's, the refresh token's and the refresh token's before it (a client
    that presents the previous one again has been copied, and the grant ends).
    One grant per sign-in; a refresh rotates both tokens in place."""

    id: str
    subject: str
    username: str
    client: str
    scope: str
    signed_in_at: str
    expires_at: str
    access_hash: str
    access_expires_at: str
    refresh_hash: str
    previous_refresh_hash: str = ""


class McpGrants(Protocol):
    """Authorizations in progress and the grants they came to, for the MCP
    gateway's authorization server."""

    async def put_mcp_request(self, record: McpAuthRequest) -> None:
        """Insert or replace by ``id``."""
        ...

    async def get_mcp_request(self, request_id: str) -> McpAuthRequest | None: ...

    async def take_mcp_request_by_code(self, code_hash: str) -> McpAuthRequest | None:
        """The request a code was issued for, removed in the same step: a
        second take finds nothing."""
        ...

    async def delete_mcp_request(self, request_id: str) -> None: ...

    async def save_mcp_grant(self, record: McpGrant) -> None:
        """Insert or replace by ``id``."""
        ...

    async def replace_mcp_grant(self, record: McpGrant, *, expected_refresh_hash: str) -> bool:
        """Write ``record`` over the grant with its ``id`` — only while that
        grant's refresh token is still ``expected_refresh_hash``. Of two
        refreshes with one token exactly one succeeds."""
        ...

    async def get_mcp_grant_by_access(self, access_hash: str) -> McpGrant | None: ...

    async def get_mcp_grant_by_refresh(self, refresh_hash: str) -> McpGrant | None:
        """The grant whose current or previous refresh token this is."""
        ...

    async def delete_mcp_grant(self, grant_id: str) -> None: ...

    async def purge_mcp_auth(self, now: str) -> int:
        """Forget requests and grants past their time; how many went."""
        ...
