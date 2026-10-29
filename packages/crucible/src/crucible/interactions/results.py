"""What feeding a click back into a conversation came to. A leaf, so a gateway
can name the outcome without importing the store-backed dispatcher."""

from enum import Enum, auto


class ActionResult(Enum):
    FED = auto()  # the value was fed back into the conversation as a new turn
    UNKNOWN = auto()  # no live interaction for this token (retire the buttons)
    UNAVAILABLE = auto()  # the agent has no sink to route to


class TurnStop(Enum):
    """What asking to stop a conversation's turn came to."""

    STOPPED = auto()  # a turn was running and was told to end
    NOTHING_RUNNING = auto()  # no turn in this conversation right now
    UNAVAILABLE = auto()  # the agent has no sink, or its sink cannot stop a turn


# What the person who asked is told, privately.
TURN_STOP_ANSWERS = {
    TurnStop.STOPPED: "Stopping.",
    TurnStop.NOTHING_RUNNING: "Nothing is running here right now.",
    TurnStop.UNAVAILABLE: "This agent cannot be stopped here.",
}
