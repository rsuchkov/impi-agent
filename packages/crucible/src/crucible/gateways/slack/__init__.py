"""Slack adapter — the only layer that knows the Slack API (slack_bolt/slack_sdk).

events.py    — pure event normalization into neutral chat types
rendering.py — Block Kit rendering + the widget token round-trip
client.py    — ChatClient + ChatAdmin over the AsyncWebClient
gateway.py   — Socket Mode loop + respond decision; interactive callbacks routed
               to the neutral InteractionDispatcher (no HTTP receiver needed)
"""

from crucible.gateways.slack.client import SlackChatClient
from crucible.gateways.slack.gateway import SlackGateway

# Appended to a Slack agent's system prompt so it formats replies for Slack (whose
# mrkdwn differs from Markdown). See app wiring: GatewayHandle.prompt_hint.
PROMPT_HINT = (
    "You are connected through Slack. Format replies as Slack mrkdwn, NOT Markdown: "
    "*bold* (single asterisks), _italic_, ~strike~, `code`, ```code blocks```, "
    "<https://example.com|link text> for links, and lines starting with > for quotes. "
    "Mention a user with <@USER_ID>. Do NOT use **bold**, [text](url), or # headings."
)

__all__ = ["PROMPT_HINT", "SlackChatClient", "SlackGateway"]
