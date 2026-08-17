# Secrets

An agent that can push a release needs a token. Handing it one means the token
is in its context window, which means it is in the transcript, in the model
provider's logs, and in whatever the agent decides to print. This is the
alternative: the agent asks for a secret by name, you approve the request in
chat, and the value goes straight into the process it wanted to run.

```
agent's shell
   └─ secret-exec --env GITHUB_TOKEN=vault://github-token \
                  --reason "push the release" -- gh release create v1.2.0
        │
        ▼
   the engine ── policy ── a window already open? ──yes──┐
        │ no                                             │
        │   🔐 assistant is asking for a secret.         │
        │   Secret: vault://github-token                 │
        │   Reason: push the release                     │
        │   Command: gh release create v1.2.0            │
        │   [Allow once] [Allow for… ▾] [Deny]           │
        ▼                                                ▼
      Vault  ───────────── value ──────────────────────► exec, with the value
        │                                                 in the child's env
        └─ one row in the ledger (never the value)
```

## What this protects, and what it doesn't

Worth reading before you rely on it.

**It protects** the value from ever reaching the model: not in the context
window, not in the pi session history, not in the engine's logs, not at the
model provider. It makes every use visible — a request nobody approves is a
request that doesn't happen — and it leaves a ledger row for every attempt,
including the refused ones.

**It does not protect** against a compromised container. The engine and the
agents' shells run as the same user in the same container, and `/app/conf/.env`
is readable there — so an agent with `bash` can already read the Mattermost and
Slack tokens today, broker or no broker. Separating those (a distinct uid, or a
container of its own for the runtime) is real work and has not been done.

Two consequences worth stating plainly:

- The approval card shows the **exact command**. It has to, because a caller may
  legitimately ask for a secret in order to run `sh -c 'echo $TOKEN'`, and
  reading the command is the only thing between an approval and a leak.
- Choosing the unattended unlock mode (key files on disk) puts the credential
  that opens the store next to the store, readable by the same processes. It is
  a real convenience — a scheduled task at 3am needs it — and a real trade.

## Setting it up

The installer asks whether you want a secret store; answering yes adds the Vault
container and writes the settings. To add it to an existing deployment, set
`SECRETS_ENABLED=true` (see [configuration.md](configuration.md)), add
`IMPI_VAULT=1` to `~/.impi/compose.env`, and `impi restart`.

Then, once:

```bash
impi secret init
```

This initialises Vault, unseals it, creates the engine's role, and prints three
things **once**: the unseal key, Vault's root token, and the engine's AppRole
secret. Put them in a password manager. The engine needs the unseal key and the
AppRole secret after every restart; nothing else ever needs the root token.

After each restart of the stack:

```bash
impi secret unlock          # asks for the unseal key and the AppRole secret
```

Until then the store is locked and every request is refused with
`decision=locked`. If you would rather not be in the loop — a scheduled task
runs while you are asleep — write the two values to files, mount them, and point
`SECRETS_UNSEAL_KEY_FILE` and `SECRETS_SECRET_ID_FILE` at them; the engine then
opens the store itself at startup. Re-read the threat model above before you do.

## Storing a secret, and saying who may use it

A value and a policy are two separate things, and a secret with no policy is
reachable by nobody:

```bash
impi secret set github-token                       # prompts; never echoes
impi secret policy set github-token \
    --subjects assistant \
    --approval always \
    --max-grant 15m
```

- `--subjects` is an allowlist of agent names. Empty — the default — means
  nobody, so a new secret starts unreachable rather than open.
- `--approval always` asks a human every time, unless a window is open.
  `--approval never` is for the fully automatic ones (a model endpoint key a
  nightly task needs) and asks nobody, ever.
- `--max-grant` is the longest window a human may leave open from the card. `0`
  means no window at all: every single use is asked about.

A multi-field secret is one secret:

```bash
impi secret set smtp --field username=bot --field password=hunter2
```

## What an agent can do

Exactly one thing:

```
secret-exec --env NAME=vault://secret[#field] [--env ...] [--reason TEXT] -- command [args...]
```

`secret-exec` is on `PATH` inside the container. It asks the engine, and on an
approval replaces itself with `command`, with the values bound to the named
environment variables. It never prints a value.

There is **no way to list what exists**. A list of names is a list of things to
try, so there is no `ls` for an agent and no route on the engine that would
answer one. An agent either knows the reference it needs — from its own
`SYSTEM.md`, a skill, or a person telling it — or it does not.

