"""`impi escape`: an agent's own pi on the operator's host, for a person.

The engine never writes a configuration for an agent — it builds a command
line and an environment at spawn time, and the runtime reads the rest
(`.pi/SYSTEM.md`, skills, permissions) from the profile directory itself. So
"start this agent's pi by hand" is that same command line, built by the same
code, minus the engine: no RPC mode, no tool bridge, no per-turn session.

What this module decides, and why:

* **Paths are the container's, and the host is not the container.** Profiles
  and the skill library are bind mounts, so every path the engine resolved has
  a host twin; the caller says which prefix is which (`--map`) and a path with
  no twin is refused rather than handed over broken.
* **The environment is the model's and nothing else.** Chat tokens, tool
  tokens and the secret broker's variables stay in the container: on the host
  the agent is the operator, and the one thing it must not be able to do is
  present an identity from outside a container.
* **The agent is told where it is.** Its skills were written for the engine
  ("ask the operator to run …"); a short note turns those into things it does
  itself, since the operator is watching it do them.
"""

from __future__ import annotations

import shlex
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

# The engine's tool modules, imported for their registrations: what they
# register is exactly what does not exist on the host, and the registry is how
# that list stays true when a tool is added.
import crucible.builtin_tools  # noqa: F401
import impi.agent_tools  # noqa: F401
import impi.chat_tools  # noqa: F401
import impi.skill_tools  # noqa: F401
import impi.task_tools  # noqa: F401
from crucible.ports.agent import AgentSpec
from crucible.runtimes.pi.hosts.local import interactive_args
from crucible.runtimes.pi.profiles import build_pi_profile
from crucible.runtimes.pi.spawn import SpawnRequest
from crucible.tools.registry import build_registry
from impi.config import ImpiSettings
from impi.profiles import IMPI_ROOT, build_pi_env

ESCAPE_NOTE = """\
You are running on the operator's own host, started by `impi escape` in a
terminal — not in a chat, and not inside the engine. The engine's tools
(create_agent, the skill tools, open_screen, ask_user_confirm, schedule_task,
send_file) do not exist here; if a skill names one, use the file tools and the
shell instead. The `impi` wrapper is on PATH: whatever a skill tells you to
ask the operator to run — `impi start`, `impi reload`, `impi doctor`,
`impi logs`, `impi agent sync`, `impi ward …` — you run yourself, and you say
what you ran and what it answered. Re-reading profiles is `impi reload`, not
`pkill`. $IMPI_ROOT is the engine's checkout on this host and $AGENTS_PATH the
operator's agents. You hold no secret and no identity: the store's material is
the operator's, `impi ward unlock --from …` reads it without you, and you
never print or copy it.\
"""

# Env names that describe the model, and are therefore the operator's to hand
# to a pi they start themselves. Everything else the engine grants a process is
# a credential for the engine's own services, which do not exist on the host.
_MODEL_ENV = ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "NODE_TLS_REJECT_UNAUTHORIZED")


# Tools the tool-bridge extension registers by itself, beside the engine's
# typed ones: not in the registry, and just as absent without the bridge.
BRIDGE_TOOLS = frozenset({"ask_user_confirm"})


def host_tools(tools: Iterable[str]) -> tuple[str, ...]:
    """The profile's allowlist minus what only the engine could serve. pi's
    own built-ins and anything an extension of the profile adds stay."""
    engine = set(build_registry().names()) | BRIDGE_TOOLS
    return tuple(name for name in tools if name not in engine)


class EscapeError(Exception):
    """A plan that cannot be built: a path with no host twin, a bad --map."""


@dataclass(frozen=True)
class Plan:
    """Everything a shell needs to start the agent's pi: where, with what, how."""

    cwd: str
    env: dict[str, str]
    argv: tuple[str, ...]


def parse_map(spec: str) -> tuple[str, str]:
    """``FROM=TO`` — a path prefix in this container and its twin on the host."""
    src, sep, dst = spec.partition("=")
    if not sep or not src or not dst:
        raise EscapeError(f"--map wants FROM=TO, got {spec!r}")
    if not src.startswith("/") or not dst.startswith("/"):
        raise EscapeError(f"--map wants two absolute paths, got {spec!r}")
    return src.rstrip("/") or "/", dst.rstrip("/") or "/"


def map_path(path: str, maps: Sequence[tuple[str, str]]) -> str:
    """The host's name for a path of this container. Longest prefix wins; with
    no maps at all the path is its own twin (a checkout, not a container)."""
    if not maps:
        return path
    for src, dst in sorted(maps, key=lambda m: len(m[0]), reverse=True):
        if path == src:
            return dst
        if path.startswith(src + "/"):
            return dst + path[len(src):]
    raise EscapeError(f"{path} is not reachable from the host: no --map covers it")


def build_plan(
    spec: AgentSpec,
    settings: ImpiSettings,
    *,
    engine_owned: bool,
    agents_path: str,
    maps: Sequence[tuple[str, str]] = (),
    tools: Iterable[str] | None = None,
    extra_note: str = "",
) -> Plan:
    """The agent's profile as the engine would spawn it, translated to the host.

    ``tools`` replaces the profile's allowlist for this run when given (an empty
    list means no tools); ``extra_note`` is appended after ESCAPE_NOTE.
    """
    profile = build_pi_profile(spec)
    note = ESCAPE_NOTE if not extra_note else f"{ESCAPE_NOTE}\n\n{extra_note}"
    request = SpawnRequest(
        agent=profile.name,
        profile_dir=Path(map_path(str(profile.config_dir), maps)),
        # An explicit list is the operator's word and is not second-guessed.
        tools=host_tools(profile.tools) if tools is None else tuple(tools),
        skills=tuple(map_path(skill, maps) for skill in profile.skills),
        provider=profile.provider,
        model=profile.model,
        append_system_prompt=note,
    )
    env = {name: value for name, value in build_pi_env(settings).items() if name in _MODEL_ENV}
    if engine_owned:
        # What the engine hands its own agents (app.py), with the host's names.
        env["AGENTS_PATH"] = map_path(agents_path, maps)
        env["IMPI_ROOT"] = map_path(str(IMPI_ROOT), maps)
    return Plan(
        cwd=str(request.profile_dir),
        env=env,
        argv=tuple(interactive_args(request, session_dir=None)),
    )


def render_nul(plan: Plan) -> bytes:
    """One record per item, NUL-terminated: ``cwd=…``, ``env=NAME=value``,
    ``arg=…``. Readable by a POSIX shell loop (`read -r -d ''`) with no JSON
    parser on the host, and a system-prompt note full of newlines survives it."""
    records = [f"cwd={plan.cwd}"]
    records += [f"env={name}={value}" for name, value in plan.env.items()]
    records += [f"arg={arg}" for arg in plan.argv]
    return "".join(f"{record}\0" for record in records).encode("utf-8")


def render_text(plan: Plan) -> str:
    """For a person's eyes (`--dry-run`): a credential's value is not shown,
    only that it is set."""
    lines = [f"cd {shlex.quote(plan.cwd)}"]
    for name, value in plan.env.items():
        shown = "…(set)" if any(word in name for word in ("KEY", "TOKEN", "SECRET")) else value
        lines.append(f"export {name}={shlex.quote(shown)}")
    lines.append("pi " + shlex.join(plan.argv))
    return "\n".join(lines) + "\n"
