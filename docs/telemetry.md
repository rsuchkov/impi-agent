# Telemetry

The engine tells what it did, as events, for whoever wants to count: a turn
ended, a tool was called, a card was answered, an MCP server was called or
checked, a scheduled task ran, a person's connection moved, and on a timer how
the engine stands. Metrics — counts, rates, totals per person or per model —
are what a receiver derives; the engine sends the facts and is tied to no
exporter. `impi` writes them to the log; an application with a metrics system
of its own gives the engine a receiver instead.

## Events

Every event is a frozen record of flat fields: names, numbers, durations.
Never an object of the engine's, never the text people wrote. `user_id` is the
platform's id as the gateway gave it; a receiver that must not keep it hashes
or maps it on the way out.

| Event | When | Fields |
|---|---|---|
| `TurnEnded` | a turn of an agent ended | `agent`, `gateway`, `user_id`, `conversation_id` (the key), `kind`, `direct`, `synthetic`, `messages`, `attachments`, `outcome` (`replied`, `acted`, `empty`, `truncated`, `timeout`, `error`, `stopped`, `not_admitted`), `notice` (the code the person got, or `""`), `duration_s`, `model` (`provider/model` that answered — after a fallback, the one that did), `stop_reason`, `tool_calls`, `truncated`, `usage`, `turn_id`, `started_at` (ISO 8601 UTC), `parent_turn_id` (the asker's turn, on a turn another agent's turn opened with `delegate`/`ask_agent`; `""` otherwise) |
| `ModelCalled` | the model answered once within a turn — a turn that calls tools has several | `agent`, `turn_id`, `conversation_id`, `user_id`, `gateway`, `index` (the answer's number in the turn, from 0), `model`, `usage` (what that one answer cost, when the runtime reports it per answer; `None` otherwise), `stop_reason`, `duration_s`, `started_at` |
| `ToolCalled` | a call reached the tool server, however it ended | `agent`, `tool`, `conversation_id`, `user_id`, `gateway`, `outcome` (`ok`, `error`, `crashed`, `unauthorized`, `unknown_tool`, `not_allowed`, `no_session`, `not_in_conversation`, `not_through_gateway`, `unconfirmable`, `not_asked`, `cannot_run`, `denied`, `unanswered`, `no_approver`, `abandoned`), `duration_s`, `confirmed`, `turn_id` (`""` when the call named no known turn), `started_at` |
| `CardAnswered` | a confirmation for a tool call ended | `agent`, `tool`, `outcome` (`reused_grant`, `allowed_once`, `allowed_for`, `denied`, `unanswered`, `no_approver`, `abandoned`), `wait_s` (how long people took), `grant_s`, `turn_id` |
| `McpCalled` | the engine called an MCP server's tool | `server`, `tool`, `agent`, `outcome` (`ok`, `is_error`, `unreachable`, `unanswered`, `refused`, `token_refused`, `protocol`, `unsupported`, `no_file` (for a confirmed tool a stray path ends earlier, as `ToolCalled.cannot_run`), `file_gone`, `file_changed`, `file_type`, `not_connected`, `not_direct`, `no_provider`, `provider_unavailable`), `duration_s`, `file_bytes`, `turn_id` |
| `McpChecked` | a server's tools were compared with their snapshot | `server`, `result` — one word per server, the most specific: `restored`, `withdrawn`, `refreshed`, `unpinned`, `unreachable`, else `connected` |
| `TaskRan` | a scheduled occurrence finished | `agent`, `task`, `outcome` (the run's status word, as [tasks.md](tasks.md) lists them: `ok`, `empty`, `timeout`, `error`, `deadline`, `no_agent`, …), `duration_s` |
| `BudgetHit` | a turn was not admitted for the agent's budget (`runtime.budget`) | `agent`, `user_id` (`""` when nobody's), `rule` (the key hit: `tokens_per_day`, `usd_per_day`, `tokens_per_day_per_person`, `usd_per_day_per_person`), `spent`, `limit` (in the rule's unit) |
| `ConnectionChanged` | a person's connected account moved | `provider`, `event` (`connect_start`, `connect`, `refresh`, `forget`, `disconnect`, `grant`), `result` (`ok`, `unavailable`, `rejected`, `not_connected`, `revoke_failed`; on `connect` also the link's word: `expired`, `foreign`, `denied`, `account_refused`) |
| `EngineState` | every `TELEMETRY_STATE_S` seconds | `agents`, `sessions_alive`, `sessions_busy`, `capacity`, `turns_waiting`, `http_turns_active` |

`usage` on a turn is a `TokenUsage`: `input`, `output`, `cache_read`,
`cache_write`, `total` and `cost_usd` — the tokens the runtime reported for
every answer of the turn, summed, and the runtime's own price estimate (pi's
table), not a bill. `None` when the runtime reported none. Tokens per person
per model is `usage` grouped by `user_id` and `model`.

**Putting the events of one turn together.** Every turn has a `turn_id` —
the http caller's turn id where there is one, a task's id on a task's turn,
one the flow mints otherwise — and every event the turn produces names it:
its `ModelCalled`s, the `ToolCalled`s of its calls, the `CardAnswered`s and
`McpCalled`s of those calls, and its `TurnEnded` at the end, each with the
time it began (`started_at`) and its `duration_s`. A turn one agent's turn
opened for another (`delegate`, `ask_agent`) names that one as
`parent_turn_id`. A receiver that draws trees — spans in a tracing backend —
has what it needs: the turn is the root, its model calls and tool calls the
children, the asked agent's turn a child of the asker's. A receiver that
counts ignores both ids. The events of a turn arrive as they happen, the
children before the parent's `TurnEnded`.

