"""InteractionWiring: an application's interactivity plumbing, built from an
``IntegrationsSettings`` config and an ``AgentPresence``.

Builds the blocking UI bridge, the transport-neutral dispatcher, the interaction
service, and (when ``needs_receiver``) the HTTP callback receiver — all up front,
all reading the presence lazily. The receiver's ``CallbackCodec`` is injected; the
interactions layer never imports a gateway. Holds no per-agent state — the app owns
the presence registry, so there is no ``register`` and no ``finalize``.

A convenience for composition roots; an app may wire these pieces by hand instead.
"""

from collections.abc import Callable

from crucible.config import IntegrationsSettings
from crucible.interactions.callbacks import CallbackCodec
from crucible.interactions.dispatcher import InteractionDispatcher
from crucible.interactions.pending_ui import PendingUiRequests
from crucible.interactions.presence import AgentPresence
from crucible.interactions.screens import ScreenRegistry
from crucible.interactions.server import CommandTokens, InteractionsServer
from crucible.interactions.service import InteractionService
from crucible.interactions.ui_bridge import WidgetUiBridge
from crucible.ports.chat.types import IncomingMessage
from crucible.store.sessions import SqliteSessionStore


class InteractionWiring:
    """``needs_receiver`` (whether any agent runs on an HTTP-callback gateway) is
    resolved by the app up front, so everything can be built in the constructor.
    """

    def __init__(
        self,
        integrations: IntegrationsSettings,
        sessions: SqliteSessionStore,
        presence: AgentPresence,
        *,
        codec: CallbackCodec,
        needs_receiver: bool,
        command_tokens: CommandTokens | None = None,
        screens: ScreenRegistry | None = None,
    ) -> None:
        # The concrete store, not the SessionStore port: the dispatcher/interaction
        # service need its InteractionStore + FormStore facets too.
        self.enabled = integrations.enabled
        self.pending_ui: PendingUiRequests | None = None
        self.ui_bridge: WidgetUiBridge | None = None
        self.dispatcher: InteractionDispatcher | None = None
        self.interaction_svc: InteractionService | None = None
        self.receiver: InteractionsServer | None = None
        if not integrations.enabled:
            # Interactions off: a runtime mid-turn UI request falls back to the
            # session's auto-reject backstop; no widgets/forms, no receiver.
            return

        self.pending_ui = PendingUiRequests()
        # Blocking UI bridge: a runtime mid-turn confirm/select becomes a widget the
        # turn waits on. Feeds the runtime.
        self.ui_bridge = WidgetUiBridge(
            presence, sessions, self.pending_ui,
            callback_url=integrations.interact_url, timeout=integrations.ui_timeout,
        )
        # Transport-neutral dispatch: resolves a blocking mid-turn request or feeds a
        # click back as a synthetic message. Shared by the HTTP receiver and every
        # socket-driven gateway. Feeds the gateway factory.
        self.dispatcher = InteractionDispatcher(
            sessions, presence, self.pending_ui, sessions,
            # Commands the engine answers itself, and the clicks that redraw them.
            screens=screens, callback_url=integrations.interact_url,
        )
        # One service for widgets (ask) and forms (open_form). The one concrete store
        # backs all three store facets.
        self.interaction_svc = InteractionService(
            presence, sessions, sessions, sessions, callback_url=integrations.interact_url,
        )
        # The HTTP receiver is only for gateways that deliver callbacks over HTTP
        # (Mattermost); socket gateways (Slack) drive the same dispatcher over their
        # socket, so a Slack-only deployment builds no receiver (and binds no port).
        if needs_receiver:
            self.receiver = InteractionsServer(
                self.dispatcher, codec, presence,
                host=integrations.host, port=integrations.port,
                dialog_submit_url=integrations.dialog_url,
                command_tokens=command_tokens,
            )

    def on_arrival_for(self, name: str) -> Callable[[IncomingMessage], object] | None:
        # A real message cancels any blocking UI request outstanding in that
        # conversation (the user typed instead of clicking).
        pending = self.pending_ui
        if pending is None:
            return None
        return lambda m: pending.cancel_for_conversation(name, m.conversation_id)
