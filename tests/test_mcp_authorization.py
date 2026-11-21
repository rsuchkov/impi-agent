"""The MCP gateway's authorization server over real HTTP: the hub with its
routes on one port, the connections callback on another, a fake provider in
between (offline, aiohttp client)."""

import base64
import hashlib
import logging
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

import aiohttp
import pytest
from aiohttp import web

from crucible.connections import Connections, ProviderAccount
from crucible.connections.callback import CALLBACK_PATH, CallbackServer
from crucible.connections.sealing import FernetSealer, new_sealing_key
from crucible.gateways.mcp import MCP_PATH, McpChatClient, McpHub
from crucible.gateways.mcp.hub import RESOURCE_METADATA_PATH
from crucible.mcp_authorization import (
    AS_METADATA_PATH,
    AUDIT_LOGGER,
    AUTHORIZE_PATH,
    CONSENT_COOKIE,
    CONSENT_PATH,
    REVOKE_PATH,
    SIGN_IN_OWNER_PREFIX,
    SIGN_IN_PATH,
    TOKEN_PATH,
    AuthorizationServer,
    ClientRegistration,
    ConsentRequest,
    GrantStanding,
    SignedIn,
)
from crucible.ports.telemetry import McpGrantChanged
from crucible.store.sqlite import SqliteSessionStore
from tests.fakes.connections import MemoryConnectionStore, MemoryLinkStore
from tests.fakes.mcp_turns import MemoryMcpTurns
from tests.test_connections import ALICE, Clock, FakeProvider
from tests.test_mcp_hub import FakeSink, _reply

HUB_PORT = 8621
CALLBACK_PORT = 8622
PUBLIC = f"http://127.0.0.1:{HUB_PORT}"
REDIRECT = "http://127.0.0.1:43123/callback"
CLIENTS = {
    "https://ide.example/mcp-client": ClientRegistration("The IDE"),
    "portal": ClientRegistration("Portal", redirect_uris=("https://portal.example/oauth/done",), loopback=False),
}
IDE = "https://ide.example/mcp-client"


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


class Recorder:
    def __init__(self) -> None:
        self.events: list[McpGrantChanged] = []

    def record(self, event) -> None:
        self.events.append(event)


class Policy:
    """The application's word at a refresh: holds, unless told otherwise."""

    def __init__(self) -> None:
        self.standing = GrantStanding.HOLDS
        self.asked: list[str] = []

    async def still_holds(self, grant):
        self.asked.append(grant.subject)
        return self.standing


@dataclass
class World:
    hub: McpHub
    server: AuthorizationServer
    callback: CallbackServer
    connections: Connections
    provider: FakeProvider
    store: SqliteSessionStore
    links: MemoryLinkStore
    clock: Clock
    telemetry: Recorder
    sink: FakeSink
    policy: "Policy"


@pytest.fixture
async def world(tmp_path: Path) -> AsyncIterator[World]:
    clock, provider, telemetry, policy = Clock(), FakeProvider(), Recorder(), Policy()
    store = SqliteSessionStore(tmp_path / "s.db")
    links = MemoryLinkStore()
    connections = Connections(
        connections=MemoryConnectionStore(), links=links, providers={"okta": provider},
        sealer=FernetSealer([new_sealing_key()]), clock=clock, poll_s=0.001,
    )
    server = AuthorizationServer(
        store, connections, public_url=PUBLIC, clients=CLIENTS, clock=clock, telemetry=telemetry,
        grant_idle_s=12 * 3600, grant_max_s=7 * 24 * 3600, access_ttl_s=3600, grant_policy=policy,
    )
    hub = McpHub("127.0.0.1", HUB_PORT, server, store=MemoryMcpTurns(), public_url=PUBLIC, routes=server.routes())
    sink = FakeSink(_reply)
    hub.register_agent("helper", sink, McpChatClient(hub.turns, "helper", cards=None, asker_of=hub.asker_of))
    callback = CallbackServer(connections, host="127.0.0.1", port=CALLBACK_PORT, sign_ins=server)
    await hub.start()
    await callback.start()
    try:
        yield World(hub, server, callback, connections, provider, store, links, clock, telemetry, sink, policy)
    finally:
        await callback.stop()
        await hub.stop()
        await store.close()


