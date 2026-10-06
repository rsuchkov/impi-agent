"""Composition smoke: build_app wires everything from Settings, no network."""

import dataclasses
from pathlib import Path

import pytest

from crucible.flows.coalescer import MessageCoalescer
from crucible.gateways.mattermost import MattermostChatClient, MattermostGateway
from crucible.process_scopes import AgentProcessScopes
from crucible.runtimes.pi.profiles import PiProfile
from crucible.runtimes.pi.runtime import PiRuntime
from crucible.store.sqlite import SqliteSessionStore
from impi.app import App, build_app, build_pi_extensions
from impi.config import ImpiSettings as Settings
from impi.profiles import build_pi_env

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
    # The manifest is a file, so it rides in env_files: a host with its own
    # filesystem is handed the content, not this side's path.
    assert unit.flow._profile.env_files["TOOL_MANIFEST"].endswith("assistant.json")
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
        from crucible.flows.agent_flow import ENVELOPE_NOTE
        from crucible.gateways.slack import PROMPT_HINT, SlackGateway

        assert isinstance(app.units[0].gateway, SlackGateway)
        profile = app.units[0].flow._profile
        assert isinstance(profile, PiProfile)
        # the Slack agent's system prompt gets Slack's formatting rules...
        assert profile.append_system_prompt == f"{ENVELOPE_NOTE}\n\n{PROMPT_HINT}"
        # ...and a Slack-only deployment builds no HTTP callback receiver.
        assert app.integrations is None
    finally:
        await app.units[0].gateway.stop()  # close the socket handler's aiohttp session
        await app.sessions.close()


def test_mattermost_agent_gets_only_the_envelope_note_and_builds_the_receiver(tmp_path: Path) -> None:
    # The default MM agent needs no formatting hint (Markdown is native), so its
    # system prompt suffix is the envelope note alone; its HTTP callback
    # receiver is built.
    from crucible.flows.agent_flow import ENVELOPE_NOTE

    app = build_app(_settings(tmp_path))
    profile = app.units[0].flow._profile
    assert isinstance(profile, PiProfile)
    assert profile.append_system_prompt == ENVELOPE_NOTE
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


def test_build_pi_extensions_bundles_engine_first_then_the_named_agents_extensions(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path).model_copy(update={"runtime_extensions": "custom"})
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
        ToolSettings(
            enabled=True, server_host="127.0.0.1", server_port=8422, max_grant_s=900
        ),
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
    # The screen stays bound to /tasks so the command still answers for itself
    # rather than falling through to an agent that cannot read the schedule.
    assert app.screens.handles("tasks")


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


def test_an_engine_agent_is_told_where_the_engine_is_even_with_tools_off(
    tmp_path: Path,
) -> None:
    # add_env carries IMPI_ROOT/AGENTS_PATH for the engine's own agents. Losing
    # it when the typed-tool server is off would leave support with no idea
    # where the docs and the source are — the one thing it always needs.
    from crucible.config import ToolSettings
    from crucible.ports.agent import AgentSpec
    from crucible.tools.wiring import ToolWiring

    wiring = ToolWiring(
        ToolSettings(
            enabled=False, server_host="127.0.0.1", server_port=8422, max_grant_s=900
        ),
        data_dir=str(tmp_path), interactivity_on=True,
    )
    spec = AgentSpec(
        name="support", display_name="S", role="r", description="d",
        profile_dir=tmp_path, tools=("read",),
    )
    wiring.enroll(spec, None)  # early-returns: no registry
    wiring.add_env("support", {"IMPI_ROOT": "/app", "AGENTS_PATH": "/app/agents"})

    env = wiring.profile_env(spec)

    # Its own name travels whatever else does — enrolling is what tells an agent
    # who it is, and that must not depend on whether it has typed tools.
    assert env == {
        "AGENT_NAME": "support", "IMPI_ROOT": "/app", "AGENTS_PATH": "/app/agents"
    }
    assert wiring.profile_env(
        dataclasses.replace(spec, name="assistant")
    ) is None  # an ordinary agent still gets nothing


