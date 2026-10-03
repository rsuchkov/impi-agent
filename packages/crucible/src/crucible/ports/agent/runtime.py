"""Agent-runtime ports.

Everything outside the concrete runtime depends only on these Protocols; a
concrete driver implements them with the SAME signatures (no cast at the
composition root — the driver narrows its profile type internally at its own
boundary).

``session_id`` is chosen by the caller (SessionStore), not derived inside the
runtime: the bot's inventory and the runtime's on-disk sessions must always
agree on the key.
"""

from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class PromptImage:
    """An image handed to the runtime as part of a turn's input.

    Images are the one kind of file a model may be able to look at directly, so
    they travel with the prompt instead of only being named in it. A runtime that
    can't take them ignores them — the prompt text names every attached file's
    path either way, so the agent can always fall back to reading the file.
    """

    data: bytes
    mime: str


@runtime_checkable
class AgentEvent(Protocol):
    """One streamed runtime event (tool started, text chunk done, ...).

    Read-only property, not a mutable attribute: concrete events are frozen
    dataclasses, and a writable protocol attribute would reject them.
    """

    @property
    def type(self) -> str: ...


EventCallback = Callable[[AgentEvent], Awaitable[None] | None]


class AgentResult(Protocol):
    """A single turn's outcome. Flows read the final text and whether any tool
    was called; concrete results may carry more (duration, stop reason)."""

    text: str
    # Names of the tools invoked this turn. A turn with no text but a non-empty
    # ``tool_calls`` acted deliberately — a tool that speaks to the user posts the
    # agent's message itself, so the silence after it is the answer. Flows use
    # this to tell that apart from a genuinely empty turn.
    tool_calls: list[str]


class AgentProfile(Protocol):
    """Opaque per-agent runtime configuration.

    A flow holds one and passes it to the runtime untouched; it never inspects
    the contents (which are runtime-specific, e.g. a config dir + model).
    """


class AgentRuntime(Protocol):
    """Drives an agent over a conversation.

    - ``run_stateful``  — keeps memory across turns under ``session_id``.
    - ``run_stateless`` — a fresh, memoryless run per call.

    ``on_event`` streams this call's runtime events (basis for status and
    streaming UX). It is bound per call, not per conversation: a later turn on
    the same session gets the callback it passed, not the first one's. Tool
    activity arrives as the neutral ``ToolStarted``/``ToolFinished`` events;
    the rest is the runtime's own vocabulary and promises only a ``type``.
    """

    async def run_stateful(
        self,
        profile: AgentProfile,
        session_id: str,
        message: str,
        *,
        on_event: EventCallback | None = None,
        cwd: str | None = None,
        images: Sequence[PromptImage] = (),
    ) -> AgentResult: ...

    async def run_stateless(
        self,
        profile: AgentProfile,
        message: str,
        *,
        on_event: EventCallback | None = None,
        images: Sequence[PromptImage] = (),
    ) -> AgentResult: ...

    def start(self) -> None:
        """Start background maintenance (idle reaping, ...). Idempotent."""
        ...

    async def close(self) -> None:
        """Release the runtime's resources (sessions, subprocesses)."""
        ...

    async def drop_agent_sessions(self, agent: str) -> int:
        """Drop an agent's idle sessions so its next turn re-initializes with the
        agent's current profile (used by hot-reload). Any in-flight turn is left
        to finish; persisted memory is unaffected — the next turn resumes it.
        Returns how many sessions were dropped."""
        ...


class TurnClock(Protocol):
    """Stops a turn's timeout while a human is being waited on.

    A turn's timeout is for a runtime that is stuck, not for a person who is
    slow: a confirmation card left on screen for two minutes must not end the
    turn — and, in a runtime that discards a timed-out session, cost the
    conversation its process. The runtime already pauses its own clock for the
    interactive requests it raises itself; this is the same pause offered to
    whoever else waits on a human on the turn's behalf — the tool server
    holding a call until its gate answers. Unknown session: a no-op, so a caller
    never has to know whether the session is live.
    """

    def human_wait(self, session_id: str) -> AbstractAsyncContextManager[None]: ...


@dataclass(frozen=True)
class RuntimeStats:
    """How full the runtime is: sessions alive (a process each), how many of
    them have a turn in flight, how many the pool admits, and how many turns are
    waiting for a slot right now."""

    alive: int
    busy: int
    capacity: int
    waiting: int


class RuntimeControl(Protocol):
    """Operating a runtime from outside its turns — what an application needs
    to manage conversations rather than merely run them.

    Kept apart from ``AgentRuntime`` so a flow depends only on running turns
    and a stand-in runtime in a test need not fake any of this.
    """

    def has_memory(self, agent: str, session_id: str) -> bool:
        """Whether the runtime still remembers this conversation — a live
        session, or its memory on disk — so a caller can tell a resumed
        conversation from one that starts over, without knowing where or how
        the runtime keeps it."""
        ...

    async def reset(self, agent: str, session_id: str) -> None:
        """Forget the conversation: end its process (freeing its slot now, not
        when the idle reaper gets to it) and delete its memory. The next turn
        under this id starts from nothing."""
        ...

    async def cancel(self, session_id: str) -> bool:
        """Interrupt the turn in flight, if there is one, and keep the session
        usable: the turn ends with whatever it had, and the next one resumes
        the conversation. False when nothing was running."""
        ...

    def stats(self) -> RuntimeStats: ...
