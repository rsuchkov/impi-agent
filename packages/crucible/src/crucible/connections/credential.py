"""A ``CredentialProvider`` over HTTP: a system whose people make a credential
themselves — a personal token, an API key — and whose API says whose it is
when asked with it: a self-hosted log store, a dashboard. ``validate`` asks
that one endpoint; how the credential is carried is the system's rule
(``CredentialConfig.auth``): as a bearer, or as the user name of HTTP basic
auth with the word ``token`` for a password, which is how a log store that
knows no bearer takes it."""

import base64
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

import httpx

from crucible.connections.errors import ProviderRejected
from crucible.connections.oauth2 import account_of
from crucible.ports.connections import ProviderAccount, ProviderUnavailable

_TIMEOUT_S = 15.0
_ACCOUNT_FIELDS = {"id": "id", "username": "username", "name": "name", "email": "email"}

AUTH_BEARER = "bearer"
AUTH_BASIC = "basic"  # the credential as the user name, "token" as the password
CREDENTIAL_AUTHS = (AUTH_BEARER, AUTH_BASIC)


@dataclass(frozen=True)
class CredentialConfig:
    """One system that takes a credential: where it says whose a credential
    is (``whoami_url``), how the credential is carried there (``auth``),
    where its people make one (``hint``, for the form), and the names its
    answer gives the account's fields."""

    whoami_url: str
    hint: str = ""
    auth: Literal["bearer", "basic"] = AUTH_BEARER
    account_fields: Mapping[str, str] = field(default_factory=lambda: dict(_ACCOUNT_FIELDS))
    account_extra: tuple[str, ...] = ()


def credential_header(credential: str, auth: str) -> str:
    """The ``Authorization`` value a credential travels in, by the system's rule."""
    if auth == AUTH_BASIC:
        return "Basic " + base64.b64encode(f"{credential}:token".encode()).decode()
    return f"Bearer {credential}"


class HttpCredentialProvider:
    def __init__(self, config: CredentialConfig, *, http: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._http = http or httpx.AsyncClient(timeout=_TIMEOUT_S, follow_redirects=False)

    async def aclose(self) -> None:
        await self._http.aclose()

    @property
    def hint(self) -> str:
        return self._config.hint

    async def validate(self, credential: str) -> ProviderAccount:
        try:
            response = await self._http.get(
                self._config.whoami_url,
                headers={"Authorization": credential_header(credential, self._config.auth), "Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"the system is unreachable: {type(exc).__name__}") from exc
        if response.status_code in (401, 403):
            raise ProviderRejected("the system refused the credential")
        if response.status_code != 200:
            raise ProviderUnavailable(f"the system answered {response.status_code}")
        return account_of(response, self._config.account_fields, self._config.account_extra)
