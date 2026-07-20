"""MessageCoalescer: a burst on one conversation folds into fewer turns."""

import asyncio

from crucible.ports.chat.types import KIND_DM, ConversationRef, IncomingMessage
from crucible.flows.coalescer import MessageCoalescer
from tests.fakes.fake_chat import FakeChat


class SlowFlow:
    """Records each batch; each handle_batch takes a controllable step."""

    def __init__(self) -> None:
        self.batches: list[list[str]] = []
        self._gate = asyncio.Event()

    def release(self) -> None:
        self._gate.set()

    async def handle_batch(self, msgs, chat) -> None:
        self.batches.append([m.ref.message_id for m in msgs])
        await self._gate.wait()  # hold the turn open so more messages pile up


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

        async def handle_batch(self, msgs, chat) -> None:
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
