"""MattermostGateway: owns the WS loop, normalizes, decides, dispatches.

One gateway = one bot account; the engine runs N gateways in one process. The
driver is injected by the composition root; the gateway never builds it.

Channel residency rule: in a channel whose ONLY resident agent is this one, the
agent behaves like in a DM — answers every
post; top-level posts map to the channel session. With 2+ resident agents,
an explicit mention is required and replies go to threads.
"""

import json
import logging
import time
from dataclasses import replace

from mattermostautodriver import AsyncTypedDriver

from crucible.attachments import AttachmentStore, IncomingFile
from crucible.gateways.mattermost.events import (
    FileHandle,
    is_top_level,
    parse_files,
    parse_posted,
    should_respond,
    to_channel_session,
)
from crucible.loopguard import LoopGuard
from crucible.ports.chat.client import ChatClient
from crucible.ports.chat.directory import AgentDirectory
from crucible.ports.chat.flow import MessageSink
from crucible.ports.chat.gateway import AgentIdentity
from crucible.ports.chat.types import IncomingMessage

logger = logging.getLogger(__name__)

_MEMBERS_TTL_S = 60.0


class MattermostGateway:
    def __init__(
        self,
        driver: AsyncTypedDriver,
        sink: MessageSink,
        chat: ChatClient,
        *,
        agent: str = "",
        directory: AgentDirectory | None = None,
        loop_guard: LoopGuard | None = None,
        reply_to_agents: bool = True,
        attachments: AttachmentStore | None = None,
    ) -> None:
        self._driver = driver
        self._sink = sink  # coalescer, wired in the composition root
        self._chat = chat
        self._agent = agent  # whose attachment directory incoming files land in
        self._directory = directory
        self._loop_guard = loop_guard
        self._reply_to_agents = reply_to_agents
        self._attachments = attachments
        self._own_user_id = ""
        # channel_id -> (expires_monotonic, member user ids)
        self._members_cache: dict[str, tuple[float, frozenset[str]]] = {}

    async def login(self) -> AgentIdentity:
        """Authenticate and learn our own identity. Call before run()."""
        me = await self._driver.login()
        self._own_user_id = me["id"]
        username = me.get("username", "")
        logger.info("Mattermost gateway up: @%s (%s)", username, self._own_user_id)
        return AgentIdentity(user_id=self._own_user_id, username=username)

    async def run(self) -> None:
        """Run the WS loop until disconnect() is called."""
        await self._driver.init_websocket(self._on_ws_message)

    async def start(self) -> None:
        await self.login()
        await self.run()

    async def stop(self) -> None:
        websocket = getattr(self._driver, "websocket", None)
        if websocket is not None:
            websocket.disconnect()

    # -- internals ----------------------------------------------------------

    async def _on_ws_message(self, message) -> None:
        """WS handler. Must never raise — an exception kills the driver loop."""
        try:
            frame = json.loads(message) if isinstance(message, str) else message
            if not isinstance(frame, dict):
                return
            msg = parse_posted(frame, self._own_user_id)
            if msg is None:
                return
            decided = await self._decide(msg)
            if decided is None:
                return
            decided = await self._with_attachments(decided)
            # Hand to the sink (fire-and-forget): a long turn must not block the
            # WS loop, and a burst on one conversation folds into one turn.
            self._sink.submit(decided, self._chat)
        except Exception:
            logger.exception("failed to handle WS frame")

    async def _with_attachments(self, msg: IncomingMessage) -> IncomingMessage:
        """Download whatever the sender attached, so the message carries local
        paths. Only for messages we are actually going to answer — a download is
        wasted work on a post that was filtered out."""
        if self._attachments is None:
            return msg
        handles = parse_files(msg.raw)
        if not handles:
            return msg
        fetched: list[IncomingFile] = []
        for handle in handles:
            file = await self._fetch(handle)
            if file is not None:
                fetched.append(file)
        stored = await self._attachments.save_many(
            self._agent, msg.conversation_id, fetched
        )
        return replace(msg, attachments=stored) if stored else msg

    async def _fetch(self, handle: FileHandle) -> IncomingFile | None:
        """One attached file's bytes + description, or None (logged) if either
        call fails — a broken attachment must not cost the user their message."""
        name, mime = handle.name, handle.mime
        try:
            if not name:
                info = await self._driver.files.get_file_info(handle.file_id)
                if isinstance(info, dict):
                    name = str(info.get("name") or handle.file_id)
                    mime = mime or str(info.get("mime_type") or "")
            response = await self._driver.files.get_file(handle.file_id)
        except Exception:
            logger.warning("could not download file %s", handle.file_id, exc_info=True)
            return None
        # A file endpoint answers with raw bytes; a dict here means Mattermost
        # returned JSON instead — an error, not the file.
        data = getattr(response, "content", None)
        if not isinstance(data, bytes):
            logger.warning("unexpected response for file %s", handle.file_id)
            return None
        return IncomingFile(
            name=name or handle.file_id, data=data, mime=mime, key=handle.file_id
        )

    async def _decide(self, msg: IncomingMessage) -> IncomingMessage | None:
        """Respond-or-not (+ channel-session rewrite for resident channels).

        parse_posted already dropped our own posts, so a bot-authored message
        here is always someone else — another of our agents or a foreign bot."""
        if msg.is_from_bot:
            return self._decide_agent(msg)
        if not msg.is_dm and self._directory is not None:
            if await self._is_sole_resident(msg.channel_id):
                return to_channel_session(msg) if is_top_level(msg) else msg
        return msg if should_respond(msg) else None

    def _decide_agent(self, msg: IncomingMessage) -> IncomingMessage | None:
        """Inter-agent path: answer ANOTHER of our agents only when explicitly
        mentioned, and only if the loop guard allows it. Foreign bots never."""
        if not self._reply_to_agents or self._directory is None:
            return None
        if msg.user_id not in self._directory.agent_user_ids():
            return None  # a foreign bot, not one of ours
        if not msg.mentioned:
            return None  # our agent, but this turn wasn't addressed to us
        if self._loop_guard is not None:
            decision = self._loop_guard.check(
                conversation_id=msg.conversation_id, hop_depth=msg.hop_depth
            )
            if not decision.allow:
                logger.info(
                    "loop guard dropped agent turn in %s: %s",
                    msg.conversation_id,
                    decision.reason,
                )
                return None
        return msg

    async def _is_sole_resident(self, channel_id: str) -> bool:
        assert self._directory is not None
        agent_ids = self._directory.agent_user_ids()
        members = await self._channel_member_ids(channel_id)
        return members & agent_ids == {self._own_user_id}

    async def _channel_member_ids(self, channel_id: str) -> frozenset[str]:
        cached = self._members_cache.get(channel_id)
        if cached is not None and cached[0] > time.monotonic():
            return cached[1]
        try:
            members = await self._driver.channels.get_channel_members(
                channel_id, per_page=200
            )
            ids = frozenset(m["user_id"] for m in members)
        except Exception:
            logger.warning("get_channel_members %s failed", channel_id, exc_info=True)
            ids = frozenset()
        self._members_cache[channel_id] = (time.monotonic() + _MEMBERS_TTL_S, ids)
        return ids
