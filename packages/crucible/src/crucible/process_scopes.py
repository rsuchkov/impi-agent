"""AgentProcessScopes: the paths an agent's runtime process may touch.

The default answer to ``ports.agent.confinement.ProcessScopes`` for an
application whose agents are profiles (``AgentSpec``) with a files directory
each. A process at level ``agent`` gets: its profile and the skill library to
read; its own files directory, its own scratch ``TMPDIR`` and ``HOME`` to
write; plus whatever its profile declares beyond that. What the runtime
itself needs (its program, its memory) the runtime adds; what is in neither
list the process cannot reach.

The ``conversation`` level narrows the writable paths to one conversation's
own; until the runtime's memory is kept per conversation it behaves as
``agent`` does.
"""

import tempfile
from collections.abc import Callable
from pathlib import Path

from crucible.confinement import ProcessScope
from crucible.ports.agent.confinement import ConfinementLevel, check_confinement_level
from crucible.ports.agent.spec import AgentSpec
from crucible.private_files import private_dir


class AgentProcessScopes:
    def __init__(
        self,
        spec_for: Callable[[str], AgentSpec | None],
        *,
        default_level: str = "conversation",
        files_dir_for: Callable[[str], Path | None] = lambda _agent: None,
        skills_root: Path | None = None,
        scratch_root: Path | None = None,
        unconfined_by_default: frozenset[str] = frozenset(),
    ) -> None:
        # Read on every spawn, so a reloaded profile's level and paths apply
        # to the next process.
        self._spec_for = spec_for
        self._default_level: ConfinementLevel = check_confinement_level(default_level)
        self._files_dir_for = files_dir_for
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
        if self.level_for(agent) == "off":
            return None
        spec = self._spec_for(agent)
        scratch = private_dir(self._scratch_root / agent)
        tmp, home = private_dir(scratch / "tmp"), private_dir(scratch / "home")
        readable: list[Path] = []
        writable: list[Path] = [tmp, home]
        if spec is not None:
            readable.append(spec.profile_dir)
            readable += spec.confinement_readable
            writable += spec.confinement_writable
        if self._skills_root is not None:
            readable.append(self._skills_root)
        files_dir = self._files_dir_for(agent)
        if files_dir is not None:
            writable.append(files_dir)
        return ProcessScope(
            readable=tuple(readable),
            writable=tuple(writable),
            env={"TMPDIR": str(tmp), "HOME": str(home)},
        )
