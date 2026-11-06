"""FsProfileStore: scan a directory of agent profiles and build neutral
``AgentSpec``s.

Runtime-agnostic: it reads only ``agents/<name>/agent.yaml`` (machine settings)
and never touches a concrete runtime — a runtime maps the spec onto its own
profile. The agent dir also holds the runtime's own resources (system prompt,
settings) that the engine never reads.
"""

import logging
import re
from collections.abc import Callable
from pathlib import Path

import yaml

from crucible.ports.agent import (
    DEFAULT_IDLE_TIMEOUT_S,
    AgentSpec,
    check_thinking_level,
)
from crucible.ports.agent.confinement import ConfinementLevel, check_confinement_level
from crucible.ports.agent.inherited_env import check_inherited_env
from crucible.ports.agent.model_ref import check_model_ref, check_provider_has_model
from crucible.ports.agent.spec import (
    BUDGET_KEYS,
    DELEGATION_SESSION_PER_TASK,
    DELEGATION_SESSIONS,
    TRUST_CONTEXT_TASK,
    TRUST_CONTEXTS,
    Budget,
    TrustedAgent,
)
from crucible.ports.chat.gateway import (
    GATEWAY_DELEGATION,
    GATEWAY_KINDS,
    GATEWAY_TRUSTED_DELEGATION,
)
from crucible.profiles.errors import ProfileError
from crucible.tool_manifests import MCP_SERVER_TOOLS

# One MCP server's tool, ``mcp__<server>__<tool>``: the server is the group.
_MCP_TOOL_OF = re.compile(r"^mcp__([a-z0-9](?:_?[a-z0-9-])*)__[^*]")

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
        headless = data.get("headless", False)
        if not isinstance(headless, bool):
            raise ProfileError(f"{manifest}: headless must be true or false")
        trusted_agents = _trusted_agents(runtime, manifest)
        budget = _budget(runtime, manifest)
        delegation_session = str(runtime.get("delegation_session") or DELEGATION_SESSION_PER_TASK)
        if delegation_session not in DELEGATION_SESSIONS:
            raise ProfileError(
                f"{manifest}: runtime.delegation_session must be one of {', '.join(DELEGATION_SESSIONS)}"
            )
        tools = runtime.get("tools") or []
        if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
            raise ProfileError(f"{manifest}: runtime.tools must be a list of strings")
        for tool in tools:
            # The list is the whole allowlist, so every entry names one tool.
            # A pattern, or an entry that edits the runtime's defaults, would
            # hand the agent tools nobody named. The one pattern allowed names
            # a whole MCP server — `mcp__<server>__*` — whose tools the
            # operator pinned when they declared the server.
            if MCP_SERVER_TOOLS.match(tool):
                continue
            if "*" in tool or tool.strip().startswith(("+", "-")):
                raise ProfileError(
                    f"{manifest}: runtime.tools entry {tool!r}: name each tool — no patterns "
                    "(but `mcp__<server>__*`), no +/- edits of the runtime's defaults"
                )
        tools_by_gateway = _tools_by_gateway(manifest, runtime.get("tools_by_gateway"), tuple(tools))
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
        network_ports = _network_ports(runtime, manifest)
        shared_dirs = _shared_dirs(runtime, manifest)
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
        idle_timeout = runtime.get("idle_timeout", DEFAULT_IDLE_TIMEOUT_S)
        if not isinstance(idle_timeout, (int, float)) or idle_timeout < 0:
            raise ProfileError(f"{manifest}: runtime.idle_timeout must be a number of seconds, 0 for never")
        wrap_up_after = runtime.get("wrap_up_after", 0)
        if not isinstance(wrap_up_after, (int, float)) or wrap_up_after < 0:
            raise ProfileError(f"{manifest}: runtime.wrap_up_after must be a number of seconds, 0 for never")
        if wrap_up_after and wrap_up_after >= timeout:
            raise ProfileError(
                f"{manifest}: runtime.wrap_up_after ({wrap_up_after}) must come before runtime.timeout ({timeout})"
            )
        loop_guard = _loop_guard(runtime, manifest)
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
            idle_timeout=float(idle_timeout),
            wrap_up_after=float(wrap_up_after),
            loop_guard=loop_guard,
            thinking=thinking,
            tools=tuple(tools),
            tools_by_gateway=tools_by_gateway,
            skills=skill_paths,
            inherit=inherit,
            confinement=confinement,
            confinement_readable=readable,
            confinement_writable=writable,
            network_ports=network_ports,
            shared_dirs=shared_dirs,
            fallback_models=fallback_models,
            headless=headless,
            trusted_agents=trusted_agents,
            delegation_session=delegation_session,
            budget=budget,
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


