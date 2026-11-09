"""The trace exporter: the events of a turn as a tree of spans, named the way
OpenInference readers (Phoenix) expect, with ids derived from the turn's."""

import logging

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanContext, StatusCode

from crucible.ports.agent import TokenUsage
from crucible.ports.telemetry import (
    BudgetHit,
    CardAnswered,
    EngineState,
    McpCalled,
    ModelCalled,
    Telemetries,
    ToolCalled,
    TurnEnded,
)
from crucible.telemetry_otel import (
    LLM_MODEL_NAME,
    LLM_PROVIDER,
    LLM_TOKENS_COMPLETION,
    LLM_TOKENS_PROMPT,
    LLM_TOKENS_TOTAL,
    PROJECT_NAME,
    SESSION_ID,
    SPAN_KIND_AGENT,
    SPAN_KIND_ATTRIBUTE,
    SPAN_KIND_CHAIN,
    SPAN_KIND_LLM,
    SPAN_KIND_TOOL,
    TOOL_NAME,
    USER_ID,
    OtelTelemetry,
    trace_id,
    turn_span_id,
)


def _ctx(span: ReadableSpan) -> SpanContext:
    assert span.context is not None
    return span.context


def _parent(span: ReadableSpan) -> SpanContext:
    assert span.parent is not None
    return span.parent


def _times(span: ReadableSpan) -> tuple[int, int]:
    assert span.start_time is not None and span.end_time is not None
    return span.start_time, span.end_time


