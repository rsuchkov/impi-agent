"""Resolve an agent's gateway config from ImpiSettings.

Transport CONSTRUCTION lives in ``crucible.gateways`` (``GatewayFactory``); this
module maps the application's per-agent settings onto the neutral ``GatewayConfig``
the factory consumes — the one place that knows the ``AGENTS_*`` env conventions
and which token(s) each transport needs.
"""

import logging

from crucible.gateways import GatewayConfig
from impi.config import ImpiSettings

logger = logging.getLogger(__name__)


def resolve_gateway(settings: ImpiSettings, agent: str) -> GatewayConfig | None:
    """The agent's gateway config, or None (with a logged reason) if its required
    token(s) are unset. An unknown gateway kind passes through for the factory to
    reject."""
    kind = settings.gateway_for(agent)
    if kind == "mattermost":
        token = settings.mm_token_for(agent)
        if not token:
            logger.warning(
                "agent %s: gateway=mattermost but no token (set AGENTS_MM_TOKEN__%s) — skipping",
                agent, _key(agent),
            )
            return None
        return GatewayConfig(
            kind="mattermost",
            mattermost_url=settings.mattermost_url,
            mm_token=token,
            max_post_chars=settings.mm_max_post_chars,
            reply_to_agents=settings.agents_reply_to_agents,
        )
    if kind == "slack":
        bot, app_token = settings.slack_tokens_for(agent)
        if not (bot and app_token):
            logger.warning(
                "agent %s: gateway=slack but tokens missing "
                "(set AGENTS_SLACK_BOT_TOKEN__%s / AGENTS_SLACK_APP_TOKEN__%s) — skipping",
                agent, _key(agent), _key(agent),
            )
            return None
        return GatewayConfig(
            kind="slack",
            slack_bot_token=bot,
            slack_app_token=app_token,
            max_post_chars=settings.mm_max_post_chars,
            reply_to_agents=settings.agents_reply_to_agents,
        )
    return GatewayConfig(kind=kind)


def _key(agent: str) -> str:
    return agent.upper().replace("-", "_")
