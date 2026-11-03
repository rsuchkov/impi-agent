"""Tool-facing port for handing a task to another agent where the
conversation is.

An agent that wants another's work does not reach into that agent: the
engine posts the task, as the asking agent, in the turn's place, addressed
to the other agent, whose own gateway opens a turn on it; its answer comes
back into the same place, as a message from an agent. Asynchronous by
design: the call ends when the task is posted. The tool that calls this is
confirmed — what leaves the conversation is read by a person first.
"""

from collections.abc import Sequence
from typing import Protocol


class DelegationService(Protocol):
    """Handing a task to another agent where the conversation is: the engine
    posts, as the calling agent, a message in the turn's place that addresses
    the other agent and carries the task (and files, when given), so that
    agent's gateway opens a turn of its own on it and its answer comes back
    into the same place for everyone there to read."""

    async def delegate(
        self, agent: str, runtime_session_id: str, to: str, task: str, paths: Sequence[str] = (),
    ) -> None:
        """Post the task for ``to`` from ``agent``'s turn. ``DelegationError``
        when it cannot be posted — no such agent, a conversation nobody else
        reads, a file that may not be sent."""
        ...


class DelegationError(Exception):
    """Why a task could not be handed on, in words for the model."""
