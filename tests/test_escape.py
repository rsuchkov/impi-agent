"""`impi escape` behind the scenes (impi/escape.py, `impi agent argv`): the
agent's pi as the engine would spawn it, translated to the operator's host.

Everything here is offline: profiles in a temp directory, settings built by
hand, the CLI driven through `main`.
"""

from pathlib import Path

import pytest

import impi.cli as cli
from crucible.profiles import FsProfileStore
from impi.config import ImpiSettings
from impi.escape import (
    ESCAPE_NOTE,
    EscapeError,
    Plan,
    build_plan,
    map_path,
    parse_map,
    render_nul,
    render_text,
)
from impi.profiles import IMPI_ROOT

AGENT_YAML = """\
name: assistant
role: personal-assistant
runtime:
  provider: openai-codex
  model: gpt-5.5
  tools: [read, bash, send_file]
  skills: [own, "registry:web-browsing"]
"""


def _dirs(tmp_path: Path) -> tuple[Path, Path]:
    """A user agent with one skill of its own and one from the library."""
    agents = tmp_path / "agents-dir"
    profile = agents / "agents" / "assistant"
    (profile / ".pi" / "skills" / "own").mkdir(parents=True)
    (profile / ".pi" / "skills" / "own" / "SKILL.md").write_text("---\nname: own\n---\n")
    (profile / "agent.yaml").write_text(AGENT_YAML, encoding="utf-8")
    library = tmp_path / "skills"
    (library / "web-browsing").mkdir(parents=True)
    (library / "web-browsing" / "SKILL.md").write_text("---\nname: web-browsing\n---\n")
    return agents, library


def _settings(tmp_path: Path, **over: object) -> ImpiSettings:
    agents, library = _dirs(tmp_path)
    values: dict[str, object] = {
        "dotenv_path": str(tmp_path / "no-such.env"),
        "agents_path": str(agents),
        "skills_path": str(library),
        "data_dir": str(tmp_path / "data"),
        "mattermost_url": "http://localhost:8065",
        "mattermost_token": "chat-token",
    }
    values.update(over)
    return ImpiSettings(_env_file=None, **values)  # pyright: ignore[reportCallIssue]


def _spec(settings: ImpiSettings):
    library = Path(settings.skills_path)
    return FsProfileStore(settings.agents_path, library=lambda name: library / name).get("assistant")


# --- paths -------------------------------------------------------------------------


def test_the_longest_prefix_wins_and_an_uncovered_path_is_refused() -> None:
    maps = [parse_map("/app=/home/op/.impi/repo"), parse_map("/app/agents=/home/op/agents")]
    assert map_path("/app/agents/agents/x", maps) == "/home/op/agents/agents/x"
    assert map_path("/app/packages/impi", maps) == "/home/op/.impi/repo/packages/impi"
    assert map_path("/app", maps) == "/home/op/.impi/repo"
    with pytest.raises(EscapeError, match="not reachable from the host"):
        map_path("/opt/elsewhere", maps)
    # No maps at all: a checkout, where the paths are already the host's.
    assert map_path("/opt/elsewhere", []) == "/opt/elsewhere"


def test_a_map_is_two_absolute_paths() -> None:
    for bad in ("/app", "/app=", "=/x", "app=/x", "/app=x"):
        with pytest.raises(EscapeError):
            parse_map(bad)
    assert parse_map("/app/=/x/") == ("/app", "/x")


