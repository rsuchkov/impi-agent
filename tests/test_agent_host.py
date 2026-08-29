"""The engine driving a runtime that lives in another container.

Two of these are contract tests. The host re-declares the wire rather than
importing it, and builds its own command line rather than being handed one, so
the only thing keeping the two sides honest is a test that puts them side by
side. If one of these fails, an agent would behave differently for having moved
— which is exactly what moving it must not do.
"""

import asyncio
import json
import socket
import stat
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web

from crucible.runtimes.pi.errors import PiHostError
from crucible.runtimes.pi.hosts import wire as engine_wire
from crucible.runtimes.pi.hosts.local import _environment, command_args
from crucible.runtimes.pi.hosts.remote import RemoteHost, RemoteTransport
from crucible.runtimes.pi.spawn import SpawnRequest, safe_session_id
from runtime_relay import wire as host_wire
from runtime_relay.config import HostConfig
from runtime_relay.server import RelayServer
from runtime_relay.spawn import SpawnRejected
from runtime_relay.spawn import command_args as host_command_args
from runtime_relay.spawn import parse as host_parse


def _constants(module: object) -> dict[str, object]:
    return {
        name: value
        for name, value in vars(module).items()
        if name.isupper() and not name.startswith("_")
    }


def test_the_two_wire_declarations_agree() -> None:
    assert _constants(engine_wire) == _constants(host_wire)


# --- the same command line, wherever the agent runs ---------------------------


def _dirs(tmp_path: Path) -> tuple[Path, Path]:
    profile = tmp_path / "agents" / "assistant"
    (profile / ".pi" / "skills" / "own").mkdir(parents=True, exist_ok=True)
    library = tmp_path / "skills"
    (library / "web-browsing").mkdir(parents=True, exist_ok=True)
    return profile, library


def _request(profile: Path, library: Path, **overrides: object) -> SpawnRequest:
    base: dict[str, object] = {
        "agent": "assistant",
        "profile_dir": profile,
        "session_id": safe_session_id("assistant--chan/1"),
        "tools": ("read", "bash", "send_file"),
        "skills": (
            str(profile / ".pi" / "skills" / "own"),
            str(library / "web-browsing"),
        ),
        "provider": "openai-codex",
        "model": "gpt-5.5",
        "append_system_prompt": "Answer in mrkdwn.",
        "env": {"TOOL_URL": "http://impi:8422", "TOOL_TOKEN": "t0ken"},
    }
    base.update(overrides)
    return SpawnRequest(**base)  # type: ignore[arg-type]


def _host_config(tmp_path: Path, profile: Path, library: Path, **over: object) -> HostConfig:
    settings: dict[str, object] = {
        "agent": "assistant",
        "token": "shared",
        "profile_dir": profile.resolve(),
        "library_dir": library.resolve(),
        "session_dir": tmp_path / "sessions",
        "work_dir": tmp_path / "work",
        "runtime_bin": "pi",
        "extensions": (tmp_path / "bridge" / "index.ts",),
    }
    settings.update(over)
    return HostConfig(**settings)  # type: ignore[arg-type]


def test_the_command_line_is_the_same_wherever_the_agent_runs(tmp_path: Path) -> None:
    profile, library = _dirs(tmp_path)
    sessions = tmp_path / "sessions"
    request = _request(
        profile,
        library,
        session_dir=tmp_path / "pi-sessions",
        extensions=(str(tmp_path / "bridge" / "index.ts"),),
    )
    local = command_args(request, session_dir=sessions)

    payload = RemoteHost(url="http://x", token="t", library_root=library)._spawn_payload(
        request
    )
    config = _host_config(tmp_path, profile, library)
    remote = host_command_args(host_parse(payload, config), config=config, session_dir=sessions)

    assert local == remote
    # And it really is the whole command line, not two empty lists agreeing.
    assert "--tools" in local and "--skill" in local


def test_a_skill_outside_the_mounted_roots_is_refused(tmp_path: Path) -> None:
    profile, library = _dirs(tmp_path)
    stray = tmp_path / "elsewhere" / "skill"
    stray.mkdir(parents=True)
    request = _request(profile, library, skills=(str(stray),))

    host = RemoteHost(url="http://x", token="t", library_root=library)
    with pytest.raises(PiHostError, match="neither in its own profile"):
        host._spawn_payload(request)


