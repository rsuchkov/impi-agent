"""Application settings loaded from environment / .env (pydantic-settings).

No module-level singleton on purpose: ``main.py`` builds one Settings and
injects it by constructor — tests construct their own without env games.
"""

from __future__ import annotations

import os
import socket
from functools import cached_property
from pathlib import Path
from typing import ClassVar

from dotenv import dotenv_values
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


def _detect_lan_ip() -> str:
    """The host's primary outbound-interface IPv4. A UDP connect sends nothing —
    it just makes the kernel pick the route, revealing the local address. Falls
    back to host.containers.internal if there's no route."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "host.containers.internal"
    finally:
        s.close()


class ToolSettings(BaseModel):
    """Tool-SERVER config as its own model (not per-tool settings — those belong
    to each tool). Bound from flat env names on Settings (see ``Settings.tools``)
    — flat, not TOOLS__* nested, to avoid colliding with AGENTS_MM_TOKEN__*."""

    enabled: bool
    server_host: str
    server_port: int

    @property
    def server_url(self) -> str:
        return f"http://{self.server_host}:{self.server_port}"


class IntegrationsSettings(BaseModel):
    """The outbound-callback receiver for widgets. Binds 0.0.0.0 (MM reaches it
    from its container); ``public_url`` is what MM actually calls back to."""

    enabled: bool
    host: str
    port: int
    public_url: str  # e.g. http://host.containers.internal:8423
    ui_timeout: float  # blocking UI request: seconds to await the human before default-reject

    @property
    def interact_url(self) -> str:
        """Callback for widget/form buttons (a click)."""
        return self._endpoint("interact")

    @property
    def dialog_url(self) -> str:
        """Callback for a modal form's submission."""
        return self._endpoint("dialog")

    def _endpoint(self, path: str) -> str:
        # Safe join: a trailing slash on public_url must not double up.
        return f"{self.public_url.rstrip('/')}/{path}"


