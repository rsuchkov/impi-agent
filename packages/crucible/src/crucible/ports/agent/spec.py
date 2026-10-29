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

DEFAULT_IDLE_TIMEOUT_S = 120.0

# The levels ``AgentSpec.thinking`` may name, least to most. The scale is the
# port's; a runtime driver maps it onto its own setting (one driver's happens
# to spell the same words).
THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")


def check_thinking_level(value: str) -> str:
    """``value`` as a level, or ValueError naming the ones there are."""
    level = value.strip().lower()
    if level not in THINKING_LEVELS:
        raise ValueError(f"thinking level {value!r} is not one of {', '.join(THINKING_LEVELS)}")
    return level


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
    # What of ``tools`` a conversation on a gateway may use, by gateway kind
    # (``ports.chat.gateway.GATEWAY_*``): a gateway named here gets only these
    # (a subset of ``tools``, checked when the profile loads); one not named
    # gets them all. Empty: every gateway gets every tool.
    tools_by_gateway: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def tools_on(self, gateway: str) -> tuple[str, ...] | None:
        """The tools a conversation on ``gateway`` may use: the gateway's own
        list, or None for the agent's whole ``tools``."""
        for kind, names in self.tools_by_gateway:
            if kind == gateway:
                return names
        return None
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
    # How much the model reasons before it answers, one of ``THINKING_LEVELS``;
    # "" = the runtime's own default. A level, not a token count: the runtime
    # knows what each means for the model at hand.
    thinking: str = ""
    # How long a turn may go without a sign of life from the runtime — no text,
    # no reasoning, no tool activity — before it is called stalled and stopped;
    # not counted while a tool runs or a person is being waited on. 0 = never.
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT_S
    # Active seconds after which the model is told, once, to stop calling tools
    # and say what is done and what is left (``WRAP_UP_NOTE``); the hard
    # ``timeout`` stays as the ceiling. 0 = never.
    wrap_up_after: float = 0.0
