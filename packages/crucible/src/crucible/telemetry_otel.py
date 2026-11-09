"""The engine's telemetry as traces, over OTLP (``crucible[otel]``).

A ``Telemetry`` receiver that turns the events of a turn into spans a tracing
backend draws as a tree — Arize Phoenix, Langfuse, Jaeger, anything that
takes OTLP — and names them the way OpenInference readers expect: the turn is
an ``AGENT`` span, each answer of the model an ``LLM`` span under it with its
token counts, each tool call a ``TOOL`` span, a card a ``CHAIN`` span for the
wait, and a turn another agent's turn opened sits under the asker's.

The ids are derived, not remembered: a turn's trace id and span id come from
its ``turn_id`` (and a task's turn joins the trace of the asker's turn it names
as ``parent_turn_id``), so a child arriving before its parent's ``TurnEnded``
— as they all do — is exported at once, into the right trace, with nothing
kept here between events. Each span is built finished and handed to the
processor, which batches and ships them; ``close`` flushes what is left.

What leaves is what the events carry (``ports.telemetry``): names, ids,
numbers and outcome words — and, where the producers were told to carry
content (``CONTENT_TEXT``), the turn's input and answer and a tool call's
arguments, as ``input.value``/``output.value``.
"""

import hashlib
import logging
import secrets
from collections.abc import Mapping
from datetime import datetime, timezone

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.sdk.util.instrumentation import InstrumentationScope
from opentelemetry.trace import SpanContext, SpanKind, Status, StatusCode, TraceFlags

from crucible.ports.telemetry import (
    CardAnswered,
    McpCalled,
    ModelCalled,
    TelemetryEvent,
    ToolCalled,
    TurnEnded,
)

logger = logging.getLogger(__name__)

# OpenInference: what kind of step a span is, as Phoenix and its kin read it.
SPAN_KIND_ATTRIBUTE = "openinference.span.kind"
SPAN_KIND_AGENT = "AGENT"
SPAN_KIND_LLM = "LLM"
SPAN_KIND_TOOL = "TOOL"
SPAN_KIND_CHAIN = "CHAIN"
# OpenInference attribute names the backends know; the engine's own facts go
# under ``crucible.``.
SESSION_ID = "session.id"
USER_ID = "user.id"
LLM_MODEL_NAME = "llm.model_name"
LLM_PROVIDER = "llm.provider"
LLM_TOKENS_PROMPT = "llm.token_count.prompt"
LLM_TOKENS_COMPLETION = "llm.token_count.completion"
LLM_TOKENS_TOTAL = "llm.token_count.total"
TOOL_NAME = "tool.name"
INPUT_VALUE = "input.value"
OUTPUT_VALUE = "output.value"
INPUT_MIME_TYPE = "input.mime_type"
PROJECT_NAME = "openinference.project.name"  # a resource attribute: the project Phoenix files the spans under
OWN = "crucible."

_SAMPLED = TraceFlags(TraceFlags.SAMPLED)
_SCOPE = InstrumentationScope("crucible")


