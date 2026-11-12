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
   says how it went — the engine's plain one, or the application's own
   (`CallbackServer(page=)`, [building-an-app.md](building-an-app.md)) — and the
   message they clicked Connect in is redrawn to say they are connected.
4. The agent can now use `connected_api_read` and `connected_api_write`.

A connection belongs to the chat user on one gateway (`mattermost:<id>`): the
same person on Mattermost and Slack connects twice — unless the application
keys people by the account itself (see "owner" in
[building-an-app.md](building-an-app.md)), in which case one person connects
once, from whichever chat. With
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
   call. A system with no OAuth, where people make a credential themselves:
   `CONNECTION_KIND__<NAME>=credential` with `CONNECTION_WHOAMI_URL__` (where
   it says whose a credential is), `CONNECTION_API_URL__`,
   `CONNECTION_CREDENTIAL_AUTH__` (`bearer`, or `basic` for a system that
   takes it as the user name with `token` for a password) and
   `CONNECTION_CREDENTIAL_HINT__` (where one comes from, for the form) — no
   client, no secret; see "A credential the person enters" below. Every key is listed in [configuration.md](configuration.md#connected-accounts).
4. **Publish the callback listener.** It is a listener of its own
   (`CONNECTIONS_PORT`, 8429), serving the callback page and the credential
   form, so only it faces browsers. A person's browser must reach it at
   `CONNECTIONS_PUBLIC_URL`; put it behind TLS (a reverse proxy) — the code
   the provider sends back travels in its address, and a credential a person
   enters travels in the form's body. In compose, publish the port in an overlay of your own, a
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

## A credential the person enters

Some systems have no OAuth — a self-hosted log store, a dashboard — only
credentials their people make themselves, a personal token or an API key,
with the rights of their own account. Such a system is a provider of the
`credential` kind, and connecting to it goes
the same way as far as the person is concerned: **Connect** in the panel (or
the agent's offer) gives them a link, theirs alone, that works once and
expires in ten minutes — only it opens a form on the engine's own page
(`<CONNECTIONS_PUBLIC_URL>/connect/<link>`) instead of the provider's consent
page. The form says where a credential comes from
(`CONNECTION_CREDENTIAL_HINT__`), the person pastes one, and the engine asks
the system whose it is
(`CONNECTION_WHOAMI_URL__`); the account it names is shown as the connection,
and the application's rule on accounts (the email match) applies as to any.
A credential the system refuses connects nothing. From then on it is a connection
like any other: `grant()` hands it to tools, a refusal by the system makes the
tool `forget` it, the panel shows and ends it.

What differs is the engine's hands off the credential itself. It is never
refreshed (it has no end the engine knows of) and never revoked: it is the
person's — or a service account's, shared with others — and goes on working
at the system until they end it there, which a disconnect says. It travels
once, in the form's body, to the engine and on to the system, and is kept
sealed like an OAuth token; it is never shown again (to replace it, connect
again), never logged, never in the audit. The form and the outcome page are
the application's to draw (`CallbackPage`,
[building-an-app.md](building-an-app.md)) — the plain ones serve a
deployment with none of its own. Keep the link private: the form cannot tell
whose browser opened it, so the engine shows it only where one person reads,
and the form names whom the link was made for — a person handed somebody
else's link sees it is not theirs, since what they entered would be the
other's to use (`CONNECTION_MATCH_EMAIL__` refuses the account when the
system names an email; a system that names none leaves that to the person).
A browser may offer to remember what was pasted into the password field, as
browsers do; the engine itself keeps it sealed and never shows it again.

The bundled `connected_api_read`/`connected_api_write` carry a credential as
a bearer; a system that takes it otherwise (`basic`) gets a tool of the
application's own, which reads `grant()` and shapes the request as the
system wants ([building-an-app.md](building-an-app.md)).

## A sign-in before a turn

