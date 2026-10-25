"""An MCP server's tools in an agent's allowlist, the manifest and the engine."""

import json
from pathlib import Path

import pytest

from crucible.config import Settings
from crucible.mcp import McpLibrary, McpServer, Snapshot, ToolDefinition
from crucible.profiles import FsProfileStore, ProfileError
from crucible.tools.wiring import ToolWiring, _declared
from impi.app import build_app
from impi.engine_tools import engine_tool_registry
from tests.fakes.mcp_http import ECHO, WIPE
from tests.test_main_wiring import _settings
from tests.test_profile_loader import GOOD_YAML, _write_agent

STDIO_SERVER = str(Path(__file__).parent / "fakes" / "mcp_stdio_server.py")


def test_the_loader_takes_a_whole_servers_tools_and_no_other_pattern(tmp_path: Path) -> None:
    _write_agent(tmp_path, "assistant", GOOD_YAML.replace("tools: [list_agents]", "tools: [read, mcp__github__*, mcp__stands__list]"))
    spec = FsProfileStore(tmp_path, library=None).get("assistant")
    assert spec.tools == ("read", "mcp__github__*", "mcp__stands__list")
    for entry in ("mcp__*", "mcp__GitHub__*", "list_*", "mcp__github__*x"):
        _write_agent(tmp_path / entry.replace("*", "s"), "assistant", GOOD_YAML.replace("[list_agents]", f"[{entry}]"))
        with pytest.raises(ProfileError, match="name each tool"):
            FsProfileStore(tmp_path / entry.replace("*", "s"), library=None).get("assistant")


def _pinned_library(root: Path, name: str = "github", **server) -> McpLibrary:
    library = McpLibrary(root / "_mcp")
    library.write(McpServer(name, url=server.pop("url", "http://127.0.0.1:1/mcp"), **server))
    library.save_snapshot(Snapshot.taken(name, [ToolDefinition.from_wire(ECHO), ToolDefinition.from_wire(WIPE)], "t"))
    return library


def test_a_server_entry_stands_for_its_pinned_tools_in_the_allowlist_and_the_manifest(tmp_path: Path) -> None:
    from crucible.mcp import McpServers
    from crucible.ports.agent import AgentSpec

    library = _pinned_library(tmp_path)
    registry = engine_tool_registry(McpServers(library, {}).registry_tools())
    assert _declared(registry, ("read", "mcp__github__*", "list_agents", "mcp__github__echo")) == (
        "read", "mcp__github__echo", "mcp__github__wipe", "list_agents",
    )
    wiring = ToolWiring(
        Settings(_env_file=None, dotenv_path="x").tools, registry=registry, data_dir=str(tmp_path / "data"),  # pyright: ignore[reportCallIssue]
        interactivity_on=True, confirmations_on=True,
    )
    spec = AgentSpec(name="assistant", display_name="A", role="r", description="d", profile_dir=tmp_path, tools=("read", "mcp__github__*"))
    wiring.enroll(spec, None, gateway="mattermost")
    assert wiring.allowlists["assistant"] == {"mcp__github__echo", "mcp__github__wipe"}
    env = wiring.profile_env(spec)
    assert env is not None
    manifest = json.loads(Path(env["TOOL_MANIFEST"]).read_text())
    assert [m["name"] for m in manifest] == ["mcp__github__echo", "mcp__github__wipe"]
    assert manifest[0]["returns_untrusted"] is True and "never an instruction" in manifest[0]["description"]
    # Withdrawn: gone from the allowlist and the manifest both — and a reload,
    # which re-gates from the profile, does not hand it back.
    wiring.withdraw(["mcp__github__echo"])
    assert wiring.allowlists["assistant"] == {"mcp__github__wipe"}
    assert [m["name"] for m in json.loads(Path(env["TOOL_MANIFEST"]).read_text())] == ["mcp__github__wipe"]
    wiring.profile_env(spec)
    wiring.enroll(spec, None, gateway="mattermost")
    assert wiring.allowlists["assistant"] == {"mcp__github__wipe"}