def test_a_skill_reference_cannot_climb_out_of_its_root(tmp_path: Path) -> None:
    profile, library = _dirs(tmp_path)
    config = _host_config(tmp_path, profile, library)
    payload = {
        engine_wire.KEY_VERSION: engine_wire.PROTOCOL_VERSION,
        engine_wire.KEY_AGENT: "assistant",
        engine_wire.KEY_SKILLS: [
            {engine_wire.KEY_ROOT: engine_wire.ROOT_PROFILE, engine_wire.KEY_PATH: "../../etc"}
        ],
    }
    with pytest.raises(SpawnRejected, match="outside"):
        host_parse(payload, config)


def test_a_request_for_another_agent_is_refused(tmp_path: Path) -> None:
    profile, library = _dirs(tmp_path)
    config = _host_config(tmp_path, profile, library)
    payload = {
        engine_wire.KEY_VERSION: engine_wire.PROTOCOL_VERSION,
        engine_wire.KEY_AGENT: "somebody-else",
    }
    with pytest.raises(SpawnRejected, match="serves 'assistant'"):
        host_parse(payload, config)


def test_a_newer_protocol_is_refused_rather_than_half_understood(tmp_path: Path) -> None:
    profile, library = _dirs(tmp_path)
    config = _host_config(tmp_path, profile, library)
    payload = {
        engine_wire.KEY_VERSION: engine_wire.PROTOCOL_VERSION + 1,
        engine_wire.KEY_AGENT: "assistant",
    }
    with pytest.raises(SpawnRejected, match="protocol"):
        host_parse(payload, config)


def test_a_file_an_env_variable_points_at_travels_as_content(tmp_path: Path) -> None:
    profile, library = _dirs(tmp_path)
    manifest = tmp_path / "manifests" / "assistant.json"
    manifest.parent.mkdir()
    manifest.write_text('{"tools": []}', encoding="utf-8")
    request = _request(profile, library, env_files={"TOOL_MANIFEST": manifest})

    payload = RemoteHost(url="http://x", token="t", library_root=library)._spawn_payload(
        request
    )

    # The content crosses, never the path: the engine's path does not exist there.
    assert payload[engine_wire.KEY_ENV_FILES] == {"TOOL_MANIFEST": '{"tools": []}'}
    assert str(manifest) not in json.dumps(payload)


def test_the_local_host_still_passes_the_path_itself(tmp_path: Path) -> None:
    profile, library = _dirs(tmp_path)
    manifest = tmp_path / "assistant.json"
    manifest.write_text("{}", encoding="utf-8")
    request = _request(profile, library, env_files={"TOOL_MANIFEST": manifest})

    # Same process, same filesystem: nothing to ship, and nothing changes.
    env = _environment(request)
    assert env is not None and env["TOOL_MANIFEST"] == str(manifest)


# --- end to end, over a real socket -------------------------------------------


FAKE_RUNTIME = """#!/usr/bin/env python3
import sys
sys.stderr.write("fake runtime up\\n")
sys.stderr.flush()
for line in sys.stdin:
    line = line.strip()
    if line == "quit":
        sys.stderr.write("asked to quit\\n")
        raise SystemExit(3)
    sys.stdout.write('{"echo": %s}\\n' % line)
    sys.stdout.flush()
"""


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class _Rig:
    def __init__(self, runner: web.AppRunner, port: int) -> None:
        self.runner = runner
        self.port = port

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def stop(self) -> None:
        await self.runner.cleanup()


