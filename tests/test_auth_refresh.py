"""The engine keeps the runtime's OAuth login fresh, so a confined process —
which may only read the credential — never has to refresh it itself."""

import json
import os
import shutil
import stat
from pathlib import Path

import pytest

from crucible.runtimes.pi import auth_refresh
from crucible.runtimes.pi.auth_refresh import (
    AuthRefresher,
    oauth_providers,
    runtime_package_dir,
)

OAUTH = {"openai-codex": {"type": "oauth", "access": "ey", "refresh": "rt", "expires": 1}}
RECORDER = """#!/bin/sh
printf '%s\\n' "$@" > "$(dirname "$0")/argv"
exit ${FAKE_EXIT:-0}
"""


def _executable(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


@pytest.fixture
def stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A runtime installed as a package (its entry point a few levels below its
    package.json), a node that records what it was asked, and an agent dir."""
    package = tmp_path / "lib" / "node_modules" / "runtime"
    (package / "package.json").parent.mkdir(parents=True)
    (package / "package.json").write_text("{}")
    entry = _executable(package / "dist" / "bundle" / "cli.js", "#!/bin/sh\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "pi").symlink_to(entry)
    _executable(tmp_path / "node" / "node", RECORDER)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "agent"))
    (tmp_path / "agent").mkdir()
    return tmp_path


def _refresher(stage: Path) -> AuthRefresher:
    return AuthRefresher(node=str(stage / "node" / "node"), margin_s=3600)


def test_the_runtime_package_is_found_from_its_program(stage: Path) -> None:
    assert runtime_package_dir("pi") == (stage / "lib" / "node_modules" / "runtime").resolve()
    assert runtime_package_dir("no-such-program-here") is None


async def test_an_expiring_login_is_handed_to_the_runtimes_own_code(stage: Path) -> None:
    auth = stage / "agent" / "auth.json"
    auth.write_text(json.dumps(OAUTH))
    assert await _refresher(stage).refresh_once() is True
    argv = (stage / "node" / "argv").read_text().splitlines()
    assert argv == [str(auth_refresh.REFRESH_SCRIPT), str(runtime_package_dir("pi")), str(auth), "3600000"]


async def test_nothing_runs_without_a_login_that_expires(stage: Path) -> None:
    (stage / "agent" / "auth.json").write_text(json.dumps({"x": {"type": "api_key", "key": "sk"}}))
    assert await _refresher(stage).refresh_once() is True
    assert not (stage / "node" / "argv").exists()
    assert oauth_providers(stage / "agent" / "missing.json") == []


async def test_a_planted_command_is_never_handed_to_the_runtime(stage: Path, caplog) -> None:
    planted = {**OAUTH, "openai": {"type": "api_key", "key": "!id"}}
    (stage / "agent" / "auth.json").write_text(json.dumps(planted))
    assert await _refresher(stage).refresh_once() is False
    assert not (stage / "node" / "argv").exists()
    assert "command value" in caplog.text


async def test_a_failed_refresh_is_said_and_reported(stage: Path, monkeypatch, caplog) -> None:
    (stage / "agent" / "auth.json").write_text(json.dumps(OAUTH))
    monkeypatch.setenv("FAKE_EXIT", "1")
    assert await _refresher(stage).refresh_once() is False
    assert "refreshing the runtime's login failed" in caplog.text


@pytest.mark.skipif(shutil.which("node") is None, reason="no node on PATH")
async def test_the_script_is_valid_javascript() -> None:
    import asyncio

    process = await asyncio.create_subprocess_exec(
        "node", "--check", str(auth_refresh.REFRESH_SCRIPT),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await process.communicate()
    assert process.returncode == 0, err.decode()
