"""A credential the runtime would execute stops the process from starting."""

import json
from pathlib import Path

import pytest

from crucible.runtimes.pi.auth_file import executable_entries
from crucible.runtimes.pi.errors import PiHostError
from crucible.runtimes.pi.hosts.local import LocalHost
from crucible.runtimes.pi.spawn import SpawnRequest
from runtime_relay.server import _executable_entries as relay_entries

PLANTED = {"openai": {"type": "api_key", "key": "!id > /tmp/pwned; echo sk-x"}}
HONEST = {"openai-codex": {"type": "oauth", "access": "ey...", "refresh": "rt"},
          "anthropic": {"type": "api_key", "key": "sk-ant-$!literal"}}


@pytest.mark.parametrize("finder", [executable_entries, relay_entries])
def test_a_command_value_is_found_and_an_honest_file_is_not(tmp_path: Path, finder) -> None:
    path = tmp_path / "auth.json"
    path.write_text(json.dumps(PLANTED))
    assert finder(path) == ["openai.key"]
    path.write_text(json.dumps(HONEST))
    assert finder(path) == []
    assert finder(tmp_path / "missing.json") == []
    path.write_text("{not json")
    assert finder(path) == []


async def test_the_engine_does_not_start_a_runtime_over_a_planted_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    (agent_dir / "auth.json").write_text(json.dumps(PLANTED))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))
    started = []

    async def spawn(*args, **kwargs):
        started.append(args)

    monkeypatch.setattr("crucible.runtimes.pi.hosts.local.SubprocessTransport.spawn", spawn)
    with pytest.raises(PiHostError, match="command value"):
        await LocalHost().open(SpawnRequest(agent="assistant", profile_dir=tmp_path))
    assert started == []
