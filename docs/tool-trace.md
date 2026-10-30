# The tool trace

When an agent's turn calls tools, a progress message appears for as long as
the turn runs. In a thread it sits in the thread, above the reply, because it
appears before the reply exists. Anywhere else — a direct message, a channel
the agent answers in — it goes into a thread under the message the turn
answers, so the conversation itself holds the messages and the replies and
nothing else; a turn with no real message to hang it on (a click, a slash
command, a scheduled run) keeps it beside the reply:

```
⚙️ Running 2 tools… · Reading PROJ-123     ← while the turn runs; the count grows
✅ Searching for release notes
⏳ Reading PROJ-123
⚙️ Running 2 tools… · waiting for you: Commenting on PROJ-123   ← a confirmation card is up
✅ Searching for release notes
⏳ Commenting on PROJ-123 — waiting for you
```

Under the line, the steps so far, each in its state. On Slack they are the
platform's own **plan block** — the line as its title, a task card per call,
ticked, running or failed, "waiting for you" as a task's details — the view
a bot in the Agents panel has of its work; on Mattermost, the lines above.

After the count comes the latest word on what the turn is doing: the
**caption** of the call that just began ("Reading PROJ-123"), written by the
tool itself — a file the person sent is named as they named it, whatever the
disk calls it — or the model's last **remark on the way** — the line it wrote
before going on to call tools ("Found the workflow, now listing transitions").
A remark is shown while the turn runs and not kept — the record is the calls;
the http journal carries it as a `progress` event. The reply is what the model
says after its last call; a turn that says nothing after its last call answers
with its last remark.

When the turn ends, the progress message is **deleted** and the reply stands
alone — that is the default, `TOOL_TRACE_AFTER=nothing`. With
`TOOL_TRACE_AFTER=button` it settles into a button instead, and the reply
follows:

```
[ Running 3 tools · 1 failed → ]
```

A click redraws that same message into the list of calls. One row each — its
number, the tool, how it ended, how long it took, its caption when it had one —
and one row at a time can be opened to show its arguments, pretty-printed,
right under it. `Close` puts the button back. A turn that called no tool
leaves no message at all.

```
⚙️ 3 tool calls · 4.2s · 1 failed
▸ 1 · search_docs · ok · 0.4s · Searching for release notes
▸ 2 · read · ok · 1.1s · Reading CHANGELOG.md
▾ 3 · bash · failed · 2.7s
```json
{
  "command": "make test"
}
```
[1] [2] [3] [✕ Close]
```

Opening the panel expands the first failed call, since that is usually why it
is being opened. More than ten calls are paged.

## What it is for

- **A pause reads as work.** Without it, an agent that went into a two-minute
  tool call looks like an agent that stopped answering.
- **The answer can be checked.** Which files it read, what it searched for,
  what it ran — the reply's basis, after the fact.
- **Waste is visible.** Five searches in a row for one question, or a tool the
  task never needed.

## What it shows, and what it never shows

**Arguments and captions, never results.** The engine never holds a value an agent was
granted: a secret travels by reference (`vault://name`) and is bound into a
process the model does not read, so arguments cannot carry one. A *result*
could — `echo $TOKEN` after a granted reference is a legal command — and this
widget is not a place a value may surface. The ledger, the tool-gate card and
this panel all follow the same rule: what was asked, never what came back. See
[secrets.md](secrets.md).

Arguments are kept in the engine's inventory as compact JSON, cut at 8000
characters, and shown up to 1200 with a visible mark when cut. The panel is one
message and stays under both platforms' size limits by construction.

**A caption is the tool's line, not the arguments.** A tool that implements
`caption(args) -> str | None` (`Captioning` in `crucible.tools`) names the thing
a call is about — a ticket key, a host, a file's name — and that line goes on
the counter and into the row; an argument is as often a whole script or a whole
comment as a key, and is never shown there. The runtime's own built-ins are
captioned by the runtime driver (`read` → "Reading SYSTEM.md"; `bash` gets
none, since its argument is the command). The model's remarks on the way are
its own words to the person, made one line of bounded length; its reasoning
never appears.

Whoever can see the reply can open the panel. It is the same audience that
sees the tool-gate card, and the same audience the agent's own reply addresses.

## How it works

It is a **screen** — the same mechanism as `/skills` and `/tasks`: the engine
posts the message, the engine answers every click by rewriting it, and no model
is involved. The click carries only a token and a position; the calls
themselves are written to the inventory once, when the turn ends (with the
button; a message that is deleted keeps no record of its own), so the panel
opens minutes or a restart later. Traces older than the retention are dropped
at startup. The record (`ports.traces.TraceRecord`) is the same whatever shows
the turn: a turn an http caller started writes one too, and its journal's
`turn.finished` names the token (`GET /v1/traces/{token}`,
[http-gateway.md](http-gateway.md)), so a page opened after the journal is
gone still has the steps.

The live counter comes from the runtime's event stream. The runtime driver
translates its own tool events into three neutral ones — a call started, with
its arguments; a call finished, with its outcome and duration; the model's
remark before its next call (`TextProgress`) — the flow captions the started
call once (`CallCaptions`) and feeds them to the trace without waiting on the chat platform: a burst of calls costs
one redraw a second, not one per call. One event is the engine's own: when the
tool gate puts a call to the person, it tells the turn (`CallAwaiting`, through
the turn registry the flow bound the trace to), and the counter says "waiting
for you" with that call's caption until the call finishes — allowed and run,
or refused.

## Turning it on and off

| Variable | Default | Purpose |
|---|---|---|
| `TOOL_TRACE_ENABLED` | `true` | draw the widget for turns that ran tools |
| `TOOL_TRACE_AFTER` | `nothing` | what becomes of it when the turn ends: `nothing` — deleted, the reply stands alone; `button` — the button that opens the list of calls |
| `TOOL_TRACE_RETENTION_DAYS` | `14` | drop traces older than this at startup; `0` keeps them all |

It needs interactivity (`INTEGRATIONS_ENABLED`, on by default): the button has
to route its click somewhere. With interactivity off no widget is drawn rather
than a dead one. A Slack install needs no callback receiver for this — clicks
arrive on its socket.

Turning the widget off does not unregister its panel: a button posted before
the switch still opens.

## What it does not cover

- **An engine that dies mid-turn** leaves the counter as it was — not deleted,
  and with no button behind it. The turn never ended.
- **Memoryless scheduled runs** (`prompt` mode) do not go through the
  conversation flow and get no widget. Tasks in `turn` mode do.
- **The runtime's own cost** — starting it, waiting for the model — is not a
  tool call and does not appear.
