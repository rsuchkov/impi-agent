"""Gateway dispatch decisions: channel residency + base rules."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from mattermostautodriver import AsyncTypedDriver

from crucible.attachments import AttachmentStore
from crucible.gateways.mattermost.gateway import MattermostGateway
from crucible.ports.chat.types import (
    KIND_CHANNEL,
    KIND_DM,
    KIND_THREAD,
    ConversationRef,
    IncomingMessage,
)
from tests.fakes.fake_chat import FakeChat

ME = "uid-assistant"
OTHER_AGENT = "uid-developer"
HUMAN = "uid-roman"


class FakeDirectory:
    def __init__(self, ids: set[str]) -> None:
        self._ids = frozenset(ids)

    def agent_user_ids(self) -> frozenset[str]:
        return self._ids

    def list_agents(self):
        return []


class FakeDriver:
    """Only the surface the gateway touches: channel members and file endpoints."""

    def __init__(self, members: dict[str, list[str]]) -> None:
        driver = self

        class Channels:
            async def get_channel_members(self, channel_id, per_page=200):
                return [{"user_id": uid} for uid in driver._members.get(channel_id, [])]

        self._members = members
        self.channels = Channels()
        self.files: Any = None  # tests that exercise attachments set a FakeFiles


class SinkSpy:
    def __init__(self) -> None:
        self.submitted: list[IncomingMessage] = []

    def submit(self, msg, chat) -> None:
        self.submitted.append(msg)


def _gateway(
    members: dict[str, list[str]],
    agent_ids: set[str],
    *,
    loop_guard=None,
    reply_to_agents: bool = True,
) -> MattermostGateway:
    gw = MattermostGateway(
        cast(AsyncTypedDriver, FakeDriver(members)),
        SinkSpy(),
        FakeChat(),
        directory=FakeDirectory(agent_ids),
        loop_guard=loop_guard,
        reply_to_agents=reply_to_agents,
    )
    gw._own_user_id = ME
    return gw


def _agent_msg(
    *, author: str, mentioned: bool, hop_depth: int = 0
) -> IncomingMessage:
    return IncomingMessage(
        ref=ConversationRef(
            channel_id="ch1", conversation_id="root1", message_id="p9", thread_root_id="root1"
        ),
        text="hi",
        user_id=author,
        kind=KIND_THREAD,
        mentioned=mentioned,
        is_from_bot=True,
        hop_depth=hop_depth,
    )


def _channel_msg(*, mentioned: bool = False, top_level: bool = True) -> IncomingMessage:
    ref = ConversationRef(
        channel_id="ch1",
        conversation_id="p1" if top_level else "root1",
        message_id="p1" if top_level else "p2",
        thread_root_id="p1" if top_level else "root1",
    )
    return IncomingMessage(
        ref=ref, text="hi", user_id=HUMAN, kind=KIND_THREAD, mentioned=mentioned
    )


async def test_sole_resident_answers_top_level_as_channel_session() -> None:
    gw = _gateway({"ch1": [HUMAN, ME]}, {ME, OTHER_AGENT})

    decided = await gw._decide(_channel_msg())

    assert decided is not None
    assert decided.kind == KIND_CHANNEL
    assert decided.conversation_id == "ch1"  # the channel is the session
    assert decided.ref.thread_root_id == ""  # replies go top-level


async def test_sole_resident_answers_thread_replies_without_mention() -> None:
    gw = _gateway({"ch1": [HUMAN, ME]}, {ME, OTHER_AGENT})

    decided = await gw._decide(_channel_msg(top_level=False))

    assert decided is not None
    assert decided.kind == KIND_THREAD  # thread still wins inside threads
    assert decided.conversation_id == "root1"


async def test_multi_agent_channel_requires_mention() -> None:
    gw = _gateway({"ch1": [HUMAN, ME, OTHER_AGENT]}, {ME, OTHER_AGENT})

    assert await gw._decide(_channel_msg()) is None
    decided = await gw._decide(_channel_msg(mentioned=True))
    assert decided is not None
    assert decided.kind == KIND_THREAD  # mention flow: reply in a thread


async def test_dm_always_answered_even_with_directory() -> None:
    gw = _gateway({}, {ME})
    msg = IncomingMessage(
        ref=ConversationRef(
            channel_id="dm1", conversation_id="dm1", message_id="p1", thread_root_id=""
        ),
        text="hi",
        user_id=HUMAN,
        kind=KIND_DM,
        is_dm=True,
    )

    decided = await gw._decide(msg)
    assert decided is msg


async def test_agent_post_with_mention_is_answered() -> None:
    from crucible.loopguard import LoopGuard

    gw = _gateway({"ch1": [HUMAN, ME, OTHER_AGENT]}, {ME, OTHER_AGENT}, loop_guard=LoopGuard())

    decided = await gw._decide(_agent_msg(author=OTHER_AGENT, mentioned=True))
    assert decided is not None
    assert decided.is_from_bot


async def test_agent_post_without_mention_is_ignored() -> None:
    gw = _gateway({"ch1": [HUMAN, ME, OTHER_AGENT]}, {ME, OTHER_AGENT})
    assert await gw._decide(_agent_msg(author=OTHER_AGENT, mentioned=False)) is None


async def test_foreign_bot_is_never_answered() -> None:
    # A bot that is not in our registry, even mentioning us.
    gw = _gateway({"ch1": [HUMAN, ME, "uid-foreign"]}, {ME, OTHER_AGENT})
    assert await gw._decide(_agent_msg(author="uid-foreign", mentioned=True)) is None


async def test_agent_replies_disabled_drops_agent_posts() -> None:
    gw = _gateway({"ch1": [HUMAN, ME, OTHER_AGENT]}, {ME, OTHER_AGENT}, reply_to_agents=False)
    assert await gw._decide(_agent_msg(author=OTHER_AGENT, mentioned=True)) is None


async def test_hop_cap_stops_agent_cascade() -> None:
    from crucible.loopguard import LoopGuard

    gw = _gateway(
        {"ch1": [HUMAN, ME, OTHER_AGENT]}, {ME, OTHER_AGENT}, loop_guard=LoopGuard(max_hops=4)
    )
    assert await gw._decide(_agent_msg(author=OTHER_AGENT, mentioned=True, hop_depth=3)) is not None
    assert await gw._decide(_agent_msg(author=OTHER_AGENT, mentioned=True, hop_depth=4)) is None


async def test_members_lookup_failure_falls_back_to_mention_rule() -> None:
    class ExplodingDriver(FakeDriver):
        def __init__(self):
            super().__init__({})

            class Channels:
                async def get_channel_members(self, channel_id, per_page=200):
                    raise RuntimeError("api down")

            self.channels = Channels()

    gw = MattermostGateway(
        cast(AsyncTypedDriver, ExplodingDriver()),
        SinkSpy(),
        FakeChat(),
        directory=FakeDirectory({ME}),
    )
    gw._own_user_id = ME

    assert await gw._decide(_channel_msg()) is None  # not sole -> mention required
    assert await gw._decide(_channel_msg(mentioned=True)) is not None


# -- incoming attachments ----------------------------------------------------


class FakeFiles:
    """The driver's file endpoints: bytes by id, plus metadata for bare ids."""

    def __init__(self, blobs: dict[str, bytes], infos: dict[str, dict] | None = None) -> None:
        self.blobs = blobs
        self.infos = infos or {}
        self.fetched: list[str] = []

    async def get_file(self, file_id: str):
        self.fetched.append(file_id)
        if file_id not in self.blobs:
            raise RuntimeError("no such file")
        return SimpleNamespace(content=self.blobs[file_id])

    async def get_file_info(self, file_id: str) -> dict:
        return self.infos[file_id]


