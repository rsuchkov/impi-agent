"""Tasks, runs and the heartbeat in SQLite (crucible/store/tasks.py).

The claim protocol carries the reliability guarantee, so most of this file is
about what happens when two callers race, when a process dies mid-run, and when
a second process (the CLI container) writes to the same file.
"""

from pathlib import Path

from crucible.store.base import (
    MODE_TURN,
    NOTIFY_FAILURES,
    ON_MISSED_RUN,
    RUN_INTERRUPTED,
    RUN_MISSED,
    RUN_OK,
    RUN_RUNNING,
    STATE_IDLE,
    STATE_PAUSED,
    STATE_RUNNING,
    SchedulerHeartbeat,
    TaskRecord,
    TaskRunRecord,
)
from crucible.store.sessions import SqliteSessionStore

T0 = "2026-08-08T09:00:00+00:00"
T1 = "2026-08-08T09:15:00+00:00"
T2 = "2026-08-08T09:30:00+00:00"


def _task(**over) -> TaskRecord:
    base = dict(
        id="tsk_1", agent="assistant", name="digest", channel_id="ch1",
        conversation_id="dm1", kind="dm", mode=MODE_TURN, prompt="summarize",
        trigger_kind="every", trigger_spec="every 15m", interval_s=900, cron_expr="",
        timezone="", anchor_at=T0, next_run_at=T0, due_at=T0, jitter_s=0,
        state=STATE_IDLE, claim_owner="", claim_at="", lease_until="",
        on_missed=ON_MISSED_RUN, notify=NOTIFY_FAILURES, deadline_s=0,
        created_by="u1", created_by_username="roman", created_at=T0, updated_at=T0,
        last_run_at="", last_status="", run_count=0, miss_count=0,
        consecutive_failures=0,
    )
    base.update(over)
    return TaskRecord(**base)  # type: ignore[arg-type]


def _run(**over) -> TaskRunRecord:
    base = dict(
        run_id="run_1", task_id="tsk_1", agent="assistant", scheduled_at=T0,
        started_at=T0, finished_at="", status=RUN_RUNNING, trigger="schedule",
        owner="sched-a", duration_ms=0, detail="", reply_chars=0, tool_calls=0,
        coalesced=1, notified=0,
    )
    base.update(over)
    return TaskRunRecord(**base)  # type: ignore[arg-type]


def _store(tmp_path: Path) -> SqliteSessionStore:
    return SqliteSessionStore(tmp_path / "db.sqlite")