## Where the events go

The port is `crucible.ports.telemetry.Telemetry`: one method,
`record(event)`, which must not raise and must not block — it runs on the
turn's own path, and a receiver that fails is logged, never felt. The engine's
producers each take a `Telemetry`: `AgentFlow(telemetry=)`,
`ToolWiring.build_server(telemetry=)`, `InteractionWiring(telemetry=)`,
`McpServers(telemetry=)`, `Scheduler(telemetry=)`, `OAuthConnections(telemetry=)`,
and `crucible.engine_state.tell_engine_state` for the timer. `impi` builds
one receiver and hands it to all of them.

`impi`'s receiver is `LogTelemetry`: one JSON line per event on the logger
`impi.telemetry`, `{"event": "turn_ended", "agent": …, "usage": {…}}` —
the event's name in the log's own case, then the fields. They are INFO
lines, so `LOG_LEVEL` has to be `INFO` or lower for them to appear. Off with
`TELEMETRY_LOG=false`. A log pipeline, a dashboard that tails the log or a
person with `grep '"event": "tool_called"'` gets the same facts.

An application with a receiver of its own — a metrics exporter, a table, a
stream — implements `record` and passes the instance where `impi` passes
`LogTelemetry()`. Nothing in the engine depends on what it does with the
events; a receiver that counts by `user_id` decides for itself whether to keep
the id, hash it or map it to a person (`PersonKey`). Several receivers are one
with `Telemetries(a, b)` (`crucible.ports.telemetry`): every event goes to
each, and one that fails costs the others nothing.

## Traces: Phoenix, Langfuse and the like

`crucible.telemetry_otel.OtelTelemetry` (the `otel` extra: `crucible[otel]`,
`impi[otel]`; in the engine image already) is a receiver that exports the
events of a turn as **spans** over OTLP/http — a tree a tracing backend
draws, named the way [OpenInference](https://github.com/Arize-ai/openinference)
readers such as Arize Phoenix expect:

```
AGENT  assistant: turn           session.id, user.id, crucible.outcome, llm.token_count.* (the sum), crucible.cost_usd
├─ LLM   openai/gpt-5.5          llm.model_name, llm.provider, llm.token_count.prompt/completion/total, crucible.stop_reason
├─ TOOL  jira_search             tool.name, crucible.outcome, crucible.confirmed
├─ CHAIN card: jira_create       the wait on the person, crucible.outcome (allowed_once, denied, …)
├─ LLM   openai/gpt-5.5
└─ AGENT jira: turn              a trusted agent's turn, under the asker's (parent_turn_id)
   └─ TOOL jira_get
```

The ids are derived from `turn_id` (the trace from the asker's turn, the
turn's span from its own), so every span is exported the moment its event
comes — children before the parent's `TurnEnded` — into the right trace, with
nothing remembered between events and the same ids on every replica. What
leaves is what the events carry: names, ids, numbers, outcome words; no text of
anybody's. Spans are batched and shipped in the background; `close()` flushes
what is left, and `impi` calls it at shutdown.

In `impi`, name the collector's traces URL and the exporter joins the log
receiver (or replaces it, with `TELEMETRY_LOG=false`):

```
TELEMETRY_OTLP_ENDPOINT=http://phoenix:6006/v1/traces
TELEMETRY_OTLP_HEADERS=api_key=…         # optional, name=value CSV, on every request
TELEMETRY_SERVICE_NAME=impi              # the service the spans belong to
```

Phoenix in five minutes, beside the engine: run `arizephoenix/phoenix` with
port `6006` published (its UI and its OTLP/http endpoint are the same port),
point `TELEMETRY_OTLP_ENDPOINT` at `http://<host>:6006/v1/traces`, restart the
engine, and talk to an agent — each turn is a trace, each tool a span, a
trusted agent's turn a branch under the asker's, all in the project named by
`TELEMETRY_SERVICE_NAME` (Phoenix files spans by the resource attribute
`openinference.project.name`, which the exporter sets to the same name).
Recent Phoenix images ask for a token: either run the container with
`PHOENIX_ENABLE_AUTH=false` for a local try, or make an API key in its UI and
pass it as `TELEMETRY_OTLP_HEADERS=authorization=Bearer <key>`; a `401` from
the collector is logged by the exporter and the batch is dropped. The exporter is the engine's
process, not an agent's: a confined agent's network ([security.md](security.md))
needs no port opened for it. An application with the port's receiver of its
own gets the same from `OtelTelemetry.to_endpoint(url, headers=…,
service_name=…)` or `OtelTelemetry(exporter)` with any `SpanExporter`.

## Settings

| Variable | Default | Purpose |
|---|---|---|
| `TELEMETRY_LOG` | `true` | the events as JSON lines on logger `impi.telemetry` |
| `TELEMETRY_STATE_S` | `60` | how often `EngineState` is told; `0` never |
| `TELEMETRY_OTLP_ENDPOINT` | `` | an OTLP/http collector's traces URL (`…/v1/traces`); the turns' events go there as spans; empty = none |
| `TELEMETRY_OTLP_HEADERS` | `` | headers on every request to it, `name=value` CSV (an API key) |
| `TELEMETRY_SERVICE_NAME` | `impi` | the service the spans belong to (`service.name`, and the Phoenix project) |
