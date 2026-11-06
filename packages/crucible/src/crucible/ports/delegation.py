"""Tool-facing port for handing a task to another agent, through the engine.

An agent that wants another's work — one whose model is better at the task,
or one that is allowed what the asker is not — does not reach into that
agent, and does not need to share a chat with it: the engine opens a turn of
the other agent on the task alone, in a conversation of its own that holds
nothing but the task and its files, and when that turn ends it opens a turn
of the asker's with the answer, quoted as an agent's words. Asynchronous by
design: the call ends with the task's id, the other agent works, the answer
is a later turn. The tool that calls this is confirmed — what leaves the
conversation is read by a person first — and the asker can ask after a task
or call it off by its id.
"""

import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from crucible.ports.chat.client import ChatClient
from crucible.ports.chat.types import ConversationRef

# What the asked agent's turn carries in its scope's attributes, for the
# rules that decide who may have what (``ports.admission``,
# ``ports.entitlement``) and for a tool that wants to know: which agent handed
# the task on, the task's id, and the gateway the person behind it reached the
# asker through (the message's own ``user_id`` is that person's id there).
DELEGATED_BY_ATTRIBUTE = "delegated_by"
DELEGATION_TASK_ATTRIBUTE = "delegation_task"
ON_BEHALF_OF_GATEWAY_ATTRIBUTE = "on_behalf_of_gateway"
# Present ("1") on a trusted task's turn when the asker's trust hands the
# person's connected accounts over: the application's owner rule then reads
# whose accounts by ``on_behalf_of_gateway`` and the message's ``user_id``.
# Absent, a task's turn acts for nobody at any provider.
ACCOUNTS_HANDED_ATTRIBUTE = "accounts_handed"

# The asked agent's conversation for one task: a key of the engine's own, so
# nothing of the asker's conversation is in it, and new for every task.
_DELEGATED_PREFIX = "delegated:"


def delegation_id() -> str:
    return secrets.token_hex(6)


def delegated_conversation_key(task_id: str) -> str:
    """The key of the conversation the asked agent's turn on ``task_id`` runs in."""
    return f"{_DELEGATED_PREFIX}{task_id}"


# What a task came to. ``running`` until the asked agent's turn ends; then
# one of the rest, and the asker is told — except a task the asker cancelled.
DELEGATION_RUNNING = "running"
DELEGATION_ANSWERED = "answered"  # the asked agent said something (or ran a tool that answered)
DELEGATION_FAILED = "failed"  # its turn erred, was not admitted, or was lost to a restart
DELEGATION_TIMED_OUT = "timed_out"  # its turn ran past the asked agent's own limit
DELEGATION_CANCELLED = "cancelled"  # the asker called it off
DELEGATION_STATUSES = (
    DELEGATION_RUNNING, DELEGATION_ANSWERED, DELEGATION_FAILED, DELEGATION_TIMED_OUT, DELEGATION_CANCELLED,
)
# The notice a trusted task carries when the asker's turn ended before it did:
# what the asker handed over was the turn's, and went with it.
NOTICE_ASKER_TURN_ENDED = "asker_turn_ended"


@dataclass(frozen=True)
class DelegationView:
    """One task as the asker may see it: whom it went to, where it stands,
    and — once it ended — what came back and why it ended as it did."""

    task_id: str
    to: str
    status: str
    started_at: str
    finished_at: str = ""
    answer: str = ""
    notice: str = ""  # the notice code the asked agent's turn ended with, "" when none


@dataclass(frozen=True)
class FilePreview:
    """One file as the card shows it before it leaves: its name and size and,
    for a text file, its content as written — bounded, ``truncated`` saying
    when it was cut — with the language a fence names it by; ``text`` is None
    for a file that is not text, which the card names by name and size."""

    name: str
    size: int
    text: str | None = None
    language: str = ""
    truncated: bool = False
    # The file's content as a SHA-256 hex digest: what the card showed, for the
    # call that follows to hold the file to.
    digest: str = ""


@dataclass(frozen=True)
class TrustedLink:
    """A trusted task in flight, as the door's chat client and the tracer see
    it: whose turn asked (the asker and its runtime session), whom it went to,
    and where in the asker's conversation the task's cards and notices go —
    the asker's own client and the place its turn answers."""

    asker: str
    asker_session_id: str
    to: str
    poster: ChatClient
    ref: ConversationRef


class TrustedLinks(Protocol):
    """The trusted tasks in flight, by the conversation key their turn runs in."""

    def link_for(self, conversation_id: str) -> TrustedLink | None: ...


class DelegatedTurnChat(ChatClient, Protocol):
    """The chat client the turn on a task posts into: nobody's platform, so what
    the turn says is kept here for the asker — the replies as ``answer``, the
    code of the notice the turn ended with as ``notice_code``. A gateway
    provides one; the service reads it once the turn is over."""

    @property
    def answer(self) -> str: ...

    @property
    def notice_code(self) -> str: ...


class DelegationService(Protocol):
    """Handing a task to another agent, through the engine: a turn of that
    agent on the task alone, its answer back as a turn of the asker's."""

    async def delegate(
        self, agent: str, runtime_session_id: str, to: str, task: str, paths: Sequence[str] = (),
        *, digests: Mapping[str, str] | None = None,
    ) -> str:
        """Start the task for ``to`` from ``agent``'s turn; the task's id. The
        files are copied into the task's conversation as they are now — and,
        with ``digests`` (path → the digest ``preview_files`` gave), refused
        when one is no longer what the card showed. ``DelegationError`` when
        it cannot start — no such agent, one that cannot take a task here, a
        file that may not be sent or that changed since."""
        ...

    async def ask(
        self, agent: str, runtime_session_id: str, to: str, task: str, paths: Sequence[str] = (),
    ) -> str:
        """Start the task for ``to``, an agent ``agent`` trusts, from ``agent``'s
        turn, with no card: the task's id. The task's turn runs as the person
        with what the trust hands over, its cards come to the asker's
        conversation, and its answer is read with ``await_answers``; it ends
        with the asker's turn. ``DelegationError`` when ``to`` is not trusted or
        the task cannot start."""
        ...

    async def await_answers(
        self, agent: str, runtime_session_id: str, task_ids: Sequence[str],
    ) -> Sequence[DelegationView]:
        """Wait until every task named has ended, and how each did — for the
        tasks ``ask`` started from this conversation. ``DelegationError`` for one
        that is not this conversation's."""
        ...

    async def preview_files(self, agent: str, paths: Sequence[str]) -> Sequence[FilePreview]:
        """The files as the card shows them before the task goes, under the
        rules ``delegate`` sends them by: ``DelegationError`` for one the agent
        may not send, so the card never shows what the task could not carry."""
        ...

    async def status(self, agent: str, runtime_session_id: str, task_id: str) -> DelegationView:
        """Where a task this conversation handed on stands. ``DelegationError``
        for a task that is not this conversation's."""
        ...

    async def cancel(self, agent: str, runtime_session_id: str, task_id: str) -> bool:
        """Call a running task off: its turn is stopped and no answer comes.
        False when it had already ended. ``DelegationError`` for a task that
        is not this conversation's."""
        ...


class DelegationError(Exception):
    """Why a task could not be handed on, or asked after, in words for the model."""