def _authorize_url(challenge: str, *, client: str = IDE, redirect: str = REDIRECT, scope: str = "read act", **over) -> str:
    params = {
        "response_type": "code", "client_id": client, "redirect_uri": redirect, "code_challenge": challenge,
        "code_challenge_method": "S256", "scope": scope, "state": "xyz", "resource": f"{PUBLIC}{MCP_PATH}",
    }
    params.update(over)
    params = {k: v for k, v in params.items() if v is not None}
    return f"{PUBLIC}{AUTHORIZE_PATH}?" + "&".join(f"{k}={quote(v, safe='')}" for k, v in params.items())


def _query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


async def _sign_in_and_consent(
    s: aiohttp.ClientSession, w: World, challenge: str, *, decision: str = "allow", **over
) -> aiohttp.ClientResponse:
    """From /authorize to the redirect back to the client, as a browser would."""
    async with s.get(_authorize_url(challenge, **over), allow_redirects=False) as r:
        assert r.status == 303, await r.text()
        provider_url = r.headers["Location"]
    assert provider_url.startswith("https://provider.example/oauth/authorize")
    state, code = w.provider.consent(provider_url, account=ALICE)
    async with s.get(f"http://127.0.0.1:{CALLBACK_PORT}{CALLBACK_PATH}?state={state}&code={code}", allow_redirects=False) as r:
        assert r.status == 303, await r.text()
        consent_url = r.headers["Location"]
    assert consent_url.startswith(f"{PUBLIC}{CONSENT_PATH}/")
    async with s.get(consent_url) as r:
        page = await r.text()
        assert r.status == 200 and "The IDE" in page and "alice" in page
        # The page's policy lets its form send the browser on to the client —
        # a loopback client listens where it can, so every loopback address.
        assert r.headers["Content-Security-Policy"] == (
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' http://localhost:* http://127.0.0.1:* http://[::1]:*"
        )
        csrf = page.split('name="csrf" value="')[1].split('"')[0]
    answered = await s.post(consent_url, data={"csrf": csrf, "decision": decision}, allow_redirects=False)
    assert "http://127.0.0.1:*" in answered.headers["Content-Security-Policy"]
    return answered


async def _tokens(s: aiohttp.ClientSession, **form) -> tuple[int, dict]:
    async with s.post(f"{PUBLIC}{TOKEN_PATH}", data=form) as r:
        return r.status, await r.json()


async def _call(s: aiohttp.ClientSession, token: str, text: str = "hi") -> aiohttp.ClientResponse:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "ask_agent", "arguments": {"text": text}}}
    headers = {"Authorization": f"Bearer {token}", "MCP-Protocol-Version": "2026-07-28", "Accept": "application/json"}
    return await s.post(f"{PUBLIC}{MCP_PATH}", json=body, headers=headers)


