"""The broker as the composition root assembles it (impi/app.py).

Two things are worth a test rather than a reading: an engine that was not asked
for a broker must not grow one, and an engine that was must still start when the
store is locked — being locked is the normal state after a restart.
"""

from pathlib import Path

import pytest

from crucible.config import SecretsSettings
from crucible.secrets.ports import BackendStatus, UnlockMaterial
from impi.app import _read_key, _unlock_secrets, build_app
from impi.config import ImpiSettings as Settings

AGENT_YAML = """\
name: assistant
role: personal-assistant
"""


def _settings(tmp_path: Path, **over) -> Settings:
    agent_dir = tmp_path / "agents-dir" / "agents" / "assistant"
    agent_dir.mkdir(parents=True)
    (agent_dir / "agent.yaml").write_text(AGENT_YAML, encoding="utf-8")
    return Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        dotenv_path=str(tmp_path / "no-such.env"),
        agents_path=str(tmp_path / "agents-dir"),
        data_dir=str(tmp_path / "data"),
        mattermost_url="http://localhost:8065",
        mattermost_token="token",
        **over,
    )


def test_the_broker_is_off_unless_it_was_asked_for(tmp_path: Path) -> None:
    app = build_app(_settings(tmp_path))
    assert app.secrets is None
    # And nothing on the tool server answers for it either.
    assert app.tool_server is not None
    assert app.tool_server._secret_svc is None


def test_enabling_it_wires_a_broker_onto_the_tool_server(tmp_path: Path) -> None:
    app = build_app(
        _settings(
            tmp_path,
            secrets_enabled=True,
            secrets_vault_addr="http://vault:8200",
            secrets_approvers="roman",
        )
    )
    assert app.secrets is not None
    assert app.tool_server is not None
    assert app.tool_server._secret_svc is app.secrets


async def test_a_configured_engine_starts_locked_and_says_so(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No key files: the store stays shut until a human unlocks it, and the
    engine has to come up anyway or the deployment is unrecoverable."""
    broker = _FakeBroker()
    with caplog.at_level("INFO"):
        await _unlock_secrets(broker, _secrets_config())  # type: ignore[arg-type]
    assert broker.unlocked == []
    assert "impi secret unlock" in caplog.text


async def test_key_files_unlock_it_without_a_human(tmp_path: Path) -> None:
    unseal = tmp_path / "unseal.key"
    unseal.write_text("the-unseal-key\n", encoding="utf-8")  # a trailing newline is usual
    secret_id = tmp_path / "secret-id"
    secret_id.write_text("the-secret-id", encoding="utf-8")
    broker = _FakeBroker()

    await _unlock_secrets(
        broker,  # type: ignore[arg-type]
        _secrets_config(unseal_key_file=str(unseal), secret_id_file=str(secret_id)),
    )
    assert broker.unlocked == [
        UnlockMaterial(unseal_key="the-unseal-key", auth_secret="the-secret-id")
    ]


async def test_a_backend_that_will_not_open_does_not_stop_the_engine(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    key = tmp_path / "unseal.key"
    key.write_text("wrong", encoding="utf-8")
    broker = _FakeBroker(boom=True)
    with caplog.at_level("WARNING"):
        await _unlock_secrets(broker, _secrets_config(unseal_key_file=str(key)))  # type: ignore[arg-type]
    assert "could not unlock" in caplog.text


def test_a_missing_key_file_is_a_warning_not_a_crash(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("WARNING"):
        assert _read_key(str(tmp_path / "nope")) == ""
    assert "cannot read" in caplog.text
    assert _read_key("") == ""


def _secrets_config(**over) -> SecretsSettings:
    base = dict(
        enabled=True, vault_addr="http://vault:8200", vault_mount="impi",
        role_id="role", secret_id_file="", unseal_key_file="", approvers="roman",
        approval_channel="", approval_timeout_s=120.0, max_grant_s=3600,
    )
    base.update(over)
    return SecretsSettings(**base)  # type: ignore[arg-type]


class _FakeBroker:
    def __init__(self, *, boom: bool = False) -> None:
        self.unlocked: list[UnlockMaterial] = []
        self.boom = boom

    async def unlock(self, material: UnlockMaterial) -> BackendStatus:
        if self.boom:
            raise RuntimeError("vault said no")
        self.unlocked.append(material)
        return BackendStatus(reachable=True, sealed=False, authenticated=True)
