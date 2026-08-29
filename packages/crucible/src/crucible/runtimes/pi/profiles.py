"""PiProfile: the pi-side runtime configuration of one agent.

A profile is a config dir (the agent's own directory, holding
``.pi/SYSTEM.md`` + settings + permission policy) plus model/timeout/tool
settings mapped onto pi CLI flags. Building profiles from ``agent.yaml`` is the
job of ``crucible.profiles`` (the ProfileStore); this module only defines the
shape the pi driver consumes.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from crucible.ports.agent import AgentSpec


@dataclass(frozen=True)
class PiProfile:
    name: str
    config_dir: Path
    timeout: float
    # The single capability allowlist passed as --tools: built-in tool names
    # (read/bash/edit/write/...), extension tool names and typed tool names
    # alike. Empty = no tools at all. Naming a built-in is the only way to enable
    # it, so a coding agent lists read/bash/edit/write here.
    tools: tuple[str, ...] = ()
    # Skill dirs/files (absolute) this agent gets via --skill; ambient skill
    # discovery is disabled (--no-skills), so the set is exactly these.
    skills: tuple[str, ...] = ()
    provider: str | None = None
    model: str | None = None
    # Per-agent env for this profile's pi subprocesses (merged on top of the
    # runtime's shared env). Carries per-agent secrets like the tool-server
    # token, which must NOT be shared across agents.
    env: Mapping[str, str] = field(default_factory=dict)
    # Env entries whose value is a FILE the engine wrote and the process must be
    # able to read — the tool manifest, today. Kept apart from ``env`` because a
    # path is only meaningful where it was written: a host with its own
    # filesystem is handed the content and writes its own copy.
    env_files: Mapping[str, str] = field(default_factory=dict)
    # Extra text appended to the agent's system prompt (--append-system-prompt),
    # e.g. gateway-specific response-formatting rules. "" = nothing appended.
    append_system_prompt: str = ""


def build_pi_profile(spec: AgentSpec) -> PiProfile:
    """Map a neutral ``AgentSpec`` onto this driver's profile. The pi runtime
    owns its own spec->profile mapping, so the profile loader stays runtime-
    agnostic. Per-agent env (tool token/manifest) is layered on at the
    composition root, not here."""
    return PiProfile(
        name=spec.name,
        config_dir=spec.profile_dir,
        timeout=spec.timeout,
        tools=spec.tools,
        skills=tuple(_resolve_skill(s, spec.profile_dir) for s in spec.skills),
        provider=spec.provider,
        model=spec.model,
    )


def _resolve_skill(ref: str, profile_dir: Path) -> str:
    """A bare skill name resolves to pi's per-agent skill layout
    (``<profile>/.pi/skills/<name>``); absolute paths (already resolved by the
    loader) pass through. This is the only place that knows pi's skill dir."""
    if Path(ref).is_absolute():
        return ref
    return str((profile_dir / ".pi" / "skills" / ref).resolve())
