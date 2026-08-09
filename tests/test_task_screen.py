"""The /tasks screen: browsing a schedule and acting on it, without a turn."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from crucible.interactions.screens import ScreenState, state_from_context
from crucible.ports.chat.types import KIND_DM
from crucible.scheduler.admin import TaskAdmin
from crucible.store.base import STATE_PAUSED, SchedulerHeartbeat
from crucible.store.sessions import SqliteSessionStore
from impi.task_screen import PAGE_SIZE, TaskScreen

NOW = datetime(2026, 8, 7, 10, 0, tzinfo=timezone.utc)


class Clock:
    def __call__(self) -> datetime:
        return NOW


@pytest.fixture
async def screen(tmp_path: Path):
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    record, _ = await store.get_or_create("assistant", "ch1", "dm1", KIND_DM, user_id="u1")
    admin = TaskAdmin(store, store, default_timezone="UTC", clock=Clock())
    yield (TaskScreen(store, admin, heartbeat=store, clock=Clock()),
           store, admin, record.runtime_session_id)
    await store.close()


async def _add(admin, session, name: str, schedule: str = "every 1h"):
    return await admin.schedule("assistant", session, name=name, prompt=f"do {name}",
                                schedule=schedule)


def _click(view, label_part: str) -> ScreenState:
    """The state a control carries, found by what it says."""
    for card in view.cards:
        for action in card.actions:
            if label_part.lower() in action.label.lower():
                state = state_from_context(action.context)
                assert state is not None
                return state.with_data(value=action.value)
    raise AssertionError(f"no control matching {label_part!r}")


def _text(view) -> str:
    return "\n".join(card.text for card in view.cards)


async def test_an_empty_schedule_says_so_and_offers_no_controls(screen) -> None:
    task_screen, *_ = screen

    view = await task_screen.render(ScreenState(screen="tasks"), user_id="u1")

    assert "Nothing is scheduled yet" in _text(view)
    assert all(not card.actions for card in view.cards)


async def test_each_task_is_a_card_with_its_own_controls(screen) -> None:
    task_screen, store, admin, session = screen
    await _add(admin, session, "digest", "0 9 * * *")

    view = await task_screen.render(ScreenState(screen="tasks"), user_id="u1")

    body = _text(view)
    assert "digest" in body and "0 9 * * *" in body
    labels = [a.label for card in view.cards for a in card.actions]
    assert labels == ["Details", "⏸ Pause", "Run now"]


async def test_the_header_names_the_next_run_when_the_scheduler_is_alive(screen) -> None:
    task_screen, store, admin, session = screen
    await _add(admin, session, "digest")
    await store.write_heartbeat(
        SchedulerHeartbeat(
            scheduler_id="s1", pid=1, version="0.7.1", started_at="2026-08-07T09:00:00+00:00",
            last_tick_at="2026-08-07T09:59:50+00:00", tick_seq=9, interval_s=20.0,
            next_wake_at="2026-08-07T11:00:00+00:00", next_task_id="tsk", next_task_name="digest",
            running_count=0, tasks_total=1, last_error="", last_error_at="",
        )
    )

    view = await task_screen.render(ScreenState(screen="tasks"), user_id="u1")

    assert "next at" in _text(view)


async def test_a_dead_scheduler_is_called_out_above_the_list(screen) -> None:
    # The whole point of the heartbeat: a schedule is only as good as the loop
    # behind it, and a stopped loop must not look like a quiet one.
    task_screen, store, admin, session = screen
    await _add(admin, session, "digest")

    view = await task_screen.render(ScreenState(screen="tasks"), user_id="u1")

    assert "the scheduler is never" in _text(view)


async def test_pausing_from_the_list_redraws_it_paused(screen) -> None:
    task_screen, store, admin, session = screen
    await _add(admin, session, "digest")
    view = await task_screen.render(ScreenState(screen="tasks"), user_id="u1")

    after = await task_screen.render(_click(view, "Pause"), user_id="u1")

    assert "paused" in _text(after)
    stored = await store.find_task("assistant", "digest")
    assert stored is not None and stored.state == STATE_PAUSED
    # ...and the control now offers the opposite.
    assert any("Resume" in a.label for card in after.cards for a in card.actions)


async def test_run_now_asks_rather_than_runs(screen) -> None:
    task_screen, store, admin, session = screen
    created = await _add(admin, session, "digest")
    view = await task_screen.render(ScreenState(screen="tasks"), user_id="u1")

    after = await task_screen.render(_click(view, "Run now"), user_id="u1")

    assert "is due now" in _text(after)
    stored = await store.get_task(created.id)
    assert stored is not None and stored.due_at is not None
    assert await store.list_runs(created.id) == []  # the ticker does the running


async def test_details_show_the_prompt_and_the_recent_runs(screen) -> None:
    task_screen, store, admin, session = screen
    await _add(admin, session, "digest")
    view = await task_screen.render(ScreenState(screen="tasks"), user_id="u1")

    detail = await task_screen.render(_click(view, "Details"), user_id="u1")

    body = _text(detail)
    assert "do digest" in body  # the prompt itself
    assert "**Mode** turn" in body and "**If missed** run" in body
    assert any("Delete" in a.label for card in detail.cards for a in card.actions)


async def test_delete_is_only_offered_in_the_detail_view(screen) -> None:
    # A destructive control should take a deliberate step to reach.
    task_screen, store, admin, session = screen
    await _add(admin, session, "digest")

    listing = await task_screen.render(ScreenState(screen="tasks"), user_id="u1")

    assert not any("Delete" in a.label for card in listing.cards for a in card.actions)


async def test_deleting_returns_to_the_list_and_says_what_happened(screen) -> None:
    task_screen, store, admin, session = screen
    await _add(admin, session, "digest")
    listing = await task_screen.render(ScreenState(screen="tasks"), user_id="u1")
    detail = await task_screen.render(_click(listing, "Details"), user_id="u1")

    after = await task_screen.render(_click(detail, "Delete"), user_id="u1")

    assert "deleted" in _text(after) and "Nothing is scheduled yet" in _text(after)
    assert await store.find_task("assistant", "digest") is None


async def test_a_long_schedule_pages(screen) -> None:
    task_screen, store, admin, session = screen
    for i in range(PAGE_SIZE + 2):
        await _add(admin, session, f"task-{i}")

    first = await task_screen.render(ScreenState(screen="tasks"), user_id="u1")
    second = await task_screen.render(_click(first, "Next"), user_id="u1")

    assert "page **1** of **2**" in _text(first)
    assert "page **2** of **2**" in _text(second)
    assert len(second.cards) == 3  # the header plus the two left over


async def test_a_task_that_vanished_between_clicks_says_so(screen) -> None:
    task_screen, store, admin, session = screen
    created = await _add(admin, session, "digest")
    view = await task_screen.render(ScreenState(screen="tasks"), user_id="u1")
    click = _click(view, "Details")
    await store.delete_task(created.id)

    after = await task_screen.render(click, user_id="u1")

    assert "that task is gone" in _text(after)


async def test_action_ids_are_alphanumeric_and_unique_in_one_message(screen) -> None:
    # Mattermost's router drops anything else, and duplicate ids collide.
    task_screen, store, admin, session = screen
    for i in range(3):
        await _add(admin, session, f"task-{i}")

    view = await task_screen.render(ScreenState(screen="tasks"), user_id="u1")

    ids = [a.id for card in view.cards for a in card.actions]
    assert len(ids) == len(set(ids))
    assert all(part.isalnum() for part in ids)


async def test_with_scheduling_off_the_screen_says_so_instead_of_the_agent(
    tmp_path: Path,
) -> None:
    # Unregistered, /tasks would reach an agent, which cannot read the schedule
    # and answers about it anyway. Registered-but-off, the command answers.
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    admin = TaskAdmin(store, store, default_timezone="UTC", clock=Clock())
    off = TaskScreen(store, admin, heartbeat=store, scheduler_enabled=False, clock=Clock())

    view = await off.render(ScreenState(screen="tasks"), user_id="u1")

    assert "SCHEDULER_ENABLED=true" in _text(view)
    assert all(not card.actions for card in view.cards)
    await store.close()


async def test_the_detail_view_does_not_offer_to_open_itself(screen) -> None:
    task_screen, _store, admin, session = screen
    await _add(admin, session, "digest")
    index = await task_screen.render(ScreenState(screen="tasks"), user_id="u1")

    detail = await task_screen.render(_click(index, "Details"), user_id="u1")

    labels = [a.label for card in detail.cards for a in card.actions]
    assert "Details" not in labels
    assert {"◀ All tasks", "⏸ Pause", "Run now", "🗑 Delete"} <= set(labels)
