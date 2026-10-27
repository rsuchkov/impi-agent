"""impi application settings: the base engine/gateway Settings plus the fields
specific to this app — the engine-owned `support` agent and the inter-agent
messaging / loop-protection knobs (impi is the multi-agent configuration).
"""

from __future__ import annotations

import os
import re
from typing import ClassVar

from pydantic import Field, field_validator, model_validator

from crucible.config import Settings
from crucible.connections.callback import CALLBACK_PATH
from crucible.ports.agent.model_ref import check_provider_has_model
from impi.connections_config import (
    CONNECTION_SECRET_PREFIXES,
    ConnectionProvider,
    command_word,
    connection_providers,
)
from impi.connections_config import (
    DEFAULT_COMMAND as DEFAULT_CONNECTIONS_COMMAND,
)

# A directory name under _extensions/, nothing that could walk out of it.
EXTENSION_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


class ImpiSettings(Settings):
    """Base Settings + impi-specific fields. Reads the same .env; inherited env
    binding covers the added fields."""

    APP_SECRET_PREFIXES: ClassVar[tuple[str, ...]] = CONNECTION_SECRET_PREFIXES

    # What this engine calls its inventory: {data_dir}/impi.db on SQLite, the
    # `impi` database on MongoDB.
    STORE_NAME: ClassVar[str] = "impi"

    # The engine's own `support` agent (bundled with impi) — override its
    # provider/model separately; each falls back to default_provider/default_model.
    support_provider: str = ""
    support_model: str = ""

    # Whether the engine's telemetry (a turn ended, a tool ran — see
    # docs/telemetry.md) is written to the log as JSON lines, impi's own
    # receiver. Off for a deployment that reads the events some other way.
    telemetry_log: bool = True

    @model_validator(mode="after")
    def _support_names_a_model(self) -> "ImpiSettings":
        # Each falls back on its own, so what the support agent ends up with is
        # what has to be whole.
        if self.support_provider.strip():
            check_provider_has_model(
                self.support_provider, self.support_model or self.default_model,
                "SUPPORT_PROVIDER", "SUPPORT_MODEL",
            )
        return self

    # Inter-agent messaging + loop protection.
    agents_reply_to_agents: bool = True  # answer other agents' mentions
    # Which of AGENTS_PATH/_extensions/<name>/index.ts load into every agent's
    # runtime: names, CSV. Nothing is loaded for being present — an extension
    # is code in every agent's process, and a directory any agent with `write`
    # can add to is not where that decision belongs.
    runtime_extensions: str = ""

    @field_validator("runtime_extensions")
    @classmethod
    def _extension_names_are_names(cls, value: str) -> str:
        for name in _csv_names(value):
            if not EXTENSION_NAME_RE.fullmatch(name):
                raise ValueError(
                    f"RUNTIME_EXTENSIONS: {name!r} is not an extension name (a directory "
                    "name under _extensions/: letters, digits, '-' and '_')"
                )
        return value

    def runtime_extension_names(self) -> tuple[str, ...]:
        """The extensions the deployment named, in its order, each once —
        checked as the settings loaded."""
        return _csv_names(self.runtime_extensions)
    # People's own accounts at OAuth providers (docs/connections.md). The
    # providers are the CONNECTION_*__<NAME> family (connections_config.py);
    # these say how the deployment keeps and finishes connects.
    # Fernet keys, CSV: the first seals, every one opens — put a new key first.
    connections_keys: str = Field(default="", repr=False)
    # Where a person's browser reaches the callback listener — the address
    # registered with each provider as <this>/oauth/callback.
    connections_public_url: str = ""
    connections_host: str = "0.0.0.0"
    connections_port: int = 8429
    # The command that opens the connections screen (a direct message only).
    connections_command: str = DEFAULT_CONNECTIONS_COMMAND

    def connection_providers(self) -> dict[str, ConnectionProvider]:
        """The providers the CONNECTION_*__<NAME> keys configure, checked —
        ValueError naming the key for one that is not usable."""
        return connection_providers({**self._dotenv, **os.environ})

    def sealing_keys(self) -> tuple[str, ...]:
        """CONNECTIONS_KEYS as a list, the sealing key first."""
        return tuple(k.strip() for k in self.connections_keys.split(",") if k.strip())

    @property
    def connections_command_word(self) -> str:
        return command_word(self.connections_command)

    @property
    def connections_redirect_uri(self) -> str:
        """What each provider sends the person back to."""
        return f"{self.connections_public_url.rstrip('/')}{CALLBACK_PATH}"

    agent_max_hops: int = 4  # refuse an agent turn past this depth from a human
    agent_rate_limit_turns: int = 6  # max agent-triggered turns...
    agent_rate_window_s: float = 60.0  # ...per conversation per this window


def _csv_names(raw: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(n.strip() for n in raw.split(",") if n.strip()))


def load_settings() -> ImpiSettings:
    """Build an ImpiSettings instance from the current environment / .env.
    DOTENV_PATH relocates the .env file itself (containers mount it under
    /app/conf); unset keeps the historical ./.env."""
    # _env_file is a runtime-only pydantic-settings kwarg pyright cannot see.
    return ImpiSettings(_env_file=os.environ.get("DOTENV_PATH", ".env"))  # pyright: ignore[reportCallIssue]
