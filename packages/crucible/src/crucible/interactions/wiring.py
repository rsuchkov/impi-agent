"""InteractionWiring: an application's interactivity plumbing, assembled from an
``IntegrationsSettings`` config.

Owns the shared ``posters``/``sinks`` the agent loop fills, builds the blocking UI
bridge and the transport-neutral dispatcher up front, and (post-loop, via
``finalize``) the ``InteractionService`` and the HTTP callback receiver. The
receiver's ``CallbackCodec`` is injected — the interactions layer never imports a
gateway.

A convenience for composition roots; an app may wire these pieces by hand instead.
"""

from collections.abc import Callable

from crucible.config import IntegrationsSettings
from crucible.interactions.callbacks import CallbackCodec
from crucible.interactions.dispatcher import AgentSink, InteractionDispatcher
from crucible.interactions.pending_ui import PendingUiRequests
from crucible.interactions.server import InteractionsServer
from crucible.interactions.service import InteractionService
from crucible.interactions.ui_bridge import WidgetUiBridge
from crucible.ports.chat.client import ChatClient
from crucible.ports.chat.types import IncomingMessage
from crucible.store.sessions import SqliteSessionStore


class InteractionWiring:
    """``posters``/``sinks`` are assigned before the UI bridge and dispatcher
    capture them, so the agent loop fills the exact objects those collaborators read
    lazily at request time. ``ui_bridge`` feeds the runtime; ``dispatcher`` feeds the
    gateway factory. ``finalize`` builds the post-loop interaction service and the
    HTTP receiver once ``posters`` is populated.
    """

    def __init__(
        self,
        integrations: IntegrationsSettings,
        sessions: SqliteSessionStore,
        *,
        codec: CallbackCodec,
    ) -> None:
        # The concrete store, not the SessionStore port: the dispatcher/widget/form
        # collaborators need its InteractionStore + FormStore facets too.
        self._integrations = integrations
        self._sessions = sessions
        self._codec = codec
        self.enabled = integrations.enabled
        self.posters: dict[str, ChatClient] = {}
        self.sinks: dict[str, AgentSink] = {}
        # Blocking UI bridge: a runtime mid-turn confirm/select becomes a widget the
        # turn waits on. None when interactions are off — UI requests then fall back
        # to the session's auto-reject backstop.
        self.pending_ui = PendingUiRequests() if integrations.enabled else None
        self.ui_bridge = (
            WidgetUiBridge(
                self.posters, sessions, self.pending_ui,
                callback_url=integrations.interact_url, timeout=integrations.ui_timeout,
            )
            if self.pending_ui is not None
            else None
        )
        # Transport-neutral dispatch: resolves a blocking mid-turn request or feeds a
        # click back as a synthetic message. Shared by the HTTP receiver and every
        # socket-driven gateway; reads `sinks` lazily at dispatch time.
        self.dispatcher = (
            InteractionDispatcher(sessions, self.sinks, self.pending_ui, sessions)
            if self.pending_ui is not None
            else None
        )
        self.interaction_svc: InteractionService | None = None
        self.receiver: InteractionsServer | None = None

    def register(self, name: str, *, chat: ChatClient, sink: AgentSink) -> None:
        self.posters[name] = chat
        self.sinks[name] = sink

    def on_arrival_for(self, name: str) -> Callable[[IncomingMessage], object] | None:
        # A real message cancels any blocking UI request outstanding in that
        # conversation (the user typed instead of clicking).
        pending = self.pending_ui
        if pending is None:
            return None
        return lambda m: pending.cancel_for_conversation(name, m.conversation_id)

    def finalize(self, *, needs_receiver: bool) -> None:
        if not self.enabled or self.pending_ui is None:
            return
        ints = self._integrations
        # One service for widgets (ask) and forms (open_form). A form's "fill in"
        # button clicks to /interact like a widget; the modal submission goes to
        # /dialog. The one concrete store backs all three store facets.
        self.interaction_svc = InteractionService(
            self.posters, self._sessions, self._sessions, self._sessions,
            callback_url=ints.interact_url,
        )
        # The HTTP receiver is only for gateways that deliver callbacks over HTTP
        # (Mattermost); socket gateways (Slack) drive the same dispatcher over their
        # socket, so a Slack-only deployment builds no receiver (and binds no port).
        if needs_receiver:
            assert self.dispatcher is not None  # pending_ui is not None here
            self.receiver = InteractionsServer(
                self.dispatcher, self._codec, self.posters,
                host=ints.host, port=ints.port, dialog_submit_url=ints.dialog_url,
            )
