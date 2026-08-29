"""Describing a deployment where every agent has a container of its own.

The output of this module is what a container runtime is handed, so the tests
are about the promises that arrangement makes: an agent's directory is mounted
where the engine has it (so a filename means one thing), its sessions and its
files are volumes nobody else mounts, its network has nobody else on it, and the
build footer that drops back from root is not something an operator's fragment
can quietly remove.
"""

from pathlib import Path

import pytest
import yaml

from crucible.ports.agent import AgentSpec
from impi.agent_containers import (
    AgentBuild,
    Deployment,
    RenderError,
    ensure_token,
    read_build,
    render_compose,
    render_dockerfile,
    sync,
)


def _spec(root: Path, name: str, manifest: str = "", include: str = "") -> AgentSpec:
    profile = root / "agents" / name
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "agent.yaml").write_text(
        manifest or f"name: {name}\nrole: does things\n", encoding="utf-8"
    )
    if include:
        (profile / "Dockerfile.include").write_text(include, encoding="utf-8")
    return AgentSpec(
        name=name, display_name=name, role="does things", description="",
        profile_dir=profile,
    )


# --- what an agent asked for ---------------------------------------------------


def test_an_agent_with_no_extras_asks_for_nothing(tmp_path: Path) -> None:
    build = read_build(_spec(tmp_path, "assistant"))
    assert not build.customized
    assert build.apt == () and build.include == ""


def test_packages_and_a_fragment_are_read_from_the_agents_own_directory(
    tmp_path: Path,
) -> None:
    spec = _spec(
        tmp_path,
        "researcher",
        manifest=(
            "name: researcher\nrole: reads\n"
            "runtime:\n"
            "  tools: [read, bash]\n"
            "  packages:\n"
            "    apt: [openjdk-17-jre-headless]\n"
            "    npm: [some-cli]\n"
        ),
        include="RUN echo custom\n",
    )
    build = read_build(spec)
    assert build.apt == ("openjdk-17-jre-headless",)
    assert build.npm == ("some-cli",)
    assert build.include.strip() == "RUN echo custom"
    assert build.customized


def test_a_shell_fragment_smuggled_into_a_package_list_is_refused(
    tmp_path: Path,
) -> None:
    spec = _spec(
        tmp_path,
        "sneaky",
        manifest="runtime:\n  packages:\n    apt: ['curl && rm -rf /']\n",
    )
    with pytest.raises(RenderError, match="Dockerfile.include"):
        read_build(spec)


def test_packages_must_be_lists(tmp_path: Path) -> None:
    spec = _spec(tmp_path, "wrong", manifest="runtime:\n  packages:\n    apt: openjdk\n")
    with pytest.raises(RenderError, match="list of names"):
        read_build(spec)


# --- the per-agent Dockerfile ---------------------------------------------------


def test_the_footer_puts_the_user_back_after_the_agents_own_steps() -> None:
    text = render_dockerfile(
        AgentBuild(name="researcher", apt=("openjdk-17-jre-headless",), include="USER root\nRUN whoami")
    )
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    # Whatever the fragment did, the last word is the engine's: the runtime does
    # not run as root in its own container.
    assert lines[-1] == 'CMD ["runtime-relay", "serve"]'
    assert lines[-2] == 'USER ${IMPI_UID}:${IMPI_GID}'
    assert "openjdk-17-jre-headless" in text
    assert text.startswith("# GENERATED")


def test_an_agent_with_no_extras_still_gets_a_buildable_file() -> None:
    text = render_dockerfile(AgentBuild(name="assistant"))
    assert "FROM localhost/impi-agent:local" in text
    assert 'CMD ["runtime-relay", "serve"]' in text


# --- the compose file -----------------------------------------------------------


def _rendered(**kwargs: object) -> str:
    builds = [AgentBuild(name="assistant"), AgentBuild(name="researcher")]
    return render_compose(Deployment(builds=builds, **kwargs))  # type: ignore[arg-type]


def test_each_agent_gets_a_network_and_volumes_nobody_else_mounts() -> None:
    text = _rendered()
    for name in ("assistant", "researcher"):
        assert f"  agent-{name}:" in text
        assert f"agent-{name}-sessions:/app/sessions" in text
        assert f"agent-{name}-files:/app/files/{name}" in text
        assert f"    networks: [agent-{name}]" in text
    # An agent's own service must not mount the other agent's anything.
    block = text.split("  agent-researcher:")[1].split("  impi:")[0]
    assert "assistant" not in block


def test_the_agents_directory_is_mounted_read_only_at_the_engines_own_path() -> None:
    text = _rendered()
    assert (
        "- ${IMPI_AGENTS_DIR:?set in compose.env}/agents/assistant:"
        "/app/agents/agents/assistant:ro,z" in text
    )
    # The same path on both sides is what lets a filename mean one thing.
    assert "RUNTIME_RELAY_PROFILE_DIR: /app/agents/agents/assistant" in text


