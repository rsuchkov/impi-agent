"""PiProfile: the pi-side runtime configuration of one agent.

A profile is a config dir (the agent's own directory, holding
``.pi/SYSTEM.md`` + settings + permission policy) plus model/timeout/tool
settings mapped onto pi CLI flags. Building profiles from ``agent.yaml`` is the
job of ``crucible.profiles`` (the ProfileStore); this module only defines the
shape the pi driver consumes.
"""

import dataclasses
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from crucible.ports.agent import AgentProfile, AgentSpec
from crucible.tool_manifests import MANIFEST_ENV


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
    # Names (globs allowed) of the engine's environment variables this agent's
    # processes inherit beside the base set; ``inherit_all`` hands over the
    # whole environment instead — for an agent that is part of the engine.
    inherit: tuple[str, ...] = ()
    inherit_all: bool = False
    # Extra text appended to the agent's system prompt (--append-system-prompt),
    # e.g. gateway-specific response-formatting rules. "" = nothing appended.
    append_system_prompt: str = ""
    # Whether the system prompt also explains the envelope the flow wraps each
    # prompt in (ports/agent/envelope.py), ahead of ``append_system_prompt``.
    # Off only for a process whose prompts are not framed that way.
    envelope_note: bool = True
    # Models the process turns to, in order, when its own fails in a way the
    # runtime retries ("provider/model"); see extension/fallback.ts.
    fallback_models: tuple[str, ...] = ()
    # The runtime's thinking level (``--thinking``); "" = its default.
    thinking: str = ""
    # Read per turn like ``timeout``, so neither is part of the spawn
    # fingerprint: seconds without an event before the turn is called stalled
    # (0 = never), and active seconds after which the model is told to wrap up
    # (0 = never).
    idle_timeout: float = 120.0
    wrap_up_after: float = 0.0


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
        inherit=spec.inherit,
        fallback_models=spec.fallback_models,
        thinking=spec.thinking,
        idle_timeout=spec.idle_timeout,
        wrap_up_after=spec.wrap_up_after,
    )


def require_pi_profile(profile: AgentProfile) -> PiProfile:
    """Narrow the opaque port profile at the driver boundary (no cast upstream)."""
    if not isinstance(profile, PiProfile):
        raise TypeError(f"PiRuntime needs a PiProfile, got {type(profile).__name__}")
    return profile


def with_tool_env(profile: PiProfile, env: Mapping[str, str] | None) -> PiProfile:
    """Layer an agent's tool env (``ToolWiring.profile_env``, ``session_env``)
    onto its profile. The manifest is a PATH and goes in ``env_files``, apart
    from the rest, so a host with its own filesystem is handed the content
    instead of a path that means nothing there. None leaves the profile as it
    is: there are no typed tools."""
    if env is None:
        return profile
    values = dict(env)
    manifest = values.pop(MANIFEST_ENV, "")
    return dataclasses.replace(
        profile,
        env=values,
        env_files={MANIFEST_ENV: manifest} if manifest else {},
    )


def narrow_pi_profile(
    profile: AgentProfile,
    *,
    tools: Collection[str] | None,
    instructions: str,
    env: Mapping[str, str] | None,
) -> PiProfile:
    """This driver's ``ProfileNarrowing`` (``crucible.ports.entitlement``): the
    agent's profile as one conversation's process gets it. ``--tools`` keeps
    only the names allowed — built-ins included, since it is one list — so a
    tool the conversation was not given is not offered to the model either;
    the instructions follow the profile's own appended prompt; the env is the
    conversation's (its manifest among it)."""
    base = require_pi_profile(profile)
    if tools is not None:
        allowed = set(tools)
        base = dataclasses.replace(base, tools=tuple(t for t in base.tools if t in allowed))
    if instructions.strip():
        joined = "\n\n".join(p for p in (base.append_system_prompt, instructions.strip()) if p)
        base = dataclasses.replace(base, append_system_prompt=joined)
    return with_tool_env(base, env)


def _resolve_skill(ref: str, profile_dir: Path) -> str:
    """A bare skill name resolves to pi's per-agent skill layout
    (``<profile>/.pi/skills/<name>``); absolute paths (already resolved by the
    loader) pass through. This is the only place that knows pi's skill dir."""
    if Path(ref).is_absolute():
        return ref
    return str((profile_dir / ".pi" / "skills" / ref).resolve())
