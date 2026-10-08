"""Which files directory belongs to the conversation a runtime session serves.

Asked from two sides: the confinement, for the one directory of files a
process confined to its conversation may write (``process_scopes``), and the
tool server, for the directory a tool works in (``ToolContext.files_dir``).
Both get it from here, so neither the application nor a tool derives a
conversation's directory itself — the name ``AttachmentStore`` gives a
conversation id is its own business (a digest where it had to change one).
"""

from collections.abc import Awaitable, Callable
from pathlib import Path

from crucible.attachments import AttachmentStore
from crucible.store.base import SessionStore


def conversation_files_dir_lookup(
    attachments: AttachmentStore | None, sessions: SessionStore
) -> Callable[[str, str], Awaitable[Path | None]]:
    """(agent, runtime session id) -> that conversation's files directory,
    over the store that knows which conversation a session serves. None for a
    session the store does not know, one that belongs to another agent, or a
    deployment without attachments."""

    async def conversation_files_dir_for(agent: str, runtime_session_id: str) -> Path | None:
        if attachments is None:
            return None
        record = await sessions.get_by_runtime_session(runtime_session_id)
        if record is None or record.agent != agent:
            return None
        return attachments.conversation_dir(agent, record.conversation_id)

    return conversation_files_dir_for
