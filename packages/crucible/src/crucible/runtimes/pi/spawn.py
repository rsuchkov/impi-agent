"""SpawnRequest: what one runtime process should BE, separately from how it is
started.

The driver used to assemble a command line inline, which tied every spawn to the
filesystem of the process doing the spawning: absolute session directories,
extension paths inside this installed package, a working directory. Naming the
pieces instead lets a host that runs somewhere else — another container, its own
filesystem, its own copy of the runtime — resolve them its own way.

Paths here are still THIS side's paths, because the local host needs them
verbatim. A host that is not this side translates them at its own boundary,
where the translation can be checked and can fail loudly, rather than being
assumed to line up.
"""

import contextlib
import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from crucible.confinement import ProcessScope
from crucible.private_files import private_dir

_DIGEST_CHARS = 12


def safe_session_id(raw: str) -> str:
    """Coerce a conversation key into a valid runtime session id
    (``[A-Za-z0-9._-]``, starting and ending alphanumeric).

    The same rule as the store's ``derive_runtime_session_id`` — a digest of the
    original is appended whenever coercion changed it, so two keys that clean to
    the same string still get different sessions. The two copies exist because
    the store may not import the runtime; ``tests/test_session_store.py`` keeps
    them in agreement."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "-", raw).strip("-._")
    if cleaned == raw:
        return cleaned
    digest = hashlib.sha256(raw.encode()).hexdigest()[:_DIGEST_CHARS]
    return f"{cleaned or 'session'}-{digest}"


def conversation_memory_dir(session_dir: Path, agent: str, session_id: str) -> Path:
    """Where the runtime keeps one conversation's memory: a directory of its
    own, ``<session_dir>/<agent>/<session_id>/``, so a process allowed to
    write there reaches no other conversation's. The functions below are the
    one place that knows this layout — the host that starts the process, the
    cleanup CLI and the runtime's own memory checks all ask here."""
    return session_dir / agent / session_id


def session_files(session_dir: Path, agent: str, session_id: str) -> list[Path]:
    """The files of one conversation's memory, ``<timestamp>_<session_id>.*``
    in its directory."""
    return sorted(conversation_memory_dir(session_dir, agent, session_id).glob(f"*_{session_id}.*"))


def forget_conversation_memory(session_dir: Path, agent: str, session_id: str) -> int:
    """Delete one conversation's memory, its directory with it. Returns how
    many files went."""
    files = session_files(session_dir, agent, session_id)
    for path in files:
        path.unlink(missing_ok=True)
    with contextlib.suppress(OSError):  # gone already, or holds something not ours
        conversation_memory_dir(session_dir, agent, session_id).rmdir()
    return len(files)


def migrate_session_layout(session_dir: Path) -> int:
    """Move memory kept the earlier way — every conversation's files side by
    side in ``<session_dir>/<agent>/`` — into each conversation's own
    directory. Idempotent: a second run finds nothing to move. Returns how
    many conversations moved."""
    if not session_dir.is_dir():
        return 0
    moved: set[tuple[str, str]] = set()
    for agent_dir in session_dir.iterdir():
        if not agent_dir.is_dir() or agent_dir.is_symlink():
            continue
        for path in agent_dir.iterdir():
            stamp, sep, rest = path.name.partition("_")
            session_id = rest.rsplit(".", 1)[0]
            if not path.is_file() or not (sep and stamp and session_id):
                continue
            target = private_dir(conversation_memory_dir(session_dir, agent_dir.name, session_id))
            path.rename(target / path.name)
            moved.add((agent_dir.name, session_id))
    return len(moved)


@dataclass(frozen=True)
class SpawnRequest:
    """One process to start, described in terms a host can honour its own way."""

    # Which agent this is. A remote host serves exactly one and checks the name,
    # so a misrouted request fails instead of running as the wrong agent.
    agent: str
    # The agent's own directory (system prompt, settings, its own skills). The
    # working directory unless ``cwd`` overrides it.
    profile_dir: Path
    # None = a memoryless run (``--no-session``). Already sanitized.
    session_id: str | None = None
    # The single capability allowlist: built-ins, extension tools and typed tools
    # alike. Empty means no tools at all.
    tools: tuple[str, ...] = ()
    # Absolute skill paths, resolved by the profile loader.
    skills: tuple[str, ...] = ()
    provider: str | None = None
    model: str | None = None
    append_system_prompt: str = ""
    # Env GRANTED to the process, on top of whatever the host's own environment
    # is. Explicit rather than "everything this process happens to hold": with a
    # host in another container, the difference between the two is the engine's
    # entire secret set.
    env: Mapping[str, str] = field(default_factory=dict)
    # Names (globs allowed) of THIS process's environment variables the child
    # may inherit beside the base set a local host always passes; meaningful
    # only to a host that shares this environment. ``inherit_all`` hands the
    # whole environment over, for an agent that is part of the engine.
    inherit: tuple[str, ...] = ()
    inherit_all: bool = False
    # Env entries whose value is a FILE this side wrote and the process must be
    # able to read (the tool manifest, today). A local host passes the path
    # through; a remote one ships the content and points the variable at its own
    # copy, because the path means nothing over there.
    env_files: Mapping[str, Path] = field(default_factory=dict)
    # Where session files live — the BASE; the host owns the per-agent layout
    # underneath it.
    session_dir: Path | None = None
    # Extensions to load, as absolute paths on this side. A host with its own
    # filesystem supplies its own instead.
    extensions: tuple[str, ...] = ()
    # What the application says this process may touch beyond what the host
    # adds for the runtime itself; None = unconfined.
    scope: ProcessScope | None = None
    # Working-directory override for checkout-scoped runs; None = the profile dir.
    cwd: Path | None = None
