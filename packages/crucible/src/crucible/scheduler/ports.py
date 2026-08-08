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
