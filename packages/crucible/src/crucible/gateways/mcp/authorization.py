"""The MCP gateway's authorization server: how a client program gets a token
a person signed in for.

The protocol's authorization is OAuth 2.1: a client learns from the gateway's
protected-resource metadata where to authorize, sends the person's browser to
``/authorize``, and exchanges the code it gets for tokens at ``/token``. Here
the person proves who they are by signing in at one of the application's
connection providers — an ephemeral connect through ``Connections``, which
names the account and keeps nothing — then sees who asks for what and allows
or declines (``ConsentPage``). The grant stands from the sign-in for
``grant_ttl_s``; access tokens are short and refreshed with a rotating refresh
token, a refresh token presented twice ends the grant.

Clients are not registered dynamically: the operator lists them
(``ClientRegistration``) with the redirect addresses each may be sent back to,
loopback addresses on any port by default. A client not on the list, or a
redirect not its own, is refused on a page — never with a redirect — since
the redirect is exactly what is in question.

Tokens are random and kept as digests; nothing here is written down in the
clear, and the audit (``crucible.gateways.mcp.audit`` logger) names clients
and subjects, never tokens, codes or states.
"""

import hashlib
import html
import logging
import secrets
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit

from aiohttp import web

from crucible.connections.audit import LoggingAudit
from crucible.connections.pkce import code_challenge as challenge_of
from crucible.connections.ports import ConnectionsAudit
from crucible.connections.types import Connected
from crucible.gateways.mcp.auth import SCOPE_READ, SCOPES, Principal
from crucible.gateways.mcp.hub import MCP_PATH, RESOURCE_METADATA_PATH
from crucible.ports.connections import ProviderUnavailable
from crucible.ports.mcp_grants import McpAuthRequest, McpGrant, McpGrants
from crucible.ports.telemetry import McpGrantChanged, Telemetry

logger = logging.getLogger(__name__)

AUDIT_LOGGER = "crucible.gateways.mcp.audit"
AS_METADATA_PATH = "/.well-known/oauth-authorization-server"
AUTHORIZE_PATH = "/authorize"
SIGN_IN_PATH = "/sign-in"
CONSENT_PATH = "/consent"
TOKEN_PATH = "/token"
REVOKE_PATH = "/revoke"
# The owner an ephemeral connect is started for: the sign-in of this request.
SIGN_IN_OWNER_PREFIX = "mcp-sign-in:"
# The audit's events and results.
EVENT_AUTHORIZE = "authorize"
EVENT_SIGN_IN = "sign_in"
EVENT_CONSENT = "consent"
EVENT_TOKEN = "token"
EVENT_REFRESH = "refresh"
EVENT_REVOKE = "revoke"
RESULT_OK = "ok"
RESULT_DENIED = "denied"  # the person declined
RESULT_REFUSED = "refused"  # the request did not hold up (PKCE, the code, the client)
RESULT_REUSED = "reused"  # a refresh token presented again: the grant ends
RESULT_EXPIRED = "expired"
RESULT_UNKNOWN_CLIENT = "unknown_client"
RESULT_UNKNOWN_REDIRECT = "unknown_redirect"
RESULT_INVALID = "invalid"
GRANT_TTL_S = 12 * 3600
ACCESS_TTL_S = 3600
REQUEST_TTL_S = 600
CODE_TTL_S = 60
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_SAFE_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'",
}
_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:32rem;margin:15vh auto;padding:0 1rem;color:#222}}
h1{{font-size:1.3rem}} button{{font:inherit;padding:.4rem 1.2rem;margin-right:.6rem}} ul{{padding-left:1.2rem}}</style></head>
<body><h1>{title}</h1>{body}</body></html>
"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _page(status: int, title: str, body_html: str) -> web.Response:
    text = _PAGE.format(title=html.escape(title), body=body_html)
    return web.Response(status=status, text=text, content_type="text/html", headers=_SAFE_HEADERS)


def _refusal(status: int, text: str) -> web.Response:
    return _page(status, "Not authorized", f"<p>{html.escape(text)}</p>")


def _with_query(url: str, **params: str) -> str:
    """``url`` with ``params`` added to its query (RFC 6749 §4.1.2: the
    redirect may carry a query of its own)."""
    parts = urlsplit(url)
    query = parts.query + ("&" if parts.query else "") + urlencode({k: v for k, v in params.items() if v})
    return parts._replace(query=query, fragment="").geturl()