A connection is also a sign-in: the person proved who they are at the
provider, at a moment the engine keeps (`Connections.connected_at(owner,
provider)` — the connect, not the latest refresh, which a tool does without
them). An application that wants the chat held to a provider's own bar — a
chat session lives for months, the provider signs people in every day — asks
that moment in a `TurnAdmission` ([building-an-app.md](building-an-app.md#composition-notes-from-the-field))
and, when the connect is too old or there is none, answers with the person's
own `begin(owner, provider)` link; the flow delivers it to them alone, in the
channel or the thread they wrote in (on Mattermost the bot needs the
`create_post_ephemeral` permission for that, as for the tools' ephemeral
posts), and runs no turn until they are back.
Register the provider's application without a refresh scope (`offline_access`
at an OpenID provider) for that: with no refresh token an ended access token
ends the connection (`grant` answers `NotConnected`), and the only way on is to
connect again. Who the connect binds to is the application's rule
(`ConnectionOwner.owner_of`): compare the account the provider names with what
the chat says of the person (an email), and refuse a sign-in by somebody else.
`connected_at` is `None` for a connection made before the engine kept the
moment; such a person connects once more.

## What the provider says of the person, beyond who they are

A connection keeps four things of the account — id, username, name, email —
and whatever else the provider's userinfo answer carries that the
deployment asks for: `CONNECTION_ACCOUNT_EXTRA__<NAME>=groups,department`
names the fields, by the provider's own names, and they come back as the
provider sent them (a list stays a list; a field the answer lacks is absent,
not empty) in `ProviderAccount.extra` — to the application's account rule
and owner rule at the connect (`refusal`, `owner_of`), and from `account()`
and `connected()` after. An identity provider's groups claim is the case:
who may do what, decided by the groups the person was given there. impi's
bundled rule compares the email only; the fields are for an application
built on the engine with a rule of its own
([building-an-app.md](building-an-app.md)). The engine reads the userinfo
answer, not the ID token, so a claim must be in userinfo to be kept. The
fields are as of the connect; a reconnect refreshes them, a refresh of the
token does not.

## The tools

| Tool | Does | Asks first |
|---|---|---|
| `connected_accounts` | which providers the agent may use, which the person connected and as whom | no |
| `connected_api_read` | `GET <api url>/<path>` as the person | no |
| `connected_api_write` | `POST`/`PUT`/`PATCH`/`DELETE` with a JSON body, as the person | yes — the card shows the account and the full request; `DELETE` is marked dangerous |
| `mcp__<server>__<tool>` of a server whose header is `${connection:<provider>}` ([mcp.md](mcp.md)) | the server's tool, called with the person's token | as the server's entry says (`confirm:`, `safe:`) and, for a trusted server, its annotations |

- **Direct messages only**, for now: in a shared conversation, whose turn it is
  is not settled enough to act as anyone. A thread inside the direct message is
  a direct message for this (only the person and the agent read it), though the
  reply still goes to the thread.
- The path stays under the provider's API address: another host, a scheme, a
  step up (`..`), a backslash or a control character is refused before any
  call. A redirect is answered, not followed — following it would carry the
  token wherever the provider points.
- What comes back is marked as other people's text (an issue's description is
  whoever wrote it) and cut at 20 000 characters, saying so.
- Not connected: the engine posts the provider's Connect offer in the
  conversation, and the model is told to ask the person to press it — and
  never to ask for a token in the chat (a `credential` provider's credential
  is entered on the engine's own page, never told to the agent). A 401 ends the
  connection and asks them to connect again.
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
  per connection however many tools ask, across replicas too. A credential
  the person entered is never refreshed.
- **Disconnect** in the panel ends it here and revokes it at the provider; if
  the provider does not confirm the revoke, the panel says so and the person
  removes the application in their account there. A credential the person
  entered is not revoked: the panel says it still works at the system, for
  them to end there.
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
  are, connected accounts are a direct-message feature. The one word that
  reaches a person anywhere is the sign-in link a turn not admitted carries
  — delivered to them alone (an ephemeral post) wherever they wrote; a tool's
  Connect offer still opens in a direct message only.

Where the tokens live, and what someone with the database alone can read:
[storage.md](storage.md) and [security.md](security.md#where-the-credentials-live).
