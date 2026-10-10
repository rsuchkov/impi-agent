"""OAuthConnections: connecting an account, handing a tool its token, refreshing
it across replicas, and ending it — against a scripted provider."""

import asyncio
import gc
import logging
import pickle
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from crucible.connections import (
    ACCOUNT_REFUSED,
    LINK_EXPIRED,
    LINK_FOREIGN,
    LINK_REJECTED,
    AccessToken,
    LinkRefused,
    NotConnected,
    OAuthConnections,
    OAuthTokens,
    ProviderAccount,
    ProviderRejected,
    ProviderUnavailable,
)
from crucible.connections.pkce import code_challenge
from crucible.connections.sealing import FernetSealer, new_sealing_key
from tests.fakes.connections import MemoryConnectionStore, MemoryLinkStore

OWNER = "mattermost:u1"
ALICE = ProviderAccount(id="7", username="alice", name="Alice", email="alice@example.com")


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FakeProvider:
    """Single-use codes; refresh tokens that rotate (the old one dies); a
    record of every refresh and revoke."""

    def __init__(self, *, rotate_refresh: bool = True, expires_in: int | None = 3600) -> None:
        self.codes: dict[str, str] = {}  # code -> the challenge it was issued for
        self.live_refresh: set[str] = set()
        self.accounts: dict[str, ProviderAccount] = {}
        self.refreshes = 0
        self.revoked: list[str] = []
        self.refresh_error: Exception | None = None
        self.revoke_error: Exception | None = None
        self.identify_error: Exception | None = None
        self.rotate_refresh = rotate_refresh
        self.expires_in = expires_in
        self.gate: asyncio.Event | None = None  # holds a refresh at the endpoint
        self._minted = 0

    def authorize_url(self, *, state: str, code_challenge: str) -> str:
        return f"https://provider.example/oauth/authorize?state={state}&code_challenge={code_challenge}"

    def consent(self, url: str, *, account: ProviderAccount = ALICE) -> tuple[str, str]:
        """The person consents: (state, code) as the provider sends them back."""
        query = parse_qs(urlparse(url).query)
        code = f"code-{len(self.codes)}"
        self.codes[code] = query["code_challenge"][0]
        self._next_account = account
        return query["state"][0], code

    def _mint(self) -> OAuthTokens:
        self._minted += 1
        access, refresh = f"access-{self._minted}", f"refresh-{self._minted}"
        self.live_refresh.add(refresh)
        self.accounts[access] = self._next_account
        return OAuthTokens(access, refresh, self.expires_in, "read_api")

    async def exchange(self, code: str, code_verifier: str) -> OAuthTokens:
        challenge = self.codes.pop(code, None)
        if challenge is None or challenge != code_challenge(code_verifier):
            raise ProviderRejected("invalid_grant")
        return self._mint()

    async def refresh(self, refresh_token: str) -> OAuthTokens:
        self.refreshes += 1
        if self.refresh_error is not None:
            raise self.refresh_error
        if refresh_token not in self.live_refresh:
            raise ProviderRejected("invalid_grant")
        tokens = self._mint()
        if self.rotate_refresh:
            self.live_refresh.discard(refresh_token)
        else:
            self.live_refresh.discard(tokens.refresh_token)
            tokens = OAuthTokens(tokens.access_token, "", tokens.expires_in, tokens.scope)
        if self.gate is not None:
            await self.gate.wait()  # answered, and the answer still on its way
        return tokens

    async def revoke(self, token: str) -> None:
        if self.revoke_error is not None:
            raise self.revoke_error
        self.revoked.append(token)
        self.live_refresh.discard(token)

    async def identify(self, access_token: str) -> ProviderAccount:
        if self.identify_error is not None:
            raise self.identify_error
        return self.accounts[access_token]


class Refuse:
    def __init__(self, reason: str | None) -> None:
        self.reason = reason

    async def refusal(self, owner, provider, account) -> str | None:
        return self.reason


def _service(provider=None, *, store=None, links=None, clock=None, keys=None, **over) -> OAuthConnections:
    return OAuthConnections(
        connections=store if store is not None else MemoryConnectionStore(),
        links=links if links is not None else MemoryLinkStore(),
        providers={"gitlab": provider or FakeProvider()},
        sealer=FernetSealer(keys or [KEY]),
        clock=clock or Clock(),
        poll_s=0.001,
        **over,
    )


