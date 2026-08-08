"""Tool-facing port for scheduled work.

The sibling of ``ports/chat/interactions.py``: an agent asks for a task to exist,
and the concrete resolves which conversation the request came from, validates the
schedule and writes the row. Plain strings and dataclasses only — the tool layer
may not import the store, so nothing store-shaped may cross this line.
"""

from dataclasses import dataclass, field
from typing import Protocol


class TaskError(Exception):
    """A scheduling request that cannot be honoured, phrased for the agent: a
    schedule that doesn't parse, a name already in use, a limit reached."""


@dataclass(frozen=True)
class TaskView:
    """What an agent may know about one of its tasks. Times are rendered in the
    task's own zone, because that is the zone the person who asked was thinking
    in — the raw UTC instant is the scheduler's business, not the agent's."""

    id: str
    name: str
    prompt: str
    schedule: str  # as it was written ("every weekday at 9", "0 9 * * 1-5")
    mode: str
    state: str
    timezone: str
    next_run: str  # human-readable, in `timezone`; "" when there is none
    last_run: str
    last_status: str
    notify: str
    # Only on a fresh task: the next few fire times, so a wrong expression is
    # obvious now rather than at 03:00 on Sunday.
    upcoming: tuple[str, ...] = field(default_factory=tuple)


class TaskService(Protocol):
    """Scheduling on behalf of the calling agent, in the conversation the call
    runs inside. An agent only ever sees and touches its own tasks."""

    async def schedule(
        self, agent: str, runtime_session_id: str, *, name: str, prompt: str,
        schedule: str, mode: str = "turn", timezone: str = "",
        notify: str = "failures", on_missed: str = "run",
    ) -> TaskView: ...

    async def list_tasks(self, agent: str) -> list[TaskView]: ...

    async def cancel(self, agent: str, task: str) -> TaskView:
        """Delete a task by id or name. Its run history is kept."""
        ...

    async def set_paused(self, agent: str, task: str, paused: bool) -> TaskView: ...