class OtelTelemetry:
    """``Telemetry`` that exports spans. ``exporter`` is where they go (an
    ``OTLPSpanExporter`` — see ``to_endpoint`` — or any ``SpanExporter``);
    ``service_name`` is the resource the spans belong to; ``processor``, when
    given, replaces the batching one (a test's ``SimpleSpanProcessor``)."""

    def __init__(
        self, exporter: SpanExporter, *, service_name: str = "crucible", processor: SpanProcessor | None = None,
    ) -> None:
        # Phoenix files spans by ``openinference.project.name``; everyone else
        # reads ``service.name`` — the one name, under both.
        self._resource = Resource.create({"service.name": service_name, PROJECT_NAME: service_name})
        self._processor: SpanProcessor = processor if processor is not None else BatchSpanProcessor(exporter)

    @classmethod
    def to_endpoint(
        cls, endpoint: str, *, headers: Mapping[str, str] | None = None, service_name: str = "crucible",
    ) -> "OtelTelemetry":
        """Spans to an OTLP/http collector at ``endpoint`` (the traces URL,
        ``…/v1/traces``), with ``headers`` on every request (an API key)."""
        return cls(OTLPSpanExporter(endpoint=endpoint, headers=dict(headers or {})), service_name=service_name)

    def record(self, event: TelemetryEvent) -> None:
        try:
            span = self._span_of(event)
        except Exception:  # noqa: BLE001 - telemetry must never cost the turn
            logger.warning("a telemetry event could not be made a span", exc_info=True)
            return
        if span is not None:
            self._processor.on_end(span)

    def close(self) -> None:
        """Flush what is queued and stop. Blocking: call it off the loop."""
        self._processor.shutdown()

    # -- spans ----------------------------------------------------------------

    def _span_of(self, event: TelemetryEvent) -> ReadableSpan | None:
        if isinstance(event, TurnEnded):
            return self._turn(event)
        if isinstance(event, ModelCalled):
            return self._model(event)
        if isinstance(event, ToolCalled):
            return self._tool(event)
        if isinstance(event, CardAnswered):
            return self._card(event)
        if isinstance(event, McpCalled):
            return self._mcp(event)
        return None  # a gauge or a count, not a step of a turn

    def _turn(self, event: TurnEnded) -> ReadableSpan | None:
        if not event.turn_id:
            return None
        trace = trace_id(event.parent_turn_id or event.turn_id)
        parent = SpanContext(trace, turn_span_id(event.parent_turn_id), is_remote=False, trace_flags=_SAMPLED) if event.parent_turn_id else None
        attributes: dict[str, object] = {
            SPAN_KIND_ATTRIBUTE: SPAN_KIND_AGENT, SESSION_ID: event.conversation_id, USER_ID: event.user_id,
            **_own(
                agent=event.agent, gateway=event.gateway, kind=event.kind, direct=event.direct, synthetic=event.synthetic,
                messages=event.messages, attachments=event.attachments, outcome=event.outcome, notice=event.notice,
                stop_reason=event.stop_reason, truncated=event.truncated, tool_calls=list(event.tool_calls),
            ),
        }
        if event.model:
            attributes[LLM_MODEL_NAME] = event.model
        attributes.update(_usage(event.usage))
        if event.input_text:
            attributes[INPUT_VALUE] = event.input_text
        if event.output_text:
            attributes[OUTPUT_VALUE] = event.output_text
        failed = event.outcome in ("error", "timeout")
        return self._finished(
            f"{event.agent}: turn", trace, turn_span_id(event.turn_id), parent, attributes,
            started_at=event.started_at, duration_s=event.duration_s,
            status=Status(StatusCode.ERROR, event.notice or event.outcome) if failed else Status(StatusCode.OK),
        )

    def _model(self, event: ModelCalled) -> ReadableSpan | None:
        if not event.turn_id:
            return None
        attributes: dict[str, object] = {
            SPAN_KIND_ATTRIBUTE: SPAN_KIND_LLM, SESSION_ID: event.conversation_id, USER_ID: event.user_id,
            **_own(agent=event.agent, gateway=event.gateway, index=event.index, stop_reason=event.stop_reason),
        }
        if event.model:
            attributes[LLM_MODEL_NAME] = event.model
            provider, _, _name = event.model.partition("/")
            if provider:
                attributes[LLM_PROVIDER] = provider
        attributes.update(_usage(event.usage))
        return self._child(event.model or "model", event.turn_id, event.parent_turn_id, attributes, event.started_at, event.duration_s)

    def _tool(self, event: ToolCalled) -> ReadableSpan | None:
        if not event.turn_id:
            return None
        attributes: dict[str, object] = {
            SPAN_KIND_ATTRIBUTE: SPAN_KIND_TOOL, TOOL_NAME: event.tool, SESSION_ID: event.conversation_id, USER_ID: event.user_id,
            **_own(agent=event.agent, gateway=event.gateway, outcome=event.outcome, confirmed=event.confirmed),
        }
        if event.args:
            attributes[INPUT_VALUE] = event.args
            attributes[INPUT_MIME_TYPE] = "application/json"
        status = Status(StatusCode.OK) if event.outcome == "ok" else Status(StatusCode.ERROR, event.outcome)
        return self._child(event.tool, event.turn_id, event.parent_turn_id, attributes, event.started_at, event.duration_s, status=status)

    def _card(self, event: CardAnswered) -> ReadableSpan | None:
        if not event.turn_id:
            return None
        attributes = {
            SPAN_KIND_ATTRIBUTE: SPAN_KIND_CHAIN, TOOL_NAME: event.tool,
            **_own(agent=event.agent, event="card_answered", outcome=event.outcome, grant_s=event.grant_s),
        }
        return self._child(f"card: {event.tool}", event.turn_id, event.parent_turn_id, attributes, "", event.wait_s)

    def _mcp(self, event: McpCalled) -> ReadableSpan | None:
        if not event.turn_id:
            return None
        attributes = {
            SPAN_KIND_ATTRIBUTE: SPAN_KIND_TOOL, TOOL_NAME: f"{event.server}/{event.tool}",
            **_own(agent=event.agent, event="mcp_called", server=event.server, outcome=event.outcome, file_bytes=event.file_bytes),
        }
        status = Status(StatusCode.OK) if event.outcome == "ok" else Status(StatusCode.ERROR, event.outcome)
        return self._child(f"{event.server}/{event.tool}", event.turn_id, event.parent_turn_id, attributes, "", event.duration_s, status=status)

    def _child(
        self, name: str, turn_id: str, parent_turn_id: str, attributes: Mapping[str, object],
        started_at: str, duration_s: float, *, status: Status | None = None,
    ) -> ReadableSpan:
        """A step of the turn: in the turn's trace (the asker's, for a task's
        turn), under the turn's span, with an id of its own nobody refers to."""
        trace = trace_id(parent_turn_id or turn_id)
        parent = SpanContext(trace, turn_span_id(turn_id), is_remote=False, trace_flags=_SAMPLED)
        return self._finished(
            name, trace, _fresh_span_id(), parent, attributes, started_at=started_at, duration_s=duration_s,
            status=status if status is not None else Status(StatusCode.OK),
        )

    def _finished(
        self, name: str, trace: int, span: int, parent: SpanContext | None, attributes: Mapping[str, object],
        *, started_at: str, duration_s: float, status: Status,
    ) -> ReadableSpan:
        end_ns = _ns(started_at, duration_s)
        start_ns = end_ns - int(max(duration_s, 0.0) * 1_000_000_000)
        return ReadableSpan(
            name, context=SpanContext(trace, span, is_remote=False, trace_flags=_SAMPLED), parent=parent,
            resource=self._resource, attributes=dict(attributes),  # type: ignore[arg-type]
            kind=SpanKind.INTERNAL, status=status, start_time=start_ns, end_time=end_ns, instrumentation_scope=_SCOPE,
        )


