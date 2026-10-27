"""Getting the runtime's storage ready before its first process starts.

What every application that runs the runtime needs at startup, in one call:

- its agent directory holds the credential and the models store before any
  process reads them. The runtime creates both when they are missing — and a
  confined process may only read that directory and make the lock directory
  beside each file, so a missing one fails every turn (EACCES), as does a
  directory on a read-only root (EROFS);
- conversation memory kept before each conversation had a directory of its own
  moves into one;
- conversations idle past ``SESSION_RETENTION_DAYS`` are forgotten, record and
  memory together (``session_retention.py``).

Like ``session_retention``, it knows where the runtime keeps things. The
process's umask is not set here: the application sets it before anything — a
store included — creates a file.
"""

import logging
import os
import shutil
from pathlib import Path

from crucible.attachments import AttachmentStore
from crucible.config import Settings
from crucible.runtimes.pi.auth_file import (
    AUTH_FILE,
    MODELS_FILE,
    MODELS_STORE_FILE,
    runtime_agent_dir,
)
from crucible.runtimes.pi.spawn import migrate_session_layout
from crucible.session_retention import purge_idle
from crucible.store.base import SessionStore
from crucible.tool_manifests import manifests_dir

logger = logging.getLogger(__name__)

# What the runtime keeps in its agent directory and reads on every turn. Empty
# is what it reads as "nothing yet" — and what a check for a login (a file
# with something in it) still reads as none.
RUNTIME_AGENT_DIR_FILES = (AUTH_FILE, MODELS_STORE_FILE)


async def prepare_runtime_storage(
    settings: Settings, sessions: SessionStore, *, models_from: Path | None = None,
    attachments: AttachmentStore | None = None,
) -> None:
    """Before anything runs, so nothing live is using what is touched. Every
    step is housekeeping: a failure is said in the log and the application
    carries on serving. ``models_from``: the application's own model
    definitions (a ``models.json`` shipped in its image), copied into the
    runtime's agent directory when that has none — the runtime reads the file
    from its agent directory alone, and on a read-only image that directory is
    a writable volume the image's copy never reached. ``attachments``: the
    files a forgotten conversation received go with it."""
    prepare_runtime_agent_dir(runtime_agent_dir())
    if models_from is not None:
        provide_model_definitions(runtime_agent_dir(), models_from)
    session_dir = settings.resolved_pi_session_dir
    moved = migrate_session_layout(session_dir)
    if moved:
        logger.info("moved %d conversation memories into a directory each", moved)
    if settings.session_retention_days <= 0:
        return
    try:
        forgotten = await purge_idle(
            session_dir, sessions, days=settings.session_retention_days,
            manifests_dir=manifests_dir(settings.data_dir), attachments=attachments,
        )
    except Exception:
        # A store that cannot answer must not keep the application from serving.
        logger.exception("could not forget idle conversations; carrying on")
        return
    if forgotten:
        logger.info("forgot %d conversation(s) idle for %d+ days", len(forgotten),
                    settings.session_retention_days)


def prepare_runtime_agent_dir(agent_dir: Path) -> bool:
    """Create the runtime's stores in ``agent_dir`` (empty, 0600) where they
    are missing; one that exists is left as it is. False, and an ERROR naming
    the directory, when the runtime could not take its locks there."""
    try:
        agent_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        for name in RUNTIME_AGENT_DIR_FILES:
            _create_empty(agent_dir / name)
    except OSError as exc:
        problem = exc.strerror or str(exc)
    else:
        if os.access(agent_dir, os.W_OK):
            return True
        problem = "not writable"
    logger.error(
        "the runtime's agent directory %s: %s. The runtime locks its credential there "
        "before every model call, so every turn will fail; point PI_CODING_AGENT_DIR "
        "at a writable directory",
        agent_dir, problem,
    )
    return False


def provide_model_definitions(agent_dir: Path, source: Path) -> bool:
    """Copy ``source`` to the runtime's ``models.json`` when the agent directory
    has none. One that exists is left alone — an operator may have edited it.
    True when the copy was made."""
    target = agent_dir / MODELS_FILE
    if target.exists():
        return False
    if not source.is_file():
        logger.warning("no model definitions at %s to copy into %s", source, agent_dir)
        return False
    try:
        shutil.copyfile(source, target)
        target.chmod(0o600)
    except OSError as exc:
        logger.error("could not copy %s into %s: %s", source, agent_dir, exc.strerror or exc)
        return False
    logger.info("copied the model definitions from %s into %s", source, agent_dir)
    return True


def _create_empty(path: Path) -> None:
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        return
    os.close(fd)
