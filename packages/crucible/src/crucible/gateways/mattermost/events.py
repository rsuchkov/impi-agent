"""Pure normalization of Mattermost WS events into neutral chat types.

Quirks worth knowing (covered by tests/test_mm_events.py):
- inside a ``posted`` frame, ``data.post`` and ``data.mentions`` are
  JSON-encoded STRINGS inside the JSON frame — parse twice;
- ``root_id`` is always the thread ROOT (Mattermost threads don't nest);
- bot-authored posts carry ``props.from_bot = "true"`` (a string).

Conversation-key rule — the thread always wins:
- post with root_id (channel AND DM) -> that thread's session, reply in thread;
- top-level post in a DM            -> the DM-channel session, reply top-level;
- top-level post in a channel       -> start a new thread keyed by the post id.
"""

import json
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

from crucible.ports.chat.types import (
    KIND_CHANNEL,
    KIND_DM,
    KIND_THREAD,
    ConversationRef,
    IncomingMessage,
)

logger = logging.getLogger(__name__)

EVENT_POSTED = "posted"


@dataclass(frozen=True)
class FileHandle:
    """A file Mattermost says is attached to a post — what we need to fetch it.
    ``name``/``mime``/``size`` are empty when the post carried only ids and the
    metadata has to be fetched separately."""

    file_id: str
    name: str = ""
    mime: str = ""
    size: int = 0


def parse_files(frame: dict[str, Any]) -> tuple[FileHandle, ...]:
    """Files attached to a ``posted`` frame's post.

    Mattermost normally embeds the details under ``metadata.files``; older
    servers (and some event paths) carry only ``file_ids``, and those handles
    come back bare for the caller to fill in."""
    post = _embedded_json((frame.get("data") or {}).get("post"))
    if not isinstance(post, dict):
        return ()
    metadata = post.get("metadata") or {}
    described = metadata.get("files") if isinstance(metadata, dict) else None
    by_id: dict[str, FileHandle] = {}
    if isinstance(described, list):
        for info in described:
            if not isinstance(info, dict) or not info.get("id"):
                continue
            by_id[info["id"]] = _handle(info)
    handles: list[FileHandle] = []
    for file_id in post.get("file_ids") or by_id:
        if isinstance(file_id, str) and file_id:
            handles.append(by_id.get(file_id) or FileHandle(file_id=file_id))
    return tuple(handles)


def _handle(info: dict[str, Any]) -> FileHandle:
    size = info.get("size")
    return FileHandle(
        file_id=info["id"],
        name=str(info.get("name") or ""),
        mime=str(info.get("mime_type") or ""),
        size=size if isinstance(size, int) else 0,
    )


def parse_posted(frame: dict[str, Any], own_user_id: str) -> IncomingMessage | None:
    """Normalize one WS frame; None = not a message we could ever react to."""
    if frame.get("event") != EVENT_POSTED:
        return None
    data = frame.get("data") or {}

    post = _embedded_json(data.get("post"))
    if not isinstance(post, dict) or not post.get("id"):
        logger.warning("posted frame without a parsable post: %r", frame)
        return None

    if post.get("type"):  # system_join_channel etc. — never conversational
        return None
    user_id = post.get("user_id") or ""
    if user_id == own_user_id:  # own echo
        return None

    props = post.get("props") or {}
    is_from_bot = str(props.get("from_bot", "")).lower() == "true"
    hop_depth = _hop_depth(props)

    channel_id = post.get("channel_id") or ""
    post_id = post["id"]
    root_id = post.get("root_id") or ""
    is_dm = data.get("channel_type") == "D"

    if root_id:  # thread always wins, in DMs too
        conversation_id, kind, thread_root = root_id, KIND_THREAD, root_id
    elif is_dm:
        conversation_id, kind, thread_root = channel_id, KIND_DM, ""
    else:  # top-level channel post -> reply starts a thread under it
        conversation_id, kind, thread_root = post_id, KIND_THREAD, post_id

    mentions = _embedded_json(data.get("mentions")) or []
    mentioned = isinstance(mentions, list) and own_user_id in mentions

    return IncomingMessage(
        ref=ConversationRef(
            channel_id=channel_id,
            conversation_id=conversation_id,
            message_id=post_id,
            thread_root_id=thread_root,
        ),
        text=post.get("message") or "",
        user_id=user_id,
        username=str(data.get("sender_name") or "").lstrip("@"),
        timestamp=post_time(post),
        kind=kind,
        is_dm=is_dm,
        mentioned=mentioned,
        is_from_bot=is_from_bot,
        hop_depth=hop_depth,
        raw=frame,
    )


def should_respond(msg: IncomingMessage) -> bool:
    """Base dispatch rule: DMs — always; channels — only when mentioned. Other
    bots are ignored here; the gateway layers channel residency (sole-agent
    channels) and agent-to-agent replies on top."""
    if msg.is_from_bot:
        return False
    return msg.is_dm or msg.mentioned


def is_top_level(msg: IncomingMessage) -> bool:
    """True for a post outside any thread (its own id would root a new thread)."""
    return msg.ref.thread_root_id in ("", msg.ref.message_id)


def to_channel_session(msg: IncomingMessage) -> IncomingMessage:
    """Rewrite a top-level channel post to the channel-session shape: the
    conversation is the CHANNEL (like a DM), replies go top-level."""
    ref = ConversationRef(
        channel_id=msg.ref.channel_id,
        conversation_id=msg.ref.channel_id,
        message_id=msg.ref.message_id,
        thread_root_id="",
    )
    return replace(msg, ref=ref, kind=KIND_CHANNEL)


# Our namespace inside Mattermost post props. NOTE (portability): carrying the
# hop depth in per-message metadata only works on gateways that round-trip such
# metadata (MM post props do). A gateway without a message-metadata channel
# can't propagate hop depth this way — it would need an engine-side per-
# conversation depth counter, and until then the in-memory LoopGuard rate limit
# is the only cross-agent bound. Revisit when adding the second gateway.
PROPS_KEY = "crucible"


def _hop_depth(props: dict[str, Any]) -> int:
    meta = props.get(PROPS_KEY)
    if isinstance(meta, dict):
        depth = meta.get("depth")
        if isinstance(depth, int) and depth >= 0:
            return depth
    return 0


def post_time(post: dict[str, Any]) -> datetime | None:
    created = post.get("create_at")  # epoch milliseconds
    if isinstance(created, (int, float)) and created > 0:
        return datetime.fromtimestamp(created / 1000, tz=timezone.utc)
    return None


def _embedded_json(value: Any) -> Any:
    """data.post / data.mentions arrive as JSON strings inside the frame."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return value