KEY = new_sealing_key()


async def _connect(service: OAuthConnections, provider: FakeProvider, *, owner: str = OWNER) -> None:
    state, code = provider.consent(await service.begin(owner, "gitlab"))
    await service.finish(state, code, owner=owner)


# -- connecting -----------------------------------------------------------------


async def test_a_connect_stores_the_account_and_a_grant_hands_out_its_token() -> None:
    provider = FakeProvider()
    service = _service(provider)
    url = await service.begin(OWNER, "gitlab")
    state, code = provider.consent(url)
    connected = await service.finish(state, code)  # the link names its owner
    assert (connected.owner, connected.provider, connected.account) == (OWNER, "gitlab", ALICE)
    grant = await service.grant(OWNER, "gitlab")
    assert grant.token.reveal() == "access-1" and grant.account == ALICE and grant.version == 0
    assert await service.connected(OWNER) == {"gitlab": ALICE}
    assert await service.account("slack:U9", "gitlab") is None


async def test_a_link_finished_by_somebody_else_is_refused_and_spent() -> None:
    provider = FakeProvider()
    service = _service(provider)
    state, code = provider.consent(await service.begin(OWNER, "gitlab"))
    with pytest.raises(LinkRefused) as refused:
        await service.finish(state, code, owner="mattermost:intruder")
    assert refused.value.reason == LINK_FOREIGN
    assert await service.connected(OWNER) == {} and await service.connected("mattermost:intruder") == {}
    with pytest.raises(LinkRefused) as replayed:
        await service.finish(state, code, owner=OWNER)
    assert replayed.value.reason == LINK_EXPIRED


async def test_a_late_link_or_a_forged_code_is_refused() -> None:
    provider, clock = FakeProvider(), Clock()
    service = _service(provider, clock=clock)
    state, code = provider.consent(await service.begin(OWNER, "gitlab"))
    clock.advance(11 * 60)
    with pytest.raises(LinkRefused) as late:
        await service.finish(state, code)
    assert late.value.reason == LINK_EXPIRED
    state, _ = provider.consent(await service.begin(OWNER, "gitlab"))
    with pytest.raises(LinkRefused) as forged:
        await service.finish(state, "a-code-nobody-issued")
    assert forged.value.reason == LINK_REJECTED


async def test_an_account_the_application_refuses_is_not_kept_and_its_tokens_are_revoked() -> None:
    provider = FakeProvider()
    service = _service(provider, policy=Refuse("the email is not the chat user's"))
    state, code = provider.consent(await service.begin(OWNER, "gitlab"))
    with pytest.raises(LinkRefused) as refused:
        await service.finish(state, code)
    assert (refused.value.reason, refused.value.account) == (ACCOUNT_REFUSED, "alice")
    assert await service.connected(OWNER) == {}
    assert provider.revoked == ["refresh-1"]


async def test_tokens_issued_to_a_connect_that_then_fails_are_revoked() -> None:
    provider = FakeProvider()
    provider.identify_error = ProviderUnavailable("whoami down")
    service = _service(provider)
    state, code = provider.consent(await service.begin(OWNER, "gitlab"))
    with pytest.raises(ProviderUnavailable):
        await service.finish(state, code)
    assert provider.revoked == ["refresh-1"] and await service.connected(OWNER) == {}


async def test_a_reconnect_revokes_the_grant_it_replaces() -> None:
    provider = FakeProvider()
    service = _service(provider)
    await _connect(service, provider)
    await _connect(service, provider)
    assert provider.revoked == ["refresh-1"]
    assert (await service.grant(OWNER, "gitlab")).token.reveal() == "access-2"


async def test_a_declined_consent_spends_the_link() -> None:
    provider = FakeProvider()
    service = _service(provider)
    state, code = provider.consent(await service.begin(OWNER, "gitlab"))
    await service.refuse(state)
    with pytest.raises(LinkRefused) as refused:
        await service.finish(state, code)
    assert refused.value.reason == LINK_EXPIRED


# -- refreshing -----------------------------------------------------------------


async def test_concurrent_grants_of_an_ending_token_refresh_it_once() -> None:
    provider, clock, store = FakeProvider(), Clock(), MemoryConnectionStore()
    service = _service(provider, clock=clock, store=store)
    await _connect(service, provider)
    clock.advance(3600 - 30)  # inside the minute before it ends
    grants = await asyncio.gather(*(service.grant(OWNER, "gitlab") for _ in range(5)))
    assert provider.refreshes == 1
    assert {g.token.reveal() for g in grants} == {"access-2"}
    record = await store.get_connection(OWNER, "gitlab")
    assert record is not None and record.version == 1


