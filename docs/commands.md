# Commands (slash commands & shortcuts)

A **command** is a way to ask an agent for something without writing a message
to it: the user types `/summarize` in a Mattermost thread, or picks a shortcut
on a Slack message, and the agent runs a turn and answers in that conversation.

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

## Platform differences

|  | Mattermost | Slack |
|---|---|---|
| Entry point | slash command (works inside threads) | **message shortcut** (`crux_*`) |
| Slash command in a thread | ✅ | ❌ [not allowed for custom commands](https://docs.slack.dev/interactivity/implementing-slash-commands/) — only built-ins and Giphy |
| Thread root comes from | `root_id` in the payload | `message.thread_ts` (else `message.ts`) |
| Transport | HTTP `POST /command/{agent}` on the interactions receiver | Socket Mode (no HTTP, no public URL) |
| Verification | per-command token, checked against config | the socket is already authenticated |

## Setting up Mattermost

1. **Create the command.** System Console → Integrations → Slash Commands (or
   `mmctl command create <team> --title Summarize --trigger-word summarize
   --url <URL> --creator <user> --post`), where `<URL>` is
   `<INTEGRATIONS_PUBLIC_URL>/command/<agent>` — for example
   `http://192.168.1.10:8423/command/assistant`. The **path names the
   agent**: a Mattermost payload doesn't say which bot it is meant for.
   Request method must be **POST**.
2. **Copy the token** Mattermost generates and put it in the engine `.env`,
   keyed by the agent's name (upper-cased, `-`→`_`):
   ```
   AGENTS_COMMAND_TOKENS__ASSISTANT=<token>
   ```
   Several commands for one agent = several tokens, comma-separated. An agent
   with no tokens configured refuses every command (HTTP 403) — that is the only
   thing standing between the receiver's port and running a turn as your agent.
3. **Networking**: the receiver is the same one that serves widget callbacks, so
   `INTEGRATIONS_PUBLIC_URL` must be reachable from Mattermost and its subnet
   must be in `AllowedUntrustedInternalConnections` (see
   [troubleshooting.md](troubleshooting.md)).
4. Restart the engine (config is read at startup).

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
thread. A slash command may still be registered for channel-level use, but it
will never carry thread context.

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
| Mattermost says the command failed / nothing happens | Engine log: `command … rejected (token mismatch)` → wrong or missing `AGENTS_COMMAND_TOKENS__<AGENT>`; no log line at all → the request never arrived (URL, port, `AllowedUntrustedInternalConnections`) |
| Answer says the agent is unavailable | The named agent isn't running (check `app built: agents=[…]`) or the URL names a different agent |
| Ephemeral message never appears (Mattermost) | The bot lacks `create_post_ephemeral` — `post_ephemeral` fails with a permission error (the `send_ephemeral` tool reports it as one) |
| Slack shortcut does nothing | Callback ID must start with `SLACK_COMMAND_PREFIX` (default `crux_`); Interactivity must be on; the engine must be running (Socket Mode delivers to one connection) |