async def test_a_client_signs_the_person_in_consents_and_calls_with_the_token(world: World, caplog) -> None:
    w = world
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER)
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as s:
        # 1. Without a token the gateway names the metadata; the metadata names the server.
        async with s.post(f"{PUBLIC}{MCP_PATH}", json={}) as r:
            assert r.status == 401
            assert r.headers["WWW-Authenticate"] == f'Bearer resource_metadata="{PUBLIC}{RESOURCE_METADATA_PATH}"'
        async with s.get(f"{PUBLIC}{RESOURCE_METADATA_PATH}") as r:
            resource = await r.json()
        assert resource == {
            "resource": f"{PUBLIC}{MCP_PATH}", "authorization_servers": [PUBLIC],
            "scopes_supported": ["read", "act"], "bearer_methods_supported": ["header"],
        }
        async with s.get(f"{PUBLIC}{AS_METADATA_PATH}") as r:
            metadata = await r.json()
        assert metadata["issuer"] == PUBLIC and metadata["authorization_endpoint"] == f"{PUBLIC}{AUTHORIZE_PATH}"
        assert metadata["token_endpoint"] == f"{PUBLIC}{TOKEN_PATH}" and metadata["revocation_endpoint"] == f"{PUBLIC}{REVOKE_PATH}"
        assert metadata["code_challenge_methods_supported"] == ["S256"] and "registration_endpoint" not in metadata
        assert metadata["client_id_metadata_document_supported"] is True
        assert metadata["authorization_response_iss_parameter_supported"] is True  # the redirect carries iss

        # 2. Authorize → sign in at the provider → consent → a code to the client.
        verifier, challenge = _pkce()
        async with await _sign_in_and_consent(s, w, challenge) as r:
            assert r.status == 302
            back = r.headers["Location"]
        assert back.startswith(REDIRECT + "?")
        query = _query(back)
        assert query["state"] == "xyz" and query["iss"] == PUBLIC and query["code"]
        # The sign-in kept nothing: no connection, the provider's tokens revoked.
        assert await w.store.list_connections(SIGN_IN_OWNER_PREFIX + "x") == []
        assert w.provider.revoked == ["refresh-1"]  # the grant at the provider ends with its refresh token

        # 3. The code for tokens, with the verifier.
        status, tokens = await _tokens(
            s, grant_type="authorization_code", code=query["code"], redirect_uri=REDIRECT, client_id=IDE,
            code_verifier=verifier, resource=f"{PUBLIC}{MCP_PATH}",
        )
        assert status == 200 and tokens["token_type"] == "Bearer" and tokens["scope"] == "read act"
        assert tokens["expires_in"] == 3600 and tokens["refresh_token"]
        # The code works once.
        status, again = await _tokens(
            s, grant_type="authorization_code", code=query["code"], redirect_uri=REDIRECT, client_id=IDE, code_verifier=verifier,
        )
        assert status == 400 and again["error"] == "invalid_grant"

        # 4. The token calls the gateway as the person.
        async with await _call(s, tokens["access_token"]) as r:
            assert r.status == 200
            answer = await r.json()
        assert answer["result"]["content"][0]["text"].startswith("you said: hi\n\nConversation: ")
        msg = w.sink.submitted[0]
        assert msg.user_id == "okta:7" and msg.username == "alice"
        assert msg.turn is not None and msg.turn.attributes["mcp_client"] == IDE and msg.turn.attributes["mcp_scopes"] == "act read"
        assert msg.turn.attributes["mcp_client_name"] == "The IDE"  # the operator's name, for a person's eyes

    events = [(e.event, e.result) for e in w.telemetry.events]
    assert events == [("authorize", "ok"), ("sign_in", "ok"), ("consent", "ok"), ("token", "ok"), ("token", "refused")]
    audit = "\n".join(r.getMessage() for r in caplog.records if r.name == AUDIT_LOGGER)
    assert "event=sign_in result=ok" in audit and "subject=okta:7" in audit
    assert query["code"] not in audit and tokens["access_token"] not in audit and verifier not in audit


async def test_refresh_rotates_and_a_reused_refresh_token_ends_the_grant(world: World) -> None:
    w = world
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as s:
        verifier, challenge = _pkce()
        async with await _sign_in_and_consent(s, w, challenge) as r:
            code = _query(r.headers["Location"])["code"]
        _, first = await _tokens(
            s, grant_type="authorization_code", code=code, redirect_uri=REDIRECT, client_id=IDE, code_verifier=verifier,
        )
        w.clock.advance(1800)
        status, second = await _tokens(s, grant_type="refresh_token", refresh_token=first["refresh_token"], client_id=IDE)
        assert status == 200 and second["access_token"] != first["access_token"]
        async with await _call(s, first["access_token"]) as r:
            assert r.status == 401  # rotated away
        async with await _call(s, second["access_token"]) as r:
            assert r.status == 200
        # Another client cannot refresh with it.
        status, body = await _tokens(s, grant_type="refresh_token", refresh_token=second["refresh_token"], client_id="portal")
        assert status == 400 and body["error"] == "invalid_grant"
        # The first refresh token again: somebody has a copy. Everything ends.
        status, body = await _tokens(s, grant_type="refresh_token", refresh_token=first["refresh_token"], client_id=IDE)
        assert status == 400 and "revoked" in body["error_description"]
        async with await _call(s, second["access_token"]) as r:
            assert r.status == 401
        status, body = await _tokens(s, grant_type="refresh_token", refresh_token=second["refresh_token"], client_id=IDE)
        assert status == 400
    assert [e.result for e in w.telemetry.events if e.event == "refresh"] == ["ok", "refused", "reused", "refused"]


