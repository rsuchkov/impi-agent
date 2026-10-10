"""Tools that act as the person at a provider they connected (docs/connections.md).

The token stays here: the model names a provider and a path under that
provider's API, and gets back what the provider answered — never a token, a
connect link or whose account it is beyond the account's own name. A person
connects in a direct message with the agent (``/connections``); the model is
told to send them there and never to ask for a token.

Direct messages only, for now: in a shared conversation "the person the turn
acts for" is not settled enough to act as anyone (CHANGELOG, backlog).

Advertised only where connections are configured (``CAP_CONNECTIONS``).
Importing this module runs the decorators, so the app imports it before
building the registry.
"""

import json
import os
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import unquote

import httpx
from dotenv import dotenv_values
from pydantic import PrivateAttr
from pydantic_settings import BaseSettings, SettingsConfigDict

from crucible.connections import (
    ConnectionsError,
    Grant,
    NotConnected,
    ProviderUnavailable,
)
from crucible.ports.chat.types import KIND_DM, CallPreview, PreviewRow
from crucible.tools.base import CAP_CONNECTIONS, Tool, ToolContext, ToolError
from crucible.tools.registry import tool
from impi.connections_config import connection_providers
from impi.connections_screen import DEFAULT_COMMAND

# What the model reads of an answer; past it the body is cut and says so.
MAX_RESPONSE_CHARS = 20_000
_TIMEOUT_S = 30.0
_WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")


class ConnectedApiSettings(BaseSettings):
    """Where each provider's API is, and which command connects one — read
    from the same CONNECTION_*__<NAME> keys and .env the engine reads."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    connections_command: str = DEFAULT_COMMAND
    _api_urls: dict[str, str] = PrivateAttr(default_factory=dict)

    def __init__(self, **values: Any) -> None:
        super().__init__(**values)
        env_file = values.get("_env_file")
        dotenv = (
            {k: v for k, v in dotenv_values(env_file).items() if v is not None}
            if isinstance(env_file, str) and Path(env_file).exists() else {}
        )
        providers = connection_providers({**dotenv, **os.environ})
        self._api_urls = {name: p.api_url for name, p in providers.items() if p.api_url}

    def api_url(self, provider: str) -> str:
        return self._api_urls.get(provider, "")

    @property
    def command(self) -> str:
        return "/" + (self.connections_command.lstrip("/").strip().lower() or DEFAULT_COMMAND)


def _settings(ctx: ToolContext) -> ConnectedApiSettings:
    if not isinstance(ctx.settings, ConnectedApiSettings):
        raise ToolError("connected accounts are not configured in this deployment")
    return ctx.settings


def _require_direct(ctx: ToolContext) -> None:
    if ctx.conversation_kind != KIND_DM:
        raise ToolError(
            "acting as someone's connected account works only in a direct message with them. "
            "Tell the user to ask you there, and do not retry here"
        )


def _require_str(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"missing required string argument {key!r}")
    return value.strip()


def api_target(api_url: str, path: str) -> str:
    """``path`` under ``api_url``, refused when it would leave it: another host
    or scheme, a step up, a backslash or a control character — the token is
    sent only where the operator said the provider's API is."""
    if any(c in path for c in "\\\r\n\t ") or any(ord(c) < 0x20 for c in path):
        raise ToolError("the path may not contain spaces, backslashes or control characters")
    if "://" in path or path.startswith("//"):
        raise ToolError("give a path under the provider's API, not a URL")
    route = path.split("?", 1)[0].split("#", 1)[0]
    if ".." in unquote(route).split("/"):
        raise ToolError("the path may not step up out of the provider's API")
    base = httpx.URL(api_url.rstrip("/") + "/")
    target = httpx.URL(api_url.rstrip("/") + "/" + path.lstrip("/"))
    if (target.scheme, target.host, target.port) != (base.scheme, base.host, base.port) or not (
        target.path.startswith(base.path)
    ):
        raise ToolError("the path leaves the provider's API")
    return str(target)


async def _grant(ctx: ToolContext, provider: str, command: str) -> Grant:
    connections = ctx.require_connections()
    if provider not in connections.providers:
        known = ", ".join(connections.providers) or "none"
        raise ToolError(f"{provider!r} is not a provider you may use here (you may use: {known})")
    try:
        return await connections.grant(provider)
    except NotConnected as exc:
        raise ToolError(
            f"the user has not connected their {provider} account. Ask them to open {command} in a "
            "direct message with you and connect it there. Never ask them for a token or a password"
        ) from exc
    except ProviderUnavailable as exc:
        raise ToolError(f"{provider} did not answer; try again in a minute") from exc
    except ConnectionsError as exc:
        raise ToolError(str(exc)) from exc


def _answer(response: httpx.Response) -> dict[str, Any]:
    text = response.text
    truncated = len(text) > MAX_RESPONSE_CHARS
    body: Any = text[:MAX_RESPONSE_CHARS]
    if not truncated and "json" in response.headers.get("content-type", ""):
        try:
            body = json.loads(text)
        except ValueError:
            pass
    answer: dict[str, Any] = {"status": response.status_code, "body": body}
    if truncated:
        answer["truncated"] = f"the answer was {len(text)} characters; this is the first {MAX_RESPONSE_CHARS}"
    return answer


