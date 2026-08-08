"""MessageCoalescer: a burst on one conversation folds into fewer turns."""

import asyncio

import pytest

from crucible.flows.coalescer import MessageCoalescer
from crucible.ports.chat.flow import TurnOutcome
from crucible.ports.chat.types import KIND_DM, ConversationRef, IncomingMessage
from tests.fakes.fake_chat import FakeChat


class SlowFlow:
    """Records each batch; each handle_batch takes a controllable step."""

    def __init__(self) -> None:
        self.batches: list[list[str]] = []
        self._gate = asyncio.Event()

    def release(self) -> None:
        self._gate.set()

    async def handle_batch(self, msgs, chat) -> TurnOutcome:
        self.batches.append([m.ref.message_id for m in msgs])
        await self._gate.wait()  # hold the turn open so more messages pile up
        return TurnOutcome.REPLIED


def _msg(post_id: str, conv: str = "c1") -> IncomingMessage:
    return IncomingMessage(
        ref=ConversationRef(channel_id=conv, conversation_id=conv, message_id=post_id, thread_root_id=""),
        text=post_id,
        user_id="u1",
        kind=KIND_DM,
    )


async def test_messages_during_a_turn_coalesce_into_one_next_batch() -> None:
    flow = SlowFlow()
    co = MessageCoalescer(flow)

    co.submit(_msg("p1"), chat=FakeChat())  # starts a worker; handle_batch([p1]) blocks
    await asyncio.sleep(0)  # let the worker pick up p1
    co.submit(_msg("p2"), chat=FakeChat())  # pile up during the turn
    co.submit(_msg("p3"), chat=FakeChat())
    await asyncio.sleep(0)

    assert flow.batches == [["p1"]]  # only the first turn started so far
    flow.release()
    await asyncio.sleep(0.02)

    # p2 and p3 were merged into ONE follow-up batch, not two turns.
    assert flow.batches == [["p1"], ["p2", "p3"]]


async def test_separate_conversations_run_concurrently() -> None:
    flow = SlowFlow()
    co = MessageCoalescer(flow)

    co.submit(_msg("a1", conv="cA"), chat=FakeChat())
    co.submit(_msg("b1", conv="cB"), chat=FakeChat())
    await asyncio.sleep(0)

    # Both conversations got their own worker immediately (no blocking each other).
    assert sorted(b[0] for b in flow.batches) == ["a1", "b1"]
    flow.release()


async def test_worker_survives_a_flow_crash() -> None:
    class Crasher:
        def __init__(self) -> None:
            self.calls = 0

        async def handle_batch(self, msgs, chat) -> TurnOutcome:
            self.calls += 1
            raise RuntimeError("boom")

    flow = Crasher()
    co = MessageCoalescer(flow)
    co.submit(_msg("p1"), chat=FakeChat())
    await asyncio.sleep(0.01)
    # A second burst still gets processed (the crash didn't wedge the coalescer).
    co.submit(_msg("p2"), chat=FakeChat())
    await asyncio.sleep(0.01)
    assert flow.calls == 2


# -- tracked submits (turns the engine starts itself) --------------------------


class OutcomeFlow:
    """Answers with a canned outcome, or raises."""

    def __init__(self, outcome=TurnOutcome.REPLIED, error: Exception | None = None) -> None:
        self._outcome = outcome
        self._error = error

    async def handle_batch(self, msgs, chat) -> TurnOutcome:
        if self._error is not None:
            raise self._error
        return self._outcome


async def test_a_tracked_submit_reports_the_turns_outcome() -> None:
    co = MessageCoalescer(OutcomeFlow(TurnOutcome.EMPTY))

    outcome = await co.submit_tracked(_msg("p1"), chat=FakeChat())

    assert outcome is TurnOutcome.EMPTY


async def test_a_flow_that_crashes_still_answers_the_caller() -> None:
    # Otherwise a scheduled run would wait on a future nobody will ever resolve.
    co = MessageCoalescer(OutcomeFlow(error=RuntimeError("boom")))

    outcome = await co.submit_tracked(_msg("p1"), chat=FakeChat())

    assert outcome is TurnOutcome.ERROR


async def test_a_coalesced_batch_gives_every_waiter_the_same_ending() -> None:
    # A person wrote while a scheduled turn was queued: one turn, one ending.
    flow = SlowFlow()
    co = MessageCoalescer(flow)
    chat = FakeChat()

    co.submit(_msg("p1"), chat=chat)
    await asyncio.sleep(0)
    tracked = co.submit_tracked(_msg("p2"), chat=chat)
    typed = co.submit_tracked(_msg("p3"), chat=chat)
    flow.release()

    assert await tracked is TurnOutcome.REPLIED
    assert await typed is TurnOutcome.REPLIED
    assert flow.batches == [["p1"], ["p2", "p3"]]


async def test_an_untracked_message_leaves_nothing_behind() -> None:
    co = MessageCoalescer(OutcomeFlow())

    co.submit(_msg("p1"), chat=FakeChat())
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert co._tracked == {}


async def test_a_resolved_tracker_is_forgotten() -> None:
    co = MessageCoalescer(OutcomeFlow())

    await co.submit_tracked(_msg("p1"), chat=FakeChat())

    assert co._tracked == {}


async def test_shutdown_cancels_a_waiter_instead_of_stranding_it() -> None:
    flow = SlowFlow()
    co = MessageCoalescer(flow)

    pending = co.submit_tracked(_msg("p1"), chat=FakeChat())
    await asyncio.sleep(0)
    co._workers["c1"].cancel()  # what asyncio.run does to leftovers at exit

    with pytest.raises(asyncio.CancelledError):
        await pending