async def test_tokens_end_with_the_grant_and_a_revoke_ends_them_at_once(world: World) -> None:
    w = world
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as s:
        verifier, challenge = _pkce()
        async with await _sign_in_and_consent(s, w, challenge) as r:
            code = _query(r.headers["Location"])["code"]
        _, tokens = await _tokens(
            s, grant_type="authorization_code", code=code, redirect_uri=REDIRECT, client_id=IDE, code_verifier=verifier,
        )
        # A refresh keeps the grant alive: the idle span starts again from it.
        w.clock.advance(12 * 3600 - 600)
        status, kept = await _tokens(s, grant_type="refresh_token", refresh_token=tokens["refresh_token"], client_id=IDE)
        assert status == 200 and kept["expires_in"] == 3600  # a fresh hour: the grant now ends twelve hours from here
        assert w.policy.asked == ["okta:7"]  # the application was asked whether it still holds
        # Unused for the idle span, the grant ends.
        w.clock.advance(12 * 3600 + 1)
        async with await _call(s, kept["access_token"]) as r:
            assert r.status == 401
        status, body = await _tokens(s, grant_type="refresh_token", refresh_token=kept["refresh_token"], client_id=IDE)
        assert status == 400 and "sign in again" in body["error_description"]

        # Refreshed every hour, the grant still ends a week from the sign-in;
        # near that end an access token lives only as long as the grant.
        w.clock.advance(1)
        verifier, challenge = _pkce()
        async with await _sign_in_and_consent(s, w, challenge) as r:
            code = _query(r.headers["Location"])["code"]
        _, tokens = await _tokens(
            s, grant_type="authorization_code", code=code, redirect_uri=REDIRECT, client_id=IDE, code_verifier=verifier,
        )
        for _ in range(7 * 24 - 1):
            w.clock.advance(3600)
            status, tokens = await _tokens(s, grant_type="refresh_token", refresh_token=tokens["refresh_token"], client_id=IDE)
            assert status == 200
        w.clock.advance(3000)
        status, late = await _tokens(s, grant_type="refresh_token", refresh_token=tokens["refresh_token"], client_id=IDE)
        assert status == 200 and late["expires_in"] == 600
        w.clock.advance(601)
        status, body = await _tokens(s, grant_type="refresh_token", refresh_token=late["refresh_token"], client_id=IDE)
        assert status == 400 and "sign in again" in body["error_description"]

        # The application's word at a refresh: ended, or not to be had right now.
        w.clock.advance(1)
        verifier, challenge = _pkce()
        async with await _sign_in_and_consent(s, w, challenge) as r:
            code = _query(r.headers["Location"])["code"]
        _, tokens = await _tokens(
            s, grant_type="authorization_code", code=code, redirect_uri=REDIRECT, client_id=IDE, code_verifier=verifier,
        )
        w.policy.standing = GrantStanding.UNAVAILABLE
        async with s.post(f"{PUBLIC}{TOKEN_PATH}", data=dict(grant_type="refresh_token", refresh_token=tokens["refresh_token"], client_id=IDE)) as r:
            # Not an OAuth error, which a client would read as "sign in again": a plain "later".
            assert r.status == 503 and r.headers["Retry-After"] == "30" and r.content_type == "text/plain"
        async with await _call(s, tokens["access_token"]) as r:
            assert r.status == 200  # the access token stands meanwhile
        w.policy.standing = GrantStanding.ENDED
        status, body = await _tokens(s, grant_type="refresh_token", refresh_token=tokens["refresh_token"], client_id=IDE)
        assert status == 400 and "was ended" in body["error_description"]
        async with await _call(s, tokens["access_token"]) as r:
            assert r.status == 401  # the grant is gone, its access token with it
        w.policy.standing = GrantStanding.HOLDS

        # A fresh grant, revoked by the client.
        w.clock.advance(1)
        verifier, challenge = _pkce()
        async with await _sign_in_and_consent(s, w, challenge) as r:
            code = _query(r.headers["Location"])["code"]
        _, tokens = await _tokens(
            s, grant_type="authorization_code", code=code, redirect_uri=REDIRECT, client_id=IDE, code_verifier=verifier,
        )
        async with await _call(s, tokens["access_token"]) as r:
            assert r.status == 200
        async with s.post(f"{PUBLIC}{REVOKE_PATH}", data={"token": tokens["refresh_token"]}) as r:
            assert r.status == 200
        async with await _call(s, tokens["access_token"]) as r:
            assert r.status == 401
        async with s.post(f"{PUBLIC}{REVOKE_PATH}", data={"token": "never-issued"}) as r:
            assert r.status == 200  # revoked all the same

        # The application's side: the person's grants, and a revoke by id.
        verifier, challenge = _pkce()
        async with await _sign_in_and_consent(s, w, challenge) as r:
            code = _query(r.headers["Location"])["code"]
        _, tokens = await _tokens(
            s, grant_type="authorization_code", code=code, redirect_uri=REDIRECT, client_id=IDE, code_verifier=verifier,
        )
        mine = await w.server.grants_of("okta:7")
        assert [g.client for g in mine] == [IDE] and mine[0].scope == "read act" and mine[0].refreshed_at == ""
        w.clock.advance(60)
        _, tokens = await _tokens(s, grant_type="refresh_token", refresh_token=tokens["refresh_token"], client_id=IDE)
        mine = await w.server.grants_of("okta:7")
        assert mine[0].refreshed_at and mine[0].refreshed_at > mine[0].signed_in_at
        assert await w.server.revoke("bob@example.com", mine[0].id) is False  # not theirs
        async with await _call(s, tokens["access_token"]) as r:
            assert r.status == 200
        assert await w.server.revoke("okta:7", mine[0].id) is True
        assert await w.server.grants_of("okta:7") == []
        async with await _call(s, tokens["access_token"]) as r:
            assert r.status == 401
        assert await w.server.revoke("okta:7", mine[0].id) is False  # gone


