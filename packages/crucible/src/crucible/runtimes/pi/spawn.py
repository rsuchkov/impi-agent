"""SpawnRequest: what one runtime process should BE, separately from how it is
started.

The driver used to assemble a command line inline, which tied every spawn to the
filesystem of the process doing the spawning: absolute session directories,
extension paths inside this installed package, a working directory. Naming the
pieces instead lets a host that runs somewhere else — another container, its own
filesystem, its own copy of the runtime — resolve them its own way.

Paths here are still THIS side's paths, because the local host needs them
verbatim. A host that is not this side translates them at its own boundary,
where the translation can be checked and can fail loudly, rather than being
assumed to line up.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path


def safe_session_id(raw: str) -> str:
    """Coerce a conversation key into a valid runtime session id
    (``[A-Za-z0-9._-]``, starting and ending alphanumeric)."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "-", raw).strip("-._")
    return cleaned or "session"


@dataclass(frozen=True)
class SpawnRequest:
    """One process to start, described in terms a host can honour its own way."""

    # Which agent this is. A remote host serves exactly one and checks the name,
    # so a misrouted request fails instead of running as the wrong agent.
    agent: str
    # The agent's own directory (system prompt, settings, its own skills). The
    # working directory unless ``cwd`` overrides it.
    profile_dir: Path
    # None = a memoryless run (``--no-session``). Already sanitized.
    session_id: str | None = None
    # The single capability allowlist: built-ins, extension tools and typed tools
    # alike. Empty means no tools at all.
    tools: tuple[str, ...] = ()
    # Absolute skill paths, resolved by the profile loader.
    skills: tuple[str, ...] = ()
    provider: str | None = None
    model: str | None = None
    append_system_prompt: str = ""
    # Env GRANTED to the process, on top of whatever the host's own environment
    # is. Explicit rather than "everything this process happens to hold": with a
    # host in another container, the difference between the two is the engine's
    # entire secret set.
    env: Mapping[str, str] = field(default_factory=dict)
    # Env entries whose value is a FILE this side wrote and the process must be
    # able to read (the tool manifest, today). A local host passes the path
    # through; a remote one ships the content and points the variable at its own
    # copy, because the path means nothing over there.
    env_files: Mapping[str, Path] = field(default_factory=dict)
    # Where session files live — the BASE; the host owns the per-agent layout
    # underneath it.
    session_dir: Path | None = None
    # Extensions to load, as absolute paths on this side. A host with its own
    # filesystem supplies its own instead.
    extensions: tuple[str, ...] = ()
    # Working-directory override for checkout-scoped runs; None = the profile dir.
    cwd: Path | None = None