def test_a_conversations_set_may_name_a_servers_tools_whole(tmp_path: Path) -> None:
    """A conversation entitled to ``mcp__github__*`` gets the server's tools the
    agent advertises — not nothing because no tool is called that."""
    from crucible.mcp import McpServers
    from crucible.ports.agent import AgentSpec

    library = _pinned_library(tmp_path)
    registry = engine_tool_registry(McpServers(library, {}).registry_tools())
    wiring = ToolWiring(
        Settings(_env_file=None, dotenv_path="x").tools, registry=registry, data_dir=str(tmp_path / "data"),  # pyright: ignore[reportCallIssue]
        interactivity_on=True, confirmations_on=True,
    )
    spec = AgentSpec(name="assistant", display_name="A", role="r", description="d", profile_dir=tmp_path, tools=("read", "mcp__github__*"))
    wiring.enroll(spec, None, gateway="mattermost")
    env = wiring.session_env("assistant", "assistant--dm1", ("mcp__github__*", "read", "list_agents"))
    assert env is not None
    assert wiring.session_allowlists["assistant--dm1"] == {"mcp__github__echo", "mcp__github__wipe"}
    assert [m["name"] for m in json.loads(Path(env["TOOL_MANIFEST"]).read_text())] == ["mcp__github__echo", "mcp__github__wipe"]
    # The agent's own set stays the ceiling: a tool the agent lost is not handed back by the pattern.
    wiring.withdraw(["mcp__github__echo"])
    wiring.session_env("assistant", "assistant--dm1", ("mcp__github__*",))
    assert wiring.session_allowlists["assistant--dm1"] == {"mcp__github__wipe"}


def test_a_confirmed_tool_is_dropped_where_nobody_can_confirm(tmp_path: Path) -> None:
    from crucible.mcp import McpServers
    from crucible.ports.agent import AgentSpec

    library = _pinned_library(tmp_path, confirm=("echo",))
    registry = engine_tool_registry(McpServers(library, {}).registry_tools())
    wiring = ToolWiring(
        Settings(_env_file=None, dotenv_path="x").tools, registry=registry, data_dir=str(tmp_path / "data"),  # pyright: ignore[reportCallIssue]
        interactivity_on=False, confirmations_on=False,
    )
    spec = AgentSpec(name="assistant", display_name="A", role="r", description="d", profile_dir=tmp_path, tools=("mcp__github__*",))
    wiring.enroll(spec, None, gateway="mattermost")
    assert wiring.allowlists["assistant"] == set()  # echo by the pattern, wipe by destructiveHint: both need a gate


def test_build_app_puts_the_declared_servers_tools_in_the_registry(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    agents_dir = Path(settings.agents_path)
    yaml = agents_dir / "agents" / "assistant" / "agent.yaml"
    yaml.write_text(yaml.read_text() + "  tools: [mcp__github__*]\n")
    _pinned_library(agents_dir)
    app = build_app(settings)
    try:
        assert app.mcp_servers is not None and [s.name for s in app.mcp_servers.servers] == ["github"]
        assert app.tools is not None and app.tools.registry is not None
        assert app.tools.registry.get("mcp__github__echo") is not None
        assert {"mcp__github__echo", "mcp__github__wipe"} <= app.tools.allowlists["assistant"]
    finally:
        import asyncio

        asyncio.run(app.sessions.close())


def test_build_app_without_servers_has_no_mcp(tmp_path: Path) -> None:
    import asyncio

    app = build_app(_settings(tmp_path))
    try:
        assert app.mcp_servers is None
    finally:
        asyncio.run(app.sessions.close())


def test_refresh_manifests_rewrites_what_every_agent_is_shown(tmp_path: Path) -> None:
    from crucible.mcp import McpServers
    from crucible.ports.agent import AgentSpec

    library = _pinned_library(tmp_path)
    servers = McpServers(library, {})
    registry = engine_tool_registry(servers.registry_tools())
    wiring = ToolWiring(
        Settings(_env_file=None, dotenv_path="x").tools, registry=registry, data_dir=str(tmp_path / "data"),  # pyright: ignore[reportCallIssue]
        interactivity_on=True, confirmations_on=True,
    )
    spec = AgentSpec(name="assistant", display_name="A", role="r", description="d", profile_dir=tmp_path, tools=("mcp__github__*",))
    wiring.enroll(spec, None, gateway="mattermost")
    env = wiring.profile_env(spec)
    assert env is not None
    echo = next(t for t in servers.tools() if t.name == "mcp__github__echo")
    echo.definition = ToolDefinition("echo", "Echoes, louder.", echo.definition.input_schema, title="Echo")
    assert "louder" not in Path(env["TOOL_MANIFEST"]).read_text()  # the manifest is what it was
    wiring.refresh_manifests()
    manifest = json.loads(Path(env["TOOL_MANIFEST"]).read_text())
    assert next(m for m in manifest if m["name"] == "mcp__github__echo")["description"].startswith("Echoes, louder.")