def trace_id(turn_id: str) -> int:
    """The trace a turn's spans share, from its id: the same id, the same trace,
    on every replica and on every event, with nothing looked up."""
    return int.from_bytes(hashlib.sha256(b"trace:" + turn_id.encode()).digest()[:16], "big") or 1


def turn_span_id(turn_id: str) -> int:
    """The id of a turn's own span, from its id, so a child can name it before
    the turn's ``TurnEnded`` has come."""
    return int.from_bytes(hashlib.sha256(b"span:" + turn_id.encode()).digest()[:8], "big") or 1


def _fresh_span_id() -> int:
    return secrets.randbits(64) or 1


def _ns(started_at: str, duration_s: float) -> int:
    """When the span ended, in nanoseconds since the epoch: its start plus its
    duration when the event says when it began, else now."""
    if started_at:
        try:
            begun = datetime.fromisoformat(started_at)
            if begun.tzinfo is None:
                begun = begun.replace(tzinfo=timezone.utc)
            return int(begun.timestamp() * 1_000_000_000) + int(max(duration_s, 0.0) * 1_000_000_000)
        except ValueError:
            pass
    return int(datetime.now(timezone.utc).timestamp() * 1_000_000_000)


def _usage(usage) -> dict[str, object]:
    if usage is None:
        return {}
    return {
        LLM_TOKENS_PROMPT: usage.input, LLM_TOKENS_COMPLETION: usage.output, LLM_TOKENS_TOTAL: usage.total,
        **_own(cache_read=usage.cache_read, cache_write=usage.cache_write, cost_usd=usage.cost_usd),
    }


def _own(**facts: object) -> dict[str, object]:
    return {f"{OWN}{key}": value for key, value in facts.items()}
