"""Turning a spawn request into a command line, on this side of the wire.

The engine sends names — an agent, a session id, tool names, skill references
against a mounted root. Nothing here trusts them further than it has to: a
reference that resolves outside its root, a session id that could climb out of
the session directory, an environment name that is not one, are all refused with
a reason. The engine is not an attacker, but it is a program, and a bug there
should surface here as a refusal rather than as a runtime reading somebody
else's directory.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from runtime_relay import wire
from runtime_relay.config import HostConfig

# The runtime's own rule: alphanumeric at both ends, dots/dashes/underscores in
# between. Enforced here because the id becomes a filename in the session dir.
SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class SpawnRejected(Exception):
    """The request will not be honoured, with a reason the engine can log."""


@dataclass(frozen=True)
class Spawn:
    """One accepted request, with everything resolved to this host's paths."""

    session_id: str | None = None
    tools: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    provider: str | None = None
    model: str | None = None
    append_system_prompt: str = ""
    env: Mapping[str, str] = field(default_factory=dict)
    # Variable name -> file content the engine shipped. Written to a scratch
    # directory when the process starts, and the variable points there.
    env_files: Mapping[str, str] = field(default_factory=dict)


def parse(payload: object, config: HostConfig) -> Spawn:
    if not isinstance(payload, dict):
        raise SpawnRejected("the spawn frame is not an object")
    version = payload.get(wire.KEY_VERSION)
    if version != wire.PROTOCOL_VERSION:
        raise SpawnRejected(
            f"this host speaks protocol {wire.PROTOCOL_VERSION}, the engine speaks "
            f"{version!r} — one side has not been updated"
        )
    agent = payload.get(wire.KEY_AGENT)
    if agent != config.agent:
        raise SpawnRejected(
            f"this host serves {config.agent!r}, not {agent!r}"
        )
    return Spawn(
        session_id=_session_id(payload.get(wire.KEY_SESSION_ID)),
        tools=_strings(payload.get(wire.KEY_TOOLS), wire.KEY_TOOLS),
        skills=tuple(
            _skill(ref, config) for ref in _sequence(payload.get(wire.KEY_SKILLS))
        ),
        provider=_optional_str(payload.get(wire.KEY_PROVIDER), wire.KEY_PROVIDER),
        model=_optional_str(payload.get(wire.KEY_MODEL), wire.KEY_MODEL),
        append_system_prompt=str(payload.get(wire.KEY_SYSTEM_SUFFIX) or ""),
        env=_env(payload.get(wire.KEY_ENV), wire.KEY_ENV),
        env_files=_env(payload.get(wire.KEY_ENV_FILES), wire.KEY_ENV_FILES),
    )


def command_args(spawn: Spawn, *, config: HostConfig, session_dir: Path | None) -> list[str]:
    """The command line for one spawn. Deliberately the same shape the engine
    builds when it runs the process itself — a contract test holds the two to
    each other, because an agent must not behave differently for having moved."""
    args = ["--mode", "rpc", "--approve"]
    if spawn.session_id:
        args += ["--session-id", spawn.session_id]
    else:
        args += ["--no-session"]
    if session_dir is not None:
        args += ["--session-dir", str(session_dir)]
    for extension in config.extensions:
        args += ["-e", str(extension)]
    args += ["--tools", ",".join(spawn.tools)]
    args += ["--no-skills"]
    for skill in spawn.skills:
        args += ["--skill", skill]
    if spawn.provider:
        args += ["--provider", spawn.provider]
    if spawn.model:
        args += ["--model", spawn.model]
    if spawn.append_system_prompt:
        args += ["--append-system-prompt", spawn.append_system_prompt]
    return args


# -- validation ---------------------------------------------------------------


def _session_id(raw: object) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not SESSION_ID_RE.fullmatch(raw):
        raise SpawnRejected(f"{raw!r} is not a usable session id")
    return raw


def _sequence(raw: object) -> list[object]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise SpawnRejected("expected a list")
    return raw


def _strings(raw: object, field_name: str) -> tuple[str, ...]:
    values = _sequence(raw)
    if any(not isinstance(value, str) for value in values):
        raise SpawnRejected(f"{field_name} must be a list of strings")
    return tuple(str(value) for value in values)


def _optional_str(raw: object, field_name: str) -> str | None:
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str):
        raise SpawnRejected(f"{field_name} must be a string")
    return raw


def _env(raw: object, field_name: str) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise SpawnRejected(f"{field_name} must be an object")
    result: dict[str, str] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not ENV_NAME_RE.fullmatch(name):
            raise SpawnRejected(f"{name!r} is not an environment variable name")
        if not isinstance(value, str):
            raise SpawnRejected(f"{field_name}[{name}] must be a string")
        result[name] = value
    return result


def _skill(ref: object, config: HostConfig) -> str:
    if not isinstance(ref, dict):
        raise SpawnRejected("a skill reference must be an object")
    root_name = ref.get(wire.KEY_ROOT)
    relative = ref.get(wire.KEY_PATH)
    if not isinstance(relative, str):
        raise SpawnRejected("a skill reference must carry a path")
    root = _root(root_name, config)
    resolved = (root / relative).resolve()
    if not resolved.is_relative_to(root):
        raise SpawnRejected(
            f"skill {relative!r} resolves outside the {root_name} directory"
        )
    if not resolved.exists():
        raise SpawnRejected(
            f"skill {relative!r} is not in this host's {root_name} directory — "
            f"the image or the mount is out of date"
        )
    return str(resolved)


def _root(name: object, config: HostConfig) -> Path:
    if name == wire.ROOT_PROFILE:
        return config.profile_dir
    if name == wire.ROOT_LIBRARY:
        if config.library_dir is None:
            raise SpawnRejected(
                "this host has no shared skill library mounted, so a skill from "
                "one cannot be loaded"
            )
        return config.library_dir
    raise SpawnRejected(f"unknown skill root {name!r}")
