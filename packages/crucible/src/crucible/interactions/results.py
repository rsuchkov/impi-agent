"""What feeding a click back into a conversation came to. A leaf, so a gateway
can name the outcome without importing the store-backed dispatcher."""

from enum import Enum, auto

from crucible.ports.chat.flow import TurnStop


class ActionResult(Enum):
    FED = auto()  # the value was fed back into the conversation as a new turn
    UNKNOWN = auto()  # no live interaction for this token (retire the buttons)
    UNAVAILABLE = auto()  # the agent has no sink to route to


# What the person who asked is told, privately.
TURN_STOP_ANSWERS = {
    TurnStop.STOPPED: "Stopping.",
    TurnStop.NOTHING_RUNNING: "Nothing is running here right now.",
    TurnStop.NOT_THEIRS: "Only the person whose message started this turn can stop it.",
    TurnStop.UNAVAILABLE: "This agent cannot be stopped here.",
}
