"""The turns an MCP client's calls open (``gateways.mcp``), as every replica
can read them: what a call is doing now, what it answered once it is done.

A call lands on any pod and the turn runs on the pod it landed on; the next
call about it — a poll for a task, a wait for the answer, a cancel — may land
elsewhere. So the turn's standing is written down as it goes: the pod that
runs it writes the record at the start, on every change of step and at the
end, and any pod reads it. A task, to the client, is this record by the turn's
id. Vocabulary over nothing: the store keeps the records, the gateway reads
and writes them.
"""

from dataclasses import dataclass
from typing import Protocol

# Where a turn stands, in the gateway's own words (a task's status is read off
# these; the scheduler's ``STATE_*`` are another lifecycle's).
# ``awaiting_approval``: a card is up for the person in their chat.
TURN_WORKING = "working"
TURN_AWAITING_APPROVAL = "awaiting_approval"
TURN_DONE = "done"
TURN_FAILED = "failed"
TURN_CANCELLED = "cancelled"
TURN_ENDED = frozenset({TURN_DONE, TURN_FAILED, TURN_CANCELLED})


@dataclass(frozen=True)
class McpTurnRecord:
    """One turn an MCP call opened. ``turn_id`` is what a client polls it by
    (a task's id); ``conversation`` the engine-side key of the conversation it
    runs in and ``handle`` the client's name for that conversation;
    ``subject`` and ``client`` whose call it was — the record is read by them
    alone; ``replica`` the process that runs it ("" in a single process);
    ``state`` where it stands and ``step`` the latest word on what it is on;
    ``answer`` what came out once it ended (the agent's words, or why it
    failed), ``is_error`` whether the call called that an error;
    ``request_digest`` a digest of what was asked, so the same request again
    within a short while joins this turn instead of opening another;
    ``cancel_requested`` is a client's word that reached another replica —
    written by ``request_mcp_cancel`` alone: a ``save`` of the record leaves
    the mark as it is, since the replica that runs the turn writes from its
    own copy and must not wipe what another replica wrote."""

    turn_id: str
    conversation: str
    handle: str
    subject: str
    client: str
    agent: str
    replica: str
    state: str
    started_at: str
    updated_at: str
    expires_at: str
    step: str = ""
    answer: str = ""
    is_error: bool = False
    request_digest: str = ""
    cancel_requested: bool = False


class McpTurns(Protocol):
    """The gateway's turns, for every replica to read."""

    async def save_mcp_turn(self, record: McpTurnRecord) -> None:
        """Insert, or replace every field but ``cancel_requested`` by ``turn_id``."""
        ...

    async def get_mcp_turn(self, turn_id: str) -> McpTurnRecord | None: ...

    async def latest_mcp_turn(self, conversation: str) -> McpTurnRecord | None:
        """The newest turn of the conversation, whatever its state."""
        ...

    async def find_mcp_turn(self, subject: str, client: str, request_digest: str) -> McpTurnRecord | None:
        """The newest turn still running that the same caller opened with the
        same request, or None."""
        ...

    async def request_mcp_cancel(self, turn_id: str) -> bool:
        """Mark the turn as asked to stop; False when there is no such turn."""
        ...

    async def fail_mcp_turns(self, replica: str, reason: str, now: str, expires_at: str) -> int:
        """Every turn ``replica`` was running is over — it restarted: failed
        with ``reason``, kept until ``expires_at``. How many."""
        ...

    async def purge_mcp_turns(self, now: str) -> int:
        """Forget records past their time; how many went."""
        ...
