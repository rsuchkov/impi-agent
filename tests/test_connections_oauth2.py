"""The OAuth 2.0 provider against a scripted HTTP server, and how a tool is
handed the connections of the person its turn acts for."""

import base64
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from crucible.connections import (
    NotConnected,
    OAuthConnections,
    ProviderAccount,
    ProviderRejected,
    ProviderUnavailable,
)
from crucible.connections.memory import MemoryConnectionStore, MemoryLinkStore
from crucible.connections.oauth2 import OAuth2Config, OAuth2Provider, gitlab
from crucible.connections.sealing import FernetSealer, new_sealing_key
from crucible.connections.service import connection_access
from crucible.ports.turn import TurnScope


def _gitlab(handler) -> OAuth2Provider:
    config = gitlab("https://gitlab.example/", client_id="cid", client_secret="csecret",
                    redirect_uri="https://engine.example/oauth/callback")
    return OAuth2Provider(config, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def _form(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


def test_the_consent_url_carries_the_client_the_state_and_the_challenge() -> None:
    url = _gitlab(lambda r: httpx.Response(500)).authorize_url(state="st", code_challenge="ch")
    parsed = urlparse(url)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == "https://gitlab.example/oauth/authorize"
    assert {k: v[0] for k, v in parse_qs(parsed.query).items()} == {
        "response_type": "code", "client_id": "cid", "state": "st", "code_challenge": "ch",
        "code_challenge_method": "S256", "scope": "read_api",
        "redirect_uri": "https://engine.example/oauth/callback",
    }


async def test_the_exchange_sends_the_verifier_and_the_client_in_the_form() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"access_token": "at", "refresh_token": "rt", "scope": "read_api"})

    tokens = await _gitlab(handler).exchange("the-code", "the-verifier")
    assert str(seen[0].url) == "https://gitlab.example/oauth/token"
    assert _form(seen[0]) == {
        "grant_type": "authorization_code", "code": "the-code", "code_verifier": "the-verifier",
        "redirect_uri": "https://engine.example/oauth/callback", "client_id": "cid", "client_secret": "csecret",
    }
    # GitLab's tokens live two hours when the answer does not say.
    assert (tokens.access_token, tokens.refresh_token, tokens.expires_in) == ("at", "rt", 7200)
    assert "at" not in repr(tokens) and "csecret" not in repr(_gitlab(handler)._config)


async def test_a_basic_auth_client_proves_itself_in_the_header() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"access_token": "at"})

    config = OAuth2Config(
        authorize_url="https://p/a", token_url="https://p/t", userinfo_url="https://p/u",
        client_id="cid", client_secret="csecret", redirect_uri="https://r", client_auth="basic",
    )
    tokens = await OAuth2Provider(config, http=httpx.AsyncClient(transport=httpx.MockTransport(handler))).refresh("rt")
    assert seen[0].headers["Authorization"] == "Basic " + base64.b64encode(b"cid:csecret").decode()
    assert "client_secret" not in _form(seen[0]) and _form(seen[0])["refresh_token"] == "rt"
    # No refresh token and no expiry in the answer: none issued, never expires.
    assert (tokens.refresh_token, tokens.expires_in) == ("", None)


@pytest.mark.parametrize(("status", "body", "error"), [
    (400, {"error": "invalid_grant"}, ProviderRejected),
    (401, {}, ProviderRejected),
    (503, {}, ProviderUnavailable),
    (302, {}, ProviderUnavailable),  # a redirect is not followed
    (200, {"no": "token"}, ProviderUnavailable),
])
async def test_the_token_endpoint_tells_a_refused_grant_from_an_outage(status, body, error) -> None:
    with pytest.raises(error):
        await _gitlab(lambda r: httpx.Response(status, json=body)).refresh("rt")


async def test_a_network_failure_is_an_outage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(ProviderUnavailable):
        await _gitlab(handler).exchange("c", "v")
    with pytest.raises(ProviderUnavailable):
        await _gitlab(handler).identify("at")


