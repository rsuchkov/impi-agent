import asyncio
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from crucible.ports.agent.runtime import PromptImage
from crucible.runtimes.pi.errors import PiProcessError, PiTimeout
from crucible.runtimes.pi.profiles import PiProfile
from crucible.runtimes.pi.runtime import PiRuntime, SessionFactory, _safe_session_id
from crucible.runtimes.pi.session import PiResult, PiRpcSession


class FakeSession:
    def __init__(self, *, result: PiResult | None = None, prompt_error: Exception | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.images: list[PromptImage] = []
        self.started = False
        self.closed = False
        self.busy = False
        self._result = result or PiResult(text="ok")
        self._prompt_error = prompt_error

    def start(self) -> None:
        self.started = True

    async def prompt(
        self, message: str, *, timeout: float, images: Sequence[PromptImage] = ()
    ) -> PiResult:
        self.calls.append(("prompt", message))
        self.images = list(images)
        if self._prompt_error is not None:
            raise self._prompt_error
        return self._result

    async def follow_up(
        self, message: str, *, timeout: float, images: Sequence[PromptImage] = ()
    ) -> PiResult:
        self.calls.append(("follow_up", message))
        return self._result

    async def close(self) -> None:
        self.closed = True


def _profile(name: str = "assistant") -> PiProfile:
    return PiProfile(name=name, config_dir=Path("agents") / name, timeout=5.0)


def _runtime_with(sessions: list[FakeSession], **kwargs) -> PiRuntime:
    created: list[FakeSession] = []

    async def factory(profile, session_id, on_event, cwd=None):
        session = sessions[len(created)]
        created.append(session)
        return session

    # FakeSession is a structural stand-in for PiRpcSession; one cast at the seam.
    rt = PiRuntime(session_factory=cast(SessionFactory, factory), **kwargs)
    rt._created = created  # type: ignore[attr-defined]
    return rt


async def test_stateful_reuses_session_with_prompt_each_turn() -> None:
    session = FakeSession()
    rt = _runtime_with([session])
    profile = _profile()

    await rt.run_stateful(profile, "assistant--T1", "first")
    await rt.run_stateful(profile, "assistant--T1", "second")

    # Every turn uses prompt (the pi session keeps history); follow_up would hang
    # an idle agent, so it must NOT be used for new turns.
    assert session.calls == [("prompt", "first"), ("prompt", "second")]
    assert len(rt._created) == 1  # type: ignore[attr-defined]


async def test_stateful_different_sessions_get_different_processes() -> None:
    s1, s2 = FakeSession(), FakeSession()
    rt = _runtime_with([s1, s2])
    profile = _profile()

    await rt.run_stateful(profile, "assistant--T1", "a")
    await rt.run_stateful(profile, "assistant--T2", "b")

    assert s1.calls == [("prompt", "a")]
    assert s2.calls == [("prompt", "b")]


async def test_stateless_creates_and_closes_fresh_session() -> None:
    s1, s2 = FakeSession(), FakeSession()
    rt = _runtime_with([s1, s2])
    profile = _profile()

    await rt.run_stateless(profile, "one")
    await rt.run_stateless(profile, "two")

    assert s1.calls == [("prompt", "one")] and s1.closed
    assert s2.calls == [("prompt", "two")] and s2.closed


async def test_stateful_failure_drops_session_but_keeps_lock_object() -> None:
    bad = FakeSession(prompt_error=PiProcessError("crash"))
    good = FakeSession()
    rt = _runtime_with([bad, good])
    profile = _profile()

    with pytest.raises(PiProcessError):
        await rt.run_stateful(profile, "assistant--T1", "x")
    lock_after_drop = rt._locks.get("assistant--T1")
    assert lock_after_drop is not None  # lock survives the drop...

    await rt.run_stateful(profile, "assistant--T1", "y")

    assert good.calls == [("prompt", "y")]
    assert bad.closed
    # ...and the SAME object keeps serializing the conversation afterwards:
    # a waiter queued on it and a newcomer must never hold different locks.
    assert rt._locks.get("assistant--T1") is lock_after_drop


async def test_per_session_lock_serializes_turns() -> None:
    order: list[str] = []

    class SlowSession(FakeSession):
        async def prompt(
            self, message: str, *, timeout: float, images: Sequence[PromptImage] = ()
        ) -> PiResult:
            order.append(f"start:{message}")
            await asyncio.sleep(0.02)
            order.append(f"end:{message}")
            return PiResult(text="ok")

    session = SlowSession()
    rt = _runtime_with([session])
    profile = _profile()

    await asyncio.gather(
        rt.run_stateful(profile, "assistant--T1", "one"),
        rt.run_stateful(profile, "assistant--T1", "two"),
    )

    # No interleaving: each turn fully completes before the next starts.
    assert order in (
        ["start:one", "end:one", "start:two", "end:two"],
        ["start:two", "end:two", "start:one", "end:one"],
    )


async def test_semaphore_bounds_concurrent_sessions() -> None:
    sessions = [FakeSession() for _ in range(3)]
    rt = _runtime_with(sessions, max_concurrent_sessions=1)
    profile = _profile()

    # With a permit limit of 1, stateless runs still complete (serialized).
    await asyncio.gather(
        rt.run_stateless(profile, "a"),
        rt.run_stateless(profile, "b"),
    )
    assert all(s.closed for s in sessions[:2])


async def test_drop_agent_sessions_drops_idle_skips_busy_and_scopes_by_agent() -> None:
    a1, a2, other = FakeSession(), FakeSession(), FakeSession()
    rt = _runtime_with([a1, a2, other])

    await rt.run_stateful(_profile(), "assistant--T1", "x")
    await rt.run_stateful(_profile(), "assistant--T2", "y")
    await rt.run_stateful(_profile("developer"), "developer--T1", "z")
    a2.busy = True  # in-flight turn: the reload must leave it running

    dropped = await rt.drop_agent_sessions("assistant")

    assert dropped == 1  # only the idle assistant session
    assert a1.closed and "assistant--T1" not in rt._sessions
    assert not a2.closed and "assistant--T2" in rt._sessions  # busy one respawns later
    assert not other.closed and "developer--T1" in rt._sessions  # other agent untouched


async def test_drop_agent_sessions_returns_zero_for_unknown_agent() -> None:
    rt = _runtime_with([FakeSession()])
    await rt.run_stateful(_profile(), "assistant--T1", "x")

    assert await rt.drop_agent_sessions("nobody") == 0
    assert "assistant--T1" in rt._sessions


async def test_close_drops_all_sessions() -> None:
    s1, s2 = FakeSession(), FakeSession()
    rt = _runtime_with([s1, s2])
    profile = _profile()

    await rt.run_stateful(profile, "assistant--T1", "a")
    await rt.run_stateful(profile, "assistant--T2", "b")
    await rt.close()

    assert s1.closed and s2.closed


async def test_rejects_foreign_profile_type() -> None:
    rt = _runtime_with([FakeSession()])
    with pytest.raises(TypeError, match="PiProfile"):
        await rt.run_stateful(SimpleNamespace(), "assistant--T1", "x")  # type: ignore[arg-type]


# --- reaper ------------------------------------------------------------------


async def test_reaper_drops_idle_and_never_used_sessions_but_not_busy() -> None:
    used, never_used, busy = FakeSession(), FakeSession(), FakeSession()
    busy.busy = True
    rt = _runtime_with([used, never_used, busy], idle_ttl=10.0)
    profile = _profile()

    await rt.run_stateful(profile, "assistant--used", "x")
    # Simulate a session that was created but whose first turn never succeeded,
    # and a session stuck in a long turn.
    from crucible.runtimes.pi.runtime import _ManagedSession

    old = rt._now() - 100.0
    rt._sessions["assistant--used"].last_used = old
    rt._sessions["assistant--never"] = _ManagedSession(
        session=cast(PiRpcSession, never_used), created_at=old
    )
    rt._sessions["assistant--busy"] = _ManagedSession(
        session=cast(PiRpcSession, busy), created_at=old
    )
    # _reap_once releases a semaphore permit per drop; take permits for the
    # manually injected sessions so the count stays balanced.
    await rt._semaphore.acquire()
    await rt._semaphore.acquire()

    await rt._reap_once()

    assert used.closed  # idle past TTL
    assert never_used.closed  # last_used == 0 must not mean immortal
    assert not busy.closed  # in-flight turn is never reaped
    assert "assistant--busy" in rt._sessions


# --- spawn argument construction ----------------------------------------------


async def _capture_spawn(
    monkeypatch, profile, *, session_id: str | None = "assistant--T1", cwd=None, **runtime_kwargs
):
    captured: dict = {}

    async def fake_spawn(pi_bin, args, *, cwd, env=None):
        captured["args"] = args
        captured["cwd"] = cwd
        return SimpleNamespace()  # transport stub; PiRpcSession just holds it

    monkeypatch.setattr("crucible.runtimes.pi.runtime.SubprocessTransport.spawn", fake_spawn)
    rt = PiRuntime(**runtime_kwargs)
    await rt._spawn_session(profile, session_id, None, cwd=cwd)
    return captured


async def test_empty_allowlist_passes_empty_tools_no_skills(monkeypatch) -> None:
    # Empty pi.tools -> --tools "" (no tools at all, confirmed vs pi); --skills
    # is always suppressed and no --skill is added when none are declared.
    profile = _profile()
    cap = await _capture_spawn(monkeypatch, profile)

    assert cap["args"][cap["args"].index("--tools") + 1] == ""  # exactly no tools
    assert "--no-builtin-tools" not in cap["args"]  # gate is --tools alone now
    assert "--no-skills" in cap["args"]
    assert "--skill" not in cap["args"]
    assert cap["cwd"] == str(Path("agents") / "assistant")
    assert "--append-system-prompt" not in cap["args"]  # none set on this profile


async def test_append_system_prompt_flag(monkeypatch) -> None:
    profile = PiProfile(
        name="assistant",
        config_dir=Path("agents") / "assistant",
        timeout=5.0,
        append_system_prompt="Format as Slack mrkdwn",
    )
    cap = await _capture_spawn(monkeypatch, profile)
    i = cap["args"].index("--append-system-prompt")
    assert cap["args"][i + 1] == "Format as Slack mrkdwn"


async def test_tools_allowlist_is_the_single_gate(monkeypatch) -> None:
    # read/bash (built-ins) live in the SAME allowlist as extension/typed tools.
    profile = PiProfile(
        name="assistant",
        config_dir=Path("agents") / "assistant",
        timeout=5.0,
        tools=("read", "bash", "list_agents"),
        provider="openai-codex",
        model="gpt-5.5",
    )
    cap = await _capture_spawn(monkeypatch, profile)

    assert "--no-builtin-tools" not in cap["args"]
    assert cap["args"][cap["args"].index("--tools") + 1] == "read,bash,list_agents"
    assert cap["args"][cap["args"].index("--provider") + 1] == "openai-codex"
    assert cap["args"][cap["args"].index("--model") + 1] == "gpt-5.5"


async def test_declared_skills_become_no_skills_plus_skill_flags(monkeypatch) -> None:
    profile = PiProfile(
        name="assistant",
        config_dir=Path("agents") / "assistant",
        timeout=5.0,
        tools=("read", "bash"),
        skills=("/abs/skills/hello", "/abs/skills/greet"),
    )
    cap = await _capture_spawn(monkeypatch, profile)

    # ambient discovery off, exactly the declared skills added
    assert "--no-skills" in cap["args"]
    skill_vals = [cap["args"][i + 1] for i, a in enumerate(cap["args"]) if a == "--skill"]
    assert skill_vals == ["/abs/skills/hello", "/abs/skills/greet"]


async def test_session_dir_is_per_agent(monkeypatch, tmp_path) -> None:
    profile = _profile()
    cap = await _capture_spawn(monkeypatch, profile, session_dir=str(tmp_path / "pi-sessions"))

    sdir = cap["args"][cap["args"].index("--session-dir") + 1]
    assert sdir == str(tmp_path / "pi-sessions" / "assistant")
    assert (tmp_path / "pi-sessions" / "assistant").is_dir()  # created eagerly


async def test_session_dir_is_absolute_even_when_configured_relative(
    monkeypatch, tmp_path
) -> None:
    # pi resolves a relative --session-dir from ITS OWN cwd (the profile dir) —
    # session files would scatter into the agents dir. The runtime must resolve.
    monkeypatch.chdir(tmp_path)
    profile = _profile()
    cap = await _capture_spawn(monkeypatch, profile, session_dir="data/pi-sessions")

    sdir = Path(cap["args"][cap["args"].index("--session-dir") + 1])
    assert sdir.is_absolute()
    assert sdir == tmp_path / "data" / "pi-sessions" / "assistant"


async def test_stateless_spawn_uses_no_session(monkeypatch) -> None:
    profile = _profile()
    cap = await _capture_spawn(monkeypatch, profile, session_id=None)

    assert "--no-session" in cap["args"]
    assert "--session-id" not in cap["args"]


def test_safe_session_id_sanitizes() -> None:
    assert _safe_session_id("assistant--abc123") == "assistant--abc123"
    # Non-ASCII input on purpose: exercises the sanitizer alphabet.
    assert _safe_session_id("агент/тред:1") == "1"
    assert _safe_session_id("///") == "session"


async def test_session_id_is_injected_into_env(monkeypatch) -> None:
    captured = {}

    async def fake_spawn(pi_bin, args, *, cwd, env=None):
        captured["env"] = env
        return SimpleNamespace()

    monkeypatch.setattr("crucible.runtimes.pi.runtime.SubprocessTransport.spawn", fake_spawn)
    rt = PiRuntime()
    await rt._spawn_session(_profile(), "assistant--conv1", None)
    assert captured["env"]["RUNTIME_SESSION_ID"] == "assistant--conv1"


async def test_a_turn_does_not_wait_forever_for_a_free_slot() -> None:
    # Permits are held for as long as a session lives, idle included, so an
    # unbounded wait would hang the turn with no timeout to end it: the per-turn
    # deadline only starts once the session exists.
    rt = _runtime_with([FakeSession(), FakeSession()], max_concurrent_sessions=1)
    rt._acquire_timeout = 0.05
    profile = _profile()
    await rt.run_stateful(profile, "assistant--T1", "one")  # holds the only permit

    with pytest.raises(PiTimeout, match="no runtime slot"):
        await rt.run_stateful(profile, "assistant--T2", "two")


async def test_a_freed_slot_is_taken_by_the_waiting_turn() -> None:
    rt = _runtime_with([FakeSession(), FakeSession()], max_concurrent_sessions=1)
    rt._acquire_timeout = 5.0
    profile = _profile()
    await rt.run_stateful(profile, "assistant--T1", "one")
    await rt.drop_agent_sessions("assistant")  # releases the permit

    result = await rt.run_stateful(profile, "assistant--T2", "two")
    assert result.text == "ok"
