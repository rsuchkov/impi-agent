"""Tool-facing port for sending a file into the conversation a turn runs in.

The sibling of ``InteractionService``: a tool names a path, the service resolves
``runtime_session_id`` to a conversation and posts as that agent. A tool passes a
PATH rather than bytes — the runtime's tool bridge carries text, and the file is
already on a disk both sides share.

Which paths an agent may send from is the service's business, not the tool's, so
the policy lives in one place and a tool can't widen it.
"""

from typing import Protocol


class FileError(Exception):
    """Sending failed for a reason the agent should hear: no such file, a path
    outside its workspace, too large, or no conversation to post into."""


class FileService(Protocol):
    async def send(
        self, agent: str, runtime_session_id: str, paths: list[str], *, text: str = ""
    ) -> list[str]:
        """Send these files to whoever the turn is talking to; returns the names
        sent. Raises FileError with a reason the agent can act on."""
        ...
