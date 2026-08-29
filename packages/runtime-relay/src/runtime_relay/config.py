"""What this host is and where it keeps things — read once, from the environment.

Every path here is a mount. The engine never sends one: it sends the agent's
name, a session id and skill references, and this is the file that says what
those mean on this side. That is the whole point of the split — the two
containers do not share a filesystem, and nothing should quietly assume they do.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

# The agent this host serves. A host serves exactly one, and checks that the
# name in a spawn matches: a misrouted request must fail, not run as somebody
# else with somebody else's mounts.
ENV_AGENT = "RUNTIME_RELAY_AGENT"
# The shared secret the engine presents. The private network already keeps
# strangers out, but the agent's own shell is on that network too.
ENV_TOKEN = "RUNTIME_RELAY_TOKEN"
ENV_HOST = "RUNTIME_RELAY_HOST"
ENV_PORT = "RUNTIME_RELAY_PORT"
# The agent's own directory: system prompt, settings, its own skills. Mounted
# read-only — the runtime takes an advisory lock on settings and shrugs off
# failing to.
ENV_PROFILE_DIR = "RUNTIME_RELAY_PROFILE_DIR"
# The shared skill library, if this deployment has one mounted.
ENV_LIBRARY_DIR = "RUNTIME_RELAY_LIBRARY_DIR"
# Where the runtime's session files live. This agent's own volume: a session
# file is the memory of a conversation, and it is not other agents' to read.
ENV_SESSION_DIR = "RUNTIME_RELAY_SESSION_DIR"
ENV_RUNTIME_BIN = "RUNTIME_RELAY_BIN"
# Extensions to load on every spawn: an explicit list, then anything found by
# scanning a directory of them. The engine's tool bridge is the first entry and
# ships in this image.
ENV_EXTENSIONS = "RUNTIME_RELAY_EXTENSIONS"
ENV_EXTENSIONS_DIR = "RUNTIME_RELAY_EXTENSIONS_DIR"
# Scratch: the files an env variable points at arrive as content and are written
# here, one directory per session.
ENV_WORK_DIR = "RUNTIME_RELAY_WORK_DIR"

DEFAULT_PORT = 8427


class ConfigError(Exception):
    """The host was not given enough to start. Fatal, and loudly so: a host that
    guesses its own identity is worse than one that refuses to run."""


@dataclass(frozen=True)
class HostConfig:
    agent: str
    token: str
    profile_dir: Path
    session_dir: Path
    work_dir: Path
    runtime_bin: str
    extensions: tuple[Path, ...]
    library_dir: Path | None = None
    host: str = "0.0.0.0"
    port: int = DEFAULT_PORT


def from_env(environ: Mapping[str, str] | None = None) -> HostConfig:
    env = dict(os.environ if environ is None else environ)
    agent = env.get(ENV_AGENT, "").strip()
    if not agent:
        raise ConfigError(f"{ENV_AGENT} is not set: this host does not know whose it is")
    token = env.get(ENV_TOKEN, "").strip()
    if not token:
        raise ConfigError(
            f"{ENV_TOKEN} is not set: without it anything on this network could "
            f"ask for a process"
        )
    profile_dir = Path(env.get(ENV_PROFILE_DIR, "/agent"))
    if not profile_dir.is_dir():
        raise ConfigError(f"{ENV_PROFILE_DIR} ({profile_dir}) is not a directory")
    library = env.get(ENV_LIBRARY_DIR, "").strip()
    library_dir = Path(library) if library else None
    session_dir = Path(env.get(ENV_SESSION_DIR, "/sessions"))
    work_dir = Path(env.get(ENV_WORK_DIR, "/run/runtime-relay"))
    return HostConfig(
        agent=agent,
        token=token,
        profile_dir=profile_dir.resolve(),
        session_dir=session_dir,
        work_dir=work_dir,
        runtime_bin=env.get(ENV_RUNTIME_BIN, "pi"),
        extensions=_extensions(env),
        library_dir=library_dir.resolve() if library_dir else None,
        host=env.get(ENV_HOST, "0.0.0.0"),
        port=_port(env),
    )


def _port(env: Mapping[str, str]) -> int:
    raw = env.get(ENV_PORT, "").strip()
    if not raw:
        return DEFAULT_PORT
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{ENV_PORT} is not a number: {raw!r}") from exc


def _extensions(env: Mapping[str, str]) -> tuple[Path, ...]:
    paths = [
        Path(part) for part in env.get(ENV_EXTENSIONS, "").split(os.pathsep) if part
    ]
    scan = env.get(ENV_EXTENSIONS_DIR, "").strip()
    if scan:
        root = Path(scan)
        # Sorted, so the load order is the same on every start rather than
        # whatever order the filesystem happens to answer in.
        paths += sorted(
            index for index in root.glob("*/index.ts") if index.is_file()
        )
    return tuple(paths)
