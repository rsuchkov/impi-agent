"""Forgetting conversations by age: the record, the runtime's memory of it and
the files it received.

All of it in one motion — deleting only one part silently resets or orphans a
conversation. The one place this is done: the sessions command and an
application that purges at startup both call it. Like ``sessions_cli``, it
knows where the runtime keeps a conversation's memory.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from crucible.attachments import AttachmentStore
from crucible.runtimes.pi.spawn import forget_conversation_memory
from crucible.store.base import SessionRecord, SessionStore, idle_before
from crucible.tool_manifests import forget_session_manifest


async def purge_idle(
    session_dir: Path, store: SessionStore, *, days: int, agent: str | None = None,
    manifests_dir: Path | None = None, attachments: AttachmentStore | None = None,
) -> list[SessionRecord]:
    """Forget every conversation idle for ``days`` or more: the record, the
    runtime's memory, given ``manifests_dir`` the tool manifest a conversation
    entitled to less than its agent had of its own, and given ``attachments``
    the files it received. Returns what was forgotten."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    victims = [r for r in await store.list(agent) if idle_before(r, cutoff)]
    for record in victims:
        await store.delete(record.agent, record.conversation_id)
        forget_conversation_memory(session_dir, record.agent, record.runtime_session_id)
        if manifests_dir is not None:
            forget_session_manifest(manifests_dir, record.agent, record.runtime_session_id)
        if attachments is not None:
            await attachments.forget(record.agent, record.conversation_id)
    return victims

