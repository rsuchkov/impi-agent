"""The vocabulary of the secret broker: what a caller asks for, what a backend
must be able to do, and how a secret is named.

Nothing here reaches a network or a disk. The backend is a Protocol so the
broker's own tests run on a fake — the alternative would be a live Vault for
every assertion about who may see what, which is exactly the logic that most
needs cheap tests.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from crucible.ports.chat.client import ChatClient

# Both spellings mean the same thing: a reference resolved by the engine's
# backend. `vault://` is what the design and the operator's muscle memory use;
# `secret://` reads better when the backend is not Vault. There is deliberately
# no bare-name form — a reference has to be unmistakable in an argv, so that a
# literal value can never be mistaken for one, or the reverse.
SCHEMES = ("vault://", "secret://")

# Kept deliberately tight. This lands in a URL path on the backend, so anything
# that could climb out of the engine's own mount ("../", a slash, a space) is
# not a name. Lowercase-first also keeps names case-unambiguous across backends.
_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_FIELD = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

# The field inside a secret when the reference doesn't name one. Most secrets
# are a single string; the multi-field case (a username beside a password) is
# the exception that has to spell itself out.
DEFAULT_FIELD = "value"


class SecretBackendError(Exception):
    """The backend could not answer. Carries whether it was sealed, because that
    is the one failure with a specific remedy the operator can act on."""

    def __init__(self, message: str, *, sealed: bool = False) -> None:
        super().__init__(message)
        self.sealed = sealed


@dataclass(frozen=True)
class SecretRef:
    """Which secret, and which field of it."""

    name: str
    field: str = DEFAULT_FIELD

    def __str__(self) -> str:
        return f"vault://{self.name}" + (f"#{self.field}" if self.field != DEFAULT_FIELD else "")


def parse_ref(raw: str) -> SecretRef:
    """``vault://github-token`` / ``secret://smtp#password`` -> a SecretRef.

    Raises ValueError with a caller-safe message: it says the reference is
    malformed, never whether the named secret exists — that distinction is the
    thing the broker works hardest not to leak.
    """
    text = raw.strip()
    for scheme in SCHEMES:
        if text.startswith(scheme):
            text = text[len(scheme) :]
            break
    else:
        raise ValueError(f"not a secret reference (expected {SCHEMES[0]}…): {raw!r}")
    name, sep, field_name = text.partition("#")
    field_name = field_name if sep else DEFAULT_FIELD
    if not _NAME.match(name):
        raise ValueError(f"malformed secret name: {name!r}")
    if not _FIELD.match(field_name):
        raise ValueError(f"malformed secret field: {field_name!r}")
    return SecretRef(name=name, field=field_name)


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
    the engine holds no credential for it (unlock the engine).
    """

    reachable: bool = False
    sealed: bool = True
    authenticated: bool = False
    detail: str = ""

    @property
    def usable(self) -> bool:
        return self.reachable and not self.sealed and self.authenticated


# What the caller is told, and all it is told. Every authorization outcome
# collapses to REFUSED — a caller that could tell "no such secret" from "not
# yours" could enumerate the store by trying names. UNAVAILABLE is separate
# because it is not an authorization answer: it means the engine could not serve
# anyone right now, which is an operator's problem and reveals nothing about
# what exists.
WIRE_REFUSED = "refused"
WIRE_UNAVAILABLE = "unavailable"

_UNAVAILABLE_DECISIONS = frozenset({"locked", "sealed", "backend_error"})


def wire_status(decision: str) -> str:
    """Collapse an audit decision into the two things a caller may learn."""
    return WIRE_UNAVAILABLE if decision in _UNAVAILABLE_DECISIONS else WIRE_REFUSED


@dataclass(frozen=True)
class LeaseRequest:
    """One invocation of ``secret-exec``: the fields of ONE secret bound to
    environment variables, why, and what it intends to run.

    One secret per invocation is deliberate. It keeps the approval card
    unambiguous (a human approves a named thing, not a basket), keeps one
    request to one ledger row, and costs nothing in expressiveness: several
    fields of the same secret are one request, and two different secrets nest —
    ``secret-exec --env A=… -- secret-exec --env B=… -- cmd`` — which asks about
    each of them separately, as it should.

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
    def secret(self) -> str:
        """The one secret this request is about."""
        found = self.names
        if len(found) != 1:
            raise ValueError("a request names exactly one secret")
        return found[0]

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
    approval, and therefore knows about this package. ``AgentPresence``
    satisfies this structurally, which is all the composition root has to pass.
    """

    def poster(self, agent: str) -> ChatClient | None: ...


class SecretLeasing(Protocol):
    """What the tool server needs from the broker.

    Narrow on purpose. The server is reachable from inside the container, which
    is where the agents are, so the only things exposed there are the two an
    agent may legitimately trigger: asking for a secret, and — knowing the key —
    unlocking the engine. Reading a list of names, writing a value or editing a
    policy are operator verbs and have no route at all.
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
