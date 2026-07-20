"""AgentSpec: one agent's neutral, runtime-agnostic configuration.

The machine half of the hybrid profile (``agent.yaml``). The personality half (a
system prompt + settings the runtime loads natively from ``profile_dir``) is
never read by the engine. A concrete runtime maps this spec onto its own profile;
this type names no runtime, so profile loading and the registry stay independent
of any backend.
"""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AgentSpec:
    name: str
    display_name: str
    role: str
    description: str
    profile_dir: Path  # the agent's config dir == the runtime's cwd
    provider: str | None = None
    model: str | None = None
    timeout: float = 180.0
    # The unified capability allowlist: built-in tool names (e.g. read/bash),
    # extension tool names, and typed tool names alike.
    tools: tuple[str, ...] = ()
    # Skill references this agent gets: absolute paths (resolved by the store) or
    # bare names the runtime resolves to its own skill layout. No ambient
    # discovery — an agent gets exactly these.
    skills: tuple[str, ...] = ()