async def test_two_replicas_on_one_store_refresh_once() -> None:
    provider, clock, store, links = FakeProvider(), Clock(), MemoryConnectionStore(), MemoryLinkStore()
    a = _service(provider, clock=clock, store=store, links=links)
    b = _service(provider, clock=clock, store=store, links=links)
    await _connect(a, provider)
    clock.advance(3600)
    provider.gate = asyncio.Event()
    first = asyncio.ensure_future(a.grant(OWNER, "gitlab"))
    await asyncio.sleep(0.01)  # A holds the lease, at the provider
    second = asyncio.ensure_future(b.grant(OWNER, "gitlab"))
    await asyncio.sleep(0.01)
    provider.gate.set()
    assert (await first).token.reveal() == (await second).token.reveal() == "access-2"
    assert provider.refreshes == 1


async def test_a_refresh_the_provider_refuses_ends_the_connection() -> None:
    provider, clock = FakeProvider(), Clock()
    service = _service(provider, clock=clock)
    await _connect(service, provider)
    provider.live_refresh.clear()  # revoked at the provider
    clock.advance(3600)
    with pytest.raises(NotConnected):
        await service.grant(OWNER, "gitlab")
    assert await service.connected(OWNER) == {}


async def test_a_refresh_another_replica_won_takes_the_winners_token() -> None:
    """A replica past an expired lease rotates while ours is at the provider;
    ours is refused (its refresh token is spent) and takes the winner's."""
    from dataclasses import replace

    provider, clock, store = FakeProvider(), Clock(), MemoryConnectionStore()
    service = _service(provider, clock=clock, store=store)
    await _connect(service, provider)
    clock.advance(3600)

    async def lose(refresh_token: str) -> OAuthTokens:
        record = await store.get_connection(OWNER, "gitlab")
        assert record is not None
        won = replace(record, version=record.version + 1, expires_at="")
        await store.replace_connection(won, expected_version=record.version)
        raise ProviderRejected("invalid_grant")

    provider.refresh = lose  # type: ignore[method-assign]
    grant = await service.grant(OWNER, "gitlab")
    assert grant.version == 1 and await service.connected(OWNER) == {"gitlab": ALICE}


async def test_a_provider_that_cannot_answer_frees_the_lease_at_once() -> None:
    provider, clock = FakeProvider(), Clock()
    service = _service(provider, clock=clock)
    await _connect(service, provider)
    clock.advance(3600)
    provider.refresh_error = ProviderUnavailable("503")
    with pytest.raises(ProviderUnavailable):
        await service.grant(OWNER, "gitlab")
    provider.refresh_error = None
    grant = await asyncio.wait_for(service.grant(OWNER, "gitlab"), 1)
    assert grant.token.reveal() == "access-2"


async def test_a_provider_that_keeps_the_refresh_token_is_refreshed_again_with_it() -> None:
    provider, clock = FakeProvider(rotate_refresh=False), Clock()
    service = _service(provider, clock=clock)
    await _connect(service, provider)
    for expected in ("access-2", "access-3"):
        clock.advance(3600)
        assert (await service.grant(OWNER, "gitlab")).token.reveal() == expected


async def test_a_token_without_an_end_is_never_refreshed() -> None:
    provider, clock = FakeProvider(expires_in=None), Clock()
    service = _service(provider, clock=clock)
    await _connect(service, provider)
    clock.advance(10 * 365 * 24 * 3600)
    assert (await service.grant(OWNER, "gitlab")).token.reveal() == "access-1"
    assert provider.refreshes == 0


async def test_a_refresh_whose_connection_was_replaced_meanwhile_revokes_its_pair() -> None:
    provider, clock, store = FakeProvider(), Clock(), MemoryConnectionStore()
    service = _service(provider, clock=clock, store=store)
    await _connect(service, provider)
    clock.advance(3600)
    provider.gate = asyncio.Event()
    refreshing = asyncio.ensure_future(service.grant(OWNER, "gitlab"))
    await asyncio.sleep(0.01)
    await _connect(_service(provider, clock=clock, store=store), provider)  # a reconnect lands
    provider.gate.set()
    grant = await refreshing
    assert grant.token.reveal() == "access-3"  # the reconnect's, not the refresh's
    assert "refresh-2" in provider.revoked  # the orphaned pair