def _exporting() -> tuple[OtelTelemetry, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    return OtelTelemetry(exporter, service_name="impi-test", processor=SimpleSpanProcessor(exporter)), exporter


def _turn(turn_id: str, **over) -> TurnEnded:
    fields = dict(
        agent="assistant", gateway="slack", user_id="U1", conversation_id="c1", kind="thread", direct=False,
        synthetic=False, messages=1, attachments=0, outcome="replied", notice="", duration_s=2.5,
        model="openai/gpt-5.5", stop_reason="stop", tool_calls=("jira_search",), truncated=False,
        usage=TokenUsage(input=120, output=30, total=150, cost_usd=0.003),
        turn_id=turn_id, started_at="2026-11-09T10:00:00+00:00",
    )
    fields.update(over)
    return TurnEnded(**fields)  # type: ignore[arg-type]


def test_a_turns_events_become_one_tree_the_children_arriving_first() -> None:
    telemetry, exporter = _exporting()
    telemetry.record(ModelCalled(
        agent="assistant", turn_id="t1", conversation_id="c1", user_id="U1", gateway="slack", index=0,
        model="openai/gpt-5.5", usage=TokenUsage(input=100, output=10, total=110, cost_usd=0.002),
        stop_reason="toolUse", duration_s=0.8, started_at="2026-11-09T10:00:00+00:00",
    ))
    telemetry.record(ToolCalled(
        agent="assistant", tool="jira_search", conversation_id="c1", user_id="U1", gateway="slack", outcome="ok",
        duration_s=0.3, confirmed=False, turn_id="t1", started_at="2026-11-09T10:00:01+00:00",
    ))
    telemetry.record(CardAnswered(agent="assistant", tool="jira_create", outcome="allowed_once", wait_s=4.0, grant_s=0, turn_id="t1"))
    telemetry.record(McpCalled(server="gitlab", tool="list_mrs", agent="assistant", outcome="is_error", duration_s=0.2, file_bytes=0, turn_id="t1"))
    telemetry.record(ModelCalled(
        agent="assistant", turn_id="t1", conversation_id="c1", user_id="U1", gateway="slack", index=1,
        model="openai/gpt-5.5", usage=None, stop_reason="stop", duration_s=1.1, started_at="2026-11-09T10:00:01+00:00",
    ))
    telemetry.record(_turn("t1"))
    spans = exporter.get_finished_spans()
    assert [s.name for s in spans] == ["openai/gpt-5.5", "jira_search", "card: jira_create", "gitlab/list_mrs", "openai/gpt-5.5", "assistant: turn"]
    turn = spans[-1]
    # One trace, derived from the turn's id; the turn's span id too, so every child named it before it came.
    assert {_ctx(s).trace_id for s in spans} == {trace_id("t1")}
    assert _ctx(turn).span_id == turn_span_id("t1") and turn.parent is None
    assert all(_parent(s).span_id == _ctx(turn).span_id for s in spans[:-1])
    assert len({_ctx(s).span_id for s in spans}) == len(spans)  # each child an id of its own
    # Named as OpenInference reads them: an agent, its model calls, its tools.
    assert turn.attributes is not None and turn.attributes[SPAN_KIND_ATTRIBUTE] == SPAN_KIND_AGENT
    assert (turn.attributes[SESSION_ID], turn.attributes[USER_ID], turn.attributes[LLM_MODEL_NAME]) == ("c1", "U1", "openai/gpt-5.5")
    assert (turn.attributes[LLM_TOKENS_PROMPT], turn.attributes[LLM_TOKENS_COMPLETION], turn.attributes[LLM_TOKENS_TOTAL]) == (120, 30, 150)
    assert (turn.attributes["crucible.outcome"], turn.attributes["crucible.cost_usd"], turn.attributes["crucible.tool_calls"]) == ("replied", 0.003, ["jira_search"])
    first, tool, card, mcp, second = spans[:-1]
    assert first.attributes is not None and (first.attributes[SPAN_KIND_ATTRIBUTE], first.attributes[LLM_PROVIDER]) == (SPAN_KIND_LLM, "openai")
    assert (first.attributes[LLM_TOKENS_TOTAL], first.attributes["crucible.index"], first.attributes["crucible.stop_reason"]) == (110, 0, "toolUse")
    assert second.attributes is not None and LLM_TOKENS_TOTAL not in second.attributes and second.attributes["crucible.index"] == 1
    assert tool.attributes is not None and (tool.attributes[SPAN_KIND_ATTRIBUTE], tool.attributes[TOOL_NAME]) == (SPAN_KIND_TOOL, "jira_search")
    assert card.attributes is not None and (card.attributes[SPAN_KIND_ATTRIBUTE], card.attributes["crucible.outcome"]) == (SPAN_KIND_CHAIN, "allowed_once")
    assert mcp.attributes is not None and (mcp.attributes[TOOL_NAME], mcp.attributes["crucible.server"]) == ("gitlab/list_mrs", "gitlab")
    # Timing from the event: start as it said, end that much later; a card's wait counted back from now.
    assert _times(turn) == (1794218400_000_000_000, 1794218402_500_000_000)
    assert _times(tool) == (1794218401_000_000_000, 1794218401_300_000_000)
    card_start, card_end = _times(card)
    assert card_end - card_start == 4_000_000_000
    # How it went: ok where it went well, an error with the word otherwise.
    assert turn.status.status_code is StatusCode.OK and tool.status.status_code is StatusCode.OK
    assert mcp.status.status_code is StatusCode.ERROR and mcp.status.description == "is_error"
    assert turn.resource.attributes["service.name"] == "impi-test" == turn.resource.attributes[PROJECT_NAME]


def test_a_tasks_turn_sits_under_the_askers_in_the_askers_trace() -> None:
    telemetry, exporter = _exporting()
    # The task's events come first, while the asker still waits; each names the asker's turn.
    telemetry.record(ToolCalled(
        agent="jira", tool="jira_get", conversation_id="delegated:x", user_id="U1", gateway="trusted-delegation", outcome="denied",
        duration_s=0.1, confirmed=True, turn_id="task9", started_at="2026-11-09T10:00:02+00:00", parent_turn_id="t1",
    ))
    telemetry.record(_turn("task9", agent="jira", gateway="trusted-delegation", conversation_id="delegated:x", parent_turn_id="t1", outcome="error", notice="agent_error"))
    telemetry.record(_turn("t1"))
    tool, task, asker = exporter.get_finished_spans()
    assert {_ctx(s).trace_id for s in (tool, task, asker)} == {trace_id("t1")}  # one trace: the asker's
    assert _parent(task).span_id == turn_span_id("t1") and asker.parent is None
    assert _parent(tool).span_id == turn_span_id("task9") == _ctx(task).span_id
    assert task.status.status_code is StatusCode.ERROR and task.status.description == "agent_error"
    assert tool.status.status_code is StatusCode.ERROR and tool.status.description == "denied"


def test_what_is_not_a_step_of_a_turn_makes_no_span_and_a_bad_event_costs_nothing(caplog) -> None:
    telemetry, exporter = _exporting()
    telemetry.record(EngineState(agents=1, sessions_alive=1, sessions_busy=0, capacity=4, turns_waiting=0, http_turns_active=0))
    telemetry.record(BudgetHit(agent="assistant", user_id="U1", rule="tokens_per_day", spent=10, limit=5))
    telemetry.record(_turn(""))  # a turn with no id: nothing to hang spans on
    telemetry.record(ToolCalled(agent="assistant", tool="x", conversation_id="", user_id="", gateway="", outcome="unauthorized", duration_s=0.0, confirmed=False))
    assert exporter.get_finished_spans() == ()
    # A time the event could not say (an odd started_at) is now, not a crash.
    with caplog.at_level(logging.WARNING):
        telemetry.record(_turn("t2", started_at="not a time"))
    (span,) = exporter.get_finished_spans()
    begun, ended = _times(span)
    assert ended - begun == 2_500_000_000
    assert not [r for r in caplog.records if "could not be made a span" in r.getMessage()]


def test_ids_are_the_same_for_the_same_turn_everywhere() -> None:
    assert trace_id("t1") == trace_id("t1") != trace_id("t2") and 0 < trace_id("t1") < 2**128
    assert turn_span_id("t1") == turn_span_id("t1") != turn_span_id("t2") and 0 < turn_span_id("t1") < 2**64


def test_several_receivers_each_get_every_event_and_one_that_fails_costs_the_others_nothing(caplog) -> None:
    class _Keeps:
        def __init__(self) -> None:
            self.events: list = []

        def record(self, event) -> None:
            self.events.append(event)

    class _Broken:
        def record(self, event) -> None:
            raise RuntimeError("the collector is down")

    first, second = _Keeps(), _Keeps()
    fanned = Telemetries(first, _Broken(), second)
    event = _turn("t1")
    with caplog.at_level(logging.WARNING):
        fanned.record(event)
    assert first.events == [event] and second.events == [event]
    assert any("failed to record TurnEnded" in r.getMessage() for r in caplog.records)


def test_the_endpoint_constructor_builds_an_otlp_exporter() -> None:
    telemetry = OtelTelemetry.to_endpoint("http://phoenix:6006/v1/traces", headers={"api_key": "k"}, service_name="impi")
    exporter = telemetry._processor.span_exporter  # type: ignore[attr-defined]  # noqa: SLF001
    assert exporter._endpoint == "http://phoenix:6006/v1/traces" and exporter._client._headers["api_key"] == "k"  # noqa: SLF001
    telemetry.close()
