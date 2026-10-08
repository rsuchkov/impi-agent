"""AgentSpec: one agent's neutral, runtime-agnostic configuration.

The machine half of the hybrid profile (``agent.yaml``). The personality half (a
system prompt + settings the runtime loads natively from ``profile_dir``) is
never read by the engine. A concrete runtime maps this spec onto its own profile;
this type names no runtime, so profile loading and the registry stay independent
of any backend.
"""

from dataclasses import dataclass
from pathlib import Path

from crucible.ports.agent.confinement import ConfinementLevel


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
    # Names of variables in the ENGINE's environment that this agent's runtime
    # process may inherit, beside the base set every process gets (PATH, HOME,
    # the user, locale, proxies, certificates, PI_*). A runtime process is handed what the
    # engine grants it, never everything the engine happens to hold: the
    # difference is the engine's entire secret set. Globs are allowed
    # (``MYSERVICE_*``), checked by ``inherited_env.check_inherited_env``.
    inherit: tuple[str, ...] = ()
    # How far this agent's processes are confined; None = the deployment's
    # default. Paths beyond its own it may read or write, for a skill that
    # needs more (a mounted checkout, a cache it keeps) — absolute here; the
    # profile may give them relative to its own directory.
    confinement: ConfinementLevel | None = None
    confinement_readable: tuple[Path, ...] = ()
    confinement_writable: tuple[Path, ...] = ()
    # Models to turn to, in order, when a turn's model fails in a way the
    # runtime retries (an overloaded or unreachable service), as
    # ``provider/model`` (``model_ref.check_model_ref``). The runtime skips
    # the agent's own model and repeats when it starts a process.
    fallback_models: tuple[str, ...] = ()

