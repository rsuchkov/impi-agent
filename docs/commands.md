# Commands (slash commands, shortcuts & reactions)

A **command** is a way to ask an agent for something without writing a message
to it: the user types `/summarize` in a Mattermost thread, picks a shortcut on
a Slack message, or puts an emoji the operator mapped to the command on a
message, and the agent runs a turn and answers in that conversation.

> **The answer is public.** A command's reply is posted into the thread like any
> other, visible to everyone in it. If the result must reach only the person who
> invoked it — or must be produced deterministically rather than left to the
> model — don't route it through an agent turn: see [a handler beside the
> agent](#private-deterministic-results-a-handler-beside-the-agent) below.

## How it works

A command is not a special execution path. The gateway normalizes the platform's
payload and hands it to the engine, which turns it into a **synthetic message**
in the conversation the command was invoked from, and runs an ordinary turn. The
agent therefore gets everything it normally has: the thread's session, replayed
history, its tools — and its answer is posted the usual way.

One thing makes a command different from a message: **the conversation comes
from the payload**, not from where a message was typed — a command invoked in a
thread runs in that thread's session.

**What the command means is up to the agent**, not the engine: the text
(`/summarize the last hour`) arrives as the message, and the agent's `.pi/SYSTEM.md`
says what to do with it. One mechanism, any number of commands.

## Panels, and asking for one in plain language

A few words are answered by the engine itself rather than by an agent — the skill
browser, the task browser, and `/connections` where people connect their own
accounts ([connections.md](connections.md); a direct message only). Those are **panels**: the engine renders the view
and answers every click on it by rewriting the same message, with no turn behind
it (see [skills.md](skills.md) and [tasks.md](tasks.md)). One more word is the
engine's without a panel: the stop word (`/stop`, `STOP_COMMAND`), which ends
the agent's turn ([below](#stopping-a-turn)).

A slash command is one way to open one. The other is the **`open_screen` tool**:
give it to an agent and "show me which skills there are" opens the same panel that
`/skills` would. The model's only part is deciding to open it — what the panel
says, and everything that happens when a button is pressed, stays engine-only.
So the cost of the natural-language route is one turn at the door, not a model
between the user and the data.

Two consequences worth knowing. The panel appears wherever the agent is,
including a shared channel — except one that shows a person's own things, such
as `/connections`, which opens only in a direct message with the agent — and
**anyone who can see it can press its buttons**: the engine does not check who
clicks (a panel that acts on the clicker's own things acts on the clicker's,
whoever opened it). And because a tool works over any gateway,
this is the only way to reach a panel from Slack, where a custom slash command
cannot run inside a thread at all.

Grant it like any other tool (`open_screen` in `runtime.tools`); it needs
`INTEGRATIONS_ENABLED=true`.

## Platform differences

|  | Mattermost | Slack |
|---|---|---|
| Entry point | slash command (works inside threads); a **reaction** | slash command outside threads; **message shortcut** (`crux_*`) inside them; a **reaction** |
| Slash command in a thread | ✅ | ❌ [not allowed for custom commands](https://docs.slack.dev/interactivity/implementing-slash-commands/) — only built-ins and Giphy |
| Thread root comes from | `root_id` in the payload; for a reaction, the `root_id` of the post read back | `message.thread_ts` (else `message.ts`); for a reaction, the `thread_ts` of the message read back |
| Transport | HTTP `POST /command/{agent}` (or `/command/default`) on the interactions receiver; a reaction arrives on the bot's WebSocket | Socket Mode (no HTTP, no public URL) |
| Verification | per-command token, checked against config | the socket is already authenticated; a command exists only if the app declares it |

## Setting up Mattermost

1. **Create the command.** System Console → Integrations → Slash Commands (or
   `mmctl command create <team> --title Summarize --trigger-word summarize
   --url <URL> --creator <user> --post`), where `<URL>` is
   `<INTEGRATIONS_PUBLIC_URL>/<INTEGRATIONS_SECRET>/command/<agent>` — for
   example `http://192.168.1.10:8423/k2Xy8qLmN0pRsTuVwZ/command/assistant`.
   The **path names the agent**: a Mattermost payload doesn't say which bot it
   is meant for. The **secret segment** is what says the request came from
   Mattermost at all: a callback carries no signature, so the receiver answers
   nothing off that path. The engine mints `INTEGRATIONS_SECRET` into
   `conf/.env` on its first start (the installer does too); `impi doctor`
   prints the full URL to register. Request method must be **POST**.

   Or point it at **`/<secret>/command/default`** and let the engine choose: with one
   agent running that is the one, otherwise it is `AGENT_NAME`. The startup log
   prints what `default` resolved to. If several agents run and `AGENT_NAME` is
   not one of them, the command is refused rather than given to a guess — set
   `AGENT_NAME`, or name the agent in the URL. An agent really called `default`
   keeps its own endpoint.
2. **Copy the token** Mattermost generates and put it in the engine `.env`,
   keyed by the agent's name (upper-cased, `-`→`_`):
   ```
   AGENTS_COMMAND_TOKENS__ASSISTANT=<token>
   ```
   For the default agent the unsuffixed `COMMAND_TOKENS=<token>` works too — the
   same fallback `MATTERMOST_TOKEN` gets — so a single-agent deployment never
   spells its agent's name at all. The per-agent key wins where both are set.

   Several commands for one agent = several tokens, comma-separated. An agent
   with no tokens configured refuses every command (HTTP 403) — together with
   the secret path, that is what stands between the receiver's port and running
   a turn as your agent.
   `/command/default` is no exception: resolving the agent is not authorising
   the command.
3. **Networking**: the receiver is the same one that serves widget callbacks, so
   `INTEGRATIONS_PUBLIC_URL` must be reachable from Mattermost and its subnet
   must be in `AllowedUntrustedInternalConnections` (see
   [troubleshooting.md](troubleshooting.md)).
4. Restart the engine (config is read at startup).

**Changing or adding the secret** changes every URL: re-register each slash
command, and expect the buttons on cards posted before the change to answer
403 (the log says `not on the receiver's secret path`). Rotating it is
therefore a deliberate act — edit `INTEGRATIONS_SECRET`, restart, re-register.

## Setting up Slack

Slack **does not allow custom slash commands inside threads** — that is a
platform rule, not a setting. Use a **message shortcut**, which carries the
message and its thread:

1. api.slack.com → your app → **Interactivity & Shortcuts** (Interactivity must
   be On) → *Create New Shortcut* → **On messages**.
2. Name it (`Summarize thread`), and set the **Callback ID** to the command
   prefix + command name, e.g. **`crux_summarize`**. The engine binds one handler
   to the whole prefixed family and takes the command name from the suffix. The
   prefix is `SLACK_COMMAND_PREFIX` (default `crux_`) — change it to match your
   workspace's naming, or set it empty to treat every shortcut as a command
   (then the callback id *is* the command name).
3. Save (reinstall the app if Slack asks). No URL is needed — Socket Mode
   delivers it.

Users invoke it from the message's **“More actions” (…) menu** inside the
thread.

Outside a thread — in a channel, or in a direct message with the bot — a
**slash command** works as on Mattermost: declare it in the app (*Slash
Commands* → *Create New Command*, any name, no request URL — Socket Mode
delivers it), and the engine answers `/skills`, `/tasks` and — where
connected accounts are configured — `/connections` with their panels, `/stop`
by ending the turn, and anything else as a turn of the agent. It never carries thread context,
which is why the shortcut exists.

## Reactions as commands

The shortest gesture either platform has is an emoji on a message, and people
already use it as a signal. The engine can read one as a command: an operator
maps emoji names to command words, and a person putting that emoji on a
message runs the command **in that message's conversation** — the thread it is
in; a thread started under it when it is a top-level channel message; the
direct message itself when it is one. The agent is taught the word once, in
`.pi/SYSTEM.md`, and it arrives the same whether typed, picked or reacted.

The map is **per agent**, on purpose: a mention names one bot and a shortcut
belongs to one app, but every agent in a channel sees every reaction there, so
a map shared by all of them would start all of them on one emoji.

```dotenv
AGENTS_REACTION_COMMANDS__ASSISTANT=white_check_mark=done,eyes=summarize
REACTION_COMMANDS=white_check_mark=done      # the default agent's map
```

An entry is `emoji=command` or a bare `emoji` (then the command is the emoji's
name — `tada` runs `/tada`). Emoji names are the platform's short names, as in
`:white_check_mark:`, without colons or a skin tone. Empty = reactions are not
commands.

What the agent reads is the command word as the reactor's message, and the
message the reaction was put on quoted **as its author's words** (cut at
`REACTION_QUOTE_LIMIT` characters), in the envelope's quoted-context block
with the author's name — never as something the reactor said: a reaction
inside a thread points at one message, while the turn runs in the whole
thread, and the quote says which one without lending it the reactor's voice.
An entry in the map that is not `emoji=command` stops the engine at start,
naming the key. Removing a reaction does nothing; putting it back runs the
command again. A reaction the agent itself, another agent of the deployment or
any other bot adds is never a command — the agent marks a message it is
working on with `:eyes:`, so mapping `eyes` means a person's 👀 runs the
command and a bot's never does.

Platform setup:

- **Slack:** the app needs the bot scope `reactions:read` and the event
  subscription `reaction_added` (api.slack.com → your app → *Event
  Subscriptions* → *Subscribe to bot events*), beside what it already has for
  messages. The message under the reaction is read back with the `*:history`
  scopes messages already need; without them the command still runs, pointed
  at the thread alone and without the quote, and the log says so.
- **Mattermost:** nothing to register — the bot's WebSocket already carries
  `reaction_added` for the channels it is in.

Like a shortcut, a reaction needs interactivity (`INTEGRATIONS_ENABLED=true`):
a command is run by the same dispatcher that answers clicks.

## Stopping a turn

One command is the engine's on every platform: the **stop word**, `/stop` by
default (`STOP_COMMAND`; empty turns it off). Typed in a conversation where
the agent is working, it interrupts that turn: the model's words so far are
posted, then a `stopped` notice, and the next turn is told the previous one did
not finish. Whoever typed it hears privately whether anything was running.
Mapped to an emoji in `REACTION_COMMANDS` (`octagonal_sign=stop`), a reaction
on a message in that conversation does the same. The slash command still has
to be declared on the platform like any other (`/stop` on Mattermost, a
`/stop` command in the Slack app) — the engine answers it, the platform
delivers it. A service on the ws gateway sends a `cancel` frame, one on the
http gateway calls `/cancel` ([ws-gateway.md](ws-gateway.md),
[http-gateway.md](http-gateway.md)). In Slack's Agents panel the session shows
a working indicator and a native **Stop** button while the agent works
(`agent_view` in the app's manifest, `SLACK_AGENT_SESSIONS=true`, and
`DM_REPLIES=thread` for the panel's shape — see
[creating-agents.md](creating-agents.md)); pressing it ends the turn the same
way. All five doors lead to the one ending. One Slack caveat: a slash command
never reaches a thread (the platform's rule, above), so `/stop` typed in a
channel stops that channel's own turn and never a thread's, `/stop` typed in a
direct message stops the turn there (in a thread under one of its messages
too), and a turn running in a channel's thread — every Agents-panel session is
a thread — is stopped with the reaction or the Stop button.


## Teaching the agent the command

Nothing to enable in `agent.yaml` — a command arrives as a message. Just say
what it means in `.pi/SYSTEM.md`:

```markdown
## Commands

When the message starts with `/summarize`, summarize THIS conversation:
decisions made, open questions, who is waiting for what.
```

The agent may of course reach for its own tools while doing so, including
`send_ephemeral` if it decides one person should get a private note — but that
is the agent's choice, not what makes it a command.

## Private, deterministic results: a handler beside the agent

Some commands shouldn't be an agent turn at all. `/summarize` is the typical
case: the summary is meant for the person who asked, and it must actually be
produced — not "produced if the model remembers to call the right tool".

Both properties come from the same move: **let your own code handle the
command** and use the runtime as a one-shot function. This is app-level code —
`crucible` gives you the pieces, your composition root wires them (see
[building-an-app.md](building-an-app.md)); the engine ships no such handler
itself.

**The pattern.** Register the platform's command with *your* HTTP endpoint (not
the engine's `/command/<agent>`). Your handler then:

1. pulls the conversation with `ChatClient.get_thread_posts`;
2. runs one `AgentRuntime.run_stateless(profile, prompt)` — a fresh, memoryless
   process for this call;
3. delivers the result itself with `ChatAdmin.post_ephemeral`.

```python
from crucible.ports.chat.types import ConversationRef

SUMMARY_PROMPT = (
    "Summarize the discussion below: decisions made, open questions, "
    "who is waiting for what. Answer in plain text."
)


async def summarize_thread(runtime, profile, chat, admin, *,
                           channel_id: str, root_id: str, user_id: str) -> None:
    ref = ConversationRef(channel_id=channel_id, conversation_id=root_id,
                          message_id=root_id, thread_root_id=root_id)
    posts = await chat.get_thread_posts(ref)
    transcript = "\n".join(f"[@{p.username}]: {p.text}" for p in posts)
    result = await runtime.run_stateless(profile, f"{SUMMARY_PROMPT}\n\n{transcript}")
    await admin.post_ephemeral(channel_id, user_id, result.text)
```

Where the four collaborators come from in your composition root: `runtime` is
the `PiRuntime` you already built, `profile` comes from `build_pi_profile(spec)`,
and `chat` / `admin` are `handle.chat` / `handle.admin` from
`GatewayFactory.create` — see [building-an-app.md](building-an-app.md) and
`packages/impi/src/impi/app.py`.

**Why this beats instructing the agent to do it.** Delivery is deterministic:
the message is sent by your code, so it can't be forgotten. Privacy is
structural: there is no public post to leak, because nothing else posts. The
prompt lives in your repository, versioned and testable. And the agent's session
stays clean — a housekeeping run doesn't become part of the conversation it
summarizes.

**What you take on:** your own HTTP route and verification of the platform's
command token; timeouts and errors from `run_stateless`; keeping the transcript
within a sane size; and, on Mattermost, granting the bot the
`create_post_ephemeral` permission (see
[creating-agents.md](creating-agents.md)).

**Limits.** `run_stateless` is one process per call with no memory — the run
neither sees nor joins the agent's session, and its timeout comes from the
profile (`runtime.timeout` in `agent.yaml`). You can reuse the agent's profile
(same voice, same tools) or build a separate profile dedicated to the job.

**When the engine's `/command/<agent>` is the right choice instead:** the answer
is public anyway, and the command benefits from the conversation's memory and
the agent's tools — "`/deploy` staging", "`/whois` @user".

## Diagnosing

| Symptom | Where to look |
|---|---|
| Mattermost says the command failed / nothing happens | Engine log: `command … rejected (token mismatch)` → wrong or missing `AGENTS_COMMAND_TOKENS__<AGENT>` (or `COMMAND_TOKENS` for the default agent); `resolves to no agent` → `/command/default` with several agents and no `AGENT_NAME` among them; no log line at all → the request never arrived (URL, port, `AllowedUntrustedInternalConnections`) |
| A command or a click answers 403 | Engine log: `callback to /<?>/… refused: not on the receiver's secret path` → the command was registered without, or with an old, `INTEGRATIONS_SECRET`; re-register it with the URL `impi doctor` prints. Cards posted before the secret changed stay dead |
| Answer says the agent is unavailable | The named agent isn't running (check `app built: agents=[…]`) or the URL names a different agent |
| Ephemeral message never appears (Mattermost) | The bot lacks `create_post_ephemeral` — `post_ephemeral` fails with a permission error (the `send_ephemeral` tool reports it as one) |
| Slack shortcut does nothing | Callback ID must start with `SLACK_COMMAND_PREFIX` (default `crux_`); Interactivity must be on; the engine must be running (Socket Mode delivers to one connection) |
| A reaction does nothing | The emoji is not in **that agent's** map (`AGENTS_REACTION_COMMANDS__<AGENT>`; the global `REACTION_COMMANDS` is the default agent's only); on Slack the app lacks `reactions:read` or the `reaction_added` subscription; interactivity is off; the reaction was an agent's or a bot's. The engine log says `reaction :<emoji>: by <user> in <conversation> runs /<command> for <agent>` when it worked |
