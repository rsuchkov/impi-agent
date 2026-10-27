"""OAuthConnections: people connect their accounts at a provider, and tools act
as them.

A connect is two halves. ``begin`` stores a single-use link (a state and a
PKCE verifier, ten minutes) and answers with the provider's consent URL; the
person consents there and the provider sends them back to the application,
which calls ``finish`` with the state and the code. Only then is anything
stored: the tokens, sealed, and who the account is.

A tool asks ``grant`` for the person of its turn. A token close to its end is
refreshed first — and a provider that issues a new refresh token on every
refresh makes the old one useless, so two refreshes of one connection must
never run at once: one lock per connection in this process, and a lease in the
store across replicas. Every write that starts from a record it read names the
credential it read (``connect_id`` new at every connect, ``version`` counting
refreshes), so a slow refresh, or a delete after a 401, cannot undo a connect
made after it began.

Tokens that are no longer wanted are revoked at the provider, best effort: the
pair a failed connect was issued, the grant a reconnect replaces, the pair a
refresh got after its connection was replaced or ended.

What stays the application's: who the owner is, where the callback lands and
how it is authenticated, and which account may be bound to whom
(``AccountPolicy``).
"""

import asyncio
import contextlib
import logging
import math
import secrets
import weakref
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from crucible.connections.audit import (
    EVENT_CONNECT,
    EVENT_CONNECT_START,
    EVENT_DISCONNECT,
    EVENT_FORGET,
    EVENT_REFRESH,
    RESULT_NOT_CONNECTED,
    RESULT_OK,
    RESULT_REJECTED,
    RESULT_REVOKE_FAILED,
    RESULT_UNAVAILABLE,
    LoggingAudit,
)
from crucible.connections.errors import (
    ACCOUNT_REFUSED,
    LINK_DENIED,
    LINK_EXPIRED,
    LINK_FOREIGN,
    LINK_REJECTED,
    LinkRefused,
    ProviderRejected,
)
from crucible.connections.pkce import (
    code_challenge,
    new_code_verifier,
    new_state,
    state_digest,
)
from crucible.connections.ports import (
    AccountPolicy,
    ConnectionsAudit,
    OAuthProvider,
    Sealer,
)
from crucible.connections.types import Connected, Disconnection, OAuthTokens
from crucible.ports.connections import (
    AccessToken,
    ConnectionAccessFactory,
    NotConnected,
    ProviderAccount,
    ProviderUnavailable,
    TokenGrant,
)
from crucible.ports.telemetry import ConnectionChanged, Telemetry
from crucible.ports.turn import TurnScope
from crucible.store.base import (
    ConnectionLinkRecord,
    ConnectionLinkStore,
    ConnectionRecord,
    ConnectionStore,
)

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime | None) -> str:
    return moment.isoformat(timespec="seconds") if moment is not None else ""


def _parse(value: str) -> datetime:
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _credential(record: ConnectionRecord) -> tuple[str, int]:
    return record.connect_id, record.version


def _account(record: ConnectionRecord) -> ProviderAccount:
    return ProviderAccount(
        id=record.account_id, username=record.account_username,
        name=record.account_name, email=record.account_email,
    )


async def _revoke(oauth: OAuthProvider, token: str) -> bool:
    """End a grant at the provider, best effort: whether it confirmed."""
    try:
        await oauth.revoke(token)
    except Exception:  # noqa: BLE001 - revoking is best effort; the caller says so
        logger.warning("the provider did not confirm a revoke", exc_info=True)
        return False
    return True


