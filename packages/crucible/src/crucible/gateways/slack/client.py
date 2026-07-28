"""SlackChatClient: the ChatClient + ChatAdmin implementation over Slack's
AsyncWebClient (the bolt app's ``.client``). ChatClient now carries the widget
verbs (post_actions/retract/open_dialog)."""

import logging
import re

from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from crucible.gateways.slack.events import ts_time
from crucible.gateways.slack.formatter import markdown_to_mrkdwn
from crucible.gateways.slack.rendering import build_action_blocks, build_modal_view
from crucible.ports.chat.admin import ChannelMember
from crucible.ports.chat.types import (
    Action,
    ConversationRef,
    Form,
    PostSnippet,
    UserProfile,
)

logger = logging.getLogger(__name__)

# Encodes (channel, ts) into the opaque id post_actions returns, because Slack's
# chat.update needs both to edit a message later (retract).
_ID_SEP = "\x1f"


def chunk_text(text: str, limit: int) -> list[str]:
    """Split into <=limit chunks, preferring paragraph then line boundaries."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        cut = window.rfind("\n\n")
        if cut < limit // 2:
            cut = window.rfind("\n")
        if cut < limit // 2:
            cut = limit
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip("\n")
    if rest:
        chunks.append(rest)
    return chunks


class SlackChatClient:
    def __init__(self, client: AsyncWebClient, *, max_post_chars: int = 3500) -> None:
        self._client = client
        self._max_post_chars = max_post_chars
        self._usernames: dict[str, str] = {}  # user_id -> username cache

    async def post_reply(self, ref: ConversationRef, text: str, *, hop_depth: int = 0) -> None:
        # hop_depth is not propagated: Slack messages carry no round-tripping
        # metadata another agent could read (unlike MM post props).
        # Agent prose is Markdown; convert BEFORE chunking so a code fence or
        # link never straddles a chunk boundary mid-token.
        await self._post_chunks(ref, markdown_to_mrkdwn(text))

    async def post_notice(self, ref: ConversationRef, text: str) -> None:
        # Port contract: notices are fixed system strings, sent verbatim.
        await self._post_chunks(ref, text)

    async def _post_chunks(self, ref: ConversationRef, text: str) -> None:
        for chunk in chunk_text(text, self._max_post_chars):
            await self._client.chat_postMessage(
                channel=ref.channel_id, thread_ts=ref.thread_root_id or None, text=chunk
            )

    async def add_reaction(self, ref: ConversationRef, name: str) -> None:
        try:
            await self._client.reactions_add(
                channel=ref.channel_id, timestamp=ref.message_id, name=name
            )
        except SlackApiError as exc:
            if _err(exc) != "already_reacted":
                logger.debug("reactions_add %r failed: %s", name, _err(exc))

    async def remove_reaction(self, ref: ConversationRef, name: str) -> None:
        try:
            await self._client.reactions_remove(
                channel=ref.channel_id, timestamp=ref.message_id, name=name
            )
        except SlackApiError as exc:
            if _err(exc) != "no_reaction":
                logger.debug("reactions_remove %r failed: %s", name, _err(exc))

    async def get_user_profile(self, user_id: str) -> UserProfile | None:
        try:
            resp = await self._client.users_info(user=user_id)
        except SlackApiError:
            logger.warning("users_info %s failed", user_id, exc_info=True)
            return None
        user = resp.get("user") or {}
        profile = user.get("profile") or {}
        return UserProfile(
            username=user.get("name", ""),
            display_name=profile.get("display_name") or profile.get("real_name") or user.get("name", ""),
            is_bot=bool(user.get("is_bot")),
        )

    async def get_thread_posts(self, ref: ConversationRef) -> list[PostSnippet]:
        root = ref.thread_root_id or ref.conversation_id
        try:
            resp = await self._client.conversations_replies(channel=ref.channel_id, ts=root)
        except SlackApiError:
            logger.warning("conversations_replies %s failed", root, exc_info=True)
            return []
        return await self._to_snippets(resp.get("messages") or [])

    async def get_recent_posts(self, channel_id: str, limit: int = 20) -> list[PostSnippet]:
        try:
            resp = await self._client.conversations_history(channel=channel_id, limit=limit)
        except SlackApiError:
            logger.warning("conversations_history %s failed", channel_id, exc_info=True)
            return []
        # history is newest-first; snippets are chronological.
        return await self._to_snippets(list(reversed(resp.get("messages") or [])))

    def format_mention(self, username: str) -> str:
        # Best-effort: a real Slack mention needs the user id (<@Uxxx>), which this
        # port doesn't carry. Used only for agent-to-agent addressing.
        return f"@{username}"

    # -- Interactive widgets (ChatClient) -----------------------------------

    async def post_actions(
        self, ref: ConversationRef, text: str, actions: list[Action], *, callback_url: str
    ) -> str:
        # callback_url is unused: Socket Mode delivers clicks over the WebSocket,
        # routed by action_id, not to a URL.
        # The prompt text is model-authored (ask_user_* tools) — convert it; the
        # section block renders mrkdwn.
        text = markdown_to_mrkdwn(text)
        resp = await self._client.chat_postMessage(
            channel=ref.channel_id,
            thread_ts=ref.thread_root_id or None,
            text=text,
            blocks=build_action_blocks(text, actions),
        )
        return f"{resp.get('channel', ref.channel_id)}{_ID_SEP}{resp.get('ts', '')}"

    async def retract(self, post_id: str, text: str) -> None:
        channel, _, ts = post_id.partition(_ID_SEP)
        if not ts:
            return
        try:
            await self._client.chat_update(channel=channel, ts=ts, text=text, blocks=[])
        except SlackApiError:
            logger.debug("chat_update (retract) failed for %s", post_id, exc_info=True)

    async def open_dialog(
        self, trigger_id: str, form: Form, *, submit_url: str, state: str
    ) -> None:
        # submit_url is unused: a Slack modal submission arrives as a view_submission
        # over the socket, not as an HTTP POST.
        await self._client.views_open(trigger_id=trigger_id, view=build_modal_view(form, state))

    # -- ChatAdmin port -----------------------------------------------------

    async def create_channel(
        self, name: str, display_name: str, *, private: bool = True, purpose: str = ""
    ) -> str:
        resp = await self._client.conversations_create(name=_slug(name), is_private=private)
        channel_id = (resp.get("channel") or {}).get("id", "")
        if purpose and channel_id:
            try:
                await self._client.conversations_setPurpose(channel=channel_id, purpose=purpose)
            except SlackApiError:
                logger.debug("conversations_setPurpose failed for %s", channel_id, exc_info=True)
        return channel_id

    async def invite_to_channel(self, channel_id: str, user_id: str) -> None:
        await self._client.conversations_invite(channel=channel_id, users=user_id)

    async def get_channel_members(self, channel_id: str) -> list[ChannelMember]:
        try:
            resp = await self._client.conversations_members(channel=channel_id)
        except SlackApiError:
            logger.warning("conversations_members %s failed", channel_id, exc_info=True)
            return []
        return [
            ChannelMember(user_id=uid, username=await self._username(uid))
            for uid in (resp.get("members") or [])
        ]

    async def resolve_username(self, username: str) -> str | None:
        # Slack has no @username lookup; scan the workspace by handle (bounded).
        # Agents are resolved by name via the directory first, so this is only hit
        # for a stray @username.
        target = username.lstrip("@").lower()
        try:
            resp = await self._client.users_list(limit=200)
        except SlackApiError:
            return None
        for user in resp.get("members") or []:
            if str(user.get("name", "")).lower() == target:
                return user.get("id")
        return None

    async def post_message(self, channel_id: str, message: str, *, hop_depth: int = 0) -> str:
        # hop_depth is not carried on Slack (no message metadata); loop protection
        # falls back to the rate window. The message is model-authored Markdown
        # (send_message tool) — convert like a reply.
        resp = await self._client.chat_postMessage(
            channel=channel_id, text=markdown_to_mrkdwn(message)
        )
        return resp.get("ts", "")

    async def get_channel_posts(self, channel_id: str, limit: int = 20) -> list[PostSnippet]:
        return await self.get_recent_posts(channel_id, limit)

    # -- internals ----------------------------------------------------------

    async def _to_snippets(self, messages: list[dict]) -> list[PostSnippet]:
        snippets = []
        for m in messages:
            if m.get("subtype") in ("channel_join", "channel_leave") or not m.get("text"):
                continue
            user_id = m.get("user") or m.get("bot_id") or ""
            snippets.append(
                PostSnippet(
                    message_id=m.get("ts", ""),
                    username=await self._username(user_id),
                    text=m["text"],
                    timestamp=ts_time(m.get("ts", "")),
                )
            )
        return snippets

    async def _username(self, user_id: str) -> str:
        if not user_id:
            return "unknown"
        cached = self._usernames.get(user_id)
        if cached is not None:
            return cached
        profile = await self.get_user_profile(user_id)
        username = profile.username if profile and profile.username else user_id
        self._usernames[user_id] = username
        return username


def _err(exc: SlackApiError) -> str:
    return exc.response.get("error", "unknown") if exc.response else "unknown"


def _slug(name: str) -> str:
    """Coerce a channel name to Slack's rules (lowercase, [a-z0-9-_], <= 80)."""
    slug = re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-_")
    return (slug or "channel")[:80]
