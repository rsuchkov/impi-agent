"""Slack adapter — the only layer that knows the Slack API (slack_bolt/slack_sdk).

events.py    — pure event normalization into neutral chat types
rendering.py — Block Kit rendering + the widget token round-trip
formatter.py — Markdown -> mrkdwn conversion for outgoing agent prose
client.py    — ChatClient + ChatAdmin over the AsyncWebClient
gateway.py   — Socket Mode loop + respond decision; interactive callbacks routed
               to the neutral InteractionDispatcher (no HTTP receiver needed)
"""

from crucible.gateways.slack.client import SlackChatClient
from crucible.gateways.slack.gateway import (
    DEFAULT_COMMAND_SHORTCUT_PREFIX,
    SlackGateway,
)

# Appended to a Slack agent's system prompt. The gateway converts outgoing
# Markdown to mrkdwn itself (formatter.py); the model only needs to write plain
# Markdown and avoid the one construct that degrades lossily — tables.
PROMPT_HINT = (
    "You are connected through Slack. Write plain standard Markdown — it is "
    "converted to Slack formatting automatically. Prefer bullet or numbered "
    "lists over Markdown tables (tables get flattened into lists)."
)

__all__ = [
    "DEFAULT_COMMAND_SHORTCUT_PREFIX",
    "PROMPT_HINT",
    "SlackChatClient",
    "SlackGateway",
]
