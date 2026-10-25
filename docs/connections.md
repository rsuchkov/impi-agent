# Connected accounts

A person connects their own account at an OAuth provider — GitLab, or any
provider that speaks standard OAuth 2.0 — once, in a direct message with an
agent. From then on that agent's tools act **as them**, with their rights at
the provider, and only theirs. The model never sees a token, a connect link or
a code: it names a provider and a path, and gets back what the provider
answered.

This page is the deployment's side. The library behind it is
`crucible.connections` ([building-an-app.md](building-an-app.md)).

## How a connect goes

The person rarely has to know about the panel. When the agent reaches for a
provider they have not connected, the engine posts that provider's offer into
the direct message itself — what is missing and a **Connect** button — and the
model only tells them to press it. Once they have connected, the same message
says so: **✅ Connected to gitlab as alice. Ask me again and I'll carry on.**

The panel is the other way in, and the place to see or end a connection:

1. The person opens **`/connections`** in a direct message with the agent. The
   panel lists the providers that agent may use, each with **Connect** or
   **Disconnect**. Anywhere else — a channel, a thread, a direct message with
   another person or another agent — the engine answers privately that it
   opens only in a direct message with it: whose accounts are connected is
   nobody else's business. An agent cannot post it outside one either.
2. **Connect** gives them a link, privately (Mattermost's ephemeral reply, a
   Slack whisper) — never in the message. It is theirs alone, works once and
   expires in ten minutes.
3. They consent at the provider, which sends their browser back to the
   engine's callback page (`<CONNECTIONS_PUBLIC_URL>/oauth/callback`). The page
   says how it went, and the message they clicked Connect in is redrawn to
   say they are connected.
4. The agent can now use `connected_api_read` and `connected_api_write`.

A connection belongs to the chat user on one gateway (`mattermost:<id>`): the
same person on Mattermost and Slack connects twice. With
`CONNECTION_MATCH_EMAIL__<NAME>` (on by default) the account's email must be
the one the chat profile shows — so nobody binds someone else's account, or
their own to someone else. A profile that shows no email cannot be matched:
Mattermost shows it unless its privacy settings hide it
(*ShowEmailAddress*), Slack only with the `users:read.email` scope.

**Not on the ws and http gateways.** There the user id is whatever the calling
program says, and one program's `alice` cannot be told from another's, so an
agent on them is offered no connected account. `impi doctor` names the agents
a provider allows that run there.

## Setting it up

1. **Register an application at the provider.** On GitLab: *User settings* (or
   *Admin* → *Applications* for an instance-wide one) → *Applications* → *Add
   new application*. Redirect URI: `<CONNECTIONS_PUBLIC_URL>/oauth/callback`;
   **Confidential** on; scopes `read_api` for reading (the default), `api` to
   change things too — either lets the engine read who the account is. Keep
   the application ID and secret.
2. **A sealing key.** `impi connections key` writes one into `conf/.env`
   (`CONNECTIONS_KEYS`). Tokens are kept sealed with it; lose it and everyone
   connects again.
