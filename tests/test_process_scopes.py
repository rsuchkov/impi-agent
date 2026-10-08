"""AgentProcessScopes: which level an agent's process runs at, and the paths
the application hands it."""

import os
import stat
import time
from pathlib import Path

import pytest

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


async def test_a_memoryless_runs_scratch_goes_when_its_process_ends(tmp_path: Path) -> None:
    scopes = _scopes(tmp_path, {"assistant": _spec(tmp_path)})
    first = await scopes.scope_for("assistant", None)
    second = await scopes.scope_for("assistant", None)
    assert first is not None and second is not None
    one, two = Path(first.env["TMPDIR"]).parent, Path(second.env["TMPDIR"]).parent
    assert one != two and one.name.startswith("run-")
    assert first.env["AGENT_FILES_DIR"] == first.env["TMPDIR"]
    scopes.release("assistant", first)
    assert not one.exists() and two.exists()


async def test_a_conversations_scratch_is_kept_a_day_after_its_last_process(tmp_path: Path) -> None:
    scopes = _scopes(tmp_path, {"assistant": _spec(tmp_path)})
    talk = await scopes.scope_for("assistant", "assistant--c1")
    assert talk is not None
    scratch = Path(talk.env["TMPDIR"]).parent
    kept = Path(talk.env["HOME"]) / ".gitconfig"
    kept.write_text("x")
    # The process lived for days: the day it is kept counts from its end.
    old = time.time() - 2 * 24 * 3600
    os.utime(scratch, (old, old))
    scopes.release("assistant", talk)
    assert kept.is_file()
    # The conversation's next process finds its HOME as it was left.
    again = await scopes.scope_for("assistant", "assistant--c1")
    assert again is not None and again.env["HOME"] == talk.env["HOME"] and kept.is_file()
    scopes.release("assistant", again)
    # A day after the last one ended, the agent's next release clears it.
    os.utime(scratch, (old, old))
    run = await scopes.scope_for("assistant", None)
    assert run is not None
    scopes.release("assistant", run)
    assert not scratch.exists()


async def test_scratch_in_use_is_never_cleared_however_old(tmp_path: Path) -> None:
    """A conversation that keeps talking keeps one process for days; its scratch
    space was handed out long ago and is still in use. One a stopped
    application left behind is not, and goes by age."""
    scopes = _scopes(tmp_path, {"assistant": _spec(tmp_path)})
    busy = await scopes.scope_for("assistant", "assistant--busy")
    twice = await scopes.scope_for("assistant", "assistant--twice")
    second = await scopes.scope_for("assistant", "assistant--twice")
    assert busy is not None and twice is not None and second is not None
    left = tmp_path / "scratch" / "assistant" / "assistant--left"
    left.mkdir()
    scopes.release("assistant", twice)  # one of its two processes ended
    old = time.time() - 2 * 24 * 3600
    for path in (Path(busy.env["TMPDIR"]).parent, Path(twice.env["TMPDIR"]).parent, left):
        os.utime(path, (old, old))
    run = await scopes.scope_for("assistant", None)
    assert run is not None
    scopes.release("assistant", run)
    assert Path(busy.env["TMPDIR"]).is_dir() and Path(twice.env["TMPDIR"]).is_dir()
    assert not left.exists()


async def test_the_agent_levels_shared_scratch_is_never_taken_back(tmp_path: Path) -> None:
    scopes = _scopes(tmp_path, {"assistant": _spec(tmp_path, confinement="agent")})
    shared = await scopes.scope_for("assistant", None)
    assert shared is not None
    old = time.time() - 2 * 24 * 3600
    os.utime(Path(shared.env["TMPDIR"]), (old, old))
    scopes.release("assistant", shared)
    # Even when a conversation-level neighbour sweeps the same agent's space.
    neighbours = _scopes(tmp_path, {"assistant": _spec(tmp_path)})
    run = await neighbours.scope_for("assistant", None)
    assert run is not None
    neighbours.release("assistant", run)
    assert Path(shared.env["TMPDIR"]).is_dir() and Path(shared.env["HOME"]).is_dir()


async def test_an_unknown_agent_gets_nothing_beyond_its_scratch_space(tmp_path: Path) -> None:
    scope = await _scopes(tmp_path, {}).scope_for("ghost", None)
    assert scope is not None and scope.readable == (tmp_path / "skills",)


async def test_a_scope_that_fails_before_it_is_handed_out_holds_nothing(tmp_path: Path) -> None:
    """The store that says which conversation a session serves may be down: the
    caller gets an error and no scope, so nothing will ever release it."""

    async def store_down(agent: str, session_id: str) -> Path | None:
        raise RuntimeError("store unreachable")

    scopes = _scopes(tmp_path, {"assistant": _spec(tmp_path)}, conversation_files_dir_for=store_down)
    with pytest.raises(RuntimeError):
        await scopes.scope_for("assistant", "assistant--c1")
    assert scopes._in_use == {}
    # So its scratch goes by age like any other.
    scratch = tmp_path / "scratch" / "assistant" / "assistant--c1"
    old = time.time() - 2 * 24 * 3600
    os.utime(scratch, (old, old))
    run = await scopes.scope_for("assistant", None)  # a memoryless run asks no store
    assert run is not None
    scopes.release("assistant", run)
    assert not scratch.exists()
