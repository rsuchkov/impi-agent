"""AgentFlow: turns a batch of conversation messages into one agent reply.

Depends only on ports (AgentRuntime / SessionStore / ChatClient): swapping the
runtime or the platform never touches this file. Batch-native so the coalescer
can merge messages that arrived during a long turn into a single turn/reply.
"""

import asyncio
import logging
from datetime import datetime
from pathlib import Path

from crucible.attachments import sniff_image
from crucible.ports.agent import (
    AgentError,
    AgentProfile,
    AgentRuntime,
    AgentTimeout,
    PromptImage,
    message_for,
)
from crucible.ports.chat.client import ChatClient
from crucible.ports.chat.flow import TurnOutcome
from crucible.ports.chat.types import (
    KIND_CHANNEL,
    KIND_THREAD,
    Attachment,
    IncomingMessage,
    PostSnippet,
)
from crucible.store.base import SessionRecord, SessionStore

logger = logging.getLogger(__name__)

LOADING_REACTION = "eyes"
EMPTY_ANSWER_MESSAGE = "I thought about it but produced no answer — please try rephrasing."
_MAX_BACKFILL_CHARS = 6000
_CHANNEL_BACKFILL_LIMIT = 20
_FIRST_TURN_HEADER = "[context: earlier conversation]"
# Later turns: what people said in this conversation while the agent wasn't
# addressed (in a channel it only runs when mentioned, so those messages never
# reached it as a turn).
_CATCH_UP_HEADER = "[context: posted since your last reply]"
# Attached files are named in the prompt, always: even when a picture also
# travels to the runtime as an image, the agent needs the path to work with it.
_ATTACHMENT_MARKER = "[attached]"
# Defaults for showing pictures to the runtime. The size cap is per image and
# deliberately modest — model backends reject large inline images, and an
# oversized one is still reachable by path.
DEFAULT_INLINE_IMAGE_MAX_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_INLINE_IMAGES = 4
# How a model backend's complaint about a picture reads, roughly: enough to tell
# a poisoned conversation apart from an ordinary failed turn.
_IMAGE_ERROR_WORDS = ("image", "picture")


def _format_time(dt: datetime) -> str:
    """Render a message time for the prompt envelope. UTC keeps it unambiguous; the
    newest message is ~now, so the agent can reason about when things were said."""
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def _format_size(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size} B"


def _attachment_line(attachment: Attachment) -> str:
    """One attached file, as the agent sees it: name, type, size, and the path it
    can read."""
    kind = attachment.mime or "unknown type"
    return (
        f"{_ATTACHMENT_MARKER} {attachment.name} — {kind}, "
        f"{_format_size(attachment.size)} — {attachment.path}"
    )


