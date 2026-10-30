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

from crucible.ports.turn import TurnScope

KIND_THREAD = "thread"
KIND_DM = "dm"
KIND_CHANNEL = "channel"

# What a click or a command is answered with when the answer is the engine's
# rather than an agent's. Beside the vocabulary so the HTTP receiver and a
# socket gateway say the same thing — two spellings would drift.
COMMAND_ACK_TEXT = "Working on it — the answer will appear in this conversation."
AGENT_UNAVAILABLE_TEXT = "The agent is currently unavailable."
NOT_AN_APPROVER_TEXT = "Only an approver can answer that request."
BUTTONS_RETIRED_TEXT = "These buttons are no longer active."


@dataclass(frozen=True)
class UserProfile:
    """Platform-neutral identity of a chat user (resolved on demand by the adapter)."""

    username: str = ""
    display_name: str = ""
    is_bot: bool = False
    # "" when the platform does not say — Mattermost hides it unless its
    # privacy settings show it, Slack without the users:read.email scope.
    email: str = ""


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
class Choice:
    """One option of a dropdown: what the human reads, and what comes back when
    they pick it. They differ whenever the value is machinery — an id, a routing
    key — that no one should be shown."""

    label: str
    value: str

    @staticmethod
    def of(*labels: str) -> tuple["Choice", ...]:
        """Choices whose value IS their label — a plain list to pick from."""
        return tuple(Choice(label=label, value=label) for label in labels)


# How much an application may put on a control: a Slack button carries token,
# form, value and this in one value of 2000 characters.
ACTION_DATA_MAX = 256


@dataclass(frozen=True)
class Action:
    """An affordance on an interactive message (``kind`` ∈ ``ACTION_KINDS``).
    ``label`` is what the person sees on the control and what the message says
    they chose; ``value`` is what the agent hears as the click's text; ``data``
    is the application's own, carried with the click and displayed nowhere —
    an id the click is about (``IncomingMessage.click.data``). Not a secret:
    on Slack it travels in the message's own blocks, which anyone in the
    channel may read; at most ``ACTION_DATA_MAX`` characters, since a Slack
    button carries everything in one bounded value. Buttons carry it on both
    chat platforms, menus on Mattermost only. ``context`` is opaque per-action
    data the adapter round-trips (we stash the interaction token there)."""

    id: str
    label: str
    value: str = ""
    style: str = ""  # e.g. "primary" | "danger" (adapter maps it)
    context: dict[str, Any] = field(default_factory=dict)
    kind: str = ACTION_BUTTON
    options: tuple[Choice, ...] = ()  # dropdown choices when kind is ACTION_SELECT
    data: str = ""

    def __post_init__(self) -> None:
        if len(self.data) > ACTION_DATA_MAX:
            raise ValueError(f"Action.data is {len(self.data)} characters; at most {ACTION_DATA_MAX} travel with a control")


@dataclass(frozen=True)
class Click:
    """The click a synthetic message was made from: which control
    (``Action.id``), what the application put on it (``Action.data``), and the
    interaction it answered. For the application; the model reads the
    message's text, which is the control's ``value``."""

    action_id: str
    data: str
    interaction_id: str


# The neutral field vocabulary of a modal form. Adapters translate each name into
# their own control (see the mapping table in docs/creating-agents.md); nothing
# above the ports layer knows what a platform calls them.
FIELD_TYPES = (
    "text", "textarea", "number", "email", "url", "tel",  # typed free text
    "password",                                           # free text, masked
    "select", "multiselect", "radio", "bool",             # choices
    "user", "users", "channel", "channels",               # workspace pickers
    "date", "datetime", "time",                           # temporal
    "label",                                              # static text, no value
)
# Masking is the platform's, and not every platform has it — an adapter that
# cannot mask renders plain text rather than refusing the form. A value typed
# into one of these is still a value that travelled through the chat platform;
# the field type is about shoulder-surfing, not about secrecy.
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
class PreviewRow:
    """One labelled line of a preview. ``before`` is the current value when the
    call would change it, so a card can show the change, not only the target.
    ``passage`` says the value is text the person is asked to approve as
    written — a comment, a description — and is shown whole, with its lines,
    rather than as a one-line fact cut to fit."""

    label: str
    value: str
    before: str = ""
    passage: bool = False


