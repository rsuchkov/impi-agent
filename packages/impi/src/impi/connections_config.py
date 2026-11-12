"""The providers a deployment lets people connect, from its settings: OAuth
ones, and systems that take a credential the person makes themselves.

One family of keys per field, the provider's name last — the shape of the
per-agent families (``AGENTS_MM_TOKEN__<AGENT>``), keyed by a provider the
operator names instead of an agent:

    CONNECTION_CLIENT_ID__GITLAB=…          names an OAuth provider (required for gitlab | oauth2)
    CONNECTION_CLIENT_SECRET__GITLAB=…      (required for gitlab | oauth2)
    CONNECTION_KIND__GITLAB=gitlab          gitlab (default) | oauth2 | credential
    CONNECTION_KIND__GRAYLOG=credential     names a credential provider: no client, a form instead
    CONNECTION_WHOAMI_URL__GRAYLOG=…        credential: where the system says whose a credential is (required)
    CONNECTION_CREDENTIAL_AUTH__GRAYLOG=basic   credential: bearer (default) | basic — how it is carried there
    CONNECTION_CREDENTIAL_HINT__GRAYLOG=…       credential: where one comes from, shown on the form
    CONNECTION_BASE_URL__GITLAB=https://gitlab.example.com      for gitlab
    CONNECTION_AUTHORIZE_URL__… / _TOKEN_URL__… / _USERINFO_URL__… / _REVOKE_URL__…   for oauth2
    CONNECTION_SCOPE__GITLAB=read_api
    CONNECTION_API_URL__GITLAB=…            where tools call; gitlab: <base>/api/v4
    CONNECTION_MATCH_EMAIL__GITLAB=true     the account's email must be the chat user's
    CONNECTION_AGENTS__GITLAB=assistant,…   which agents may use it; unset = all
    CONNECTION_ACCOUNT_EXTRA__GITLAB=groups,…   userinfo fields kept beside the account's four

A name is the key's suffix lower-cased, ``_`` read as ``-``: ``GITLAB`` is
``gitlab``. Read from the environment and the .env file, the environment
winning — the same rule as the rest of the settings.
"""

from collections.abc import Mapping
from dataclasses import dataclass, replace

from crucible.connections.credential import (
    AUTH_BASIC,
    AUTH_BEARER,
    CREDENTIAL_AUTHS,
    CredentialConfig,
)
from crucible.connections.oauth2 import OAuth2Config, gitlab

_PREFIX = "CONNECTION_"
_KINDS = ("gitlab", "oauth2", "credential")
KIND_CREDENTIAL = "credential"

# Gateways whose user id is whatever the calling program says: one program's
# "alice" cannot be told from another's, so nobody there may act as anyone's
# connected account.
ASSERTED_IDENTITY_GATEWAYS = frozenset({"http", "ws"})

# The word the connections screen answers to, unless CONNECTIONS_COMMAND says
# another.
DEFAULT_COMMAND = "connections"


def command_word(raw: str) -> str:
    """CONNECTIONS_COMMAND as the screen binds it: no slash, lower case."""
    return raw.lstrip("/").strip().lower() or DEFAULT_COMMAND


# The settings families an agent's process may never inherit by pattern: they
# hold every provider's client secret and the sealing keys.
CONNECTION_SECRET_PREFIXES = (_PREFIX, "CONNECTIONS_")


@dataclass(frozen=True)
class ConnectionProvider:
    name: str
    kind: str
    client_id: str
    client_secret: str
    base_url: str = ""
    authorize_url: str = ""
    token_url: str = ""
    userinfo_url: str = ""
    revoke_url: str = ""
    scope: str = ""
    api_url: str = ""
    match_email: bool = True
    agents: tuple[str, ...] | None = None  # None = every agent
    # Fields of the provider's userinfo answer kept beside the account's four
    # (``ProviderAccount.extra``): a groups claim, a department.
    account_extra: tuple[str, ...] = ()
    # A credential provider: where the system says whose a credential is,
    # how it is carried there, and where its people make one.
    whoami_url: str = ""
    credential_auth: str = AUTH_BEARER
    credential_hint: str = ""

    @property
    def takes_credential(self) -> bool:
        return self.kind == KIND_CREDENTIAL

    def allows(self, agent: str) -> bool:
        return self.agents is None or agent in self.agents

    def credential_config(self) -> CredentialConfig:
        return CredentialConfig(
            whoami_url=self.whoami_url, hint=self.credential_hint,
            auth="basic" if self.credential_auth == AUTH_BASIC else "bearer", account_extra=self.account_extra,
        )

    def oauth2_config(self, redirect_uri: str) -> OAuth2Config:
        if self.kind == "gitlab":
            config = gitlab(
                self.base_url, client_id=self.client_id, client_secret=self.client_secret,
                redirect_uri=redirect_uri, **({"scope": self.scope} if self.scope else {}),
            )
            return replace(config, account_extra=self.account_extra) if self.account_extra else config
        return OAuth2Config(
            authorize_url=self.authorize_url, token_url=self.token_url, userinfo_url=self.userinfo_url,
            revoke_url=self.revoke_url, client_id=self.client_id, client_secret=self.client_secret,
            redirect_uri=redirect_uri, scope=self.scope, account_extra=self.account_extra,
        )


