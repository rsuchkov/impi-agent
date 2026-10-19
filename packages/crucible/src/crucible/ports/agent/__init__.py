"""Ports (Protocols) between flows and a concrete agent runtime."""

from crucible.ports.agent.captions import CallCaptions, ChainedCaptions
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
    TEXT_PROGRESS,
    TOOL_FINISHED,
    TOOL_STARTED,
    TextProgress,
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
from crucible.ports.agent.spec import THINKING_LEVELS, AgentSpec, check_thinking_level
from crucible.ports.agent.ui import UiBridge, UiOutcome, UiRequest

__all__ = [
    "THINKING_LEVELS",
    "check_thinking_level",
    "TEXT_PROGRESS",
    "CallCaptions",
    "ChainedCaptions",
    "TextProgress",
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