# --- the tool widget under a reply --------------------------------------------


async def _closed(app) -> None:
    for unit in app.units:
        stop = getattr(unit.gateway, "stop", None)
        if stop is not None:
            await stop()
    await app.sessions.close()


async def test_the_tool_trace_is_wired_in_by_default_and_its_screen_is_unlisted(
    tmp_path: Path,
) -> None:
    from crucible.interactions import TRACE_SCREEN, ToolTrace

    app = build_app(_settings(tmp_path))
    try:
        assert isinstance(app.tracer, ToolTrace)
        assert app.units[0].flow._tracer is app.tracer
        # Clicks route to the screen; nobody is offered it as a panel.
        assert app.screens.get(TRACE_SCREEN) is not None
        assert TRACE_SCREEN not in app.screens.names()
    finally:
        await _closed(app)


async def test_the_tool_trace_can_be_switched_off(tmp_path: Path) -> None:
    from crucible.interactions import TRACE_SCREEN

    app = build_app(_settings(tmp_path).model_copy(update={"tool_trace_enabled": False}))
    try:
        assert app.tracer is None
        assert app.units[0].flow._tracer is None
        # The screen stays: a button posted before the switch must still open.
        assert app.screens.get(TRACE_SCREEN) is not None
    finally:
        await _closed(app)


async def test_no_interactivity_means_no_tool_trace(tmp_path: Path) -> None:
    # Without a dispatcher the button would be dead, and a dead control is
    # worse than no widget at all.
    app = build_app(_settings(tmp_path).model_copy(update={"integrations_enabled": False}))
    try:
        assert app.tracer is None
    finally:
        await _closed(app)


async def test_build_app_puts_an_agent_on_the_http_hub(tmp_path: Path, monkeypatch) -> None:
    # gateway=http + a registered caller -> the agent is served by the http hub:
    # its tool activity goes to the caller's journal, not to a chat widget.
    from crucible.gateways.http import HttpChatClient, HttpGateway

    monkeypatch.setenv("HTTP_CALLER_TOKEN__PORTAL", "tok-portal")
    settings = _settings(tmp_path).model_copy(update={"gateway": "http"})
    app = build_app(settings)
    try:
        assert app.http_hub is not None and app.ws_hub is None
        unit = app.units[0]
        assert isinstance(unit.gateway, HttpGateway)
        assert unit.flow._tracer is app.http_hub.turns  # the journal, not ToolTrace
        assert unit.flow._turns is not None  # a caller's TurnScope has somewhere to go
        sink, chat = app.http_hub._agents["assistant"]
        assert isinstance(chat, HttpChatClient)
        assert hasattr(sink, "submit_tracked")
    finally:
        await app.sessions.close()


def test_a_missing_receiver_secret_is_minted_into_the_env_file(tmp_path: Path) -> None:
    """A Mattermost deployment that starts without one gets a secret written to
    its own .env and a receiver built under it — buttons keep working after an
    upgrade, and the log says what to re-register."""
    settings = _settings(tmp_path)
    assert settings.integrations_secret == ""
    app = build_app(settings)
    try:
        written = Path(settings.dotenv_path).read_text()
        assert "INTEGRATIONS_SECRET=" in written
        minted = written.split("INTEGRATIONS_SECRET=")[1].splitlines()[0]
        assert len(minted) >= 16
        assert app.integrations is not None
        assert app.integrations._secret == minted
    finally:
        assert isinstance(app.sessions, SqliteSessionStore)
        app.sessions.close_sync()


async def test_a_slack_only_deployment_mints_no_secret(tmp_path: Path) -> None:
    settings = _settings(tmp_path).model_copy(
        update={"gateway": "slack", "slack_bot_token": "xoxb-x", "slack_app_token": "xapp-x"}
    )
    app = build_app(settings)
    try:
        assert not Path(settings.dotenv_path).exists()
    finally:
        await app.units[0].gateway.stop()
        await app.sessions.close()


