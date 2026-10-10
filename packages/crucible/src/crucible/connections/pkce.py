"""The values a connect is started and recognised by (RFC 7636): a state the
person carries through the provider and back, and a PKCE verifier whose
challenge the provider binds the code to — so a code is useless to whoever
does not hold the verifier."""

import base64
import hashlib
import secrets


def new_state() -> str:
    return secrets.token_urlsafe(32)


def new_code_verifier() -> str:
    return secrets.token_urlsafe(48)  # 64 characters, within the RFC's 43–128


def code_challenge(verifier: str) -> str:
    """The S256 challenge: unpadded base64url of the verifier's SHA-256."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def state_digest(state: str) -> str:
    """How a link is keyed at rest: a reader of the store gets no state."""
    return hashlib.sha256(state.encode("utf-8")).hexdigest()