async def test_what_is_refused_and_how(world: World) -> None:
    w = world
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as s:
        verifier, challenge = _pkce()
        # A client not on the list, or a redirect not its own: a page, never a redirect.
        async with s.get(_authorize_url(challenge, client="stranger"), allow_redirects=False) as r:
            assert r.status == 400 and "not known" in await r.text()
        async with s.get(_authorize_url(challenge, client="portal"), allow_redirects=False) as r:
            assert r.status == 400 and "not registered" in await r.text()
        async with s.get(_authorize_url(challenge, redirect="https://evil.example/cb"), allow_redirects=False) as r:
            assert r.status == 400
        # The registered client with its registered redirect is let through;
        # a loopback client with any port and path too.
        async with s.get(_authorize_url(challenge, client="portal", redirect="https://portal.example/oauth/done"), allow_redirects=False) as r:
            assert r.status == 303
        async with s.get(_authorize_url(challenge, redirect="http://localhost:9/x"), allow_redirects=False) as r:
            assert r.status == 303
        # A request that does not hold up goes back to the client with the error.
        async with s.get(_authorize_url(challenge, code_challenge_method="plain"), allow_redirects=False) as r:
            assert r.status == 302 and _query(r.headers["Location"])["error"] == "invalid_request"
        async with s.get(_authorize_url(challenge, scope="admin"), allow_redirects=False) as r:
            assert r.status == 302 and _query(r.headers["Location"])["error"] == "invalid_scope"
        async with s.get(_authorize_url(challenge, resource="https://other.example/mcp"), allow_redirects=False) as r:
            assert _query(r.headers["Location"])["error"] == "invalid_target"
        async with s.get(_authorize_url(challenge, response_type="token"), allow_redirects=False) as r:
            query = _query(r.headers["Location"])
            assert query["error"] == "unsupported_response_type" and query["state"] == "xyz"
        # The person declines.
        async with await _sign_in_and_consent(s, w, challenge, decision="deny") as r:
            assert r.status == 302
            assert _query(r.headers["Location"]) == {"error": "access_denied", "state": "xyz", "iss": PUBLIC}
        # A wrong verifier, a wrong client, a wrong redirect: the code is spent either way.
        for wrong in ({"code_verifier": secrets.token_urlsafe(48)}, {"client_id": "portal"}, {"redirect_uri": "http://127.0.0.1:1/"}):
            verifier, challenge = _pkce()
            async with await _sign_in_and_consent(s, w, challenge) as r:
                code = _query(r.headers["Location"])["code"]
            form = dict(grant_type="authorization_code", code=code, redirect_uri=REDIRECT, client_id=IDE, code_verifier=verifier)
            form.update(wrong)
            status, body = await _tokens(s, **form)
            assert status == 400 and body["error"] == "invalid_grant"
            status, body = await _tokens(
                s, grant_type="authorization_code", code=code, redirect_uri=REDIRECT, client_id=IDE, code_verifier=verifier,
            )
            assert status == 400  # spent
        status, body = await _tokens(s, grant_type="password")
        assert status == 400 and body["error"] == "unsupported_grant_type"
        # A consent answer that did not come from the page: refused, and the request is spent.
        verifier, challenge = _pkce()
        async with s.get(_authorize_url(challenge), allow_redirects=False) as r:
            state, code = w.provider.consent(r.headers["Location"])
        async with s.get(f"http://127.0.0.1:{CALLBACK_PORT}{CALLBACK_PATH}?state={state}&code={code}", allow_redirects=False) as r:
            consent_url = r.headers["Location"]
        async with s.post(consent_url, data={"csrf": "guess", "decision": "allow"}, allow_redirects=False) as r:
            assert r.status == 403
        async with s.get(consent_url) as r:
            assert r.status == 400 and "expired" in await r.text()
        # The client that started the request knows its id; it did not sign in
        # and holds no cookie: the consent page is not its to see or answer.
        verifier, challenge = _pkce()
        async with s.get(_authorize_url(challenge), allow_redirects=False) as r:
            state, code = w.provider.consent(r.headers["Location"])
        async with s.get(f"http://127.0.0.1:{CALLBACK_PORT}{CALLBACK_PATH}?state={state}&code={code}", allow_redirects=False) as r:
            consent_url = r.headers["Location"]
            assert r.cookies[CONSENT_COOKIE].value and r.cookies[CONSENT_COOKIE]["path"] == CONSENT_PATH
        async with aiohttp.ClientSession() as stranger:
            async with stranger.get(consent_url) as r:
                assert r.status == 400
            async with stranger.post(consent_url, data={"csrf": "x", "decision": "allow"}, allow_redirects=False) as r:
                assert r.status == 400
        async with s.get(consent_url) as r:
            assert r.status == 200  # the browser that signed in still may
        # The code expires on its own.
        verifier, challenge = _pkce()
        async with await _sign_in_and_consent(s, w, challenge) as r:
            code = _query(r.headers["Location"])["code"]
        w.clock.advance(61)
        status, body = await _tokens(
            s, grant_type="authorization_code", code=code, redirect_uri=REDIRECT, client_id=IDE, code_verifier=verifier,
        )
        assert status == 400 and body["error"] == "invalid_grant"
        # A bearer that is not a token of ours.
        async with await _call(s, "made-up") as r:
            assert r.status == 401


