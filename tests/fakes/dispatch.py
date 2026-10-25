"""Doubles for the neutral side of a gateway: the sink a gateway hands messages
to, and the dispatcher it forwards clicks, submissions and commands to. Shared
by the Slack, Mattermost, http and ws tests — none of this is a platform's."""

from crucible.approvals import ApprovalOutcome
from crucible.interactions.screens import Redrawn, ScreenOpened


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
        redrawn=Redrawn(),
    ) -> None:
        self.calls: list = []
        self.quoted: list = []  # what each invoked command pointed at
        self.gateways: list[str] = []  # which gateway each command came through
        self.picks: list[str] = []  # the pick kind of each consumed action
        self._form = form
        self._pending_ok = pending_ok
        self._approval = approval
        self._opened = opened
        self._command_result = command_result
        self._redrawn = redrawn

    async def resolve_approval(self, token, value, user_id, *, gateway=""):
        self.calls.append(("resolve_approval", token, value, user_id))
        self.gateways.append(gateway)
        return self._approval

    async def may_answer(self, token, user_id, *, gateway=""):
        return True

    async def resolve_pending(self, token, value) -> bool:
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

    async def redraw_screen(self, state_raw, value, *, post_id, user_id) -> Redrawn | None:
        self.calls.append(("redraw_screen", state_raw, value, post_id, user_id))
        return self._redrawn

    async def submit_form(self, state, submission, cancelled, user_id):
        self.calls.append(("submit_form", state, submission, cancelled, user_id))

    def invoke_command(
        self, agent, *, channel_id, conversation_id, kind, text, user_id, username="", quoted=None,
        gateway: str = "",
    ):
        self.calls.append(
            ("invoke_command", agent, channel_id, conversation_id, kind, text, user_id, username)
        )
        self.quoted.append(quoted)
        self.gateways.append(gateway)
        return self._command_result
