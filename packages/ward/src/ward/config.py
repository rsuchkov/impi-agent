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
    # Where the AGENTS' identities are handed out. Mounted by the engine, whose
    # container is where the agents run — so nothing an agent must not have goes
    # in here.
    issued_dir: str = "/var/lib/ward/issued"
    # And where the OPERATOR's goes, which is a different directory for exactly
    # that reason: an operator certificate beside the agents' is an operator
    # certificate any agent can read, and administering the broker would stop
    # being something only a human does.
    operator_dir: str = "/var/lib/ward/operator"
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

    # The slash command's tokens (CSV), as Mattermost minted them when the
    # command was registered. Empty = no operator surface in chat at all: the
    # receiver refuses a command it has no token for, so the feature is off
    # until somebody registers the command AND puts its token here.
    #
    # Operator-grade. Anything that reaches the receiver with this token can
    # claim to be any user, so the check that follows it — the approver list —
    # is what actually decides. Not shared with the engine's own commands.
    command_tokens: str = ""

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

    @cached_property
    def operator(self) -> Path:
        return Path(self.operator_dir)

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
    def tokens(self) -> tuple[str, ...]:
        return tuple(t.strip() for t in self.command_tokens.split(",") if t.strip())

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(n.strip() for n in self.server_names.split(",") if n.strip())

    @property
    def interact_url(self) -> str:
        return f"{self.callback_public_url.rstrip('/')}/interact"

    @property
    def dialog_url(self) -> str:
        """Where a modal's submission comes back to. A dialog the platform has
        nowhere to send is a dialog it refuses to open at all."""
        return f"{self.callback_public_url.rstrip('/')}/dialog"


def load_settings() -> WardSettings:
    return WardSettings()
