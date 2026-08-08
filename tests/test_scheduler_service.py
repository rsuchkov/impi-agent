"""The ticker, on a clock we control and a real store.

Everything here is about the four decisions a tick can make and the accounting
that follows them, because that is where a scheduler is trusted or isn't.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from crucible.ports.chat.flow import TurnOutcome
from crucible.scheduler.health import ALIVE, NEVER, STALE, liveness
from crucible.scheduler.ports import DispatchError
from crucible.scheduler.service import Scheduler
from crucible.scheduler.triggers import parse_trigger, to_iso
from crucible.store.base import (
    MODE_PROMPT,
    MODE_TURN,
    NOTIFY_ALWAYS,
    NOTIFY_FAILURES,
    NOTIFY_NEVER,
    ON_MISSED_RUN,
    ON_MISSED_SKIP,
    RUN_CANCELLED,
    RUN_DEADLINE,
    RUN_EMPTY,
    RUN_ERROR,
    RUN_INTERRUPTED,
    RUN_MISSED,
    RUN_NO_AGENT,
    RUN_OK,
    RUN_OVERLAP,
    RUN_RUNNING,
    STATE_IDLE,
    STATE_PAUSED,
    STATE_RUNNING,
    TRIGGERED_CATCHUP,
    TaskRecord,
    TaskRunRecord,
)
from crucible.store.sessions import SqliteSessionStore

START = datetime(2026, 8, 8, 9, 0, tzinfo=timezone.utc)


def iso(moment: datetime) -> str:
    """to_iso for a moment we know exists (its None is "no next run")."""
    stamp = to_iso(moment)
    assert stamp is not None
    return stamp


class Clock:
    """A hand-wound clock: the scheduler never reads the wall."""

    def __init__(self, now: datetime = START) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta) -> None:
        self.now += timedelta(**delta)


class FakeDispatcher:
    def __init__(self, outcome=TurnOutcome.REPLIED, error: Exception | None = None) -> None:
        self.requests: list = []
        self.outcome = outcome
        self.error = error
        self.gate: asyncio.Event | None = None

    async def run_turn(self, request) -> TurnOutcome:
        self.requests.append(request)
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        return self.outcome


class FakePrompts:
    def __init__(self, text: str = "the digest") -> None:
        self.calls: list[tuple[str, str]] = []
        self.text = text
        self.error: Exception | None = None

    async def run_prompt(self, agent: str, text: str):
        self.calls.append((agent, text))
        if self.error is not None:
            raise self.error
        return type("Result", (), {"text": self.text, "tool_calls": []})()


class FakeNotifier:
    def __init__(self) -> None:
        self.posts: list[tuple[str, str]] = []  # (agent, text), either verb

    async def deliver(self, agent, *, channel_id, conversation_id, kind, text) -> None:
        self.posts.append((agent, text))

    async def announce(self, agent, *, channel_id, conversation_id, kind, text) -> None:
        self.posts.append((agent, text))


def _task(clock: Clock, **over) -> TaskRecord:
    spec = over.pop("spec", "every 15m")
    trigger = parse_trigger(spec, now=clock.now)
    first = over.pop("next_run_at", iso(clock.now))
    base = dict(
        id="tsk_1", agent="assistant", name="digest", channel_id="ch1",
        conversation_id="dm1", kind="dm", mode=MODE_TURN, prompt="summarize my inbox",
        trigger_kind=trigger.kind, trigger_spec=trigger.spec,
        interval_s=trigger.interval_s, cron_expr=trigger.cron_expr,
        timezone=trigger.timezone, anchor_at=iso(trigger.anchor_at),
        next_run_at=first, due_at=first, jitter_s=0, state=STATE_IDLE,
        claim_owner="", claim_at="", lease_until="", on_missed=ON_MISSED_RUN,
        notify=NOTIFY_FAILURES, deadline_s=0, created_by="u1",
        created_by_username="roman", created_at=iso(clock.now),
        updated_at=iso(clock.now), last_run_at="", last_status="",
        run_count=0, miss_count=0, consecutive_failures=0,
    )
    base.update(over)
    return TaskRecord(**base)  # type: ignore[arg-type]


def _scheduler(store, clock, **over) -> tuple[Scheduler, dict[str, Any]]:
    # The collaborators come back with the scheduler so a test can assert on
    # what they were asked to do; Any because they are a mixed bag of fakes.
    kwargs: dict[str, Any] = dict(
        dispatcher=FakeDispatcher(), prompts=FakePrompts(), notifier=FakeNotifier(),
        tick_s=20.0, startup_grace_s=0.0, run_deadline_s=900.0, max_concurrent=2,
        max_failures=5, clock=clock,
    )
    kwargs.update(over)
    return Scheduler(store, **kwargs), kwargs  # type: ignore[arg-type]


async def _settle() -> None:
    """Let the spawned run task finish before asserting on the store."""
    for _ in range(6):
        await asyncio.sleep(0)


@pytest.fixture
async def store(tmp_path: Path):
    opened = SqliteSessionStore(tmp_path / "db.sqlite")
    yield opened
    await opened.close()


# -- the happy path ------------------------------------------------------------


async def test_a_due_task_runs_and_the_schedule_moves_on(store) -> None:
    clock = Clock()
    await store.create_task(_task(clock))
    sched, kw = _scheduler(store, clock)

    await sched.tick()
    await _settle()

    request = kw["dispatcher"].requests[0]
    assert (request.agent, request.text) == ("assistant", "summarize my inbox")
    assert request.username == "roman"  # never blank: the envelope would render []
    assert request.message_id.startswith("sched-")
    task = await store.get_task("tsk_1")
    assert task is not None
    assert (task.state, task.last_status, task.run_count) == (STATE_IDLE, RUN_OK, 1)
    assert task.next_run_at == iso(START + timedelta(minutes=15))
    run = (await store.list_runs("tsk_1"))[0]
    assert (run.status, run.trigger, run.coalesced) == (RUN_OK, "schedule", 1)
    assert kw["notifier"].posts == []  # success is quiet


async def test_nothing_is_dispatched_before_its_time(store) -> None:
    clock = Clock()
    await store.create_task(_task(clock, next_run_at=iso(START + timedelta(minutes=15))))
    sched, kw = _scheduler(store, clock)

    await sched.tick()
    await _settle()

    assert kw["dispatcher"].requests == []


# -- the four decisions --------------------------------------------------------


async def test_a_run_that_outlasts_its_period_is_skipped_not_stacked(store) -> None:
    clock = Clock()
    # As claim_due leaves it: running, with the NEXT occurrence already due.
    await store.create_task(_task(
        clock, state=STATE_RUNNING, claim_owner="sched-x",
        next_run_at=iso(START), due_at=iso(START),
    ))
    sched, kw = _scheduler(store, clock)

    await sched.tick()
    await _settle()

    assert kw["dispatcher"].requests == []
    run = (await store.list_runs("tsk_1"))[0]
    assert run.status == RUN_OVERLAP and "previous run" in run.detail
    task = await store.get_task("tsk_1")
    assert task is not None and task.state == STATE_RUNNING  # the live run is untouched


async def test_an_occurrence_past_its_window_is_recorded_missed_and_fast_forwarded(
    store,
) -> None:
    clock = Clock()
    await store.create_task(_task(clock, spec="every 15m"))
    clock.advance(hours=3)  # the engine was down; the window is 7.5 minutes
    sched, kw = _scheduler(store, clock)

    await sched.tick()
    await _settle()

    assert kw["dispatcher"].requests == []
    run = (await store.list_runs("tsk_1"))[0]
    assert run.status == RUN_MISSED
    assert "late by 3h00m" in run.detail and "window 7m" in run.detail
    # One row stands for the whole gap: the occurrence itself plus the 12 the
    # outage swallowed.
    assert run.coalesced == 13
    task = await store.get_task("tsk_1")
    assert task is not None and task.next_run_at == iso(START + timedelta(hours=3, minutes=15))
    assert kw["notifier"].posts and "did not run" in kw["notifier"].posts[0][1]


async def test_a_late_occurrence_inside_the_window_runs_once(store) -> None:
    clock = Clock()
    await store.create_task(_task(clock, spec="every 1h"))
    clock.advance(minutes=20)  # inside the 30-minute window

    sched, kw = _scheduler(store, clock)
    await sched.tick()
    await _settle()

    assert len(kw["dispatcher"].requests) == 1  # one catch-up, not twenty
    run = (await store.list_runs("tsk_1"))[0]
    assert (run.status, run.trigger) == (RUN_OK, TRIGGERED_CATCHUP)


async def test_a_task_that_asked_not_to_be_caught_up_is_only_reported(store) -> None:
    clock = Clock()
    await store.create_task(_task(clock, spec="every 1h", on_missed=ON_MISSED_SKIP))
    clock.advance(minutes=20)  # would be inside the window
    sched, kw = _scheduler(store, clock)

    await sched.tick()
    await _settle()

    assert kw["dispatcher"].requests == []
    run = (await store.list_runs("tsk_1"))[0]
    assert (run.status, run.detail) == (RUN_MISSED, "on_missed=skip")


async def test_a_late_task_waits_out_the_startup_grace_then_runs(store) -> None:
    # Gateways may still be logging in; a burst of catch-up the moment the
    # process starts is how a restart turns into a restart loop.
    clock = Clock()
    await store.create_task(_task(clock, spec="every 1h"))
    clock.advance(minutes=20)
    sched, kw = _scheduler(store, clock, startup_grace_s=60.0)

    await sched.tick()
    await _settle()
    assert kw["dispatcher"].requests == []
    assert await store.list_runs("tsk_1") == []  # deferred, not recorded

    clock.advance(seconds=61)
    await sched.tick()
    await _settle()

    assert len(kw["dispatcher"].requests) == 1


async def test_a_punctual_occurrence_fires_during_the_startup_grace(store) -> None:
    clock = Clock()
    await store.create_task(_task(clock))
    sched, kw = _scheduler(store, clock, startup_grace_s=60.0)

    await sched.tick()
    await _settle()

    assert len(kw["dispatcher"].requests) == 1  # punctual work is not delayed


# -- deadlines and failures ----------------------------------------------------


async def test_a_run_past_its_deadline_is_recorded_without_killing_the_turn(
    store,
) -> None:
    clock = Clock()
    await store.create_task(_task(clock))
    dispatcher = FakeDispatcher()
    dispatcher.gate = asyncio.Event()
    sched, kw = _scheduler(store, clock, dispatcher=dispatcher, run_deadline_s=0.01)

    await sched.tick()
    await asyncio.sleep(0.05)

    run = (await store.list_runs("tsk_1"))[0]
    assert run.status == RUN_DEADLINE and "still running after" in run.detail
    task = await store.get_task("tsk_1")
    assert task is not None and task.state == STATE_IDLE  # released, not stuck
    # The turn was NOT cancelled: cancelling mid-prompt would leave the runtime
    # session in an unknown state, and the reply still belongs in the thread.
    dispatcher.gate.set()
    await _settle()
    assert len(dispatcher.requests) == 1


async def test_an_agent_that_is_not_live_is_named_as_the_reason(store) -> None:
    clock = Clock()
    await store.create_task(_task(clock))
    dispatcher = FakeDispatcher(error=DispatchError("agent 'assistant' is not running"))
    sched, kw = _scheduler(store, clock, dispatcher=dispatcher)

    await sched.tick()
    await _settle()

    run = (await store.list_runs("tsk_1"))[0]
    assert run.status == RUN_NO_AGENT and "not running" in run.detail
    assert "could not start" in kw["notifier"].posts[0][1]


async def test_a_turn_that_failed_is_not_reported_twice(store) -> None:
    # The flow already posted its own message into that conversation.
    clock = Clock()
    await store.create_task(_task(clock))
    sched, kw = _scheduler(store, clock, dispatcher=FakeDispatcher(TurnOutcome.ERROR))

    await sched.tick()
    await _settle()

    assert (await store.list_runs("tsk_1"))[0].status == RUN_ERROR
    assert kw["notifier"].posts == []


async def test_a_task_that_keeps_failing_is_paused_and_says_so(store) -> None:
    clock = Clock()
    await store.create_task(_task(clock, consecutive_failures=2))
    dispatcher = FakeDispatcher(error=DispatchError("gone"))
    sched, kw = _scheduler(store, clock, dispatcher=dispatcher, max_failures=3)

    await sched.tick()
    await _settle()

    task = await store.get_task("tsk_1")
    assert task is not None and task.state == STATE_PAUSED
    assert "Paused after 3 failures" in kw["notifier"].posts[0][1]
    assert "impi task resume tsk_1" in kw["notifier"].posts[0][1]


async def test_notify_never_stays_quiet_even_about_failures(store) -> None:
    clock = Clock()
    await store.create_task(_task(clock, notify=NOTIFY_NEVER, spec="every 15m"))
    clock.advance(hours=3)
    sched, kw = _scheduler(store, clock)

    await sched.tick()
    await _settle()

    assert (await store.list_runs("tsk_1"))[0].status == RUN_MISSED  # still recorded
    assert kw["notifier"].posts == []


async def test_notify_always_confirms_a_good_run(store) -> None:
    clock = Clock()
    await store.create_task(_task(clock, notify=NOTIFY_ALWAYS))
    sched, kw = _scheduler(store, clock)

    await sched.tick()
    await _settle()

    assert "ran" in kw["notifier"].posts[0][1]


# -- the memoryless mode -------------------------------------------------------


async def test_a_prompt_task_posts_its_own_output(store) -> None:
    clock = Clock()
    await store.create_task(_task(clock, mode=MODE_PROMPT))
    prompts = FakePrompts("three PRs are waiting")
    sched, kw = _scheduler(store, clock, prompts=prompts)

    await sched.tick()
    await _settle()

    assert prompts.calls == [("assistant", "summarize my inbox")]
    assert kw["notifier"].posts == [("assistant", "three PRs are waiting")]
    assert (await store.list_runs("tsk_1"))[0].status == RUN_OK


async def test_a_prompt_task_with_nothing_to_say_is_recorded_empty(store) -> None:
    clock = Clock()
    await store.create_task(_task(clock, mode=MODE_PROMPT))
    sched, kw = _scheduler(store, clock, prompts=FakePrompts(""))

    await sched.tick()
    await _settle()

    assert (await store.list_runs("tsk_1"))[0].status == RUN_EMPTY
    assert kw["notifier"].posts  # nobody else would have said anything


# -- surviving the engine ------------------------------------------------------


async def test_recovery_reports_what_a_dead_engine_left_behind(store) -> None:
    clock = Clock()
    await store.create_task(_task(clock))
    await store.claim_due(
        task_id="tsk_1", seen_due_at=iso(START), next_run_at=iso(START),
        due_at=iso(START),
        run=TaskRunRecord(
            run_id="run_dead", task_id="tsk_1", agent="assistant",
            scheduled_at=iso(START), started_at=iso(START), finished_at="",
            status=RUN_RUNNING, trigger="schedule", owner="sched-dead", duration_ms=0,
            detail="", reply_chars=0, tool_calls=0, coalesced=1, notified=0,
        ),
        owner="sched-dead", lease_until=iso(START), now=iso(START),
    )
    sched, kw = _scheduler(store, clock)

    await sched.recover()
    await sched.tick()  # the notice is delivered by the ordinary tick
    await _settle()

    dead = next(r for r in await store.list_runs("tsk_1") if r.run_id == "run_dead")
    assert dead.status == RUN_INTERRUPTED
    assert "cut short" in kw["notifier"].posts[0][1]


async def test_stopping_cancels_a_run_and_records_it(store) -> None:
    clock = Clock()
    await store.create_task(_task(clock))
    dispatcher = FakeDispatcher()
    dispatcher.gate = asyncio.Event()
    sched, kw = _scheduler(store, clock, dispatcher=dispatcher)

    await sched.tick()
    await asyncio.sleep(0)
    await sched.stop()
    await _settle()

    assert (await store.list_runs("tsk_1"))[0].status == RUN_CANCELLED


# -- the loop and its heartbeat ------------------------------------------------


async def test_a_failing_tick_is_recorded_and_the_loop_keeps_going(store) -> None:
    clock = Clock()
    sched, kw = _scheduler(store, clock, tick_s=0.01)
    ticks = {"n": 0}
    original = sched.tick

    async def flaky() -> None:
        ticks["n"] += 1
        if ticks["n"] == 1:
            raise RuntimeError("the disk hiccuped")
        await original()

    sched.tick = flaky  # type: ignore[method-assign]
    loop = asyncio.create_task(sched.run())
    await asyncio.sleep(0.05)
    loop.cancel()

    assert ticks["n"] > 1  # it did not die on the first failure
    beat = await store.read_heartbeat()
    assert beat is not None and "the disk hiccuped" in beat.last_error


async def test_the_heartbeat_names_the_next_wake_and_reads_alive(store) -> None:
    clock = Clock()
    await store.create_task(_task(clock, next_run_at=iso(START + timedelta(minutes=15)),
                                  due_at=iso(START + timedelta(minutes=15))))
    sched, kw = _scheduler(store, clock)

    await sched.tick()

    beat = await store.read_heartbeat()
    assert beat is not None
    assert (beat.next_task_name, beat.tasks_total) == ("digest", 1)
    assert beat.next_wake_at == iso(START + timedelta(minutes=15))
    verdict, detail = liveness(beat, now=clock.now)
    assert verdict == ALIVE and "digest" in detail


async def test_a_scheduler_that_stopped_ticking_reads_stale(store) -> None:
    clock = Clock()
    sched, kw = _scheduler(store, clock, tick_s=20.0)
    await sched.tick()
    beat = await store.read_heartbeat()
    assert beat is not None

    verdict, detail = liveness(beat, now=clock.now + timedelta(minutes=5))

    assert verdict == STALE and "no tick for" in detail


async def test_a_database_no_scheduler_ever_touched_reads_never(store) -> None:
    verdict, _ = liveness(await store.read_heartbeat(), now=START)
    assert verdict == NEVER


async def test_only_so_many_runs_are_started_in_one_tick(store) -> None:
    clock = Clock()
    for i in range(4):
        await store.create_task(_task(clock, id=f"tsk_{i}", name=f"task-{i}"))
    dispatcher = FakeDispatcher()
    dispatcher.gate = asyncio.Event()
    sched, kw = _scheduler(store, clock, dispatcher=dispatcher, max_concurrent=2)

    await sched.tick()
    await asyncio.sleep(0)

    assert len(dispatcher.requests) == 2  # the rest keep their due_at for next tick
    dispatcher.gate.set()
    await _settle()