class OAuthConnections:
    def __init__(
        self,
        *,
        connections: ConnectionStore,
        links: ConnectionLinkStore,
        providers: Mapping[str, OAuthProvider],
        sealer: Sealer,
        policy: AccountPolicy | None = None,
        audit: ConnectionsAudit | None = None,
        telemetry: Telemetry | None = None,
        link_ttl_s: float = 600.0,
        refresh_skew_s: float = 60.0,
        lease_s: float = 45.0,
        poll_s: float = 0.5,
        clock: Callable[[], datetime] = _utcnow,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        on_connected: Callable[[Connected], Awaitable[None]] | None = None,
    ) -> None:
        self._connections = connections
        self._links = links
        self._providers = dict(providers)
        self._sealer = sealer
        self._policy = policy
        self._audit = audit or LoggingAudit()
        self._telemetry = telemetry
        # How long a started connect waits for the person to come back.
        self._link_ttl_s = link_ttl_s
        # A token this close to its end is refreshed before it is handed out.
        self._refresh_skew_s = refresh_skew_s
        # How long a replica holds the refresh lease, and how often the others
        # look whether it has finished.
        self._lease_s = lease_s
        self._poll_s = poll_s
        self._clock = clock
        self._sleep = sleep
        # The application's word once a connect is stored — to tell the person
        # where they started it. Its failure is logged; the connect stands.
        self._on_connected = on_connected
        # One lock per connection, alive only while someone holds or awaits it.
        self._locks: weakref.WeakValueDictionary[tuple[str, str], asyncio.Lock] = weakref.WeakValueDictionary()

    @property
    def link_ttl_s(self) -> float:
        """How long a started connect waits for the person to come back."""
        return self._link_ttl_s

    @property
    def providers(self) -> tuple[str, ...]:
        """The providers a person may connect to, by name."""
        return tuple(sorted(self._providers))

    def for_owner(self, owner: str) -> "OwnerConnections":
        """``owner``'s connections, as a tool may use them (``access.ConnectionAccess``)."""
        return OwnerConnections(self, owner)

    # -- connecting ---------------------------------------------------------

    async def begin(self, owner: str, provider: str, *, return_to: str = "") -> str:
        """Start a connect for ``owner``: the provider's consent URL to send
        them to. The URL is the person's alone — whoever holds it can finish
        the connect within its time, so it goes to them privately.
        ``return_to`` is kept with the link and handed back in ``Connected``."""
        oauth = self._provider(provider)
        state, verifier = new_state(), new_code_verifier()
        await self._links.put_link(ConnectionLinkRecord(
            state_digest=state_digest(state), owner=owner, provider=provider,
            verifier_sealed=self._sealer.seal(verifier),
            expires_at=_iso(self._clock() + timedelta(seconds=self._link_ttl_s)),
            return_to=return_to,
        ))
        self._audit_and_tell(EVENT_CONNECT_START, RESULT_OK, owner=owner, provider=provider)
        return oauth.authorize_url(state=state, code_challenge=code_challenge(verifier))

    async def finish(
        self, state: str, code: str, *, owner: str | None = None,
        facts: Mapping[str, str] | None = None,
    ) -> Connected:
        """Finish the connect ``state`` names, with the code the provider sent
        back. ``owner`` is who the application knows came back — when it knows
        (a browser that brought its session): a link started by somebody else
        is then refused. Without it the link's own owner is taken, and holding
        the state is the proof. ``facts`` is what else the application knows
        about who came back, for its ``AccountPolicy`` to decide by."""
        link = await self._links.take_link(state_digest(state))
        if link is None or _parse(link.expires_at) <= self._clock():
            self._audit_and_tell(EVENT_CONNECT, LINK_EXPIRED, owner=owner or "")
            raise LinkRefused(LINK_EXPIRED)
        where = {"owner": link.owner, "provider": link.provider}
        if owner is not None and link.owner != owner:
            self._audit_and_tell(EVENT_CONNECT, LINK_FOREIGN, **where)
            raise LinkRefused(LINK_FOREIGN, provider=link.provider)
        oauth = self._providers.get(link.provider)
        verifier = self._sealer.open(link.verifier_sealed)
        if oauth is None or verifier is None:
            self._audit_and_tell(EVENT_CONNECT, LINK_EXPIRED, **where)
            raise LinkRefused(LINK_EXPIRED, provider=link.provider)
        try:
            tokens = await oauth.exchange(code, verifier)
        except ProviderRejected as exc:
            self._audit_and_tell(EVENT_CONNECT, LINK_REJECTED, **where)
            raise LinkRefused(LINK_REJECTED, provider=link.provider) from exc
        except ProviderUnavailable:
            self._audit_and_tell(EVENT_CONNECT, RESULT_UNAVAILABLE, **where)
            raise
        try:
            account = await oauth.identify(tokens.access_token)
            refusal = (
                await self._policy.refusal(link.owner, link.provider, account, dict(facts or {}))
                if self._policy else None
            )
        except BaseException:
            # Issued, and about to be dropped: nobody may keep using them.
            await self._revoke_tokens(oauth, tokens)
            self._audit_and_tell(EVENT_CONNECT, RESULT_UNAVAILABLE, **where)
            raise
        if refusal is not None:
            await self._revoke_tokens(oauth, tokens)
            self._audit_and_tell(EVENT_CONNECT, ACCOUNT_REFUSED, account=account.username, **where)
            raise LinkRefused(ACCOUNT_REFUSED, provider=link.provider, account=account.username, detail=refusal)
        previous = await self._connections.get_connection(link.owner, link.provider)
        try:
            await self._connections.save_connection(self._record(link.owner, link.provider, tokens, account))
        except BaseException:
            await self._revoke_tokens(oauth, tokens)
            raise
        if previous is not None:
            await self._revoke_stored(previous)  # the grant this connect replaced
        self._audit_and_tell(EVENT_CONNECT, RESULT_OK, account=account.username, **where)
        connected = Connected(owner=link.owner, provider=link.provider, account=account, return_to=link.return_to)
        if self._on_connected is not None:
            try:
                await self._on_connected(connected)
            except Exception:  # noqa: BLE001 - telling them is a courtesy; the connect is stored
                logger.warning("connections: the on_connected hook failed", exc_info=True)
        return connected

    async def refuse(self, state: str) -> None:
        """The person declined at the provider: spend the link, so the state
        cannot be used again."""
        link = await self._links.take_link(state_digest(state))
        if link is not None:
            self._audit_and_tell(EVENT_CONNECT, LINK_DENIED, owner=link.owner, provider=link.provider)

    # -- reading ------------------------------------------------------------

    async def account(self, owner: str, provider: str) -> ProviderAccount | None:
        """Who ``owner`` is connected as at ``provider``, without touching the token."""
        record = await self._connections.get_connection(owner, provider)
        return _account(record) if record is not None else None

    async def connected(self, owner: str) -> dict[str, ProviderAccount]:
        """Every provider ``owner`` is connected to, and as whom."""
        return {r.provider: _account(r) for r in await self._connections.list_connections(owner)}

    async def grant(self, owner: str, provider: str) -> TokenGrant:
        """The token to act as ``owner`` at ``provider`` with — refreshed first
        when it is about to end. ``NotConnected`` when there is none to give."""
        try:
            record, access = await self._load(owner, provider)
            if not self._fresh(record):
                record = await self._refresh(owner, provider)
                access = self._sealer.open(record.access_sealed) or ""
        except NotConnected:
            self._tell(provider, "grant", RESULT_NOT_CONNECTED)
            raise
        self._tell(provider, "grant", RESULT_OK)
        return TokenGrant(
            provider=provider, token=AccessToken(access), account=_account(record),
            connect_id=record.connect_id, version=record.version,
        )

    def _audit_and_tell(self, event: str, result: str, **fields: str) -> None:
        self._audit.record(event, result, **fields)
        self._tell(fields.get("provider", ""), event, result)

    def _tell(self, provider: str, event: str, result: str) -> None:
        if self._telemetry is None:
            return
        try:
            self._telemetry.record(ConnectionChanged(provider=provider, event=event, result=result))
        except Exception:  # noqa: BLE001 - telemetry must never cost the connection
            logger.warning("telemetry failed to record a connection event", exc_info=True)

    async def forget(self, owner: str, grant: TokenGrant) -> None:
        """The provider answered a call made with ``grant`` with 401: that
        connection is over. Only that one — a connect made since survives."""
        record = await self._connections.get_connection(owner, grant.provider)
        deleted = await self._connections.delete_connection(
            owner, grant.provider, connect_id=grant.connect_id, version=grant.version,
        )
        if deleted and record is not None and _credential(record) == (grant.connect_id, grant.version):
            await self._revoke_stored(record)
        self._audit_and_tell(
            EVENT_FORGET, RESULT_OK if deleted else RESULT_NOT_CONNECTED,
            owner=owner, provider=grant.provider, account=grant.account.username,
        )

    async def disconnect(self, owner: str, provider: str) -> Disconnection | None:
        """End ``owner``'s connection to ``provider`` and revoke it there. None
        when there was none."""
        for _ in range(3):
            record = await self._connections.get_connection(owner, provider)
            if record is None:
                self._audit_and_tell(EVENT_DISCONNECT, RESULT_NOT_CONNECTED, owner=owner, provider=provider)
                return None
            # The record seen, not whatever is there by then: a connect that
            # lands in between is somebody's new connection, not this one.
            if await self._connections.delete_connection(
                owner, provider, connect_id=record.connect_id, version=record.version,
            ):
                revoked = await self._revoke_stored(record)
                self._audit_and_tell(
                    EVENT_DISCONNECT, RESULT_OK if revoked else RESULT_REVOKE_FAILED,
                    owner=owner, provider=provider, account=record.account_username,
                )
                return Disconnection(account=_account(record), revoked=revoked)
        raise ProviderUnavailable(f"{owner}'s connection to {provider} kept changing while it was being ended")

    # -- refreshing -----------------------------------------------------------

    async def _refresh(self, owner: str, provider: str) -> ConnectionRecord:
        key = (owner, provider)
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        async with lock:
            for _ in range(2):
                record, _ = await self._load(owner, provider)
                if self._fresh(record):
                    return record  # somebody ahead of us refreshed it
                now = self._clock()
                if await self._connections.claim_refresh(
                    owner, provider, connect_id=record.connect_id, version=record.version,
                    now=_iso(now), until=_iso(now + timedelta(seconds=self._lease_s)),
                ):
                    return await self._rotate(record)
                await self._wait_for_rotation(record)
        raise ProviderUnavailable(f"another replica is still refreshing {owner}'s {provider} token")

    async def _wait_for_rotation(self, seen: ConnectionRecord) -> None:
        for _ in range(max(1, math.ceil(self._lease_s / self._poll_s))):
            await self._sleep(self._poll_s)
            latest = await self._connections.get_connection(seen.owner, seen.provider)
            if latest is None or _credential(latest) != _credential(seen):
                return

    async def _rotate(self, record: ConnectionRecord) -> ConnectionRecord:
        owner, provider = record.owner, record.provider
        where = {"owner": owner, "provider": provider, "account": record.account_username}
        oauth = self._provider(provider)
        refresh_token = self._sealer.open(record.refresh_sealed) if record.refresh_sealed else None
        if not refresh_token:
            # Nothing to refresh with: the connection has run its course.
            await self._connections.delete_connection(owner, provider, connect_id=record.connect_id, version=record.version)
            self._audit_and_tell(EVENT_REFRESH, RESULT_NOT_CONNECTED, **where)
            raise NotConnected(owner, provider)
        try:
            fresh = await oauth.refresh(refresh_token)
        except ProviderRejected as exc:
            latest = await self._connections.get_connection(owner, provider)
            if latest is not None and _credential(latest) != _credential(record):
                return latest  # another replica got there and rotated it
            await self._connections.delete_connection(owner, provider, connect_id=record.connect_id, version=record.version)
            self._audit_and_tell(EVENT_REFRESH, RESULT_REJECTED, **where)
            raise NotConnected(owner, provider) from exc
        except BaseException:
            # The lease would lapse anyway; ending it lets the next try in now.
            with contextlib.suppress(Exception):
                await self._connections.release_refresh(owner, provider, connect_id=record.connect_id, version=record.version)
            self._audit_and_tell(EVENT_REFRESH, RESULT_UNAVAILABLE, **where)
            raise
        rotated = replace(
            record,
            access_sealed=self._sealer.seal(fresh.access_token),
            refresh_sealed=self._sealer.seal(fresh.refresh_token) if fresh.refresh_token else record.refresh_sealed,
            expires_at=_iso(fresh.expires_at(self._clock())),
            scope=fresh.scope or record.scope,
            version=record.version + 1,
        )
        if await self._connections.replace_connection(rotated, expected_version=record.version):
            self._audit_and_tell(EVENT_REFRESH, RESULT_OK, **where)
            return rotated
        # Replaced or ended while the provider answered. A pair from a grant
        # that is gone is revoked; one from the same grant is not — revoking
        # it could end the grant the record now holds.
        latest = await self._connections.get_connection(owner, provider)
        if latest is None or latest.connect_id != record.connect_id:
            await self._revoke_tokens(oauth, fresh)
        record, _ = await self._load(owner, provider)
        return record

    # -- plumbing -------------------------------------------------------------

    def _provider(self, name: str) -> OAuthProvider:
        oauth = self._providers.get(name)
        if oauth is None:
            raise ProviderUnavailable(f"{name} is not a provider this application is configured for")
        return oauth

    def _fresh(self, record: ConnectionRecord) -> bool:
        if not record.expires_at:
            return True  # a token that does not expire
        return (_parse(record.expires_at) - self._clock()).total_seconds() > self._refresh_skew_s

    async def _load(self, owner: str, provider: str) -> tuple[ConnectionRecord, str]:
        """The record and its access token, opened. A record the keys no longer
        open is no connection at all: it is dropped, and the person connects
        again."""
        record = await self._connections.get_connection(owner, provider)
        if record is None:
            raise NotConnected(owner, provider)
        access = self._sealer.open(record.access_sealed)
        if access is None:
            logger.warning(
                "%s's connection to %s cannot be opened with the current keys; forgetting it", owner, provider,
            )
            await self._connections.delete_connection(owner, provider, connect_id=record.connect_id, version=record.version)
            raise NotConnected(owner, provider)
        return record, access

    def _record(self, owner: str, provider: str, tokens: OAuthTokens, account: ProviderAccount) -> ConnectionRecord:
        return ConnectionRecord(
            owner=owner, provider=provider,
            access_sealed=self._sealer.seal(tokens.access_token),
            refresh_sealed=self._sealer.seal(tokens.refresh_token) if tokens.refresh_token else "",
            expires_at=_iso(tokens.expires_at(self._clock())),
            account_id=account.id, account_username=account.username,
            account_name=account.name, account_email=account.email,
            scope=tokens.scope, connect_id=secrets.token_hex(16),
        )

    async def _revoke_tokens(self, oauth: OAuthProvider, tokens: OAuthTokens) -> bool:
        return await _revoke(oauth, tokens.refresh_token or tokens.access_token)

    async def _revoke_stored(self, record: ConnectionRecord) -> bool:
        oauth = self._providers.get(record.provider)
        sealed = record.refresh_sealed or record.access_sealed
        token = self._sealer.open(sealed) if sealed else None
        if oauth is None or token is None:
            return False
        return await _revoke(oauth, token)


