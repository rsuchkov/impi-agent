"""AgentFlow: turns a batch of conversation messages into one agent reply.

Depends only on ports (AgentRuntime / SessionStore / ChatClient): swapping the
runtime or the platform never touches this file. Batch-native so the coalescer
can merge messages that arrived during a long turn into a single turn/reply.
"""

import logging
from datetime import datetime

from crucible.ports.agent import (
    INTERNAL_ERROR_MESSAGE,
    LLM_FALLBACK_MESSAGE,
    AgentError,
    AgentProfile,
    AgentRuntime,
    AgentTimeout,
)
from crucible.ports.chat.client import ChatClient
from crucible.ports.chat.types import KIND_CHANNEL, KIND_THREAD, IncomingMessage, PostSnippet
from crucible.store.base import SessionStore

logger = logging.getLogger(__name__)

LOADING_REACTION = "eyes"
EMPTY_ANSWER_MESSAGE = "I thought about it but produced no answer — please try rephrasing."
_MAX_BACKFILL_CHARS = 6000
_CHANNEL_BACKFILL_LIMIT = 20


def _format_time(dt: datetime) -> str:
    """Render a message time for the prompt envelope. UTC keeps it unambiguous; the
    newest message is ~now, so the agent can reason about when things were said."""
    return dt.strftime("%Y-%m-%d %H:%M UTC")


class AgentFlow:
    def __init__(
        self,
        runtime: AgentRuntime,
        profile: AgentProfile,
        sessions: SessionStore,
        *,
        agent_name: str,
    ) -> None:
        self._runtime = runtime
        self._profile = profile
        self._sessions = sessions
        self._agent_name = agent_name

    def set_profile(self, profile: AgentProfile) -> None:
        """Swap the profile the next turn runs with (hot-reload). Only affects
        conversations whose session has not started yet; a live conversation
        keeps its current profile until the runtime resets its session."""
        self._profile = profile

    async def handle(self, msg: IncomingMessage, chat: ChatClient) -> None:
        await self.handle_batch([msg], chat)

    async def handle_batch(self, msgs: list[IncomingMessage], chat: ChatClient) -> None:
        """Handle one or more messages of the SAME conversation as a single turn.

        Dedup drops replays; the newest surviving message anchors the reply (its
        thread position, hop accounting). All messages share the runtime session,
        so their texts are merged into one prompt with per-message attribution."""
        fresh = [
            m
            for m in msgs
            if await self._sessions.mark_processed(self._agent_name, m.ref.message_id)
        ]
        if not fresh:
            return
        anchor = fresh[-1]

        record, created = await self._sessions.get_or_create(
            self._agent_name, anchor.channel_id, anchor.conversation_id, anchor.kind
        )
        prompt = await self._render_prompt(fresh, chat, first_turn=created)

        await chat.add_reaction(anchor.ref, LOADING_REACTION)
        try:
            result = await self._runtime.run_stateful(
                self._profile, record.runtime_session_id, prompt
            )
        except AgentTimeout:
            await chat.post_notice(anchor.ref, LLM_FALLBACK_MESSAGE)
            return
        except AgentError:
            logger.exception("agent run failed for %s", record.runtime_session_id)
            await chat.post_notice(anchor.ref, INTERNAL_ERROR_MESSAGE)
            return
        finally:
            await chat.remove_reaction(anchor.ref, LOADING_REACTION)

        await self._sessions.touch(self._agent_name, anchor.conversation_id)
        if result.text:
            # One hop past the deepest message in the batch, so any agent we
            # mention accounts for the cascade (see LoopGuard).
            hop = max(m.hop_depth for m in fresh) + 1
            await chat.post_reply(anchor.ref, result.text, hop_depth=hop)
        elif not result.tool_calls:
            # No text AND no tool call: the turn produced nothing at all — nudge
            # the user to rephrase. When the agent DID call a tool (e.g. a
            # fire-and-forget widget like ask_user_buttons, whose posted buttons
            # ARE the reply), the silence is deliberate and a fallback here would
            # just double up on the already-visible action.
            await chat.post_notice(anchor.ref, EMPTY_ANSWER_MESSAGE)

    # -- internals ----------------------------------------------------------

    async def _render_prompt(
        self, msgs: list[IncomingMessage], chat: ChatClient, *, first_turn: bool
    ) -> str:
        """Envelope: who is speaking (+ one-shot history backfill).

        The conversation itself lives in the runtime session, so only sender
        identity travels with each message — vital once multiple people and agents
        share a channel. On the FIRST turn of a session with prior history (a
        pre-existing thread, or a channel session), the transcript is replayed into
        the prompt — the runtime has no history-injection API."""
        anchor = msgs[-1]
        parts: list[str] = []
        if first_turn:
            batch_ids = {m.ref.message_id for m in msgs}
            transcript = self._render_transcript(
                await self._backfill_snippets(anchor, chat), exclude=batch_ids
            )
            if transcript:
                parts.append(f"[context: earlier conversation]\n{transcript}\n[/context]")
        for m in msgs:
            author = f"@{m.username}" if m.username else m.user_id
            when = f" · {_format_time(m.timestamp)}" if m.timestamp else ""
            parts.append(f"[{author}{when}]: {m.text}")
        return "\n\n".join(parts)

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
