"""resolve_gateway (impi/gateways.py): the settings an agent's gateway is built
from, as the application maps them onto the neutral GatewayConfig."""

from impi.config import ImpiSettings
from impi.gateways import resolve_gateway


def _settings(**over) -> ImpiSettings:
    return ImpiSettings(dotenv_path="/dev/null", integrations_port=8423, **over)


def test_the_reaction_map_reaches_the_mattermost_gateway(monkeypatch) -> None:
    monkeypatch.setenv("AGENTS_REACTION_COMMANDS__ASSISTANT", "eyes=summarize")
    config = resolve_gateway(
        _settings(agent_name="assistant", gateway="mattermost", mattermost_token="tok", reaction_quote_limit=99),
        "assistant",
    )
    assert config is not None and config.kind == "mattermost"
    assert config.reaction_commands.by_emoji == {"eyes": "summarize"}
    assert config.reaction_commands.quote_limit == 99


def test_the_reaction_map_reaches_the_slack_gateway(monkeypatch) -> None:
    monkeypatch.setenv("AGENTS_REACTION_COMMANDS__ASSISTANT", "white_check_mark=done")
    config = resolve_gateway(
        _settings(agent_name="assistant", gateway="slack", slack_bot_token="xoxb", slack_app_token="xapp"),
        "assistant",
    )
    assert config is not None and config.kind == "slack"
    assert config.reaction_commands.by_emoji == {"white_check_mark": "done"}
