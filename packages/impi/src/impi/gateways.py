"""Resolve an agent's gateway config from ImpiSettings.

Transport CONSTRUCTION lives in ``crucible.gateways`` (``GatewayFactory``); this
module maps the application's per-agent settings onto the neutral ``GatewayConfig``
the factory consumes — the one place that knows the ``AGENTS_*`` env conventions
and which token(s) each transport needs.
"""

import logging

from crucible.gateways import GatewayConfig
from crucible.gateways.reactions import ReactionCommands
from impi.config import ImpiSettings

logger = logging.getLogger(__name__)


def resolve_gateways(settings: ImpiSettings, agent: str) -> list[GatewayConfig]:
    """The agent's gateway configs, one per gateway it is reached through, in
    the configured order (the first is its default). A gateway whose token(s)
    are unset is left out with a logged reason; an unknown kind passes through
    for the factory to reject."""
    configs: list[GatewayConfig] = []
    for kind in settings.gateways_for(agent):
        config = resolve_gateway_kind(settings, agent, kind)
        if config is not None:
            configs.append(config)
    return configs


def resolve_gateway(settings: ImpiSettings, agent: str) -> GatewayConfig | None:
    """The agent's default gateway's config, or None when it has none it can run."""
    configs = resolve_gateways(settings, agent)
    return configs[0] if configs else None


def resolve_gateway_kind(settings: ImpiSettings, agent: str, kind: str) -> GatewayConfig | None:
    """One gateway's config for the agent, or None (with a logged reason) if its
    required token(s) are unset."""
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
            reaction_commands=_reaction_commands(settings, agent),
            dm_replies=settings.dm_replies_for(agent),
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
            slack_command_prefix=settings.slack_command_prefix,
            slack_agent_sessions=settings.slack_agent_sessions_for(agent),
            dm_replies=settings.dm_replies_for(agent),
            max_post_chars=settings.mm_max_post_chars,
            reply_to_agents=settings.agents_reply_to_agents,
            reaction_commands=_reaction_commands(settings, agent),
        )
    if kind in ("ws", "http"):
        # No per-agent token gate: access is authorized by the callers' own
        # tokens on the hub (WS_SERVICE_TOKEN__* / HTTP_CALLER_TOKEN__*).
        return GatewayConfig(kind=kind, reply_to_agents=settings.agents_reply_to_agents)
    return GatewayConfig(kind=kind)


def _reaction_commands(settings: ImpiSettings, agent: str) -> ReactionCommands:
    return ReactionCommands(settings.reaction_commands_for(agent), settings.reaction_quote_limit)


def _key(agent: str) -> str:
    return agent.upper().replace("-", "_")
