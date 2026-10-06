"""AgentProcessScopes: the paths an agent's runtime process may touch.

The default answer to ``ports.agent.confinement.ProcessScopes`` for an
application whose agents are profiles (``AgentSpec``) with a files directory
each. Every confined process reads its profile and the skill library, and
writes whatever its profile declares beyond that. What else it writes depends
on the level:

* ``agent`` — the agent's files directory and the agent's scratch ``TMPDIR``
  and ``HOME``, shared by all its conversations;
* ``conversation`` — that conversation's own files directory and its own
  ``TMPDIR`` and ``HOME``. A memoryless run (a scheduled task) gets a scratch
  directory of its own and no files directory: it has no conversation.
  Scratch space is scratch: a day after its last process it is cleared.

What the runtime itself needs (its program, its memory) the runtime adds;
what is in neither list the process cannot reach.
"""

import os
import shutil
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from crucible.confinement import ProcessScope
from crucible.ports.agent.confinement import ConfinementLevel, check_confinement_level
from crucible.ports.agent.spec import AgentSpec
from crucible.private_files import private_dir

# This side does not see a process end, so a conversation's or a memoryless
# run's scratch directory outlives it. One left untouched for longer than any
# process lives is cleared when the next process of that agent starts.
_RUN_PREFIX = "run-"
_SCRATCH_KEPT_S = 24 * 3600
# The agent level's own scratch, beside the per-conversation directories.
_AGENT_SCRATCH = frozenset({"tmp", "home"})


async def _no_conversation_files(_agent: str, _session_id: str) -> Path | None:
    return None


class AgentProcessScopes:
    def __init__(
        self,
        spec_for: Callable[[str], AgentSpec | None],
        *,
        default_level: str = "conversation",
        files_dir_for: Callable[[str], Path | None] = lambda _agent: None,
        conversation_files_dir_for: Callable[[str, str], Awaitable[Path | None]] = _no_conversation_files,
        files_dir_env: str = "",
        skills_root: Path | None = None,
        scratch_root: Path | None = None,
        unconfined_by_default: frozenset[str] = frozenset(),
    ) -> None:
        # Read on every spawn, so a reloaded profile's level and paths apply
        # to the next process.
        self._spec_for = spec_for
        self._default_level: ConfinementLevel = check_confinement_level(default_level)
        # The agent's files directory, and one conversation's (by the runtime
        # session id the process is started under) — the application knows
        # where both are. A variable named ``files_dir_env`` tells the process
        # which one it was given.
        self._files_dir_for = files_dir_for
        self._conversation_files_dir_for = conversation_files_dir_for
        self._files_dir_env = files_dir_env
        self._skills_root = skills_root
        self._scratch_root = scratch_root or Path(tempfile.gettempdir()) / "agent-scratch"
        # Agents that are part of the application rather than its users' (an
        # operator's assistant that reads the application's own configuration):
        # off unless their profile says otherwise.
        self._unconfined_by_default = unconfined_by_default

    def level_for(self, agent: str) -> ConfinementLevel:
        spec = self._spec_for(agent)
        if spec is not None and spec.confinement is not None:
            return check_confinement_level(spec.confinement)
        if agent in self._unconfined_by_default:
            return "off"
        return self._default_level

    async def scope_for(self, agent: str, session_id: str | None) -> ProcessScope | None:
        level = self.level_for(agent)
        if level == "off":
            return None
        spec = self._spec_for(agent)
        scratch = self._scratch_for(agent, session_id, level)
        tmp, home = private_dir(scratch / "tmp"), private_dir(scratch / "home")
        readable: list[Path] = []
        writable: list[Path] = [tmp, home]
        if spec is not None:
            readable.append(spec.profile_dir)
            readable += spec.confinement_readable
            writable += spec.confinement_writable
        if self._skills_root is not None:
            readable.append(self._skills_root)
        env = {"TMPDIR": str(tmp), "HOME": str(home)}
        files_dir = await self._files_dir(agent, session_id, level)
        if files_dir is not None:
            writable.append(files_dir)
        if self._files_dir_env:
            # Never the agent's directory for a process that may not write it:
            # a skill told to put a file there would fail on every call.
            env[self._files_dir_env] = str(files_dir or tmp)
        return ProcessScope(readable=tuple(readable), writable=tuple(writable), env=env)

    async def _files_dir(self, agent: str, session_id: str | None, level: ConfinementLevel) -> Path | None:
        if level == "agent":
            return self._files_dir_for(agent)
        if session_id is None:
            return None
        found = await self._conversation_files_dir_for(agent, session_id)
        return private_dir(found) if found is not None else None

    def _scratch_for(self, agent: str, session_id: str | None, level: ConfinementLevel) -> Path:
        agent_scratch = private_dir(self._scratch_root / agent)
        if level == "agent":
            return agent_scratch
        if session_id:
            scratch = private_dir(agent_scratch / session_id)
            os.utime(scratch)  # in use: not stale — before the sweep looks
        else:
            scratch = Path(tempfile.mkdtemp(prefix=_RUN_PREFIX, dir=agent_scratch))
        _clear_stale_scratch(agent_scratch)
        return scratch


def _clear_stale_scratch(agent_scratch: Path) -> None:
    cutoff = time.time() - _SCRATCH_KEPT_S
    for path in agent_scratch.iterdir():
        try:
            if (
                path.name not in _AGENT_SCRATCH and path.is_dir() and not path.is_symlink()
                and path.stat().st_mtime < cutoff
            ):
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            continue