@dataclass(frozen=True)
class CallPreview:
    """What a call a person is asked to confirm would do, as the system knows
    it: a one-line title, labelled rows, whether it deserves a warning (an
    irreversible step, a file that will run), and a verbatim piece the person
    must read as written — a script, a command line — under ``block_label``.
    Plain data; each channel draws it its own way — a chat renders it into the
    card's text, a program reading a journal gets the rows themselves."""

    title: str
    rows: tuple[PreviewRow, ...] = ()
    danger: bool = False
    block_label: str = ""
    block: str = ""
    # What the block is written in — ``json``, ``groovy``, ``diff``, a shell —
    # for a renderer that highlights ("" when it is plain text or unknown).
    block_language: str = ""


@dataclass(frozen=True)
class CannotRun:
    """What a tool says instead of a preview when the call cannot be done as
    asked — the fragment to replace is not in the file, the account is not
    connected, the record does not exist: nobody is asked, and the model hears
    the reason. Not a failure of the preview (that falls back to the arguments
    and still asks) but the tool's word that there is nothing to ask about."""

    reason: str


# How a closed card was decided, for a program that draws its own line rather
# than show the headline: allowed for this call, allowed for a window
# (``grant_s`` long), refused, nobody answered in time, or the caller withdrew
# the question before anyone did.
VERDICT_ALLOWED_ONCE = "allowed_once"
VERDICT_ALLOWED_FOR = "allowed_for"
VERDICT_DENIED = "denied"
VERDICT_EXPIRED = "expired"
VERDICT_WITHDRAWN = "withdrawn"


@dataclass(frozen=True)
class ApprovalCard:
    """A request for a human's yes, as data: who asks, what for, and the facts
    to decide by. Every channel draws it its own way — Markdown, Block Kit
    fields, a JSON event — so nothing here is rendered.

    ``agent`` and ``headline`` are the engine's sentence ("wants to run
    transition_issue.") and nothing the caller wrote goes in them; ``title`` is
    the one-line account of the call from whoever asks (a tool's preview),
    ``tool`` the id of the tool being confirmed ("" on a card about something
    else, a credential say) — both their own fields, so a program reading the
    card does not tell them apart from ``fields`` by an English label; ``fields``
    are what the call would change (a ``before`` where it changes something);
    ``block`` is a verbatim piece the
    person must read as written — a command line, the raw arguments — under
    ``block_label``; ``danger`` asks for a warning; ``expires_at`` (ISO-8601
    UTC) is until when the question stands. Values may be the caller's text:
    renderers harden them, the card does not. A closed card says its verdict
    in ``headline`` for a person and in ``verdict`` (``VERDICT_*``, "" while
    the question stands) for a program, with ``grant_s`` for a window."""

    agent: str
    headline: str
    icon: str = "⚙️"
    title: str = ""
    tool: str = ""
    fields: tuple[PreviewRow, ...] = ()
    block_label: str = ""
    block: str = ""
    block_language: str = ""  # the preview's word for what the block is written in
    danger: bool = False
    expires_at: str = ""
    verdict: str = ""
    grant_s: int = 0


STEP_RUNNING = "running"
STEP_DONE = "done"
STEP_FAILED = "failed"
STEP_STATES = (STEP_RUNNING, STEP_DONE, STEP_FAILED)


@dataclass(frozen=True)
class Step:
    """One step of a turn as a person watching sees it — a tool call with its
    caption — in the state it is in. ``note`` is a line under the title: the
    model's remark that led to the call, or "waiting for your approval"."""

    id: str
    title: str
    state: str = STEP_RUNNING
    note: str = ""


