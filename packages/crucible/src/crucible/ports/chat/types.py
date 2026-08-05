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


# What an interactive message may carry. A button echoes ``value`` back; the
# menus echo the pick — of ``options`` (select), of the workspace's people
# (user_select) or of its channels (channel_select).
ACTION_BUTTON = "button"
ACTION_SELECT = "select"
ACTION_USER_SELECT = "user_select"
ACTION_CHANNEL_SELECT = "channel_select"
ACTION_KINDS = (ACTION_BUTTON, ACTION_SELECT, ACTION_USER_SELECT, ACTION_CHANNEL_SELECT)
PICKER_KINDS = frozenset({ACTION_USER_SELECT, ACTION_CHANNEL_SELECT})


@dataclass(frozen=True)
class Action:
    """An affordance on an interactive message (``kind`` ∈ ``ACTION_KINDS``).
    ``context`` is opaque per-action data the adapter round-trips (we stash the
    interaction token there)."""

    id: str
    label: str
    value: str = ""
    style: str = ""  # e.g. "primary" | "danger" (adapter maps it)
    context: dict[str, Any] = field(default_factory=dict)
    kind: str = ACTION_BUTTON
    options: tuple[str, ...] = ()  # dropdown choices when kind is ACTION_SELECT


# The neutral field vocabulary of a modal form. Adapters translate each name into
# their own control (see the mapping table in docs/creating-agents.md); nothing
# above the ports layer knows what a platform calls them.
FIELD_TYPES = (
    "text", "textarea", "number", "email", "url", "tel",  # typed free text
    "select", "multiselect", "radio", "bool",             # choices
    "user", "users", "channel", "channels",               # workspace pickers
    "date", "datetime", "time",                           # temporal
    "label",                                              # static text, no value
)
# Types whose value is a list of picks rather than one.
MULTI_FIELD_TYPES = frozenset({"multiselect", "users", "channels"})
# Types the user picks from the workspace: the platform returns an ID, which the
# engine resolves to a readable name.
USER_FIELD_TYPES = frozenset({"user", "users"})
CHANNEL_FIELD_TYPES = frozenset({"channel", "channels"})
# Types that carry no value at all — rendered as static text inside the form.
STATIC_FIELD_TYPES = frozenset({"label"})
# Which field type a picked widget answers with. It travels with the click (and
# is read off the payload on Slack), so the engine knows a value is an id to
# resolve into a name — see crucible.interactions.labels.
PICK_FIELD_BY_KIND = {ACTION_USER_SELECT: "user", ACTION_CHANNEL_SELECT: "channel"}


@dataclass(frozen=True)
class FormField:
    """One input of a modal form (``type`` ∈ ``FIELD_TYPES``). ``options`` are the
    choices of a select/multiselect/radio; ``help_text`` is the hint shown under
    the control. Values come back keyed by ``name`` — a ``label`` field has none
    (it is static text)."""

    name: str
    label: str
    type: str = "text"
    options: tuple[str, ...] = ()  # for select / multiselect / radio
    optional: bool = False
    placeholder: str = ""
    help_text: str = ""


@dataclass(frozen=True)
class Form:
    """A structured modal collected in one submit. ``intro`` shows next to the
    "fill in" button that opens it (a modal needs a click for its trigger).

    Two labels, two buttons: ``open_label`` is the one in the conversation that
    opens the modal ("" = the engine's default wording), ``submit_label`` the one
    inside it."""

    title: str
    fields: tuple[FormField, ...]
    intro: str = ""
    submit_label: str = "Submit"
    open_label: str = ""


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