def test_the_engine_joins_every_agents_network_and_keeps_the_default_one() -> None:
    text = _rendered()
    assert "    networks: [default, agent-assistant, agent-researcher]" in text
    # And stops binding the tool server where only its own children could reach it.
    assert "TOOL_SERVER_HOST: 0.0.0.0" in text
    assert "TOOL_PUBLIC_URL: http://impi:8422" in text
    assert 'AGENT_HOSTS_ENABLED: "true"' in text


def test_the_broker_answers_on_every_network_an_agent_might_ask_from() -> None:
    text = _rendered(with_vault=True)
    vault = text.split("  vault:")[1].split("networks:\n  default:")[0]
    assert vault.count("aliases: [ward]") == 3  # default + one per agent


def test_without_the_secret_store_the_broker_is_never_named() -> None:
    assert "vault:" not in _rendered()


def test_the_browser_reaches_the_agents_rather_than_the_engine() -> None:
    text = _rendered(with_browser=True)
    assert "  browser:\n    networks: [browser, agent-assistant, agent-researcher]" in text


def test_every_network_a_service_names_is_declared() -> None:
    # podman-compose validates the whole map as soon as one network is named, so
    # a missing entry fails the merge rather than one service.
    text = _rendered(with_browser=True, with_vault=True)
    declarations = text.split("networks:\n")[-1]
    for network in ("default", "browser", "agent-assistant", "agent-researcher"):
        assert f"  {network}:" in declarations


def test_the_extensions_directory_is_mounted_only_when_there_is_one() -> None:
    assert "_extensions" not in _rendered()
    assert "RUNTIME_RELAY_EXTENSIONS_DIR: /app/agents/_extensions" in _rendered(extensions_dir=True)


def test_the_file_says_it_is_generated_and_where_hand_edits_belong() -> None:
    text = _rendered()
    assert text.startswith("# GENERATED")
    assert "compose.d/" in text


# --- tokens and the whole sync --------------------------------------------------


