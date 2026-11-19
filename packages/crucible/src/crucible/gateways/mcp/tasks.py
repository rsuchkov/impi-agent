"""A call that runs as a task: the client asks and goes away, then polls.

A turn may take minutes and may stop to ask the person for a yes; a client
that cannot hold a request open that long asks for the call as a task
(``params.task``), gets a task id at once and reads the task's status
(``tasks/get``) until it ends, then the result (``tasks/result``). The status
is read off the turn as it runs: ``working`` with the turn's latest word,
``input_required`` while a card is up for the person, then ``completed`` with
the result, ``failed`` when the tool itself failed, ``cancelled`` when the
client cancelled it. A tool that would wait on a person declares it must run
as a task (``requires_task``); called plainly, it says so.

The tasks live in the process: a restart forgets them, and a client asking
after one is told the task is unknown — it asks again.
"""

import asyncio
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from crucible.gateways.http.journal import STATUS_AWAITING_INPUT
from crucible.gateways.http.turns import HttpTurns
from crucible.gateways.mcp.tools import GatewayCall

STATUS_WORKING = "working"
STATUS_INPUT_REQUIRED = "input_required"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
TERMINAL = frozenset({STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED})
# What a tool declares about running as a task (``execution.taskSupport``).
TASK_SUPPORT_REQUIRED = "required"
TASK_SUPPORT_OPTIONAL = "optional"
# How long an ended task is kept for the client to read: the client's wish,
# within a ceiling; and how often it is told to look.
DEFAULT_TTL_MS = 10 * 60 * 1000
MAX_TTL_MS = 60 * 60 * 1000
POLL_INTERVAL_MS = 2000
# How long ``tasks/result`` waits for a task still working before saying so.
RESULT_WAIT_S = 25.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class McpTask:
    """One call running as a task: whose, what it is doing, how it ended."""

    def __init__(self, task_id: str, owner: str, call: GatewayCall, runner: "asyncio.Future[dict[str, Any]]", ttl_ms: int) -> None:
        self.id = task_id
        self.owner = owner
        self.call = call
        self.runner = runner
        self.ttl_ms = ttl_ms
        self.created_at = _now_iso()
        self.updated_at = self.created_at
        self.finished_at: float | None = None  # loop time, for retention
        self.message = ""  # the tool's latest word on what it is doing
        self.cancelled = False
        runner.add_done_callback(self._ended)

    def _ended(self, done: "asyncio.Future[dict[str, Any]]") -> None:
        self.finished_at = asyncio.get_running_loop().time()
        self.updated_at = _now_iso()

    def progress(self, text: str) -> None:
        self.message = text
        self.updated_at = _now_iso()

    def status(self, turns: HttpTurns, conversation: str) -> tuple[str, str]:
        """(status, statusMessage) as the client reads them; ``conversation``
        is the key of the turn the call runs in ("" when it asked no agent)."""
        if self.cancelled or self.runner.cancelled():
            return STATUS_CANCELLED, "cancelled by the client"  # told to end, whether it has yet
        if self.runner.done():
            if self.runner.exception() is not None:
                return STATUS_FAILED, "the call failed"
            return STATUS_COMPLETED, ""
        turn = turns.active(conversation) if conversation else None
        if turn is not None and turn.journal.status == STATUS_AWAITING_INPUT:
            waiting = turn.calls.awaiting
            what = f" to {waiting.tool}" if waiting is not None else ""
            return STATUS_INPUT_REQUIRED, f"waiting for the person's yes{what}"
        word = self.message or (turn.calls.latest if turn is not None else "")
        return STATUS_WORKING, word

    def wire(self, status: str, message: str) -> dict[str, Any]:
        task: dict[str, Any] = {
            "taskId": self.id, "status": status, "createdAt": self.created_at, "lastUpdatedAt": self.updated_at,
            "ttl": self.ttl_ms, "pollInterval": POLL_INTERVAL_MS,
        }
        if message:
            task["statusMessage"] = message
        return task


class McpTasks:
    """The tasks of one process, by id; each read by its owner alone."""

    def __init__(self) -> None:
        self._by_id: dict[str, McpTask] = {}

    def create(self, task_id: str, owner: str, call: GatewayCall, runner: "asyncio.Future[dict[str, Any]]", asked: Mapping[str, Any]) -> McpTask:
        ttl = asked.get("ttl")
        ttl_ms = min(int(ttl), MAX_TTL_MS) if isinstance(ttl, int | float) and ttl > 0 else DEFAULT_TTL_MS
        task = McpTask(task_id, owner, call, runner, ttl_ms)
        self._by_id[task_id] = task
        return task

    def get(self, owner: str, task_id: str) -> McpTask | None:
        task = self._by_id.get(task_id)
        return task if task is not None and task.owner == owner else None

    def owned_by(self, owner: str) -> list[McpTask]:
        return [task for task in self._by_id.values() if task.owner == owner]

    def sweep(self, now: float) -> int:
        """Forget ended tasks past their retention."""
        stale = [
            task_id for task_id, task in self._by_id.items()
            if task.finished_at is not None and now - task.finished_at > task.ttl_ms / 1000
        ]
        for task_id in stale:
            del self._by_id[task_id]
        return len(stale)