async def test_revoke_counts_only_a_confirmation() -> None:
    seen: list[httpx.Request] = []

    def ok(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    await _gitlab(ok).revoke("rt")
    assert str(seen[0].url) == "https://gitlab.example/oauth/revoke" and _form(seen[0])["token"] == "rt"
    with pytest.raises(ProviderUnavailable):
        await _gitlab(lambda r: httpx.Response(503)).revoke("rt")
    config = OAuth2Config(authorize_url="a", token_url="t", userinfo_url="u", client_id="c",
                          client_secret="s", redirect_uri="r")
    with pytest.raises(ProviderUnavailable, match="no revocation endpoint"):
        await OAuth2Provider(config).revoke("rt")


async def test_identify_reads_the_account_with_the_token_in_the_header_only() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": 7, "username": "alice", "name": "Alice", "email": "a@example.com"})

    account = await _gitlab(handler).identify("at")
    assert account == ProviderAccount(id="7", username="alice", name="Alice", email="a@example.com")
    assert seen[0].headers["Authorization"] == "Bearer at" and "at" not in str(seen[0].url)
    with pytest.raises(ProviderRejected):
        await _gitlab(lambda r: httpx.Response(401)).identify("at")
    with pytest.raises(ProviderUnavailable):
        await _gitlab(lambda r: httpx.Response(200, json={"username": "no id"})).identify("at")


async def test_a_provider_with_other_field_names_maps_them() -> None:
    config = OAuth2Config(
        authorize_url="a", token_url="t", userinfo_url="https://p/me", client_id="c", client_secret="s",
        redirect_uri="r", account_fields={"id": "key", "username": "name", "name": "displayName", "email": "emailAddress"},
    )
    body = {"key": "JIRAUSER1", "name": "alice", "displayName": "Alice", "emailAddress": "a@example.com"}
    provider = OAuth2Provider(config, http=httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, content=json.dumps(body))
    )))
    assert await provider.identify("at") == ProviderAccount("JIRAUSER1", "alice", "Alice", "a@example.com")


# -- what a tool is handed ------------------------------------------------------------


async def test_a_tool_is_handed_the_connections_of_the_person_its_turn_acts_for() -> None:
    from tests.test_connections import FakeProvider

    provider = FakeProvider()
    service = OAuthConnections(
        connections=MemoryConnectionStore(), links=MemoryLinkStore(), providers={"gitlab": provider},
        sealer=FernetSealer([new_sealing_key()]),
    )
    state, code = provider.consent(await service.begin("mattermost:u1", "gitlab"))
    await service.finish(state, code)
    asked: list[tuple] = []

    def owner_for(agent: str, user_id: str, turn: TurnScope | None) -> str | None:
        asked.append((agent, user_id, turn))
        return f"mattermost:{user_id}" if user_id else None

    access = connection_access(service, owner_for)
    mine = access("assistant", "u1", None)
    assert mine is not None and mine.providers == ("gitlab",)
    grant = await mine.grant("gitlab")
    account = await mine.account("gitlab")
    assert grant.token.reveal() == "access-1" and account is not None and account.username == "alice"
    stranger = access("assistant", "u2", None)
    assert stranger is not None
    with pytest.raises(NotConnected):
        await stranger.grant("gitlab")
    assert access("assistant", "", None) is None  # a turn that acts for nobody
    await mine.forget(grant)
    assert await service.connected("mattermost:u1") == {}
    assert asked[0] == ("assistant", "u1", None)


def test_the_providers_own_client_never_follows_a_redirect() -> None:
    """A token endpoint that redirects is misconfigured; the client secret and
    the code must not follow it somewhere else."""
    config = gitlab("https://gitlab.example", client_id="c", client_secret="s", redirect_uri="r")
    assert OAuth2Provider(config)._http.follow_redirects is False
