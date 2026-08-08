"""Composition smoke: build_app wires everything from Settings, no network."""

import dataclasses
from pathlib import Path

import pytest

from crucible.flows.coalescer import MessageCoalescer
from crucible.gateways.mattermost import MattermostChatClient, MattermostGateway
from crucible.runtimes.pi.profiles import PiProfile
from crucible.store.sessions import SqliteSessionStore
from impi.app import App, build_app, build_pi_env, build_pi_extensions
from impi.config import ImpiSettings as Settings

AGENT_YAML = """\
name: assistant
role: personal-assistant
runtime:
  provider: openai-codex
  model: gpt-5.5
"""


def _agents_dir(tmp_path: Path) -> Path:
    agent_dir = tmp_path / "agents-dir" / "agents" / "assistant"
    agent_dir.mkdir(parents=True)
    (agent_dir / "agent.yaml").write_text(AGENT_YAML, encoding="utf-8")
    return tmp_path / "agents-dir"


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        dotenv_path=str(tmp_path / "no-such.env"),
        agents_path=str(_agents_dir(tmp_path)),
        data_dir=str(tmp_path / "data"),
        mattermost_url="http://localhost:8065",
        mattermost_token="token",
    )


def test_build_app_wires_the_graph(tmp_path: Path) -> None:
    app = build_app(_settings(tmp_path))

    assert isinstance(app, App)
    unit = app.units[0]
    assert unit.spec.name == "assistant"
    assert unit.flow._agent_name == "assistant"
    assert isinstance(unit.flow._profile, PiProfile)
    assert unit.flow._profile.model == "gpt-5.5"
    assert isinstance(unit.gateway, MattermostGateway)
    assert isinstance(unit.gateway._sink, MessageCoalescer)
    assert unit.gateway._sink._flow is unit.flow
    assert isinstance(unit.gateway._chat, MattermostChatClient)
    assert unit.gateway._directory is app.registry
    assert (tmp_path / "data" / "impi.db").exists()
    assert isinstance(app.sessions, SqliteSessionStore)
    assert app.tool_server is not None  # tools enabled by default
    # per-agent tool token + manifest path are injected into the profile env
    assert unit.flow._profile.env.get("TOOL_TOKEN")
    assert unit.flow._profile.env["TOOL_URL"].startswith("http://127.0.0.1")
    assert unit.flow._profile.env["TOOL_MANIFEST"].endswith("assistant.json")
    app.sessions.close_sync()