def _loop_guard(runtime: dict, manifest: Path) -> bool | dict[str, int] | None:
    """``runtime.loop_guard``: ``true`` for the runtime's defaults, a map of
    limits to whole numbers (0 turns a rule off; a key the runtime does not
    know is its to ignore), ``false`` or absent for none. An empty map names
    no limit of its own and means the defaults, as ``true`` does — the engine
    checks the shape, never the names: the limits are the runtime's."""
    raw = runtime.get("loop_guard")
    if raw is None or raw is False:
        return None
    if raw is True:
        return True
    if not isinstance(raw, dict) or not all(
        isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool) and v >= 0 for k, v in raw.items()
    ):
        raise ProfileError(f"{manifest}: runtime.loop_guard must be true, false or a map of limits to whole numbers")
    return dict(raw) if raw else True


# A shared directory's name: a path segment and a variable's tail — lowercase
# letters, digits and hyphens, so it cannot climb out of the root or carry a
# character an environment variable's name cannot.
SHARED_DIR_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _shared_dirs(runtime: dict, manifest: Path) -> tuple[str, ...]:
    """``runtime.shared_dirs``: a list of names, each once."""
    raw = runtime.get("shared_dirs") or []
    if not isinstance(raw, list) or not all(isinstance(n, str) and SHARED_DIR_NAME.match(n) for n in raw):
        raise ProfileError(
            f"{manifest}: runtime.shared_dirs must be a list of names (lowercase letters, digits, hyphens)"
        )
    return tuple(dict.fromkeys(raw))


def _budget(runtime: dict, manifest: Path) -> Budget:
    """``runtime.budget``: a map of ``tokens_per_turn``, ``tokens_per_day``,
    ``usd_per_day``, ``tokens_per_day_per_person``, ``usd_per_day_per_person``
    to numbers of zero or more (tokens whole); absent = no limit."""
    raw = runtime.get("budget")
    if raw is None:
        return Budget()
    if not isinstance(raw, dict):
        raise ProfileError(f"{manifest}: runtime.budget must be a map of {', '.join(BUDGET_KEYS)} to numbers")
    unknown = set(raw) - set(BUDGET_KEYS)
    if unknown:
        raise ProfileError(f"{manifest}: runtime.budget has unknown keys: {', '.join(sorted(unknown))}")
    values: dict[str, int | float] = {}
    for key, value in raw.items():
        whole = key.startswith("tokens")
        if isinstance(value, bool) or not isinstance(value, int | float) or value < 0 or (whole and not isinstance(value, int)):
            raise ProfileError(
                f"{manifest}: runtime.budget.{key} must be a {'whole number' if whole else 'number'} of zero or more"
            )
        values[key] = int(value) if whole else float(value)
    return Budget(**values)  # type: ignore[arg-type]


def _trusted_agents(runtime: dict, manifest: Path) -> tuple[TrustedAgent, ...]:
    """``runtime.trusted_agents``: a list of agent names (everything default),
    or a mapping of name to ``{secrets: [names], connections: bool, context:
    task|conversation}``, each name once."""
    raw = runtime.get("trusted_agents")
    if raw is None:
        return ()
    shape = f"{manifest}: runtime.trusted_agents must be a list of agent names or a mapping of name to {{secrets, connections, context}}"
    entries: list[tuple[str, dict]] = []
    if isinstance(raw, list):
        if not all(isinstance(n, str) and n.strip() for n in raw):
            raise ProfileError(shape)
        entries = [(n.strip(), {}) for n in raw]
    elif isinstance(raw, dict):
        for name, body in raw.items():
            if not isinstance(name, str) or not name.strip() or not isinstance(body, dict | type(None)):
                raise ProfileError(shape)
            entries.append((name.strip(), body or {}))
    else:
        raise ProfileError(shape)
    out: dict[str, TrustedAgent] = {}
    for name, body in entries:
        unknown = set(body) - {"secrets", "connections", "context"}
        if unknown:
            raise ProfileError(f"{manifest}: runtime.trusted_agents.{name} has unknown keys: {', '.join(sorted(unknown))}")
        secrets = body.get("secrets") or []
        if not isinstance(secrets, list) or not all(isinstance(s, str) and s.strip() for s in secrets):
            raise ProfileError(f"{manifest}: runtime.trusted_agents.{name}.secrets must be a list of secret names")
        connections = body.get("connections", False)
        if not isinstance(connections, bool):
            raise ProfileError(f"{manifest}: runtime.trusted_agents.{name}.connections must be true or false")
        context = str(body.get("context") or TRUST_CONTEXT_TASK)
        if context not in TRUST_CONTEXTS:
            raise ProfileError(
                f"{manifest}: runtime.trusted_agents.{name}.context must be one of {', '.join(TRUST_CONTEXTS)}"
            )
        out[name] = TrustedAgent(
            name=name, secrets=tuple(dict.fromkeys(s.strip() for s in secrets)), connections=connections, context=context,
        )
    return tuple(out.values())


