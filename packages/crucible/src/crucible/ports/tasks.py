"""Tool-facing port for scheduled work.

The sibling of ``ports/chat/interactions.py``: an agent asks for a task to exist,
and the concrete resolves which conversation the request came from, validates the
schedule and writes the row. Plain strings and dataclasses only — the tool layer
may not import the store, so nothing store-shaped may cross this line.
"""

from dataclasses import dataclass, field
from datetime import datetime
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
    # A watch's own: what it waits for (the application's key) and until when.
    watch_key: str = ""
    until: str = ""


class TaskService(Protocol):
    """Scheduling on behalf of the calling agent, in the conversation the call
    runs inside. An agent only ever sees and touches its own tasks."""

    async def schedule(
        self, agent: str, runtime_session_id: str, *, name: str, prompt: str,
        schedule: str, mode: str = "turn", timezone: str = "",
        notify: str = "failures", on_missed: str = "run",
    ) -> TaskView: ...

    async def watch(
        self, agent: str, runtime_session_id: str, *, name: str, watch_key: str,
        every_s: int, until: datetime, prompt: str = "",
    ) -> TaskView:
        """Wait for something outside the conversation — ``watch_key`` is the key
        the application's hook is asked about, every ``every_s`` seconds until
        ``until`` — and start a turn here when it has happened (or when the
        wait is over), with ``prompt`` as what the agent is then told to do."""
        ...

    async def list_tasks(self, agent: str) -> list[TaskView]: ...

    async def cancel(self, agent: str, task: str) -> TaskView:
        """Delete a task by id or name, together with its run history."""
        ...

    async def set_paused(self, agent: str, task: str, paused: bool) -> TaskView: ...
