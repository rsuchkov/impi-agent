"""Build an AgentPresence for tests from a chat client (and an optional sink)."""

from crucible.interactions import AgentSink, MappingPresence
from crucible.ports.chat.client import ChatClient
from crucible.ports.chat.flow import MessageSink


class _DropSink:
    """A MessageSink that drops messages — for poster-only tests that never route."""

    def submit(self, msg, chat) -> None:  # noqa: D401 - trivial
        pass


def presence_of(
    chat: ChatClient, *, sink: MessageSink | None = None, agent: str = "assistant"
) -> MappingPresence:
    """A single-agent presence: `poster(agent)` -> chat, `sink(agent)` -> the sink."""
    return MappingPresence({agent: AgentSink(sink=sink or _DropSink(), chat=chat)})
