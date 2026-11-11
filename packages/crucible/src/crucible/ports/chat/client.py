"""Outbound port: how an agent talks to the platform (replies, reactions, and
interactive widgets), regardless of platform."""

from typing import Protocol, runtime_checkable

from crucible.ports.chat.types import (
    Action,
    ApprovalCard,
    Card,
    ConversationRef,
    Form,
    OutgoingFile,
    PostSnippet,
    UserProfile,
)


class NobodyToAnswer(Exception):
    """A card, a widget or a form was put to a chat client behind which no
    person can answer it — a turn's that runs for nobody. Raised rather than
    degraded, so what waits for a yes is refused instead of waiting."""


class ChatClient(Protocol):
    """The agent's outbound platform surface. Posting verbs:
    - ``post_reply``  — a real answer from the agent (rich rendering applies);
    - ``post_notice`` — verbatim status/system text (fallback message), as-is.

    Reactions are an optional capability — adapters on platforms without them
    may no-op. ``format_mention`` builds platform mention markup (sync, pure).
    The widget verbs (``post_actions``/``retract``/``open_dialog``) post clickable
    affordances whose clicks call back to the engine.
    """

    async def post_reply(self, ref: ConversationRef, text: str, *, hop_depth: int = 0) -> None:
        """Post an agent reply. ``hop_depth`` travels with the message (agent-to-
        agent loop accounting); adapters stamp it where other agents can read it."""
        ...

    async def post_notice(self, ref: ConversationRef, text: str, *, code: str = "") -> None:
        """Post status/system text as-is. ``code`` names the kind of notice
        (``timeout``, ``empty_answer``, …) for a client that is a program rather
        than a person and words things itself; a chat adapter shows the text and
        ignores it. "" = no kind, just text."""
        ...

    async def add_reaction(self, ref: ConversationRef, name: str) -> None: ...

    async def remove_reaction(self, ref: ConversationRef, name: str) -> None: ...

    async def get_user_profile(self, user_id: str) -> UserProfile | None: ...

    async def resolve_channel(self, channel_id: str) -> str:
        """Channel id -> its display name, "" if unknown. The counterpart of
        ``get_user_profile`` for channels: a channel picker returns an id, and the
        engine shows the agent a name. Adapters without channels return ""."""
        ...

    async def get_thread_posts(self, ref: ConversationRef) -> list[PostSnippet]:
        """The whole thread ``ref`` belongs to, oldest first (context backfill).
        Takes the full ref so adapters that address a thread by (channel, root)
        — not by a globally-unique root id — can fetch it. [] if unavailable."""
        ...

    async def get_recent_posts(self, channel_id: str, limit: int = 20) -> list[PostSnippet]:
        """Recent channel messages, oldest first (channel-session backfill)."""
        ...

    def format_mention(self, username: str, *, user_id: str = "") -> str:
        """How this platform addresses a person or an agent in text so that it
        is a mention — by name, or by ``user_id`` where the platform wants
        the id (Slack); a client that needs no id ignores it."""
        ...

    async def post_files(
        self, ref: ConversationRef, files: list[OutgoingFile], *, text: str = "", hop_depth: int = 0
    ) -> None:
        """Post files into the conversation as the agent, with ``text`` as their
        message. One message carries them all where the platform allows it. ``hop_depth`` as for ``post_reply``: a
        message another agent is meant to answer carries the cascade count."""

    # -- Interactive widgets ------------------------------------------------

    async def post_actions(
        self, ref: ConversationRef, text: str, actions: list[Action], *, callback_url: str
    ) -> str:
        """Post ``text`` with clickable ``actions`` as the agent. Each action, when
        clicked, makes the platform POST to ``callback_url`` with the action's
        ``context``. Returns the posted message id."""
        ...

    async def post_confirmation(
        self, ref: ConversationRef, card: ApprovalCard, actions: list[Action], *, callback_url: str
    ) -> str:
        """Put a request for a human's yes in front of them: the card as data,
        drawn the way this platform draws things (Markdown, native fields, a
        structured event), with ``actions`` as its answers. Returns the posted
        message id, which ``retract`` later retires."""
        ...

    async def update_confirmation(
        self,
        post_id: str,
        card: ApprovalCard,
        actions: list[Action] | None = None,
        *,
        callback_url: str = "",
    ) -> None:
        """Rewrite a posted confirmation with new data — the verdict once it is
        answered, a notice folding another occurrence into itself. No
        ``actions`` means nothing is left to click. Best-effort, like ``retract``:
        a platform hiccup here must not undo a decision already made."""
        ...

    async def retract(self, post_id: str, text: str) -> None:
        """Replace a previously-posted widget's text and drop its buttons — used
        when a blocking request expires or is cancelled, so no stale button can be
        clicked afterwards. Best-effort (a failure must not break the turn)."""
        ...

    async def delete_post(self, post_id: str) -> None:
        """Remove a post the engine made — a progress message whose turn is
        over. Best-effort: a post already gone, or a platform that cannot
        delete, costs the turn nothing."""
        ...

    async def post_cards(
        self, ref: ConversationRef, cards: list[Card], *, callback_url: str
    ) -> str:
        """Post a message built from cards — each with its own text and its own
        controls, so a button sits next to the thing it acts on. Returns the
        posted message id."""
        ...

    async def update_cards(
        self, post_id: str, cards: list[Card], *, callback_url: str
    ) -> None:
        """Rewrite a posted message as these cards — how a screen redraws itself
        in place (paging a list must not post a new message every click).
        ``retract`` is the degenerate case: one card, no controls."""
        ...

    async def open_dialog(
        self, trigger_id: str, form: Form, *, submit_url: str, state: str
    ) -> None:
        """Open ``form`` as a modal for the user whose click produced ``trigger_id``
        (short-lived — call synchronously in the click handler). On submit the
        platform POSTs to ``submit_url`` with ``state`` echoed back."""
        ...


