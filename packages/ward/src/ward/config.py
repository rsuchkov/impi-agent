"""ward's settings, read from the environment / its own .env.

Flat names under a `WARD_` prefix, and deliberately few: this process holds the
credential to the secret store, so every knob is something an operator has to
understand before turning it.
"""

from functools import cached_property
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class WardSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="WARD_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Where its own state lives: the policies, the windows and the ledger. Its
    # own database, not the engine's — the engine has no business reading who
    # was allowed what.
    data_dir: str = "/var/lib/ward"

    # The secret store. Loopback by default because ward and the store share a
    # container: a hop that never reaches the network needs no TLS, which is
    # better than a hop that has some.
    vault_addr: str = "http://127.0.0.1:8200"
    vault_mount: str = "secrets"
    role_id: str = ""
    # Unattended unlock: both halves mounted as files. Convenient — a restart
    # needs no human, so a scheduled task at 3am still runs — and weaker in
    # exactly that way, since the files sit beside the store they open.
    unseal_key_file: str = ""
    secret_id_file: str = ""

    # The door agents and the operator come through.
    listen_host: str = "0.0.0.0"
    listen_port: int = 8425
    # The CA, ward's own certificate, and the key. Created by `ward init`.
    tls_dir: str = "/var/lib/ward/tls"
    # Where identities are handed OUT: the operator's, and later each agent's.
    # A deployment mounts the directory its clients read from, so `init` can put
    # the operator's certificate somewhere the operator can actually reach — the
    # authority's key stays behind, in tls_dir.
    issued_dir: str = "/var/lib/ward/issued"
    # Names ward's certificate is issued for — what a client verifies it reached.
    server_names: str = "ward,localhost,127.0.0.1"

    # Asking a human. ward posts as its own bot on purpose: an approver learns
    # that a request for a credential only ever comes from this account, so a
    # compromised agent bot can imitate the card but not its author.
    mattermost_url: str = "http://mattermost:8065"
    mattermost_token: str = ""
    approvers: str = ""  # usernames or user ids, CSV; nobody by default
    approval_channel: str = ""  # "" = a direct message to the first approver
    approval_timeout_s: float = 120.0
    max_grant_s: int = 3600

    # Where the click on an approval comes back to. Its own receiver, on its own
    # port, because the engine's belongs to the engine.
    callback_host: str = "0.0.0.0"
    callback_port: int = 8426
    callback_public_url: str = "http://ward:8426"

    log_level: str = "INFO"

    @cached_property
    def tls(self) -> Path:
        return Path(self.tls_dir)

    @cached_property
    def issued(self) -> Path:
        return Path(self.issued_dir)

    @property
    def ca_cert(self) -> Path:
        return self.tls / "ca.crt"

    @property
    def ca_key(self) -> Path:
        return self.tls / "ca.key"

    @property
    def server_cert(self) -> Path:
        return self.tls / "ward.crt"

    @property
    def server_key(self) -> Path:
        return self.tls / "ward.key"

    @property
    def db_path(self) -> Path:
        return Path(self.data_dir) / "ward.db"

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(n.strip() for n in self.server_names.split(",") if n.strip())

    @property
    def interact_url(self) -> str:
        return f"{self.callback_public_url.rstrip('/')}/interact"


def load_settings() -> WardSettings:
    return WardSettings()
