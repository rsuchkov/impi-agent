"""What a runtime session's conversation is, from crucible's own session store:
where it happens, who spoke last, and its files directory.

Asked from two sides. The tool server fills a tool's context from it
(``ports.conversation``), and the confinement gives a process confined to its
conversation that one directory of files to write (``process_scopes``). Both
get it from here — one read of the session record, one rule for whose it is —
so neither the application nor a tool derives a conversation's facts itself:
the name ``AttachmentStore`` gives a conversation id is its own business (a
digest where it had to change one).
"""

from collections.abc import Awaitable, Callable
from pathlib import Path

from crucible.attachments import AttachmentStore
from crucible.ports.conversation import (
    ConversationResolver,
    EngineTurn,
    EngineTurnsSince,
    SessionConversation,
)
from crucible.store.base import SessionStore, gateway_addresses


def conversation_lookup(attachments: AttachmentStore | None, sessions: SessionStore) -> ConversationResolver:
    """(agent, runtime session id) -> its conversation: None for a session the
    store does not know, or one that belongs to another agent. ``files_dir``
    is None without attachments."""

    async def conversation(agent: str, runtime_session_id: str) -> SessionConversation | None:
        record = await sessions.get_by_runtime_session(runtime_session_id)
        if record is None or record.agent != agent:
            return None
        files_dir = attachments.conversation_dir(agent, record.conversation_id) if attachments else None
        # The place the turn is in: a conversation keyed across gateways began
        # somewhere (the record's own fields) but is now wherever the speaker
        # wrote from — a direct message there, a thread elsewhere.
        addresses = gateway_addresses(record)
        place = addresses.get(record.last_gateway)
        channel_id, kind = (place.channel_id, place.kind) if place else (record.channel_id, record.kind)
        return SessionConversation(
            channel_id, record.last_user_id, files_dir, kind,
            gateway=record.last_gateway, gateways=tuple(sorted(addresses)),
        )

    return conversation


def conversation_files_dir_lookup(
    attachments: AttachmentStore | None, sessions: SessionStore
) -> Callable[[str, str], Awaitable[Path | None]]:
    """(agent, runtime session id) -> that conversation's files directory, by
    the same lookup — what ``AgentProcessScopes(conversation_files_dir_for=…)``
    takes."""
    lookup = conversation_lookup(attachments, sessions)

    async def conversation_files_dir_for(agent: str, runtime_session_id: str) -> Path | None:
        found = await lookup(agent, runtime_session_id)
        return found.files_dir if found is not None else None

    return conversation_files_dir_for


def engine_turns_lookup(sessions: SessionStore) -> EngineTurnsSince:
    """(agent, since) -> the conversations the engine started a turn in since
    then — what ``HttpHub(engine_turns=…)`` answers ``startedSince`` from."""

    async def engine_turns(agent: str, since: str) -> list[EngineTurn]:
        return [
            EngineTurn(record.conversation_id, record.last_user_id, record.last_engine_turn_at)
            for record in await sessions.recent_engine_turns(agent, since)
        ]

    return engine_turns
