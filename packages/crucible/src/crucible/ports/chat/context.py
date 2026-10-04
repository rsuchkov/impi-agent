"""What a flow puts in front of the person's message: the context the runtime
does not have.

Two sources, both the application's to implement and both optional. A
``HistorySource`` is where the conversation is kept when the chat platform
cannot hand it back — a direct message over the http gateway has no thread to
read, so the application's own transcript is the only copy; the flow asks for it
when the runtime has no memory of the conversation (a fresh session, or one it
has since forgotten) and replays it into the first prompt. A ``ContextSource``
says who is asking — the person's role, the system they are signed in to —
ahead of what they said, every turn, because the runtime cannot learn it from
the message.
"""

from typing import Protocol

from crucible.ports.chat.types import IncomingMessage, PostSnippet


class HistorySource(Protocol):
    """The application's own record of a conversation, for a runtime that
    starts it over. ``recent`` returns the earlier messages, oldest first, as
    snippets (the current message may be among them; the flow leaves it out).
    An empty list means there is nothing to replay."""

    async def recent(self, agent: str, msg: IncomingMessage) -> list[PostSnippet]: ...


class ContextSource(Protocol):
    """A few lines about the person asking, put before their message. An empty
    string means nothing to add this turn."""

    async def preamble(self, agent: str, msg: IncomingMessage) -> str: ...
