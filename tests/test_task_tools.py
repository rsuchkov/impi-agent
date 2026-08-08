"""Scheduling from a turn: the admin service and the tools over it."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from crucible.ports.chat.types import KIND_DM, KIND_THREAD
from crucible.ports.tasks import TaskError
from crucible.scheduler.admin import TaskAdmin
from crucible.store.base import MODE_PROMPT, STATE_IDLE, STATE_PAUSED
from crucible.store.sessions import SqliteSessionStore
from crucible.tools.base import CAP_SCHEDULER, ToolContext, ToolError
from impi.task_tools import CancelTask, ListTasks, PauseTask, ScheduleTask

NOW = datetime(2026, 8, 7, 10, 0, tzinfo=timezone.utc)  # a Friday
BELGRADE = "Europe/Belgrade"


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class FakeDirectory:
    def agent_user_ids(self):
        return frozenset()

    def list_agents(self):
        return []


@pytest.fixture
async def store(tmp_path: Path):
    opened = SqliteSessionStore(tmp_path / "db.sqlite")
    yield opened
    await opened.close()


async def _admin(store, **over) -> tuple[TaskAdmin, str]:
    """An admin plus the runtime session id of a live DM conversation."""
    record, _ = await store.get_or_create("assistant", "ch1", "dm1", KIND_DM, user_id="u1")
    kwargs = dict(default_timezone="UTC", max_per_agent=50, clock=Clock())
    kwargs.update(over)
    return TaskAdmin(store, store, **kwargs), record.runtime_session_id  # type: ignore[arg-type]


def _ctx(admin: TaskAdmin, session_id: str) -> ToolContext:
    return ToolContext(
        agent_name="assistant", directory=FakeDirectory(),  # type: ignore[arg-type]
        runtime_session_id=session_id, task_svc=admin,
    )


# -- creating ------------------------------------------------------------------


async def test_a_new_task_belongs_to_this_conversation_and_previews_its_times(
    store,
) -> None:
    admin, session = await _admin(store, default_timezone=BELGRADE)

    view = await admin.schedule(
        "assistant", session, name="digest", prompt="summarize my inbox",
        schedule="0 9 * * 1-5",
    )

    assert (view.state, view.mode, view.timezone) == (STATE_IDLE, "turn", BELGRADE)
    # The echo that makes a wrong expression obvious now, in the person's zone.
    assert view.upcoming == (
        "2026-08-10 09:00 (Europe/Belgrade)",
        "2026-08-11 09:00 (Europe/Belgrade)",
        "2026-08-12 09:00 (Europe/Belgrade)",
    )
    stored = await store.find_task("assistant", "digest")
    assert stored is not None
    assert (stored.channel_id, stored.conversation_id, stored.kind) == ("ch1", "dm1", KIND_DM)
    assert stored.created_by == "u1"


async def test_a_one_shot_is_scheduled_from_now(store) -> None:
    admin, session = await _admin(store)

    view = await admin.schedule(
        "assistant", session, name="remind", prompt="stand up", schedule="in 90m"
    )

    assert view.next_run == "2026-08-07 11:30 (UTC)"
    assert view.upcoming == ("2026-08-07 11:30 (UTC)",)


async def test_a_recurring_task_gets_a_stable_smear_a_one_shot_does_not(store) -> None:
    admin, session = await _admin(store)

    await admin.schedule("assistant", session, name="poll", prompt="check",
                         schedule="every 1h")
    await admin.schedule("assistant", session, name="once", prompt="ping",
                         schedule="in 2h")

    recurring = await store.find_task("assistant", "poll")
    one_shot = await store.find_task("assistant", "once")
    assert recurring is not None and one_shot is not None
    # The smear rides on due_at from the FIRST occurrence, never on next_run_at:
    # a fleet of daily tasks created in one sitting must not all fire together.
    assert recurring.jitter_s > 0
    assert recurring.due_at != recurring.next_run_at
    assert one_shot.jitter_s == 0 and one_shot.due_at == one_shot.next_run_at


@pytest.mark.parametrize(
    ("schedule", "match"),
    [
        ("every 5s", "at least 60s"),
        ("cron: nonsense here now", "not a valid"),
        ("yesterday", "could not read"),
    ],
)
async def test_a_schedule_that_cannot_be_read_says_why(store, schedule, match) -> None:
    admin, session = await _admin(store)

    with pytest.raises(TaskError, match=match):
        await admin.schedule("assistant", session, name="x", prompt="y", schedule=schedule)


async def test_a_duplicate_name_is_refused(store) -> None:
    admin, session = await _admin(store)
    await admin.schedule("assistant", session, name="digest", prompt="a", schedule="every 1h")

    with pytest.raises(TaskError, match="already have a task"):
        await admin.schedule("assistant", session, name="digest", prompt="b",
                             schedule="every 2h")


async def test_the_per_agent_limit_holds(store) -> None:
    admin, session = await _admin(store, max_per_agent=2)
    await admin.schedule("assistant", session, name="a", prompt="a", schedule="every 1h")
    await admin.schedule("assistant", session, name="b", prompt="b", schedule="every 1h")

    with pytest.raises(TaskError, match="the limit is 2"):
        await admin.schedule("assistant", session, name="c", prompt="c", schedule="every 1h")


async def test_scheduling_outside_a_conversation_is_refused(store) -> None:
    admin, _ = await _admin(store)

    with pytest.raises(TaskError, match="conversation could not be resolved"):
        await admin.schedule("assistant", "assistant--never-seen", name="x", prompt="y",
                             schedule="every 1h")


async def test_a_thread_task_remembers_it_is_a_thread(store) -> None:
    record, _ = await store.get_or_create("assistant", "ch1", "root1", KIND_THREAD)
    admin = TaskAdmin(store, store, clock=Clock())

    await admin.schedule("assistant", record.runtime_session_id, name="standup",
                         prompt="ask", schedule="every 1d")

    stored = await store.find_task("assistant", "standup")
    assert stored is not None and stored.kind == KIND_THREAD


# -- listing, pausing, cancelling ----------------------------------------------


async def test_pausing_clears_the_schedule_and_resuming_keeps_the_phase(store) -> None:
    admin, session = await _admin(store)
    await admin.schedule("assistant", session, name="poll", prompt="check",
                         schedule="every 1h")
    before = await store.find_task("assistant", "poll")
    assert before is not None

    paused = await admin.set_paused("assistant", "poll", True)
    assert paused.state == STATE_PAUSED
    assert (await store.find_task("assistant", "poll")).next_run_at is None  # type: ignore[union-attr]

    resumed = await admin.set_paused("assistant", "poll", False)

    assert resumed.state == STATE_IDLE
    after = await store.find_task("assistant", "poll")
    assert after is not None and after.next_run_at == before.next_run_at


async def test_a_task_can_be_cancelled_by_name_or_id(store) -> None:
    admin, session = await _admin(store)
    created = await admin.schedule("assistant", session, name="poll", prompt="p",
                                   schedule="every 1h")

    await admin.cancel("assistant", created.id)

    assert await store.find_task("assistant", "poll") is None


async def test_one_agent_cannot_touch_anothers_task(store) -> None:
    admin, session = await _admin(store)
    created = await admin.schedule("assistant", session, name="poll", prompt="p",
                                   schedule="every 1h")

    for act in (admin.cancel("developer", created.id),
                admin.set_paused("developer", created.id, True)):
        with pytest.raises(TaskError, match="no task"):
            await act


async def test_listing_shows_only_this_agents_tasks(store) -> None:
    admin, session = await _admin(store)
    await admin.schedule("assistant", session, name="mine", prompt="p", schedule="every 1h")
    other, _ = await store.get_or_create("developer", "ch2", "dm2", KIND_DM)
    await admin.schedule("developer", other.runtime_session_id, name="theirs", prompt="p",
                         schedule="every 1h")

    assert [v.name for v in await admin.list_tasks("assistant")] == ["mine"]


# -- the tools over it ---------------------------------------------------------


async def test_the_schedule_tool_returns_the_preview_for_the_agent_to_read_back(
    store,
) -> None:
    admin, session = await _admin(store, default_timezone=BELGRADE)

    result = await ScheduleTask().execute(
        _ctx(admin, session),
        {"name": "digest", "prompt": "summarize", "schedule": "0 9 * * 1-5"},
    )

    assert result["scheduled"] is True
    assert result["upcoming"][0] == "2026-08-10 09:00 (Europe/Belgrade)"


async def test_the_tools_pass_a_refusal_through_as_a_tool_error(store) -> None:
    admin, session = await _admin(store)

    with pytest.raises(ToolError, match="at least 60s"):
        await ScheduleTask().execute(
            _ctx(admin, session), {"name": "x", "prompt": "y", "schedule": "every 5s"}
        )
    with pytest.raises(ToolError, match="no task"):
        await CancelTask().execute(_ctx(admin, session), {"task": "ghost"})


async def test_the_tools_are_gated_on_the_scheduler_being_on(store) -> None:
    off = ToolContext(agent_name="assistant", directory=FakeDirectory())  # type: ignore[arg-type]

    with pytest.raises(ToolError, match="turned off"):
        await ListTasks().execute(off, {})
    assert ScheduleTask.requires == frozenset({CAP_SCHEDULER})


async def test_pause_and_resume_through_the_tool(store) -> None:
    admin, session = await _admin(store)
    ctx = _ctx(admin, session)
    await ScheduleTask().execute(ctx, {"name": "poll", "prompt": "p", "schedule": "every 1h"})

    paused = await PauseTask().execute(ctx, {"task": "poll"})
    resumed = await PauseTask().execute(ctx, {"task": "poll", "resume": True})

    assert paused["paused"] is True and paused["state"] == STATE_PAUSED
    assert resumed["paused"] is False and resumed["state"] == STATE_IDLE


async def test_a_memoryless_task_is_created_in_prompt_mode(store) -> None:
    admin, session = await _admin(store)

    result = await ScheduleTask().execute(
        _ctx(admin, session),
        {"name": "poll", "prompt": "check the deploy", "schedule": "every 30m",
         "mode": MODE_PROMPT},
    )

    assert result["mode"] == MODE_PROMPT


async def test_listing_through_the_tool_reports_the_next_run(store) -> None:
    admin, session = await _admin(store)
    await ScheduleTask().execute(
        _ctx(admin, session), {"name": "poll", "prompt": "p", "schedule": "every 1h"}
    )

    listed = await ListTasks().execute(_ctx(admin, session), {})

    assert listed["tasks"][0]["next_run"] == "2026-08-07 11:00 (UTC)"
    assert listed["tasks"][0]["next_run"] != ""


async def test_an_unknown_zone_is_refused_before_the_task_exists(store) -> None:
    admin, session = await _admin(store)

    with pytest.raises(ToolError, match="unknown timezone"):
        await ScheduleTask().execute(
            _ctx(admin, session),
            {"name": "x", "prompt": "y", "schedule": "every 1h", "timezone": "Mars/Olympus"},
        )
    assert await admin.list_tasks("assistant") == []


def test_an_interval_task_previews_from_now(store) -> None:
    # A sanity check on the rendering helper itself: no zone means UTC, and the
    # format is the one a person reads, not an ISO instant.
    from crucible.scheduler.admin import local_time

    assert local_time(NOW + timedelta(hours=1), "") == "2026-08-07 11:00 (UTC)"
    assert local_time(None, "UTC") == ""