async def _call(
    ctx: ToolContext, args: dict[str, Any], method: str, *, body: Any = None,
) -> dict[str, Any]:
    _require_direct(ctx)
    settings = _settings(ctx)
    provider = _require_str(args, "provider")
    api_url = settings.api_url(provider)
    if not api_url:
        raise ToolError(f"{provider} has no API address configured (CONNECTION_API_URL__…)")
    url = api_target(api_url, _require_str(args, "path"))
    grant = await _grant(ctx, provider, settings.command)
    try:
        # A redirect is answered, not followed: following it would carry the
        # token wherever the provider points.
        async with httpx.AsyncClient(timeout=_TIMEOUT_S, follow_redirects=False) as client:
            response = await client.request(
                method, url, json=body,
                headers={"Authorization": f"Bearer {grant.token.reveal()}", "Accept": "application/json"},
            )
    except httpx.HTTPError as exc:
        raise ToolError(f"{provider} did not answer ({type(exc).__name__}); try again in a minute") from exc
    if response.status_code == 401:
        await ctx.require_connections().forget(grant)
        raise ToolError(
            f"{provider} no longer accepts the user's connection, so it has ended. Ask them to "
            f"connect again with {settings.command} in a direct message with you"
        )
    return _answer(response)


_PROVIDER = {"type": "string", "description": "The provider, as connected_accounts lists it"}
_PATH = {
    "type": "string",
    "description": "Path under the provider's API, with any query string — e.g. "
                   "'projects?membership=true' for GitLab's /api/v4/projects",
}


@tool
class ConnectedAccounts(Tool):
    name: ClassVar[str] = "connected_accounts"
    requires: ClassVar[frozenset[str]] = frozenset({CAP_CONNECTIONS})
    settings_cls: ClassVar[type | None] = ConnectedApiSettings
    description: ClassVar[str] = (
        "List the providers you may act as the user at, and which of them the user has "
        "connected and as whom. Works in a direct message with the user."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object", "properties": {}, "additionalProperties": False,
    }

    async def execute(self, ctx: ToolContext, args: dict[str, Any]) -> Any:
        _require_direct(ctx)
        connections = ctx.require_connections()
        accounts = []
        for provider in connections.providers:
            account = await connections.account(provider)
            accounts.append({
                "provider": provider,
                "connected": account is not None,
                **({"account": account.username or account.name} if account is not None else {}),
            })
        return {"accounts": accounts, "connect_with": _settings(ctx).command}


@tool
class ConnectedApiRead(Tool):
    name: ClassVar[str] = "connected_api_read"
    requires: ClassVar[frozenset[str]] = frozenset({CAP_CONNECTIONS})
    settings_cls: ClassVar[type | None] = ConnectedApiSettings
    returns_untrusted: ClassVar[bool] = True
    description: ClassVar[str] = (
        "Read from a provider's API as the user, with their connected account and their "
        "rights there (an HTTP GET). Returns the status and the body. Works in a direct "
        "message with the user."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"provider": _PROVIDER, "path": _PATH},
        "required": ["provider", "path"],
        "additionalProperties": False,
    }

    async def execute(self, ctx: ToolContext, args: dict[str, Any]) -> Any:
        return await _call(ctx, args, "GET")


@tool
class ConnectedApiWrite(Tool):
    name: ClassVar[str] = "connected_api_write"
    display_name: ClassVar[str] = "Change something in your connected account"
    requires: ClassVar[frozenset[str]] = frozenset({CAP_CONNECTIONS})
    settings_cls: ClassVar[type | None] = ConnectedApiSettings
    requires_confirmation: ClassVar[bool] = True
    returns_untrusted: ClassVar[bool] = True
    description: ClassVar[str] = (
        "Change something at a provider as the user, with their connected account and "
        "their rights there (POST, PUT, PATCH or DELETE with an optional JSON body). The "
        "user confirms each call. Works in a direct message with the user."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "provider": _PROVIDER,
            "method": {"type": "string", "enum": list(_WRITE_METHODS)},
            "path": _PATH,
            "body": {"type": "object", "description": "JSON body, when the call takes one"},
        },
        "required": ["provider", "method", "path"],
        "additionalProperties": False,
    }

    async def describe(self, ctx: ToolContext, args: dict[str, Any]) -> CallPreview | None:
        """The card: the address the call goes to and the account it acts as,
        as this deployment resolves them — not the model's account of it."""
        provider = str(args.get("provider") or "").strip()
        method = str(args.get("method") or "").strip().upper()
        settings = ctx.settings if isinstance(ctx.settings, ConnectedApiSettings) else None
        api_url = settings.api_url(provider) if settings is not None else ""
        if not api_url or method not in _WRITE_METHODS:
            return None
        try:
            url = api_target(api_url, str(args.get("path") or ""))
        except ToolError:
            return None
        account = (
            await ctx.connections.account(provider)
            if ctx.connections is not None and provider in ctx.connections.providers else None
        )
        body = args.get("body")
        rows = (
            ("Account", f"{account.username or account.name} at {provider}" if account else f"not connected to {provider}"),
            ("Request", f"{method} {url}"),
            ("Body", json.dumps(body, ensure_ascii=False, indent=2) if body is not None else ""),
        )
        return CallPreview(
            title=f"{method} at {provider} as you",
            rows=tuple(PreviewRow(label, value) for label, value in rows if value),
            danger=method == "DELETE",
        )

    async def execute(self, ctx: ToolContext, args: dict[str, Any]) -> Any:
        method = _require_str(args, "method").upper()
        if method not in _WRITE_METHODS:
            raise ToolError(f"method must be one of {', '.join(_WRITE_METHODS)}")
        body = args.get("body")
        if body is not None and not isinstance(body, dict):
            raise ToolError("body must be a JSON object")
        return await _call(ctx, args, method, body=body)
