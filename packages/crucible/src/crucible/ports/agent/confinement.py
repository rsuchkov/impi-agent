"""How far a runtime process is confined, and who says which paths are its own.

A runtime process can run under rules that leave it only the paths it needs
(``crucible.confinement``). Three levels:

* ``conversation`` — each conversation's process sees only its own memory,
  its own files, its own scratch space; other conversations of the same agent,
  other agents and the engine are out of its reach. For a deployment where one
  agent talks to many people.
* ``agent`` — an agent's processes share the agent's paths, and nothing of
  any other agent or of the engine.
* ``off`` — no rules: the process reaches whatever its user can.

The runtime knows what it itself needs (its program, its memory); the
application knows where an agent's and a conversation's files are. This port
is the application's half.
"""

from typing import Literal, Protocol, get_args

from crucible.confinement import ProcessScope

ConfinementLevel = Literal["conversation", "agent", "off"]
CONFINEMENT_LEVELS: tuple[str, ...] = get_args(ConfinementLevel)


def check_confinement_level(value: str) -> ConfinementLevel:
    """``value`` as a level, or ``ValueError`` naming the ones there are."""
    if value not in CONFINEMENT_LEVELS:
        raise ValueError(f"{value!r} is not a confinement level: one of {', '.join(CONFINEMENT_LEVELS)}")
    return value  # type: ignore[return-value]


class ProcessScopes(Protocol):
    """The paths of one agent's process, for one conversation or for a
    memoryless run (``session_id`` None). None = this process runs unconfined
    (its agent's level is ``off``)."""

    async def scope_for(self, agent: str, session_id: str | None) -> ProcessScope | None: ...