async def test_a_consent_for_a_client_elsewhere_may_send_the_browser_to_its_address(world: World) -> None:
    w = world
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as s:
        verifier, challenge = _pkce()
        url = _authorize_url(challenge, client="portal", redirect="https://portal.example/oauth/done")
        async with s.get(url, allow_redirects=False) as r:
            state, code = w.provider.consent(r.headers["Location"], account=ALICE)
        async with s.get(f"http://127.0.0.1:{CALLBACK_PORT}{CALLBACK_PATH}?state={state}&code={code}", allow_redirects=False) as r:
            consent_url = r.headers["Location"]
        async with s.get(consent_url) as r:
            policy = r.headers["Content-Security-Policy"]
            assert policy.endswith("form-action 'self' https://portal.example") and "localhost" not in policy
            csrf = (await r.text()).split('name="csrf" value="')[1].split('"')[0]
        async with s.post(consent_url, data={"csrf": csrf, "decision": "allow"}, allow_redirects=False) as r:
            assert r.status == 302 and r.headers["Location"].startswith("https://portal.example/oauth/done?")
            assert r.headers["Content-Security-Policy"].endswith("form-action 'self' https://portal.example")


async def test_with_several_providers_the_person_picks_one(tmp_path: Path) -> None:
    clock, okta, github = Clock(), FakeProvider(), FakeProvider()
    store = SqliteSessionStore(tmp_path / "s.db")
    connections = Connections(
        connections=MemoryConnectionStore(), links=MemoryLinkStore(), providers={"okta": okta, "github": github},
        sealer=FernetSealer([new_sealing_key()]), clock=clock, poll_s=0.001,
    )

    class Page:
        async def render(self, consent: ConsentRequest, request):
            return web.Response(text=f"custom {consent.client_name} {consent.username} {' '.join(consent.scopes)} {consent.csrf}")

    async def subject_of(connected, request):
        if connected.account.email.endswith("@stranger.example"):
            return web.Response(status=403, text="Sign in to the assistant in your chat first.")
        return SignedIn(connected.account.email, connected.account.name)

    server = AuthorizationServer(
        store, connections, public_url=PUBLIC, clients=CLIENTS, clock=clock, consent_page=Page(), subject_of=subject_of,
        sign_in_providers=("okta", "github"),
    )
    hub = McpHub("127.0.0.1", HUB_PORT, server, store=MemoryMcpTurns(), public_url=PUBLIC, routes=server.routes())
    callback = CallbackServer(connections, host="127.0.0.1", port=CALLBACK_PORT, sign_ins=server)
    await hub.start()
    await callback.start()
    try:
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as s:
            verifier, challenge = _pkce()
            async with s.get(_authorize_url(challenge, scope="read"), allow_redirects=False) as r:
                page = await r.text()
                assert r.status == 200 and "okta" in page and "github" in page
                link = page.split('href="')[2].split('"')[0]
            assert link.startswith(f"{PUBLIC}{SIGN_IN_PATH}/") and link.endswith("provider=github")
            async with s.get(link, allow_redirects=False) as r:
                assert r.status == 303
                state, code = github.consent(r.headers["Location"], account=ALICE)
            async with s.get(f"http://127.0.0.1:{CALLBACK_PORT}{CALLBACK_PATH}?state={state}&code={code}", allow_redirects=False) as r:
                consent_url = r.headers["Location"]
            async with s.get(consent_url) as r:
                text = await r.text()
                assert text.startswith("custom The IDE Alice read ")
                assert r.headers["Cache-Control"] == "no-store"  # the flow's headers on the application's page
                assert "form-action 'self' http://localhost:*" in r.headers["Content-Security-Policy"]  # and the consent's policy
                csrf = text.split()[-1]
            async with s.post(consent_url, data={"csrf": csrf, "decision": "allow"}, allow_redirects=False) as r:
                code = _query(r.headers["Location"])["code"]
            status, tokens = await _tokens(
                s, grant_type="authorization_code", code=code, redirect_uri=REDIRECT, client_id=IDE, code_verifier=verifier,
            )
            assert status == 200 and tokens["scope"] == "read"
            caller = await server.authenticate(_FakeRequest(tokens["access_token"]))  # type: ignore[arg-type]
            assert caller is not None and caller.subject == "alice@example.com" and caller.username == "Alice"
            assert caller.scopes == {"read"}
            # The sign-in link cannot be used twice.
            async with s.get(link, allow_redirects=False) as r:
                assert r.status == 400
            # The application refuses a sign-in with a page of its own.
            verifier, challenge = _pkce()
            async with s.get(_authorize_url(challenge, scope="read"), allow_redirects=False) as r:
                page = await r.text()
                link = page.split('href="')[1].split('"')[0]
            async with s.get(link, allow_redirects=False) as r:
                state, code = okta.consent(r.headers["Location"], account=ProviderAccount(id="9", username="eve", name="Eve", email="eve@stranger.example"))
            async with s.get(f"http://127.0.0.1:{CALLBACK_PORT}{CALLBACK_PATH}?state={state}&code={code}", allow_redirects=False) as r:
                assert r.status == 403 and await r.text() == "Sign in to the assistant in your chat first."
                assert r.headers["Cache-Control"] == "no-store"
    finally:
        await callback.stop()
        await hub.stop()
        await store.close()