def test_build_app_skips_agents_without_tokens(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    dev_dir = Path(settings.agents_path) / "agents" / "developer"
    dev_dir.mkdir(parents=True)
    (dev_dir / "agent.yaml").write_text(
        AGENT_YAML.replace("assistant", "developer"), encoding="utf-8"
    )

    # No AGENTS_MM_TOKEN__DEVELOPER anywhere -> developer is skipped, assistant
    # rides the MATTERMOST_TOKEN fallback.
    app = build_app(settings)
    assert [u.spec.name for u in app.units] == ["assistant"]
    assert isinstance(app.sessions, SqliteSessionStore)
    app.sessions.close_sync()

    monkeypatch.setenv("AGENTS_MM_TOKEN__DEVELOPER", "dev-token")
    app2 = build_app(settings)
    assert [u.spec.name for u in app2.units] == ["assistant", "developer"]
    assert isinstance(app2.sessions, SqliteSessionStore)
    app2.sessions.close_sync()


def test_gate_tools_drops_tools_missing_a_capability() -> None:
    from crucible.tools import build_registry
    from crucible.tools.base import CAP_CHAT_ADMIN, CAP_FORMS, CAP_WIDGETS
    from crucible.tools.wiring import _gate_tools

    reg = build_registry()
    slack_caps = frozenset({CAP_WIDGETS, CAP_FORMS})  # a Slack agent: no chat_admin
    kept, dropped = _gate_tools(
        reg, ("list_agents", "create_channel", "ask_user_buttons", "read"), slack_caps
    )
    assert "ask_user_buttons" in kept  # needs widgets (present)
    assert "list_agents" in kept  # requires nothing
    assert "read" not in kept  # a pi builtin, not a typed tool -> ignored
    assert "create_channel" not in kept  # needs chat_admin (absent)
    assert dropped["create_channel"] == frozenset({CAP_CHAT_ADMIN})


async def test_build_app_picks_slack_gateway_when_configured(tmp_path: Path) -> None:
    # gateway=slack + both Slack tokens -> the agent runs on Slack, not Mattermost.
    settings = _settings(tmp_path).model_copy(
        update={"gateway": "slack", "slack_bot_token": "xoxb-x", "slack_app_token": "xapp-x"}
    )
    app = build_app(settings)
    try:
        from crucible.gateways.slack import PROMPT_HINT, SlackGateway

        assert isinstance(app.units[0].gateway, SlackGateway)
        profile = app.units[0].flow._profile
        assert isinstance(profile, PiProfile)
        # the Slack agent's system prompt gets Slack's formatting rules...
        assert profile.append_system_prompt == PROMPT_HINT
        # ...and a Slack-only deployment builds no HTTP callback receiver.
        assert app.integrations is None
    finally:
        await app.units[0].gateway.stop()  # close the socket handler's aiohttp session
        await app.sessions.close()


def test_mattermost_agent_has_no_prompt_hint_and_builds_the_receiver(tmp_path: Path) -> None:
    # The default MM agent needs no formatting hint (Markdown is native) and its
    # HTTP callback receiver is built.
    app = build_app(_settings(tmp_path))
    profile = app.units[0].flow._profile
    assert isinstance(profile, PiProfile)
    assert profile.append_system_prompt == ""
    assert app.integrations is not None
    assert isinstance(app.sessions, SqliteSessionStore)
    app.sessions.close_sync()


def test_skills_override_reaches_the_agent_profile(tmp_path: Path, monkeypatch) -> None:
    # AGENTS_SKILLS__<AGENT> flows through the store into the built profile, and the
    # bare name resolves to the pi runtime's .pi/skills/<name> layout.
    monkeypatch.setenv("AGENTS_SKILLS__ASSISTANT", "note-taker")
    app = build_app(_settings(tmp_path))
    profile = app.units[0].flow._profile
    assert isinstance(profile, PiProfile)
    skill_dir = tmp_path / "agents-dir" / "agents" / "assistant" / ".pi" / "skills" / "note-taker"
    assert profile.skills == (str(skill_dir.resolve()),)
    assert isinstance(app.sessions, SqliteSessionStore)
    app.sessions.close_sync()


def test_build_pi_env_empty_for_subscription_mode(tmp_path: Path) -> None:
    # Subscription mode: LLM_* are empty, pi authenticates itself — no env forwarded.
    assert build_pi_env(_settings(tmp_path)) == {}


def test_build_pi_env_forwards_custom_endpoint(tmp_path: Path) -> None:
    settings = _settings(tmp_path).model_copy(
        update={
            "llm_base_url": "https://llm.local",
            "llm_api_key": "k",
            "llm_model": "m",
            "llm_verify_ssl": False,
        }
    )
    env = build_pi_env(settings)
    assert env["LLM_BASE_URL"] == "https://llm.local"
    assert env["LLM_API_KEY"] == "k"
    assert env["LLM_MODEL"] == "m"
    assert env["NODE_TLS_REJECT_UNAUTHORIZED"] == "0"


def test_build_pi_extensions_bundles_engine_first_then_agents(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    ext = Path(settings.agents_path) / "_extensions" / "custom"
    ext.mkdir(parents=True)
    (ext / "index.ts").write_text("// ext", encoding="utf-8")

    paths = build_pi_extensions(settings)
    # The engine's own tool bridge is bundled with the driver and always first...
    assert paths[0].endswith("runtimes/pi/extension/index.ts")
    assert Path(paths[0]).is_file()  # it really ships in the tree
    # ...and a custom extension in the agents directory is appended.
    assert any(p.endswith("_extensions/custom/index.ts") for p in paths[1:])


def test_build_pi_extensions_is_just_the_bundle_without_agents_extensions(tmp_path: Path) -> None:
    # No _extensions dir in the agents directory -> only the bundled engine bridge.
    paths = build_pi_extensions(_settings(tmp_path))
    assert len(paths) == 1
    assert paths[0].endswith("runtimes/pi/extension/index.ts")


def test_a_reloaded_profile_updates_the_servers_allowlist(tmp_path: Path) -> None:
    # A tool added to a profile and hot-reloaded must be callable, not just
    # advertised: the tool server 403s on anything outside this mapping.
    from crucible.config import ToolSettings
    from crucible.ports.agent import AgentSpec
    from crucible.tools.wiring import ToolWiring

    wiring = ToolWiring(
        ToolSettings(enabled=True, server_host="127.0.0.1", server_port=8422),
        data_dir=str(tmp_path), interactivity_on=True,
    )
    before = AgentSpec(
        name="assistant", display_name="A", role="r", description="d",
        profile_dir=tmp_path, tools=("list_agents",),
    )
    wiring.enroll(before, None)
    assert wiring.allowlists["assistant"] == frozenset({"list_agents"})

    after = dataclasses.replace(before, tools=("list_agents", "ask_user_buttons"))
    wiring.profile_env(after)

    assert wiring.allowlists["assistant"] == frozenset({"list_agents", "ask_user_buttons"})


def test_build_app_wires_the_scheduler(tmp_path: Path) -> None:
    app = build_app(_settings(tmp_path))

    assert app.scheduler is not None
    # The store is the same file the sessions live in — one database, one writer.
    assert app.scheduler._store is app.sessions


def test_the_scheduler_can_be_turned_off(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")

    app = build_app(_settings(tmp_path))

    assert app.scheduler is None


async def test_the_scheduler_dispatches_through_the_agents_own_sink(tmp_path: Path) -> None:
    # The seam that matters: a scheduled turn takes the same path a command
    # does, and comes back with the outcome.
    from crucible.flows.coalescer import MessageCoalescer
    from crucible.interactions import AgentSink
    from crucible.ports.chat.flow import TurnOutcome
    from crucible.scheduler.ports import DispatchError, TurnRequest
    from impi.scheduling import SinkTurnDispatcher
    from tests.fakes.fake_chat import FakeChat

    class Answering:
        def __init__(self) -> None:
            self.batches: list = []

        async def handle_batch(self, msgs, chat) -> TurnOutcome:
            self.batches.append(msgs)
            return TurnOutcome.REPLIED

    flow = Answering()
    chat = FakeChat()
    sinks = {"assistant": AgentSink(sink=MessageCoalescer(flow), chat=chat)}
    dispatcher = SinkTurnDispatcher(sinks)

    outcome = await dispatcher.run_turn(
        TurnRequest(
            agent="assistant", channel_id="ch1", conversation_id="root1", kind="thread",
            text="the digest, please", message_id="sched-abc", username="roman",
        )
    )

    assert outcome is TurnOutcome.REPLIED
    (message,) = flow.batches[0]
    assert message.synthetic and message.mentioned  # addressed, not typed
    assert message.ref.thread_root_id == "root1"  # a thread task answers in its thread
    assert message.username == "roman"

    with pytest.raises(DispatchError, match="not running"):
        await dispatcher.run_turn(
            TurnRequest(agent="nobody", channel_id="c", conversation_id="c", kind="dm",
                        text="hi", message_id="sched-x")
        )
