"""Connections: people connect their accounts at a provider, and tools act
as them.

Two kinds of provider. An OAuth one (``OAuthProvider``): the person consents
at the provider, tokens come back, are refreshed and revoked. A credential
one (``CredentialProvider``): a system with no OAuth, where the person makes
a credential themselves — a personal token, an API key — and enters it into
a form the application serves; it is kept exactly as a grant is, never
refreshed, and never revoked by the engine — it is the person's, or a
service account's shared with others, and goes on working at the system
until they end it there.

A connect is two halves. ``begin`` stores a single-use link (a state and a
PKCE verifier, ten minutes) and answers with the provider's consent URL; the
person consents there and the provider sends them back to the application,
which calls ``finish`` with the state and the code. Only then is anything
stored: the tokens, sealed, and who the account is. For a credential provider
``begin`` answers with the form's address instead (``credential_form_url`` and
the state), the link keeping the secret the form must hand back; the
application serves the form (``form_of``) and calls ``finish_with_credential``
with what the person entered.

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
import json
import logging
import math
import secrets
import weakref
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

from crucible.connections.audit import (
    EVENT_CONNECT,
    EVENT_CONNECT_START,
    EVENT_DISCONNECT,
    EVENT_FORGET,
    EVENT_MOVE,
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
    CREDENTIAL_REJECTED,
    LINK_DENIED,
    LINK_EXPIRED,
    LINK_FOREIGN,
    LINK_FORGED,
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
    ConnectionOwner,
    ConnectionsAudit,
    CredentialProvider,
    OAuthProvider,
    Sealer,
)
from crucible.connections.types import (
    Connected,
    CredentialForm,
    Disconnection,
    OAuthTokens,
)
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


def _grant_identity(record: ConnectionRecord) -> tuple[str, int]:
    return record.connect_id, record.version


def _account(record: ConnectionRecord) -> ProviderAccount:
    return ProviderAccount(
        id=record.account_id, username=record.account_username,
        name=record.account_name, email=record.account_email,
        extra=_extra_of(record.account_extra),
    )


def _extra_of(raw: str) -> dict[str, Any]:
    """The kept fields back from the record; a record that holds none, or
    one a newer engine wrote in a shape this one cannot read, is none."""
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
    except ValueError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


async def _revoke(oauth: OAuthProvider, token: str) -> bool:
    """End a grant at the provider, best effort: whether it confirmed."""
    try:
        await oauth.revoke(token)
    except Exception:  # noqa: BLE001 - revoking is best effort; the caller says so
        logger.warning("the provider did not confirm a revoke", exc_info=True)
        return False
    return True


class Connections:
    def __init__(
        self,
        *,
        connections: ConnectionStore,
        links: ConnectionLinkStore,
        providers: Mapping[str, OAuthProvider | CredentialProvider],
        sealer: Sealer,
        policy: AccountPolicy | None = None,
        owners: ConnectionOwner | None = None,
        audit: ConnectionsAudit | None = None,
        telemetry: Telemetry | None = None,
        link_ttl_s: float = 600.0,
        refresh_skew_s: float = 60.0,
        lease_s: float = 45.0,
        poll_s: float = 0.5,
        clock: Callable[[], datetime] = _utcnow,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        on_connected: Callable[[Connected], Awaitable[None]] | None = None,
        credential_form_url: str = "",
    ) -> None:
        self._connections = connections
        self._links = links
        self._providers = dict(providers)
        self._sealer = sealer
        # Where the form that asks a person for a credential is served, without
        # the state — the application's listener (``CallbackServer``); "" when
        # no credential provider is wired, and a begin for one is then refused.
        self._credential_form_url = credential_form_url.rstrip("/")
        self._policy = policy
        # Who a finished connect is saved for, when that is known only once the
        # provider has named the account (None: whom begin() started it for).
        self._owners = owners
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
        """Start a connect for ``owner``: the URL to send them to — the
        provider's consent page, or, for a credential provider, the form that
        asks them for a credential. The URL is the person's alone — whoever holds it can
        finish the connect within its time, so it goes to them privately.
        ``return_to`` is kept with the link and handed back in ``Connected``."""
        source = self._provider(provider)
        if isinstance(source, CredentialProvider) and not self._credential_form_url:
            raise ProviderUnavailable(f"{provider} takes a token, and this application serves no form for one")
        # The verifier proves the OAuth exchange; for a token provider the
        # same slot keeps the secret the form must hand back.
        state, verifier = new_state(), new_code_verifier()
        await self._links.put_link(ConnectionLinkRecord(
            state_digest=state_digest(state), owner=owner, provider=provider,
            verifier_sealed=self._sealer.seal(verifier),
            expires_at=_iso(self._clock() + timedelta(seconds=self._link_ttl_s)),
            return_to=return_to,
        ))
        self._audit_and_tell(EVENT_CONNECT_START, RESULT_OK, owner=owner, provider=provider)
        if isinstance(source, CredentialProvider):
            return f"{self._credential_form_url}/{state}"
        return source.authorize_url(state=state, code_challenge=code_challenge(verifier))

    async def form_of(self, state: str) -> CredentialForm | None:
        """The form the link ``state`` names, to draw for the person: None
        when there is no such link, it has expired, or it is an OAuth
        connect's. The link stays: the form is drawn, then posted."""
        link = await self._links.peek_link(state_digest(state))
        if link is None or _parse(link.expires_at) <= self._clock():
            return None
        source = self._providers.get(link.provider)
        csrf = self._sealer.open(link.verifier_sealed)
        if not isinstance(source, CredentialProvider) or csrf is None:
            return None
        return CredentialForm(provider=link.provider, hint=source.hint, post_url=f"{self._credential_form_url}/{state}", csrf=csrf)

    async def finish_with_credential(
        self, state: str, credential: str, csrf: str, *, owner: str | None = None,
        facts: Mapping[str, str] | None = None,
    ) -> Connected:
        """Finish the connect ``state`` names with the credential the person
        entered into its form, and the secret the form handed back (a post
        without it is forged, and spends the link). ``owner`` and ``facts``
        as for ``finish``. The credential is never written down: not in the
        audit, not in the log."""
        link = await self._take_link(state, owner)
        source = self._providers.get(link.provider)
        secret = self._sealer.open(link.verifier_sealed)
        where = {"owner": link.owner, "provider": link.provider}
        if not isinstance(source, CredentialProvider) or secret is None:
            self._audit_and_tell(EVENT_CONNECT, LINK_EXPIRED, **where)
            raise LinkRefused(LINK_EXPIRED, provider=link.provider)
        if not secrets.compare_digest(csrf.encode(), secret.encode()):
            self._audit_and_tell(EVENT_CONNECT, LINK_FORGED, **where)
            raise LinkRefused(LINK_FORGED, provider=link.provider)
        try:
            account = await source.validate(credential)
        except ProviderRejected as exc:
            self._audit_and_tell(EVENT_CONNECT, CREDENTIAL_REJECTED, **where)
            raise LinkRefused(CREDENTIAL_REJECTED, provider=link.provider) from exc
        except ProviderUnavailable:
            self._audit_and_tell(EVENT_CONNECT, RESULT_UNAVAILABLE, **where)
            raise
        # Kept as the token of a grant that never refreshes and never ends.
        return await self._settle(link, account, OAuthTokens(access_token=credential), issued_by=None, facts=facts)

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
        link = await self._take_link(state, owner)
        where = {"owner": link.owner, "provider": link.provider}
        oauth = self._providers.get(link.provider)
        verifier = self._sealer.open(link.verifier_sealed)
        if not isinstance(oauth, OAuthProvider) or verifier is None:
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
        except BaseException:
            # Issued, and about to be dropped: nobody may keep using them.
            await self._revoke_tokens(oauth, tokens)
            self._audit_and_tell(EVENT_CONNECT, RESULT_UNAVAILABLE, **where)
            raise
        return await self._settle(link, account, tokens, issued_by=oauth, facts=facts)

    async def _take_link(self, state: str, owner: str | None) -> ConnectionLinkRecord:
        """Spend the link ``state`` names: a second take finds nothing."""
        link = await self._links.take_link(state_digest(state))
        if link is None or _parse(link.expires_at) <= self._clock():
            self._audit_and_tell(EVENT_CONNECT, LINK_EXPIRED, owner=owner or "")
            raise LinkRefused(LINK_EXPIRED)
        if owner is not None and link.owner != owner:
            self._audit_and_tell(EVENT_CONNECT, LINK_FOREIGN, owner=link.owner, provider=link.provider)
            raise LinkRefused(LINK_FOREIGN, provider=link.provider)
        return link

    async def _settle(
        self, link: ConnectionLinkRecord, account: ProviderAccount, tokens: OAuthTokens, *,
        issued_by: OAuthProvider | None, facts: Mapping[str, str] | None,
    ) -> Connected:
        """Store what a connect came to, once the account is known: the
        application's rule on the account, whose it is saved as, the record,
        the grant it replaces. ``issued_by`` is the provider that issued the
        tokens, to revoke them when they are not kept — None for a credential
        the person entered, which is theirs to keep whatever happens here."""
        where = {"owner": link.owner, "provider": link.provider}
        known = dict(facts or {})
        try:
            refusal = (
                await self._policy.refusal(link.owner, link.provider, account, known)
                if self._policy else None
            )
            # Whom it is saved for: whom the connect was started for, unless
            # the application names somebody else now that the account is known.
            saved_for = (
                await self._owners.owner_of(link.owner, link.provider, account, known) if self._owners else ""
            ) or link.owner
        except BaseException:
            if issued_by is not None:
                await self._revoke_tokens(issued_by, tokens)
            self._audit_and_tell(EVENT_CONNECT, RESULT_UNAVAILABLE, **where)
            raise
        if refusal is not None:
            if issued_by is not None:
                await self._revoke_tokens(issued_by, tokens)
            self._audit_and_tell(EVENT_CONNECT, ACCOUNT_REFUSED, account=account.username, **where)
            raise LinkRefused(ACCOUNT_REFUSED, provider=link.provider, account=account.username, detail=refusal)
        if saved_for != link.owner:
            where = {"owner": saved_for, "provider": link.provider, "started_by": link.owner}
        previous = await self._connections.get_connection(saved_for, link.provider)
        try:
            await self._connections.save_connection(self._record(saved_for, link.provider, tokens, account))
        except BaseException:
            if issued_by is not None:
                await self._revoke_tokens(issued_by, tokens)
            raise
        if previous is not None:
            await self._revoke_stored(previous)  # the grant this connect replaced
        self._audit_and_tell(EVENT_CONNECT, RESULT_OK, account=account.username, **where)
        connected = Connected(
            owner=saved_for, provider=link.provider, account=account, return_to=link.return_to, started_by=link.owner,
        )
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

    async def connected_at(self, owner: str, provider: str) -> datetime | None:
        """When ``owner``'s current connection to ``provider`` was made — the
        connect, not the latest refresh — for a rule that wants a recent one
        (a sign-in no older than a working day). None when there is none, or
        the record predates the engine keeping the moment."""
        record = await self._connections.get_connection(owner, provider)
        if record is None or not record.connected_at:
            return None
        return _parse(record.connected_at)

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
        if deleted and record is not None and _grant_identity(record) == (grant.connect_id, grant.version):
            await self._revoke_stored(record)
        self._audit_and_tell(
            EVENT_FORGET, RESULT_OK if deleted else RESULT_NOT_CONNECTED,
            owner=owner, provider=grant.provider, account=grant.account.username,
        )

    async def move(self, owner: str, provider: str, *, to: str) -> bool:
        """Hand ``owner``'s connection to ``provider`` to ``to`` — the one
        record, with its grant, its version and any refresh lease, under the
        new owner; nothing is copied, since a provider that rotates its
        refresh token would leave two records of one grant killing each
        other. For an application that learns whom a connection is a person's
        after it was made. False when ``owner`` has none, or ``to`` already
        has one of that provider: which to keep is the application's call
        (``disconnect`` one first)."""
        moved = await self._connections.move_connection(owner, provider, to=to)
        self._audit_and_tell(
            EVENT_MOVE, RESULT_OK if moved else RESULT_NOT_CONNECTED, owner=owner, provider=provider, to=to,
        )
        return moved

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
                revocable = isinstance(self._providers.get(provider), OAuthProvider)
                revoked = await self._revoke_stored(record) if revocable else False
                self._audit_and_tell(
                    EVENT_DISCONNECT, RESULT_OK if revoked or not revocable else RESULT_REVOKE_FAILED,
                    owner=owner, provider=provider, account=record.account_username,
                )
                return Disconnection(account=_account(record), revoked=revoked, revocable=revocable)
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
            if latest is None or _grant_identity(latest) != _grant_identity(seen):
                return

    async def _rotate(self, record: ConnectionRecord) -> ConnectionRecord:
        owner, provider = record.owner, record.provider
        where = {"owner": owner, "provider": provider, "account": record.account_username}
        oauth = self._oauth(provider)
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
            if latest is not None and _grant_identity(latest) != _grant_identity(record):
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

    def _provider(self, name: str) -> OAuthProvider | CredentialProvider:
        source = self._providers.get(name)
        if source is None:
            raise ProviderUnavailable(f"{name} is not a provider this application is configured for")
        return source

    def _oauth(self, name: str) -> OAuthProvider:
        source = self._provider(name)
        if not isinstance(source, OAuthProvider):
            raise ProviderUnavailable(f"{name} takes a credential the person entered: nothing to refresh it with")
        return source

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
            connected_at=_iso(self._clock()),
            account_extra=json.dumps(dict(account.extra), ensure_ascii=False) if account.extra else "",
        )

    async def _revoke_tokens(self, oauth: OAuthProvider, tokens: OAuthTokens) -> bool:
        return await _revoke(oauth, tokens.refresh_token or tokens.access_token)

    async def _revoke_stored(self, record: ConnectionRecord) -> bool:
        oauth = self._providers.get(record.provider)
        sealed = record.refresh_sealed or record.access_sealed
        token = self._sealer.open(sealed) if sealed else None
        if not isinstance(oauth, OAuthProvider) or token is None:
            return False  # a credential the person entered is theirs: not the engine's to end
        return await _revoke(oauth, token)


class OwnerConnections:
    """One person's connections: what a tool acting for them is handed. It can
    use them and end one after a 401; it cannot start or end a connection,
    and never learns whose they are."""

    def __init__(self, service: Connections, owner: str) -> None:
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
    service: Connections, owner_for: Callable[[str, str, str, TurnScope | None], str | None],
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