class _FakeRequest:
    def __init__(self, token: str) -> None:
        self.headers = {"Authorization": f"Bearer {token}"}


async def test_a_sign_in_connect_stores_nothing_and_asks_no_rule() -> None:
    clock, provider = Clock(), FakeProvider()
    store, links = MemoryConnectionStore(), MemoryLinkStore()
    asked: list = []

    class Policy:
        async def refusal(self, owner, provider, account, facts):
            asked.append(owner)
            return "no"

    told: list = []

    async def on_connected(connected):
        told.append(connected)

    connections = Connections(
        connections=store, links=links, providers={"okta": provider}, sealer=FernetSealer([new_sealing_key()]),
        clock=clock, poll_s=0.001, policy=Policy(), on_connected=on_connected,
    )
    url = await connections.begin("mcp-sign-in:r1", "okta", return_to="r1", sign_in=True)
    state, code = provider.consent(url, account=ALICE)
    connected = await connections.finish(state, code)
    assert connected.sign_in and connected.return_to == "r1" and connected.account == ALICE
    assert connected.owner == "mcp-sign-in:r1"
    assert asked == [] and told == []  # no rule, no word: nobody is connected
    assert await store.list_connections("mcp-sign-in:r1") == []
    assert provider.revoked == ["refresh-1"]
    # An ordinary connect still asks the rule.
    url = await connections.begin("mattermost:u1", "okta")
    state, code = provider.consent(url, account=ALICE)
    with pytest.raises(Exception, match="account_refused"):
        await connections.finish(state, code)
    assert asked == ["mattermost:u1"]


