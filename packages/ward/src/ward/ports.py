"""The broker's own vocabulary: what a request is, what a decision is, and what
a backend must be able to do.

None of this crosses the wire as-is — the shared spelling of a reference and the
two words a caller may be told live in ``wardline.wire``, because the
client needs them too. What is here is the deciding side, and it stays on the
side of the door that holds the credential.

Nothing here reaches a network or a disk. The backend is a Protocol so the
broker's own tests run on a fake — the alternative would be a live Vault for
every assertion about who may see what, which is exactly the logic that most
needs cheap tests.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from crucible.ports.chat.client import ChatClient
from ward.decisions import UNAVAILABLE_DECISIONS
from wardline.wire import WIRE_REFUSED, WIRE_UNAVAILABLE, SecretRef


class SecretBackendError(Exception):
    """The backend could not answer. Carries whether it was sealed, because that
    is the one failure with a specific remedy the operator can act on."""

    def __init__(self, message: str, *, sealed: bool = False) -> None:
        super().__init__(message)
        self.sealed = sealed


@dataclass(frozen=True)
class UnlockMaterial:
    """What a backend needs to become usable again after a restart.

    Two parts because Vault has two: a key that makes the store readable at all,
    and a credential that says who is reading. A backend that has only one
    notion of "the key" leaves the other empty.
    """

    unseal_key: str = ""
    auth_secret: str = ""

    def __bool__(self) -> bool:
        return bool(self.unseal_key or self.auth_secret)


@dataclass(frozen=True)
class BackendStatus:
    """Why the broker can or cannot serve right now.

    Three separate facts, because they have three different remedies: the
    backend is unreachable (fix the deployment), it is sealed (unseal it), or
    the broker holds no credential for it (unlock the broker).
    """

    reachable: bool = False
    sealed: bool = True
    authenticated: bool = False
    detail: str = ""

    @property
    def usable(self) -> bool:
        return self.reachable and not self.sealed and self.authenticated


def wire_status(decision: str) -> str:
    """Collapse a ledger decision into the two things a caller may learn."""
    return WIRE_UNAVAILABLE if decision in UNAVAILABLE_DECISIONS else WIRE_REFUSED


@dataclass(frozen=True)
class LeaseRequest:
    """One invocation of ``secret-exec``: the fields of one or more secrets bound
    to environment variables, why, and what it intends to run.

    Several secrets in one request are served together or not at all. Asking
    about them one at a time would double the questions a human answers for a
    single operation, and approval fatigue is what defeats a system like this
    long before anything clever does. The card lists all of them, the most
    restrictive policy governs the window, and a refusal on any one refuses the
    whole request rather than handing back half an environment.

    ``command`` is not decoration. It is what the human is shown before
    deciding, and the only defence against a caller that asks for a secret in
    order to print it rather than to use it.
    """

    agent: str
    runtime_session_id: str
    bindings: tuple[tuple[str, SecretRef], ...]  # (env var name, reference)
    reason: str = ""
    command: tuple[str, ...] = ()

    @property
    def names(self) -> tuple[str, ...]:
        """The distinct secret names asked for, in the order first requested."""
        return tuple(dict.fromkeys(ref.name for _, ref in self.bindings))

    @property
    def references(self) -> tuple[str, ...]:
        """How the caller wrote each reference — what the human is shown, so
        they approve the thing that was actually asked for."""
        return tuple(dict.fromkeys(str(ref) for _, ref in self.bindings))


@dataclass(frozen=True)
class LeaseResult:
    """What the broker decided. ``values`` is populated only when granted, and
    is never logged, stored or echoed."""

    granted: bool
    decision: str
    values: Mapping[str, str] = field(default_factory=dict)


class AgentPosters(Protocol):
    """Where the broker finds the chat client to ask through.

    Declared here rather than imported from the interactions layer so the
    dependency runs one way: that layer routes the click that answers an
    approval, and therefore must not have to know about this one.
    ``AgentPresence`` satisfies this structurally, which is all a composition
    root has to pass.
    """

    def poster(self, agent: str) -> ChatClient | None: ...


class SecretLeasing(Protocol):
    """What the door needs from the broker.

    Narrow on purpose. Two of these three verbs are reachable by an agent, so
    the only things exposed are what an agent may legitimately trigger: asking
    for a secret, and — knowing the key — unlocking the broker. Reading a list
    of names, writing a value or editing a policy are operator verbs and have no
    route from an agent at all.
    """

    async def lease(self, request: LeaseRequest) -> LeaseResult: ...

    async def unlock(self, material: UnlockMaterial) -> BackendStatus: ...

    async def status(self) -> BackendStatus: ...


class SecretBackend(Protocol):
    """Where the values actually live.

    Deliberately narrow: the broker owns policy, approval, grants and the
    ledger, so a backend only has to store bytes, say whether it is usable, and
    be openable after a restart.
    """

    async def status(self) -> BackendStatus: ...

    async def unlock(self, material: UnlockMaterial) -> BackendStatus:
        """Make the backend usable with the material a human (or a mounted file)
        supplied. Idempotent: unlocking an already-usable backend is a no-op."""
        ...

    async def read(self, ref: SecretRef) -> str:
        """The value, or SecretBackendError. A missing secret raises like any
        other failure — the broker decides what the caller is told."""
        ...

    async def write(self, name: str, values: Mapping[str, str]) -> None: ...

    async def delete(self, name: str) -> None: ...

    async def names(self) -> list[str]:
        """Every secret stored. Operator-facing only — there is no path from an
        agent to this method, because a list of names is a list of things to
        try."""
        ...

    async def close(self) -> None: ...
