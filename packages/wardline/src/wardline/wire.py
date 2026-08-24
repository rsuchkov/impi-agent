"""What every end of the secret protocol has to agree on: how a secret is named,
the two words a caller may be told when it does not get one, and the terms a
policy is written in.

This is the whole of the shared vocabulary. The two clients here and the broker
elsewhere are separate programs in separate containers, so what travels between
them lives in one module rather than in each of them. Everything about
*deciding* — backends, windows, the ledger — belongs to whoever answers, and is
not in this package at all.

Nothing here reaches a network or a disk.
"""

import re
from dataclasses import dataclass

# Both spellings mean the same thing: a reference the broker resolves against
# its backend. `vault://` is what the design and the operator's muscle memory
# use; `secret://` reads better when the backend is not Vault. There is
# deliberately no bare-name form — a reference has to be unmistakable in an
# argv, so that a literal value can never be mistaken for one, or the reverse.
SCHEMES = ("vault://", "secret://")

# Kept deliberately tight. This lands in a URL path on the backend, so anything
# that could climb out of the broker's own mount ("../", a slash, a space) is
# not a name. Lowercase-first also keeps names case-unambiguous across backends.
_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_FIELD = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

# The field inside a secret when the reference doesn't name one. Most secrets
# are a single string; the multi-field case (a username beside a password) is
# the exception that has to spell itself out.
DEFAULT_FIELD = "value"

# What the caller is told, and all it is told. Every authorization outcome
# collapses to REFUSED — a caller that could tell "no such secret" from "not
# yours" could enumerate the store by trying names. UNAVAILABLE is separate
# because it is not an authorization answer: it means the broker could not serve
# anyone right now, which is an operator's problem and reveals nothing about
# what exists.
WIRE_REFUSED = "refused"
WIRE_UNAVAILABLE = "unavailable"

# How a policy answers "does a human have to see this?". These travel in the
# body of a policy — the operator writes one, the broker stores it, and both
# have to spell it the same way. `never` is for the fully automatic ones (a key
# a nightly task needs); `always` means every use is asked about unless a live
# window already covers it.
APPROVAL_ALWAYS = "always"
APPROVAL_NEVER = "never"
APPROVALS = (APPROVAL_ALWAYS, APPROVAL_NEVER)


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