3. **The provider and the callback**, in `.env`:

   ```bash
   CONNECTION_CLIENT_ID__GITLAB=<application id>
   CONNECTION_CLIENT_SECRET__GITLAB=<secret>
   CONNECTION_BASE_URL__GITLAB=https://gitlab.example.com
   CONNECTION_SCOPE__GITLAB=read_api
   # CONNECTION_AGENTS__GITLAB=assistant      # unset = every agent
   CONNECTIONS_PUBLIC_URL=https://engine.example.com:8429
   ```

   Any other provider: `CONNECTION_KIND__<NAME>=oauth2` with
   `CONNECTION_AUTHORIZE_URL__`, `_TOKEN_URL__`, `_USERINFO_URL__` (and
   `_REVOKE_URL__` if it has one) and `CONNECTION_API_URL__` — where the tools
   call. Every key is listed in [configuration.md](configuration.md#connected-accounts).
4. **Publish the callback listener.** It is a listener of its own
   (`CONNECTIONS_PORT`, 8429), serving that one page, so only it faces
   browsers. A person's browser must reach it at `CONNECTIONS_PUBLIC_URL`; put
   it behind TLS (a reverse proxy) — the code the provider sends back travels
   in its address. In compose, publish the port in an overlay of your own, a
   file in `~/.impi/compose.d/` ([installation.md](installation.md)):

   ```yaml
   services:
     impi:
       ports:
         - "8429:8429"
   ```
5. **Give the agent the tools**, in its `agent.yaml` allowlist:

   ```yaml
   runtime:
     tools: [connected_accounts, connected_api_read, connected_api_write]
   ```

   and say in its `SYSTEM.md` what it is for ("you can read the user's GitLab
   projects and issues as them").
6. **The command.** On Mattermost, register a slash command `connections`
   like any other ([commands.md](commands.md)); on Slack, declare
   `/connections` in the app. `CONNECTIONS_COMMAND` changes the word. A
   Mattermost command belongs to the agent its URL names and opens only in
   that agent's direct message, so register it for the agent people connect
   through; with several agents, the Connect offer a tool posts covers the
   rest.
7. Restart. `impi doctor` and the startup log say which providers are on, for
   which agents, the redirect URI to register, and what would keep a connect
   from working.

## The tools

| Tool | Does | Asks first |
|---|---|---|
| `connected_accounts` | which providers the agent may use, which the person connected and as whom | no |
| `connected_api_read` | `GET <api url>/<path>` as the person | no |
| `connected_api_write` | `POST`/`PUT`/`PATCH`/`DELETE` with a JSON body, as the person | yes — the card shows the account and the full request; `DELETE` is marked dangerous |
| `mcp__<server>__<tool>` of a server whose header is `${connection:<provider>}` ([mcp.md](mcp.md)) | the server's tool, called with the person's token | as the server's entry says (`confirm:`, `safe:`) and, for a trusted server, its annotations |

- **Direct messages only**, for now: in a shared conversation, whose turn it is
  is not settled enough to act as anyone. A thread inside the direct message
  counts as shared too, for now.
- The path stays under the provider's API address: another host, a scheme, a
  step up (`..`), a backslash or a control character is refused before any
  call. A redirect is answered, not followed — following it would carry the
  token wherever the provider points.
- What comes back is marked as other people's text (an issue's description is
  whoever wrote it) and cut at 20 000 characters, saying so.
- Not connected: the engine posts the provider's Connect offer in the
  conversation, and the model is told to ask the person to press it — and
  never to ask for a token. A 401 ends the connection and asks them to connect
  again.
- **Connecting needs interactivity.** With `INTEGRATIONS_ENABLED=false` there
  is no button and no panel, so nobody can connect; `impi doctor` warns.

## Keys, refresh, ending one

- **A new key** goes first: `impi connections key` puts it ahead of the old
  ones, which keep opening what they sealed — nobody connects again. A token
  is resealed under the first key when it is refreshed, and a refresh happens
  only when a tool uses the connection — so drop an old key only once the
  connections sealed with it have been used since (GitLab's tokens live two
  hours) or ended; one nobody used since stops opening and is connected again.
  The refresh token is resealed only when the provider issues a new one, as
  GitLab does; for a provider that does not, keep the old key until those
  people have connected again.
- **Refresh** happens when a tool asks for a token that is about to end, once
  per connection however many tools ask, across replicas too.
- **Disconnect** in the panel ends it here and revokes it at the provider; if
  the provider does not confirm the revoke, the panel says so and the person
  removes the application in their account there.
- **Someone leaves.** Their connection is not ended by leaving the chat. They
  disconnect, or the provider's administrator revokes the application for
  them. There is no `impi` command for it yet.

## What it does not do

- **A skill's own CLI gets no token.** These tools run in the engine; nothing
  puts the person's token in an agent's process. A skill that needs a
  credential in a command line uses [ward](secrets.md) — and then it is the
  deployment's credential, not the person's.
- **Scheduled work acts as nobody.** A task's turn has no person behind it —
  even one scheduled in somebody's direct message — so no connection is used
  there, and its tools say so.
- **Several people in one place.** Channels, merged threads, an allowlist of
  who may connect, and an offboarding command are not designed yet; until they
  are, connected accounts are a direct-message feature.

Where the tokens live, and what someone with the database alone can read:
[storage.md](storage.md) and [security.md](security.md#where-the-credentials-live).
