"""Forgetting a conversation's files by the conversation's age, not the file's.

A file is kept as long as its conversation is alive: ``ATTACHMENT_RETENTION_DAYS``
counts from the conversation's last activity, so a two-week-old file in a
conversation still going stays, and a conversation nobody has touched that
long loses its files — all of them, and the record of what arrived. A
directory the engine has no conversation for (a gateway's key before the flow
adopted the file, a record deleted by hand) goes when nothing has been written
to it for that long. ``session_retention.purge_idle`` forgets the files with
the conversation; this sweep is for the files of conversations the engine still
remembers.

A loop like the heartbeat and the telemetry pulse: the application supervises
``run_attachment_sweeps``; ``sweep_attachments`` is one pass.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from crucible.attachments import AttachmentStore
from crucible.store.base import SessionStore, idle_before

logger = logging.getLogger(__name__)

# How often the files of idle conversations are looked for.
SWEEP_INTERVAL_S = 3600.0


@dataclass(frozen=True)
class SweptAttachments:
    """What one pass took: conversations the engine knows and found idle,
    directories it had no conversation for, files in all."""

    idle_conversations: int = 0
    without_conversation: int = 0
    files: int = 0


async def sweep_attachments(attachments: AttachmentStore, sessions: SessionStore, *, days: int) -> SweptAttachments:
    """One pass: forget the files of every conversation idle for ``days`` or
    more, and every directory with no conversation that nothing has written to
    for as long. ``days <= 0`` keeps everything."""
    if days <= 0:
        return SweptAttachments()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    records = await sessions.list()
    known = {attachments.conversation_dir(r.agent, r.conversation_id): r for r in records}
    idle = unowned = files = 0
    for directory in await attachments.conversation_dirs():
        record = known.get(directory)
        if record is not None:
            if not idle_before(record, cutoff):
                continue
            files += await attachments.forget(record.agent, record.conversation_id)
            idle += 1
        elif await attachments.newest_mtime(directory) < cutoff.timestamp():
            files += await attachments.forget_dir(directory)
            unowned += 1
    swept = SweptAttachments(idle_conversations=idle, without_conversation=unowned, files=files)
    if swept.idle_conversations or swept.without_conversation:
        logger.info(
            "attachments: forgot %d file(s) of %d conversation(s) idle for %d+ days and %d directory(ies) of none",
            swept.files, swept.idle_conversations, days, swept.without_conversation,
        )
    return swept


async def run_attachment_sweeps(
    attachments: AttachmentStore, sessions: SessionStore, *, days: int,
    interval_s: float = SWEEP_INTERVAL_S, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Sweep now and every ``interval_s`` seconds, until cancelled; ``days <= 0``
    returns at once — there is nothing to look for."""
    if days <= 0:
        return
    while True:
        try:
            await sweep_attachments(attachments, sessions, days=days)
        except Exception:  # noqa: BLE001 - housekeeping must never stop the engine
            logger.exception("attachment sweep failed; trying again next period")
        await sleep(interval_s)