def _key(field: str, suffix: str) -> str:
    return f"{_PREFIX}{field}__{suffix}"


def _bool(key: str, raw: str) -> bool:
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{key}: {raw!r} is not true or false")


def connection_providers(values: Mapping[str, str]) -> dict[str, ConnectionProvider]:
    """Every provider ``values`` names (environment over .env, merged by the
    caller), checked. ValueError naming the key for one that is not usable."""
    found: dict[str, ConnectionProvider] = {}
    # An OAuth provider is named by its client id; a credential provider has
    # no client and is named by its kind.
    suffixes = {
        key[len(marker):]
        for marker, wanted in ((_key("CLIENT_ID", ""), None), (_key("KIND", ""), KIND_CREDENTIAL))
        for key, raw in values.items()
        if key.startswith(marker) and raw.strip() and (wanted is None or raw.strip().lower() == wanted)
    }
    for suffix in sorted(suffixes):
        name = suffix.lower().replace("_", "-")

        def get(field: str, default: str = "") -> str:
            return (values.get(_key(field, suffix)) or default).strip()

        kind = get("KIND", "gitlab").lower()
        if kind not in _KINDS:
            raise ValueError(f"{_key('KIND', suffix)}: {kind!r} is not one of {', '.join(_KINDS)}")
        client_id = get("CLIENT_ID")
        secret = get("CLIENT_SECRET")
        if kind != KIND_CREDENTIAL and not client_id:
            raise ValueError(f"{_key('CLIENT_ID', suffix)} is required for a {kind} provider")
        if kind != KIND_CREDENTIAL and not secret:
            raise ValueError(f"{_key('CLIENT_SECRET', suffix)} is required beside {_key('CLIENT_ID', suffix)}")
        base_url = get("BASE_URL").rstrip("/")
        endpoints = {f: get(f.upper()) for f in ("authorize_url", "token_url", "userinfo_url", "revoke_url")}
        if kind == "gitlab" and not base_url:
            raise ValueError(f"{_key('BASE_URL', suffix)} is required for a gitlab provider")
        if kind == "oauth2":
            for field in ("authorize_url", "token_url", "userinfo_url"):
                if not endpoints[field]:
                    raise ValueError(f"{_key(field.upper(), suffix)} is required for an oauth2 provider")
        credential_auth = get("CREDENTIAL_AUTH", AUTH_BEARER).lower()
        if kind == KIND_CREDENTIAL:
            for field in ("WHOAMI_URL", "API_URL"):
                if not get(field):
                    raise ValueError(f"{_key(field, suffix)} is required for a credential provider")
            if credential_auth not in CREDENTIAL_AUTHS:
                raise ValueError(f"{_key('CREDENTIAL_AUTH', suffix)}: {credential_auth!r} is not one of {', '.join(CREDENTIAL_AUTHS)}")
        raw_agents = values.get(_key("AGENTS", suffix))
        agents = (
            tuple(a.strip() for a in raw_agents.split(",") if a.strip()) if raw_agents is not None else None
        )
        match = get("MATCH_EMAIL")
        found[name] = ConnectionProvider(
            name=name, kind=kind, client_id=client_id, client_secret=secret,
            base_url=base_url, scope=get("SCOPE"),
            api_url=get("API_URL") or (f"{base_url}/api/v4" if kind == "gitlab" else ""),
            match_email=_bool(_key("MATCH_EMAIL", suffix), match) if match else True,
            agents=agents,
            account_extra=tuple(dict.fromkeys(f.strip() for f in get("ACCOUNT_EXTRA").split(",") if f.strip())),
            whoami_url=get("WHOAMI_URL"), credential_auth=credential_auth, credential_hint=get("CREDENTIAL_HINT"),
            **endpoints,
        )
    return found
