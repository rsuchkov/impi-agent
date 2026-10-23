"""A gateway that runs only where its lease is held."""

import asyncio
import contextlib

from crucible.gateways import LeasedGateway
from crucible.ports.chat.gateway import AgentIdentity


class _Lease:
    """A lease book with one knob: who it says yes to."""

    def __init__(self, holder: str | None = None) -> None:
        self.holder = holder
        self.asked: list[tuple[str, str]] = []
        self.released: list[tuple[str, str]] = []

    async def acquire_lease(self, name: str, owner: str, *, ttl_s: float) -> bool:
        self.asked.append((name, owner))
        if self.holder is None or self.holder == owner:
            self.holder = owner
            return True
        return False

    async def release_lease(self, name: str, owner: str) -> None:
        self.released.append((name, owner))
        if self.holder == owner:
            self.holder = None


class _Gateway:
    def __init__(self) -> None:
        self.runs = 0
        self.stops = 0
        self._stop = asyncio.Event()

    async def login(self) -> AgentIdentity:
        return AgentIdentity(user_id="u", username="bot")

    async def run(self) -> None:
        self.runs += 1
        self._stop.clear()
        await self._stop.wait()

    async def stop(self) -> None:
        self.stops += 1
        self._stop.set()


async def _no_sleep(_: float) -> None:
    await asyncio.sleep(0)


async def test_the_gateway_runs_only_while_the_lease_is_held_and_stops_when_it_is_lost() -> None:
    lease = _Lease(holder="other:1")
    gateway = _Gateway()
    leased = LeasedGateway(gateway, lease, name="gateway:slack:assistant", owner="me:2", sleep=_no_sleep)

    task = asyncio.ensure_future(leased.run())
    await asyncio.sleep(0.01)
    assert gateway.runs == 0 and not leased.held  # waiting: somebody else holds it
    assert ("gateway:slack:assistant", "me:2") in lease.asked

    lease.holder = None  # the holder lapsed
    await asyncio.sleep(0.01)
    assert gateway.runs == 1 and leased.held

    lease.holder = "other:1"  # taken away from under us (our renewal is refused)
    await asyncio.sleep(0.01)
    assert gateway.stops == 1 and not leased.held
    assert gateway.runs == 1  # not running while another holds it

    lease.holder = None  # ...and it comes back
    await asyncio.sleep(0.01)
    assert gateway.runs == 2 and leased.held

    await leased.stop()
    await asyncio.wait_for(task, 1)
    assert lease.released[-1] == ("gateway:slack:assistant", "me:2")
    assert lease.holder is None


async def test_a_gateway_that_returns_releases_the_lease_and_the_wrapper_returns_too() -> None:
    lease = _Lease()
    gateway = _Gateway()
    leased = LeasedGateway(gateway, lease, name="g", owner="me:1", sleep=_no_sleep)
    task = asyncio.ensure_future(leased.run())
    await asyncio.sleep(0.01)
    assert leased.held
    await gateway.stop()  # the platform disconnected: the gateway's own run returns
    await asyncio.wait_for(task, 1)
    assert lease.released == [("g", "me:1")] and not leased.held
    assert await leased.login() == AgentIdentity(user_id="u", username="bot")


async def test_a_cancelled_holder_ends_its_renewal_before_stop_releases() -> None:
    """Shutdown cancels the run loop; a renewal left running would race the
    release and take the lease back for a process that is gone."""
    lease, gateway = _Lease(), _Gateway()
    renewals: list[str] = []
    waiting = asyncio.Event()

    async def sleep(_: float) -> None:
        renewals.append("sleep")
        waiting.set()
        await asyncio.Event().wait()  # a renewal mid-sleep when the shutdown comes

    leased = LeasedGateway(gateway, lease, name="gateway:slack:a", owner="pod-a", sleep=sleep)
    task = asyncio.create_task(leased.run())
    await waiting.wait()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert leased.held is True  # until stop() says otherwise
    asked_before = len(lease.asked)
    await leased.stop()
    assert lease.holder is None and leased.held is False
    await asyncio.sleep(0)
    assert len(lease.asked) == asked_before  # nothing renewed after the release
