"""LoopGuard: bounds agent-to-agent cascades so they can't run away.

Two independent limits (either can veto a turn triggered by another agent):

- **hop depth** — every agent reply is stamped with its distance from the last
  human message; a turn deeper than ``max_hops`` is refused. This caps a single
  cascade regardless of timing.
- **rate limit** — a sliding window per conversation caps how many
  agent-triggered turns may run, bounding cascades that span several human
  messages or slip under the hop cap.

Human-triggered turns are never limited — only turns an agent triggers pass
through :meth:`check`. State is in-memory (a restart resets counters, which is
acceptable — a fresh process can't be mid-loop). One shared instance across all
gateways makes the rate limit a global per-conversation bound.
"""

import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class LoopDecision:
    allow: bool
    reason: str = ""


class LoopGuard:
    def __init__(
        self,
        *,
        max_hops: int = 4,
        max_agent_turns: int = 6,
        window_s: float = 60.0,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_hops = max_hops
        self._max_turns = max_agent_turns
        self._window_s = window_s
        self._now = now
        self._turns: dict[str, deque[float]] = defaultdict(deque)

    def check(self, *, conversation_id: str, hop_depth: int) -> LoopDecision:
        """Called only for a turn triggered by ANOTHER agent. On allow it also
        records the turn against the rate window (so it must not be called twice
        per turn)."""
        if hop_depth >= self._max_hops:
            return LoopDecision(False, f"hop depth {hop_depth} >= cap {self._max_hops}")

        now = self._now()
        window = self._turns[conversation_id]
        cutoff = now - self._window_s
        while window and window[0] < cutoff:
            window.popleft()
        if len(window) >= self._max_turns:
            return LoopDecision(
                False, f"rate limit {self._max_turns}/{self._window_s:.0f}s"
            )
        window.append(now)
        return LoopDecision(True)
