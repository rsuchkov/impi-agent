"""Sealing at rest with Fernet (AES-128-CBC + HMAC-SHA256), over a list of keys.

The first key seals; every key opens. So a new key is put first and the old
one kept after it until everything sealed under the old one has been resealed
or has ended — nobody is logged out by the change. A value no key opens
answers None, and the service treats that connection as gone.

Needs the ``cryptography`` package: ``crucible[connections]``.
"""

from collections.abc import Sequence

from cryptography.fernet import Fernet, InvalidToken, MultiFernet


class FernetSealer:
    def __init__(self, keys: Sequence[str]) -> None:
        if not keys:
            raise ValueError("no sealing key: give at least one Fernet key")
        try:
            self._fernet = MultiFernet([Fernet(key.encode("ascii")) for key in keys])
        except (ValueError, TypeError) as exc:
            raise ValueError("a sealing key is not a Fernet key (32 url-safe base64-encoded bytes)") from exc

    def seal(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def open(self, sealed: str) -> str | None:
        try:
            return self._fernet.decrypt(sealed.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError, UnicodeError):
            return None


def new_sealing_key() -> str:
    """A fresh key, for an operator to put in the application's settings."""
    return Fernet.generate_key().decode("ascii")
