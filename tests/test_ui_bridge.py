"""WidgetUiBridge (concrete) + PendingUiRequests: the blocking round-trip."""

import asyncio
from pathlib import Path

from crucible.interactions.pending_ui import PendingUiRequests
from crucible.interactions.ui_bridge import WidgetUiBridge
from crucible.ports.agent.ui import UiRequest
from crucible.ports.chat.types import KIND_DM, Action, ConversationRef
from crucible.store.sessions import SqliteSessionStore
from tests.fakes.presence import presence_of


async def _wait_until(predicate, timeout: float = 1.0) -> None:
    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(_poll(), timeout=timeout)


class FakePoster:
    def __init__(self) -> None:
        self.posted: list[tuple] = []
        self.retracted: list[tuple[str, str]] = []

    async def post_actions(
        self, ref: ConversationRef, text, actions: list[Action], *, callback_url
    ) -> str:
        self.posted.append((ref, text, actions, callback_url))
        return "post-id"

    async def retract(self, post_id: str, text: str) -> None:
        self.retracted.append((post_id, text))

    async def open_dialog(self, trigger_id, form, *, submit_url, state) -> None:
        pass


def _bridge(store, poster, pending, *, timeout: float = 5.0) -> WidgetUiBridge:
    return WidgetUiBridge(
        presence_of(poster), store, pending, callback_url="http://x/interact", timeout=timeout
    )


async def test_confirm_posts_two_buttons_and_resolves_on_click(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    poster, pending = FakePoster(), PendingUiRequests()
    try:
        rec, _ = await store.get_or_create("assistant", "dm1", "dm1", KIND_DM)
        bridge = _bridge(store, poster, pending)
        req = UiRequest(request_id="r1", method="confirm", title="Send email?", message="to Bob")
        task = asyncio.ensure_future(bridge.request(rec.runtime_session_id, req))

        await _wait_until(lambda: len(poster.posted) >= 1)
        _, text, actions, _ = poster.posted[0]
        assert text == "Send email?\n\nto Bob"
        assert [a.label for a in actions] == ["Allow", "Block"]  # a confirm -> two buttons
        token = actions[0].context["token"]

        assert pending.resolve(token, "Allow") is True  # the click
        outcome = await task
        assert outcome.confirmed is True and outcome.cancelled is False
    finally:
        await store.close()


async def test_confirm_block_click_declines(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    poster, pending = FakePoster(), PendingUiRequests()
    try:
        rec, _ = await store.get_or_create("assistant", "dm1", "dm1", KIND_DM)
        task = asyncio.ensure_future(
            _bridge(store, poster, pending).request(
                rec.runtime_session_id, UiRequest(request_id="r", method="confirm", title="?")
            )
        )
        await _wait_until(lambda: len(poster.posted) >= 1)
        pending.resolve(poster.posted[0][2][0].context["token"], "Block")
        outcome = await task
        assert outcome.confirmed is False
    finally:
        await store.close()


async def test_select_posts_dropdown_and_resolves_with_value(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    poster, pending = FakePoster(), PendingUiRequests()
    try:
        rec, _ = await store.get_or_create("assistant", "dm1", "dm1", KIND_DM)
        req = UiRequest(request_id="r2", method="select", title="Pick one", options=("A", "B", "C"))
        task = asyncio.ensure_future(_bridge(store, poster, pending).request(rec.runtime_session_id, req))

        await _wait_until(lambda: len(poster.posted) >= 1)
        action = poster.posted[0][2][0]
        assert action.kind == "select"
        assert [c.label for c in action.options] == ["A", "B", "C"]

        pending.resolve(action.context["token"], "B")  # selected_option
        outcome = await task
        assert outcome.value == "B" and outcome.confirmed is None
    finally:
        await store.close()


async def test_times_out_to_cancelled(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    poster, pending = FakePoster(), PendingUiRequests()
    try:
        rec, _ = await store.get_or_create("assistant", "dm1", "dm1", KIND_DM)
        bridge = _bridge(store, poster, pending, timeout=0.05)
        outcome = await bridge.request(
            rec.runtime_session_id, UiRequest(request_id="r3", method="confirm", title="?")
        )
        assert outcome.cancelled is True
        # the stale buttons were retracted so a late click can't error
        assert len(poster.retracted) == 1 and poster.retracted[0][0] == "post-id"
    finally:
        await store.close()


async def test_unknown_session_declines_without_posting(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    poster, pending = FakePoster(), PendingUiRequests()
    try:
        outcome = await _bridge(store, poster, pending).request(
            "no-such-session", UiRequest(request_id="r4", method="confirm", title="?")
        )
        assert outcome.cancelled is True
        assert poster.posted == []
    finally:
        await store.close()


async def test_cancel_for_conversation_unblocks_outstanding(tmp_path: Path) -> None:
    # The user typed instead of clicking: the outstanding confirm is cancelled.
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    poster, pending = FakePoster(), PendingUiRequests()
    try:
        rec, _ = await store.get_or_create("assistant", "dm1", "dm1", KIND_DM)
        task = asyncio.ensure_future(
            _bridge(store, poster, pending).request(
                rec.runtime_session_id, UiRequest(request_id="r", method="confirm", title="?")
            )
        )
        await _wait_until(lambda: len(poster.posted) >= 1)

        assert pending.cancel_for_conversation("assistant", "dm1") == 1
        outcome = await task
        assert outcome.cancelled is True
        assert len(poster.retracted) == 1  # buttons retracted on cancel-by-typing
    finally:
        await store.close()


async def test_post_failure_declines_and_names_the_cause(tmp_path: Path, caplog) -> None:
    # The platform error summary must land in the log MESSAGE itself (grep-able
    # without the traceback: missing_scope vs channel_not_found etc.).
    class BoomPoster(FakePoster):
        async def post_actions(self, ref, text, actions, *, callback_url) -> str:
            raise RuntimeError("slack says: missing_scope")

    store = SqliteSessionStore(tmp_path / "db.sqlite")
    poster, pending = BoomPoster(), PendingUiRequests()
    try:
        rec, _ = await store.get_or_create("assistant", "dm1", "dm1", KIND_DM)
        with caplog.at_level("ERROR"):
            outcome = await _bridge(store, poster, pending).request(
                rec.runtime_session_id, UiRequest(request_id="r", method="confirm", title="?")
            )
        assert outcome.cancelled is True
        assert any("missing_scope" in r.message for r in caplog.records)
    finally:
        await store.close()
