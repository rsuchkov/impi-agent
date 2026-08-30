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


# --- impi task ----------------------------------------------------------------


def _task_db(tmp_path, monkeypatch):
    """A throwaway engine database with one conversation the CLI can schedule in."""
    from crucible.ports.chat.types import KIND_DM
    from crucible.store.sessions import SqliteSessionStore

    monkeypatch.setenv("DOTENV_PATH", "/dev/null")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SCHEDULER_TIMEZONE", "Europe/Belgrade")
    store = SqliteSessionStore(tmp_path / "impi.db")
    store.get_or_create_sync("assistant", "ch1", "dm1", KIND_DM, "u1")
    store.close_sync()


def test_task_add_echoes_the_next_fire_times(tmp_path, monkeypatch, capsys) -> None:
    _task_db(tmp_path, monkeypatch)

    code = cli.main([
        "task", "add", "--agent", "assistant", "--conversation", "dm1",
        "--name", "digest", "--prompt", "summarize", "--schedule", "0 9 * * 1-5",
    ])

    out = capsys.readouterr().out
    assert code == 0
    assert "digest" in out and "(Europe/Belgrade)" in out
    assert out.count("Europe/Belgrade") == 3  # the next three, read back


def test_task_add_names_the_conversations_it_knows(tmp_path, monkeypatch, capsys) -> None:
    _task_db(tmp_path, monkeypatch)

    code = cli.main([
        "task", "add", "--agent", "assistant", "--conversation", "nowhere",
        "--name", "x", "--prompt", "y", "--schedule", "every 1h",
    ])

    assert code == 1
    assert "known: dm1" in capsys.readouterr().err


def test_a_schedule_that_does_not_parse_is_a_message_not_a_traceback(
    tmp_path, monkeypatch, capsys
) -> None:
    _task_db(tmp_path, monkeypatch)

    code = cli.main([
        "task", "add", "--agent", "assistant", "--conversation", "dm1",
        "--name", "x", "--prompt", "y", "--schedule", "every 5s",
    ])

    assert code == 1
    assert "at least 60s" in capsys.readouterr().err


def test_task_run_now_only_asks_the_engine(tmp_path, monkeypatch, capsys) -> None:
    # The CLI container has no gateways: it moves the schedule, the engine runs it.
    from crucible.store.sessions import SqliteSessionStore

    _task_db(tmp_path, monkeypatch)
    cli.main([
        "task", "add", "--agent", "assistant", "--conversation", "dm1",
        "--name", "digest", "--prompt", "p", "--schedule", "every 1h",
    ])
    capsys.readouterr()

    assert cli.main(["task", "run-now", "digest"]) == 0

    store = SqliteSessionStore(tmp_path / "impi.db")
    try:
        task = store.find_task_sync("assistant", "digest")
        assert task is not None and task.due_at is not None
        assert store.list_runs_sync(task.id) == []  # nothing ran here
    finally:
        store.close_sync()


def test_task_status_tells_an_untouched_database_apart(tmp_path, monkeypatch, capsys) -> None:
    _task_db(tmp_path, monkeypatch)

    code = cli.main(["task", "status"])

    assert code == 1
    assert "has ever ticked" in capsys.readouterr().out


def test_task_status_says_off_when_the_scheduler_is_disabled(
    tmp_path, monkeypatch, capsys
) -> None:
    _task_db(tmp_path, monkeypatch)
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")

    code = cli.main(["task", "status"])

    assert code == 0  # off on purpose is not a failure
    assert "turned off" in capsys.readouterr().out


def test_an_unknown_task_is_reported_once_not_raised(tmp_path, monkeypatch, capsys) -> None:
    _task_db(tmp_path, monkeypatch)

    assert cli.main(["task", "show", "ghost"]) == 1
    assert "no task 'ghost'" in capsys.readouterr().err


def test_sessions_read_the_database_the_engine_writes(
    tmp_path, monkeypatch, capsys
) -> None:
    # The library's own entry point resolves crucible's default filename; run
    # against an impi deployment it opens a file nobody writes.
    _task_db(tmp_path, monkeypatch)

    assert cli.main(["sessions", "list"]) == 0

    assert "dm1" in capsys.readouterr().out
    assert not (tmp_path / "agent.db").exists()


def test_deleting_a_task_with_no_terminal_to_ask_refuses_instead_of_raising(
    tmp_path, monkeypatch, capsys
) -> None:
    _task_db(tmp_path, monkeypatch)
    cli.main([
        "task", "add", "--agent", "assistant", "--conversation", "dm1",
        "--name", "digest", "--prompt", "summarize", "--schedule", "every 1h",
    ])
    monkeypatch.setattr("builtins.input", _no_terminal)

    code = cli.main(["task", "rm", "digest"])

    assert code == 1
    assert "--yes" in capsys.readouterr().out
    assert cli.main(["task", "show", "digest"]) == 0  # still there