async def test_a_refresh_that_lost_to_the_same_grant_revokes_nothing() -> None:
    """Revoking a pair from the grant the record still holds could end that
    grant, the winner's tokens with it."""
    from dataclasses import replace

    provider, clock, store = FakeProvider(), Clock(), MemoryConnectionStore()
    service = _service(provider, clock=clock, store=store)
    await _connect(service, provider)
    clock.advance(3600)
    provider.gate = asyncio.Event()
    refreshing = asyncio.ensure_future(service.grant(OWNER, "gitlab"))
    await asyncio.sleep(0.01)
    record = await store.get_connection(OWNER, "gitlab")
    assert record is not None  # another replica, past an expired lease, rotates the same grant
    await store.replace_connection(replace(record, version=1, expires_at=""), expected_version=0)
    provider.gate.set()
    await refreshing
    assert provider.revoked == []


async def test_one_lock_per_connection_does_not_outlive_its_use() -> None:
    provider, clock = FakeProvider(), Clock()
    service = _service(provider, clock=clock)
    await _connect(service, provider)
    clock.advance(3600)
    await service.grant(OWNER, "gitlab")
    gc.collect()
    assert len(service._locks) == 0


# -- ending -----------------------------------------------------------------------


async def test_forget_ends_only_the_connection_the_grant_came_from() -> None:
    provider = FakeProvider()
    service = _service(provider)
    await _connect(service, provider)
    old = await service.grant(OWNER, "gitlab")
    await _connect(service, provider)
    await service.forget(OWNER, old)  # a 401 on a call made before the reconnect
    assert (await service.grant(OWNER, "gitlab")).token.reveal() == "access-2"
    current = await service.grant(OWNER, "gitlab")
    await service.forget(OWNER, current)
    assert await service.connected(OWNER) == {}
    assert provider.revoked[-1] == "refresh-2"


async def test_disconnect_revokes_and_says_whether_the_provider_confirmed() -> None:
    provider = FakeProvider()
    service = _service(provider)
    await _connect(service, provider)
    ended = await service.disconnect(OWNER, "gitlab")
    assert ended is not None and ended.revoked and ended.account == ALICE
    assert provider.revoked == ["refresh-1"]
    assert await service.disconnect(OWNER, "gitlab") is None
    await _connect(service, provider)
    provider.revoke_error = ProviderUnavailable("503")
    ended = await service.disconnect(OWNER, "gitlab")
    assert ended is not None and not ended.revoked and await service.connected(OWNER) == {}


async def test_disconnect_revokes_what_it_deleted_when_a_reconnect_lands_in_between() -> None:
    """A reconnect between the read and the delete: the delete of what was
    read misses, and the connection ended — and revoked — is the one there."""
    provider = FakeProvider()

    class Racing(MemoryConnectionStore):
        raced = False

        async def delete_connection(self, owner, provider_name, *, connect_id=None, version=None):
            if not Racing.raced:
                Racing.raced = True
                await _connect(_service(provider, store=self), provider)
            return await super().delete_connection(owner, provider_name, connect_id=connect_id, version=version)

    store = Racing()
    service = _service(provider, store=store)
    await _connect(service, provider)
    ended = await service.disconnect(OWNER, "gitlab")
    assert ended is not None and await service.connected(OWNER) == {}
    # refresh-1 went with the reconnect; the disconnect revoked the one it deleted.
    assert provider.revoked == ["refresh-1", "refresh-2"]


# -- sealing ------------------------------------------------------------------------


async def test_tokens_are_sealed_at_rest_and_a_key_put_first_keeps_the_old_ones_readable() -> None:
    provider, store = FakeProvider(), MemoryConnectionStore()
    old_key, new_key = new_sealing_key(), new_sealing_key()
    await _connect(_service(provider, store=store, keys=[old_key]), provider)
    record = await store.get_connection(OWNER, "gitlab")
    assert record is not None and "access-1" not in record.access_sealed and "refresh-1" not in record.refresh_sealed
    rotated = _service(provider, store=store, keys=[new_key, old_key])
    assert (await rotated.grant(OWNER, "gitlab")).token.reveal() == "access-1"


