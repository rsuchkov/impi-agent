"""SessionProofBook: the per-session secret that lets the tool server believe a
call's ``X-Runtime-Session``.

The agent token says WHICH AGENT is calling and is one per agent: every process
that agent runs, in every conversation, holds the same one. The session id a
call carries is therefore a claim, and anything in an agent's environment that
has the token — the runtime, a shell it opened — could make the claim for a
conversation that is not its own, and so reach what that conversation's turn
holds: its channel, its user, and now whatever a turn's scope carries for it.

So each process is given a second secret for its session alone. The runtime
asks for one when it spawns the process (``issue``), the process sends it beside
the session id, and the server believes the id only when the two match. The
book lives in the tool layer because the server is what checks it; the runtime
sees only the ``SessionProofs`` port.

What this does and does not close. It stops a claim made from knowledge: a
model cannot name a conversation it was never told about, an agent with only
typed tools cannot name one at all, and a copy that outlived its process buys
nothing. It does not stop a process that can read another process's
environment — a shell, or the ``read`` built-in, run as the same user in the
same container sees every neighbour's ``/proc/<pid>/environ``, proofs included.
Against that the boundary is process isolation (an agent's own container), not
a secret; a kernel-attested caller identity would be the next step.
"""

import hmac
import secrets


class SessionProofBook:
    def __init__(self) -> None:
        self._by_session: dict[str, str] = {}

    def issue(self, session_id: str) -> str:
        """A fresh proof for this session — a respawn gets a new one, and the
        old process's copy stops working with it."""
        proof = secrets.token_hex(16)
        self._by_session[session_id] = proof
        return proof

    def revoke(self, session_id: str) -> None:
        self._by_session.pop(session_id, None)

    def verify(self, session_id: str, proof: str) -> bool:
        """Whether ``proof`` is the one issued for ``session_id`` and still
        current. Compared in constant time; a session never issued one fails."""
        expected = self._by_session.get(session_id)
        if expected is None:
            return False
        # Bytes, not str: compare_digest refuses non-ASCII strings with a
        # TypeError, and a header is whatever the caller put there.
        return hmac.compare_digest(expected.encode(), proof.encode())
