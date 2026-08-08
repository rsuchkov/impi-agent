"""Gateway supervision: one agent's WS failure is isolated + retried."""

import asyncio

import pytest

from impi.app import _supervise


async def _fast_sleep(_delay: float) -> None:
    # Yield to the loop (so sibling tasks run) but without a real delay.
    await asyncio.sleep(0)


class FlakyGateway:
    """run() fails `fail_times`, then returns cleanly (or hangs if never)."""

    def __init__(self, fail_times: int, *, then_hang: bool = False) -> None:
        self.fail_times = fail_times
        self.then_hang = then_hang
        self.runs = 0

    async def login(self) -> str:  # pragma: no cover - unused here
        return "id"

    async def run(self) -> None:
        self.runs += 1
        if self.runs <= self.fail_times:
            raise RuntimeError(f"ws drop {self.runs}")
        if self.then_hang:
            await asyncio.Event().wait()

    async def stop(self) -> None:  # pragma: no cover
        pass


async def test_supervise_retries_until_clean_return() -> None:
    gw = FlakyGateway(fail_times=3)

    await asyncio.wait_for(_supervise("assistant", gw.run, sleep=_fast_sleep), timeout=1.0)

    assert gw.runs == 4  # 3 failures retried, 4th returns cleanly


async def test_one_gateway_failure_does_not_kill_others() -> None:
    # The whole-engine bug: a crashing gateway must not cancel a healthy one.
    flaky = FlakyGateway(fail_times=2)
    healthy = FlakyGateway(fail_times=0, then_hang=True)

    healthy_task = asyncio.ensure_future(_supervise("healthy", healthy.run, sleep=_fast_sleep))
    await asyncio.wait_for(_supervise("flaky", flaky.run, sleep=_fast_sleep), timeout=1.0)

    # flaky recovered; healthy kept running the whole time.
    assert flaky.runs == 3
    assert not healthy_task.done()
    assert healthy.runs == 1
    healthy_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await healthy_task
