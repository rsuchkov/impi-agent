"""Platform-neutral message/conversation types.

The adapter (gateway) owns ALL platform addressing semantics: it decides the
conversation key and where replies go while normalizing, so flows never think
in terms of root_id vs thread_ts vs anything platform-shaped.

Conversation-kind rule (the vocabulary is platform-neutral):
the thread always wins — a post inside a thread belongs to that thread's
session (kind=thread), a top-level DM post to the DM-channel session (kind=dm),
a top-level channel post starts a new thread (kind=thread); a whole-channel
session is kind=channel.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

KIND_THREAD = "thread"
KIND_DM = "dm"
KIND_CHANNEL = "channel"


@dataclass(frozen=True)
class UserProfile:
    """Platform-neutral identity of a chat user (resolved on demand by the adapter)."""

    username: str = ""
    display_name: str = ""
    is_bot: bool = False


@dataclass(frozen=True)
class PostSnippet:
    """One historical message, as used for context backfill transcripts."""

    message_id: str
    username: str
    text: str
    timestamp: datetime | None = None  # when it was sent (UTC-aware); None if unknown
    # Author's platform user id ("" if unknown) — lets a flow tell the agent's own
    # posts apart from everyone else's when replaying history.
    user_id: str = ""


@dataclass(frozen=True)
class Action:
    """An affordance on an interactive message. A button (``kind="button"``, the
    default) echoes ``value`` back when clicked; a select (``kind="select"``)
    renders a dropdown of ``options`` and echoes the picked one back. ``context``
    is opaque per-action data the adapter round-trips (we stash the interaction
    token there)."""

    id: str
    label: str
    value: str = ""
    style: str = ""  # e.g. "primary" | "danger" (adapter maps it)
    context: dict[str, Any] = field(default_factory=dict)
    kind: str = "button"  # "button" | "select"
    options: tuple[str, ...] = ()  # dropdown choices when kind == "select"


@dataclass(frozen=True)
class FormField:
    """One input of a modal form. ``type`` ∈ {text, textarea, select, bool}; a
    select carries ``options``. Values come back keyed by ``name``."""

    name: str
    label: str
    type: str = "text"
    options: tuple[str, ...] = ()  # for type == "select"
    optional: bool = False
    placeholder: str = ""


@dataclass(frozen=True)
class Form:
    """A structured modal collected in one submit. ``intro`` shows next to the
    "fill in" button that opens it (a modal needs a click for its trigger)."""

    title: str
    fields: tuple[FormField, ...]
    intro: str = ""
    submit_label: str = "Submit"


@dataclass(frozen=True)
class ConversationRef:
    """Platform-neutral address of a conversation/message."""

    channel_id: str
    conversation_id: str  # session key: thread root id, or channel id (dm/channel)
    message_id: str  # the message to react to / reply under
    thread_root_id: str = ""  # thread replies go into; "" = post top-level


@dataclass
class IncomingMessage:
    ref: ConversationRef
    text: str
    user_id: str
    username: str = ""  # sender's login name, for the identity envelope
    timestamp: datetime | None = None  # when it was sent (UTC-aware); None if unknown
    kind: str = KIND_THREAD  # KIND_* of the conversation this message belongs to
    is_dm: bool = False
    mentioned: bool = False  # explicitly mentions the agent
    is_from_bot: bool = False
    hop_depth: int = 0  # agent-to-agent hops since the last human (human = 0)
    # True for messages the engine synthesizes from a widget click (not typed by a
    # human). Lets the coalescer skip the "typed instead of clicking" auto-cancel.
    synthetic: bool = False
    # Escape hatch: the platform-native payload. Only adapters/dispatchers may
    # read it — flows never do (they'd silently couple to the platform).
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def channel_id(self) -> str:
        return self.ref.channel_id

    @property
    def conversation_id(self) -> str:
        return self.ref.conversation_id
