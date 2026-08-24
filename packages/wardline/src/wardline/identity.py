"""Where to ask, and what to prove — worked out from the environment alone.

Nothing hands these programs a config object. `secret-exec` runs inside an
agent's shell and `ward-admin` runs wherever an operator runs it, so the
environment is the only thing both are given, and the engine that spawned one of
them is not supposed to know this protocol exists.

Two variables carry a deployment:

* ``SECRET_BROKER_URL`` — where the broker answers.
* ``SECRET_BROKER_CERTS_DIR`` — where the identities are mounted.

Everything else is derived. An agent's certificate is its own name inside that
directory, and the name comes from ``AGENT_NAME``, which the engine gives every
agent as a plain fact about itself. The operator's is ``operator.crt`` in the
same place. Explicit paths override any of it, for a deployment that mounts them
somewhere else.

Deriving the agent's path from a name in the environment is not a way of proving
who is asking — the certificate is, on the handshake. The name only picks which
file to read, and an agent that named another agent would be reading a file it
can already read: same container, same user, one directory. See docs/secrets.md
for what that does and does not buy.
"""

import os
import ssl
from dataclasses import dataclass
from pathlib import Path

ENV_URL = "SECRET_BROKER_URL"
ENV_CERTS_DIR = "SECRET_BROKER_CERTS_DIR"
ENV_CA = "SECRET_BROKER_CA"
# An agent's own identity: derived from its name, or pointed at explicitly.
ENV_AGENT = "AGENT_NAME"
ENV_CERT = "SECRET_BROKER_CERT"
ENV_KEY = "SECRET_BROKER_KEY"
# The operator's, which is a different certificate and a different set of routes.
ENV_OPERATOR_CERT = "SECRET_BROKER_OPERATOR_CERT"
ENV_OPERATOR_KEY = "SECRET_BROKER_OPERATOR_KEY"

OPERATOR = "operator"


class IdentityError(Exception):
    """Nothing to ask with, or nowhere to ask. Always says which variable would
    have answered it — this is the error a fresh deployment hits first."""


@dataclass(frozen=True)
class Identity:
    """An address, and the three files that prove who is calling."""

    url: str
    certificate: Path
    key: Path
    ca: Path

    def context(self) -> ssl.SSLContext:
        """The TLS context to call the broker with.

        Verifying the broker's certificate is half the point: a client that
        would talk to anything answering on that address could be told to hand
        its request somewhere else.
        """
        try:
            context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(self.ca))
            context.load_cert_chain(certfile=str(self.certificate), keyfile=str(self.key))
        except OSError as exc:
            raise IdentityError(
                f"cannot read the identity ({exc.strerror}): {self.certificate}"
            ) from exc
        return context


def agent_identity(environ: dict[str, str] | None = None) -> Identity:
    """The identity of the agent this process is running as."""
    env = dict(os.environ if environ is None else environ)
    url, certs = _address(env)
    explicit_cert, explicit_key = env.get(ENV_CERT, ""), env.get(ENV_KEY, "")
    if explicit_cert and explicit_key:
        return Identity(url, Path(explicit_cert), Path(explicit_key), _ca(env, certs))
    agent = env.get(ENV_AGENT, "").strip()
    if not agent:
        raise IdentityError(
            f"no identity to ask with: neither {ENV_AGENT} (to derive one from "
            f"{ENV_CERTS_DIR}) nor {ENV_CERT}/{ENV_KEY}"
        )
    if not certs:
        raise IdentityError(f"no {ENV_CERTS_DIR}, so there is nowhere to find {agent}'s identity")
    return Identity(url, certs / f"{agent}.crt", certs / f"{agent}.key", _ca(env, certs))


def operator_identity(environ: dict[str, str] | None = None) -> Identity:
    """The operator's identity — the one the administrative routes answer to."""
    env = dict(os.environ if environ is None else environ)
    url, certs = _address(env)
    cert = env.get(ENV_OPERATOR_CERT, "")
    key = env.get(ENV_OPERATOR_KEY, "")
    if not cert or not key:
        if not certs:
            raise IdentityError(
                f"no operator identity: set {ENV_OPERATOR_CERT} and {ENV_OPERATOR_KEY}, "
                f"or {ENV_CERTS_DIR} to derive them from"
            )
        cert = cert or str(certs / f"{OPERATOR}.crt")
        key = key or str(certs / f"{OPERATOR}.key")
    missing = [path for path in (cert, key) if not Path(path).is_file()]
    if missing:
        raise IdentityError(f"no operator identity at: {', '.join(missing)}")
    return Identity(url, Path(cert), Path(key), _ca(env, certs))


def certs_dir(environ: dict[str, str] | None = None) -> Path | None:
    """Where the identities are mounted, if this deployment says so. None is a
    normal answer — a caller that was given explicit paths never needs it."""
    env = os.environ if environ is None else environ
    raw = env.get(ENV_CERTS_DIR, "").strip()
    return Path(raw) if raw else None


def _address(env: dict[str, str]) -> tuple[str, Path | None]:
    url = env.get(ENV_URL, "").strip().rstrip("/")
    if not url:
        raise IdentityError(
            f"no secret broker in this deployment ({ENV_URL} is unset) — see docs/secrets.md"
        )
    raw = env.get(ENV_CERTS_DIR, "").strip()
    return url, Path(raw) if raw else None


def _ca(env: dict[str, str], certs: Path | None) -> Path:
    """The authority both sides verify against. Beside the certificates unless
    it is pointed elsewhere — it is the one file every identity shares."""
    explicit = env.get(ENV_CA, "").strip()
    if explicit:
        return Path(explicit)
    if certs is None:
        raise IdentityError(f"no {ENV_CA}, and no {ENV_CERTS_DIR} to find one beside")
    return certs / "ca.crt"
