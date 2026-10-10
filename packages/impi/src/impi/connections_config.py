"""The OAuth providers a deployment lets people connect, from its settings.

One family of keys per field, the provider's name last — the same shape as
every other per-name family here (``AGENTS_MM_TOKEN__<AGENT>``):

    CONNECTION_CLIENT_ID__GITLAB=…          names the provider (required)
    CONNECTION_CLIENT_SECRET__GITLAB=…      (required)
    CONNECTION_KIND__GITLAB=gitlab          gitlab (default) | oauth2
    CONNECTION_BASE_URL__GITLAB=https://gitlab.example.com      for gitlab
    CONNECTION_AUTHORIZE_URL__… / _TOKEN_URL__… / _USERINFO_URL__… / _REVOKE_URL__…   for oauth2
    CONNECTION_SCOPE__GITLAB=read_api
    CONNECTION_API_URL__GITLAB=…            where tools call; gitlab: <base>/api/v4
    CONNECTION_MATCH_EMAIL__GITLAB=true     the account's email must be the chat user's
    CONNECTION_AGENTS__GITLAB=assistant,…   which agents may use it; unset = all

A name is the key's suffix lower-cased, ``_`` read as ``-``: ``GITLAB`` is
``gitlab``. Read from the environment and the .env file, the environment
winning — the same rule as the rest of the settings.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from crucible.connections.oauth2 import OAuth2Config, gitlab

_PREFIX = "CONNECTION_"
_KINDS = ("gitlab", "oauth2")


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

    def allows(self, agent: str) -> bool:
        return self.agents is None or agent in self.agents

    def oauth2_config(self, redirect_uri: str) -> OAuth2Config:
        if self.kind == "gitlab":
            config = gitlab(
                self.base_url, client_id=self.client_id, client_secret=self.client_secret,
                redirect_uri=redirect_uri, **({"scope": self.scope} if self.scope else {}),
            )
            return config
        return OAuth2Config(
            authorize_url=self.authorize_url, token_url=self.token_url, userinfo_url=self.userinfo_url,
            revoke_url=self.revoke_url, client_id=self.client_id, client_secret=self.client_secret,
            redirect_uri=redirect_uri, scope=self.scope,
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
    marker = _key("CLIENT_ID", "")
    for key, client_id in sorted(values.items()):
        if not key.startswith(marker) or not client_id.strip():
            continue
        suffix = key[len(marker):]
        name = suffix.lower().replace("_", "-")

        def get(field: str, default: str = "") -> str:
            return (values.get(_key(field, suffix)) or default).strip()

        kind = get("KIND", "gitlab").lower()
        if kind not in _KINDS:
            raise ValueError(f"{_key('KIND', suffix)}: {kind!r} is not one of {', '.join(_KINDS)}")
        secret = get("CLIENT_SECRET")
        if not secret:
            raise ValueError(f"{_key('CLIENT_SECRET', suffix)} is required beside {key}")
        base_url = get("BASE_URL").rstrip("/")
        endpoints = {f: get(f.upper()) for f in ("authorize_url", "token_url", "userinfo_url", "revoke_url")}
        if kind == "gitlab" and not base_url:
            raise ValueError(f"{_key('BASE_URL', suffix)} is required for a gitlab provider")
        if kind == "oauth2":
            for field in ("authorize_url", "token_url", "userinfo_url"):
                if not endpoints[field]:
                    raise ValueError(f"{_key(field.upper(), suffix)} is required for an oauth2 provider")
        raw_agents = values.get(_key("AGENTS", suffix))
        agents = (
            tuple(a.strip() for a in raw_agents.split(",") if a.strip()) if raw_agents is not None else None
        )
        match = get("MATCH_EMAIL")
        found[name] = ConnectionProvider(
            name=name, kind=kind, client_id=client_id.strip(), client_secret=secret,
            base_url=base_url, scope=get("SCOPE"),
            api_url=get("API_URL") or (f"{base_url}/api/v4" if kind == "gitlab" else ""),
            match_email=_bool(_key("MATCH_EMAIL", suffix), match) if match else True,
            agents=agents, **endpoints,
        )
    return found
