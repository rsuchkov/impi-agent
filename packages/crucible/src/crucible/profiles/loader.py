"""FsProfileStore: scan a directory of agent profiles and build neutral
``AgentSpec``s.

Runtime-agnostic: it reads only ``agents/<name>/agent.yaml`` (machine settings)
and never touches a concrete runtime — a runtime maps the spec onto its own
profile. The agent dir also holds the runtime's own resources (system prompt,
settings) that the engine never reads.
"""

import logging
from collections.abc import Callable
from pathlib import Path

import yaml

from crucible.ports.agent import AgentSpec
from crucible.profiles.errors import ProfileError

logger = logging.getLogger(__name__)


# A skill from the shared library rather than the agent's own directory.
LIBRARY_PREFIX = "registry:"


def _resolve_skill(
    ref: str, profile_dir: Path, library: Callable[[str], Path | None] | None
) -> str:
    """Resolve a skill reference. Three forms, in order: ``registry:<name>`` is a
    skill from the shared library; an absolute or path-like ref resolves against
    the profile dir; a bare name passes through untouched for the runtime to
    resolve to its own per-agent skill layout."""
    if ref.startswith(LIBRARY_PREFIX):
        name = ref[len(LIBRARY_PREFIX):].strip()
        path = library(name) if library else None
        if path is None:
            raise ProfileError(
                f"unknown library skill {name!r} — install it first "
                f"(a missing skill would otherwise reach the runtime as a broken path)"
            )
        return str(path)
    if Path(ref).is_absolute():
        return ref
    if "/" not in ref and "\\" not in ref:
        return ref
    return str((profile_dir / ref).resolve())


class FsProfileStore:
    """Loads agent profiles from a directory of ``agents/<name>/agent.yaml`` — the
    user's agents directory or any profiles directory (e.g. the engine's built-in
    agents). A plain directory; nothing here assumes it is a git repo.

    Profiles are read once at construction; ``reload()`` re-scans (hot-reload
    hooks in later). Unknown yaml keys are ignored on purpose — the profiles may
    be newer than the engine.
    """

    def __init__(
        self,
        profiles_path: str | Path,
        *,
        default_timeout: float = 180.0,
        default_provider: str = "",
        default_model: str = "",
        skills_override: Callable[[str], tuple[str, ...] | None] | None = None,
        library: Callable[[str], Path | None] | None = None,
    ) -> None:
        self._root = Path(profiles_path)
        self._default_timeout = default_timeout
        self._default_provider = default_provider
        self._default_model = default_model
        # Per-agent skills override: given the agent name, returns the skill list
        # that replaces agent.yaml's, or None to keep it. Held here (not applied
        # once) so a hot-reload re-applies it. Kept a plain callback so the store
        # stays free of config knowledge.
        self._skills_override = skills_override
        # Resolves a shared-library skill name to its directory (None = unknown).
        # A callback for the same reason: the library lives in config, not here.
        self._library = library
        self._specs: dict[str, AgentSpec] = {}
        self.reload()

    def reload(self) -> None:
        agents_dir = self._root / "agents"
        if not agents_dir.is_dir():
            raise ProfileError(f"Profiles path has no agents/ directory: {self._root}")
        specs: dict[str, AgentSpec] = {}
        for manifest in sorted(agents_dir.glob("*/agent.yaml")):
            spec = self._parse(manifest)
            specs[spec.name] = spec
        if not specs:
            raise ProfileError(f"No agent.yaml found under {agents_dir}")
        self._specs = specs
        logger.info("Loaded %d agent profile(s): %s", len(specs), ", ".join(specs))

    def list(self) -> list[AgentSpec]:
        return list(self._specs.values())

    def get(self, name: str) -> AgentSpec:
        try:
            return self._specs[name]
        except KeyError:
            raise ProfileError(
                f"Unknown agent {name!r}; available: {', '.join(self._specs)}"
            ) from None

    # -- internals ----------------------------------------------------------

    def _parse(self, manifest: Path) -> AgentSpec:
        try:
            data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ProfileError(f"{manifest}: invalid YAML: {exc}") from exc
        if not isinstance(data, dict):
            raise ProfileError(f"{manifest}: expected a mapping at the top level")

        def require_str(key: str) -> str:
            value = data.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ProfileError(f"{manifest}: missing or empty required field {key!r}")
            return value.strip()

        name = require_str("name")
        if name != manifest.parent.name:
            raise ProfileError(
                f"{manifest}: name {name!r} must match its directory {manifest.parent.name!r}"
            )

        # The platform account is bound by the per-agent token (env) and its
        # identity is discovered at gateway login — the profile stays neutral
        # and declares no platform username.
        # Neutral runtime-config block: provider/model/timeout/tools/skills — all
        # generic, so it names no concrete runtime.
        runtime = data.get("runtime") or {}
        if not isinstance(runtime, dict):
            raise ProfileError(f"{manifest}: 'runtime' must be a mapping")
        tools = runtime.get("tools") or []
        if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
            raise ProfileError(f"{manifest}: runtime.tools must be a list of strings")
        skills = runtime.get("skills") or []
        if not isinstance(skills, list) or not all(isinstance(s, str) for s in skills):
            raise ProfileError(f"{manifest}: runtime.skills must be a list of strings")
        # A config override, when present, replaces the agent.yaml list entirely.
        override = self._skills_override(name) if self._skills_override else None
        skill_refs = override if override is not None else tuple(skills)
        skill_paths = tuple(
            _resolve_skill(s, manifest.parent, self._library) for s in skill_refs
        )
        timeout = runtime.get("timeout", self._default_timeout)
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ProfileError(f"{manifest}: runtime.timeout must be a positive number")

        return AgentSpec(
            name=name,
            display_name=str(data.get("display_name") or name),
            role=require_str("role"),
            description=str(data.get("description") or ""),
            profile_dir=manifest.parent,
            # agent.yaml value wins; else the store's default; else None (runtime decides).
            provider=str(runtime["provider"]).strip() if runtime.get("provider") else (self._default_provider or None),
            model=str(runtime["model"]).strip() if runtime.get("model") else (self._default_model or None),
            timeout=float(timeout),
            tools=tuple(tools),
            skills=skill_paths,
        )
