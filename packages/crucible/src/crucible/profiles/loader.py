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

from crucible.ports.agent import AgentSpec, check_thinking_level
from crucible.ports.agent.confinement import ConfinementLevel, check_confinement_level
from crucible.ports.agent.inherited_env import check_inherited_env
from crucible.ports.agent.model_ref import check_model_ref, check_provider_has_model
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
        # Two different failures, and they used to read as one. "Nobody wired a
        # library" is a bug in the caller and the message has to say so; telling
        # an operator to install a skill they already installed sends them to
        # look in the one place where nothing is wrong.
        if library is None:
            raise ProfileError(
                f"this profile store was built without a skill library, so the "
                f"reference {ref!r} cannot be resolved — whoever constructed it "
                f"has to pass one (library=...)"
            )
        path = library(name)
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
        inherit_override: Callable[[str], tuple[str, ...] | None] | None = None,
        confinement_override: Callable[[str], str | None] | None = None,
        default_fallback_models: tuple[str, ...] = (),
        fallback_models_override: Callable[[str], tuple[str, ...] | None] | None = None,
        # REQUIRED, with no default on purpose. It had one, and two of the three
        # places that build a store forgot to pass it — one of them for five
        # releases, because the store works perfectly until a profile happens to
        # name a `registry:` skill. A caller that genuinely has no library says
        # so with library=None, out loud, where the decision is made.
        library: Callable[[str], Path | None] | None,
        app_secret_prefixes: tuple[str, ...] = (),
    ) -> None:
        self._root = Path(profiles_path)
        # The application's own secret families, never inherited by pattern.
        self._app_secret_prefixes = app_secret_prefixes
        self._default_timeout = default_timeout
        self._default_provider = default_provider
        self._default_model = default_model
        # Per-agent skills override: given the agent name, returns the skill list
        # that replaces agent.yaml's, or None to keep it. Held here (not applied
        # once) so a hot-reload re-applies it. Kept a plain callback so the store
        # stays free of config knowledge.
        self._skills_override = skills_override
        # Same shape for the environment names an agent inherits: a set value
        # replaces the agent.yaml list, None keeps it.
        self._inherit_override = inherit_override
        # The confinement level an operator set for one agent from config.
        self._confinement_override = confinement_override
        # The models an agent turns to when its own fails: the deployment's
        # list unless agent.yaml names one, and an operator's override above both.
        self._default_fallback_models = default_fallback_models
        self._fallback_models_override = fallback_models_override
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
        for tool in tools:
            # The list is the whole allowlist, so every entry names one tool.
            # A pattern, or an entry that edits the runtime's defaults, would
            # hand the agent tools nobody named.
            if "*" in tool or tool.strip().startswith(("+", "-")):
                raise ProfileError(
                    f"{manifest}: runtime.tools entry {tool!r}: name each tool — no patterns, "
                    "no +/- edits of the runtime's defaults"
                )
        skills = runtime.get("skills") or []
        if not isinstance(skills, list) or not all(isinstance(s, str) for s in skills):
            raise ProfileError(f"{manifest}: runtime.skills must be a list of strings")
        # A config override, when present, replaces the agent.yaml list entirely.
        override = self._skills_override(name) if self._skills_override else None
        skill_refs = override if override is not None else tuple(skills)
        skill_paths = tuple(
            _resolve_skill(s, manifest.parent, self._library) for s in skill_refs
        )
        env_names = runtime.get("env") or []
        if not isinstance(env_names, list) or not all(isinstance(e, str) for e in env_names):
            raise ProfileError(f"{manifest}: runtime.env must be a list of variable names")
        # An override is checked where it is read, and its error names its own
        # key: the operator is sent to the setting, not to an agent.yaml that
        # is fine.
        inherit_override = _read_override(self._inherit_override, name)
        if inherit_override is not None:
            inherit = inherit_override
        else:
            inherit = tuple(env_names)
            for pattern in inherit:
                try:
                    check_inherited_env(pattern, reserved=self._app_secret_prefixes)
                except ValueError as exc:
                    raise ProfileError(f"{manifest}: runtime.env entry {exc}") from exc
        overridden = _read_override(self._confinement_override, name)
        confinement: ConfinementLevel | None = None
        if overridden is not None:
            confinement = check_confinement_level(overridden)  # checked already, under its own key
        elif runtime.get("confinement") is not None:
            try:
                confinement = check_confinement_level(str(runtime["confinement"]))
            except ValueError as exc:
                raise ProfileError(f"{manifest}: runtime.confinement: {exc}") from exc
        readable = _confinement_paths(runtime, "confinement_read", manifest)
        writable = _confinement_paths(runtime, "confinement_write", manifest)
        timeout = runtime.get("timeout", self._default_timeout)
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ProfileError(f"{manifest}: runtime.timeout must be a positive number")
        provider = str(runtime["provider"]).strip() if runtime.get("provider") else None
        model = str(runtime["model"]).strip() if runtime.get("model") else None
        # Blank is unset, here as in the settings.
        provider = provider or self._default_provider.strip() or None
        model = model or self._default_model.strip() or None
        try:
            check_provider_has_model(provider or "", model or "", "runtime.provider", "runtime.model")
        except ValueError as exc:
            raise ProfileError(f"{manifest}: {exc} (or the deployment's default model)") from exc
        fallback_models = self._fallback_models(name, runtime, manifest)
        thinking = ""
        if runtime.get("thinking") is not None:
            try:
                thinking = check_thinking_level(str(runtime["thinking"]))
            except ValueError as exc:
                raise ProfileError(f"{manifest}: runtime.thinking: {exc}") from exc

        return AgentSpec(
            name=name,
            display_name=str(data.get("display_name") or name),
            role=require_str("role"),
            description=str(data.get("description") or ""),
            profile_dir=manifest.parent,
            # agent.yaml value wins; else the store's default; else None (runtime decides).
            provider=provider,
            model=model,
            timeout=float(timeout),
            thinking=thinking,
            tools=tuple(tools),
            skills=skill_paths,
            inherit=inherit,
            confinement=confinement,
            confinement_readable=readable,
            confinement_writable=writable,
            fallback_models=fallback_models,
        )

    def _fallback_models(self, name: str, runtime: dict, manifest: Path) -> tuple[str, ...]:
        """The list as configured, each entry checked; what the runtime makes
        of it (skipping the agent's own model, repeats) is the runtime's."""
        overridden = _read_override(self._fallback_models_override, name)
        if overridden is not None:
            refs, where = overridden, "the override"  # checked already, under its own key
        elif runtime.get("fallback_models") is not None:
            raw = runtime["fallback_models"]
            if not isinstance(raw, list) or not all(isinstance(r, str) for r in raw):
                raise ProfileError(f"{manifest}: runtime.fallback_models must be a list of provider/model")
            refs, where = tuple(raw), f"{manifest}: runtime.fallback_models"
        else:
            refs, where = self._default_fallback_models, "the default fallback models"
        try:
            return tuple(check_model_ref(ref) for ref in refs)
        except ValueError as exc:
            raise ProfileError(f"{where}: {exc}") from exc


def _read_override[T](read: Callable[[str], T | None] | None, agent: str) -> T | None:
    """A configured override for ``agent``, or None — its own error, naming its
    key, as a ProfileError."""
    if read is None:
        return None
    try:
        return read(agent)
    except ValueError as exc:
        raise ProfileError(str(exc)) from exc


def _confinement_paths(runtime: dict, key: str, manifest: Path) -> tuple[Path, ...]:
    """A list of paths under ``runtime.<key>``, made absolute: a relative one is
    relative to the agent's own directory."""
    raw = runtime.get(key) or []
    if not isinstance(raw, list) or not all(isinstance(p, str) and p.strip() for p in raw):
        raise ProfileError(f"{manifest}: runtime.{key} must be a list of paths")
    return tuple((manifest.parent / p).resolve() if not Path(p).is_absolute() else Path(p) for p in raw)
