"""ProfileReloader: re-read profiles, rebuild each flow's profile via the
injected factory, drop the agent's sessions, resync the registry — and leave the
running config untouched when an edit is broken.

The reloader is runtime-agnostic: profile building (pi mapping, manifest, env)
is the injected ``build_profile``; here it is a thin wrapper over the store, and
the env/manifest specifics are exercised by test_main_wiring instead.
"""

from pathlib import Path
from typing import cast

from crucible.ports.agent import AgentRuntime, AgentSpec
from crucible.ports.chat.gateway import AgentIdentity, Gateway
from crucible.flows.agent_flow import AgentFlow
from crucible.profiles import FsProfileStore
from impi.registry import RegistryService
from crucible.reloader import ProfileReloader
from crucible.runtimes.pi.profiles import PiProfile, build_pi_profile
from crucible.store.base import SessionStore
from crucible.unit import AgentUnit

ASSISTANT_YAML = """\
name: assistant
role: personal-assistant
runtime:
  provider: openai-codex
  model: gpt-5.5
  tools: [list_agents]
"""


class FakeRuntime:
    """Only the reload surface: record which agents got their sessions dropped."""

    def __init__(self) -> None:
        self.dropped: list[str] = []

    async def drop_agent_sessions(self, agent: str) -> int:
        self.dropped.append(agent)
        return 2  # pretend two idle sessions respawn


class FakeRegistry:
    def __init__(self) -> None:
        self.syncs: list[list[str]] = []

    async def sync(self, specs, identities) -> None:
        self.syncs.append([s.name for s in specs])


def _pi_profile(unit: AgentUnit) -> PiProfile:
    # The flow holds the AgentProfile port; narrow to the pi concrete to read flags.
    profile = unit.flow._profile
    assert isinstance(profile, PiProfile)
    return profile


def _write_yaml(repo: Path, name: str, body: str) -> None:
    agent_dir = repo / "agents" / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "agent.yaml").write_text(body, encoding="utf-8")


def _build(tmp_path: Path):
    repo = tmp_path / "agents-repo"
    _write_yaml(repo, "assistant", ASSISTANT_YAML)

    profiles = FsProfileStore(str(repo), default_timeout=60.0)
    runtime = FakeRuntime()
    registry = FakeRegistry()

    built: list[str] = []

    def build_profile(spec: AgentSpec) -> PiProfile:
        built.append(spec.name)
        return build_pi_profile(spec)

    spec = profiles.get("assistant")
    flow = AgentFlow(
        cast(AgentRuntime, runtime), build_profile(spec), cast(SessionStore, None),
        agent_name="assistant",
    )
    built.clear()  # ignore the initial build; assert only on reloads
    unit = AgentUnit(spec=spec, flow=flow, gateway=cast(Gateway, None))
    reloader = ProfileReloader(
        profiles=profiles,
        runtime=cast(AgentRuntime, runtime),
        registry=cast(RegistryService, registry),
        units=[unit],
        build_profile=build_profile,
    )
    identities = {"assistant": AgentIdentity(user_id="uA", username="r42-assistant")}
    return repo, reloader, unit, runtime, registry, built, identities


async def test_reload_rebuilds_profile_drops_sessions_and_resyncs(tmp_path: Path) -> None:
    repo, reloader, unit, runtime, registry, built, identities = _build(tmp_path)

    _write_yaml(repo, "assistant", ASSISTANT_YAML.replace("gpt-5.5", "gpt-6"))
    await reloader.reload(identities)

    assert built == ["assistant"]  # profile rebuilt via the injected factory
    assert _pi_profile(unit).model == "gpt-6"  # next turn uses the new model
    assert unit.spec.model == "gpt-6"  # spec updated for the registry
    assert runtime.dropped == ["assistant"]  # live sessions dropped to respawn
    assert registry.syncs == [["assistant"]]


async def test_reload_keeps_running_config_when_an_edit_is_broken(tmp_path: Path) -> None:
    repo, reloader, unit, runtime, registry, built, identities = _build(tmp_path)

    # name must match its directory: this makes FsProfileStore.reload() raise.
    _write_yaml(repo, "assistant", ASSISTANT_YAML.replace("name: assistant", "name: wrong"))
    await reloader.reload(identities)

    assert built == []  # nothing rebuilt
    assert _pi_profile(unit).model == "gpt-5.5"  # untouched
    assert runtime.dropped == []  # no sessions dropped
    assert registry.syncs == []  # no resync


async def test_reload_keeps_old_config_for_a_vanished_agent(tmp_path: Path) -> None:
    repo, reloader, unit, runtime, registry, built, identities = _build(tmp_path)

    # A second agent keeps the repo loadable; the reloaded unit's agent is gone.
    _write_yaml(repo, "developer", ASSISTANT_YAML.replace("assistant", "developer"))
    (repo / "agents" / "assistant" / "agent.yaml").unlink()
    await reloader.reload(identities)

    assert built == []  # the vanished agent is never rebuilt
    assert _pi_profile(unit).model == "gpt-5.5"  # kept — agent no longer in repo
    assert runtime.dropped == []  # nothing dropped for the vanished agent
    assert registry.syncs == [["assistant"]]  # still resyncs from the kept spec
