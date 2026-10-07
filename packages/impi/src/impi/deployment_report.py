"""What the engine says about its own deployment: at startup in its log, and on
request in `impi health`. One place, so the two cannot disagree — which agents
run, whether a receiver is needed, how each agent's process is confined and
which of them can read their neighbours' environments.
"""

from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path

from crucible.gateways import GatewayConfig, needs_http_receiver
from crucible.ports.agent import AgentSpec
from crucible.profiles import ProfileStore
from impi.config import ImpiSettings

# What lets a process read a neighbour's /proc/<pid>/environ: a shell, or a
# built-in that reads files.
_ENVIRON_READERS = frozenset({"bash", "read", "grep", "edit"})

SHARED_PROCESS_WARNING = (
    "agents %s run unconfined in the engine's own process space with a shell: each "
    "can read the environment of every other process here — the engine's own, "
    "support's, every other agent's tool token and session proofs "
    "(/proc/<pid>/environ). The boundary is confinement (RUNTIME_CONFINEMENT, on a "
    "kernel with Landlock) or a container per agent — docs/security.md"
)


def select_specs(settings: ImpiSettings, user_store: ProfileStore, engine_store: ProfileStore) -> list[AgentSpec]:
    """The agents the engine builds: the user's (all, or AGENTS_ENABLED) and
    its own. Whether each runs is its gateway's to say."""
    names = settings.enabled_agent_names()
    users = user_store.list() if names is None else [user_store.get(name) for name in names]
    return [*users, *engine_store.list()]


def needs_receiver(configs: Iterable[GatewayConfig | None]) -> bool:
    """Whether any running agent's platform calls back over HTTP."""
    return any(cfg is not None and needs_http_receiver(cfg.kind) for cfg in configs)


def shared_process_agents(
    specs: Sequence[AgentSpec],
    *,
    in_process: Callable[[str], bool],
    confined: Callable[[str], bool] = lambda _name: False,
) -> tuple[str, ...]:
    """The user's agents that run in the engine's process space unconfined AND
    can read a neighbour's environment. One is enough: the engine itself and
    its own agent (support) are always neighbours, and their environments are
    the engine's. A confined process cannot read another's environment — the
    kernel refuses it outside the process's own domain — so it is not one of
    them. See docs/security.md."""
    return tuple(sorted(
        spec.name for spec in specs
        if in_process(spec.name) and not confined(spec.name) and _ENVIRON_READERS & set(spec.tools)
    ))


def confinement_line(levels: Mapping[str, str], abi: int) -> str:
    """What confines the agents' processes and which run without it. ``abi``
    0: the kernel has no Landlock, and every level is a wish."""
    if not abi:
        return "confinement: this kernel has no Landlock — every agent's process runs unconfined"
    confined = ", ".join(f"{n}={lvl}" for n, lvl in sorted(levels.items()) if lvl != "off")
    off = ", ".join(sorted(n for n, lvl in levels.items() if lvl == "off"))
    return f"confinement: Landlock ABI {abi}; {confined or 'no agent confined'}; unconfined: {off or 'none'}"


def confinement_report(
    specs: Sequence[AgentSpec],
    configs: Mapping[str, GatewayConfig | None],
    engine_names: frozenset[str] | set[str],
    *,
    level_for: Callable[[str], str],
    abi: int,
    in_process: Callable[[str], bool],
) -> tuple[str, str | None]:
    """The confinement line, and the shared-process warning when one is due —
    over the agents that run (those with a gateway)."""
    running = [s for s in specs if configs.get(s.name) is not None]
    line = confinement_line({s.name: level_for(s.name) for s in running}, abi)
    shared = shared_process_agents(
        [s for s in running if s.name not in engine_names],
        in_process=in_process,
        confined=lambda name: bool(abi) and level_for(name) != "off",
    )
    return line, (SHARED_PROCESS_WARNING % ", ".join(shared)) if shared else None


def agents_dir_extensions(ext_root: Path, names: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """What the agents directory offers and what the deployment chose:
    (present, enabled), both sorted, the second a subset of the first in the
    order the operator named them."""
    present = tuple(sorted(
        index.parent.name for index in ext_root.glob("*/index.ts") if index.is_file()
    ))
    enabled = tuple(name for name in names if name in present)
    return present, enabled