def test_a_token_is_created_once_and_then_kept(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    conf = tmp_path / "conf"
    first = ensure_token(dotenv, conf, "assistant")
    second = ensure_token(dotenv, conf, "assistant")
    # Rotating on every sync would leave a running host answering with a token
    # the engine has already forgotten.
    assert first == second
    env_file = conf / "agents" / "assistant.env"
    assert env_file.read_text(encoding="utf-8").strip() == f"RUNTIME_RELAY_TOKEN={first}"
    assert oct(env_file.stat().st_mode)[-3:] == "600"
    # And the engine's side knows the same secret, under its own key.
    assert f"AGENTS_HOST_TOKEN__ASSISTANT={first}" in dotenv.read_text(encoding="utf-8")


def test_a_hyphenated_agent_gets_the_key_the_engine_looks_up(tmp_path: Path) -> None:
    ensure_token(tmp_path / ".env", tmp_path / "conf", "greek-teacher")
    assert "AGENTS_HOST_TOKEN__GREEK_TEACHER=" in (tmp_path / ".env").read_text(
        encoding="utf-8"
    )


def test_sync_writes_everything_a_deployment_needs(tmp_path: Path) -> None:
    agents_root = tmp_path / "profiles"
    specs = [
        _spec(agents_root, "assistant"),
        _spec(
            agents_root,
            "researcher",
            manifest="runtime:\n  packages:\n    apt: [ffmpeg]\n",
        ),
    ]
    conf = tmp_path / "conf"
    notes = sync(
        specs,
        dotenv_path=tmp_path / ".env",
        conf_dir=conf,
        agents_path=agents_root,
        with_vault=True,
    )

    assert (conf / "agents.compose.yaml").is_file()
    for name in ("assistant", "researcher"):
        assert (conf / "agents" / name / "Dockerfile").is_file()
        assert (conf / "agents" / f"{name}.env").is_file()
    assert "ffmpeg" in (conf / "agents" / "researcher" / "Dockerfile").read_text(
        encoding="utf-8"
    )
    assert any("extra build steps" in note for note in notes)
    assert any("no extras" in note for note in notes)


def test_sync_is_idempotent(tmp_path: Path) -> None:
    agents_root = tmp_path / "profiles"
    specs = [_spec(agents_root, "assistant")]
    conf = tmp_path / "conf"
    kwargs = {"dotenv_path": tmp_path / ".env", "conf_dir": conf, "agents_path": agents_root}
    sync(specs, **kwargs)  # type: ignore[arg-type]
    first = (conf / "agents.compose.yaml").read_text(encoding="utf-8")
    token = (conf / "agents" / "assistant.env").read_text(encoding="utf-8")
    sync(specs, **kwargs)  # type: ignore[arg-type]
    assert (conf / "agents.compose.yaml").read_text(encoding="utf-8") == first
    assert (conf / "agents" / "assistant.env").read_text(encoding="utf-8") == token


# --- the broker identity, which is the whole point ------------------------------


def _with_certs(tmp_path: Path, *names: str) -> Path:
    certs = tmp_path / "certs"
    certs.mkdir(parents=True, exist_ok=True)
    (certs / "ca.crt").write_text("ca", encoding="utf-8")
    for name in names:
        (certs / f"{name}.crt").write_text("crt", encoding="utf-8")
        (certs / f"{name}.key").write_text("key", encoding="utf-8")
    return certs


def test_an_agent_gets_its_own_identity_and_no_one_elses(tmp_path: Path) -> None:
    agents_root = tmp_path / "profiles"
    specs = [_spec(agents_root, "assistant"), _spec(agents_root, "researcher")]
    conf = tmp_path / "conf"
    sync(
        specs,
        dotenv_path=tmp_path / ".env",
        conf_dir=conf,
        agents_path=agents_root,
        with_vault=True,
        certs_dir=_with_certs(tmp_path, "assistant", "researcher"),
    )
    text = (conf / "agents.compose.yaml").read_text(encoding="utf-8")
    block = text.split("  agent-assistant:")[1].split("  agent-researcher:")[0]
    # Three files, not the directory they came from — the directory is where
    # every agent's key used to be readable by every agent.
    assert "/certs/assistant.crt:/app/conf/certs/assistant.crt:ro,z" in block
    assert "/certs/assistant.key:/app/conf/certs/assistant.key:ro,z" in block
    assert "/certs/ca.crt:/app/conf/certs/ca.crt:ro,z" in block
    assert "researcher.key" not in block
    assert "SECRET_BROKER_CERT: /app/conf/certs/assistant.crt" in block


def test_an_agent_without_a_certificate_is_left_unmounted_and_named(
    tmp_path: Path,
) -> None:
    agents_root = tmp_path / "profiles"
    specs = [_spec(agents_root, "assistant"), _spec(agents_root, "newcomer")]
    conf = tmp_path / "conf"
    notes = sync(
        specs,
        dotenv_path=tmp_path / ".env",
        conf_dir=conf,
        agents_path=agents_root,
        with_vault=True,
        certs_dir=_with_certs(tmp_path, "assistant"),
    )
    text = (conf / "agents.compose.yaml").read_text(encoding="utf-8")
    newcomer = text.split("  agent-newcomer:")[1].split("  impi:")[0]
    # A bind mount of a file that is not there is a directory on one runtime and
    # an error on the other. Neither is what anybody meant.
    assert "SECRET_BROKER_CERT" not in newcomer
    assert "certs/newcomer" not in newcomer
    assert any("impi ward cert newcomer" in note for note in notes)


def test_without_the_secret_store_no_identity_is_mounted_at_all(tmp_path: Path) -> None:
    agents_root = tmp_path / "profiles"
    conf = tmp_path / "conf"
    sync(
        [_spec(agents_root, "assistant")],
        dotenv_path=tmp_path / ".env",
        conf_dir=conf,
        agents_path=agents_root,
        certs_dir=_with_certs(tmp_path, "assistant"),
    )
    assert "SECRET_BROKER" not in (conf / "agents.compose.yaml").read_text(encoding="utf-8")


# --- it has to be a file a container runtime will read -------------------------


def test_the_generated_compose_is_valid_yaml_and_the_right_shape() -> None:
    # Text templating is what makes the file readable and commented; this is the
    # price of it. An indentation slip would otherwise surface as a failed merge
    # on somebody's host.
    parsed = yaml.safe_load(_rendered(with_vault=True, with_browser=True, extensions_dir=True))
    services = parsed["services"]
    assert set(services) == {"agent-assistant", "agent-researcher", "impi", "vault", "browser"}
    agent = services["agent-assistant"]
    assert agent["image"] == "localhost/impi-agent-assistant:local"
    assert agent["networks"] == ["agent-assistant"]
    assert agent["environment"]["RUNTIME_RELAY_AGENT"] == "assistant"
    # The healthcheck is a list, not a string the shell would have to re-split.
    assert isinstance(agent["healthcheck"]["test"], list)
    assert parsed["networks"].keys() >= {"default", "browser", "agent-assistant"}
    assert "agent-assistant-sessions" in parsed["volumes"]


def test_a_dockerfile_fragment_cannot_break_the_yaml() -> None:
    # The fragment goes into a Dockerfile, never into the compose file — worth
    # holding to, because a fragment is the one input here a person writes free-hand.
    nasty = 'RUN echo "a: b\\n  - c"   # ${IMPI_HOME} and: colons'
    text = render_compose(Deployment(builds=[AgentBuild(name="a", include=nasty)]))
    assert yaml.safe_load(text) is not None
    assert "colons" not in text


def test_rootless_gives_the_agents_the_engines_own_user_mapping() -> None:
    # The bug this guards: without it the agent's container has no keep-id
    # mapping while the engine's does, so the same volume has two different
    # owners — the agent cannot write what the engine reads, and a 0600
    # certificate issued to it is unreadable by it.
    assert "userns_mode" not in _rendered()
    text = _rendered(rootless=True)
    assert text.count('userns_mode: "keep-id:uid=${IMPI_UID:-1000},gid=${IMPI_GID:-1000}"') == 2


def test_the_engines_own_mapping_is_left_to_its_own_overlay() -> None:
    # compose.podman.yaml already maps the engine; saying it again here would be
    # this file deciding something that is not its to decide.
    engine = _rendered(rootless=True).split("  impi:")[1]
    assert "userns_mode" not in engine