def test_only_the_engines_own_agent_inherits_the_whole_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """support is part of the engine and runs its CLI; a user's agent gets the
    base set plus what its profile and the deployment name."""
    monkeypatch.setenv("AGENTS_MM_TOKEN__SUPPORT", "support-token")
    app = build_app(_settings(tmp_path))
    try:
        by_name = {unit.spec.name: unit.flow._profile for unit in app.units}
        assistant, support = by_name["assistant"], by_name["support"]
        assert isinstance(assistant, PiProfile) and isinstance(support, PiProfile)
        assert assistant.inherit_all is False
        assert support.inherit_all is True
    finally:
        assert isinstance(app.sessions, SqliteSessionStore)
        app.sessions.close_sync()


SHELL_AGENT_YAML = """\
name: {name}
role: helper
runtime:
  tools: [read, bash]
"""


def _two_shell_agents(tmp_path: Path) -> Settings:
    settings = _settings(tmp_path)
    root = Path(settings.agents_path) / "agents"
    for name in ("alpha", "beta"):
        (root / name).mkdir()
        (root / name / "agent.yaml").write_text(SHELL_AGENT_YAML.format(name=name), encoding="utf-8")
    return settings


def test_agents_sharing_the_engines_process_with_a_shell_are_named_at_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Two agents with a shell in one process space can read each other's tool
    token and proofs; the engine says so by name rather than letting the
    operator find out."""
    monkeypatch.setenv("AGENTS_MM_TOKEN__ALPHA", "a")
    monkeypatch.setenv("AGENTS_MM_TOKEN__BETA", "b")
    with caplog.at_level("WARNING", logger="impi.app"):
        app = build_app(_two_shell_agents(tmp_path))
    try:
        assert "agents alpha, beta run in the engine's own process space" in caplog.text
        assert "the engine's own, support's" in caplog.text
        assert "docs/security.md" in caplog.text
    finally:
        assert isinstance(app.sessions, SqliteSessionStore)
        app.sessions.close_sync()


def test_one_agent_alone_in_the_engine_is_nobodys_neighbour(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = _settings(tmp_path)  # assistant alone, no shell either
    with caplog.at_level("WARNING", logger="impi.app"):
        app = build_app(settings)
    try:
        assert "process space" not in caplog.text
    finally:
        assert isinstance(app.sessions, SqliteSessionStore)
        app.sessions.close_sync()


def test_shared_process_agents_is_about_shells_among_neighbours() -> None:
    from impi.app import shared_process_agents

    def spec(name: str, *tools: str):
        return dataclasses.replace(
            __import__("crucible.ports.agent", fromlist=["AgentSpec"]).AgentSpec(
                name=name, display_name=name, role="r", description="", profile_dir=Path("x")
            ),
            tools=tuple(tools),
        )

    alpha, beta, gamma = spec("alpha", "bash"), spec("beta", "list_agents"), spec("gamma", "read")
    everyone_here = {"in_process": lambda name: True}
    assert shared_process_agents([alpha, beta, gamma], **everyone_here) == ("alpha", "gamma")
    assert shared_process_agents([alpha, beta], **everyone_here) == ("alpha",)
    assert shared_process_agents([beta, spec("delta", "ls")], **everyone_here) == ()
    # alone with a shell: the engine and support are still its neighbours
    assert shared_process_agents([alpha], **everyone_here) == ("alpha",)
    # moved out: a neighbour no longer
    assert shared_process_agents([alpha, gamma], in_process=lambda name: False) == ()


def test_an_extension_that_is_present_but_not_named_is_not_loaded_and_is_named_in_the_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An extension is code in every agent's process; a directory any agent
    with `write` can add to does not get to decide what runs there."""
    settings = _settings(tmp_path).model_copy(update={"runtime_extensions": "jira, ghost"})
    root = Path(settings.agents_path) / "_extensions"
    for name in ("jira", "planted"):
        (root / name).mkdir(parents=True)
        (root / name / "index.ts").write_text("// x", encoding="utf-8")
    with caplog.at_level("WARNING", logger="impi.app"):
        paths = build_pi_extensions(settings)
    assert [Path(p).parent.name for p in paths[1:]] == ["jira"]
    assert "extension planted is present" in caplog.text and "not enabled" in caplog.text
    assert "names ghost" in caplog.text


