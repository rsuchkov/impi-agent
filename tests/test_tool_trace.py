"""The message under a reply that says what tools the turn ran.

Three things are pinned here. The renderings — what a person sees in each of
the widget's states. The recorder — that a burst of events costs one redraw,
that a turn without tools leaves nothing, and that the trace is on disk before
the button that opens it exists. And the click — that opening the panel is the
engine rewriting one message, with no turn and no second post.
"""

import asyncio
from pathlib import Path

import pytest

from crucible.interactions import InteractionDispatcher
from crucible.interactions.pending_ui import PendingUiRequests
from crucible.interactions.screens import (
    ScreenRegistry,
    ScreenState,
    state_from_context,
)
from crucible.interactions.tooltrace import (
    ARGS_SHOWN_MAX,
    ARGS_STORED_MAX,
    CALL_FAILED,
    CALL_OK,
    CALL_UNFINISHED,
    CARD_MAX,
    PAGE_SIZE,
    TRACE_SCREEN,
    Call,
    ToolTrace,
    TraceScreen,
    TurnTrace,
    render_accordion,
    render_collapsed,
    render_counter,
)
from crucible.ports.agent.events import ToolFinished, ToolStarted
from crucible.store.base import SessionRecord, TraceRecord
from crucible.store.sqlite import SqliteSessionStore
from tests.fakes.fake_chat import FakeChat
from tests.fakes.presence import presence_of

CB = "http://x/interact"


def _record() -> SessionRecord:
    return SessionRecord(
        agent="assistant", channel_id="ch1", conversation_id="dm1", kind="dm",
        runtime_session_id="assistant--dm1", created_at="2026-09-17T09:00:00+00:00",
        last_active="2026-09-17T09:00:00+00:00",
    )


def _calls(*states: str) -> list[Call]:
    return [
        Call(index=i, call_id=f"c{i}", tool=f"tool_{i}", args='{"n": %d}' % i,
             state=state, duration_s=0.5 * (i + 1) if state != CALL_UNFINISHED else 0.0)
        for i, state in enumerate(states)
    ]


def _state(**data) -> ScreenState:
    return ScreenState(screen=TRACE_SCREEN, agent="assistant", data={"t": "tr1", **data})


def _text(view) -> str:
    return "\n".join(card.text for card in view.cards)


def _labels(view) -> list[str]:
    return [action.label for card in view.cards for action in card.actions]


class SinkSpy:
    def __init__(self) -> None:
        self.submitted: list = []

    def submit(self, msg, chat) -> None:
        self.submitted.append(msg)


@pytest.fixture
async def store(tmp_path: Path):
    opened = SqliteSessionStore(tmp_path / "db.sqlite")
    yield opened
    await opened.close()


# --- the renderings -----------------------------------------------------------


def test_the_counter_counts_and_the_button_summarises() -> None:
    assert _text(render_counter(1)) == "⚙️ Running 1 tool…"
    assert _text(render_counter(3)) == "⚙️ Running 3 tools…"

    plain = render_collapsed(_state(), _calls(CALL_OK, CALL_OK), interrupted=False)
    assert _text(plain) == ""  # the message IS the button; nothing else shows
    assert _labels(plain) == ["Running 2 tools →"]

    marked = render_collapsed(_state(), _calls(CALL_OK, CALL_FAILED), interrupted=True)
    assert _labels(marked) == ["Running 2 tools · 1 failed · interrupted →"]


def test_the_accordion_has_a_row_per_call_and_the_open_one_shows_its_arguments() -> None:
    view = render_accordion(_state(), _calls(CALL_OK, CALL_FAILED), page=0, expanded=1,
                            interrupted=False)
    text = _text(view)
    assert "⚙️ 2 tool calls · 1.5s · 1 failed" in text
    assert "▸ 1 · tool_0 · ok · 0.5s" in text
    assert "▾ 2 · tool_1 · failed · 1.0s" in text
    # Pretty-printed, in a fence, right under its own row.
    assert '```json\n{\n  "n": 1\n}\n```' in text
    assert "{\n  \"n\": 0" not in text  # only one row open at a time, by construction
    assert _labels(view) == ["1", "2", "✕ Close"]


def test_an_unfinished_call_has_no_duration_and_says_so() -> None:
    view = render_accordion(_state(), _calls(CALL_OK, CALL_UNFINISHED), page=0,
                            expanded=None, interrupted=True)
    assert "▸ 2 · tool_1 · unfinished · —" in _text(view)
    assert "· interrupted" in _text(view)


def test_long_arguments_are_cut_with_a_visible_mark_and_the_card_stays_in_budget() -> None:
    huge = Call(index=0, call_id="c0", tool="write",
                args='{"content": "' + "x" * 5000 + '"}')
    view = render_accordion(_state(), [huge], page=0, expanded=0, interrupted=False)
    text = _text(view)
    assert len(text) <= CARD_MAX
    assert f"… cut at {ARGS_SHOWN_MAX} of " in text