def test_the_plan_translates_every_path_it_hands_over(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    agents, library = tmp_path / "agents-dir", tmp_path / "skills"
    maps = [parse_map(f"{agents}=/host/agents"), parse_map(f"{library}=/host/skills")]
    plan = build_plan(_spec(settings), settings, engine_owned=False, agents_path=str(agents), maps=maps)

    assert plan.cwd == "/host/agents/agents/assistant"
    skills = [plan.argv[i + 1] for i, flag in enumerate(plan.argv) if flag == "--skill"]
    assert skills == ["/host/agents/agents/assistant/.pi/skills/own", "/host/skills/web-browsing"]
    # A library the host cannot see is an error up front, not a pi that starts
    # without half its skills.
    with pytest.raises(EscapeError, match="not reachable"):
        build_plan(_spec(settings), settings, engine_owned=False, agents_path=str(agents), maps=maps[:1])


# --- env ---------------------------------------------------------------------------


def test_the_env_is_the_models_and_nothing_else(tmp_path: Path) -> None:
    """The engine grants a process tool tokens, chat tokens, the broker's
    variables. On the host every one of those would be an identity presented
    from outside a container — so only what names the model crosses over."""
    settings = _settings(
        tmp_path,
        llm_base_url="http://llm.invalid/v1",
        llm_api_key="k3y",
        llm_model="m",
        llm_verify_ssl=False,
        tool_create_agent_admin_token="admin-token",
    )
    plan = build_plan(_spec(settings), settings, engine_owned=False, agents_path=settings.agents_path)
    assert plan.env == {
        "LLM_BASE_URL": "http://llm.invalid/v1",
        "LLM_API_KEY": "k3y",
        "LLM_MODEL": "m",
        "NODE_TLS_REJECT_UNAUTHORIZED": "0",
    }
    assert not any("TOKEN" in name or "SECRET" in name or "MATTERMOST" in name for name in plan.env)


def test_an_engine_owned_agent_gets_the_engines_two_paths_in_host_terms(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = str(IMPI_ROOT)
    maps = [parse_map(f"{root}=/host/repo"), parse_map(f"{settings.agents_path}=/host/agents"),
            parse_map(f"{settings.skills_path}=/host/skills")]
    plan = build_plan(_spec(settings), settings, engine_owned=True, agents_path=settings.agents_path, maps=maps)
    assert plan.env["AGENTS_PATH"] == "/host/agents"
    assert plan.env["IMPI_ROOT"] == "/host/repo"
    without = build_plan(_spec(settings), settings, engine_owned=False, agents_path=settings.agents_path, maps=maps)
    assert "IMPI_ROOT" not in without.env and "AGENTS_PATH" not in without.env


# --- argv --------------------------------------------------------------------------


def _flag(argv: tuple[str, ...], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def test_the_note_is_appended_and_tools_can_be_replaced_for_one_run(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    plan = build_plan(_spec(settings), settings, engine_owned=False, agents_path=settings.agents_path)
    assert _flag(plan.argv, "--append-system-prompt") == ESCAPE_NOTE
    # send_file is the engine's: without the engine there is nobody to serve it.
    assert _flag(plan.argv, "--tools") == "read,bash"
    assert "--mode" not in plan.argv and "-e" not in plan.argv and "--session-id" not in plan.argv

    narrowed = build_plan(
        _spec(settings), settings, engine_owned=False, agents_path=settings.agents_path,
        tools=["read"], extra_note="Be brief.",
    )
    assert _flag(narrowed.argv, "--tools") == "read"
    # The operator's own list is taken as given, engine tool or not.
    insisted = build_plan(
        _spec(settings), settings, engine_owned=False, agents_path=settings.agents_path,
        tools=["read", "send_file"],
    )
    assert _flag(insisted.argv, "--tools") == "read,send_file"
    assert _flag(narrowed.argv, "--append-system-prompt") == ESCAPE_NOTE + "\n\nBe brief."
    none = build_plan(_spec(settings), settings, engine_owned=False, agents_path=settings.agents_path, tools=[])
    assert _flag(none.argv, "--tools") == ""


# --- formats -----------------------------------------------------------------------


def test_nul_records_carry_an_argument_with_newlines_whole() -> None:
    plan = Plan(cwd="/p", env={"LLM_MODEL": "m"}, argv=("--approve", "--append-system-prompt", "line one\nline two"))
    records = render_nul(plan).decode("utf-8").split("\0")
    assert records[-1] == ""  # every record is terminated, none is dangling
    assert records[:-1] == ["cwd=/p", "env=LLM_MODEL=m", "arg=--approve",
                            "arg=--append-system-prompt", "arg=line one\nline two"]


def test_the_text_form_shows_that_a_key_is_set_but_not_what_it_is() -> None:
    plan = Plan(cwd="/p q", env={"LLM_API_KEY": "s3cret", "LLM_MODEL": "m"}, argv=("--tools", "read,bash"))
    text = render_text(plan)
    assert "s3cret" not in text
    assert "export LLM_API_KEY=" in text and "export LLM_MODEL=m" in text
    assert "cd '/p q'" in text
    assert "pi --tools read,bash" in text


# --- the CLI -----------------------------------------------------------------------


@pytest.fixture
def _cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    agents, library = _dirs(tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text("GATEWAY=mattermost\nSUPPORT_PROVIDER=anthropic\nSUPPORT_MODEL=claude-sonnet-5\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DOTENV_PATH", str(env_file))
    monkeypatch.setenv("AGENTS_PATH", str(agents))
    monkeypatch.setenv("SKILLS_PATH", str(library))
    for var in ("MATTERMOST_URL", "GATEWAY", "SUPPORT_PROVIDER", "SUPPORT_MODEL", "DEFAULT_PROVIDER"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def test_argv_finds_the_engines_own_agent_with_its_own_defaults(_cli_env: Path, capsysbinary) -> None:
    """`support` lives in the package, not the agents directory, and takes
    SUPPORT_PROVIDER/SUPPORT_MODEL — the command must see it the way the engine
    does, or the one agent this exists for would be "unknown"."""
    assert cli.main(["agent", "argv", "support", "--format", "nul"]) == 0
    records = capsysbinary.readouterr().out.decode("utf-8").split("\0")[:-1]
    assert records[0].endswith("/builtin_agents/agents/support")
    args = [r[len("arg="):] for r in records if r.startswith("arg=")]
    assert _flag(tuple(args), "--provider") == "anthropic"
    assert _flag(tuple(args), "--model") == "claude-sonnet-5"
    # Its allowlist names create_agent, the skill tools, open_screen, the task
    # tools — every one of them the engine's. What is left is pi's own.
    assert _flag(tuple(args), "--tools") == "read,write,edit,bash,grep,find,ls"
    assert f"env=IMPI_ROOT={IMPI_ROOT}" in records
    assert any(r.startswith("env=AGENTS_PATH=") for r in records)


def test_argv_maps_a_user_agent_and_names_the_unknown(_cli_env: Path, capsys) -> None:
    agents = str(_cli_env / "agents-dir")
    assert cli.main(["agent", "argv", "assistant", "--map", f"{agents}=/host/agents",
                     "--map", f"{_cli_env / 'skills'}=/host/skills"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("cd /host/agents/agents/assistant\n")
    assert "--skill /host/skills/web-browsing" in out
    assert "export IMPI_ROOT" not in out  # a user agent is not handed the engine's paths

    assert cli.main(["agent", "argv", "nobody"]) == 2
    err = capsys.readouterr().err
    assert "unknown agent 'nobody'" in err and "assistant" in err and "support" in err
