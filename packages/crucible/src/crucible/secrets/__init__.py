"""A human-approved secret broker: the authorization layer between an agent and
a credential.

The engine never hands a secret to a model. An agent asks by running
``secret-exec``, which names the secret it wants and the command it wants to run;
the broker checks the policy, asks a human when the policy says to, reads the
value from the backend and injects it straight into the child process. The value
exists in the agent's process tree, never in its context window.
"""

from crucible.secrets.ports import (
    BackendStatus,
    LeaseRequest,
    LeaseResult,
    SecretBackend,
    SecretBackendError,
    SecretRef,
    UnlockMaterial,
    parse_ref,
)

__all__ = [
    "BackendStatus",
    "LeaseRequest",
    "LeaseResult",
    "SecretBackend",
    "SecretBackendError",
    "SecretRef",
    "UnlockMaterial",
    "parse_ref",
]
