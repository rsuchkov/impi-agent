"""What the engine says about its own deployment: at startup in its log, and on
request in `impi health`. One place, so the two cannot disagree — which agents
run, whether a receiver is needed, how each agent's process is confined and
which of them can read their neighbours' environments, and which models each
agent falls back to and which of those it has no login for, and which
providers people may connect their accounts at.
"""

from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from crucible.gateways import GatewayConfig, needs_http_receiver
from crucible.ports.agent import AgentSpec
from crucible.profiles import ProfileStore
from impi.config import ImpiSettings
from impi.connections_config import ConnectionProvider

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


def fallback_report(
    specs: Sequence[AgentSpec],
    configs: Mapping[str, GatewayConfig | None],
    *,
    providers_for: Callable[[str], Collection[str]],
) -> tuple[str | None, str | None]:
    """Which running agents move on to which models when theirs fails, as the
    operator wrote it — and the fallback models whose provider the runtime
    that agent runs on has no login or definition for (``providers_for``):
    it would refuse to switch to them. (None, None) when no agent names any."""
    with_fallbacks = {s.name: s for s in specs if configs.get(s.name) is not None and s.fallback_models}
    if not with_fallbacks:
        return None, None
    # Not "fallback: " — that is how the runtime words a switch, and an
    # operator searching the log for switches should find switches.
    line = "fallback models: " + "; ".join(
        f"{name}: {' → '.join(spec.fallback_models)}" for name, spec in sorted(with_fallbacks.items())
    )
    missing = [
        f"{name} → {ref}" for name, spec in sorted(with_fallbacks.items())
        for ref in spec.fallback_models if ref.partition("/")[0] not in providers_for(name)
    ]
    warning = (
        "fallback models with no login or definition for their provider where the agent "
        "runs: " + ", ".join(missing) + " — the runtime will not switch to them (an agent "
        "in its own container has the credential file alone, not models.json; a key taken "
        "from the environment is not seen here)"
    ) if missing else None
    return line, warning


# Gateways whose users carry no email the engine can read.
_NO_EMAIL_GATEWAYS = frozenset({"ws", "http"})
_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})


@dataclass
class ConnectionsReport:
    line: str | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def connections_report(
    settings: ImpiSettings,
    providers: Mapping[str, ConnectionProvider],
    configs: Mapping[str, GatewayConfig | None],
    *,
    tools_on: bool,
    widgets_on: bool,
) -> ConnectionsReport:
    """Which providers people may connect to and from which agents, the address
    to register with each, and what keeps a connect from finishing or a tool
    from acting. An empty report when no provider is configured."""
    report = ConnectionsReport()
    if not providers:
        return report
    running = sorted(name for name, cfg in configs.items() if cfg is not None)
    report.line = (
        "connections: " + "; ".join(
            f"{name} (agents: {', '.join(p.agents) if p.agents is not None else 'all'})"
            for name, p in sorted(providers.items())
        )
        + f" — register {settings.connections_redirect_uri} as the redirect URI with each; "
        f"people connect with /{settings.connections_command} in a direct message"
    )
    if not settings.sealing_keys():
        report.errors.append(
            "CONNECTIONS_KEYS is empty: the engine will not start with connections configured "
            "— generate a key with `impi connections key`"
        )
    if not settings.connections_public_url:
        report.errors.append(
            "CONNECTIONS_PUBLIC_URL is empty: the engine will not start with connections "
            "configured — set the address a person's browser reaches the callback on"
        )
    else:
        url = urlsplit(settings.connections_public_url)
        if url.scheme != "https" and (url.hostname or "") not in _LOOPBACK:
            report.warnings.append(
                f"CONNECTIONS_PUBLIC_URL is {url.scheme}:// — the code a provider sends back "
                "travels in clear; put the callback behind TLS"
            )
    if not widgets_on:
        report.warnings.append(
            "interactivity is off (INTEGRATIONS_ENABLED): the connections screen has no way to "
            "open, so nobody can connect an account"
        )
    if not tools_on:
        report.warnings.append(
            "the tool server is off (TOOL_ENABLED): people can connect, but no agent can act "
            "as them"
        )
    for name, p in sorted(providers.items()):
        allowed = running if p.agents is None else [a for a in p.agents if a in running]
        unknown = [] if p.agents is None else [a for a in p.agents if a not in running]
        if unknown:
            report.warnings.append(
                f"CONNECTION_AGENTS__{name.upper().replace('-', '_')} names agents this engine "
                f"does not run: {', '.join(unknown)}"
            )
        no_email = [a for a in allowed if (cfg := configs.get(a)) is not None and cfg.kind in _NO_EMAIL_GATEWAYS]
        if p.match_email and no_email:
            report.warnings.append(
                f"{name} matches the account's email to the chat user's, but {', '.join(no_email)} "
                "run on a gateway whose users show no email — every connect from there is refused "
                f"(CONNECTION_MATCH_EMAIL__{name.upper().replace('-', '_')}=false lifts it)"
            )
    return report


def agents_dir_extensions(ext_root: Path, names: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """What the agents directory offers and what the deployment chose:
    (present, enabled), both sorted, the second a subset of the first in the
    order the operator named them."""
    present = tuple(sorted(
        index.parent.name for index in ext_root.glob("*/index.ts") if index.is_file()
    ))
    enabled = tuple(name for name in names if name in present)
    return present, enabled
