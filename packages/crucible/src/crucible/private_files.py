"""Files and directories only their owner can read.

What the engine keeps on disk is conversation memory, the inventory, the
manifest of an agent's tools, files people sent: none of it is for another
account on the same machine. The process's umask decides that by default, and a
default is what a development checkout, a bind mount or a shared volume
inherits without anyone choosing it. These two helpers choose it — 0700 for a
directory, 0600 for a file — and correct what an earlier version left wider.

A leaf: pure filesystem calls, imported by whatever writes.
"""

import contextlib
import os
from pathlib import Path

DIR_MODE = 0o700
FILE_MODE = 0o600


def private_dir(path: Path) -> Path:
    """Create ``path`` (and its parents) and make it the owner's alone. The
    parents keep what they had: only the directory asked for is narrowed."""
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        # A volume an agent can write may hold a link planted where the engine
        # is about to put files; narrowing or writing through it would act on
        # whatever it points at.
        raise OSError(f"{path} is a symbolic link; not writing through it")
    _narrow(path, DIR_MODE)
    return path


def private_file(path: Path) -> Path:
    """Make sure ``path`` exists as a file only its owner can read, without
    touching its content — for a file something else is about to open."""
    descriptor = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, FILE_MODE)
    os.close(descriptor)
    _narrow(path, FILE_MODE)
    return path


def write_private(path: Path, text: str) -> Path:
    """Write ``text`` to a file only its owner can read."""
    private_file(path)
    path.write_text(text, encoding="utf-8")
    return path


def _narrow(path: Path, mode: int) -> None:
    # Not ours to change (another owner on a shared mount): leave it, the
    # write that follows will say so if it matters.
    with contextlib.suppress(PermissionError):
        if (path.stat().st_mode & 0o777) != mode:
            path.chmod(mode)