def _parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class AgentFlow:
    def __init__(
        self,
        runtime: AgentRuntime,
        profile: AgentProfile,
        sessions: SessionStore,
        *,
        agent_name: str,
        inline_image_max_bytes: int = DEFAULT_INLINE_IMAGE_MAX_BYTES,
        max_inline_images: int = DEFAULT_MAX_INLINE_IMAGES,
    ) -> None:
        self._runtime = runtime
        self._profile = profile
        self._sessions = sessions
        self._agent_name = agent_name
        self._own_user_id = ""
        self._inline_image_max_bytes = inline_image_max_bytes
        self._max_inline_images = max_inline_images

    def set_identity(self, user_id: str) -> None:
        """This agent's own platform user id, learned at gateway login. Used to
        keep the agent's own posts out of replayed history (they are already in
        its runtime session). Unset = no filtering."""
        self._own_user_id = user_id

    @property
    def profile(self) -> AgentProfile:
        """The profile the next turn will use — kept current by set_profile, so
        a caller that runs something outside the flow (a memoryless scheduled
        run) uses the same configuration a reload just applied."""
        return self._profile

    def set_profile(self, profile: AgentProfile) -> None:
        """Swap the profile the next turn runs with (hot-reload). Only affects
        conversations whose session has not started yet; a live conversation
        keeps its current profile until the runtime resets its session."""
        self._profile = profile

    async def handle(self, msg: IncomingMessage, chat: ChatClient) -> TurnOutcome:
        return await self.handle_batch([msg], chat)

    async def handle_batch(
        self, msgs: list[IncomingMessage], chat: ChatClient
    ) -> TurnOutcome:
        """Handle one or more messages of the SAME conversation as a single turn.

        Dedup drops replays; the newest surviving message anchors the reply (its
        thread position, hop accounting). All messages share the runtime session,
        so their texts are merged into one prompt with per-message attribution.

        The outcome is for a caller that started this turn and has to report on
        it; a gateway ignores it. Every failing outcome has already been told to
        the user by the time it is returned."""
        fresh = [
            m
            for m in msgs
            if await self._sessions.mark_processed(self._agent_name, m.ref.message_id)
        ]
        if not fresh:
            return TurnOutcome.DUPLICATE
        anchor = fresh[-1]

        record, created = await self._sessions.get_or_create(
            self._agent_name, anchor.channel_id, anchor.conversation_id, anchor.kind,
            user_id=anchor.user_id,
        )
        # On a later turn, last_active is still the PREVIOUS turn's end (touch()
        # runs after the turn, and get_or_create doesn't refresh it) — exactly the
        # cursor for "what was said while we weren't addressed".
        since = None if created else _parse_iso(record.last_active)
        prompt = await self._render_prompt(fresh, chat, first_turn=created, since=since)
        images = await self._inline_images(fresh)

        # Only a real message can wear the "working on it" mark. A synthetic
        # anchor — a slash command, a widget click, a scheduled run — carries an
        # id the platform never issued, so reacting to it just makes the gateway
        # log a rejection nobody can act on.
        acknowledge = not anchor.synthetic
        if acknowledge:
            await chat.add_reaction(anchor.ref, LOADING_REACTION)
        try:
            result = await self._runtime.run_stateful(
                self._profile, record.runtime_session_id, prompt, images=images
            )
        except AgentTimeout as exc:
            logger.warning(
                "agent turn timed out for %s: %s", record.runtime_session_id, exc
            )
            await chat.post_notice(anchor.ref, message_for(exc))
            return TurnOutcome.TIMEOUT
        except AgentError as exc:
            # The cause goes to the log in full; what reaches the conversation is
            # which KIND of failure it was, because a notice is an ordinary
            # message everyone present can read.
            logger.exception("agent run failed for %s", record.runtime_session_id)
            self._warn_if_picture_rejected(exc, record)
            await chat.post_notice(anchor.ref, message_for(exc))
            return TurnOutcome.ERROR
        finally:
            if acknowledge:
                await chat.remove_reaction(anchor.ref, LOADING_REACTION)

        await self._sessions.touch(self._agent_name, anchor.conversation_id)
        if result.text:
            # One hop past the deepest message in the batch, so any agent we
            # mention accounts for the cascade (see LoopGuard).
            hop = max(m.hop_depth for m in fresh) + 1
            await chat.post_reply(anchor.ref, result.text, hop_depth=hop)
            return TurnOutcome.REPLIED
        if result.tool_calls:
            return TurnOutcome.ACTED
        # No text AND no tool call: the turn produced nothing at all — nudge the
        # user to rephrase. When the agent DID call a tool, the silence is
        # deliberate: a tool that declares it speaks to the user has already put
        # the message there (buttons, a form, a panel), and a fallback here would
        # double up on the already-visible action.
        await chat.post_notice(anchor.ref, EMPTY_ANSWER_MESSAGE)
        return TurnOutcome.EMPTY

    # -- internals ----------------------------------------------------------

    async def _render_prompt(
        self, msgs: list[IncomingMessage], chat: ChatClient, *, first_turn: bool,
        since: datetime | None = None,
    ) -> str:
        """Envelope: who is speaking (+ replayed history).

        The conversation itself lives in the runtime session, so only sender
        identity travels with each message — vital once multiple people and agents
        share a channel. History is replayed into the prompt (the runtime has no
        history-injection API) in two shapes: on the FIRST turn of a session with
        prior history, the whole transcript; on later turns, only what was posted
        since our last reply — in a channel the agent only runs when mentioned, so
        anything said in between never reached it as a turn."""
        anchor = msgs[-1]
        parts: list[str] = []
        batch_ids = {m.ref.message_id for m in msgs}
        snippets = await self._backfill_snippets(anchor, chat)
        if not first_turn:
            snippets = self._since(snippets, since)
        if snippets:
            transcript = self._render_transcript(snippets, exclude=batch_ids)
            if transcript:
                header = _FIRST_TURN_HEADER if first_turn else _CATCH_UP_HEADER
                parts.append(f"{header}\n{transcript}\n[/context]")
        for m in msgs:
            author = f"@{m.username}" if m.username else m.user_id
            when = f" · {_format_time(m.timestamp)}" if m.timestamp else ""
            lines = [f"[{author}{when}]: {m.text}"]
            lines += [_attachment_line(a) for a in m.attachments]
            parts.append("\n".join(lines))
        return "\n\n".join(parts)

    @staticmethod
    def _warn_if_picture_rejected(error: Exception, record: SessionRecord) -> None:
        """A picture the model backend refuses is not a one-turn problem: the
        session replays its history, so the same request fails from then on. The
        engine only shows pictures it has verified, so reaching this means the
        backend refused one it accepts the format of (an enormous one, say) —
        say so plainly, with the way out."""
        if not any(word in str(error).lower() for word in _IMAGE_ERROR_WORDS):
            return
        logger.error(
            "the model backend refused a picture in session %s. Its history "
            "replays on every turn, so this conversation will keep failing until "
            "it is reset: `python -m crucible.sessions_cli delete %s %s`",
            record.runtime_session_id, record.agent, record.conversation_id,
        )

    async def _inline_images(self, msgs: list[IncomingMessage]) -> list[PromptImage]:
        """Pictures from this batch, for runtimes that can look at them.

        Newest first and capped in count and size: a model backend rejects large
        inline images, and everything attached is named by path in the prompt
        anyway, so leaving one out costs the agent nothing but a read."""
        images: list[PromptImage] = []
        for msg in reversed(msgs):
            for attachment in msg.attachments:
                if len(images) >= self._max_inline_images:
                    return images
                if not attachment.is_image:
                    continue
                if attachment.size > self._inline_image_max_bytes:
                    logger.info(
                        "not showing %s inline (%d bytes); the path is in the prompt",
                        attachment.name, attachment.size,
                    )
                    continue
                try:
                    data = await asyncio.to_thread(Path(attachment.path).read_bytes)
                except OSError as exc:
                    logger.warning("could not read attachment %s: %s", attachment.path, exc)
                    continue
                # Trust the bytes, not the label: a file that only claims to be a
                # picture would be rejected by the model backend on this turn AND
                # on every later one, since the session replays its history.
                actual = sniff_image(data)
                if not actual:
                    logger.warning(
                        "%s is not a picture the runtime can read despite its %s type; "
                        "the path is in the prompt", attachment.name, attachment.mime,
                    )
                    continue
                images.append(PromptImage(data=data, mime=actual))
        return images

    async def _backfill_snippets(
        self, msg: IncomingMessage, chat: ChatClient
    ) -> list[PostSnippet]:
        root_id = msg.ref.thread_root_id
        if msg.kind == KIND_THREAD and root_id and root_id != msg.ref.message_id:
            return await chat.get_thread_posts(msg.ref)
        if msg.kind == KIND_CHANNEL:
            return await chat.get_recent_posts(
                msg.channel_id, limit=_CHANNEL_BACKFILL_LIMIT
            )
        return []

    def _since(self, snippets: list[PostSnippet], since: datetime | None) -> list[PostSnippet]:
        """Catch-up filter: only messages posted after our last reply, and not our
        own (those are already in the runtime session). A snippet without a
        timestamp can't be placed in time, so it is left out rather than replayed
        on every turn."""
        if since is None:
            return []
        return [
            s
            for s in snippets
            if s.timestamp is not None
            and s.timestamp > since
            and not (self._own_user_id and s.user_id == self._own_user_id)
        ]

    @staticmethod
    def _render_transcript(snippets: list[PostSnippet], *, exclude: set[str]) -> str:
        lines = [
            f"[@{s.username}{f' · {_format_time(s.timestamp)}' if s.timestamp else ''}]: {s.text}"
            for s in snippets
            if s.message_id not in exclude
        ]
        # Budget from the END: the latest messages carry the most context.
        kept: list[str] = []
        budget = _MAX_BACKFILL_CHARS
        for line in reversed(lines):
            budget -= len(line) + 1
            if budget < 0:
                break
            kept.append(line)
        return "\n".join(reversed(kept))