@dataclass(frozen=True)
class Card:
    """A block of text with its own controls — one row of a rendered screen.

    A message built from cards puts the buttons NEXT TO what they act on (a
    skill and its "Details"), instead of one prompt above a row of buttons.
    ``accent`` is a colour hint an adapter may use for the card's edge (""=none);
    platforms without the notion ignore it. ``steps`` are the steps of a turn
    in flight, under the text: a platform with a view of a plan draws them as
    one (Slack's plan block), another lists them as lines."""

    text: str
    actions: tuple[Action, ...] = ()
    accent: str = ""
    steps: tuple[Step, ...] = ()


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
    # What the control starts out holding — real content, not the grey hint.
    # A form that edits something existing needs this: a placeholder showing the
    # current value looks pre-filled and reads as something to add to, while the
    # submit gets an empty field and the caller has to guess what that meant.
    value: str = ""


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


# A file's kind is decided by its media type; images are the one class the
# runtime may be able to look at directly.
IMAGE_MIME_PREFIX = "image/"


@dataclass(frozen=True)
class Attachment:
    """A file that arrived with a message, already saved to local disk.

    The adapter downloads it while normalizing the event, so everything above the
    gateway sees a plain local file: ``path`` is absolute and readable by the
    runtime, ``name`` is what the sender called it."""

    name: str
    path: str
    mime: str = ""
    size: int = 0

    @property
    def is_image(self) -> bool:
        return self.mime.startswith(IMAGE_MIME_PREFIX)


@dataclass(frozen=True)
class OutgoingFile:
    """A file the agent sends into a conversation, already read into memory."""

    name: str
    data: bytes
    mime: str = ""


@dataclass(frozen=True)
class ConversationRef:
    """Platform-neutral address of a conversation/message."""

    channel_id: str
    conversation_id: str  # session key: thread root id, or channel id (dm/channel)
    message_id: str  # the message to react to / reply under
    thread_root_id: str = ""  # thread replies go into; "" = post top-level


@dataclass(frozen=True)
class EventReport:
    """What happened outside the conversation, for a turn the engine starts
    on an event: where the report comes from (a system's name) and the report
    itself. Data, not a request — the flow quotes it in a context block, so
    the model reads it as information rather than as somebody's instructions."""

    source: str
    text: str


@dataclass(frozen=True)
class QuotedMessage:
    """A message this one points at — the one a reaction was put on — with who
    wrote it. Somebody else's words: the flow quotes them as that person's, in
    a context block, never as the speaker's own."""

    author: str  # a username, or an id when the platform gave none
    text: str


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
    # Which gateway delivered it (``ports.chat.gateway.GATEWAY_*``): the one the
    # reply goes back through, and the one the conversation is now live in. ""
    # when the engine made the message itself and nobody reached in.
    gateway: str = ""
    hop_depth: int = 0  # agent-to-agent hops since the last human (human = 0)
    # Files the sender attached, already downloaded to local disk by the adapter.
    attachments: tuple[Attachment, ...] = ()
    # True for messages the engine synthesizes from a widget click (not typed by a
    # human). Lets the coalescer skip the "typed instead of clicking" auto-cancel.
    synthetic: bool = False
    # What belongs to the turn this message starts — a credential sent along
    # with it, a request id — for the tools that run inside it. Never part of
    # ``raw`` and kept out of repr: it is the one field that must not be written
    # down. None on platforms where nothing is per turn (every chat gateway).
    turn: TurnScope | None = field(default=None, repr=False)
    # The message this one points at, when it does — a command run by a
    # reaction names the message the emoji was put on. Rendered as another
    # person's quoted words, under their name, not as this speaker's text.
    quoted: QuotedMessage | None = None
    # The click this message was made from, when it was (``synthetic``): the
    # control's id and the application's data on it. Not shown to the model.
    click: Click | None = None
    # A turn the engine started by itself — on a schedule, on an event — not
    # one a person's message opened. Recorded on the conversation so a program
    # can ask which of a person's conversations moved while they were away.
    engine_started: bool = False
    # The event such a turn is about, when it is about one: rendered as data
    # ahead of the speaker line, which then carries the one-phrase summary.
    event: EventReport | None = None
    # Escape hatch: the platform-native payload. Only adapters/dispatchers may
    # read it — flows never do (they'd silently couple to the platform).
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def channel_id(self) -> str:
        return self.ref.channel_id

    @property
    def conversation_id(self) -> str:
        return self.ref.conversation_id
