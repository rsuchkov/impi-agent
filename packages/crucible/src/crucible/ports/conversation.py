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


# (agent, runtime session id) -> its conversation, or None.
ConversationResolver = Callable[[str, str], Awaitable[SessionConversation | None]]
