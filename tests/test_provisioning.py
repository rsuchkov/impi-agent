"""Offline tests for the provisioning core: bot creation flow against a fake
Mattermost driver, profile scaffolding, and .env editing."""

import stat
from pathlib import Path

import pytest
import yaml

from impi.provisioning import (
    AGENT_NAME_RE,
    ProvisioningError,
    agent_env_key,
    provision_mm_bot,
    set_env_key,
    write_agent_profile,
)


class _Endpoint:
    """Records calls; each method resolves to a canned response."""

    def __init__(self, responses: dict) -> None:
        self.calls: list[tuple] = []
        self._responses = responses

    def __getattr__(self, method: str):
        async def call(*args, **kwargs):
            self.calls.append((method, args, kwargs))
            value = self._responses.get(method)
            if isinstance(value, Exception):
                raise value
            return value

        return call


class FakeDriver:
    def __init__(self, *, bots=None, users=None, teams=None) -> None:
        self.bots = _Endpoint(bots or {"create_bot": {"user_id": "bot-uid"}})
        self.users = _Endpoint(users or {"create_user_access_token": {"token": "bot-pat"}})
        self.teams = _Endpoint(
            teams
            or {
                "get_all_teams": [{"id": "team-1", "name": "main"}],
                "get_team_by_name": {"id": "team-2", "name": "wanted"},
                "add_team_member": {},
            }
        )
        self.logged_in = False

    async def login(self):
        self.logged_in = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


async def test_provision_bot_full_flow_first_team() -> None:
    driver = FakeDriver()
    creds = await provision_mm_bot(
        "http://mm:8065", "admin-pat", username="helper",
        display_name="Helper", driver=driver,
    )
    assert (creds.user_id, creds.username, creds.token) == ("bot-uid", "helper", "bot-pat")
    assert creds.team == "main"  # no team named -> first on the server
    assert driver.logged_in
    assert driver.bots.calls == [("create_bot", ("helper", "Helper", ""), {})]
    assert driver.users.calls == [("create_user_access_token", ("bot-uid", "impi agent token"), {})]
    assert ("add_team_member", ("team-1", "bot-uid"), {}) in driver.teams.calls


async def test_provision_bot_joins_named_team() -> None:
    driver = FakeDriver()
    creds = await provision_mm_bot(
        "http://mm:8065", "admin-pat", username="helper", team="wanted", driver=driver
    )
    assert creds.team == "wanted"
    assert ("add_team_member", ("team-2", "bot-uid"), {}) in driver.teams.calls


async def test_provision_bot_wraps_username_conflict() -> None:
    driver = FakeDriver(bots={"create_bot": RuntimeError("400 store.sql_bot.save")})
    with pytest.raises(ProvisioningError, match="helper"):
        await provision_mm_bot("http://mm:8065", "t", username="helper", driver=driver)


async def test_provision_bot_wraps_login_failure() -> None:
    class NoAuth(FakeDriver):
        async def login(self):
            raise RuntimeError("401")

    with pytest.raises(ProvisioningError, match="authenticate"):
        await provision_mm_bot("http://mm:8065", "bad", username="x", driver=NoAuth())


# --- write_agent_profile -----------------------------------------------------


def test_write_agent_profile_scaffolds_parseable_yaml(tmp_path: Path) -> None:
    profile_dir = write_agent_profile(
        tmp_path, name="news-bot", role="news: curator",
        display_name="News Bot", description='daily "digest"',
    )
    assert profile_dir == tmp_path / "agents" / "news-bot"
    data = yaml.safe_load((profile_dir / "agent.yaml").read_text())
    # Values with colons/quotes survive because they are JSON-encoded into YAML.
    assert data["name"] == "news-bot"
    assert data["role"] == "news: curator"
    assert data["description"] == 'daily "digest"'
    assert "read" in data["runtime"]["tools"]
    system = (profile_dir / ".pi" / "SYSTEM.md").read_text()
    assert "News Bot" in system


def test_write_agent_profile_honors_custom_system_prompt(tmp_path: Path) -> None:
    profile_dir = write_agent_profile(
        tmp_path, name="poet", role="poet", system_prompt="Ты поэт.\n"
    )
    assert (profile_dir / ".pi" / "SYSTEM.md").read_text() == "Ты поэт.\n"


def test_write_agent_profile_refuses_overwrite(tmp_path: Path) -> None:
    write_agent_profile(tmp_path, name="dup", role="r")
    with pytest.raises(ProvisioningError, match="already exists"):
        write_agent_profile(tmp_path, name="dup", role="r")


def test_write_agent_profile_rejects_bad_names(tmp_path: Path) -> None:
    for bad in ("UPPER", "with space", "-lead", "trail-", "unicode-ё", ""):
        assert not AGENT_NAME_RE.match(bad)
        with pytest.raises(ProvisioningError):
            write_agent_profile(tmp_path, name=bad, role="r")


# --- set_env_key ---------------------------------------------------------------


def test_set_env_key_creates_file_with_owner_only_mode(tmp_path: Path) -> None:
    env = tmp_path / "conf" / ".env"
    set_env_key(env, "A", "1")
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert "A=1" in env.read_text()


def test_set_env_key_updates_in_place_and_preserves_others(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("KEEP=untouched\nAGENTS_MM_TOKEN__X=old\n")
    set_env_key(env, "AGENTS_MM_TOKEN__X", "new")
    content = env.read_text()
    assert "KEEP=untouched" in content
    assert "AGENTS_MM_TOKEN__X=new" in content
    assert "old" not in content


def test_agent_env_key_matches_engine_lookup_convention() -> None:
    assert agent_env_key("greek-teacher") == "AGENTS_MM_TOKEN__GREEK_TEACHER"
    assert agent_env_key("x", "GATEWAY") == "AGENTS_GATEWAY__X"
    assert agent_env_key("a-b", "SLACK_BOT_TOKEN") == "AGENTS_SLACK_BOT_TOKEN__A_B"