def test_more_calls_than_a_page_are_paged_with_the_open_row_on_its_page() -> None:
    calls = _calls(*([CALL_OK] * (PAGE_SIZE + 1)))
    first = render_accordion(_state(), calls, page=0, expanded=None, interrupted=False)
    assert "page 1/2" in _text(first)
    assert _labels(first) == [str(i) for i in range(1, PAGE_SIZE + 1)] + ["›", "✕ Close"]
    assert f"tool_{PAGE_SIZE}" not in _text(first)

    last = render_accordion(_state(), calls, page=1, expanded=PAGE_SIZE, interrupted=False)
    assert f"▾ {PAGE_SIZE + 1} · tool_{PAGE_SIZE}" in _text(last)
    assert _labels(last) == [str(PAGE_SIZE + 1), "‹", "✕ Close"]


# --- the screen: what a click does --------------------------------------------


async def _stored(store, *states: str) -> None:
    await store.create_trace(TraceRecord(
        token="tr1", agent="assistant", channel_id="ch1", conversation_id="dm1", kind="dm",
        created_at="t0", finished_at="t1", calls=__import__("json").dumps(
            [c.__dict__ for c in _calls(*states)]), post_id="p1",
    ))


async def test_open_expands_the_first_failed_call_and_close_puts_the_button_back(store) -> None:
    await _stored(store, CALL_OK, CALL_FAILED, CALL_FAILED)
    screen = TraceScreen(store)

    opened = await screen.render(_state(value="open"), user_id="u1")
    assert "▾ 2 · tool_1 · failed" in _text(opened)
    assert "▸ 3 · tool_2 · failed" in _text(opened)  # the first failed one, not every one

    closed = await screen.render(_state(value="close"), user_id="u1")
    assert _text(closed) == "" and _labels(closed) == ["Running 3 tools · 2 failed →"]


async def test_a_row_click_toggles_and_never_leaves_two_open(store) -> None:
    await _stored(store, CALL_OK, CALL_OK)
    screen = TraceScreen(store)

    one = await screen.render(_state(value="row:0"), user_id="u1")
    assert "▾ 1 ·" in _text(one)
    both = await screen.render(_state(x=0, value="row:1"), user_id="u1")
    assert "▾ 2 ·" in _text(both) and "▾ 1 ·" not in _text(both)
    none = await screen.render(_state(x=1, value="row:1"), user_id="u1")
    assert "▾" not in _text(none)


async def test_an_expired_trace_renders_a_dead_card(store) -> None:
    view = await TraceScreen(store).render(_state(value="open"), user_id="u1")
    assert "expired" in _text(view)
    assert _labels(view) == []


async def test_a_click_redraws_the_same_message_with_no_turn(store) -> None:
    # The invariant every screen shares: no turn, no second message, the same
    # post rewritten — here for a button the engine posted under a reply.
    await _stored(store, CALL_OK)
    chat, spy = FakeChat(), SinkSpy()
    screens = ScreenRegistry()
    screens.register(TraceScreen(store))
    dispatcher = InteractionDispatcher(
        store, presence_of(chat, sink=spy), PendingUiRequests(), store,
        screens=screens, callback_url=CB,
    )
    button = render_collapsed(_state(), _calls(CALL_OK), interrupted=False).cards[0].actions[0]

    assert await dispatcher.redraw_screen(
        button.context["state"], button.value, post_id="p1", user_id="u1"
    ) is True

    assert spy.submitted == []
    assert chat.posted_cards == []
    post_id, cards = chat.updated[0]
    assert post_id == "p1" and "▸ 1 · tool_0 · ok" in cards[0].text


async def test_the_panel_cannot_be_opened_by_hand_but_admits_clicks(store) -> None:
    chat, spy = FakeChat(), SinkSpy()
    screens = ScreenRegistry()
    screens.register(TraceScreen(store))
    dispatcher = InteractionDispatcher(
        store, presence_of(chat, sink=spy), PendingUiRequests(), store,
        screens=screens, callback_url=CB,
    )

    opened = await dispatcher.open_screen(
        "assistant", TRACE_SCREEN, channel_id="ch1", conversation_id="dm1", kind="dm",
        user_id="u1",
    )
    assert opened.owned and "by hand" in opened.refused
    assert chat.posted_cards == []
    # And it is not among the panels a caller may ask for.
    assert TRACE_SCREEN not in screens.names()
    assert screens.get(TRACE_SCREEN) is not None  # but its clicks still route


# --- the recorder: one turn's events into one message -------------------------


def _started(n: int, tool: str = "bash") -> ToolStarted:
    return ToolStarted(call_id=f"c{n}", tool=tool, args={"n": n})


def _finished(n: int, *, error: bool = False) -> ToolFinished:
    return ToolFinished(call_id=f"c{n}", tool="bash", is_error=error, duration_s=0.25)