async def test_a_connection_no_key_opens_is_dropped(caplog: pytest.LogCaptureFixture) -> None:
    provider, store = FakeProvider(), MemoryConnectionStore()
    await _connect(_service(provider, store=store), provider)
    lost = _service(provider, store=store, keys=[new_sealing_key()])
    with caplog.at_level(logging.WARNING), pytest.raises(NotConnected):
        await lost.grant(OWNER, "gitlab")
    assert await store.get_connection(OWNER, "gitlab") is None
    assert "cannot be opened with the current keys" in caplog.text


def test_a_sealer_refuses_a_bad_key_and_opens_nothing_it_did_not_seal() -> None:
    with pytest.raises(ValueError, match="not a Fernet key"):
        FernetSealer(["not-a-key"])
    with pytest.raises(ValueError, match="no sealing key"):
        FernetSealer([])
    sealer = FernetSealer([KEY])
    assert sealer.open(sealer.seal("v")) == "v"
    assert FernetSealer([new_sealing_key()]).open(sealer.seal("v")) is None
    assert sealer.open("plain text") is None


def test_an_access_token_shows_nothing_of_itself() -> None:
    token = AccessToken("glpat-secret")
    assert "glpat" not in repr(token) and "glpat" not in str(token) and token.reveal() == "glpat-secret"
    with pytest.raises(TypeError):
        pickle.dumps(token)
    assert "glpat" not in repr(OAuthTokens("glpat-a", "glrt-b", 60))


# -- audit ------------------------------------------------------------------------------


async def test_the_audit_trail_names_events_and_never_a_token(caplog: pytest.LogCaptureFixture) -> None:
    provider = FakeProvider()
    service = _service(provider)
    with caplog.at_level(logging.INFO, logger="crucible.connections.audit"):
        await _connect(service, provider)
        await service.disconnect(OWNER, "gitlab")
    lines = [r.getMessage() for r in caplog.records if r.name == "crucible.connections.audit"]
    assert lines == [
        "event=connect_start result=ok owner=mattermost:u1 provider=gitlab",
        "event=connect result=ok account=alice owner=mattermost:u1 provider=gitlab",
        "event=disconnect result=ok owner=mattermost:u1 provider=gitlab account=alice",
    ]
    assert "access-" not in caplog.text and "refresh-" not in caplog.text


def test_an_audit_value_cannot_add_a_field_or_a_line(caplog: pytest.LogCaptureFixture) -> None:
    from crucible.connections import LoggingAudit

    with caplog.at_level(logging.INFO, logger="crucible.connections.audit"):
        LoggingAudit().record("connect", "ok", account="eve result=ok\nevent=forged")
    assert caplog.records[-1].getMessage() == 'event=connect result=ok account="eve result=ok\\nevent=forged"'


# --- telling the person where they started -------------------------------------------


async def test_a_connect_hands_back_where_it_was_started_and_says_it_finished() -> None:
    provider, heard = FakeProvider(), []

    async def on_connected(connected) -> None:
        heard.append(connected)

    service = _service(provider, on_connected=on_connected)
    state, code = provider.consent(await service.begin(OWNER, "gitlab", return_to="message-7"))
    connected = await service.finish(state, code)

    assert connected.return_to == "message-7" and heard == [connected]
    state, code = provider.consent(await service.begin(OWNER, "gitlab"))
    assert (await service.finish(state, code)).return_to == ""  # started from nowhere


async def test_a_connect_refused_or_failing_to_tell_is_not_told_or_undone(caplog: pytest.LogCaptureFixture) -> None:
    provider, heard = FakeProvider(), []

    async def on_connected(connected) -> None:
        heard.append(connected)
        raise RuntimeError("chat is down")

    refused = _service(provider, on_connected=on_connected, policy=Refuse("not yours"))
    state, code = provider.consent(await refused.begin(OWNER, "gitlab", return_to="m"))
    with pytest.raises(LinkRefused):
        await refused.finish(state, code)
    assert heard == []

    service = _service(provider, on_connected=on_connected)
    state, code = provider.consent(await service.begin(OWNER, "gitlab", return_to="m"))
    with caplog.at_level(logging.WARNING):
        connected = await service.finish(state, code)
    assert heard == [connected] and "on_connected hook failed" in caplog.text
    assert await service.account(OWNER, "gitlab") == ALICE  # the connect stands
