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
from typing import Protocol

from mattermostautodriver import AsyncTypedDriver
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from crucible.approvals import PendingApprovals
from crucible.gateways.mattermost import MattermostCallbackCodec, MattermostChatClient
from crucible.gateways.mattermost.options import driver_options
from crucible.gateways.slack import SlackChatClient, SlackInteractions
from crucible.interactions import InteractionDispatcher, InteractionsServer
from crucible.interactions.pending_ui import PendingUiRequests
from crucible.interactions.ports import FormHandlers
from crucible.interactions.screens import ScreenRegistry
from crucible.ports.chat.client import ChatClient
from ward.approvers import Approvers
from ward.broker import SecretBroker
from ward.ca import CertificateAuthority
from ward.chatops import COMMAND, OperatorForms, PendingOperatorForms, WardScreen
from ward.chatops import HANDLER as WARD_HANDLER
from ward.config import WardSettings
from ward.operations import Operations
from ward.ports import UnlockMaterial
from ward.server import WardServer, mutual_tls
from ward.store import WardStore
from ward.vault import VaultBackend, wait_for_store

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


class Listener(Protocol):
    """What brings the clicks back: the HTTP receiver on Mattermost, the socket
    on Slack. Started after the broker is built, stopped when it goes."""

    async def start(self) -> None: ...
    async def stop(self) -> None: ...


class ChatSide(Protocol):
    """The chat platform as ward needs it, and nothing more: one account that
    posts the cards and opens the direct messages, a sign-in, and a listener
    for the answers. Which platform it is stays behind this."""

    @property
    def chat(self) -> ChatClient: ...  # and the ChatAdmin — both adapters implement both

    @property
    def callback_url(self) -> str: ...  # "" where clicks come over the platform's socket

    @property
    def variables(self) -> str: ...  # named in the log when signing in fails

    async def sign_in(self) -> str: ...
    def listener(self, dispatcher: InteractionDispatcher, presence: OneBot) -> Listener: ...


class _Mattermost:
    variables = "WARD_MATTERMOST_TOKEN and _URL"

    def __init__(self, settings: WardSettings) -> None:
        self._settings = settings
        # Kept because it has to be signed in before anything can be posted —
        # a token in the driver's options is not a session.
        self.driver = AsyncTypedDriver(
            driver_options(settings.mattermost_url, settings.mattermost_token)
        )
        self.chat = MattermostChatClient(self.driver)
        self.callback_url = settings.interact_url

    async def sign_in(self) -> str:
        me = await self.driver.login()
        return me.get("username", "?")

    def listener(self, dispatcher: InteractionDispatcher, presence: OneBot) -> Listener:
        # Mattermost calls back over HTTP: clicks, dialog submits and the slash
        # command, each verified by the codec and the command token.
        return InteractionsServer(
            dispatcher,
            MattermostCallbackCodec(),
            presence,  # type: ignore[arg-type]
            host=self._settings.callback_host,
            port=self._settings.callback_port,
            dialog_submit_url=self._settings.dialog_url,
            command_tokens=lambda _agent: self._settings.tokens,
        )


class _Socket:
    """The Socket Mode connection as a listener. Opened, not driven: the process
    has other things to serve, and the handler runs on the loop by itself."""

    def __init__(self, handler: AsyncSocketModeHandler) -> None:
        self._handler = handler

    async def start(self) -> None:
        await self._handler.connect_async()

    async def stop(self) -> None:
        await self._handler.close_async()


class _Slack:
    variables = "WARD_SLACK_BOT_TOKEN and _APP_TOKEN"

    def __init__(self, settings: WardSettings) -> None:
        self._app_token = settings.slack_app_token
        self.app = AsyncApp(token=settings.slack_bot_token)
        self.chat = SlackChatClient(self.app.client)
        # Clicks come down the socket, routed by action id; a card carries no
        # address to call back to.
        self.callback_url = ""

    async def sign_in(self) -> str:
        auth = await self.app.client.auth_test()
        return auth.get("user", "?")

    def listener(self, dispatcher: InteractionDispatcher, presence: OneBot) -> Listener:
        # The interactive half of a Slack app, and only that: ward runs no
        # agents, so there is no message handler beside it. `/ward` arrives as
        # a slash command of ward's own app — declared there, no token here.
        SlackInteractions(self.app, dispatcher, self.chat, agent=COMMAND).register()
        return _Socket(AsyncSocketModeHandler(self.app, self._app_token))


_CHAT_SIDES: dict[str, type[_Mattermost] | type[_Slack]] = {
    "mattermost": _Mattermost,
    "slack": _Slack,
}


@dataclass
class Ward:
    settings: WardSettings
    store: WardStore
    broker: SecretBroker
    door: WardServer
    listener: Listener
    chat: ChatSide


