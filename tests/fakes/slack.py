"""Doubles for the Slack side, shared by the client, gateway, interaction and
broker tests: a Web API that records calls, a dispatcher that records what it
was asked, a poster that records the modals it opened."""

from slack_sdk.errors import SlackApiError

from crucible.approvals import ApprovalOutcome
from crucible.interactions.screens import ScreenOpened


class FakeWeb:
    """Records calls; returns canned responses. Duck-types AsyncWebClient."""

    def __init__(self, **canned) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._canned = canned
        self.raise_on: dict[str, SlackApiError] = {}
        self.token = "xoxb-fake"

    def __getattr__(self, method_name: str):
        async def method(**kwargs):
            self.calls.append((method_name, kwargs))
            if method_name in self.raise_on:
                raise self.raise_on[method_name]
            return self._canned.get(method_name, {})

        return method

    def last(self, name: str) -> dict:
        return next(kw for n, kw in reversed(self.calls) if n == name)

    def all(self, name: str) -> list[dict]:
        return [kw for n, kw in self.calls if n == name]


class FakeSink:
    def __init__(self) -> None:
        self.submitted: list = []

    def submit(self, msg, chat) -> None:
        self.submitted.append(msg)


class FakeDispatcher:
    """Answers like the neutral dispatcher would, and writes down every question."""

    def __init__(
        self,
        *,
        form=None,
        pending_ok=False,
        approval=ApprovalOutcome.RESOLVED,
        opened=ScreenOpened(owned=False),
        command_result=None,
    ) -> None:
        self.calls: list = []
        self.picks: list[str] = []  # the pick kind of each consumed action
        self._form = form
        self._pending_ok = pending_ok
        self._approval = approval
        self._opened = opened
        self._command_result = command_result

    def resolve_approval(self, token, value, user_id):
        self.calls.append(("resolve_approval", token, value, user_id))
        return self._approval

    def resolve_pending(self, token, value) -> bool:
        self.calls.append(("resolve_pending", token, value))
        return self._pending_ok

    async def consume_action(self, token, value, user_id, *, pick=""):
        self.calls.append(("consume_action", token, value, user_id))
        self.picks.append(pick)

    async def load_form(self, form_token):
        self.calls.append(("load_form", form_token))
        return self._form

    async def open_screen(self, agent, command, *, channel_id, conversation_id, kind, user_id):
        self.calls.append(("open_screen", agent, command, channel_id, conversation_id, kind, user_id))
        return self._opened

    async def redraw_screen(self, state_raw, value, *, post_id, user_id) -> bool:
        self.calls.append(("redraw_screen", state_raw, value, post_id, user_id))
        return True

    async def submit_form(self, state, submission, cancelled, user_id):
        self.calls.append(("submit_form", state, submission, cancelled, user_id))

    def invoke_command(self, agent, *, channel_id, conversation_id, kind, text, user_id, username=""):
        self.calls.append(
            ("invoke_command", agent, channel_id, conversation_id, kind, text, user_id, username)
        )
        return self._command_result


class FakePoster:
    def __init__(self) -> None:
        self.opened: list = []

    async def open_dialog(self, trigger_id, form, *, submit_url, state) -> None:
        self.opened.append((trigger_id, form, state))


class Respond:
    """The `respond` a bolt slash-command handler is handed: records the text."""

    def __init__(self) -> None:
        self.said: list[str] = []

    async def __call__(self, text: str) -> None:
        self.said.append(text)


class FakeApp:
    """Enough of bolt's AsyncApp for SlackInteractions: a client to call, and
    registration decorators that keep the handlers rather than a socket."""

    def __init__(self, client: FakeWeb) -> None:
        self.client = client
        self.handlers: dict[str, object] = {}

    def _keep(self, kind: str):
        def decorator(func):
            self.handlers[kind] = func
            return func

        return decorator

    def action(self, _matcher):
        return self._keep("action")

    def view(self, _callback_id):
        return self._keep("view")

    def shortcut(self, _matcher):
        return self._keep("shortcut")

    def command(self, _matcher):
        return self._keep("command")
