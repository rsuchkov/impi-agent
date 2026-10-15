"""What the scheduler needs from the engine around it.

Narrow verbs, so the scheduler never holds a flow, a runtime, a gateway or
the presence registry — the composition root implements them over whatever it
happens to have. Between this and the store, the scheduler's whole world is
ports and rows. The turn it starts goes through ``ports.engine_turn`` —
``TurnRequest`` / ``TurnDispatcher`` — which it shares with every other way
the engine opens a turn by itself.
"""

from typing import Protocol

from crucible.ports.agent.runtime import AgentResult


class PromptRunner(Protocol):
    """Runs a prompt with no conversation and no memory — the cheap, repeatable
    kind of scheduled work. The scheduler posts the result itself."""

    async def run_prompt(self, agent: str, text: str) -> AgentResult: ...


class Notifier(Protocol):
    """Putting words in a conversation on the scheduler's behalf.

    Two verbs, for the same reason ChatClient has both: ``deliver`` carries the
    agent's own prose from a memoryless run and is rendered as a reply, while
    ``announce`` is engine chrome — why a run did not happen — and goes out
    verbatim."""

    async def deliver(
        self, agent: str, *, channel_id: str, conversation_id: str, kind: str, text: str
    ) -> None: ...

    async def announce(
        self, agent: str, *, channel_id: str, conversation_id: str, kind: str, text: str
    ) -> None: ...