async def test_a_burst_of_events_costs_one_post_and_at_most_one_redraw(store) -> None:
    chat = FakeChat()
    trace = TurnTrace(store, chat, _record(), callback_url=CB)

    for n in range(5):
        trace.on_event(_started(n))
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)

    assert len(chat.posted_cards) == 1  # the counter appeared with the first call
    assert "Running 1 tool…" in chat.posted_cards[0][1][0].text
    assert len(chat.updated) <= 1  # the rest waited for the interval
    await trace.end()


async def test_a_turn_without_tools_leaves_no_message_and_no_trace(store) -> None:
    chat = FakeChat()
    trace = TurnTrace(store, chat, _record(), callback_url=CB)
    await trace.end()
    assert chat.posted_cards == [] and chat.updated == []


async def test_ending_before_the_counter_was_drawn_posts_the_button_directly(store) -> None:
    # A sub-millisecond turn: the redraw task never got the loop before the
    # turn ended. The button still appears — once — and nothing counts.
    chat = FakeChat()
    trace = TurnTrace(store, chat, _record(), callback_url=CB)
    trace.on_event(_started(0))
    trace.on_event(_finished(0))
    await trace.end()

    assert len(chat.posted_cards) == 1 and chat.updated == []
    card = chat.posted_cards[0][1][0]
    assert card.text == "" and card.actions[0].label == "Running 1 tool →"


async def test_the_trace_is_stored_before_the_button_that_opens_it(store, monkeypatch) -> None:
    order: list[str] = []
    real_create = store.create_trace

    async def create(record):
        order.append("store")
        await real_create(record)

    chat = FakeChat()
    real_update = chat.update_cards

    async def update(post_id, cards, *, callback_url):
        order.append("button")
        await real_update(post_id, cards, callback_url=callback_url)

    monkeypatch.setattr(store, "create_trace", create)
    monkeypatch.setattr(chat, "update_cards", update)
    trace = TurnTrace(store, chat, _record(), callback_url=CB)
    trace.on_event(_started(0))
    await asyncio.sleep(0.02)  # let the counter be drawn
    trace.on_event(_finished(0, error=True))
    await trace.end()

    assert order == ["store", "button"]
    carried = state_from_context(chat.updated[-1][1][0].actions[0].context)
    assert carried is not None
    stored = await store.get_trace(carried.data["t"])
    assert stored is not None and '"state": "failed"' in stored.calls


async def test_an_interrupted_turn_marks_what_never_finished(store) -> None:
    chat = FakeChat()
    trace = TurnTrace(store, chat, _record(), callback_url=CB)
    trace.on_event(_started(0))
    trace.on_event(_finished(0))
    trace.on_event(_started(1))  # never finishes
    await trace.end(interrupted=True)

    button = chat.posted_cards[0][1][0].actions[0]
    assert button.label == "Running 2 tools · interrupted →"
    assert [c.state for c in trace.calls] == [CALL_OK, CALL_UNFINISHED]


async def test_stored_arguments_are_capped_and_the_cut_is_recorded(store) -> None:
    chat = FakeChat()
    trace = TurnTrace(store, chat, _record(), callback_url=CB)
    trace.on_event(ToolStarted(call_id="c0", tool="write", args={"content": "x" * 20000}))
    await trace.end()

    call = trace.calls[0]
    assert len(call.args) == ARGS_STORED_MAX
    assert call.cut > ARGS_STORED_MAX


async def test_a_failed_first_post_gives_up_without_a_trace(store, monkeypatch) -> None:
    chat = FakeChat()

    async def refuse(*a, **k):
        raise RuntimeError("platform down")

    monkeypatch.setattr(chat, "post_cards", refuse)
    trace = TurnTrace(store, chat, _record(), callback_url=CB)
    trace.on_event(_started(0))
    await asyncio.sleep(0.02)
    trace.on_event(_finished(0))
    await trace.end()  # does not raise, writes nothing

    assert chat.updated == []
    assert await store.prune_traces(before="9999") == 0


async def test_sweep_drops_only_what_is_past_the_retention(store) -> None:
    from datetime import datetime, timezone

    now = datetime(2026, 9, 17, tzinfo=timezone.utc)
    old = TraceRecord(token="old", agent="a", channel_id="c", conversation_id="d", kind="dm",
                      created_at="x", finished_at="2026-08-01T00:00:00+00:00", calls="[]")
    new = TraceRecord(token="new", agent="a", channel_id="c", conversation_id="d", kind="dm",
                      created_at="x", finished_at="2026-09-16T00:00:00+00:00", calls="[]")
    await store.create_trace(old)
    await store.create_trace(new)

    kept_forever = ToolTrace(store, callback_url=CB, retention_days=0, now=lambda: now)
    assert await kept_forever.sweep() == 0
    fortnight = ToolTrace(store, callback_url=CB, retention_days=14, now=lambda: now)
    assert await fortnight.sweep() == 1
    assert await store.get_trace("new") is not None
