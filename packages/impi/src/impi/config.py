"""impi application settings: the base engine/gateway Settings plus the fields
specific to this app — the engine-owned `support` agent and the inter-agent
messaging / loop-protection knobs (impi is the multi-agent configuration).
"""

from __future__ import annotations

import os
import re
from typing import ClassVar

from crucible.config import Settings

# A directory name under _extensions/, nothing that could walk out of it.
EXTENSION_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


class ImpiSettings(Settings):
    """Base Settings + impi-specific fields. Reads the same .env; inherited env
    binding covers the added fields."""

    # What this engine calls its inventory: {data_dir}/impi.db on SQLite, the
    # `impi` database on MongoDB.
    STORE_NAME: ClassVar[str] = "impi"

    # The engine's own `support` agent (bundled with impi) — override its
    # provider/model separately; each falls back to default_provider/default_model.
    support_provider: str = ""
    support_model: str = ""

    # Inter-agent messaging + loop protection.
    agents_reply_to_agents: bool = True  # answer other agents' mentions
    # Which of AGENTS_PATH/_extensions/<name>/index.ts load into every agent's
    # runtime: names, CSV. Nothing is loaded for being present — an extension
    # is code in every agent's process, and a directory any agent with `write`
    # can add to is not where that decision belongs.
    runtime_extensions: str = ""

    def runtime_extension_names(self) -> tuple[str, ...]:
        names = tuple(dict.fromkeys(n.strip() for n in self.runtime_extensions.split(",") if n.strip()))
        for name in names:
            if not EXTENSION_NAME_RE.fullmatch(name):
                raise ValueError(
                    f"RUNTIME_EXTENSIONS: {name!r} is not an extension name (a directory "
                    "name under _extensions/: letters, digits, '-' and '_')"
                )
        return names
    agent_max_hops: int = 4  # refuse an agent turn past this depth from a human
    agent_rate_limit_turns: int = 6  # max agent-triggered turns...
    agent_rate_window_s: float = 60.0  # ...per conversation per this window


def load_settings() -> ImpiSettings:
    """Build an ImpiSettings instance from the current environment / .env.
    DOTENV_PATH relocates the .env file itself (containers mount it under
    /app/conf); unset keeps the historical ./.env."""
    # _env_file is a runtime-only pydantic-settings kwarg pyright cannot see.
    return ImpiSettings(_env_file=os.environ.get("DOTENV_PATH", ".env"))  # pyright: ignore[reportCallIssue]
