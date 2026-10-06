"""AgentProcessScopes: which level an agent's process runs at, and the paths
the application hands it."""

import stat
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
        tmp_path, confinement_readable=(Path("/srv/checkout"),),
        confinement_writable=(tmp_path / "cache",),
    )
    scope = await _scopes(tmp_path, {"assistant": spec}).scope_for("assistant", "assistant--c1")
    assert scope is not None
    assert spec.profile_dir in scope.readable and tmp_path / "skills" in scope.readable
    assert Path("/srv/checkout") in scope.readable
    assert tmp_path / "files" / "assistant" in scope.writable and tmp_path / "cache" in scope.writable
    tmp, home = Path(scope.env["TMPDIR"]), Path(scope.env["HOME"])
    assert tmp in scope.writable and home in scope.writable
    for path in (tmp, home):
        assert path.is_dir() and stat.S_IMODE(path.stat().st_mode) == 0o700
    assert tmp_path / "agents" not in scope.readable  # not every profile: its own


async def test_an_unknown_agent_gets_nothing_beyond_its_scratch_space(tmp_path: Path) -> None:
    scope = await _scopes(tmp_path, {}).scope_for("ghost", None)
    assert scope is not None and scope.readable == (tmp_path / "skills",)
