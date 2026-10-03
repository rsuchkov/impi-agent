"""Ports (Protocols) between flows and a concrete agent runtime."""

from crucible.ports.agent.errors import (
    INTERNAL_ERROR_MESSAGE,
    LLM_FALLBACK_MESSAGE,
    AgentBusy,
    AgentError,
    AgentTimeout,
    AgentUnavailable,
    message_for,
    notice_for,
)
from crucible.ports.agent.events import (
    TOOL_FINISHED,
    TOOL_STARTED,
    ToolFinished,
    ToolStarted,
)
from crucible.ports.agent.runtime import (
    AgentEvent,
    AgentProfile,
    AgentResult,
    AgentRuntime,
    EventCallback,
    PromptImage,
    RuntimeControl,
    RuntimeStats,
    SessionProofs,
    TurnClock,
)
from crucible.ports.agent.spec import AgentSpec
from crucible.ports.agent.ui import UiBridge, UiOutcome, UiRequest

__all__ = [
    "AgentBusy",
    "AgentError",
    "AgentTimeout",
    "AgentUnavailable",
    "message_for",
    "notice_for",
    "AgentEvent",
    "AgentProfile",
    "AgentResult",
    "AgentRuntime",
    "AgentSpec",
    "EventCallback",
    "TOOL_FINISHED",
    "TOOL_STARTED",
    "ToolFinished",
    "ToolStarted",
    "LLM_FALLBACK_MESSAGE",
    "INTERNAL_ERROR_MESSAGE",
    "PromptImage",
    "RuntimeControl",
    "RuntimeStats",
    "SessionProofs",
    "TurnClock",
    "UiBridge",
    "UiRequest",
    "UiOutcome",
]
