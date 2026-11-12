"""The page a provider sends the person back to."""

import logging

import aiohttp
import pytest
from aiohttp import web

from crucible.connections import Connections, ProviderAccount, ProviderUnavailable
from crucible.connections.callback import (
    DEFAULT_START_AGAIN,
    CallbackOutcome,
    CallbackServer,
)
from crucible.connections.sealing import FernetSealer, new_sealing_key
from tests.fakes.connections import MemoryConnectionStore, MemoryLinkStore
from tests.test_connections import FakeProvider, Refuse

PORT = 8606
URL = f"http://127.0.0.1:{PORT}/oauth/callback"


async def _serve(provider: FakeProvider, *, start_again: str | None = None, **over) -> tuple[Connections, CallbackServer]:
    service = Connections(
        connections=MemoryConnectionStore(), links=MemoryLinkStore(), providers={"gitlab": provider},
        sealer=FernetSealer([new_sealing_key()]), **over,
    )
    server = CallbackServer(
        service, host="127.0.0.1", port=PORT, start_again=start_again if start_again else DEFAULT_START_AGAIN,
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


async def test_the_application_draws_the_page_and_the_flow_keeps_its_headers() -> None:
    """A page of the application's gets how the connect ended and answers as it
    likes — its words, its own content-security policy; the flow's safety
    headers are added where the page set none."""
    from aiohttp import web

    from crucible.connections.callback import (
        OUTCOME_NO_CODE,
        OUTCOME_NOT_A_CONNECT,
        OUTCOME_PROVIDER_UNAVAILABLE,
        CallbackOutcome,
    )
    from crucible.connections.errors import LINK_DENIED, LINK_EXPIRED

    outcomes: list[CallbackOutcome] = []

    class _Picture:
        def render(self, outcome: CallbackOutcome) -> web.Response:
            outcomes.append(outcome)
            if outcome.connected is not None:
                text = f"<img src='data:image/gif;base64,R0lGOD'> {outcome.connected.provider} as {outcome.connected.account.name}"
            else:
                text = f"{outcome.refusal}: {outcome.account} {outcome.detail} {outcome.start_again}".strip()
            return web.Response(
                status=outcome.status, text=text, content_type="text/html",
                headers={"Content-Security-Policy": "default-src 'none'; img-src data:", "Referrer-Policy": "same-origin"},
            )

        def form(self, form) -> web.Response:
            raise AssertionError("no token provider here")

    provider = FakeProvider()
    service = Connections(
        connections=MemoryConnectionStore(), links=MemoryLinkStore(), providers={"gitlab": provider},
        sealer=FernetSealer([new_sealing_key()]),
    )
    server = CallbackServer(service, host="127.0.0.1", port=PORT, start_again="Press Connect.", page=_Picture())
    await server.start()
    try:
        state, code = provider.consent(await service.begin("mattermost:u1", "gitlab"))
        status, body, headers = await _get({"state": state, "code": code})
        assert status == 200 and "gitlab as" in body and "<img" in body
        # The page's own policy and referrer stand; what it did not set, the flow adds.
        assert headers["Content-Security-Policy"] == "default-src 'none'; img-src data:"
        assert headers["Referrer-Policy"] == "same-origin" and headers["Cache-Control"] == "no-store"
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert outcomes[-1].connected is not None and outcomes[-1].refusal == "" and outcomes[-1].start_again == "Press Connect."
        # Every way a connect does not go through is a word the page reads.
        status, body, _ = await _get({})
        assert status == 400 and body.startswith(OUTCOME_NOT_A_CONNECT)
        state2, _code2 = provider.consent(await service.begin("mattermost:u1", "gitlab"))
        status, body, _ = await _get({"state": state2, "error": "access_denied"})
        assert status == 200 and body.startswith(LINK_DENIED)
        state3, _code3 = provider.consent(await service.begin("mattermost:u1", "gitlab"))
        status, body, _ = await _get({"state": state3})
        assert status == 400 and body.startswith(OUTCOME_NO_CODE)
        status, body, _ = await _get({"state": "stale", "code": "x"})
        assert status == 400 and body.startswith(LINK_EXPIRED)
        provider.identify_error = ProviderUnavailable("down")
        state4, code4 = provider.consent(await service.begin("mattermost:u1", "gitlab"))
        status, body, _ = await _get({"state": state4, "code": code4})
        assert status == 503 and body.startswith(OUTCOME_PROVIDER_UNAVAILABLE)
        assert [o.status for o in outcomes] == [200, 400, 200, 400, 400, 503]
    finally:
        await server.stop()


# -- the credential form --------------------------------------------------------------


async def _serve_credential(provider, *, page=None) -> tuple[Connections, CallbackServer]:
    service = Connections(
        connections=MemoryConnectionStore(), links=MemoryLinkStore(), providers={"graylog": provider},
        sealer=FernetSealer([new_sealing_key()]), credential_form_url=f"http://127.0.0.1:{PORT}/connect",
    )
    server = CallbackServer(service, host="127.0.0.1", port=PORT, start_again="Press Connect.", page=page)
    await server.start()
    return service, server


async def _get_form(url: str) -> tuple[int, str, dict]:
    async with aiohttp.ClientSession() as s:
        async with s.get(url, allow_redirects=False) as resp:
            return resp.status, await resp.text(), dict(resp.headers)


async def _post_form(url: str, fields: dict) -> tuple[int, str]:
    async with aiohttp.ClientSession() as s:
        async with s.post(url, data=fields, allow_redirects=False) as resp:
            return resp.status, await resp.text()


def _csrf_of(page: str) -> str:
    import re

    found = re.search(r'name="csrf" value="([^"]+)"', page)
    assert found
    return found.group(1)


async def test_a_credential_is_entered_into_the_form_and_connects(caplog: pytest.LogCaptureFixture) -> None:
    from tests.test_connections import FakeCredentialProvider

    provider = FakeCredentialProvider()
    service, server = await _serve_credential(provider)
    try:
        url = await service.begin("mattermost:u1", "graylog")
        with caplog.at_level(logging.DEBUG):
            status, page, headers = await _get_form(url)
            assert status == 200 and provider.hint in page and 'type="password"' in page and 'autocomplete="off"' in page
            assert headers["Cache-Control"] == "no-store" and "form-action 'self'" in headers["Content-Security-Policy"]
            status, page = await _post_form(url, {"credential": "tok-alice", "csrf": _csrf_of(page)})
        assert status == 200 and "account alice is connected" in page
        assert await service.connected("mattermost:u1") == {"graylog": provider.tokens["tok-alice"]}
        assert "tok-alice" not in caplog.text and "tok-alice" not in page
        status, page, _ = await _get_form(url)  # spent
        assert status == 400 and "expired or was already used" in page
    finally:
        await server.stop()


async def test_a_form_without_a_credential_is_drawn_again_and_one_without_its_secret_is_refused() -> None:
    from tests.test_connections import FakeCredentialProvider

    service, server = await _serve_credential(FakeCredentialProvider())
    try:
        url = await service.begin("mattermost:u1", "graylog")
        status, page = await _post_form(url, {"credential": "", "csrf": "x"})
        assert status == 200 and 'name="credential"' in page  # the form again, the link intact
        status, page = await _post_form(url, {"credential": "tok-alice", "csrf": "not-it"})
        assert status == 403 and "not the one the link opened" in page
        status, page = await _post_form(url, {"credential": "tok-alice", "csrf": "not-it"})
        assert status == 400 and "expired or was already used" in page  # spent by the forged post
        url = await service.begin("mattermost:u1", "graylog")
        _, page, _ = await _get_form(url)
        status, page = await _post_form(url, {"credential": "tok-nobody", "csrf": _csrf_of(page)})
        assert status == 400 and "did not accept that credential" in page
        status, _ = await _post_form(url, {"credential": "x" * 20000, "csrf": "y"})
        assert status == 413
    finally:
        await server.stop()


async def test_the_applications_page_draws_the_form_too() -> None:
    from crucible.connections import CredentialForm
    from tests.test_connections import FakeCredentialProvider

    drawn: list[CredentialForm] = []

    class _Own:
        def render(self, outcome: CallbackOutcome) -> web.Response:
            return web.Response(status=outcome.status, text=f"own: {outcome.refusal or 'ok'}", content_type="text/html")

        def form(self, form: CredentialForm) -> web.Response:
            drawn.append(form)
            return web.Response(status=200, text=f"own form for {form.provider}: {form.start_again}", content_type="text/html")

    service, server = await _serve_credential(FakeCredentialProvider(), page=_Own())
    try:
        url = await service.begin("mattermost:u1", "graylog")
        status, page, headers = await _get_form(url)
        assert status == 200 and page == "own form for graylog: Press Connect." and headers["Referrer-Policy"] == "no-referrer"
        assert drawn[0].post_url == url and drawn[0].csrf
        status, page = await _post_form(url, {"credential": "tok-alice", "csrf": drawn[0].csrf})
        assert (status, page) == (200, "own: ok")
    finally:
        await server.stop()
