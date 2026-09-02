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

    ``on_event`` streams runtime events (basis for status/streaming UX); for
    stateful runs it is bound when the underlying session is first created.
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
