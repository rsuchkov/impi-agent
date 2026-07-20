"""impi application settings: the base engine/gateway Settings plus the fields
specific to this app — the engine-owned `support` agent and the inter-agent
messaging / loop-protection knobs (impi is the multi-agent configuration).
"""

from __future__ import annotations

from typing import ClassVar

from crucible.config import Settings


class ImpiSettings(Settings):
    """Base Settings + impi-specific fields. Reads the same .env; inherited env
    binding covers the added fields."""

    # Keep impi's historical inventory filename ({data_dir}/impi.db).
    DB_FILENAME: ClassVar[str] = "impi.db"

    # The engine's own `support` agent (bundled with impi) — override its
    # provider/model separately; each falls back to default_provider/default_model.
    support_provider: str = ""
    support_model: str = ""

    # Inter-agent messaging + loop protection.
    agents_reply_to_agents: bool = True  # answer other agents' mentions
    agent_max_hops: int = 4  # refuse an agent turn past this depth from a human
    agent_rate_limit_turns: int = 6  # max agent-triggered turns...
    agent_rate_window_s: float = 60.0  # ...per conversation per this window


def load_settings() -> ImpiSettings:
    """Build an ImpiSettings instance from the current environment / .env."""
    return ImpiSettings()
