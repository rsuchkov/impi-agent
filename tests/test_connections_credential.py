"""The HTTP credential provider against a scripted system."""

import base64

import httpx
import pytest

from crucible.connections import (
    CredentialProvider,
    ProviderRejected,
    ProviderUnavailable,
)
from crucible.connections.credential import (
    AUTH_BASIC,
    CredentialConfig,
    HttpCredentialProvider,
    credential_header,
)


def _provider(handler, **over) -> HttpCredentialProvider:
    config = CredentialConfig(whoami_url="https://logs.example/api/users/me", hint="Profile → Tokens", **over)
    return HttpCredentialProvider(config, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_a_token_is_asked_about_as_a_bearer_and_names_the_account() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": 7, "username": "alice", "full_name": "Alice", "email": "a@example.com", "roles": ["Reader"]})

    provider = _provider(handler, account_fields={"id": "id", "username": "username", "name": "full_name", "email": "email"}, account_extra=("roles",))
    assert isinstance(provider, CredentialProvider) and provider.hint == "Profile → Tokens"
    account = await provider.validate("tok-1")
    assert (account.id, account.username, account.name, account.email, account.extra) == ("7", "alice", "Alice", "a@example.com", {"roles": ["Reader"]})
    assert seen[0].headers["Authorization"] == "Bearer tok-1" and str(seen[0].url) == "https://logs.example/api/users/me"


async def test_a_system_that_takes_the_token_as_basic_auth_gets_it_so() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "u1", "username": "alice"})

    await _provider(handler, auth=AUTH_BASIC).validate("tok-1")
    assert seen[0].headers["Authorization"] == "Basic " + base64.b64encode(b"tok-1:token").decode()
    assert credential_header("t", "bearer") == "Bearer t"


async def test_a_refused_token_is_rejected_and_a_broken_system_is_unavailable() -> None:
    with pytest.raises(ProviderRejected):
        await _provider(lambda r: httpx.Response(401)).validate("bad")
    with pytest.raises(ProviderRejected):
        await _provider(lambda r: httpx.Response(403)).validate("bad")
    with pytest.raises(ProviderUnavailable):
        await _provider(lambda r: httpx.Response(500)).validate("tok")
    with pytest.raises(ProviderUnavailable):
        await _provider(lambda r: httpx.Response(200, text="not json")).validate("tok")

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(ProviderUnavailable):
        await _provider(down).validate("tok")