def _no_terminal(_prompt: str = "") -> str:
    raise EOFError


def test_skill_install_bundled_resolves_a_shipped_skill(tmp_path, _isolated_env):
    """--bundled names a skill that ships in the image, so nobody has to know
    where inside the package it lives."""
    library = tmp_path / "skills"
    rc = cli.main([
        "skill", "install", "--bundled", "web-browsing",
        "--skills-dir", str(library), "--yes",
    ])
    assert rc == 0
    assert (library / "web-browsing" / "SKILL.md").is_file()


def test_skill_install_bundled_names_what_is_available(capsys, tmp_path, _isolated_env):
    """A typo answers with the list rather than a path the operator never typed."""
    rc = cli.main([
        "skill", "install", "--bundled", "no-such-skill",
        "--skills-dir", str(tmp_path / "skills"), "--yes",
    ])
    assert rc == 2
    assert "web-browsing" in capsys.readouterr().err


def test_skill_install_bundled_refuses_a_name_that_is_a_path(capsys, tmp_path, _isolated_env):
    """The tool behind this takes the name from a model, so the resolved
    directory is checked back against its parent — joining a caller's string
    onto a path is how `../something` becomes a skill source.

    The traversal used here lands on a real skill directory that is deliberately
    NOT part of the bundled set (one of the support agent's own), so the refusal
    can only come from the parent check: a target with no SKILL.md would be
    turned away by the existence test alone and prove nothing."""
    rc = cli.main([
        "skill", "install", "--bundled",
        "../builtin_agents/agents/support/.pi/skills/ward",
        "--skills-dir", str(tmp_path / "skills"), "--yes",
    ])
    assert rc == 2
    assert "no bundled skill" in capsys.readouterr().err
    assert not (tmp_path / "skills" / "ward").exists()


# --- the CLI reads the same profiles the engine does -------------------------
#
# Both commands below construct a profile store of their own, and both forgot to
# give it the skill library. The store works perfectly until a profile happens to
# name a `registry:` skill — so a deployment that followed the documented advice
# (`impi skill install --bundled`, then `impi skill assign`) is exactly the one
# that broke, and the error told the operator to install what they had installed.
#
# These go through the whole command rather than through the renderer, because
# the renderer was never the part that was wrong.


def _agent_with_library_skill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    profiles = tmp_path / "profiles" / "agents" / "assistant"
    profiles.mkdir(parents=True)
    (profiles / "agent.yaml").write_text(
        "name: assistant\nrole: probe\nruntime:\n  tools: [read]\n"
        "  skills:\n    - registry:web-browsing\n"
    )
    library = tmp_path / "skills"
    (library / "web-browsing").mkdir(parents=True)
    (library / "web-browsing" / "SKILL.md").write_text("---\nname: web-browsing\n---\n")
    monkeypatch.setenv("SKILLS_PATH", str(library))
    return library


def test_agent_list_resolves_a_library_skill(tmp_path, monkeypatch, _isolated_env, capsys):
    _agent_with_library_skill(tmp_path, monkeypatch)

    assert cli.main(["agent", "list"]) == 0
    assert "assistant" in capsys.readouterr().out


def test_agent_render_puts_the_library_path_into_the_compose_file(
    tmp_path, monkeypatch, _isolated_env
):
    library = _agent_with_library_skill(tmp_path, monkeypatch)
    conf = tmp_path / "conf"

    assert cli.main(["agent", "render", "--conf-dir", str(conf)]) == 0

    rendered = (conf / "agents.compose.yaml").read_text()
    assert "agent-assistant" in rendered
    # The skill has to have resolved to the library, since that is the directory
    # the agent's container mounts; a bare `registry:` name would reach the
    # runtime as something it cannot open.
    assert str(library) in rendered or "/app/skills" in rendered


def test_a_missing_library_skill_still_says_so(tmp_path, monkeypatch, _isolated_env, capsys):
    """The other half of the same message: wiring the library back must not hide
    a skill that genuinely is not installed."""
    profiles = tmp_path / "profiles" / "agents" / "assistant"
    profiles.mkdir(parents=True)
    (profiles / "agent.yaml").write_text(
        "name: assistant\nrole: probe\nruntime:\n  skills:\n    - registry:absent\n"
    )
    monkeypatch.setenv("SKILLS_PATH", str(tmp_path / "skills"))

    assert cli.main(["agent", "list"]) == 2
    assert "unknown library skill 'absent'" in capsys.readouterr().err
