from datetime import datetime, timezone
from pathlib import Path

from crucible.flows.agent_flow import EMPTY_ANSWER_MESSAGE, AgentFlow
from crucible.ports.agent.errors import (
    INTERNAL_ERROR_MESSAGE,
    LLM_FALLBACK_MESSAGE,
)
from crucible.ports.chat.types import (
    KIND_CHANNEL,
    KIND_DM,
    KIND_THREAD,
    ConversationRef,
    IncomingMessage,
    PostSnippet,
)
from crucible.runtimes.pi.errors import PiProcessError, PiTimeout
from crucible.runtimes.pi.profiles import PiProfile
from crucible.runtimes.pi.session import PiResult
from crucible.store.sessions import SqliteSessionStore
from tests.fakes.fake_chat import FakeChat


class FakeRuntime:
    def __init__(self, *, result: PiResult | None = None, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, str]] = []  # (session_id, prompt)
        self.profiles: list[object] = []  # profile used per turn
        self._result = result or PiResult(text="agent answer")
        self._error = error

    async def run_stateful(self, profile, session_id, message, *, on_event=None, cwd=None):
        self.calls.append((session_id, message))
        self.profiles.append(profile)
        if self._error is not None:
            raise self._error
        return self._result

    async def run_stateless(self, profile, message, *, on_event=None):
        raise AssertionError("stage-1 flow must be stateful")

    def start(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def drop_agent_sessions(self, agent: str) -> int:
        return 0


PROFILE = PiProfile(name="assistant", config_dir=Path("agents/assistant"), timeout=5.0)


def _dm(text: str = "hello") -> IncomingMessage:
    return IncomingMessage(
        ref=ConversationRef(
            channel_id="dm1", conversation_id="dm1", message_id="p1", thread_root_id=""
        ),
        text=text,
        user_id="u1",
        username="roman",
        kind=KIND_DM,
        is_dm=True,
    )


def _thread_msg(*, root: str = "root1", post: str = "p2") -> IncomingMessage:
    return IncomingMessage(
        ref=ConversationRef(
            channel_id="ch1", conversation_id=root, message_id=post, thread_root_id=root
        ),
        text="a question in the thread",
        user_id="u1",
        username="roman",
        kind=KIND_THREAD,
        mentioned=True,
    )


def _flow(tmp_path: Path, runtime: FakeRuntime) -> tuple[AgentFlow, SqliteSessionStore]:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    return AgentFlow(runtime, PROFILE, store, agent_name="assistant"), store


async def test_happy_path_replies_in_conversation(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    flow, store = _flow(tmp_path, runtime)
    chat = FakeChat()

    await flow.handle(_dm("how are you?"), chat)

    session_id, prompt = runtime.calls[0]
    assert session_id == "assistant--dm1"  # deterministic id from the store
    assert prompt == "[@roman]: how are you?"  # identity envelope
    assert chat.replies == [(_dm().ref, "agent answer")]
    assert chat.reactions == [("+eyes", "p1"), ("-eyes", "p1")]
    assert (await store.list())[0].kind == KIND_DM
    await store.close()


async def test_prompt_envelope_includes_the_timestamp(tmp_path: Path) -> None:
    # When the message carries a time, the envelope shows it so the agent knows
    # WHEN it was sent.
    runtime = FakeRuntime()
    flow, store = _flow(tmp_path, runtime)
    msg = IncomingMessage(
        ref=ConversationRef(channel_id="dm1", conversation_id="dm1", message_id="p1"),
        text="hi",
        user_id="u1",
        username="roman",
        timestamp=datetime(2026, 7, 18, 14, 30, tzinfo=timezone.utc),
        kind=KIND_DM,
        is_dm=True,
    )
    await flow.handle(msg, FakeChat())
    assert runtime.calls[0][1] == "[@roman · 2026-07-18 14:30 UTC]: hi"
    await store.close()


async def test_thread_first_turn_backfills_transcript(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    flow, store = _flow(tmp_path, runtime)
    chat = FakeChat()
    chat.thread_posts["root1"] = [
        PostSnippet(message_id="root1", username="roman", text="thread root message"),
        PostSnippet(message_id="pX", username="colleague", text="an earlier reply"),
        PostSnippet(message_id="p2", username="roman", text="a question in the thread"),
    ]

    await flow.handle(_thread_msg(), chat)
    await flow.handle(_thread_msg(post="p3"), chat)

    first_prompt = runtime.calls[0][1]
    assert "[@roman]: thread root message" in first_prompt  # transcript, once
    assert "[@colleague]: an earlier reply" in first_prompt
    assert first_prompt.count("a question in the thread") == 1  # current msg excluded
    assert "[@roman]: a question in the thread" in first_prompt
    second_prompt = runtime.calls[1][1]
    assert "thread root message" not in second_prompt
    await store.close()


async def test_channel_session_first_turn_backfills_recent_posts(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    flow, store = _flow(tmp_path, runtime)
    chat = FakeChat()
    chat.recent_posts["ch1"] = [
        PostSnippet(message_id="old1", username="roman", text="earlier channel chatter"),
    ]
    msg = IncomingMessage(
        ref=ConversationRef(
            channel_id="ch1", conversation_id="ch1", message_id="p9", thread_root_id=""
        ),
        text="and now a question",
        user_id="u1",
        username="roman",
        kind=KIND_CHANNEL,
    )

    await flow.handle(msg, chat)

    prompt = runtime.calls[0][1]
    assert "[@roman]: earlier channel chatter" in prompt
    assert prompt.endswith("[@roman]: and now a question")
    assert (await store.list())[0].kind == KIND_CHANNEL
    await store.close()


async def test_duplicate_post_is_processed_once(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    flow, store = _flow(tmp_path, runtime)
    chat = FakeChat()

    await flow.handle(_dm(), chat)
    await flow.handle(_dm(), chat)  # WS reconnect replays the same post id

    assert len(runtime.calls) == 1
    assert len(chat.replies) == 1
    await store.close()


async def test_new_thread_from_own_root_has_no_backfill(tmp_path: Path) -> None:
    # Top-level channel post: the conversation IS the post — nothing to backfill.
    runtime = FakeRuntime()
    flow, store = _flow(tmp_path, runtime)
    chat = FakeChat()
    msg = _thread_msg(root="p2", post="p2")

    await flow.handle(msg, chat)

    assert runtime.calls[0][1] == "[@roman]: a question in the thread"
    await store.close()


async def test_timeout_posts_fallback_and_removes_reaction(tmp_path: Path) -> None:
    flow, store = _flow(tmp_path, FakeRuntime(error=PiTimeout("slow")))
    chat = FakeChat()

    await flow.handle(_dm(), chat)

    assert chat.notices == [(_dm().ref, LLM_FALLBACK_MESSAGE)]
    assert chat.replies == []
    assert chat.reactions == [("+eyes", "p1"), ("-eyes", "p1")]
    await store.close()


async def test_agent_error_posts_internal_fallback(tmp_path: Path) -> None:
    flow, store = _flow(tmp_path, FakeRuntime(error=PiProcessError("boom")))
    chat = FakeChat()

    await flow.handle(_dm(), chat)

    assert chat.notices == [(_dm().ref, INTERNAL_ERROR_MESSAGE)]
    await store.close()


async def test_empty_answer_posts_nudge(tmp_path: Path) -> None:
    flow, store = _flow(tmp_path, FakeRuntime(result=PiResult(text="")))
    chat = FakeChat()

    await flow.handle(_dm(), chat)

    assert chat.notices == [(_dm().ref, EMPTY_ANSWER_MESSAGE)]
    assert chat.replies == []
    await store.close()


async def test_fire_and_forget_tool_turn_stays_silent(tmp_path: Path) -> None:
    # No text but a tool ran (e.g. ask_user_buttons posted the buttons itself):
    # the turn acted deliberately, so no nudge and no empty reply on top of it.
    result = PiResult(text="", tool_calls=["ask_user_buttons"])
    flow, store = _flow(tmp_path, FakeRuntime(result=result))
    chat = FakeChat()

    await flow.handle(_dm(), chat)

    assert chat.notices == []
    assert chat.replies == []
    await store.close()


async def test_reply_is_stamped_one_hop_past_trigger(tmp_path: Path) -> None:
    # A reply to a human (hop 0) is stamped hop 1; to an agent at hop 2 -> hop 3.
    runtime = FakeRuntime()
    flow, store = _flow(tmp_path, runtime)
    chat = FakeChat()

    await flow.handle(_dm(), chat)
    assert chat.reply_hops == [1]

    agent_msg = IncomingMessage(
        ref=ConversationRef(
            channel_id="ch1", conversation_id="root1", message_id="p2", thread_root_id="root1"
        ),
        text="@r42-assistant thoughts?",
        user_id="uid-dev",
        username="r42-developer",
        kind=KIND_THREAD,
        mentioned=True,
        is_from_bot=True,
        hop_depth=2,
    )
    await flow.handle(agent_msg, chat)
    assert chat.reply_hops[-1] == 3
    await store.close()


async def test_set_profile_takes_effect_on_the_next_turn(tmp_path: Path) -> None:
    # Hot-reload swaps the profile; the very next turn must run with the new one.
    runtime = FakeRuntime()
    flow, store = _flow(tmp_path, runtime)
    chat = FakeChat()

    await flow.handle(_dm("first"), chat)
    assert runtime.profiles[0] is PROFILE

    new_profile = PiProfile(name="assistant", config_dir=Path("agents/assistant"), timeout=9.0)
    flow.set_profile(new_profile)
    m2 = IncomingMessage(
        ref=ConversationRef(channel_id="dm1", conversation_id="dm1", message_id="p2", thread_root_id=""),
        text="second", user_id="u1", username="roman", kind=KIND_DM, is_dm=True,
    )
    await flow.handle(m2, chat)

    assert runtime.profiles[1] is new_profile
    await store.close()


async def test_handle_batch_merges_messages_into_one_prompt_and_reply(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    flow, store = _flow(tmp_path, runtime)
    chat = FakeChat()

    m1 = _dm("first")
    m2 = IncomingMessage(
        ref=ConversationRef(channel_id="dm1", conversation_id="dm1", message_id="p2", thread_root_id=""),
        text="second", user_id="u1", username="roman", kind=KIND_DM, is_dm=True,
    )
    await flow.handle_batch([m1, m2], chat)

    # One turn, one reply; both messages present with attribution; reply anchored on the latest.
    assert len(runtime.calls) == 1
    prompt = runtime.calls[0][1]
    assert "[@roman]: first" in prompt and "[@roman]: second" in prompt
    assert len(chat.replies) == 1
    assert chat.replies[0][0].message_id == "p2"  # anchored on the newest
    await store.close()


# --- catch-up backfill (messages posted while the agent wasn't addressed) ------


from datetime import timedelta  # noqa: E402


def _at(offset_s: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=offset_s)


async def test_later_turn_backfills_messages_posted_since_last_reply(tmp_path: Path) -> None:
    # In a channel the agent only runs when mentioned, so whatever people said
    # in the thread between two mentions must be replayed on the next turn.
    runtime = FakeRuntime()
    flow, store = _flow(tmp_path, runtime)
    chat = FakeChat()
    chat.thread_posts["root1"] = [
        PostSnippet(message_id="root1", username="roman", text="thread root", timestamp=_at(-3600)),
    ]

    await flow.handle(_thread_msg(post="p2"), chat)  # first turn: full backfill

    # Colleagues keep talking without mentioning the agent, then someone pings it.
    chat.thread_posts["root1"] += [
        PostSnippet(message_id="pA", username="colleague", text="missed while away",
                    timestamp=_at(60), user_id="u2"),
        PostSnippet(message_id="p3", username="roman", text="a question in the thread",
                    timestamp=_at(120), user_id="u1"),
    ]
    await flow.handle(_thread_msg(post="p3"), chat)

    second = runtime.calls[1][1]
    assert "posted since your last reply" in second
    assert "missed while away" in second
    assert "thread root" not in second  # old history isn't replayed again
    assert second.count("a question in the thread") == 1  # current message not duplicated


async def test_catch_up_skips_the_agents_own_posts(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    flow, store = _flow(tmp_path, runtime)
    flow.set_identity("uid-bot")  # learned at gateway login
    chat = FakeChat()
    chat.thread_posts["root1"] = []

    await flow.handle(_thread_msg(post="p2"), chat)

    chat.thread_posts["root1"] = [
        PostSnippet(message_id="pBot", username="agent", text="my own earlier reply",
                    timestamp=_at(60), user_id="uid-bot"),
        PostSnippet(message_id="pHuman", username="colleague", text="human follow-up",
                    timestamp=_at(90), user_id="u2"),
    ]
    await flow.handle(_thread_msg(post="p3"), chat)

    second = runtime.calls[1][1]
    assert "human follow-up" in second
    assert "my own earlier reply" not in second  # already in the runtime session
    await store.close()


async def test_catch_up_ignores_snippets_without_a_timestamp(tmp_path: Path) -> None:
    # Undatable posts can't be placed against the cursor — replaying them every
    # turn would duplicate history endlessly.
    runtime = FakeRuntime()
    flow, store = _flow(tmp_path, runtime)
    chat = FakeChat()
    chat.thread_posts["root1"] = [
        PostSnippet(message_id="pX", username="colleague", text="undated line"),
    ]

    await flow.handle(_thread_msg(post="p2"), chat)
    await flow.handle(_thread_msg(post="p3"), chat)

    assert "undated line" in runtime.calls[0][1]  # first turn: full replay
    assert "undated line" not in runtime.calls[1][1]  # later turn: skipped
    await store.close()


async def test_dm_turns_have_no_catch_up_block(tmp_path: Path) -> None:
    # A DM delivers every message as a turn, so there is nothing to catch up on.
    runtime = FakeRuntime()
    flow, store = _flow(tmp_path, runtime)
    chat = FakeChat()

    await flow.handle(_dm("first"), chat)
    await flow.handle(
        IncomingMessage(
            ref=ConversationRef(channel_id="dm1", conversation_id="dm1", message_id="p2"),
            text="second", user_id="u1", username="roman", kind=KIND_DM, is_dm=True,
        ),
        chat,
    )

    assert "[context:" not in runtime.calls[1][1]
    await store.close()


async def test_channel_session_catches_up_too(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    flow, store = _flow(tmp_path, runtime)
    chat = FakeChat()
    chat.recent_posts["ch1"] = []

    def _channel_msg(post_id: str) -> IncomingMessage:
        return IncomingMessage(
            ref=ConversationRef(channel_id="ch1", conversation_id="ch1", message_id=post_id),
            text="question", user_id="u1", username="roman", kind=KIND_CHANNEL,
        )

    await flow.handle(_channel_msg("p1"), chat)
    chat.recent_posts["ch1"] = [
        PostSnippet(message_id="pB", username="other-bot", text="a bot notice",
                    timestamp=_at(60), user_id="u-bot"),
    ]
    await flow.handle(_channel_msg("p2"), chat)

    assert "a bot notice" in runtime.calls[1][1]
    await store.close()
