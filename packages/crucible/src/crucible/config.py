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
    # Ceiling on "allow this tool for a while". Shorter than a secret's by
    # default: leave bash open and you have left everything open.
    max_grant_s: int
    # What an agent should CALL, when that is not the same as what this binds.
    # They differ as soon as the agent is not in this container: the bind may be
    # 0.0.0.0 and the caller needs a name that resolves on its own network.
    # Empty = they are the same thing, which is the single-container case.
    public_url: str = ""

    @property
    def server_url(self) -> str:
        return self.public_url or f"http://{self.server_host}:{self.server_port}"


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

    def command_url(self, agent: str) -> str:
        """Request URL to register a slash command with, for one agent — the path
        carries the agent, since a command payload doesn't say whom it addresses."""
        return self._endpoint(f"command/{agent}")

    def _endpoint(self, path: str) -> str:
        # Safe join: a trailing slash on public_url must not double up.
        return f"{self.public_url.rstrip('/')}/{path}"


class SchedulerSettings(BaseModel):
    """The ticker over the task store. Flat env names on Settings, like the
    other groups here (SCHEDULER_TICK_S, not SCHEDULER__TICK_S)."""

    enabled: bool
    tick_s: float
    timezone: str  # IANA default for tasks that name none
    max_concurrent: int
    run_deadline_s: float
    startup_grace_s: float
    max_failures: int  # consecutive failures before a task is paused
    max_tasks_per_agent: int


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
    # Message shortcuts whose callback id starts with this are commands, and the
    # rest of the id is the command name (crux_summarize -> "summarize"). Change
    # it to match a workspace's own naming; empty = every shortcut is a command.
    slack_command_prefix: str = "crux_"

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
    # The shared skill library any agent can be given skills from — its own
    # directory (ideally its own git repo). Empty = "_skills" beside the agents,
    # so an existing deployment keeps working without new configuration.
    skills_path: str = ""
    # The slash command that opens the library browser. Configurable because a
    # workspace may already use /skills for something else; the platform's
    # command must be registered under this exact word.
    skills_command: str = "skills"
    # The default agent: the one the unsuffixed keys below belong to, and the one
    # /command/default resolves to when more than one agent is running.
    agent_name: str = "assistant"
    # Slash-command tokens for that default agent, so a single-agent deployment
    # never has to spell its name (CSV, same shape as the per-agent key).
    command_tokens: str = ""
    # Where mm_token_for() looks up dynamic AGENTS_MM_TOKEN__* keys. Explicit
    # (not the class env_file) so tests can point it away from the real .env.
    dotenv_path: str = ".env"

    # Bot-side state.
    data_dir: str = "data"
    db_path: str = ""  # default: {data_dir}/<DB_FILENAME>

    # Files people attach in chat, and files agents send back. Off = attachments
    # are ignored on the way in and no agent gets the send-a-file tool.
    attachments_enabled: bool = True
    attachments_dir: str = ""  # default: {data_dir}/attachments
    attachment_max_mb: float = 20.0  # per file, both directions
    attachment_retention_days: int = 14  # 0 = keep forever
    # Per-image cap for showing a picture to the runtime directly: model backends
    # reject large inline images, and an oversized one is still readable by path.
    inline_image_max_mb: float = 4.0

    # pi CLI (@earendil-works/pi-coding-agent), assumed on PATH.
    pi_bin: str = "pi"
    pi_session_dir: str = ""  # default: {data_dir}/pi-sessions (per-agent subdirs)
    pi_timeout: float = 180.0  # fallback when agent.yaml omits runtime.timeout
    pi_max_concurrent_sessions: int = 4
    # A second bound, per agent. 0 = only the global one. Worth setting once the
    # agents have hosts of their own: the global number then bounds a resource
    # this process no longer owns, and one busy agent should not be able to take
    # every slot from the rest.
    pi_max_sessions_per_agent: int = 0
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
    tool_max_grant_s: int = 900
    # Where agents reach the tool server, when that is not where it binds. Set
    # when the agents run in containers of their own; empty otherwise.
    tool_public_url: str = ""

    # Agent containers. Off = every agent's runtime is a child process of this
    # one, which is what a deployment made before this existed keeps doing.
    # On = the engine asks each agent's own host to run it instead, over the
    # network, and holds none of the agent's dependencies or credentials itself.
    agent_hosts_enabled: bool = False
    # Where an agent's host is. ``{agent}`` is the agent's name; a per-agent
    # AGENTS_HOST_URL__<AGENT> overrides it for one agent.
    agent_host_url: str = "http://agent-{agent}:8427"
    # How long to wait for a host to accept a connection and answer a spawn.
    agent_host_timeout: float = 30.0

    # Widget callbacks. Binds 0.0.0.0; MM calls back over
    # host.containers.internal. env: INTEGRATIONS_*
    integrations_enabled: bool = True
    integrations_host: str = "0.0.0.0"
    integrations_port: int = 8423
    integrations_public_url: str = ""  # default: http://host.containers.internal:{port}
    integrations_ui_timeout: float = 90.0  # blocking confirm/select: human-answer window

    # Scheduled and recurring work (env: SCHEDULER_*) — read grouped via
    # `.scheduler`. Off means the ticker never starts and no agent is offered
    # the scheduling tools; a health check then says "off", not "broken".
    scheduler_enabled: bool = True
    scheduler_tick_s: float = 20.0
    # Where "09:00" is, for tasks that do not name a zone of their own. The
    # container has no local time to inherit: it runs in UTC.
    scheduler_timezone: str = "UTC"
    scheduler_max_concurrent: int = 2  # scheduled runs at once (the runtime allows 4 sessions)
    scheduler_run_deadline_s: float = 900.0  # stop WAITING on a run after this
    scheduler_startup_grace_s: float = 60.0  # let the gateways log in before catching up
    scheduler_max_failures: int = 5  # then pause the task and say so
    scheduler_max_tasks_per_agent: int = 50
    # The slash command that browses tasks, configurable for the same reason
    # SKILLS_COMMAND is: a workspace may already use the word.
    tasks_command: str = "tasks"

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

    def agent_host_for(self, agent: str) -> tuple[str, str]:
        """(url, token) of the host that runs this agent, or ("", "") when it
        runs here. The token is a dynamic per-agent key written when the agent's
        container is created; without one there is no host, whatever the URL
        says — a host that would accept anybody is not one we will talk to."""
        if not self.agent_hosts_enabled:
            return "", ""
        up = agent.upper().replace("-", "_")
        token = self._token(f"AGENTS_HOST_TOKEN__{up}")
        if not token:
            return "", ""
        url = self._token(f"AGENTS_HOST_URL__{up}") or self.agent_host_url.format(
            agent=agent
        )
        return url, token

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

    def command_tokens_for(self, agent: str) -> tuple[str, ...]:
        """Verification tokens of the slash commands this agent accepts:
        AGENTS_COMMAND_TOKENS__<AGENT> (CSV — one command, one token), with
        COMMAND_TOKENS as the fallback for the default agent only, the same rule
        the bot tokens follow. Empty = commands are disabled for the agent and
        its endpoint refuses everything."""
        raw = self._token(f"AGENTS_COMMAND_TOKENS__{agent.upper().replace('-', '_')}")
        if not raw and agent == self.agent_name:
            raw = self.command_tokens
        return tuple(t.strip() for t in raw.split(",") if t.strip())

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
            max_grant_s=self.tool_max_grant_s,
            public_url=self.tool_public_url,
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

    @property
    def scheduler(self) -> SchedulerSettings:
        return SchedulerSettings(
            enabled=self.scheduler_enabled,
            tick_s=self.scheduler_tick_s,
            timezone=self.scheduler_timezone,
            max_concurrent=self.scheduler_max_concurrent,
            run_deadline_s=self.scheduler_run_deadline_s,
            startup_grace_s=self.scheduler_startup_grace_s,
            max_failures=self.scheduler_max_failures,
            max_tasks_per_agent=self.scheduler_max_tasks_per_agent,
        )

    @property
    def resolved_attachments_dir(self) -> Path:
        """Where incoming files land. Under data_dir by default, so a deployment
        keeps them in the same volume as the rest of the engine's state."""
        if self.attachments_dir:
            return Path(self.attachments_dir)
        return Path(self.data_dir) / "attachments"

    @property
    def resolved_skills_path(self) -> Path:
        """The shared skill library. Its own directory when configured; otherwise
        ``_skills`` beside the agents, which needs no deployment change."""
        if self.skills_path:
            return Path(self.skills_path)
        return Path(self.agents_path or ".") / "_skills"


def load_settings() -> Settings:
    """Build a Settings instance from the current environment / .env."""
    return Settings()