class Settings(BaseSettings):
    """Base engine + gateway configuration. An application subclasses this to add
    its own fields (see ImpiSettings). Extend as the system grows."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Basename of the SQLite inventory under data_dir. Neutral default; an app
    # subclass may override it to keep its own filename.
    DB_FILENAME: ClassVar[str] = "agent.db"

    # LLM via a custom OpenAI-compatible endpoint (optional provider extension).
    # Empty when the ChatGPT subscription is used — pi authenticates itself
    # via its own OAuth store (~/.pi/agent/auth.json).
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_verify_ssl: bool = True

    # Mattermost gateway.
    mattermost_url: str = "http://localhost:8065"
    mattermost_token: str = ""
    mm_max_post_chars: int = 16000  # chunk threshold; MM MaxPostSize default 16383

    # Slack gateway (Socket Mode; interactivity arrives over the WebSocket, so no
    # HTTP receiver is needed). Empty tokens = the Slack gateway is not started.
    slack_bot_token: str = ""  # xoxb-… bot token
    slack_app_token: str = ""  # xapp-… app-level token for Socket Mode

    # ws gateway (duplex WebSocket hub for custom client services). The hub is
    # started only when some agent runs on the "ws" gateway; access is per
    # SERVICE, via dynamic WS_SERVICE_TOKEN__<NAME> keys (see ws_services()).
    ws_host: str = "0.0.0.0"
    ws_port: int = 8424

    # Which chat gateway an agent runs on ("mattermost" | "slack" | "ws"). The
    # default for all agents; override per agent with AGENTS_GATEWAY__<AGENT>.
    gateway: str = "mattermost"

    # Agents directory (profiles) + which agents to run. A plain directory, not
    # necessarily a git repo (it may be one, but nothing here assumes it).
    agents_path: str = ""
    agents_enabled: str = ""  # CSV of agent names; empty = all agents in the dir
    agent_name: str = "assistant"  # the agent MATTERMOST_TOKEN falls back to
    # Where mm_token_for() looks up dynamic AGENTS_MM_TOKEN__* keys. Explicit
    # (not the class env_file) so tests can point it away from the real .env.
    dotenv_path: str = ".env"

    # Bot-side state.
    data_dir: str = "data"
    db_path: str = ""  # default: {data_dir}/<DB_FILENAME>

    # pi CLI (@earendil-works/pi-coding-agent), assumed on PATH.
    pi_bin: str = "pi"
    pi_session_dir: str = ""  # default: {data_dir}/pi-sessions (per-agent subdirs)
    pi_timeout: float = 180.0  # fallback when agent.yaml omits runtime.timeout
    pi_max_concurrent_sessions: int = 4
    pi_session_idle_ttl: float = 1800.0

    # Default LLM provider/model when an agent's agent.yaml omits them (empty =
    # pass no flag, letting pi use its own ~/.pi/agent/settings.json default).
    # `provider` selects pi's backend (--provider, e.g. openai-codex); `model`
    # picks the model within it (--model).
    default_provider: str = ""
    default_model: str = ""

    # Tool SERVER (env: TOOL_ENABLED / TOOL_SERVER_HOST / TOOL_SERVER_PORT) —
    # read grouped via `.tools`. Per-tool settings live with each tool
    # (env TOOL_<TOOL>_*), loaded by the registry — never declared here.
    tool_enabled: bool = True
    tool_server_host: str = "127.0.0.1"
    tool_server_port: int = 8422

    # Widget callbacks. Binds 0.0.0.0; MM calls back over
    # host.containers.internal. env: INTEGRATIONS_*
    integrations_enabled: bool = True
    integrations_host: str = "0.0.0.0"
    integrations_port: int = 8423
    integrations_public_url: str = ""  # default: http://host.containers.internal:{port}
    integrations_ui_timeout: float = 90.0  # blocking confirm/select: human-answer window

    log_level: str = "INFO"

    def enabled_agent_names(self) -> list[str] | None:
        """Parsed agents_enabled; None means "all agents found in the repo"."""
        names = [name.strip() for name in self.agents_enabled.split(",") if name.strip()]
        return names or None

    def mm_token_for(self, agent: str) -> str:
        """Per-agent bot token: AGENTS_MM_TOKEN__<AGENT> (env or .env), with
        MATTERMOST_TOKEN as the fallback for the default agent only. Tokens are
        dynamic per-agent keys, which pydantic-settings can't model as fields."""
        key = f"AGENTS_MM_TOKEN__{agent.upper().replace('-', '_')}"
        token = os.environ.get(key) or self._dotenv.get(key) or ""
        if token:
            return token
        if agent == self.agent_name:
            return self.mattermost_token
        return ""

    def slack_tokens_for(self, agent: str) -> tuple[str, str]:
        """(bot_token, app_token) for an agent's Slack gateway, or ("", "") if not
        configured. Per-agent AGENTS_SLACK_BOT_TOKEN__<AGENT> /
        AGENTS_SLACK_APP_TOKEN__<AGENT>, with the global slack_bot_token/
        slack_app_token as the default agent's fallback. Both must be present."""
        up = agent.upper().replace("-", "_")
        bot = self._token(f"AGENTS_SLACK_BOT_TOKEN__{up}")
        app = self._token(f"AGENTS_SLACK_APP_TOKEN__{up}")
        if bot and app:
            return bot, app
        if agent == self.agent_name and self.slack_bot_token and self.slack_app_token:
            return self.slack_bot_token, self.slack_app_token
        return "", ""

    def gateway_for(self, agent: str) -> str:
        """The chat gateway kind for an agent: AGENTS_GATEWAY__<AGENT> (env/.env),
        else the global `gateway` default."""
        return self._token(f"AGENTS_GATEWAY__{agent.upper().replace('-', '_')}") or self.gateway

    def skills_for(self, agent: str) -> tuple[str, ...] | None:
        """Per-agent skills override: AGENTS_SKILLS__<AGENT> (CSV, env or .env).
        None means the key is unset (keep the agent.yaml list); a set value — even
        empty — REPLACES it (empty => the agent gets no skills). Unlike _token(),
        this distinguishes "unset" from "set to empty"."""
        key = f"AGENTS_SKILLS__{agent.upper().replace('-', '_')}"
        raw = os.environ.get(key)
        if raw is None:
            raw = self._dotenv.get(key)
        if raw is None:
            return None
        return tuple(s.strip() for s in raw.split(",") if s.strip())

    def ws_services(self) -> dict[str, tuple[str, tuple[str, ...] | None]]:
        """Client services allowed on the ws gateway hub: name -> (bearer token,
        agent allowlist or None). Dynamic keys, like the per-agent tokens:
        WS_SERVICE_TOKEN__<NAME> registers a service (value = its token);
        WS_SERVICE_AGENTS__<NAME> (CSV) restricts which agents it may address —
        unset = every ws agent, set-but-empty = none. The service name is the
        key suffix, lower-cased with '_' back to '-' (mirroring the agent-key
        transform)."""
        prefix = "WS_SERVICE_TOKEN__"
        merged = {**self._dotenv, **os.environ}
        services: dict[str, tuple[str, tuple[str, ...] | None]] = {}
        for key, token in merged.items():
            if not key.startswith(prefix) or not token:
                continue
            suffix = key[len(prefix):]
            name = suffix.lower().replace("_", "-")
            raw_allow = os.environ.get(f"WS_SERVICE_AGENTS__{suffix}")
            if raw_allow is None:
                raw_allow = self._dotenv.get(f"WS_SERVICE_AGENTS__{suffix}")
            allow = (
                None
                if raw_allow is None
                else tuple(a.strip() for a in raw_allow.split(",") if a.strip())
            )
            services[name] = (token, allow)
        return services

    def _token(self, key: str) -> str:
        return os.environ.get(key) or self._dotenv.get(key) or ""

    @cached_property
    def _dotenv(self) -> dict[str, str]:
        if not self.dotenv_path or not Path(self.dotenv_path).exists():
            return {}
        return {k: v for k, v in dotenv_values(self.dotenv_path).items() if v is not None}

    @property
    def tools(self) -> ToolSettings:
        return ToolSettings(
            enabled=self.tool_enabled,
            server_host=self.tool_server_host,
            server_port=self.tool_server_port,
        )

    @property
    def integrations(self) -> IntegrationsSettings:
        if self.integrations_public_url == "auto":
            # Detect the host's LAN IP so a containerized Mattermost can reach the
            # receiver when host.containers.internal isn't routable. Resolved at
            # startup (the host IP may change); keep that subnet in MM's
            # AllowedUntrustedInternalConnections.
            url = f"http://{_detect_lan_ip()}:{self.integrations_port}"
        else:
            url = (
                self.integrations_public_url
                or f"http://host.containers.internal:{self.integrations_port}"
            )
        return IntegrationsSettings(
            enabled=self.integrations_enabled,
            host=self.integrations_host,
            port=self.integrations_port,
            public_url=url,
            ui_timeout=self.integrations_ui_timeout,
        )

    @property
    def resolved_db_path(self) -> Path:
        return Path(self.db_path) if self.db_path else Path(self.data_dir) / self.DB_FILENAME

    @property
    def resolved_pi_session_dir(self) -> Path:
        if self.pi_session_dir:
            return Path(self.pi_session_dir)
        return Path(self.data_dir) / "pi-sessions"


def load_settings() -> Settings:
    """Build a Settings instance from the current environment / .env."""
    return Settings()
