"""What the scheduler needs from the engine around it.

Three narrow verbs, so the scheduler never holds a flow, a runtime, a gateway or
the presence registry — the composition root implements them over whatever it
happens to have. Between this and the store, the scheduler's whole world is
ports and rows.
"""

from dataclasses import dataclass
from typing import Protocol

from crucible.ports.agent.runtime import AgentResult
from crucible.ports.chat.flow import TurnOutcome


class DispatchError(Exception):
    """The turn could not be started at all — the agent is not live, or the
    conversation cannot be addressed. Distinct from a turn that ran and failed."""


@dataclass(frozen=True)
class TurnRequest:
    """One scheduled turn, addressed the way a click or a command is.

    ``message_id`` is the run's own id, so the flow's replay dedup never mistakes
    two runs for one; ``username`` is who the prompt appears to come from, and is
    never empty — the prompt envelope would render an anonymous ``[]:`` line."""

    agent: str
    channel_id: str
    conversation_id: str
    kind: str
    text: str
    message_id: str
    user_id: str = ""
    username: str = "scheduler"


class TurnDispatcher(Protocol):
    """Runs a scheduled turn in the task's own conversation, with the agent's
    memory, and reports how it ended. Raises DispatchError if it never started."""

    async def run_turn(self, request: TurnRequest) -> TurnOutcome: ...


class PromptRunner(Protocol):
    """Runs a prompt with no conversation and no memory — the cheap, repeatable
    kind of scheduled work. The scheduler posts the result itself."""

    async def run_prompt(self, agent: str, text: str) -> AgentResult: ...


class Notifier(Protocol):
    """Where the scheduler speaks in its own voice: the output of a memoryless
    run, and the failures a turn could not report because it never happened."""

    async def post(
        self, agent: str, *, channel_id: str, conversation_id: str, kind: str, text: str
    ) -> None: ...
