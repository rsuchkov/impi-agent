"""Offline tests for the `impi` CLI: non-interactive `agent add` against temp
dirs with the network provisioning stubbed out."""

from pathlib import Path

import pytest

import impi.cli as cli
from impi.provisioning import BotCredentials, ProvisioningError


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point every settings source at the temp dir so tests never read the
    developer's real .env or environment."""
    env_file = tmp_path / ".env"
    env_file.write_text("GATEWAY=mattermost\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DOTENV_PATH", str(env_file))
    monkeypatch.setenv("AGENTS_PATH", str(tmp_path / "profiles"))
    for var in ("MATTERMOST_URL", "TOOL_CREATE_AGENT_ADMIN_TOKEN", "GATEWAY"):
        monkeypatch.delenv(var, raising=False)
    return env_file


def _read_env(env_file: Path) -> str:
    return env_file.read_text()


async def _fake_provision(url, admin_token, *, username, **kwargs):
    return BotCredentials(user_id="uid", username=username, token="minted-token", team="t")


def test_agent_add_provisions_bot_and_writes_everything(tmp_path, monkeypatch, _isolated_env):
    calls = {}

    async def spy(url, admin_token, *, username, **kwargs):
        calls["url"], calls["admin"], calls["username"] = url, admin_token, username
        return await _fake_provision(url, admin_token, username=username, **kwargs)

    monkeypatch.setattr(cli.prov, "provision_mm_bot", spy)
    rc = cli.main([
        "agent", "add", "--name", "helper", "--role", "helps out",
        "--mm-url", "http://mm:8065", "--admin-token", "admin-pat", "--yes",
    ])
    assert rc == 0
    assert calls == {"url": "http://mm:8065", "admin": "admin-pat", "username": "helper"}
    profile = tmp_path / "profiles" / "agents" / "helper" / "agent.yaml"
    assert profile.exists()
    content = _read_env(_isolated_env)
    assert "AGENTS_MM_TOKEN__HELPER=minted-token" in content
    assert "AGENTS_GATEWAY__HELPER" not in content  # matches the global gateway


def test_agent_add_with_manual_bot_token_skips_provisioning(tmp_path, monkeypatch, _isolated_env):
    async def boom(*a, **k):
        raise AssertionError("must not auto-provision when --bot-token is given")

    monkeypatch.setattr(cli.prov, "provision_mm_bot", boom)
    rc = cli.main([
        "agent", "add", "--name", "manual", "--role", "r",
        "--bot-token", "pasted-token", "--yes",
    ])
    assert rc == 0
    assert "AGENTS_MM_TOKEN__MANUAL=pasted-token" in _read_env(_isolated_env)


def test_agent_add_writes_gateway_override_when_it_differs(tmp_path, monkeypatch, _isolated_env):
    _isolated_env.write_text("GATEWAY=slack\n")
    monkeypatch.setattr(cli.prov, "provision_mm_bot", _fake_provision)
    rc = cli.main([
        "agent", "add", "--name", "mm-agent", "--role", "r",
        "--gateway", "mattermost", "--admin-token", "pat", "--yes",
    ])
    assert rc == 0
    content = _read_env(_isolated_env)
    assert "AGENTS_MM_TOKEN__MM_AGENT=minted-token" in content
    assert "AGENTS_GATEWAY__MM_AGENT=mattermost" in content


def test_agent_add_slack_writes_token_pair(tmp_path, _isolated_env):
    rc = cli.main([
        "agent", "add", "--name", "slacky", "--role", "r", "--gateway", "slack",
        "--slack-bot-token", "xoxb-1", "--slack-app-token", "xapp-1", "--yes",
    ])
    assert rc == 0
    content = _read_env(_isolated_env)
    assert "AGENTS_SLACK_BOT_TOKEN__SLACKY=xoxb-1" in content
    assert "AGENTS_SLACK_APP_TOKEN__SLACKY=xapp-1" in content
    assert "AGENTS_GATEWAY__SLACKY=slack" in content  # global default is mattermost


def test_agent_add_requires_credentials_in_yes_mode(_isolated_env):
    rc = cli.main(["agent", "add", "--name", "x", "--role", "r", "--yes"])
    assert rc == 2
    assert "AGENTS_MM_TOKEN__X" not in _read_env(_isolated_env)


def test_agent_add_rejects_bad_slug(_isolated_env):
    rc = cli.main(["agent", "add", "--name", "Bad Name", "--role", "r",
                   "--bot-token", "t", "--yes"])
    assert rc == 2


def test_agent_add_surfaces_provisioning_error(tmp_path, monkeypatch, _isolated_env):
    async def fail(*a, **k):
        raise ProvisioningError("username taken")

    monkeypatch.setattr(cli.prov, "provision_mm_bot", fail)
    rc = cli.main(["agent", "add", "--name", "dup", "--role", "r",
                   "--admin-token", "pat", "--yes"])
    assert rc == 2
    assert "AGENTS_MM_TOKEN__DUP" not in _read_env(_isolated_env)


def test_provision_support_writes_support_token(tmp_path, monkeypatch, _isolated_env):
    monkeypatch.setattr(cli.prov, "provision_mm_bot", _fake_provision)
    rc = cli.main(["provision", "support", "--admin-token", "pat", "--yes"])
    assert rc == 0
    assert "AGENTS_MM_TOKEN__SUPPORT=minted-token" in _read_env(_isolated_env)


def test_mm_bootstrap_token_prints_only_the_token(monkeypatch, capsys, _isolated_env):
    async def fake_pat(url, login_id, password, **kwargs):
        assert (url, login_id, password) == ("http://mm:8065", "admin", "s3cret")
        return "the-pat", "admin-uid"

    monkeypatch.setattr(cli.prov, "mm_admin_pat", fake_pat)
    monkeypatch.setattr(cli.sys, "stdin", __import__("io").StringIO("s3cret\n"))
    rc = cli.main(["mm", "bootstrap-token", "--url", "http://mm:8065",
                   "--login-id", "admin", "--password-stdin"])
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out == "the-pat\n"  # stdout is machine-readable: token only
    assert "admin-uid" in captured.err


def test_agent_add_ws_writes_only_gateway_override(tmp_path, _isolated_env):
    rc = cli.main(["agent", "add", "--name", "wsbot", "--role", "r",
                   "--gateway", "ws", "--yes"])
    assert rc == 0
    content = _read_env(_isolated_env)
    assert "AGENTS_GATEWAY__WSBOT=ws" in content
    assert "AGENTS_MM_TOKEN__WSBOT" not in content  # no per-agent ws credentials
    assert (tmp_path / "profiles" / "agents" / "wsbot" / "agent.yaml").exists()


def test_ws_add_service_registers_token_and_allowlist(capsys, _isolated_env):
    rc = cli.main(["ws", "add-service", "my-svc", "--agents", "wsbot,scribe"])
    assert rc == 0
    content = _read_env(_isolated_env)
    assert "WS_SERVICE_TOKEN__MY_SVC=" in content
    assert "WS_SERVICE_AGENTS__MY_SVC=" in content and "wsbot,scribe" in content
    token_line = next(
        line for line in content.splitlines()
        if line.startswith("WS_SERVICE_TOKEN__MY_SVC=")
    )
    token = token_line.split("=", 1)[1]
    assert len(token) == 48  # token_hex(24)
    assert token in capsys.readouterr().out  # printed once for the operator


def test_ws_add_service_rejects_bad_names(_isolated_env):
    assert cli.main(["ws", "add-service", "Bad Name"]) == 2
