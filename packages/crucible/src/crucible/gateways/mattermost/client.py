"""MattermostChatClient: the ChatClient implementation over AsyncTypedDriver."""

import logging
import re
from typing import Any

from mattermostautodriver import AsyncTypedDriver

from crucible.gateways.mattermost.events import PROPS_KEY, post_time
from crucible.ports.chat.admin import ChannelMember
from crucible.ports.chat.types import (
    Action,
    ConversationRef,
    Form,
    PostSnippet,
    UserProfile,
)

logger = logging.getLogger(__name__)


def chunk_text(text: str, limit: int) -> list[str]:
    """Split into <=limit chunks, preferring paragraph then line boundaries."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        cut = window.rfind("\n\n")
        if cut < limit // 2:  # no good paragraph break — try a line break
            cut = window.rfind("\n")
        if cut < limit // 2:  # still nothing usable — hard cut
            cut = limit
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip("\n")
    if rest:
        chunks.append(rest)
    return chunks


class MattermostChatClient:
    """Posts as the agent's own bot account (markdown is native in MM)."""

    def __init__(self, driver: AsyncTypedDriver, *, max_post_chars: int = 4000) -> None:
        self._driver = driver
        self._max_post_chars = max_post_chars
        self._usernames: dict[str, str] = {}  # user_id -> username cache
        self._team_id: str = ""  # resolved lazily from the agent's memberships

    @property
    def _own_user_id(self) -> str:
        return self._driver.client.userid or ""

    async def post_reply(self, ref: ConversationRef, text: str, *, hop_depth: int = 0) -> None:
        # Stamp our hop depth so other agents reading this post account for the
        # cascade; chunked replies all carry it (harmless on the extra chunks).
        props = {PROPS_KEY: {"depth": hop_depth}} if hop_depth else None
        for chunk in chunk_text(text, self._max_post_chars):
            await self._driver.posts.create_post(
                channel_id=ref.channel_id,
                message=chunk,
                root_id=ref.thread_root_id or None,
                props=props,
            )

    async def post_notice(self, ref: ConversationRef, text: str) -> None:
        # Same transport as post_reply; the verb split keeps the port's
        # "verbatim system text" semantics available to richer adapters.
        await self.post_reply(ref, text)

    async def add_reaction(self, ref: ConversationRef, name: str) -> None:
        try:
            await self._driver.reactions.save_reaction(
                {
                    "user_id": self._own_user_id,
                    "post_id": ref.message_id,
                    "emoji_name": name,
                }
            )
        except Exception:  # reactions are cosmetic — never fail a turn over one
            logger.debug("add_reaction %r failed", name, exc_info=True)

    async def remove_reaction(self, ref: ConversationRef, name: str) -> None:
        try:
            await self._driver.reactions.delete_reaction(
                self._own_user_id, ref.message_id, name
            )
        except Exception:
            logger.debug("remove_reaction %r failed", name, exc_info=True)

    async def get_user_profile(self, user_id: str) -> UserProfile | None:
        try:
            user = await self._driver.users.get_user(user_id)
        except Exception:
            logger.warning("get_user %s failed", user_id, exc_info=True)
            return None
        display = (
            user.get("nickname")
            or f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
            or user.get("username", "")
        )
        return UserProfile(
            username=user.get("username", ""),
            display_name=display,
            is_bot=bool(user.get("is_bot")),
        )

    async def get_thread_posts(self, ref: ConversationRef) -> list[PostSnippet]:
        root_id = ref.thread_root_id or ref.conversation_id
        try:
            data = await self._driver.posts.get_post_thread(root_id)
        except Exception:
            logger.warning("get_post_thread %s failed", root_id, exc_info=True)
            return []
        return await self._to_snippets(data)

    async def get_recent_posts(self, channel_id: str, limit: int = 20) -> list[PostSnippet]:
        try:
            data = await self._driver.posts.get_posts_for_channel(
                channel_id, per_page=limit
            )
        except Exception:
            logger.warning("get_posts_for_channel %s failed", channel_id, exc_info=True)
            return []
        return await self._to_snippets(data)

    async def _to_snippets(self, data: dict) -> list[PostSnippet]:
        """Posts payload ({order, posts}) -> chronological snippets, no system posts."""
        posts = [
            post
            for post in (data.get("posts") or {}).values()
            if isinstance(post, dict) and not post.get("type") and post.get("message")
        ]
        posts.sort(key=lambda post: post.get("create_at", 0))
        return [
            PostSnippet(
                message_id=post.get("id", ""),
                username=await self._username(post.get("user_id", "")),
                text=post["message"],
                timestamp=post_time(post),
            )
            for post in posts
        ]

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

    def format_mention(self, username: str) -> str:
        return f"@{username}"

    # -- Interactive widgets (ChatClient) -----------------------------------

    async def post_actions(
        self, ref: ConversationRef, text: str, actions: list[Action], *, callback_url: str
    ) -> str:
        # MM interactive actions: props.attachments[].actions[]. type must be
        # "button" or "select" (else MM silently drops the integration), each
        # with an integration.{url,context} that fires on click. A button carries
        # its value in context statically; a select's value is dynamic — MM adds
        # the picked option to the callback context as "selected_option".
        att_actions = []
        for a in actions:
            if a.kind == "select":
                att_actions.append(
                    {
                        "id": a.id,
                        "name": a.label,
                        "type": "select",
                        "options": [{"text": o, "value": o} for o in a.options],
                        "integration": {"url": callback_url, "context": {**a.context}},
                    }
                )
            else:
                att_actions.append(
                    {
                        "id": a.id,
                        "name": a.label,
                        "type": "button",
                        **({"style": a.style} if a.style else {}),
                        "integration": {
                            "url": callback_url,
                            "context": {**a.context, "value": a.value},
                        },
                    }
                )
        post = await self._driver.posts.create_post(
            channel_id=ref.channel_id,
            message=text,
            root_id=ref.thread_root_id or None,
            props={"attachments": [{"actions": att_actions}]},
        )
        return post["id"]

    async def retract(self, post_id: str, text: str) -> None:
        # Rewrite the message and drop its attachments, so an expired/cancelled
        # widget's buttons can't be clicked afterwards (MM shows an integration
        # error on a stale click). props is a JSON object here — the driver
        # mistypes patch_post's props as str (create_post types it dict), so the
        # ignore is a library-typing gap, not our bug.
        await self._driver.posts.patch_post(
            post_id,
            message=text,
            props={"attachments": []},  # type: ignore[arg-type]
        )

    async def open_dialog(
        self, trigger_id: str, form: Form, *, submit_url: str, state: str
    ) -> None:
        # trigger_id is short-lived (~3s) — this is called synchronously from the
        # click handler. MM posts the submission to submit_url with state echoed.
        elements: list[dict[str, Any]] = []
        for f in form.fields:
            el: dict[str, Any] = {
                "display_name": f.label,
                "name": f.name,
                "type": f.type,
                "optional": f.optional,
            }
            if f.placeholder:
                el["placeholder"] = f.placeholder
            if f.type == "select":
                el["options"] = [{"text": o, "value": o} for o in f.options]
            elements.append(el)
        await self._driver.integration_actions.open_interactive_dialog(
            trigger_id=trigger_id,
            url=submit_url,
            dialog={
                "callback_id": "form",
                "title": form.title,
                "submit_label": form.submit_label,
                "state": state,
                "elements": elements,
            },
        )

    # -- ChatAdmin port -----------------------------------------------------

    async def create_channel(
        self, name: str, display_name: str, *, private: bool = True, purpose: str = ""
    ) -> str:
        team_id = await self._resolve_team_id()
        channel = await self._driver.channels.create_channel(
            team_id=team_id,
            name=_slugify(name),
            display_name=display_name or name,
            type="P" if private else "O",
            purpose=purpose,
        )
        return channel["id"]

    async def invite_to_channel(self, channel_id: str, user_id: str) -> None:
        await self._driver.channels.add_channel_member(channel_id, user_id=user_id)

    async def get_channel_members(self, channel_id: str) -> list[ChannelMember]:
        members = await self._driver.channels.get_channel_members(channel_id, per_page=200)
        return [
            ChannelMember(
                user_id=m["user_id"], username=await self._username(m["user_id"])
            )
            for m in members
        ]

    async def resolve_username(self, username: str) -> str | None:
        try:
            user = await self._driver.users.get_user_by_username(username.lstrip("@"))
        except Exception:
            return None
        return user.get("id")

    async def post_message(self, channel_id: str, message: str, *, hop_depth: int = 0) -> str:
        # Top-level post (no root_id), same hop-stamp + chunking as post_reply.
        # Returns the first chunk's id.
        props = {PROPS_KEY: {"depth": hop_depth}} if hop_depth else None
        post_id = ""
        for chunk in chunk_text(message, self._max_post_chars):
            post = await self._driver.posts.create_post(
                channel_id=channel_id, message=chunk, props=props
            )
            post_id = post_id or post["id"]
        return post_id

    async def get_channel_posts(self, channel_id: str, limit: int = 20) -> list[PostSnippet]:
        return await self.get_recent_posts(channel_id, limit)

    async def post_ephemeral(self, channel_id: str, user_id: str, message: str) -> None:
        # Only user_id sees it; markdown is native in MM, so post as-is.
        await self._driver.posts.create_post_ephemeral(
            user_id=user_id, post={"channel_id": channel_id, "message": message}
        )

    async def _resolve_team_id(self) -> str:
        if self._team_id:
            return self._team_id
        teams = await self._driver.teams.get_teams_for_user(self._own_user_id)
        if not teams:
            raise RuntimeError("agent belongs to no team; cannot create a channel")
        self._team_id = teams[0]["id"]
        return self._team_id


def _slugify(name: str) -> str:
    """Coerce a channel name to Mattermost's slug rules (lowercase [a-z0-9-])."""
    slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    return slug or "channel"
