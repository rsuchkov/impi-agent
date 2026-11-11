"""AgentSpec: one agent's neutral, runtime-agnostic configuration.

The machine half of the hybrid profile (``agent.yaml``). The personality half (a
system prompt + settings the runtime loads natively from ``profile_dir``) is
never read by the engine. A concrete runtime maps this spec onto its own profile;
this type names no runtime, so profile loading and the registry stay independent
of any backend.
"""

from collections.abc import Mapping
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


# What a trusted agent is handed of the asker's conversation: the task alone,
# or the conversation so far as well (where the platform keeps it).
TRUST_CONTEXT_TASK = "task"
TRUST_CONTEXT_CONVERSATION = "conversation"
TRUST_CONTEXTS = (TRUST_CONTEXT_TASK, TRUST_CONTEXT_CONVERSATION)
# How an agent keeps the turns on tasks handed to it: a session per task, or one
# per asking conversation (the same project, the same fields, remembered).
DELEGATION_SESSION_PER_TASK = "per_task"
DELEGATION_SESSION_PER_CONVERSATION = "per_conversation"
DELEGATION_SESSIONS = (DELEGATION_SESSION_PER_TASK, DELEGATION_SESSION_PER_CONVERSATION)


@dataclass(frozen=True)
class Budget:
    """What an agent may spend at the model: tokens in one turn, and tokens or
    dollars (the runtime's own estimate) in one calendar day, UTC — for the
    agent as a whole and for each person it answers. 0 = no limit of that kind."""

    tokens_per_turn: int = 0
    tokens_per_day: int = 0
    usd_per_day: float = 0.0
    tokens_per_day_per_person: int = 0
    usd_per_day_per_person: float = 0.0

    @property
    def daily(self) -> bool:
        """Whether any rule needs the day's spending read before a turn."""
        return bool(self.tokens_per_day or self.usd_per_day or self.tokens_per_day_per_person or self.usd_per_day_per_person)


BUDGET_KEYS = ("tokens_per_turn", "tokens_per_day", "usd_per_day", "tokens_per_day_per_person", "usd_per_day_per_person")


@dataclass(frozen=True)
class TrustedAgent:
    """An agent this one hands tasks to without a card, as the person it acts
    for — the same person, the same deployment: a specialist of its own that
    only knows more of one area. ``secrets`` names the turn secrets handed
    over (none by default), ``connections`` whether the person's connected
    accounts are, ``context`` whether the conversation so far goes with the
    task."""

    name: str
    secrets: tuple[str, ...] = ()
    connections: bool = False
    context: str = TRUST_CONTEXT_TASK


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
    # The tools a channel's conversation — the channel, a thread in it — may
    # use, on any gateway (``runtime.tools_in_channels``); None = no narrowing.
    tools_in_channels: tuple[str, ...] | None = None
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
    # TCP ports its confined processes may connect to beside the deployment's
    # (an internal service on a port of its own); nothing when the deployment
    # does not restrict the network.
    network_ports: tuple[int, ...] = ()
    # Directories this agent shares with every other agent naming the same
    # — by name; the application keeps them under one root and tells each
    # process where its are. Where two agents exchange files on a task.
    shared_dirs: tuple[str, ...] = ()
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
    # The agent's loop guard, as written in the profile: ``True`` for the
    # runtime's defaults, a map of limits (``repeat``, ``same_result``,
    # ``calls``), or None — off, the default. What the limits mean and what a
    # guard does is the runtime's (its extension reads the value); the engine
    # only hands it on.
    loop_guard: bool | Mapping[str, int] | None = None
    # An agent nobody talks to: no gateway of its own, reached only when
    # another agent hands it a task (``ports.delegation``). The application
    # runs it without a platform account; the directory lists it without one.
    headless: bool = False
    # Agents this one hands tasks to without a card, as the person, with what
    # each entry says of the person's (``ask_agent``); every other agent is
    # handed a task with a card and nothing of the person's (``delegate``).
    trusted_agents: tuple[TrustedAgent, ...] = ()
    # How this agent keeps the turns on tasks handed to it (``DELEGATION_SESSIONS``).
    delegation_session: str = DELEGATION_SESSION_PER_TASK
    # What it may spend at the model (``runtime.budget``); none by default.
    budget: Budget = Budget()

    def trust_in(self, name: str) -> TrustedAgent | None:
        """What this agent hands ``name`` of the person's, or None: not trusted."""
        return next((t for t in self.trusted_agents if t.name == name), None)

    def tools_on(self, gateway: str) -> tuple[str, ...] | None:
        """The tools a conversation on ``gateway`` may use: the gateway's own
        list, or None for the agent's whole ``tools``."""
        for kind, names in self.tools_by_gateway:
            if kind == gateway:
                return names
        return None