@dataclass(frozen=True)
class ClientRegistration:
    """One client program the operator lets in: its name for the consent
    page, the redirect addresses it may be sent back to — exactly as it will
    present them — and whether a loopback address (``http://127.0.0.1``,
    ``localhost``, ``[::1]``, any port, any path) is one of them, as it is for
    a client that runs on the person's own machine (RFC 8252)."""

    name: str
    redirect_uris: tuple[str, ...] = ()
    loopback: bool = True

    def allows(self, redirect_uri: str) -> bool:
        if redirect_uri in self.redirect_uris:
            return True
        if not self.loopback or not redirect_uri:
            return False
        parts = urlsplit(redirect_uri)
        return parts.scheme == "http" and (parts.hostname or "") in _LOOPBACK_HOSTS


@dataclass(frozen=True)
class ConsentRequest:
    """What the consent page shows the person: which client asks (its id — a
    URL for a client that publishes its metadata — and the operator's name
    for it), for which scopes, as whom they signed in, and the form to answer
    with: ``post_url`` takes ``decision`` (``allow`` or ``deny``) and ``csrf``,
    the form's own secret. The names are the client's and the provider's
    words: a page that puts them in HTML escapes them."""

    client: str
    client_name: str
    scopes: tuple[str, ...]
    subject: str
    username: str
    post_url: str
    csrf: str


class SignIn(Protocol):
    """What the server needs of the application's connections to sign a
    person in: the providers one may sign in at, and a connect started as a
    sign-in (``Connections`` is one)."""

    @property
    def providers(self) -> tuple[str, ...]: ...

    async def begin(self, owner: str, provider: str, *, return_to: str = "", ephemeral: bool = False) -> str: ...


class ConsentPage(Protocol):
    """The application's consent page. The response gets the flow's safety
    headers where it set none, as the connections callback's pages do."""

    async def render(self, consent: ConsentRequest, request: web.Request) -> web.StreamResponse: ...


class DefaultConsentPage:
    """The plain consent page: who asks, as whom, for what; two buttons."""

    async def render(self, consent: ConsentRequest, request: web.Request) -> web.StreamResponse:
        who = html.escape(consent.username or consent.subject)
        client = html.escape(consent.client_name or consent.client)
        scopes = "".join(f"<li>{html.escape(_SCOPE_WORDS.get(s, s))}</li>" for s in consent.scopes)
        body = (
            f"<p><strong>{client}</strong> ({html.escape(consent.client)}) asks to use the agent as <strong>{who}</strong>, "
            f"and would be able to:</p><ul>{scopes}</ul>"
            f'<form method="post" action="{html.escape(consent.post_url, quote=True)}">'
            f'<input type="hidden" name="csrf" value="{html.escape(consent.csrf, quote=True)}">'
            '<button type="submit" name="decision" value="allow">Allow</button>'
            '<button type="submit" name="decision" value="deny">Deny</button></form>'
        )
        return _page(200, f"Allow {consent.client_name or consent.client}?", body)


_SCOPE_WORDS = {
    SCOPE_READ: "ask the agent, and use the tools that change nothing (read)",
    "act": "use the tools that act, each asking you for a yes first (act)",
}


def _subject_of(connected: Connected) -> tuple[str, str]:
    """The default: the provider's name and the account's id, and the
    account's username for the prompt."""
    account = connected.account
    return f"{connected.provider}:{account.id}", account.username or account.name or account.email or account.id


