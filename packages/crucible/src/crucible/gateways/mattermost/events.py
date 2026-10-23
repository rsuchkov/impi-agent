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
from typing import Any, NamedTuple

from crucible.gateways.reactions import Reaction, emoji_name
from crucible.ports.chat.gateway import GATEWAY_MATTERMOST
from crucible.ports.chat.types import (
    KIND_CHANNEL,
    KIND_DM,
    KIND_THREAD,
    ConversationRef,
    IncomingMessage,
)

logger = logging.getLogger(__name__)

EVENT_POSTED = "posted"
EVENT_REACTION_ADDED = "reaction_added"
# A direct message's channel type, as Mattermost spells it.
DIRECT_CHANNEL_TYPE = "D"


class ConversationKey(NamedTuple):
    """Where a post's turn runs: the session key, its kind, and the thread a
    reply goes into ("" = top-level)."""

    conversation_id: str
    kind: str
    thread_root: str


def conversation_of(post_id: str, root_id: str, channel_id: str, *, is_dm: bool) -> ConversationKey:
    """The one conversation rule, for a post and a reaction alike: a reply
    belongs to its thread (in a DM too); a top-level DM post to the DM
    channel's session; a top-level channel post starts a thread under itself."""
    if root_id:
        return ConversationKey(root_id, KIND_THREAD, root_id)
    if is_dm:
        return ConversationKey(channel_id, KIND_DM, "")
    return ConversationKey(post_id, KIND_THREAD, post_id)


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
    is_dm = data.get("channel_type") == DIRECT_CHANNEL_TYPE
    conversation_id, kind, thread_root = conversation_of(post_id, root_id, channel_id, is_dm=is_dm)

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
        gateway=GATEWAY_MATTERMOST,
        raw=frame,
    )


def parse_reaction(frame: dict[str, Any]) -> Reaction | None:
    """Normalize one ``reaction_added`` WS frame; None = not one, or one that
    does not name the post, the person and the emoji. The channel comes from
    the broadcast envelope — the reaction itself names only the post."""
    if frame.get("event") != EVENT_REACTION_ADDED:
        return None
    data = frame.get("data") or {}
    reaction = _embedded_json(data.get("reaction"))
    if not isinstance(reaction, dict):
        return None
    emoji = emoji_name(str(reaction.get("emoji_name") or ""))
    user_id = str(reaction.get("user_id") or "")
    post_id = str(reaction.get("post_id") or "")
    channel_id = str((frame.get("broadcast") or {}).get("channel_id") or "")
    if not (emoji and user_id and post_id):
        return None
    return Reaction(emoji=emoji, user_id=user_id, channel_id=channel_id, message_id=post_id)


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
