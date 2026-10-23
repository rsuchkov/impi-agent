"""Lease: one holder at a time, across every process that shares the store.

For whatever must run in exactly one place — a chat connection the platform
hands events to by its own choice, a job that must not run twice. A lease is
taken for a time and kept by taking it again before that time is up; a holder
that stops renewing loses it when it lapses, and somebody else takes it.
Compare-and-swap, so two processes asking at once get one yes. The store
implements it (``store.base.LeaseStore``); a consumer depends only on this.
"""

from typing import Protocol


class Lease(Protocol):
    async def acquire_lease(self, name: str, owner: str, *, ttl_s: float) -> bool:
        """True when ``owner`` now holds ``name`` — taken free or lapsed, or
        renewed when it was theirs already. False when another holder has it."""
        ...

    async def release_lease(self, name: str, owner: str) -> None:
        """Let ``name`` go, if ``owner`` holds it; somebody else's is left alone."""
        ...