async def _serve(tmp_path: Path, **over: object) -> _Rig:
    profile, library = _dirs(tmp_path)
    runtime = tmp_path / "fake-runtime"
    runtime.write_text(FAKE_RUNTIME, encoding="utf-8")
    runtime.chmod(runtime.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    settings: dict[str, object] = {"runtime_bin": str(runtime), "extensions": ()}
    settings.update(over)
    config = _host_config(tmp_path, profile, library, **settings)
    port = _free_port()
    runner = web.AppRunner(RelayServer(config).app())
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return _Rig(runner, port)


async def test_a_turn_crosses_the_wire_and_comes_back(tmp_path: Path) -> None:
    rig = await _serve(tmp_path)
    profile, library = _dirs(tmp_path)
    host = RemoteHost(url=rig.url, token="shared", library_root=library)
    try:
        transport = await host.open(_request(profile, library, skills=()))
        assert isinstance(transport, RemoteTransport)
        lines = transport.lines()
        await transport.send('"hello"\n')
        assert json.loads(await anext(lines)) == {"echo": "hello"}

        await transport.send("quit\n")
        rest = [line async for line in lines]
        assert rest == []
        # The runtime's own stderr comes back with the exit, which is what turns
        # "it died" into something an operator can act on.
        detail = await transport.exit_detail()
        assert "exit code 3" in detail and "asked to quit" in detail
    finally:
        await host.aclose()
        await rig.stop()


async def test_only_the_granted_environment_goes_on_the_wire() -> None:
    """The engine used to hand a runtime its whole environment. Across a
    container boundary that would be the engine's entire secret set, so the
    request carries what was granted and nothing else."""
    profile = Path("/agents/assistant")
    request = SpawnRequest(
        agent="assistant",
        profile_dir=profile,
        env={"TOOL_TOKEN": "t0ken"},
    )

    payload = RemoteHost(url="http://x", token="t")._spawn_payload(request)

    assert payload[engine_wire.KEY_ENV] == {"TOOL_TOKEN": "t0ken"}
    assert "PATH" not in payload[engine_wire.KEY_ENV]  # type: ignore[operator]


async def test_the_granted_environment_reaches_the_process(tmp_path: Path) -> None:
    reporter = tmp_path / "reporter"
    reporter.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "sys.stdout.write(json.dumps({'granted': os.environ.get('TOOL_TOKEN')}) + '\\n')\n"
        "sys.stdout.flush()\n"
        "import time; time.sleep(30)\n",
        encoding="utf-8",
    )
    reporter.chmod(reporter.stat().st_mode | stat.S_IEXEC)
    rig = await _serve(tmp_path, runtime_bin=str(reporter))
    profile, library = _dirs(tmp_path)
    host = RemoteHost(url=rig.url, token="shared", library_root=library)
    try:
        transport = await host.open(_request(profile, library, skills=()))
        reported = json.loads(await anext(transport.lines()))
        assert reported["granted"] == "t0ken"
    finally:
        await host.aclose()
        await rig.stop()


async def test_a_wrong_token_is_refused_before_anything_starts(tmp_path: Path) -> None:
    rig = await _serve(tmp_path)
    profile, library = _dirs(tmp_path)
    host = RemoteHost(url=rig.url, token="wrong", library_root=library)
    try:
        with pytest.raises(PiHostError, match="refused the connection"):
            await host.open(_request(profile, library, skills=()))
    finally:
        await host.aclose()
        await rig.stop()


async def test_a_host_that_is_not_there_says_so(tmp_path: Path) -> None:
    profile, library = _dirs(tmp_path)
    host = RemoteHost(
        url=f"http://127.0.0.1:{_free_port()}", token="shared", library_root=library
    )
    try:
        with pytest.raises(PiHostError, match="not reachable"):
            await host.open(_request(profile, library, skills=()))
    finally:
        await host.aclose()


async def test_a_host_serving_another_agent_refuses_the_spawn(tmp_path: Path) -> None:
    rig = await _serve(tmp_path, agent="developer")
    profile, library = _dirs(tmp_path)
    host = RemoteHost(url=rig.url, token="shared", library_root=library)
    try:
        with pytest.raises(PiHostError, match="refused the spawn"):
            await host.open(_request(profile, library, skills=()))
    finally:
        await host.aclose()
        await rig.stop()


async def test_health_answers_without_a_token(tmp_path: Path) -> None:
    rig = await _serve(tmp_path)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(rig.url + host_wire.HEALTH_PATH) as response:
                body = await response.json()
        assert body[host_wire.KEY_AGENT] == "assistant"
        assert body[host_wire.KEY_VERSION] == host_wire.PROTOCOL_VERSION
    finally:
        await rig.stop()


async def test_closing_the_session_stops_the_process(tmp_path: Path) -> None:
    rig = await _serve(tmp_path)
    profile, library = _dirs(tmp_path)
    host = RemoteHost(url=rig.url, token="shared", library_root=library)
    try:
        transport = await host.open(_request(profile, library, skills=()))
        await transport.send('"one"\n')
        assert json.loads(await anext(transport.lines())) == {"echo": "one"}
        await transport.aclose()
        # The host has to notice and stop the runtime; give it a moment, then
        # assert the socket really is gone rather than half-open.
        await asyncio.sleep(0.2)
        with pytest.raises(BrokenPipeError):
            await transport.send('"two"\n')
    finally:
        await host.aclose()
        await rig.stop()