async def test_the_callback_without_a_sign_in_taker_reports_a_failure(tmp_path: Path) -> None:
    clock, provider = Clock(), FakeProvider()
    connections = Connections(
        connections=MemoryConnectionStore(), links=MemoryLinkStore(), providers={"okta": provider},
        sealer=FernetSealer([new_sealing_key()]), clock=clock, poll_s=0.001,
    )
    callback = CallbackServer(connections, host="127.0.0.1", port=CALLBACK_PORT)
    await callback.start()
    try:
        url = await connections.begin("mcp-sign-in:r1", "okta", return_to="r1", sign_in=True)
        state, code = provider.consent(url)
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as s:
            async with s.get(f"http://127.0.0.1:{CALLBACK_PORT}{CALLBACK_PATH}?state={state}&code={code}") as r:
                assert r.status == 500 and "Connected" not in await r.text()
    finally:
        await callback.stop()


def test_a_registration_knows_its_redirects() -> None:
    loopback = ClientRegistration("x")
    assert loopback.allows("http://127.0.0.1:5000/cb") and loopback.allows("http://localhost/cb")
    assert loopback.allows("http://[::1]:8/") and not loopback.allows("https://127.0.0.1/cb")
    assert not loopback.allows("http://127.0.0.1.evil.example/cb") and not loopback.allows("")
    fixed = ClientRegistration("y", redirect_uris=("https://y.example/done",), loopback=False)
    assert fixed.allows("https://y.example/done") and not fixed.allows("https://y.example/done?x=1")
    assert not fixed.allows("http://127.0.0.1:1/")
