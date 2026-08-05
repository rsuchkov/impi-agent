"""Neutral interaction-callback vocabulary.

A gateway delivers widget clicks and modal submissions in its own wire shape
(Mattermost over an HTTP callback, Slack over the socket). A ``CallbackCodec``
translates that shape to and from these neutral records, so the HTTP receiver and
the InteractionDispatcher never speak a platform's payload/response format.
"""

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class ActionCallback:
    """A normalized button/select click."""

    token: str = ""  # the widget / pending-UI token carried in the action's context
    value: str = ""  # the chosen value (button value or picked option)
    form_token: str = ""  # set when the click is a "fill in" button that opens a form
    trigger: str = ""  # short-lived token to open a modal (form-open clicks only)
    user_id: str = ""  # who clicked
    # "user" / "channel" when the widget was a picker — then ``value`` is a
    # platform id the engine resolves to a name before the agent sees it.
    pick: str = ""
    # Set when the click belongs to a screen the engine renders itself: which
    # screen, its encoded state, and the message to rewrite in place.
    screen: str = ""
    state: str = ""
    post_id: str = ""


@dataclass(frozen=True)
class DialogCallback:
    """A normalized modal-form submission."""

    state: str = ""  # the form token echoed back from the open call
    submission: dict = field(default_factory=dict)  # field name -> value
    cancelled: bool = False  # the user dismissed the modal
    user_id: str = ""


@dataclass(frozen=True)
class CommandCallback:
    """A normalized slash-command invocation.

    ``root_id`` is the thread the command was typed in ("" outside a thread) —
    it becomes the conversation the agent's turn runs in. ``token`` is the
    platform's per-command verification token, checked against the configured
    ones before anything reaches an agent."""

    command: str = ""  # the trigger, e.g. "/summarize"
    text: str = ""  # arguments typed after the trigger
    channel_id: str = ""
    root_id: str = ""  # thread root, "" when invoked outside a thread
    user_id: str = ""  # who invoked it
    user_name: str = ""
    token: str = ""  # per-command verification token
    response_url: str = ""  # for delayed replies (unused today)


class CallbackCodec(Protocol):
    """A gateway's translation between its callback wire-shape and the neutral
    callbacks/replies. Injected into the receiver so the receiver stays neutral.

    ``reply_*`` return the response body a platform expects for a callback:
    ``reply_replace`` swaps the widget message text and drops its buttons,
    ``reply_notice`` shows an ephemeral note, ``reply_none`` makes no change,
    ``reply_ack`` answers a command immediately (a receipt — the agent's own
    reply follows in the conversation when the turn finishes)."""

    def parse_action(self, body: dict) -> ActionCallback: ...
    def parse_dialog(self, body: dict) -> DialogCallback: ...
    def parse_command(self, body: dict) -> CommandCallback: ...
    def reply_replace(self, text: str) -> dict: ...
    def reply_notice(self, text: str) -> dict: ...
    def reply_none(self) -> dict: ...
    def reply_ack(self, text: str) -> dict: ...