async def test_a_task_round_trips_with_every_field(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        await store.create_task(_task())
        assert await store.get_task("tsk_1") == _task()
        assert await store.find_task("assistant", "digest") == _task()
    finally:
        await store.close()


async def test_a_claim_fires_an_occurrence_exactly_once(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        await store.create_task(_task())
        args = dict(task_id="tsk_1", seen_due_at=T0, next_run_at=T1, due_at=T1,
                    owner="sched-a", lease_until=T2, now=T0)

        first = await store.claim_due(run=_run(), **args)  # type: ignore[arg-type]
        second = await store.claim_due(run=_run(run_id="run_2"), **args)  # type: ignore[arg-type]

        assert first is not None and second is None  # the CAS token moved
        task = await store.get_task("tsk_1")
        assert task is not None
        assert (task.state, task.claim_owner, task.run_count) == (STATE_RUNNING, "sched-a", 1)
        # The schedule advanced in the SAME transaction as the claim, so a crash
        # an instant later cannot re-fire this occurrence.
        assert (task.next_run_at, task.due_at) == (T1, T1)
        assert len(await store.list_runs("tsk_1")) == 1
    finally:
        await store.close()


async def test_the_same_occurrence_cannot_be_written_twice(tmp_path: Path) -> None:
    # Belt and braces behind the CAS: UNIQUE (task_id, scheduled_at).
    store = _store(tmp_path)
    try:
        await store.create_task(_task())
        await store.claim_due(task_id="tsk_1", seen_due_at=T0, next_run_at=T1, due_at=T1,
                              run=_run(), owner="sched-a", lease_until=T2, now=T0)
        await store.complete_run(run_id="run_1", task_id="tsk_1", status=RUN_OK,
                                 finished_at=T0, duration_ms=5)
        # Rewind the schedule by hand (as a buggy caller might) and re-claim.
        await store.reschedule("tsk_1", next_run_at=T0, due_at=T0, now=T0)

        again = await store.claim_due(
            task_id="tsk_1", seen_due_at=T0, next_run_at=T1, due_at=T1,
            run=_run(run_id="run_dup"), owner="sched-a", lease_until=T2, now=T0,
        )

        assert again is None
        assert [r.run_id for r in await store.list_runs("tsk_1")] == ["run_1"]
    finally:
        await store.close()


async def test_a_lost_claim_leaves_no_transaction_open(tmp_path: Path) -> None:
    # A no-match UPDATE still opens a transaction; without a rollback the write
    # lock would be held until something else committed.
    store = _store(tmp_path)
    try:
        await store.create_task(_task())

        lost = await store.claim_due(
            task_id="tsk_1", seen_due_at="2026-01-01T00:00:00+00:00",  # stale token
            next_run_at=T1, due_at=T1, run=_run(), owner="sched-a", lease_until=T2, now=T0,
        )

        assert lost is None
        assert not store._conn.in_transaction
    finally:
        await store.close()


async def test_completing_a_run_releases_the_lease_and_counts_the_streak(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        await store.create_task(_task())
        await store.claim_due(task_id="tsk_1", seen_due_at=T0, next_run_at=T1, due_at=T1,
                              run=_run(), owner="sched-a", lease_until=T2, now=T0)

        await store.complete_run(run_id="run_1", task_id="tsk_1", status="error",
                                 finished_at=T1, duration_ms=120, detail="boom", failed=True)

        task = await store.get_task("tsk_1")
        assert task is not None
        assert (task.state, task.claim_owner, task.lease_until) == (STATE_IDLE, "", "")
        assert (task.last_status, task.consecutive_failures) == ("error", 1)
        run = (await store.list_runs("tsk_1"))[0]
        assert (run.status, run.detail, run.duration_ms) == ("error", "boom", 120)
    finally:
        await store.close()


async def test_a_success_clears_the_failure_streak(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        await store.create_task(_task(consecutive_failures=4))
        await store.claim_due(task_id="tsk_1", seen_due_at=T0, next_run_at=T1, due_at=T1,
                              run=_run(), owner="sched-a", lease_until=T2, now=T0)

        await store.complete_run(run_id="run_1", task_id="tsk_1", status=RUN_OK,
                                 finished_at=T1, duration_ms=10, reply_chars=42)

        task = await store.get_task("tsk_1")
        assert task is not None and task.consecutive_failures == 0
    finally:
        await store.close()


async def test_a_skip_advances_the_schedule_without_taking_a_lease(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        await store.create_task(_task())

        wrote = await store.record_skip(
            task_id="tsk_1", seen_due_at=T0,
            run=_run(status=RUN_MISSED, started_at="", finished_at=T1,
                     detail="late by 3:00:00 (grace 2:00:00)", coalesced=12),
            next_run_at=T1, due_at=T1, now=T1,
        )

        assert wrote
        task = await store.get_task("tsk_1")
        assert task is not None
        assert (task.state, task.claim_owner) == (STATE_IDLE, "")  # nothing was claimed
        assert (task.miss_count, task.last_status, task.next_run_at) == (1, RUN_MISSED, T1)
        run = (await store.list_runs("tsk_1"))[0]
        assert (run.status, run.coalesced, run.started_at) == (RUN_MISSED, 12, "")
    finally:
        await store.close()


async def test_an_engine_that_died_mid_run_leaves_an_interrupted_verdict(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        await store.create_task(_task())
        await store.claim_due(task_id="tsk_1", seen_due_at=T0, next_run_at=T1, due_at=T1,
                              run=_run(), owner="sched-dead", lease_until=T2, now=T0)

        # A fresh process starts: the run it finds belongs to nobody it knows.
        orphans = await store.reclaim_orphans(scheduler_id="sched-new", now=T2)

        assert [o.run_id for o in orphans] == ["run_1"]
        run = (await store.list_runs("tsk_1"))[0]
        assert run.status == RUN_INTERRUPTED and run.finished_at == T2
        task = await store.get_task("tsk_1")
        assert task is not None and task.state == STATE_IDLE and task.claim_owner == ""
    finally:
        await store.close()


async def test_our_own_live_run_is_not_reclaimed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        await store.create_task(_task())
        await store.claim_due(task_id="tsk_1", seen_due_at=T0, next_run_at=T1, due_at=T1,
                              run=_run(), owner="sched-a", lease_until=T2, now=T0)

        # Same scheduler, lease still in the future.
        assert await store.reclaim_orphans(scheduler_id="sched-a", now=T1) == []
        run = (await store.list_runs("tsk_1"))[0]
        assert run.status == RUN_RUNNING
    finally:
        await store.close()


async def test_a_late_completion_cannot_overwrite_an_interrupted_verdict(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        await store.create_task(_task())
        await store.claim_due(task_id="tsk_1", seen_due_at=T0, next_run_at=T1, due_at=T1,
                              run=_run(), owner="sched-dead", lease_until=T2, now=T0)
        await store.reclaim_orphans(scheduler_id="sched-new", now=T2)

        await store.complete_run(run_id="run_1", task_id="tsk_1", status=RUN_OK,
                                 finished_at=T2, duration_ms=1)

        assert (await store.list_runs("tsk_1"))[0].status == RUN_INTERRUPTED
    finally:
        await store.close()


async def test_due_tasks_ignores_a_finished_or_paused_schedule(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        await store.create_task(_task(id="tsk_due"))
        # NULL, not '': an empty string sorts before every timestamp and would
        # make a finished task permanently due.
        await store.create_task(_task(id="tsk_done", name="done", next_run_at=None,
                                      due_at=None, state="done"))
        await store.create_task(_task(id="tsk_paused", name="paused", state=STATE_PAUSED))

        due = await store.due_tasks(T1)

        assert [t.id for t in due] == ["tsk_due"]
    finally:
        await store.close()


async def test_pausing_clears_the_schedule_and_resuming_restores_it(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        await store.create_task(_task())

        assert await store.set_paused("tsk_1", True, now=T0)
        paused = await store.get_task("tsk_1")
        assert paused is not None
        assert (paused.state, paused.next_run_at, paused.due_at) == (STATE_PAUSED, None, None)
        assert await store.due_tasks(T2) == []

        assert await store.set_paused("tsk_1", False, now=T1, next_run_at=T2, due_at=T2)
        resumed = await store.get_task("tsk_1")
        assert resumed is not None
        assert (resumed.state, resumed.next_run_at) == (STATE_IDLE, T2)
    finally:
        await store.close()


async def test_a_running_task_cannot_be_paused_out_from_under_its_run(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        await store.create_task(_task())
        await store.claim_due(task_id="tsk_1", seen_due_at=T0, next_run_at=T1, due_at=T1,
                              run=_run(), owner="sched-a", lease_until=T2, now=T0)

        assert not await store.set_paused("tsk_1", True, now=T1)
    finally:
        await store.close()


async def test_run_now_only_moves_the_schedule_forward(tmp_path: Path) -> None:
    # The CLI runs in another container with no gateways: it asks, the engine acts.
    store = _store(tmp_path)
    try:
        await store.create_task(_task(next_run_at=T2, due_at=T2))

        assert await store.request_run_now("tsk_1", now=T0)

        task = await store.get_task("tsk_1")
        assert task is not None and task.due_at == T0
        assert [t.id for t in await store.due_tasks(T0)] == ["tsk_1"]
    finally:
        await store.close()


async def test_peek_next_names_the_task_the_scheduler_wakes_for(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        await store.create_task(_task(id="tsk_late", name="late", next_run_at=T2, due_at=T2))
        await store.create_task(_task(id="tsk_soon", name="soon", next_run_at=T1, due_at=T1))

        nxt = await store.peek_next()

        assert nxt is not None and (nxt.id, nxt.name) == ("tsk_soon", "soon")
    finally:
        await store.close()


async def test_a_finished_run_waits_to_be_notified_exactly_once(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        await store.create_task(_task())
        await store.claim_due(task_id="tsk_1", seen_due_at=T0, next_run_at=T1, due_at=T1,
                              run=_run(), owner="sched-a", lease_until=T2, now=T0)
        assert await store.unnotified_runs() == []  # still running: nothing to say yet

        await store.complete_run(run_id="run_1", task_id="tsk_1", status="error",
                                 finished_at=T1, duration_ms=1, failed=True)

        assert [r.run_id for r in await store.unnotified_runs()] == ["run_1"]
        await store.mark_notified("run_1")
        assert await store.unnotified_runs() == []
    finally:
        await store.close()


async def test_history_is_pruned_but_the_recent_runs_survive(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        await store.create_task(_task())
        for i in range(10):
            await store.claim_due(
                task_id="tsk_1", seen_due_at=T0 if i == 0 else f"2026-08-08T10:{i:02d}:00+00:00",
                next_run_at=f"2026-08-08T10:{i + 1:02d}:00+00:00",
                due_at=f"2026-08-08T10:{i + 1:02d}:00+00:00",
                run=_run(run_id=f"run_{i}", scheduled_at=f"2026-08-08T0{i}:00:00+00:00"),
                owner="sched-a", lease_until=T2, now=T0,
            )
            await store.complete_run(run_id=f"run_{i}", task_id="tsk_1", status=RUN_OK,
                                     finished_at=f"2026-08-08T0{i}:05:00+00:00", duration_ms=1)

        removed = await store.prune_runs(keep_per_task=3, before="2026-09-01T00:00:00+00:00")

        assert removed == 7
        assert len(await store.list_runs("tsk_1")) == 3
    finally:
        await store.close()


async def test_the_heartbeat_is_one_row_that_keeps_being_overwritten(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        assert await store.read_heartbeat() is None  # never ticked

        beat = SchedulerHeartbeat(
            scheduler_id="sched-a", pid=42, version="0.7.1", started_at=T0,
            last_tick_at=T0, tick_seq=1, interval_s=20.0, next_wake_at=T1,
            next_task_id="tsk_1", next_task_name="digest", running_count=0,
            tasks_total=1, last_error="", last_error_at="",
        )
        await store.write_heartbeat(beat)
        await store.write_heartbeat(
            SchedulerHeartbeat(**{**beat.__dict__, "tick_seq": 2, "last_tick_at": T1})
        )

        stored = await store.read_heartbeat()
        assert stored is not None and (stored.tick_seq, stored.last_tick_at) == (2, T1)
        assert store._conn.execute("SELECT count(*) FROM scheduler_heartbeat").fetchone()[0] == 1
    finally:
        await store.close()


async def test_a_second_process_sees_and_writes_the_same_tasks(tmp_path: Path) -> None:
    # The CLI opens the same file from its own container.
    engine = _store(tmp_path)
    cli = SqliteSessionStore(tmp_path / "db.sqlite")
    try:
        await engine.create_task(_task())

        assert cli.get_task_sync("tsk_1") is not None
        assert cli.request_run_now_sync("tsk_1", now=T0)
        task = await engine.get_task("tsk_1")
        assert task is not None and task.due_at == T0
    finally:
        await cli.close()
        await engine.close()