For the same reason every refusal looks identical. No such secret, not on the
allowlist, refused by a human, nobody answered in time: one message, one exit
code (`77`). The real reason goes to the ledger, not to the caller. A store that
cannot serve anyone right now is the one distinguishable case — exit code `75`,
"not available" — because it says nothing about what exists.

One invocation is about **one** secret; several fields of it are fine. Two
different secrets nest, and each is approved on its own terms:

```bash
secret-exec --env A=vault://one -- secret-exec --env B=vault://two -- deploy
```

Tell an agent about a secret in its profile or a skill, e.g.:

> To publish a release, run:
> `secret-exec --env GITHUB_TOKEN=vault://github-token --reason "<why>" -- gh release create …`

## Approving

The request arrives as a direct message from the agent that made it, to the
first name in `SECRETS_APPROVERS`. Set `SECRETS_APPROVAL_CHANNEL` to send them
to a channel instead — but note that a request in a shared channel is a request
everyone in it can read.

Only a configured approver can answer. A click from anyone else changes nothing
and leaves the buttons live for the person it was addressed to.

Three answers:

- **Allow once** — this call only. Nothing is left behind; the next one asks
  again.
- **Allow for…** — a window (1 min / 5 min / 15 min / 1 hour, filtered by the
  policy's `--max-grant`). Every request from that agent for that secret is
  served without asking until it closes. The value is still read fresh each
  time, so rotating the secret or revoking the window takes effect at once.
- **Deny** — refuses this request. The next one asks again.

Nobody answering is a refusal: after `SECRETS_APPROVAL_TIMEOUT_S` (120s by
default) the request fails closed and the card is struck out. Keep that timeout
well under the agent's own turn timeout — the wait blocks a command inside a
turn, and a turn that dies waiting is worse than a refusal.

## Windows and the ledger

```bash
impi secret grants              # what is currently open
impi secret revoke gr_ab12cd    # close one now
impi secret audit --limit 20    # every request, granted or not
```

The ledger holds one row per request: when, which agent, which secret, the
reason and command it gave, what was decided, who decided it, and how long it
waited. It never holds a value. Its decisions are a closed set, so "why didn't
that work" is always greppable:

| decision | what happened |
|---|---|
| `approved_once` | a human allowed that one call |
| `approved_grant` | a human opened a window; this call used it |
| `reused_grant` | served by a window opened earlier |
| `auto` | the policy says `approval: never` |
| `denied` | a human refused |
| `timeout` | nobody answered in time |
| `no_policy` | nothing is configured under that name |
| `not_permitted` | the policy does not list that agent |
| `no_approver` | approval was needed and nobody is configured to give it |
| `locked` | the engine holds no credential — run `impi secret unlock` |
| `sealed` | the store itself is sealed |
| `backend_error` | approved, but the read failed |

The four in the middle — `denied`, `timeout`, `no_policy`, `not_permitted` — are
what the caller saw as one identical refusal. This table is the only place they
are told apart.

## When something doesn't work

Start with `impi secret status`: it says whether the store is reachable, sealed
or simply unopened, how many policies exist, and when the last request was.

- **Everything is refused right after a restart** — the store is locked. `impi
  secret unlock`.
- **An agent is refused and you expected it to work** — `impi secret audit
  --agent <name>`. `no_policy` means the name is wrong or nothing is configured;
  `not_permitted` means the agent is not in `--subjects`.
- **No card arrives** — `no_approver` in the ledger means `SECRETS_APPROVERS` is
  empty or names somebody the platform doesn't resolve. On a gateway with no
  channel administration the engine cannot open a direct message; set
  `SECRETS_APPROVAL_CHANNEL`.
- **The agent's turn dies while you are deciding** — `SECRETS_APPROVAL_TIMEOUT_S`
  is too close to the agent's `runtime.timeout`. Lower the former, or raise the
  latter in the agent's `agent.yaml`.

## Where things live

| | |
|---|---|
| values | Vault (KV v2, mount `secrets`), encrypted at rest, sealed until unlocked |
| policies, windows, ledger | the engine's SQLite database, beside sessions and tasks |
| the engine's credential | memory only, unless you chose the key files |
| the agent's access | `secret-exec`, and nothing else |

Vault holds bytes; impi owns the authorization layer on top of it. That split is
why revoking a window or editing a policy takes effect on the next call rather
than the next restart, and why nothing in the database can leak a value.
