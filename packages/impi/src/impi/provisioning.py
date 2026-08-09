"""Provisioning core shared by the `impi` CLI and the create_agent engine tool:
create a Mattermost bot account (needs a system-admin token), scaffold an agent
profile on disk, and record credentials in the .env file. The network side uses
the same mattermostautodriver the gateway uses; the driver is injectable so
tests stay offline.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from dotenv import set_key
from mattermostautodriver import AsyncTypedDriver
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from crucible.gateways.mattermost.options import driver_options

AGENT_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")


class ProvisioningError(Exception):
    """A provisioning step failed in an expected, user-reportable way."""


class CreateAgentSettings(BaseSettings):
    """Shared provisioning config (env: TOOL_CREATE_AGENT_*) — the create_agent
    tool's settings_cls, also used as CLI defaults. URL/paths fall back to the
    engine's plain keys (MATTERMOST_URL, AGENTS_PATH, DOTENV_PATH, GATEWAY) so a
    dev setup needs no duplicate values; the admin token is always explicit."""

    model_config = SettingsConfigDict(
        env_prefix="TOOL_CREATE_AGENT_",
        env_file=".env",
        extra="ignore",
        # Aliased fields must stay constructible by field name (tests, CLI).
        populate_by_name=True,
    )

    admin_token: str = ""  # Mattermost system-admin PAT (bot creation needs it)
    mattermost_url: str = Field(
        default="",
        validation_alias=AliasChoices("TOOL_CREATE_AGENT_MATTERMOST_URL", "MATTERMOST_URL"),
    )
    agents_path: str = Field(
        default="",
        validation_alias=AliasChoices("TOOL_CREATE_AGENT_AGENTS_PATH", "AGENTS_PATH"),
    )
    dotenv_path: str = Field(
        default=".env",
        validation_alias=AliasChoices("TOOL_CREATE_AGENT_DOTENV_PATH", "DOTENV_PATH"),
    )
    # The engine's global gateway — a Mattermost bot on a non-mattermost default
    # needs a per-agent AGENTS_GATEWAY__<NAME> override written alongside the token.
    gateway: str = Field(
        default="mattermost",
        validation_alias=AliasChoices("TOOL_CREATE_AGENT_GATEWAY", "GATEWAY"),
    )
    team: str = ""  # team the new bot joins; empty = first team on the server


@dataclass(frozen=True)
class BotCredentials:
    user_id: str
    username: str
    token: str
    team: str = ""


def _default_driver(url: str, token: str, *, verify: bool) -> Any:
    return AsyncTypedDriver(driver_options(url, token, verify=verify))


async def provision_mm_bot(
    url: str,
    admin_token: str,
    *,
    username: str,
    display_name: str = "",
    description: str = "",
    team: str = "",
    verify: bool = True,
    driver: Any = None,
) -> BotCredentials:
    """Create a bot account, mint its personal access token, and add it to a
    team (the named one, else the server's first). `driver` overrides the
    Mattermost client for tests; it must support `async with`."""
    if driver is None:
        driver = _default_driver(url, admin_token, verify=verify)
    async with driver:
        try:
            await driver.login()
        except Exception as exc:
            raise ProvisioningError(
                f"cannot authenticate to Mattermost at {url}: {exc}"
            ) from exc
        try:
            bot = await driver.bots.create_bot(
                username, display_name or username, description or ""
            )
        except Exception as exc:
            raise ProvisioningError(
                f"could not create bot {username!r} (username taken, or the token "
                f"lacks admin rights?): {exc}"
            ) from exc
        user_id = bot["user_id"]
        try:
            token = (
                await driver.users.create_user_access_token(user_id, "impi agent token")
            )["token"]
        except Exception as exc:
            raise ProvisioningError(
                f"bot {username!r} created but token creation failed (enable "
                f"personal access tokens on the server?): {exc}"
            ) from exc
        team_name = ""
        try:
            if team:
                team_obj = await driver.teams.get_team_by_name(team)
            else:
                teams = await driver.teams.get_all_teams()
                team_obj = teams[0] if teams else None
            if team_obj is not None:
                await driver.teams.add_team_member(team_obj["id"], user_id)
                team_name = team_obj.get("name", "")
        except Exception as exc:
            raise ProvisioningError(
                f"bot {username!r} created but could not join team "
                f"{team or '(first on server)'}: {exc}"
            ) from exc
        return BotCredentials(
            user_id=user_id, username=username, token=token, team=team_name
        )


async def mm_admin_pat(
    url: str, login_id: str, password: str, *, verify: bool = True
) -> tuple[str, str]:
    """Log in with admin credentials and mint a personal access token; returns
    (token, user_id). The installer uses this right after bootstrapping a fresh
    server — mmctl's local mode cannot generate tokens."""
    base = url.rstrip("/")
    async with httpx.AsyncClient(verify=verify, timeout=30.0) as client:
        try:
            resp = await client.post(
                f"{base}/api/v4/users/login",
                json={"login_id": login_id, "password": password},
            )
        except httpx.HTTPError as exc:
            raise ProvisioningError(f"cannot reach Mattermost at {url}: {exc}") from exc
        if resp.status_code != 200:
            raise ProvisioningError(
                f"Mattermost login failed for {login_id!r}: "
                f"HTTP {resp.status_code} {resp.text[:200]}"
            )
        session_token = resp.headers.get("Token", "")
        user_id = resp.json().get("id", "")
        if not session_token or not user_id:
            raise ProvisioningError("Mattermost login response carried no session token")
        resp = await client.post(
            f"{base}/api/v4/users/{user_id}/tokens",
            json={"description": "impi provisioning"},
            headers={"Authorization": f"Bearer {session_token}"},
        )
        if resp.status_code not in (200, 201):
            raise ProvisioningError(
                f"could not create a personal access token: "
                f"HTTP {resp.status_code} {resp.text[:200]}"
            )
        return resp.json()["token"], user_id


# Templates for a fresh profile. Values are JSON-encoded on interpolation, which
# is also valid YAML — arbitrary role/description strings stay parseable.
_AGENT_YAML_TMPL = """\
# Machine config for this agent. `name` must equal the directory name.
name: {name}
display_name: {display_name}
role: {role}
description: {description}
runtime:
  # provider/model omitted -> DEFAULT_PROVIDER/DEFAULT_MODEL, then the runtime default.
  tools:
    - read
    - write
    - bash
"""

_SYSTEM_MD_TMPL = """\
# {plain_display_name}

You are {plain_display_name}, a chat agent with the role: {plain_role}.

- Be concise, concrete, and honest; say so when you do not know.
- Ask a clarifying question when the request is ambiguous.
- Use Markdown that renders well in chat (short paragraphs, lists, code fences).
"""


def write_agent_profile(
    agents_dir: str | Path,
    *,
    name: str,
    role: str,
    display_name: str = "",
    description: str = "",
    system_prompt: str = "",
) -> Path:
    """Scaffold agents/<name>/ (agent.yaml + .pi/SYSTEM.md) under the agents
    directory. Refuses to overwrite an existing profile."""
    if not AGENT_NAME_RE.match(name):
        raise ProvisioningError(
            f"invalid agent name {name!r}: use lowercase letters, digits and "
            f"hyphens (max 64 chars)"
        )
    if not role.strip():
        raise ProvisioningError("agent role must not be empty")
    profile_dir = Path(agents_dir) / "agents" / name
    if (profile_dir / "agent.yaml").exists():
        raise ProvisioningError(f"agent profile already exists: {profile_dir}")
    shown_name = display_name or name
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "agent.yaml").write_text(
        _AGENT_YAML_TMPL.format(
            name=json.dumps(name),
            display_name=json.dumps(shown_name),
            role=json.dumps(role),
            description=json.dumps(description),
        ),
        encoding="utf-8",
    )
    pi_dir = profile_dir / ".pi"
    pi_dir.mkdir(exist_ok=True)
    (pi_dir / "SYSTEM.md").write_text(
        system_prompt
        or _SYSTEM_MD_TMPL.format(plain_display_name=shown_name, plain_role=role),
        encoding="utf-8",
    )
    return profile_dir


def set_env_key(dotenv_path: str | Path, key: str, value: str) -> None:
    """Create-or-update KEY in the .env file (created 0600 when missing).
    dotenv's rewrite is rename-based, which is safe here because deployments
    mount the config directory, not the file itself."""
    path = Path(dotenv_path)
    if path.parent != Path("."):
        path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.touch(mode=0o600)
    # Tokens/URLs/names stay raw like the rest of the file; quote only values
    # the dotenv format cannot carry verbatim.
    needs_quoting = any(c.isspace() for c in value) or any(c in value for c in "#'\"")
    set_key(path, key, value, quote_mode="always" if needs_quoting else "never")


def agent_env_key(agent: str, kind: str = "MM_TOKEN") -> str:
    """The per-agent .env key, matching the engine's lookup convention
    (AGENTS_<KIND>__<NAME>, name upper-cased with hyphens as underscores)."""
    return f"AGENTS_{kind}__{agent.upper().replace('-', '_')}"
