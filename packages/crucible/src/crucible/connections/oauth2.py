"""An OAuth 2.0 provider over HTTP: authorization code with PKCE (S256), the
token endpoint for the exchange and the refresh, RFC 7009 revocation, and a
userinfo call for who the account is.

One class for every provider that speaks the standard; what differs is in
``OAuth2Config`` — the endpoints, how the client authenticates, which userinfo
fields mean what. ``gitlab()`` is the configuration for GitLab.

A token endpoint that refuses with 400 or 401 has refused the grant
(``ProviderRejected``: connect again); anything else that goes wrong —
another status, a network failure, an answer that does not parse — is
``ProviderUnavailable`` (try later). Redirects are not followed: an endpoint
that redirects is misconfigured, and a credential must not follow it.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlencode

import httpx

from crucible.connections.errors import (
    ProviderRejected,
)
from crucible.connections.types import OAuthTokens
from crucible.ports.connections import ProviderAccount, ProviderUnavailable

_TIMEOUT_S = 15.0
# Which userinfo field fills each part of a ProviderAccount, by default: the
# names most providers use.
_ACCOUNT_FIELDS = {"id": "id", "username": "username", "name": "name", "email": "email"}


@dataclass(frozen=True)
class OAuth2Config:
    authorize_url: str
    token_url: str
    userinfo_url: str
    client_id: str
    client_secret: str = field(repr=False)
    redirect_uri: str
    scope: str = ""
    revoke_url: str = ""  # "" = the provider cannot revoke; a disconnect says so
    # How the client proves itself at the token endpoint: in the form body
    # (client_secret_post) or as HTTP Basic (client_secret_basic).
    client_auth: Literal["post", "basic"] = "post"
    account_fields: Mapping[str, str] = field(default_factory=lambda: dict(_ACCOUNT_FIELDS))
    extra_authorize_params: Mapping[str, str] = field(default_factory=dict)
    # How long an access token lives when the answer does not say; None = it
    # does not expire.
    default_expires_in: int | None = None


def gitlab(
    base_url: str, *, client_id: str, client_secret: str, redirect_uri: str, scope: str = "read_api",
) -> OAuth2Config:
    """GitLab (gitlab.com or self-managed) at ``base_url``. Its tokens live two
    hours, and every refresh issues a new refresh token."""
    base = base_url.rstrip("/")
    return OAuth2Config(
        authorize_url=f"{base}/oauth/authorize", token_url=f"{base}/oauth/token",
        userinfo_url=f"{base}/api/v4/user", revoke_url=f"{base}/oauth/revoke",
        client_id=client_id, client_secret=client_secret, redirect_uri=redirect_uri, scope=scope,
        default_expires_in=7200,
    )


class OAuth2Provider:
    def __init__(self, config: OAuth2Config, *, http: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._http = http or httpx.AsyncClient(timeout=_TIMEOUT_S, follow_redirects=False)

    async def aclose(self) -> None:
        await self._http.aclose()

    def authorize_url(self, *, state: str, code_challenge: str) -> str:
        params = {
            "response_type": "code",
            "client_id": self._config.client_id,
            "redirect_uri": self._config.redirect_uri,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            **({"scope": self._config.scope} if self._config.scope else {}),
            **self._config.extra_authorize_params,
        }
        return f"{self._config.authorize_url}?{urlencode(params)}"

    async def exchange(self, code: str, code_verifier: str) -> OAuthTokens:
        return await self._token({
            "grant_type": "authorization_code", "code": code,
            "code_verifier": code_verifier, "redirect_uri": self._config.redirect_uri,
        })

    async def refresh(self, refresh_token: str) -> OAuthTokens:
        return await self._token({
            "grant_type": "refresh_token", "refresh_token": refresh_token,
            "redirect_uri": self._config.redirect_uri,
        })

    async def revoke(self, token: str) -> None:
        if not self._config.revoke_url:
            raise ProviderUnavailable("this provider has no revocation endpoint")
        response = await self._post(self._config.revoke_url, {"token": token})
        if response.status_code != 200:
            raise ProviderUnavailable(f"revoke answered {response.status_code}")

    async def identify(self, access_token: str) -> ProviderAccount:
        try:
            response = await self._http.get(
                self._config.userinfo_url, headers={"Authorization": f"Bearer {access_token}"},
            )
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"userinfo unreachable: {type(exc).__name__}") from exc
        if response.status_code == 401:
            raise ProviderRejected("userinfo refused the token")
        if response.status_code != 200:
            raise ProviderUnavailable(f"userinfo answered {response.status_code}")
        try:
            data = response.json()
            names = self._config.account_fields
            return ProviderAccount(
                id=str(data[names["id"]]),
                username=str(data.get(names["username"]) or ""),
                name=str(data.get(names["name"]) or ""),
                email=str(data.get(names["email"]) or ""),
            )
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ProviderUnavailable("userinfo answer does not parse") from exc

    async def _token(self, form: dict[str, str]) -> OAuthTokens:
        response = await self._post(self._config.token_url, form)
        if response.status_code in (400, 401):
            raise ProviderRejected(_error_of(response) or str(response.status_code))
        if response.status_code != 200:
            raise ProviderUnavailable(f"token endpoint answered {response.status_code}")
        try:
            data = response.json()
            expires_in = data.get("expires_in", self._config.default_expires_in)
            return OAuthTokens(
                access_token=str(data["access_token"]),
                refresh_token=str(data.get("refresh_token") or ""),
                expires_in=int(expires_in) if expires_in is not None else None,
                scope=str(data.get("scope") or ""),
            )
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ProviderUnavailable("token answer does not parse") from exc

    async def _post(self, url: str, form: dict[str, str]) -> httpx.Response:
        headers = {"Accept": "application/json"}
        try:
            if self._config.client_auth == "basic":
                auth = httpx.BasicAuth(self._config.client_id, self._config.client_secret)
                return await self._http.post(url, data=form, auth=auth, headers=headers)
            secret = {"client_id": self._config.client_id, "client_secret": self._config.client_secret}
            return await self._http.post(url, data={**form, **secret}, headers=headers)
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"{url} unreachable: {type(exc).__name__}") from exc


def _error_of(response: httpx.Response) -> str:
    try:
        body: Any = response.json()
    except ValueError:
        return ""
    return str(body.get("error") or "") if isinstance(body, dict) else ""
