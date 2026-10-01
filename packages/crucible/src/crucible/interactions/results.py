"""What feeding a click back into a conversation came to. A leaf, so a gateway
can name the outcome without importing the store-backed dispatcher."""

from enum import Enum, auto


class ActionResult(Enum):
    FED = auto()  # the value was fed back into the conversation as a new turn
    UNKNOWN = auto()  # no live interaction for this token (retire the buttons)
    UNAVAILABLE = auto()  # the agent has no sink to route to