class AuthorizationServer:
    """``Authorization`` for the hub, the routes of the server itself
    (``routes``, mounted on the hub's listener), and the ``SignIns`` the
    connections callback hands a finished sign-in to. ``public_url`` is where
    clients reach the listener from outside: the issuer, and the base of
    every address in the metadata. ``sign_in_providers`` are the connection
    providers a person may sign in at (every one the application has, when
    None); ``subject_of`` turns the account a sign-in named into the subject
    the gateway runs turns as and the name the prompt shows."""

    def __init__(
        self,
        grants: McpGrants,
        connections: SignIn,
        *,
        public_url: str,
        clients: Mapping[str, ClientRegistration],
        sign_in_providers: Sequence[str] | None = None,
        consent_page: ConsentPage | None = None,
        subject_of: Callable[[Connected], tuple[str, str]] = _subject_of,
        grant_ttl_s: float = GRANT_TTL_S,
        access_ttl_s: float = ACCESS_TTL_S,
        request_ttl_s: float = REQUEST_TTL_S,
        code_ttl_s: float = CODE_TTL_S,
        audit: ConnectionsAudit | None = None,
        telemetry: Telemetry | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._grants = grants
        self._connections = connections
        self._public_url = public_url.rstrip("/")
        self._clients = dict(clients)
        self._providers = tuple(sign_in_providers) if sign_in_providers is not None else connections.providers
        self._consent: ConsentPage = consent_page if consent_page is not None else DefaultConsentPage()
        self._subject_of = subject_of
        self._grant_ttl_s = grant_ttl_s
        self._access_ttl_s = access_ttl_s
        self._request_ttl_s = request_ttl_s
        self._code_ttl_s = code_ttl_s
        self._audit = audit or LoggingAudit(logging.getLogger(AUDIT_LOGGER))
        self._telemetry = telemetry
        self._clock = clock

    @property
    def resource(self) -> str:
        """The protected resource's identifier: the gateway's address."""
        return f"{self._public_url}{MCP_PATH}"

    @property
    def issuer(self) -> str:
        return self._public_url

    def routes(self) -> list[web.RouteDef]:
        return [
            web.get(RESOURCE_METADATA_PATH, self._resource_metadata),
            web.get(AS_METADATA_PATH, self._server_metadata),
            web.get(AUTHORIZE_PATH, self._authorize),
            web.get(SIGN_IN_PATH + "/{request}", self._sign_in),
            web.get(CONSENT_PATH + "/{request}", self._consent_form),
            web.post(CONSENT_PATH + "/{request}", self._consent_answer),
            web.post(TOKEN_PATH, self._token),
            web.post(REVOKE_PATH, self._revoke),
        ]

    # -- Authorization (the hub asks) -----------------------------------------------

    async def authenticate(self, request: web.Request) -> Principal | None:
        token = bearer_of(request.headers)
        if not token:
            return None
        grant = await self._grants.get_mcp_grant_by_access(_digest(token))
        if grant is None or self._past(grant.access_expires_at) or self._past(grant.expires_at):
            return None
        if grant.client not in self._clients:
            return None  # a client struck off the list is out, tokens or no tokens
        return Principal(
            subject=grant.subject, client=grant.client, scopes=frozenset(grant.scope.split()), username=grant.username,
        )

    # -- metadata ------------------------------------------------------------------

    async def _resource_metadata(self, request: web.Request) -> web.Response:
        return web.json_response({
            "resource": self.resource,
            "authorization_servers": [self.issuer],
            "scopes_supported": list(SCOPES),
            "bearer_methods_supported": ["header"],
        })

    async def _server_metadata(self, request: web.Request) -> web.Response:
        base = self._public_url
        return web.json_response({
            "issuer": self.issuer,
            "authorization_endpoint": base + AUTHORIZE_PATH,
            "token_endpoint": base + TOKEN_PATH,
            "revocation_endpoint": base + REVOKE_PATH,
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "revocation_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": list(SCOPES),
            "client_id_metadata_document_supported": True,
        })

    # -- /authorize ------------------------------------------------------------------

    async def _authorize(self, request: web.Request) -> web.StreamResponse:
        query = request.query
        client_id = query.get("client_id", "")
        registration = self._clients.get(client_id)
        if registration is None:
            self._tell(EVENT_AUTHORIZE, RESULT_UNKNOWN_CLIENT, client=client_id)
            return _refusal(400, "This client is not known here. The operator lists the clients that may connect.")
        redirect_uri = query.get("redirect_uri", "")
        if not registration.allows(redirect_uri):
            self._tell(EVENT_AUTHORIZE, RESULT_UNKNOWN_REDIRECT, client=client_id)
            return _refusal(400, "The client asked to be sent back to an address that is not registered for it.")
        state = query.get("state", "")

        def back(error: str, description: str) -> web.Response:
            self._tell(EVENT_AUTHORIZE, RESULT_INVALID, client=client_id, error=error)
            raise web.HTTPFound(
                _with_query(redirect_uri, error=error, error_description=description, state=state, iss=self.issuer),
                headers=_SAFE_HEADERS,
            )

        if query.get("response_type") != "code":
            back("unsupported_response_type", "only response_type=code is supported")
        code_challenge = query.get("code_challenge", "")
        if query.get("code_challenge_method") != "S256" or not (43 <= len(code_challenge) <= 128):
            back("invalid_request", "PKCE with S256 is required")
        resource = query.get("resource", "")
        if resource and resource != self.resource:
            back("invalid_target", f"this server is {self.resource}")
        scopes = tuple(dict.fromkeys(query.get("scope", "").split())) or (SCOPE_READ,)
        if any(scope not in SCOPES for scope in scopes):
            back("invalid_scope", f"the scopes here are {', '.join(SCOPES)}")
        now = self._clock()
        record = McpAuthRequest(
            id=secrets.token_urlsafe(24), client=client_id, redirect_uri=redirect_uri, code_challenge=code_challenge,
            scope=" ".join(scopes), resource=resource, state=state,
            expires_at=_iso(now + timedelta(seconds=self._request_ttl_s)), secret=secrets.token_urlsafe(24),
        )
        await self._grants.put_mcp_request(record)
        self._tell(EVENT_AUTHORIZE, RESULT_OK, client=client_id, scope=record.scope)
        if len(self._providers) == 1:
            return await self._start_sign_in(record, self._providers[0])
        return self._choose_provider(record)

    def _choose_provider(self, record: McpAuthRequest) -> web.Response:
        if not self._providers:
            return _refusal(503, "Nobody can sign in here: this engine has no provider to sign in at.")
        links = "".join(
            f'<li><a href="{html.escape(f"{self._public_url}{SIGN_IN_PATH}/{record.id}?provider={name}", quote=True)}">'
            f"{html.escape(name)}</a></li>"
            for name in self._providers
        )
        return _page(200, "Sign in", f"<p>Sign in to let {html.escape(self._name_of(record.client))} use the agent as you:</p><ul>{links}</ul>")

    async def _sign_in(self, request: web.Request) -> web.StreamResponse:
        record = await self._pending(request.match_info["request"])
        provider = request.query.get("provider", "")
        if record is None or record.subject:
            return _refusal(400, "This sign-in has expired or was already made. Start again from your client.")
        if provider not in self._providers:
            return _refusal(400, "Not a provider one can sign in at here.")
        return await self._start_sign_in(record, provider)

    async def _start_sign_in(self, record: McpAuthRequest, provider: str) -> web.StreamResponse:
        try:
            url = await self._connections.begin(
                SIGN_IN_OWNER_PREFIX + record.id, provider, return_to=record.id, ephemeral=True,
            )
        except ProviderUnavailable:
            logger.warning("mcp authorization: %s cannot take a sign-in", provider, exc_info=True)
            return _refusal(503, f"Signing in at {provider} is not possible right now. Try again later.")
        raise web.HTTPSeeOther(url, headers=_SAFE_HEADERS)

    async def signed_in(self, connected: Connected, request: web.Request) -> web.StreamResponse:
        """``SignIns``: the person came back from the provider, the account is
        known, the request is theirs now, and the consent page is next."""
        record = await self._pending(connected.return_to)
        if record is None or record.subject:
            self._tell(EVENT_SIGN_IN, RESULT_EXPIRED, client=record.client if record else "")
            return _refusal(400, "This sign-in has expired or was already made. Start again from your client.")
        subject, username = self._subject_of(connected)
        if not subject:
            self._tell(EVENT_SIGN_IN, RESULT_REFUSED, client=record.client)
            return _refusal(403, "The account you signed in with cannot use the agent here.")
        await self._grants.put_mcp_request(replace(record, subject=subject, username=username))
        self._tell(EVENT_SIGN_IN, RESULT_OK, client=record.client, subject=subject, provider=connected.provider)
        raise web.HTTPSeeOther(f"{self._public_url}{CONSENT_PATH}/{record.id}", headers=_SAFE_HEADERS)

    # -- /consent ----------------------------------------------------------------------

    async def _consent_form(self, request: web.Request) -> web.StreamResponse:
        record = await self._pending(request.match_info["request"])
        if record is None or not record.subject or record.code_hash:
            return _refusal(400, "This request has expired or was already answered. Start again from your client.")
        response = await self._consent.render(self._consent_request(record), request)
        for name, value in _SAFE_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    def _consent_request(self, record: McpAuthRequest) -> ConsentRequest:
        return ConsentRequest(
            client=record.client, client_name=self._name_of(record.client), scopes=tuple(record.scope.split()),
            subject=record.subject, username=record.username,
            post_url=f"{self._public_url}{CONSENT_PATH}/{record.id}", csrf=record.secret,
        )

    async def _consent_answer(self, request: web.Request) -> web.StreamResponse:
        record = await self._pending(request.match_info["request"])
        if record is None or not record.subject or record.code_hash:
            return _refusal(400, "This request has expired or was already answered. Start again from your client.")
        posted = await request.post()
        csrf = str(posted.get("csrf") or "")
        if not secrets.compare_digest(csrf.encode(), record.secret.encode()):
            # Not the form this server drew: the request is spent, whoever posted.
            await self._grants.delete_mcp_request(record.id)
            self._tell(EVENT_CONSENT, RESULT_REFUSED, client=record.client, subject=record.subject)
            return _refusal(403, "This answer did not come from the consent page. Start again from your client.")
        if str(posted.get("decision") or "") != "allow":
            await self._grants.delete_mcp_request(record.id)
            self._tell(EVENT_CONSENT, RESULT_DENIED, client=record.client, subject=record.subject)
            raise web.HTTPFound(
                _with_query(record.redirect_uri, error="access_denied", state=record.state, iss=self.issuer),
                headers=_SAFE_HEADERS,
            )
        code = secrets.token_urlsafe(32)
        await self._grants.put_mcp_request(replace(
            record, code_hash=_digest(code), code_expires_at=_iso(self._clock() + timedelta(seconds=self._code_ttl_s)),
        ))
        self._tell(EVENT_CONSENT, RESULT_OK, client=record.client, subject=record.subject, scope=record.scope)
        raise web.HTTPFound(
            _with_query(record.redirect_uri, code=code, state=record.state, iss=self.issuer), headers=_SAFE_HEADERS
        )

    # -- /token and /revoke ------------------------------------------------------------

    async def _token(self, request: web.Request) -> web.Response:
        posted = await request.post()
        form = {key: str(posted.get(key) or "") for key in (
            "grant_type", "code", "redirect_uri", "client_id", "code_verifier", "resource", "refresh_token",
        )}
        if form["grant_type"] == "authorization_code":
            return await self._exchange(form)
        if form["grant_type"] == "refresh_token":
            return await self._refresh(form)
        return _token_error("unsupported_grant_type", "authorization_code or refresh_token")

    async def _exchange(self, form: Mapping[str, str]) -> web.Response:
        record = await self._grants.take_mcp_request_by_code(_digest(form["code"])) if form["code"] else None
        if record is None:
            self._tell(EVENT_TOKEN, RESULT_REFUSED, client=form["client_id"])
            return _token_error("invalid_grant", "the code is not one this server issued, or was used")
        holds = (
            form["client_id"] == record.client
            and form["redirect_uri"] == record.redirect_uri
            and challenge_of(form["code_verifier"]) == record.code_challenge
            and (not form["resource"] or form["resource"] == self.resource)
            and not self._past(record.code_expires_at)
            and not self._past(record.expires_at)
        ) if 43 <= len(form["code_verifier"]) <= 128 else False
        if not holds:
            self._tell(EVENT_TOKEN, RESULT_REFUSED, client=record.client, subject=record.subject)
            return _token_error("invalid_grant", "the code does not belong to this client, redirect and verifier")
        now = self._clock()
        grant = McpGrant(
            id=uuid.uuid4().hex, subject=record.subject, username=record.username, client=record.client,
            scope=record.scope, signed_in_at=_iso(now), expires_at=_iso(now + timedelta(seconds=self._grant_ttl_s)),
            access_hash="", access_expires_at="", refresh_hash="",
        )
        grant, access, refresh = self._rotated(grant, now)
        await self._grants.save_mcp_grant(grant)
        self._tell(EVENT_TOKEN, RESULT_OK, client=grant.client, subject=grant.subject, scope=grant.scope)
        return self._tokens(grant, access, refresh, now)

    async def _refresh(self, form: Mapping[str, str]) -> web.Response:
        presented = _digest(form["refresh_token"]) if form["refresh_token"] else ""
        grant = await self._grants.get_mcp_grant_by_refresh(presented) if presented else None
        if grant is None:
            self._tell(EVENT_REFRESH, RESULT_REFUSED, client=form["client_id"])
            return _token_error("invalid_grant", "the refresh token is not one this server issued")
        if presented == grant.previous_refresh_hash:
            # The token before the current one, presented again: somebody has
            # a copy. Nobody keeps the grant.
            await self._grants.delete_mcp_grant(grant.id)
            self._tell(EVENT_REFRESH, RESULT_REUSED, client=grant.client, subject=grant.subject)
            return _token_error("invalid_grant", "the refresh token was already used; the grant is revoked")
        if form["client_id"] != grant.client:
            self._tell(EVENT_REFRESH, RESULT_REFUSED, client=form["client_id"], subject=grant.subject)
            return _token_error("invalid_grant", "the refresh token belongs to another client")
        now = self._clock()
        if self._past(grant.expires_at):
            await self._grants.delete_mcp_grant(grant.id)
            self._tell(EVENT_REFRESH, RESULT_EXPIRED, client=grant.client, subject=grant.subject)
            return _token_error("invalid_grant", "the grant has expired: sign in again")
        rotated, access, refresh = self._rotated(grant, now)
        if not await self._grants.replace_mcp_grant(rotated, expected_refresh_hash=grant.refresh_hash):
            self._tell(EVENT_REFRESH, RESULT_REFUSED, client=grant.client, subject=grant.subject)
            return _token_error("invalid_grant", "the refresh token was rotated meanwhile")
        self._tell(EVENT_REFRESH, RESULT_OK, client=grant.client, subject=grant.subject)
        return self._tokens(rotated, access, refresh, now)

    def _rotated(self, grant: McpGrant, now: datetime) -> tuple[McpGrant, str, str]:
        """New tokens for the grant; the access token ends with the grant at
        the latest."""
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        until = min(now + timedelta(seconds=self._access_ttl_s), datetime.fromisoformat(grant.expires_at))
        rotated = replace(
            grant, access_hash=_digest(access), access_expires_at=_iso(until),
            refresh_hash=_digest(refresh), previous_refresh_hash=grant.refresh_hash,
        )
        return rotated, access, refresh

    def _tokens(self, grant: McpGrant, access: str, refresh: str, now: datetime) -> web.Response:
        expires_in = max(0, int((datetime.fromisoformat(grant.access_expires_at) - now).total_seconds()))
        return web.json_response(
            {
                "access_token": access, "token_type": "Bearer", "expires_in": expires_in,
                "refresh_token": refresh, "scope": grant.scope,
            },
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )

    async def _revoke(self, request: web.Request) -> web.Response:
        posted = await request.post()
        token = str(posted.get("token") or "")
        if token:
            digest = _digest(token)
            grant = await self._grants.get_mcp_grant_by_access(digest) or await self._grants.get_mcp_grant_by_refresh(digest)
            if grant is not None:
                await self._grants.delete_mcp_grant(grant.id)
                self._tell(EVENT_REVOKE, RESULT_OK, client=grant.client, subject=grant.subject)
        # RFC 7009: a token already gone, or never issued, is revoked all the same.
        return web.Response(status=200, headers={"Cache-Control": "no-store"})

    # -- plumbing ----------------------------------------------------------------------

    async def _pending(self, request_id: str) -> McpAuthRequest | None:
        """The authorization in progress, or None when there is none or it is past its time."""
        record = await self._grants.get_mcp_request(request_id) if request_id else None
        if record is None or self._past(record.expires_at):
            return None
        return record

    def _past(self, moment: str) -> bool:
        return not moment or datetime.fromisoformat(moment) <= self._clock()

    def _name_of(self, client: str) -> str:
        registration = self._clients.get(client)
        return registration.name if registration is not None else client

    def _tell(self, event: str, result: str, **fields: str) -> None:
        self._audit.record(event, result, **fields)
        if self._telemetry is None:
            return
        try:
            self._telemetry.record(McpGrantChanged(client=fields.get("client", ""), event=event, result=result))
        except Exception:  # noqa: BLE001 - telemetry must never cost the authorization
            logger.warning("telemetry failed to record an authorization event", exc_info=True)


def _token_error(error: str, description: str) -> web.Response:
    return web.json_response(
        {"error": error, "error_description": description}, status=400, headers={"Cache-Control": "no-store"}
    )


def bearer_of(headers: Mapping[str, Any]) -> str:
    """The bearer token on a request, "" when it carries none."""
    return str(headers.get("Authorization", "")).removeprefix("Bearer ").strip()
