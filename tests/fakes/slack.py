"""Doubles for the Slack side, shared by the client, gateway, interaction and
broker tests: a Web API that records calls, and a poster that records the
modals it opened. The sink and the dispatcher are neutral: tests/fakes/dispatch.py."""

from slack_sdk.errors import SlackApiError


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
