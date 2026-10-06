"""AgentProcessScopes: which level an agent's process runs at, and the paths
the application hands it."""

import os
import stat
import time
from pathlib import Path

from crucible.ports.agent.spec import AgentSpec
from crucible.process_scopes import AgentProcessScopes


def _spec(tmp_path: Path, name: str = "assistant", **over) -> AgentSpec:
    profile = tmp_path / "agents" / name
    profile.mkdir(parents=True, exist_ok=True)
    return AgentSpec(name=name, display_name=name, role="r", description="", profile_dir=profile, **over)


def _scopes(tmp_path: Path, specs: dict[str, AgentSpec], **over) -> AgentProcessScopes:
    return AgentProcessScopes(
        specs.get, scratch_root=tmp_path / "scratch",
        files_dir_for=lambda name: tmp_path / "files" / name,
        files_dir_env="AGENT_FILES_DIR",
        skills_root=tmp_path / "skills", **over,
    )


def test_the_level_comes_from_the_profile_then_the_application_then_the_default(tmp_path: Path) -> None:
    specs = {
        "plain": _spec(tmp_path, "plain"),
        "free": _spec(tmp_path, "free", confinement="off"),
        "support": _spec(tmp_path, "support"),
        "tight": _spec(tmp_path, "tight", confinement="agent"),
    }
    scopes = _scopes(tmp_path, specs, unconfined_by_default=frozenset({"support", "tight"}))
    assert scopes.level_for("plain") == "conversation"  # the default
    assert scopes.level_for("free") == "off"  # the profile says so
    assert scopes.level_for("support") == "off"  # part of the application
    assert scopes.level_for("tight") == "agent"  # the profile wins over that too


async def test_an_agent_at_level_off_runs_unconfined(tmp_path: Path) -> None:
    scopes = _scopes(tmp_path, {"free": _spec(tmp_path, "free", confinement="off")})
    assert await scopes.scope_for("free", "free--c1") is None


async def test_a_confined_agent_reads_its_profile_and_skills_and_writes_its_own_paths(tmp_path: Path) -> None:
    spec = _spec(
        tmp_path, confinement="agent", confinement_readable=(Path("/srv/checkout"),),
        confinement_writable=(tmp_path / "cache",),
    )
    scope = await _scopes(tmp_path, {"assistant": spec}).scope_for("assistant", "assistant--c1")
    assert scope is not None
    assert spec.profile_dir in scope.readable and tmp_path / "skills" in scope.readable
    assert Path("/srv/checkout") in scope.readable
    assert tmp_path / "files" / "assistant" in scope.writable and tmp_path / "cache" in scope.writable
    tmp, home = Path(scope.env["TMPDIR"]), Path(scope.env["HOME"])
    assert tmp in scope.writable and home in scope.writable
    assert tmp.parent == tmp_path / "scratch" / "assistant"  # the agent's, shared
    for path in (tmp, home):
        assert path.is_dir() and stat.S_IMODE(path.stat().st_mode) == 0o700
    assert scope.env["AGENT_FILES_DIR"] == str(tmp_path / "files" / "assistant")
    assert tmp_path / "agents" not in scope.readable  # not every profile: its own


async def test_a_process_confined_to_its_conversation_writes_that_conversations_paths_alone(
    tmp_path: Path,
) -> None:
    async def conversation_files(agent: str, session_id: str) -> Path | None:
        return tmp_path / "files" / agent / session_id.split("--")[1] if session_id != "assistant--gone" else None

    spec = _spec(tmp_path, confinement_writable=(tmp_path / "cache",))
    scopes = _scopes(tmp_path, {"assistant": spec}, conversation_files_dir_for=conversation_files)
    first = await scopes.scope_for("assistant", "assistant--c1")
    second = await scopes.scope_for("assistant", "assistant--c2")
    assert first is not None and second is not None

    own = tmp_path / "files" / "assistant" / "c1"
    assert own in first.writable and own.is_dir() and stat.S_IMODE(own.stat().st_mode) == 0o700
    assert first.env["AGENT_FILES_DIR"] == str(own)
    assert tmp_path / "files" / "assistant" not in first.writable  # not the agent's whole tree
    assert tmp_path / "cache" in first.writable  # declared paths still apply
    assert Path(first.env["TMPDIR"]).parent == tmp_path / "scratch" / "assistant" / "assistant--c1"
    assert not set(first.writable) & set(second.writable) - {tmp_path / "cache"}

    # A conversation the application does not know: scratch space, nothing more,
    # and the files variable points somewhere the process may write.
    lost = await scopes.scope_for("assistant", "assistant--gone")
    assert lost is not None and lost.env["AGENT_FILES_DIR"] == lost.env["TMPDIR"]


async def test_a_memoryless_run_gets_scratch_of_its_own_and_stale_scratch_is_cleared(tmp_path: Path) -> None:
    scopes = _scopes(tmp_path, {"assistant": _spec(tmp_path)})
    first = await scopes.scope_for("assistant", None)
    second = await scopes.scope_for("assistant", None)
    talk = await scopes.scope_for("assistant", "assistant--c1")
    assert first is not None and second is not None and talk is not None
    one, two = Path(first.env["TMPDIR"]).parent, Path(second.env["TMPDIR"]).parent
    assert one != two and one.name.startswith("run-")
    assert first.env["AGENT_FILES_DIR"] == first.env["TMPDIR"]

    old = time.time() - 2 * 24 * 3600
    conversation = Path(talk.env["TMPDIR"]).parent
    for path in (one, conversation):
        os.utime(path, (old, old))
    agent_level = await _scopes(
        tmp_path, {"assistant": _spec(tmp_path, confinement="agent")}
    ).scope_for("assistant", None)
    assert agent_level is not None
    os.utime(Path(agent_level.env["TMPDIR"]), (old, old))
    await scopes.scope_for("assistant", None)
    assert not one.exists() and not conversation.exists() and two.exists()
    assert Path(agent_level.env["TMPDIR"]).exists()  # the agent level's own is not a run's

    # A conversation that is talking is not stale, however old its directory.
    talk = await scopes.scope_for("assistant", "assistant--c1")
    assert talk is not None
    kept = Path(talk.env["TMPDIR"]) / "draft"
    kept.write_text("x")
    os.utime(kept.parent.parent, (old, old))
    await scopes.scope_for("assistant", "assistant--c1")
    assert kept.is_file()


async def test_an_unknown_agent_gets_nothing_beyond_its_scratch_space(tmp_path: Path) -> None:
    scope = await _scopes(tmp_path, {}).scope_for("ghost", None)
    assert scope is not None and scope.readable == (tmp_path / "skills",)
