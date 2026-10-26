"""What a runtime session's conversation is, to a tool: where it happens, who
spoke last, and where its files are.

The tool server fills a tool's context from it (``ToolContext.channel_id``,
``user_id``, ``files_dir``) by asking a ``ConversationResolver`` once per call,
with the calling agent and the session its proof was checked for. The
application answers from wherever it keeps its conversations —
``crucible.conversation_files.conversation_lookup`` over crucible's own session
store — and answers None for a session it does not know or that is not that
agent's.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SessionConversation:
    channel_id: str  # where the conversation happens
    user_id: str  # who spoke last ("" when nobody has)
    files_dir: Path | None = None  # what people sent in it is kept here; may not exist yet
    kind: str = ""  # KIND_DM, KIND_THREAD, KIND_CHANNEL; "" when not known
    # Whether only the person and the agent see it — a direct message, or a
    # thread inside one. ``kind`` says where a reply goes; this says who reads
    # it, which is what a tool that acts as the person asks.
    direct: bool = False
    # The gateway the conversation last spoke through, and every gateway it is
    # live in — what a tool's call may do depends on the first; where a card
    # goes, on the second.
    gateway: str = ""
    gateways: tuple[str, ...] = ()


# (agent, runtime session id) -> its conversation, or None.
ConversationResolver = Callable[[str, str], Awaitable[SessionConversation | None]]


@dataclass(frozen=True)
class EngineTurn:
    """A conversation the engine started a turn in by itself: which one, whose
    it is (who spoke last there), and when — so a program can show a person
    that something moved while they were away."""

    conversation_id: str
    user_id: str
    at: str  # ISO8601 UTC


# (agent, since — ISO8601 UTC) -> the agent's conversations with an engine
# turn at or after that moment, newest first.
EngineTurnsSince = Callable[[str, str], Awaitable[list[EngineTurn]]]
