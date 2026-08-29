"""LocalHost: the runtime as a child of this process.

The arrangement there always was, and still the default. This is the only host
that builds a command line, because the flags name paths on THIS filesystem — a
host elsewhere is handed the request and builds its own.
"""

import logging
import os
from pathlib import Path

from crucible.runtimes.pi.spawn import SpawnRequest
from crucible.runtimes.pi.transport import PiTransport, SubprocessTransport

logger = logging.getLogger(__name__)


class LocalHost:
    """Runs the process as a child of this one, in this filesystem."""

    def __init__(self, *, program: str = "pi") -> None:
        self._program = program

    async def open(self, request: SpawnRequest) -> PiTransport:
        session_dir = self._session_dir(request)
        args = command_args(request, session_dir=session_dir)
        cwd = str(request.cwd or request.profile_dir)
        return await SubprocessTransport.spawn(
            self._program, args, cwd=cwd, env=_environment(request)
        )

    async def aclose(self) -> None:
        return

    @staticmethod
    def _session_dir(request: SpawnRequest) -> Path | None:
        if request.session_dir is None:
            return None
        # Per-agent subdir (sanitized ids can collide across agents), and an
        # ABSOLUTE path — a relative --session-dir resolves from the process's
        # own cwd (the profile dir), scattering session files into the agents dir.
        resolved = (request.session_dir / request.agent).resolve()
        resolved.mkdir(parents=True, exist_ok=True)
        return resolved


def command_args(request: SpawnRequest, *, session_dir: Path | None) -> list[str]:
    """The command line for one spawn. Pure: the caller owns any directory that
    has to exist first, so the mapping from request to flags stays assertable."""
    # --approve: RPC mode shows no trust prompt and otherwise ignores the
    # project's own resources (system prompt, permission policy). Approving for
    # the run loads them from the agent's profile dir.
    args = ["--mode", "rpc", "--approve"]
    if request.session_id:
        args += ["--session-id", request.session_id]
    else:
        args += ["--no-session"]
    if session_dir is not None:
        args += ["--session-dir", str(session_dir)]
    for ext in request.extensions:
        args += ["-e", ext]
    # Single capability gate: --tools is the allowlist over built-in, extension
    # and typed tools alike. An empty list yields no tools at all; a built-in an
    # agent wants (e.g. read/bash for skills) is just named in its profile.
    args += ["--tools", ",".join(request.tools)]
    # No ambient skill discovery — each agent gets EXACTLY its declared skills
    # (this also closes the ancestor-dir walk-up the agents directory would leak).
    args += ["--no-skills"]
    for skill in request.skills:
        args += ["--skill", skill]
    if request.provider:
        args += ["--provider", request.provider]
    if request.model:
        args += ["--model", request.model]
    # Extra system-prompt text (e.g. the gateway's response-formatting rules).
    if request.append_system_prompt:
        args += ["--append-system-prompt", request.append_system_prompt]
    return args


def _environment(request: SpawnRequest) -> dict[str, str] | None:
    """This process's environment plus what the request grants. None when the
    request grants nothing, so the child simply inherits."""
    if not request.env and not request.env_files:
        return None
    granted = {name: str(path) for name, path in request.env_files.items()}
    return {**os.environ, **request.env, **granted}
