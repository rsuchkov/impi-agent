"""Confining a child process with Landlock.

A process this library starts for an agent is handed only the paths it
needs: what it may read, what it may write, which existing files it may
rewrite, where it may only make directories — and, when the scope names
ports, the TCP ports it may connect to. Everything else is out of reach,
including other processes' environments in ``/proc`` and, with ports named,
every other TCP port. The rules are applied by ``launcher.py``, which the
child runs before it becomes the real command — so they hold for the whole
process and every process it starts.

Plain OS machinery: nothing here knows an agent, a runtime or an application.
"""

import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from crucible.confinement.launcher import (
    CONNECT_TCP,
    MKDIR_ONLY,
    NET_ABI,
    READABLE,
    WRITABLE,
    WRITABLE_FILE,
    landlock_abi,
)

LAUNCHER_PATH = Path(__file__).with_name("launcher.py")

__all__ = ["LAUNCHER_PATH", "NET_ABI", "ProcessScope", "confined_command", "landlock_abi", "url_port"]


@dataclass(frozen=True)
class ProcessScope:
    """What one process may touch. Paths only; anything not named is denied."""

    # Read and execute beneath these.
    readable: tuple[Path, ...] = ()
    # Read, write, create and remove beneath these.
    writable: tuple[Path, ...] = ()
    # Existing files that may be read and rewritten in place, not created.
    writable_files: tuple[Path, ...] = ()
    # Directories where subdirectories may be made and removed, and nothing
    # else written — room for a lock directory, none for a planted file.
    mkdir_only: tuple[Path, ...] = ()
    # TCP ports the process may connect to, at any address; binding one is
    # refused. Empty = the network is not restricted at all.
    tcp_ports: tuple[int, ...] = ()
    # Variables the process should see, for the paths above to be the ones it
    # uses (its own TMPDIR, its own HOME).
    env: dict[str, str] = field(default_factory=dict)


def confined_command(
    scope: ProcessScope, command: list[str], *, python: str = sys.executable
) -> list[str]:
    """``command`` prefixed by the launcher that confines it to ``scope``.
    ``-I -S``: no user site, no PYTHON* variables, no site import — the
    launcher needs only the standard library, and starts faster without it."""
    rules: list[str] = []
    for option, paths in (
        (READABLE, scope.readable),
        (WRITABLE, scope.writable),
        (WRITABLE_FILE, scope.writable_files),
        (MKDIR_ONLY, scope.mkdir_only),
    ):
        for path in paths:
            rules += [option, str(path)]
    for port in scope.tcp_ports:
        rules += [CONNECT_TCP, str(port)]
    return [python, "-I", "-S", str(LAUNCHER_PATH), *rules, "--", *command]


def url_port(url: str) -> int | None:
    """The TCP port a URL is reached on — the one it names, else its scheme's
    (443 for https, 80 for http) — or None for anything else: whoever adds a
    URL's port to a scope has nothing to add for a URL that has none."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if port is not None:
        return port
    return {"https": 443, "http": 80}.get(parts.scheme)