def _posted_frame(files: list[dict] | None = None, *, message: str = "look") -> str:
    post: dict = {
        "id": "p1",
        "create_at": 1783371725003,
        "user_id": HUMAN,
        "channel_id": "dm1",
        "root_id": "",
        "message": message,
        "type": "",
        "props": {},
    }
    if files is not None:
        post["file_ids"] = [f["id"] for f in files]
        post["metadata"] = {"files": files}
    return json.dumps(
        {
            "event": "posted",
            "data": {"channel_type": "D", "post": json.dumps(post), "sender_name": "@roman"},
            "broadcast": {},
            "seq": 2,
        }
    )


def _files_gateway(driver_files: FakeFiles, store: AttachmentStore) -> tuple[MattermostGateway, SinkSpy]:
    driver = FakeDriver({})
    driver.files = driver_files
    sink = SinkSpy()
    gw = MattermostGateway(
        cast(AsyncTypedDriver, driver),
        sink,
        FakeChat(),
        agent="assistant",
        directory=FakeDirectory({ME}),
        attachments=store,
    )
    gw._own_user_id = ME
    return gw, sink


async def test_attached_files_are_downloaded_and_travel_with_the_message(
    tmp_path: Path,
) -> None:
    store = AttachmentStore(tmp_path, max_bytes=1024, retention_days=14)
    files = FakeFiles({"f1": b"PNGDATA"})
    gw, sink = _files_gateway(files, store)

    await gw._on_ws_message(
        _posted_frame([{"id": "f1", "name": "screen.png", "mime_type": "image/png"}])
    )

    (msg,) = sink.submitted
    (attachment,) = msg.attachments
    assert attachment.name == "screen.png"
    assert attachment.mime == "image/png"
    assert Path(attachment.path).read_bytes() == b"PNGDATA"