def build(settings: WardSettings) -> Ward:
    if not settings.ca_cert.is_file():
        raise SystemExit(
            f"no certificate authority at {settings.tls} — run `ward init` first"
        )
    side_cls = _CHAT_SIDES.get(settings.gateway)
    if side_cls is None:
        raise SystemExit(
            f"WARD_GATEWAY={settings.gateway!r} is not a chat platform ward knows; "
            f"one of: {', '.join(_CHAT_SIDES)}"
        )
    Path(settings.data_dir).mkdir(parents=True, exist_ok=True)
    store = WardStore(settings.db_path)
    approvals = PendingApprovals()

    side = side_cls(settings)
    chat = side.chat
    presence = OneBot(chat)

    # Who may answer, and — the same trust — who may administer from chat.
    approvers = Approvers(settings.approvers, chat)

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
        approvers,
        approval_channel=settings.approval_channel,
        approval_timeout_s=settings.approval_timeout_s,
        max_grant_s=settings.max_grant_s,
        notice_fold_s=settings.notice_fold_s,
        callback_url=side.callback_url,
    )

    operations = Operations(broker.backend, store, store)
    ca = CertificateAuthority.load(settings.ca_cert, settings.ca_key)
    door = WardServer(
        broker,
        ca,
        operations,
        host=settings.listen_host,
        port=settings.listen_port,
        ssl_context=mutual_tls(
            certificate=settings.server_cert, key=settings.server_key, ca=settings.ca_cert
        ),
    )
    # The operator surface in chat. Registered whatever the settings say — what
    # gates it is the platform's own entry (a command token on Mattermost, the
    # app's slash command on Slack) and the approver list (nobody named, nobody
    # allowed), both checked on every call.
    pending_forms = PendingOperatorForms()
    handlers = FormHandlers()
    handlers.register(
        WARD_HANDLER,
        OperatorForms(broker, operations, approvers, chat, chat, store, pending_forms),
    )
    screens = ScreenRegistry()
    screens.register(
        WardScreen(broker, operations, approvers, chat, store, store, pending_forms)
    )
    dispatcher = InteractionDispatcher(
        store, presence, PendingUiRequests(), store,
        screens=screens,
        approvals=approvals,
        handlers=handlers,
        callback_url=side.callback_url,
    )  # type: ignore[arg-type]
    listener = side.listener(dispatcher, presence)
    logger.info(
        "ward built: store=%s, vault=%s, chat=%s, approvers=%s",
        settings.db_path, settings.vault_addr, settings.gateway,
        settings.approvers or "(nobody)",
    )
    return Ward(settings, store, broker, door, listener, side)


async def _sign_in(ward: Ward) -> None:
    """Sign the chat account in before anything is posted as it.

    Not fatal: without a session every request that needs a human is refused
    with no_approver, but the broker can still serve what needs none, and can
    still be driven by an operator — including to find out why.
    """
    try:
        username = await ward.chat.sign_in()
    except Exception as exc:
        logger.error(
            "cannot sign in to chat (%s) — every request that needs a human will "
            "be refused with no_approver. Check %s.",
            exc, ward.chat.variables,
        )
        return
    logger.info("posting approval cards as @%s", username)


# How long the broker waits for the store before opening it with material kept
# on disk, and how often it looks. Longer than the ceremony's wait: this runs
# unattended after a reboot, where the store's container can be well behind the
# broker's, and nobody is sitting at a prompt for it to be quick.
_STORE_WAIT_S = 120.0
_STORE_POLL_S = 1.0


async def _unlock(ward: Ward) -> None:
    """Open the store at startup, if the deployment keeps the material on disk.

    Nothing here is fatal: without the files ward starts locked, every request
    is refused, and the log says which state it is in.

    With the files, the store is waited for first. Compose brings the broker up
    only once the store is healthy, but a daemon restoring containers after a
    reboot does not — and one attempt made before the store answers would leave
    a deployment that chose to run unattended locked all the same.
    """
    material = UnlockMaterial(
        unseal_key=_read(ward.settings.unseal_key_file),
        auth_secret=_read(ward.settings.secret_id_file),
    )
    if not material:
        logger.info("secrets: locked — waiting to be unlocked")
        return
    if not await wait_for_store(
        ward.broker.backend,
        timeout_s=_STORE_WAIT_S,
        poll_s=_STORE_POLL_S,
        on_wait=lambda: logger.info("secrets: waiting for the store to come up"),
    ):
        logger.warning(
            "secrets: the store did not come up in %.0fs — trying anyway", _STORE_WAIT_S
        )
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
    await ward.listener.start()
    await ward.door.start()
    await _sign_in(ward)
    await _unlock(ward)
    try:
        # Nothing to drive: the door is a server and the listener runs on the
        # loop by itself. Sleep until told to stop.
        await asyncio.Event().wait()
    finally:
        await ward.door.stop()
        await ward.listener.stop()
        await ward.store.close()
