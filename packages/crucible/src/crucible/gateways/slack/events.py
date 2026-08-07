"""Pure normalization of Slack events into neutral chat types.

Quirks worth knowing (covered by tests/test_slack_events.py):
- a message's thread key is ``thread_ts or ts`` — a reply carries the parent's
  ``thread_ts``; a root message keys its own thread by its ``ts``;
- ``message_id`` is always the individual message's ``ts`` (used for reactions);
- a DM arrives with ``channel_type == "im"``; a mention is ``<@own_id>`` in text;
- the bot's own posts echo back with our ``bot_id`` (and/or ``user``) — dropped.

Conversation-key rule — the thread always wins (mirrors the Mattermost adapter):
- reply (has thread_ts) -> that thread's session, reply in thread;
- top-level DM post      -> the DM-channel session, reply top-level;
- top-level channel post -> start a new thread keyed by the post ts.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from crucible.ports.chat.types import (
    KIND_DM,
    KIND_THREAD,
    ConversationRef,
    IncomingMessage,
)

logger = logging.getLogger(__name__)

# Message subtypes we treat as conversational. None = a plain user message;
# file_share = a message with attachments; bot_message = another bot/agent. Edits,
# deletes, joins, etc. carry other subtypes and are ignored.
_ACCEPTED_SUBTYPES = frozenset({None, "file_share", "bot_message"})


def event_to_incoming(
    event: dict[str, Any], own_user_id: str, own_bot_id: str = ""
) -> IncomingMessage | None:
    """Normalize one Slack ``message`` event; None = nothing we could react to."""
    channel = event.get("channel") or ""
    if not channel:
        return None
    if event.get("subtype") not in _ACCEPTED_SUBTYPES:
        return None

    user_id = event.get("user") or ""
    bot_id = event.get("bot_id") or ""
    # Drop our own echoes (a bot post comes back over the socket).
    if user_id and user_id == own_user_id:
        return None
    if bot_id and own_bot_id and bot_id == own_bot_id:
        return None

    is_from_bot = bool(bot_id) or event.get("subtype") == "bot_message"
    ts = event.get("ts") or ""
    thread_ts = event.get("thread_ts") or ""
    is_dm = event.get("channel_type") == "im"

    if thread_ts and thread_ts != ts:  # a reply inside a thread — the thread wins
        conversation_id, kind, thread_root = thread_ts, KIND_THREAD, thread_ts
    elif is_dm:  # top-level DM post -> the DM-channel session, reply top-level
        conversation_id, kind, thread_root = channel, KIND_DM, ""
    else:  # top-level channel post -> a reply starts a thread under it
        conversation_id, kind, thread_root = ts, KIND_THREAD, ts

    text = event.get("text") or ""
    mentioned = bool(own_user_id) and f"<@{own_user_id}>" in text

    return IncomingMessage(
        ref=ConversationRef(
            channel_id=channel,
            conversation_id=conversation_id,
            message_id=ts,
            thread_root_id=thread_root,
        ),
        text=text,
        user_id=user_id or bot_id,
        username=str(event.get("username") or ""),
        timestamp=ts_time(ts),
        kind=kind,
        is_dm=is_dm,
        mentioned=mentioned,
        is_from_bot=is_from_bot,
        raw=event,
    )


@dataclass(frozen=True)
class FileHandle:
    """A file Slack says is attached — what we need to fetch it. The download URL
    is private: it only answers with the bot token in an Authorization header."""

    file_id: str
    name: str
    url: str
    mime: str = ""
    size: int = 0


def parse_files(event: dict[str, Any]) -> tuple[FileHandle, ...]:
    """Files attached to a ``message`` event (subtype ``file_share``). A file
    without a private download URL is skipped — that is what the bot can fetch."""
    handles: list[FileHandle] = []
    for info in event.get("files") or []:
        if not isinstance(info, dict):
            continue
        url = str(info.get("url_private_download") or info.get("url_private") or "")
        if not url:
            continue
        size = info.get("size")
        handles.append(
            FileHandle(
                file_id=str(info.get("id") or ""),
                name=str(info.get("name") or info.get("title") or "file"),
                url=url,
                mime=str(info.get("mimetype") or ""),
                size=size if isinstance(size, int) else 0,
            )
        )
    return tuple(handles)


def should_respond(msg: IncomingMessage) -> bool:
    """Base dispatch rule: DMs — always; channels — only when mentioned. Bots are
    ignored here; the gateway layers agent-to-agent replies on top."""
    if msg.is_from_bot:
        return False
    return msg.is_dm or msg.mentioned


def ts_time(ts: str) -> datetime | None:
    """A Slack ``ts`` ("1698…") is epoch seconds -> a UTC-aware datetime."""
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc) if ts else None
    except ValueError:
        return None
