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
| `TurnEnded` | a turn of an agent ended | `agent`, `gateway`, `user_id`, `conversation_id` (the key), `kind`, `direct`, `synthetic`, `messages`, `attachments`, `outcome` (`replied`, `acted`, `empty`, `truncated`, `timeout`, `error`, `stopped`), `notice` (the code the person got, or `""`), `duration_s`, `model` (`provider/model` that answered — after a fallback, the one that did), `stop_reason`, `tool_calls`, `truncated`, `usage` |
| `ToolCalled` | a call reached the tool server, however it ended | `agent`, `tool`, `conversation_id`, `user_id`, `gateway`, `outcome` (`ok`, `error`, `crashed`, `unauthorized`, `unknown_tool`, `not_allowed`, `no_session`, `not_in_conversation`, `not_through_gateway`, `unconfirmable`, `not_asked`, `denied`, `unanswered`, `no_approver`, `abandoned`), `duration_s`, `confirmed` |
| `CardAnswered` | a confirmation for a tool call ended | `agent`, `tool`, `outcome` (`reused_grant`, `allowed_once`, `allowed_for`, `denied`, `unanswered`, `no_approver`, `abandoned`), `wait_s` (how long people took), `grant_s` |
| `McpCalled` | the engine called an MCP server's tool | `server`, `tool`, `agent`, `outcome` (`ok`, `is_error`, `unreachable`, `unanswered`, `refused`, `token_refused`, `protocol`, `unsupported`, `no_file`, `file_gone`, `file_changed`, `file_type`, `not_connected`, `not_direct`, `no_provider`, `provider_unavailable`), `duration_s`, `file_bytes` |
| `McpChecked` | a server's tools were compared with their snapshot | `server`, `result` — one word per server, the most specific: `restored`, `withdrawn`, `refreshed`, `unpinned`, `unreachable`, else `connected` |
| `TaskRan` | a scheduled occurrence finished | `agent`, `task`, `outcome` (the run's status word, as [tasks.md](tasks.md) lists them: `ok`, `empty`, `timeout`, `error`, `deadline`, `no_agent`, …), `duration_s` |
| `ConnectionChanged` | a person's connected account moved | `provider`, `event` (`connect_start`, `connect`, `refresh`, `forget`, `disconnect`, `grant`), `result` (`ok`, `unavailable`, `rejected`, `not_connected`, `revoke_failed`; on `connect` also the link's word: `expired`, `foreign`, `denied`, `account_refused`) |
| `EngineState` | every `TELEMETRY_STATE_S` seconds | `agents`, `sessions_alive`, `sessions_busy`, `capacity`, `turns_waiting`, `http_turns_active` |

`usage` on a turn is a `TokenUsage`: `input`, `output`, `cache_read`,
`cache_write`, `total` and `cost_usd` — the tokens the runtime reported for
every answer of the turn, summed, and the runtime's own price estimate (pi's
table), not a bill. `None` when the runtime reported none. Tokens per person
per model is `usage` grouped by `user_id` and `model`.

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
the id, hash it or map it to a person (`PersonKey`).

## Settings

| Variable | Default | Purpose |
|---|---|---|
| `TELEMETRY_LOG` | `true` | the events as JSON lines on logger `impi.telemetry` |
| `TELEMETRY_STATE_S` | `60` | how often `EngineState` is told; `0` never |
