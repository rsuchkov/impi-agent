"""Composition: the whole of ward, wired from one settings object.

Small on purpose. ward is a chat client, a store, a Vault adapter, a broker and
two listeners — and nothing else. What it deliberately does not build is the
half of `crucible` that runs agents: no runtime, no gateways beyond the one
chat client, no flows, no scheduler. That absence is a security property, not an
omission, and the import contracts in `pyproject.toml` keep it true.
"""

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from mattermostautodriver import AsyncTypedDriver

from crucible.approvals import PendingApprovals
from crucible.gateways.mattermost import MattermostCallbackCodec, MattermostChatClient
from crucible.gateways.mattermost.options import driver_options
from crucible.interactions import InteractionDispatcher, InteractionsServer
from crucible.interactions.pending_ui import PendingUiRequests
from crucible.ports.chat.client import ChatClient
from ward.broker import SecretBroker
from ward.ca import CertificateAuthority
from ward.config import WardSettings
from ward.operations import Operations
from ward.ports import UnlockMaterial
from ward.server import WardServer, mutual_tls
from ward.store import WardStore
from ward.vault import VaultBackend

logger = logging.getLogger(__name__)


class OneBot:
    """ward talks as exactly one account, so "which agent's client" has one
    answer everywhere it is asked.

    Stands in for both the presence registry (which client posts for an agent)
    and the admin map (which client opens a direct message with an approver).
    The engine needs those keyed per agent because it runs many; ward runs none.
    """

    def __init__(self, chat: ChatClient) -> None:
        self._chat = chat

    def poster(self, agent: str) -> ChatClient:
        return self._chat

    def sink(self, agent: str) -> None:
        # ward runs no turns: a click on its card resolves a waiting request and
        # never becomes a conversation.
        return None

    def get(self, agent: str, default: object = None) -> ChatClient:
        """The admin-map half. Same account, whoever is asking."""
        return self._chat


@dataclass
class Ward:
    settings: WardSettings
    store: WardStore
    broker: SecretBroker
    door: WardServer
    callbacks: InteractionsServer


def build(settings: WardSettings) -> Ward:
    if not settings.ca_cert.is_file():
        raise SystemExit(
            f"no certificate authority at {settings.tls} — run `ward init` first"
        )
    Path(settings.data_dir).mkdir(parents=True, exist_ok=True)
    store = WardStore(settings.db_path)
    approvals = PendingApprovals()

    chat = MattermostChatClient(
        AsyncTypedDriver(driver_options(settings.mattermost_url, settings.mattermost_token))
    )
    presence = OneBot(chat)

    broker = SecretBroker(
        VaultBackend(
            settings.vault_addr, mount=settings.vault_mount, role_id=settings.role_id
        ),
        store,   # secret policies
        store,   # windows and the ledger
        presence,
        # The same one account posts the card and opens the direct message.
        presence,  # type: ignore[arg-type]  # a one-entry map, without the map
        approvals,
        approvers=settings.approvers,
        approval_channel=settings.approval_channel,
        approval_timeout_s=settings.approval_timeout_s,
        max_grant_s=settings.max_grant_s,
        callback_url=settings.interact_url,
    )

    ca = CertificateAuthority.load(settings.ca_cert, settings.ca_key)
    door = WardServer(
        broker,
        ca,
        Operations(broker.backend, store, store),
        host=settings.listen_host,
        port=settings.listen_port,
        ssl_context=mutual_tls(
            certificate=settings.server_cert, key=settings.server_key, ca=settings.ca_cert
        ),
    )
    callbacks = InteractionsServer(
        InteractionDispatcher(
            store, presence, PendingUiRequests(), store, approvals=approvals
        ),  # type: ignore[arg-type]
        MattermostCallbackCodec(),
        presence,  # type: ignore[arg-type]
        host=settings.callback_host,
        port=settings.callback_port,
    )
    logger.info(
        "ward built: store=%s, vault=%s, approvers=%s",
        settings.db_path, settings.vault_addr, settings.approvers or "(nobody)",
    )
    return Ward(settings, store, broker, door, callbacks)


async def _unlock(ward: Ward) -> None:
    """Open the store at startup, if the deployment keeps the material on disk.

    Nothing here is fatal: without the files ward starts locked, every request
    is refused, and the log says which state it is in.
    """
    material = UnlockMaterial(
        unseal_key=_read(ward.settings.unseal_key_file),
        auth_secret=_read(ward.settings.secret_id_file),
    )
    if not material:
        logger.info("secrets: locked — waiting to be unlocked")
        return
    try:
        state = await ward.broker.unlock(material)
    except Exception:
        logger.warning("secrets: could not unlock at startup", exc_info=True)
        return
    if not state.usable:
        logger.warning("secrets: still not usable — %s", state.detail or "sealed")


def _read(path: str) -> str:
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.warning("cannot read %s (%s)", path, exc.strerror)
        return ""


async def run(settings: WardSettings) -> None:
    ward = build(settings)
    await ward.callbacks.start()
    await ward.door.start()
    await _unlock(ward)
    try:
        # Nothing to drive: both listeners are servers. Sleep until told to stop.
        await asyncio.Event().wait()
    finally:
        await ward.door.stop()
        await ward.callbacks.stop()
        await ward.store.close()
