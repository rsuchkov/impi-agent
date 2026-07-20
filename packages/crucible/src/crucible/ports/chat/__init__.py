"""Neutral chat vocabulary + ports. The ONLY shared language between gateways,
flows and stores — platform-specific fields never leak in here."""

from crucible.ports.chat.client import ChatClient
from crucible.ports.chat.flow import Flow, MessageSink
from crucible.ports.chat.gateway import AgentIdentity, Gateway
from crucible.ports.chat.types import (
    KIND_CHANNEL,
    KIND_DM,
    KIND_THREAD,
    Action,
    ConversationRef,
    IncomingMessage,
    UserProfile,
)
from crucible.ports.chat.widgets import WidgetPoster, WidgetService

__all__ = [
    "ChatClient",
    "Flow",
    "MessageSink",
    "Gateway",
    "AgentIdentity",
    "Action",
    "WidgetPoster",
    "WidgetService",
    "ConversationRef",
    "IncomingMessage",
    "UserProfile",
    "KIND_THREAD",
    "KIND_DM",
    "KIND_CHANNEL",
]