def test_an_extension_name_that_could_walk_out_of_the_directory_is_refused(tmp_path: Path) -> None:
    settings = _settings(tmp_path).model_copy(update={"runtime_extensions": "../engine"})
    with pytest.raises(ValueError, match="RUNTIME_EXTENSIONS"):
        settings.runtime_extension_names()


def test_an_unwritable_env_file_does_not_stop_the_engine(tmp_path: Path) -> None:
    """A deployment configured by its environment alone (or a read-only conf/)
    starts on the bare paths, warned, rather than not at all."""
    from impi.provisioning import ensure_receiver_secret

    blocked = tmp_path / "ro"
    blocked.mkdir()
    blocked.chmod(0o500)
    try:
        settings = _settings(tmp_path).model_copy(update={"dotenv_path": str(blocked / ".env")})
        assert ensure_receiver_secret(settings, needs_receiver=True).integrations_secret == ""
    finally:
        blocked.chmod(0o700)


async def test_a_users_agent_is_confined_and_the_engines_own_is_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTS_MM_TOKEN__SUPPORT", "support-token")
    app = build_app(_settings(tmp_path))
    try:
        assert isinstance(app.runtime, PiRuntime)
        scopes = app.runtime._scopes
        assert isinstance(scopes, AgentProcessScopes)
        assert scopes.level_for("assistant") == "conversation"
        assert scopes.level_for("support") == "off"
        assert await scopes.scope_for("support", None) is None
        assistant = await scopes.scope_for("assistant", None)
        assert assistant is not None and any(str(p).endswith("assistant") for p in assistant.readable)
    finally:
        assert isinstance(app.sessions, SqliteSessionStore)
        app.sessions.close_sync()


async def test_a_conversations_process_writes_its_own_attachments_directory(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path).model_copy(update={"attachments_enabled": True})
    app = build_app(settings)
    try:
        assert isinstance(app.runtime, PiRuntime)
        scopes = app.runtime._scopes
        assert isinstance(scopes, AgentProcessScopes)
        record, _ = await app.sessions.get_or_create("assistant", "dm1", "dm1", "dm")
        scope = await scopes.scope_for("assistant", record.runtime_session_id)
        assert scope is not None
        own = settings.resolved_attachments_dir / "assistant" / "dm1"
        assert own in scope.writable and scope.env["AGENT_FILES_DIR"] == str(own)
        assert settings.resolved_attachments_dir / "assistant" not in scope.writable
        # Another agent's conversation is not this agent's to write.
        theirs, _ = await app.sessions.get_or_create("other", "dm2", "dm2", "dm")
        borrowed = await scopes.scope_for("assistant", theirs.runtime_session_id)
        assert borrowed is not None and borrowed.env["AGENT_FILES_DIR"] == borrowed.env["TMPDIR"]
        # A session the inventory does not know is no conversation's.
        stranger = await scopes.scope_for("assistant", "assistant--nobody")
        assert stranger is not None and stranger.env["AGENT_FILES_DIR"] == stranger.env["TMPDIR"]
    finally:
        assert isinstance(app.sessions, SqliteSessionStore)
        app.sessions.close_sync()


def test_an_agent_in_a_container_of_its_own_is_told_where_its_files_are(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its host resolves the conversation's files directory by name, against
    this agent's files directory — so the engine has to know which one that is."""
    from crucible.runtimes.pi.hosts.remote import RemoteHost

    monkeypatch.setenv("AGENTS_HOST_TOKEN__ASSISTANT", "host-token")
    settings = _settings(tmp_path).model_copy(
        update={"attachments_enabled": True, "agent_hosts_enabled": True}
    )
    app = build_app(settings)
    try:
        assert isinstance(app.runtime, PiRuntime)
        host = app.runtime._hosts.for_agent("assistant")
        assert isinstance(host, RemoteHost)
        assert host._files_root == (settings.resolved_attachments_dir / "assistant").resolve()
    finally:
        assert isinstance(app.sessions, SqliteSessionStore)
        app.sessions.close_sync()
