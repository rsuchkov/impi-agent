"""The page a provider sends the person back to."""

import logging

import aiohttp
import pytest

from crucible.connections import OAuthConnections, ProviderAccount, ProviderUnavailable
from crucible.connections.callback import CallbackServer
from crucible.connections.sealing import FernetSealer, new_sealing_key
from tests.fakes.connections import MemoryConnectionStore, MemoryLinkStore
from tests.test_connections import FakeProvider, Refuse

PORT = 8606
URL = f"http://127.0.0.1:{PORT}/oauth/callback"


async def _serve(provider: FakeProvider, *, start_again: str | None = None, **over) -> tuple[OAuthConnections, CallbackServer]:
    service = OAuthConnections(
        connections=MemoryConnectionStore(), links=MemoryLinkStore(), providers={"gitlab": provider},
        sealer=FernetSealer([new_sealing_key()]), **over,
    )
    server = CallbackServer(
        service, host="127.0.0.1", port=PORT, **({"start_again": start_again} if start_again else {}),
    )
    await server.start()
    return service, server


async def _get(params: dict) -> tuple[int, str, dict]:
    async with aiohttp.ClientSession() as s:
        async with s.get(URL, params=params, allow_redirects=False) as resp:
            return resp.status, await resp.text(), dict(resp.headers)


async def test_a_connect_comes_back_and_is_finished(caplog: pytest.LogCaptureFixture) -> None:
    provider = FakeProvider()
    service, server = await _serve(provider)
    try:
        state, code = provider.consent(await service.begin("mattermost:u1", "gitlab"))
        with caplog.at_level(logging.DEBUG):
            status, page, headers = await _get({"state": state, "code": code})
        assert status == 200 and "account alice is connected" in page
        assert await service.connected("mattermost:u1") == {"gitlab": provider.accounts["access-1"]}
        # Nothing of the request is written down, and the page lets nothing on.
        assert state not in caplog.text and code not in caplog.text and state not in page
        assert headers["Referrer-Policy"] == "no-referrer" and headers["Cache-Control"] == "no-store"
    finally:
        await server.stop()


async def test_a_declined_consent_spends_the_link() -> None:
    provider = FakeProvider()
    service, server = await _serve(provider)
    try:
        state, code = provider.consent(await service.begin("mattermost:u1", "gitlab"))
        status, page, _ = await _get({"state": state, "error": "access_denied"})
        assert status == 200 and "declined" in page
        status, page, _ = await _get({"state": state, "code": code})
        assert status == 400 and "expired or was already used" in page
    finally:
        await server.stop()


async def test_an_account_the_rule_refuses_is_named_and_escaped() -> None:
    provider = FakeProvider()
    service, server = await _serve(provider, policy=Refuse("its email is not yours"))
    try:
        hostile = ProviderAccount(id="1", username="<script>x</script>", email="e@example.com")
        state, code = provider.consent(await service.begin("mattermost:u1", "gitlab"), account=hostile)
        status, page, _ = await _get({"state": state, "code": code})
        assert status == 403 and "its email is not yours" in page
        assert "<script>" not in page and "&lt;script&gt;" in page
    finally:
        await server.stop()


async def test_a_provider_that_cannot_answer_asks_for_another_try() -> None:
    provider = FakeProvider()
    provider.identify_error = ProviderUnavailable("down")
    service, server = await _serve(provider)
    try:
        state, code = provider.consent(await service.begin("mattermost:u1", "gitlab"))
        status, page, _ = await _get({"state": state, "code": code})
        assert status == 503 and "did not answer" in page
    finally:
        await server.stop()


async def test_a_request_that_is_no_connect_is_turned_away() -> None:
    _, server = await _serve(FakeProvider())
    try:
        assert (await _get({}))[0] == 400
        assert (await _get({"state": "s"}))[0] == 400
        assert (await _get({"state": "never-issued", "code": "c"}))[0] == 400
    finally:
        await server.stop()


async def test_a_page_that_did_not_connect_says_where_to_start_again() -> None:
    _, server = await _serve(FakeProvider(), start_again="Press Connect in the chat.")
    try:
        status, page, _ = await _get({"state": "never-issued", "code": "c"})
        assert status == 400 and "Press Connect in the chat." in page
    finally:
        await server.stop()
