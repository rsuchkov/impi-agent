"""Session-inventory port + the deterministic session-id rule.

The id is derived, never invented: ``<agent>--<conversation_id>``, coerced to the
runtime's allowed alphabet. Deterministic derivation means a lost DB still resumes
runtime-side memory from disk — the DB only exists so humans (and the cleanup CLI)
can enumerate what sessions exist. The runtime applies the same coercion
defensively when it starts a session; ``tests/test_session_store.py`` pins the
agreement so the two never drift.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from crucible.ports.chat.directory import AgentInfo


def derive_runtime_session_id(agent: str, conversation_id: str) -> str:
    """Deterministic, filesystem-safe session key from (agent, conversation).

    Deterministic so the DB stays inventory, not source of truth: the key is
    recomputable from the pair alone. The charset is a portable safe-identifier
    set (also valid as the runtime's session id); the runtime re-coerces to the
    same set at its own boundary, so stored key and on-disk session agree.
    """
    raw = f"{agent}--{conversation_id}"
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "-", raw).strip("-._")
    return cleaned or "session"


@dataclass(frozen=True)
class SessionRecord:
    agent: str
    channel_id: str
    conversation_id: str
    kind: str  # KIND_THREAD | KIND_DM | KIND_CHANNEL
    runtime_session_id: str
    created_at: str  # ISO8601 UTC
    last_active: str


@dataclass(frozen=True)
class InteractionRecord:
    """A widget awaiting a click. ``token`` gates the callback; the conversation
    fields say where the click resumes."""

    interaction_id: str
    token: str
    agent: str
    channel_id: str
    conversation_id: str
    kind: str
    created_at: str


class InteractionStore(Protocol):
    """Pending widget interactions, keyed for one-shot consumption on click."""

    async def create_interaction(self, record: InteractionRecord) -> None: ...

    async def take_interaction(self, token: str) -> InteractionRecord | None:
        """Return and CONSUME the interaction for this token (one-shot); None if
        unknown/already used — so a replayed click can't fire twice."""
        ...


@dataclass(frozen=True)
class FormRecord:
    """A pending modal form (open_form). The button was posted; on click we
    rebuild the dialog from ``spec`` (opaque JSON — a serialized chat.Form), on
    submit we feed the values back into the conversation."""

    token: str
    agent: str
    channel_id: str
    conversation_id: str
    kind: str
    spec: str
    created_at: str


class FormStore(Protocol):
    """Pending forms. Unlike a one-shot interaction the token is READ on the
    open-click (to build the dialog) and only deleted on submit/cancel."""

    async def create_form(self, record: FormRecord) -> None: ...

    async def get_form(self, token: str) -> FormRecord | None: ...

    async def delete_form(self, token: str) -> None: ...


class AgentStore(Protocol):
    """Persistence for the agent registry (synced from profiles at boot)."""

    async def upsert_agent(self, info: AgentInfo) -> None: ...

    async def list_agents(self) -> list[AgentInfo]: ...


class SessionStore(Protocol):
    """Inventory of conversations -> runtime sessions, surviving restarts."""

    async def get_or_create(
        self, agent: str, channel_id: str, conversation_id: str, kind: str
    ) -> tuple[SessionRecord, bool]:
        """Returns (record, created); created=True on first sight of the
        conversation — flows use it to backfill thread context once."""
        ...

    async def touch(self, agent: str, conversation_id: str) -> None: ...

    async def list(self, agent: str | None = None) -> list[SessionRecord]: ...

    async def delete(self, agent: str, conversation_id: str) -> SessionRecord | None: ...

    async def get_by_runtime_session(self, runtime_session_id: str) -> SessionRecord | None:
        """Reverse lookup: which conversation a runtime session serves. Used to
        give a tool the ConversationRef of the turn it runs inside."""
        ...

    async def mark_processed(self, agent: str, post_id: str) -> bool:
        """True on first sight of the post for this agent; False on a replay
        (WS reconnects redeliver). Callers drop already-processed posts."""
        ...

    async def close(self) -> None: ...
