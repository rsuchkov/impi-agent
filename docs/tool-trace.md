# The tool trace

When an agent's turn calls tools, one extra message appears — above the reply,
because it appears before the reply exists:

```
⚙️ Running 2 tools…          ← while the turn runs; the count grows
```

When the turn ends it settles into a button, and the reply lands under it:

```
[ Running 3 tools · 1 failed → ]
```

A click redraws that same message into the list of calls. One row each — its
number, the tool, how it ended, how long it took — and one row at a time can be
opened to show its arguments, pretty-printed, right under it. `Close` puts the
button back. A turn that called no tool leaves no message at all.

```
⚙️ 3 tool calls · 4.2s · 1 failed
▸ 1 · search_docs · ok · 0.4s
▸ 2 · read · ok · 1.1s
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

**Arguments only, never results.** The engine never holds a value an agent was
granted: a secret travels by reference (`vault://name`) and is bound into a
process the model does not read, so arguments cannot carry one. A *result*
could — `echo $TOKEN` after a granted reference is a legal command — and this
widget is not a place a value may surface. The ledger, the tool-gate card and
this panel all follow the same rule: what was asked, never what came back. See
[secrets.md](secrets.md).

Arguments are kept in the engine's inventory as compact JSON, cut at 8000
characters, and shown up to 1200 with a visible mark when cut. The panel is one
message and stays under both platforms' size limits by construction.

Whoever can see the reply can open the panel. It is the same audience that
sees the tool-gate card, and the same audience the agent's own reply addresses.

## How it works

It is a **screen** — the same mechanism as `/skills` and `/tasks`: the engine
posts the message, the engine answers every click by rewriting it, and no model
is involved. The click carries only a token and a position; the calls
themselves are written to the inventory once, when the turn ends, so the panel
opens minutes or a restart later. Traces older than the retention are dropped
at startup.

The live counter comes from the runtime's event stream. The runtime driver
translates its own tool events into two neutral ones — a call started, with its
arguments; a call finished, with its outcome and duration — and the flow feeds
them to the trace without waiting on the chat platform: a burst of calls costs
one redraw a second, not one per call.

## Turning it on and off

| Variable | Default | Purpose |
|---|---|---|
| `TOOL_TRACE_ENABLED` | `true` | draw the widget under replies |
| `TOOL_TRACE_RETENTION_DAYS` | `14` | drop traces older than this at startup; `0` keeps them all |

It needs interactivity (`INTEGRATIONS_ENABLED`, on by default): the button has
to route its click somewhere. With interactivity off no widget is drawn rather
than a dead one. A Slack install needs no callback receiver for this — clicks
arrive on its socket.

Turning the widget off does not unregister its panel: a button posted before
the switch still opens.

## What it does not cover

- **An engine that dies mid-turn** leaves the counter as it was, with no button
  behind it. The trace is written when the turn ends, and that turn never did.
- **Memoryless scheduled runs** (`prompt` mode) do not go through the
  conversation flow and get no widget. Tasks in `turn` mode do.
- **The runtime's own cost** — starting it, waiting for the model — is not a
  tool call and does not appear.
