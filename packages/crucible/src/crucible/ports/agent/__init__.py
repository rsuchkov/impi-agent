"""Ports (Protocols) between flows and a concrete agent runtime."""

from crucible.ports.agent.errors import (
    INTERNAL_ERROR_MESSAGE,
    LLM_FALLBACK_MESSAGE,
    AgentError,
    AgentTimeout,
)
from crucible.ports.agent.runtime import (
    AgentEvent,
    AgentProfile,
    AgentResult,
    AgentRuntime,
    EventCallback,
    PromptImage,
)
from crucible.ports.agent.spec import AgentSpec
from crucible.ports.agent.ui import UiBridge, UiOutcome, UiRequest

__all__ = [
    "AgentError",
    "AgentTimeout",
    "AgentEvent",
    "AgentProfile",
    "AgentResult",
    "AgentRuntime",
    "AgentSpec",
    "EventCallback",
    "LLM_FALLBACK_MESSAGE",
    "INTERNAL_ERROR_MESSAGE",
    "PromptImage",
    "UiBridge",
    "UiRequest",
    "UiOutcome",
]
