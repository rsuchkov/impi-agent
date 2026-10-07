"""Forgetting conversations by age: the record and the runtime's memory of it.

Both halves in one motion — deleting only one silently resets or orphans a
conversation. The one place this is done: the sessions command and an
application that purges at startup both call it. Like ``sessions_cli``, it
knows where the runtime keeps a conversation's memory.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from crucible.runtimes.pi.spawn import forget_conversation_memory
from crucible.store.base import SessionRecord, SessionStore


async def purge_idle(
    session_dir: Path, store: SessionStore, *, days: int, agent: str | None = None
) -> list[SessionRecord]:
    """Forget every conversation idle for ``days`` or more. Returns what was
    forgotten."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    victims = [r for r in await store.list(agent) if idle_before(r, cutoff)]
    for record in victims:
        await store.delete(record.agent, record.conversation_id)
        forget_conversation_memory(session_dir, record.agent, record.runtime_session_id)
    return victims


def idle_before(record: SessionRecord, cutoff: datetime) -> bool:
    """Whether ``record`` was last active before ``cutoff``. A value that does
    not parse is kept, not forgotten — and never stops the sweep; a naive one
    is read as UTC, which is what the engine writes."""
    value: object = record.last_active  # a string; a Mongo row may carry a datetime
    try:
        moment: datetime = (
            value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        )
    except ValueError:
        return False
    if moment.tzinfo is None:
        moment = datetime.combine(moment.date(), moment.time(), tzinfo=timezone.utc)
    return moment < cutoff
