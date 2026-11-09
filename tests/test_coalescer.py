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


async def test_two_gateways_messages_of_one_conversation_queue_behind_one_turn() -> None:
    """Keyed by the application, a Slack thread and a panel dialog about one
    task are one conversation: a message from the second gateway waits for
    the turn the first one started, while another task runs beside it."""

    class _Key:
        def key(self, agent, msg):
            return msg.conversation_id.split(":")[-1]

    flow = SlowFlow()
    co = MessageCoalescer(flow, agent="assistant", keying=_Key())
    chat = FakeChat()
    co.submit(_msg("p1", conv="slack:task-7"), chat)
    await asyncio.sleep(0)
    co.submit(_msg("p2", conv="portal:task-7"), chat)  # same task: waits for p1's turn
    co.submit(_msg("p3", conv="portal:task-8"), chat)  # another task: runs now
    await asyncio.sleep(0.05)
    assert flow.batches == [["p1"], ["p3"]]
    flow.release()
    await asyncio.sleep(0.05)
    assert flow.batches == [["p1"], ["p3"], ["p2"]]


async def test_the_coalescer_stops_the_turn_through_its_flow_when_the_flow_can() -> None:
    """The coalescer is the sink a gateway holds; stopping is the flow's. One
    that cannot stop a turn makes the coalescer answer False, never raise."""

    class StoppingFlow(OutcomeFlow):
        def __init__(self) -> None:
            super().__init__()
            self.stopped: list[tuple[str, str]] = []

        async def stop(self, conversation_id: str, *, gateway: str = "") -> bool:
            self.stopped.append((conversation_id, gateway))
            return True

    flow = StoppingFlow()
    assert await MessageCoalescer(flow).stop("dm1", gateway="slack") is True
    assert flow.stopped == [("dm1", "slack")]
    assert await MessageCoalescer(OutcomeFlow()).stop("dm1") is False


class SteeringFlow(SlowFlow):
    """A flow that also takes a message into the running turn, and says at
    the end whether the model read it."""

    def __init__(self, *, reads: bool = True) -> None:
        super().__init__()
        self.steered: list[str] = []
        self.joined: list = []
        self.reads = reads

    async def join(self, msg, chat):
        from crucible.ports.chat.flow import Joined

        self.steered.append(msg.ref.message_id)
        joined = Joined(message=msg, read=asyncio.get_running_loop().create_future())
        self.joined.append(joined)
        return joined

    async def handle_batch(self, msgs, chat) -> TurnOutcome:
        outcome = await super().handle_batch(msgs, chat)
        for joined in self.joined:
            if not joined.read.done():
                joined.read.set_result(self.reads)
        return outcome


async def test_with_joining_on_a_message_during_a_turn_goes_to_that_turn_and_a_burst_before_one_still_coalesces() -> None:
    flow = SteeringFlow()
    co = MessageCoalescer(flow, join_running_turns=True)
    co.submit(_msg("p1"), chat=FakeChat())
    await asyncio.sleep(0)
    co.submit(_msg("p2"), chat=FakeChat())  # mid-turn: joins it
    await asyncio.sleep(0.01)
    assert flow.steered == ["p2"] and flow.batches == [["p1"]]
    # The sink's own steer, for a gateway that asks before it submits: a conversation with no turn says None.
    assert await co.join(_msg("q1", conv="cQ"), FakeChat()) is None
    assert flow.steered == ["p2"]
    assert (await co.join(_msg("p3"), FakeChat())) is not None and flow.steered == ["p2", "p3"]
    flow.release()
    await asyncio.sleep(0.02)
    assert flow.batches == [["p1"]]  # both were read in the turn: no next turn
    # Off (the default), a message during a turn waits for the next one, as always.
    plain = SteeringFlow()
    co2 = MessageCoalescer(plain)
    co2.submit(_msg("a1"), chat=FakeChat())
    await asyncio.sleep(0)
    co2.submit(_msg("a2"), chat=FakeChat())
    plain.release()
    await asyncio.sleep(0.02)
    assert plain.steered == [] and plain.batches == [["a1"], ["a2"]]


async def test_a_joined_message_the_turn_did_not_read_becomes_the_next_turn() -> None:
    flow = SteeringFlow(reads=False)
    co = MessageCoalescer(flow, join_running_turns=True)
    co.submit(_msg("p1"), chat=FakeChat())
    await asyncio.sleep(0)
    co.submit(_msg("p2"), chat=FakeChat())
    await asyncio.sleep(0.01)
    assert flow.steered == ["p2"]
    flow.release()
    await asyncio.sleep(0.03)
    assert flow.batches == [["p1"], ["p2"]]  # not read: queued, and run as the next turn
    # A message with files, or a synthetic one, never joins: it queues.
    from dataclasses import replace

    from crucible.ports.chat.types import Attachment

    flow2 = SteeringFlow()
    co3 = MessageCoalescer(flow2, join_running_turns=True)
    co3.submit(_msg("b1"), chat=FakeChat())
    await asyncio.sleep(0)
    co3.submit(replace(_msg("b2"), attachments=(Attachment(name="a.txt", path="/tmp/a.txt", mime="text/plain", size=1),)), chat=FakeChat())
    co3.submit(replace(_msg("b3"), synthetic=True), chat=FakeChat())
    flow2.release()
    await asyncio.sleep(0.03)
    assert flow2.steered == [] and flow2.batches == [["b1"], ["b2", "b3"]]
    # A flow that will not take a message (another person's, say) says None: it queues for the next turn.
    class _Picky(SteeringFlow):
        async def join(self, msg, chat):
            return None if msg.user_id != "u1" else await super().join(msg, chat)

    picky = _Picky()
    co4 = MessageCoalescer(picky, join_running_turns=True)
    co4.submit(_msg("d1"), chat=FakeChat())
    await asyncio.sleep(0)
    co4.submit(replace(_msg("d2"), user_id="u2"), chat=FakeChat())
    await asyncio.sleep(0.01)
    picky.release()
    await asyncio.sleep(0.03)
    assert picky.steered == [] and picky.batches == [["d1"], ["d2"]]


async def test_messages_that_joined_and_were_never_read_all_run_once_the_turn_ends() -> None:
    """Whatever joined the turn and was not read comes back to the queue as
    the turn ends — none is lost, each runs; the order among them and what
    arrived meanwhile is the loop's."""
    flow = SteeringFlow(reads=False)
    co = MessageCoalescer(flow, join_running_turns=True)
    co.submit(_msg("p1"), chat=FakeChat())
    await asyncio.sleep(0)
    co.submit(_msg("p2"), chat=FakeChat())  # joins; the turn will not read it
    await asyncio.sleep(0.01)
    flow.release()
    co.submit(_msg("p3"), chat=FakeChat())  # arrives as the turn ends
    await asyncio.sleep(0.03)
    assert flow.batches[0] == ["p1"] and sorted(m for b in flow.batches[1:] for m in b) == ["p2", "p3"]