def _network_ports(runtime: dict, manifest: Path) -> tuple[int, ...]:
    """``runtime.network_ports``: a list of TCP ports, each once."""
    raw = runtime.get("network_ports") or []
    if not isinstance(raw, list) or not all(isinstance(p, int) and not isinstance(p, bool) and 0 < p < 65536 for p in raw):
        raise ProfileError(f"{manifest}: runtime.network_ports must be a list of TCP ports (1–65535)")
    return tuple(dict.fromkeys(raw))


def _confinement_paths(runtime: dict, key: str, manifest: Path) -> tuple[Path, ...]:
    """A list of paths under ``runtime.<key>``, made absolute: a relative one is
    relative to the agent's own directory."""
    raw = runtime.get(key) or []
    if not isinstance(raw, list) or not all(isinstance(p, str) and p.strip() for p in raw):
        raise ProfileError(f"{manifest}: runtime.{key} must be a list of paths")
    return tuple((manifest.parent / p).resolve() if not Path(p).is_absolute() else Path(p) for p in raw)


def _tools_by_gateway(
    manifest: Path, raw: object, tools: tuple[str, ...],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """``runtime.tools_by_gateway``: a mapping of gateway kind to the tools a
    conversation there may use — a list of names from ``runtime.tools`` (or of
    an MCP server's tools whose ``mcp__<server>__*`` is there), or
    ``{except: [names]}`` for every entry of ``runtime.tools`` but those, each
    an entry of the list as written. Either way the agent's list stays the
    ceiling and a gateway's can only narrow it; the ``except`` form is unrolled
    here, so a tool added to ``runtime.tools`` later reaches that gateway too."""
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise ProfileError(f"{manifest}: runtime.tools_by_gateway must be a mapping of gateway to a list of tools")
    out: list[tuple[str, tuple[str, ...]]] = []
    for gateway, names in raw.items():
        if gateway not in (*GATEWAY_KINDS, GATEWAY_DELEGATION, GATEWAY_TRUSTED_DELEGATION):
            raise ProfileError(
                f"{manifest}: runtime.tools_by_gateway names {gateway!r}; a gateway is one of "
                f"{', '.join((*GATEWAY_KINDS, GATEWAY_DELEGATION, GATEWAY_TRUSTED_DELEGATION))}"
            )
        if isinstance(names, dict):
            if set(names) != {"except"} or not isinstance(names["except"], list) or not all(isinstance(n, str) for n in names["except"]):
                raise ProfileError(
                    f"{manifest}: runtime.tools_by_gateway.{gateway} is a list of tool names, or {{except: [names]}}"
                )
            for name in names["except"]:
                if name not in tools:
                    raise ProfileError(
                        f"{manifest}: runtime.tools_by_gateway.{gateway}.except names {name!r}, which runtime.tools does "
                        "not list as written"
                    )
            names = [t for t in tools if t not in names["except"]]
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise ProfileError(f"{manifest}: runtime.tools_by_gateway.{gateway} must be a list of tool names, or {{except: [names]}}")
        for name in names:
            if name in tools:
                continue
            of_server = _MCP_TOOL_OF.match(name)
            if of_server and f"mcp__{of_server.group(1)}__*" in tools:
                continue
            raise ProfileError(
                f"{manifest}: runtime.tools_by_gateway.{gateway} names {name!r}, which runtime.tools does not: "
                "a gateway's list narrows the agent's, it cannot add to it"
            )
        out.append((gateway, tuple(names)))
    return tuple(out)
