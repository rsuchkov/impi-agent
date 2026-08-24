"""What the engine does about secrets: nothing at all.

There used to be a settings block, a per-agent identity to assemble and a broker
to build. All three moved out — the broker to its own container, the client and
the operator CLI to their own package — and what is left in the engine is one
generic fact: every agent is told its own name. A tool that needs an identity
derives it from that; the engine never learns the protocol.

These pin the absence, because an absence is exactly what a later change
restores by accident.
"""

from pathlib import Path

from crucible.tools.wiring import AGENT_NAME_ENV
from impi.app import build_app
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


def _agent_env(tmp_path: Path, **over) -> dict[str, str]:
    app = build_app(_settings(tmp_path, **over))
    return dict(getattr(app.units[0].flow._profile, "env", {}))


def test_an_agent_is_told_its_own_name(tmp_path: Path) -> None:
    """The one thing the engine says about an agent to whatever runs inside it.
    A fact, not a credential — anything that must be proved is proved with a
    token or a certificate."""
    assert _agent_env(tmp_path)[AGENT_NAME_ENV] == "assistant"


def test_the_engine_says_nothing_about_secrets(tmp_path: Path) -> None:
    """Not the address, not a certificate path, not a hint that a broker exists.
    Where to ask comes from the container's own environment, and which identity
    to present the tool works out from the name above."""
    env = _agent_env(tmp_path)
    assert not any("SECRET" in key or "BROKER" in key for key in env)


def test_no_broker_is_built_in_the_engine(tmp_path: Path) -> None:
    app = build_app(_settings(tmp_path))
    assert not hasattr(app, "secrets")
    # And the tool server has no route to one: those moved with it.
    assert app.tool_server is not None
    assert not hasattr(app.tool_server, "_secret_svc")


def test_the_engine_settings_carry_no_broker_knobs(tmp_path: Path) -> None:
    """A settings field is how this would creep back: someone adds SECRET_BROKER_URL
    "just so the engine can tell whether the feature is on"."""
    settings = _settings(tmp_path)
    assert not [name for name in type(settings).model_fields if "secret" in name.lower()]
