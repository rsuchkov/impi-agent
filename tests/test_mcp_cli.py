"""`impi mcp`: declare a server, pin its tools, see them, pin them again, forget it."""

import json
from pathlib import Path

import pytest

from impi import cli
from tests.fakes.mcp_http import FakeMcpHttp


@pytest.fixture
def agents_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    env_file = tmp_path / ".env"
    env_file.write_text("GATEWAY=mattermost\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DOTENV_PATH", str(env_file))
    monkeypatch.setenv("AGENTS_PATH", str(tmp_path / "profiles"))
    monkeypatch.setenv("MCP_GITHUB_TOKEN", "t0k")
    return tmp_path / "profiles"


async def _server(port: int, **kw) -> FakeMcpHttp:
    server = FakeMcpHttp(port, **kw)
    await server.start()
    return server


async def test_add_declares_the_server_and_pins_its_tools(agents_dir: Path, capsys) -> None:
    server = await _server(8586, token="t0k")
    try:
        rc = await _run(cli.main, [
            "mcp", "add", "github", "--url", server.url,
            "--header", "Authorization=Bearer ${MCP_GITHUB_TOKEN}", "--confirm", "wipe", "--description", "GitHub",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "2 tools pinned" in out and "mcp__github__echo" in out and "mcp__github__wipe ⚠ destructive" in out
        declared = (agents_dir / "_mcp" / "github.yaml").read_text()
        assert "Bearer ${MCP_GITHUB_TOKEN}" in declared and "t0k" not in declared  # the secret stays in the env
        assert "trust: untrusted" in declared and "- wipe" in declared
        lock = json.loads((agents_dir / "_mcp" / "github.lock.json").read_text())
        assert [t["name"] for t in lock["tools"]] == ["echo", "wipe"] and lock["server_info"]["name"] == "fake-http"
        assert await _run(cli.main, ["mcp", "add", "github", "--url", server.url]) == 2  # exists; --force replaces
        capsys.readouterr()
        assert await _run(cli.main, ["mcp", "list"]) == 0
        assert "github  [untrusted]" in capsys.readouterr().out
        assert await _run(cli.main, ["mcp", "show", "github"]) == 0
        assert "confirm: wipe" in capsys.readouterr().out
    finally:
        await server.stop()


async def test_refresh_pins_again_and_remove_forgets(agents_dir: Path, capsys) -> None:
    server = await _server(8587)
    try:
        assert await _run(cli.main, ["mcp", "add", "stands", "--url", server.url, "--trusted"]) == 0
        server.tools = server.tools[:1]
        capsys.readouterr()
        assert await _run(cli.main, ["mcp", "refresh", "stands"]) == 0
        assert "1 tools pinned (changed)" in capsys.readouterr().out
        assert await _run(cli.main, ["mcp", "remove", "stands"]) == 0
        assert not (agents_dir / "_mcp" / "stands.yaml").exists() and not (agents_dir / "_mcp" / "stands.lock.json").exists()
        assert await _run(cli.main, ["mcp", "remove", "stands"]) == 2
        assert await _run(cli.main, ["mcp", "add", "nowhere", "--url", "http://127.0.0.1:8597/mcp"]) == 1  # unreachable
        assert await _run(cli.main, ["mcp", "add", "both", "--url", "http://x/mcp", "--command", "x"]) == 2
    finally:
        await server.stop()


async def _run(main, argv: list[str]) -> int:
    # The CLI runs its own event loop; from a test's loop it goes to a thread.
    import asyncio

    return await asyncio.to_thread(main, argv)
