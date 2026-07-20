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


@dataclass(frozen=True)
class DialogCallback:
    """A normalized modal-form submission."""

    state: str = ""  # the form token echoed back from the open call
    submission: dict = field(default_factory=dict)  # field name -> value
    cancelled: bool = False  # the user dismissed the modal
    user_id: str = ""


class CallbackCodec(Protocol):
    """A gateway's translation between its callback wire-shape and the neutral
    callbacks/replies. Injected into the receiver so the receiver stays neutral.

    ``reply_*`` return the response body a platform expects for a callback:
    ``reply_replace`` swaps the widget message text and drops its buttons,
    ``reply_notice`` shows an ephemeral note, ``reply_none`` makes no change."""

    def parse_action(self, body: dict) -> ActionCallback: ...
    def parse_dialog(self, body: dict) -> DialogCallback: ...
    def reply_replace(self, text: str) -> dict: ...
    def reply_notice(self, text: str) -> dict: ...
    def reply_none(self) -> dict: ...
