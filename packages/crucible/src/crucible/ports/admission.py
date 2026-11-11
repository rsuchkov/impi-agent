"""Whether a turn may run at all, as distinct from what it may do.

``ports.entitlement`` says what a conversation is allowed out of its agent's
profile; this port says whether the person who wrote may have a turn right
now. An application that knows better than the chat platform who is on the
other side — a chat session lives for months while the systems the agent acts
in expect a person to sign in again every few hours — answers from the message
that opens the turn: admitted, or not, with a word for the person and a link
only they may follow (a sign-in). The flow then runs no turn: nothing is
rendered, no process starts, and the person is told privately
(``ports.chat.client.WhisperedNotices``); the turn's outcome is
``TurnOutcome.NOT_ADMITTED`` and its notice code ``NOTICE_NOT_ADMITTED``.

Asked once per author of the turn's messages, every turn, synthetic messages
included — a command, a click, a scheduled run — so the application's rule
decides what a turn with no person behind it gets. Without a source every
turn is admitted, which is how the engine has always behaved.
"""

from dataclasses import dataclass
from typing import Protocol

from crucible.ports.chat.types import IncomingMessage

NOTICE_NOT_ADMITTED = "not_admitted"


@dataclass(frozen=True)
class NotAdmitted:
    """Why a person may not have a turn now, said to them alone. ``text`` is a
    sentence for them; ``link`` where they go to be admitted (a sign-in URL,
    theirs alone — never posted where others read), ``label`` the word on its
    button where the platform has one ("" = the client's default)."""

    text: str
    link: str = ""
    label: str = ""
    # The notice code the person's client gets; the application's rule says
    # ``not_admitted`` and the engine's budget rule its own word.
    code: str = NOTICE_NOT_ADMITTED


class TurnAdmission(Protocol):
    """Where a flow asks whether a turn may run: answered from the message
    that opens it (who sent it, where), before anything else happens."""

    async def admit(self, agent: str, msg: IncomingMessage) -> NotAdmitted | None:
        """None = admitted; ``NotAdmitted`` = not, and what to tell the person."""
        ...