@runtime_checkable
class ConversationView(Protocol):
    """What a platform shows of a conversation beside its messages — a chat
    client implements this when its platform has such a view (Slack's agent
    sessions: a working indicator and a Stop button while the agent is busy).
    The flow marks a conversation busy for the length of a turn and free after
    it; a client without the view ignores the matter and is never asked."""

    async def set_busy(self, ref: ConversationRef, busy: bool) -> None: ...

    async def set_waiting(self, ref: ConversationRef, waiting: bool) -> None:
        """The busy conversation waits on the person — a confirmation card is
        up — and goes on once they answer. Only ever between ``set_busy(True)``
        and ``set_busy(False)``; a view with no such state ignores it."""
        ...


# The word on a whispered notice's link where the platform draws a button and
# the notice names none.
WHISPER_LINK_LABEL = "Open"


@runtime_checkable
class WhisperedNotices(Protocol):
    """A word to one person in a conversation, seen by them alone. A client
    implements this where its platform can address one person inside a
    conversation (an ephemeral post, on Slack and Mattermost) or where the
    conversation has one reader anyway (an http caller's journal, a ws
    service's frames). The flow uses it for what nobody else there may read:
    a turn that was not admitted, with the link only that person may follow
    (a sign-in). A client without it is never asked; the flow then says in the
    open what it can, and keeps the link to itself."""

    async def whisper_notice(
        self, ref: ConversationRef, user_id: str, text: str, *, code: str = "", link: str = "", label: str = "",
    ) -> None:
        """``text`` to ``user_id`` in the conversation ``ref`` names — markdown,
        rendered as the platform renders a reply (a ``post_notice`` is sent as
        it is; this is a sentence the application wrote); ``code`` as for
        ``post_notice``; ``link`` a URL for that person alone, shown as a
        button where the platform has one (``label`` its word,
        ``WHISPER_LINK_LABEL`` when "") and as a link in the text where it has
        not."""
        ...


@runtime_checkable
class WhisperedCards(Protocol):
    """A request for one person's yes, shown to them alone where other people
    read. A client implements this where its platform can put a message with
    buttons in front of one person inside a conversation (an ephemeral post,
    on Slack and Mattermost). The tool gate uses it in a channel or a thread:
    the question goes to the person who asked, not to everyone there. A
    client without it is asked the way it always was, ``post_confirmation``.

    What such a card can become afterwards is the platform's: a message only
    one person sees is kept by nobody, so the platform may have no way to
    rewrite it by its id. ``update_confirmation`` and ``retract`` on the id
    returned here rewrite the card where the platform can — Slack can, once
    the person has clicked it — and leave it as it is where it cannot; the
    click's own reply then says what was decided."""

    async def whisper_confirmation(
        self, ref: ConversationRef, user_id: str, card: ApprovalCard, actions: list[Action], *, callback_url: str
    ) -> str:
        """As ``post_confirmation``, to ``user_id`` alone, in the conversation
        ``ref`` names. Returns the posted message id."""
        ...
