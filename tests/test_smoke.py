"""Smoke tests: the package imports and settings load with defaults."""

import impi
from impi.config import Settings


def test_package_version():
    assert impi.__version__


def test_settings_defaults():
    settings = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert settings.pi_bin == "pi"
    assert settings.mattermost_url.startswith("http")


def test_mm_token_per_agent_resolution(monkeypatch, tmp_path):
    settings = Settings(_env_file=None, mattermost_token="fallback", dotenv_path="")  # pyright: ignore[reportCallIssue]
    # Specific env var wins; hyphens map to underscores.
    monkeypatch.setenv("AGENTS_MM_TOKEN__AGENT_BUILDER", "builder-token")
    assert settings.mm_token_for("agent-builder") == "builder-token"
    # The default agent falls back to MATTERMOST_TOKEN, others get nothing.
    assert settings.mm_token_for("assistant") == "fallback"
    assert settings.mm_token_for("developer") == ""


def test_enabled_agent_names_parsing():
    settings = Settings(_env_file=None, agents_enabled=" assistant, developer ")  # pyright: ignore[reportCallIssue]
    assert settings.enabled_agent_names() == ["assistant", "developer"]
    empty = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert empty.enabled_agent_names() is None


def test_tool_settings_grouped_view():
    settings = Settings(_env_file=None, tool_server_port=9999)  # pyright: ignore[reportCallIssue]
    assert settings.tools.server_port == 9999
    assert settings.tools.server_url == "http://127.0.0.1:9999"
    assert settings.tools.enabled is True