class OwnerConnections:
    """One person's connections: what a tool acting for them is handed. It can
    use them and end one after a 401; it cannot start or end a connection,
    and never learns whose they are."""

    def __init__(self, service: OAuthConnections, owner: str) -> None:
        self._service = service
        self._owner = owner

    @property
    def providers(self) -> tuple[str, ...]:
        return self._service.providers

    async def grant(self, provider: str) -> TokenGrant:
        return await self._service.grant(self._owner, provider)

    async def forget(self, grant: TokenGrant) -> None:
        await self._service.forget(self._owner, grant)

    async def account(self, provider: str) -> ProviderAccount | None:
        return await self._service.account(self._owner, provider)


def connection_access(
    service: OAuthConnections, owner_for: Callable[[str, str, str, TurnScope | None], str | None],
) -> ConnectionAccessFactory:
    """What the tool server is handed: for each call, the connections of the
    person its turn acts for. ``owner_for(agent, user_id, gateway, turn)`` is
    the application's rule for who that is — a chat user as ``<gateway>:<id>``,
    a caller's realm — or None for a turn that acts for nobody it can name;
    ``gateway`` is the conversation's."""

    def access(agent: str, user_id: str, gateway: str, turn: TurnScope | None) -> OwnerConnections | None:
        owner = owner_for(agent, user_id, gateway, turn)
        return service.for_owner(owner) if owner else None

    return access