async def test_bare_file_ids_are_described_by_a_metadata_lookup(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path, max_bytes=1024, retention_days=14)
    files = FakeFiles(
        {"f1": b"PDF"}, infos={"f1": {"name": "отчёт.pdf", "mime_type": "application/pdf"}}
    )  # Non-ASCII on purpose
    driver = FakeDriver({})
    driver.files = files
    sink = SinkSpy()
    gw = MattermostGateway(
        cast(AsyncTypedDriver, driver), sink, FakeChat(),
        agent="assistant", directory=FakeDirectory({ME}), attachments=store,
    )
    gw._own_user_id = ME
    frame = json.loads(_posted_frame([{"id": "f1", "name": "x", "mime_type": "x"}]))
    post = json.loads(frame["data"]["post"])
    post.pop("metadata")  # older servers send ids only
    frame["data"]["post"] = json.dumps(post)

    await gw._on_ws_message(json.dumps(frame))

    (msg,) = sink.submitted
    (attachment,) = msg.attachments
    assert (attachment.name, attachment.mime) == ("отчёт.pdf", "application/pdf")


async def test_a_failed_download_never_costs_the_user_their_message(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path, max_bytes=1024, retention_days=14)
    gw, sink = _files_gateway(FakeFiles({}), store)

    await gw._on_ws_message(
        _posted_frame([{"id": "gone", "name": "a.png", "mime_type": "image/png"}])
    )

    (msg,) = sink.submitted
    assert msg.attachments == ()
    assert msg.text == "look"


async def test_without_a_store_files_are_ignored() -> None:
    driver = FakeDriver({})
    driver.files = FakeFiles({"f1": b"x"})
    sink = SinkSpy()
    gw = MattermostGateway(
        cast(AsyncTypedDriver, driver), sink, FakeChat(), directory=FakeDirectory({ME})
    )
    gw._own_user_id = ME

    await gw._on_ws_message(
        _posted_frame([{"id": "f1", "name": "a.png", "mime_type": "image/png"}])
    )

    (msg,) = sink.submitted
    assert msg.attachments == ()
    assert driver.files.fetched == []
