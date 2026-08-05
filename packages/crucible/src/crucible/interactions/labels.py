"""Human-readable answers: platform ids -> names.

A user/channel picker hands back an id (``u4x…``, ``C09…``) — useless to an
agent reading its next prompt. These helpers resolve it through the agent's own
chat client and render ``@name (id)`` / ``~name (id)``: the name is what the
model reasons about, the id is what it passes to a tool afterwards.

Best-effort by design — an unknown id or a failing lookup degrades to the raw
value rather than breaking the turn.
"""

import logging

from crucible.ports.chat.client import ChatClient
from crucible.ports.chat.types import (
    CHANNEL_FIELD_TYPES,
    PICK_FIELD_BY_KIND,
    USER_FIELD_TYPES,
)

logger = logging.getLogger(__name__)

_SEPARATOR = ", "


async def humanize(value: str, field_type: str, chat: ChatClient | None) -> str:
    """Render one picked value (or a comma-separated list of them) for the agent.
    Anything that isn't a picker's answer is returned untouched."""
    if not value or chat is None:
        return value
    if field_type in USER_FIELD_TYPES:
        return await _each(value, chat, _user)
    if field_type in CHANNEL_FIELD_TYPES:
        return await _each(value, chat, _channel)
    return value


async def humanize_pick(value: str, kind: str, chat: ChatClient | None) -> str:
    """The same, for a widget pick — keyed by the Action kind instead."""
    field_type = PICK_FIELD_BY_KIND.get(kind, "")
    return await humanize(value, field_type, chat) if field_type else value


async def _each(value: str, chat: ChatClient, render) -> str:
    ids = [part.strip() for part in value.split(",") if part.strip()]
    return _SEPARATOR.join([await render(i, chat) for i in ids])


async def _user(user_id: str, chat: ChatClient) -> str:
    try:
        profile = await chat.get_user_profile(user_id)
    except Exception:
        logger.warning("could not resolve user %s for a form answer", user_id, exc_info=True)
        return user_id
    return f"@{profile.username} ({user_id})" if profile and profile.username else user_id


async def _channel(channel_id: str, chat: ChatClient) -> str:
    try:
        name = await chat.resolve_channel(channel_id)
    except Exception:
        logger.warning("could not resolve channel %s for a form answer", channel_id, exc_info=True)
        return channel_id
    return f"~{name} ({channel_id})" if name else channel_id
