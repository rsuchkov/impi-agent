---
name: connections
description: Let an agent act as the person at their own GitLab (or any OAuth 2.0 provider) account, and explain why a connect or a connected-account tool failed. Use when the operator wants an agent to read or change things in someone's own account, or someone reports that /connections, the consent page or connected_api_* does not work.
---

# Connected accounts

A person connects their own account at a provider once, in a **direct message**
with an agent; that agent's `connected_api_read` / `connected_api_write` then
act as them, with their rights. Usually they never type anything: the first
call that finds them not connected posts a Connect button in the DM, and the
same message turns into "✅ Connected …" once they have. `/connections` is the
panel to see or end connections. The token stays in
the engine — never in the model, never in an agent's process. Reference:
`$IMPI_ROOT/docs/connections.md` (setup), `docs/configuration.md#connected-accounts`
(every key).

You hold none of these tools and connect nobody: you set it up and diagnose it.

## 1. Setting it up (what the operator does)

1. An OAuth application at the provider, redirect URI
   `<CONNECTIONS_PUBLIC_URL>/oauth/callback`, confidential, scope `read_api`
   (or `api` to change things). The operator does this in the provider's UI.
2. `impi connections key` — writes `CONNECTIONS_KEYS`.
3. In `.env`: `CONNECTION_CLIENT_ID__<NAME>`, `CONNECTION_CLIENT_SECRET__<NAME>`,
   `CONNECTION_BASE_URL__<NAME>` (GitLab) — or `CONNECTION_KIND__<NAME>=oauth2`
   with the authorize/token/userinfo/API URLs — and `CONNECTIONS_PUBLIC_URL`.
   `CONNECTION_AGENTS__<NAME>` narrows which agents may use it.
4. Publish `CONNECTIONS_PORT` (8429) where browsers reach it, behind TLS.
5. The agent's `runtime.tools`: `connected_accounts`, `connected_api_read`,
   `connected_api_write` — you can edit that (agent-builder), and say in its
   `SYSTEM.md` what the account is for.
6. A slash command `connections` on Mattermost (chat-commands skill), or
   `/connections` in the Slack app.
7. Restart (new `.env` keys), then `impi doctor` on the host — `impi health`
   from your own shell says the same.

Do not write the client secret or the key yourself unless the operator pastes
them to you for that; they are credentials.

## 2. Why it does not work

`impi health` says most of it — a `connections:` line (providers, agents, the
redirect URI to register) and `!` lines for what is wrong.

| Symptom | Cause | Fix |
|---|---|---|
| Engine will not start: `CONNECTIONS_KEYS is empty` / `CONNECTIONS_PUBLIC_URL is required` | a provider is configured without them | `impi connections key`; set the URL |
| `CONNECTION_… is required` / `is not one of` at start | a provider key missing or wrong — the message names it | fix that key |
| `/connections` answers "opens only in a direct message" | typed in a channel, a thread, or a DM with another person or another agent (a Mattermost command belongs to the agent its URL names) | open a DM with that agent; with several agents, rely on the Connect offer a tool posts |
| `/connections` reaches the agent as a turn | the word is not the engine's: slash command not named `CONNECTIONS_COMMAND`, or no provider configured | match the word; configure a provider |
| "This agent uses no connected accounts" | no provider allows this agent | `CONNECTION_AGENTS__<NAME>` |
| Provider shows "redirect URI is invalid" | the registered URI is not `<CONNECTIONS_PUBLIC_URL>/oauth/callback` exactly | copy it from `impi health` |
| Browser cannot open the callback page | the port is not published, or the URL is not one a browser reaches | publish `CONNECTIONS_PORT`; fix `CONNECTIONS_PUBLIC_URL` |
| Page: "expired or was already used" | the link is single-use and lives ten minutes | Connect again |
| Page: "your chat account shows no email to compare it with" | `CONNECTION_MATCH_EMAIL__<NAME>` on, and the chat hides emails (Mattermost privacy settings, Slack without `users:read.email`) | show emails, or set it to `false` and accept that anyone may bind any account they can log into |
| Page: "its email is not the one your chat account has" | they logged in as another account at the provider | log in as their own |
| Agent says the tool is missing | not in `runtime.tools`, the tool server off, or no provider configured (the tools are advertised only then) | engine-diagnostics §3 |
| Tool: "works only in a direct message" | the agent was asked in a channel, or a thread in one (a thread inside the DM is fine) | by design, for now |
| Tool: "there is no person here whose connected accounts a tool could use" | a scheduled turn (it acts for nobody), or an agent on the ws/http gateway (`impi health`: `… is not offered to …`) | by design: those turns have no person the engine can name |
| Tool: "has not connected their … account" (an `mcp__<server>__…` tool says the same, naming the server) | they never connected, or a 401 ended it | the Connect button the engine posted, or `/connections` in the DM |
| No Connect button appeared | interactivity off (`INTEGRATIONS_ENABLED`): there is then no button and no panel, and nobody can connect | turn interactivity on |
| Connected, but the button message did not change | the chat refused the edit (`impi logs`: `on_connected hook failed`) | harmless: the connection is stored; `/connections` shows it |
| Tool: "give a path under the provider's API, not a URL" / "may not step up out of the provider's API" / "the path leaves the provider's API" | the model passed a full URL, a `..`, or a path that resolves elsewhere | the path is relative to `CONNECTION_API_URL__<NAME>` |
| Panel: "did not confirm the revoke" | the provider did not answer the revoke | the person removes the application in their account there |

Audit lines are on the logger `crucible.connections.audit` (`impi logs`): each
connect, refresh, refusal and disconnect, with whose and which provider — never
a token or a code.

## 3. What it does not do — say so plainly

- A skill's own CLI gets no token. For a credential in a command line, use
  **ward** (that is the deployment's credential, not the person's).
- Scheduled tasks act as nobody, even one scheduled in somebody's DM.
- Agents on the ws/http gateway are offered no connected accounts: the user id
  there is whatever the calling program says.
- Channels and shared threads: not designed yet. A thread inside the person's DM works (only they and the agent read it).
- Someone who leaves keeps their connection until they disconnect or the
  provider revokes the application; there is no `impi` command for it yet.
