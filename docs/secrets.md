# Secrets

An agent that can push a release needs a token. Handing it one means the token
is in its context window, which means it is in the transcript, in the model
provider's logs, and in whatever the agent decides to print. This is the
alternative: the agent asks for a secret by name, you approve the request in
chat, and the value goes straight into the process it wanted to run.

```
 engine container                    broker container
   agent's shell                       ward ── loopback ──► the store
     └─ secret-exec ── mutual TLS ──►    │      (never on the network)
        --env GITHUB_TOKEN=…            │
        -- gh release create v1.2.0     │  🔐 assistant is asking for a secret.
                                        │  Secret: vault://github-token
                                        │  Reason: push the release
                                        │  Command: gh release create v1.2.0
                                        │  [Allow once] [Allow for… ▾] [Deny]
                                        │
        exec, with the value ◄──────────┘  and one row in the ledger,
        in the child's env                 which never holds the value
```

## What this protects, and what it doesn't

Worth reading before you rely on it.

**It protects** the value from ever reaching the model: not in the context
window, not in the pi session history, not in the engine's logs, not at the
model provider. It makes every use visible — a request nobody approves is a
request that doesn't happen — and it leaves a ledger row for every attempt,
including the refused ones.

**It limits, but does not undo,** a compromised engine. The broker runs in its
own container with the store's credential, and the engine has none — so taking
the engine no longer means taking the secrets silently. What it still means is
being able to *ask*: a compromised engine can make requests as any agent it
runs, with a reason and a command of its choosing. Every one of them is a card
somebody has to read and click, and a row in the ledger.

**It does not protect** an agent's own container from itself. The engine and the
agents' shells still run as the same user there, and the certificate an agent
proves itself with has to be readable for `secret-exec` to use it — so anything
else in that container can present it too, including another agent: every
identity is mounted in the same directory. What that buys today is that nothing
*outside* that container can present one at all. Telling agents apart becomes a
real boundary the day they get containers of their own; the per-agent
certificate is what makes that a move rather than a rewrite.

Three consequences worth stating plainly:

- The approval card shows the **exact command**. It has to, because a caller may
  legitimately ask for a secret in order to run `sh -c 'echo $TOKEN'`, and
  reading the command is the only thing between an approval and a leak.
- Choosing the unattended unlock mode (key files on disk) puts the credential
  that opens the store next to the store, readable by the same processes. It is
  a real convenience — a scheduled task at 3am needs it — and a real trade.
- The card is posted by the broker's own bot. Get used to that account: a
  compromised agent can draw a card that looks like one, but it cannot post as
  the broker.

## Setting it up

The installer asks whether you want a secret store. Answering yes adds two
containers — the broker and the store it opens — creates `conf/ward.env` for the
broker's own settings, and `certs/` for the identities. Nothing is written to the
engine's `conf/.env`: the engine has no settings for any of this.

The broker posts approval cards as its **own** chat account, so give it one:
create a bot named `ward` and put its token in `conf/ward.env` as
`WARD_MATTERMOST_TOKEN`. Without it the broker starts, decides nothing, and
every request that needs a human is refused with `no_approver`.

Then the one-time ceremony:

```bash
impi ward init
```

This initialises the store, creates the certificate authority, and issues the
broker's own certificate and the operator's. The material it produces — the
unseal key and the broker's credential — is **not printed**: it is written to
`~/.impi/ward-recovery.txt` (mode 600, in a directory no container mounts) and
the command prints the path. A credential printed to a terminal lives on in
scrollback, in a screen share, and in the transcript of whatever ran the
command; that is not a risk worth taking for the convenience of copying it by
eye. Move that file into your password manager.

There is no root token to keep: it is destroyed at the end of the ceremony.
Nothing needs it again — the broker runs on its own credential and can replace
that itself — and if the day ever comes, the unseal key regenerates one with
`vault operator generate-root`.

The role id is not a credential, so `impi ward init` writes it into
`conf/ward.env` for you; the broker picks it up on the next `impi start`.

Give each agent an identity, and yourself the operator one:

```bash
impi ward cert assistant     # writes assistant.crt/.key and ca.crt
```

No restart for that: the tool reads the certificate when it asks, so an identity
minted now is used by the next request. A restart is only needed when the
container's own environment changes — adding the store to a running deployment
does, which is why the walkthrough below has one.

## Turning it on in a deployment that already runs

Enabling the store on a running installation is a handful of steps, and one of
them (`conf/ward.env`) has to exist before the stack will come up at all —
compose refuses to start a service whose env file is missing.

```bash
# 1. the broker's own settings, before anything is restarted
$EDITOR ~/.impi/conf/ward.env
#   WARD_MATTERMOST_TOKEN=<the ward bot's token>
#   WARD_MATTERMOST_URL=http://mattermost:8065
#   WARD_APPROVERS=<your username>

mkdir -p ~/.impi/certs                  # the identities, mounted at /app/conf/certs

# 2. add the two containers
echo IMPI_VAULT=1 >> ~/.impi/compose.env

# 3. the ceremony, BEFORE the first start: it writes the role id the broker
#    needs, and a broker without a certificate authority only restarts in a loop
impi ward init                          # -> ~/.impi/ward-recovery.txt

# 4. now bring the stack up, and open the store
impi start                              # `restart` is the engine only
impi ward unlock --from ~/.impi/ward-recovery.txt
impi ward cert assistant                # an identity, used from the next request
impi ward status                        # should say: secrets: open
```

Nothing goes in the engine's `conf/.env`: the engine has no settings for any of
this. Where to ask and where the identities are mounted are declared by the
compose overlay, and the tool reads them from the container's environment.

If Mattermost runs outside this stack, add `ward` to its
**AllowedUntrustedInternalConnections** as well, or the click on an approval
card never reaches the broker. The bundled Mattermost already allows it.

After each restart of the stack — and that includes `impi start` and every
`impi update` — the store is sealed again and every request is refused. Both
commands say so when they finish. To open it:

```bash
impi ward unlock --from ~/.impi/ward-recovery.txt
```

Without `--from` it asks for the two values instead, which means typing a key at
a prompt; prefer the file. `impi doctor` says whether the store is open.

Until then the store is locked and every request is refused. If you would rather
not be in the loop — a scheduled task runs while you are asleep — write the two
values to files, mount them, and point `WARD_UNSEAL_KEY_FILE` and
`WARD_SECRET_ID_FILE` at them; the broker then opens the store itself at
startup. Re-read the threat model above before you do.

## Storing a secret, and saying who may use it

A value and a policy are two separate things, and a secret with no policy is
reachable by nobody:

```bash
impi ward set github-token                       # prompts; never echoes
impi ward policy set github-token \
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
impi ward set smtp --field username=bot --field password=hunter2
```

## What an agent can do

Exactly one thing:

```
secret-exec --env NAME=vault://secret[#field] [--env ...] [--reason TEXT] -- command [args...]
```

`secret-exec` is on `PATH` inside the agent's container. It asks the broker over
mutual TLS — the certificate is what says which agent is asking — and on an
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

One invocation may name **several** secrets — up to five — and they are served
together or not at all:

```bash
secret-exec --env GITHUB_TOKEN=vault://github-token \
            --env NPM_TOKEN=vault://npm-token \
            --reason "publish the release" -- make release
```

One card lists all of them, one click answers for all of them, and the window
offered is the shortest any of their policies allows. A refusal on any one
refuses the whole request: half an environment would let the command run and
fail somewhere less obvious. If some are already covered by open windows, only
the rest are asked about — but the card still lists everything, so you see the
whole picture before deciding.

Tell an agent about a secret in its profile or a skill, e.g.:

> To publish a release, run:
> `secret-exec --env GITHUB_TOKEN=vault://github-token --reason "<why>" -- gh release create …`

## Approving

The request arrives as a direct message **from the broker's own bot** — not from
the agent that asked — to the first name in `WARD_APPROVERS`. Set
`WARD_APPROVAL_CHANNEL` to send them to a channel instead, but note that a
request in a shared channel is a request everyone in it can read.

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

Nobody answering is a refusal: after `WARD_APPROVAL_TIMEOUT_S` (120s by
default) the request fails closed and the card is struck out. Keep that timeout
well under the agent's own turn timeout — the wait blocks a command inside a
turn, and a turn that dies waiting is worse than a refusal.

## Windows and the ledger

```bash
impi ward grants              # what is currently open
impi ward revoke gr_ab12cd    # close one now
impi ward audit --limit 20    # every request, granted or not
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
| `locked` | the broker holds no credential — run `impi ward unlock` |
| `sealed` | the store itself is sealed |
| `backend_error` | approved, but the read failed |

The four in the middle — `denied`, `timeout`, `no_policy`, `not_permitted` — are
what the caller saw as one identical refusal. This table is the only place they
are told apart.

## Replacing the broker's credential

If the secret id ever leaks — a screen share, a log, a file in the wrong place:

```bash
impi ward rotate     # mints a new one, destroys the old
```

The running broker keeps serving on the session it already has, so nothing is
interrupted, but **the next unlock needs the new value** — put it in
`ward-recovery.txt` and your password manager straight away. Use `--machine` to
have it written rather than printed.

The unseal key is a different matter: it is Vault's, not the broker's, and
changing it is `vault operator rekey` inside the store's container. If both have
leaked and the store holds little, the honest fastest answer is to remove the
store's volume and run the ceremony again — you lose the values, and every
policy and window with them.

## When something doesn't work

Start with `impi ward status`: it says whether the store is reachable, sealed
or simply unopened, how many policies exist, and when the last request was.

- **Everything is refused right after a restart** — the store is sealed, as it
  is after every restart. `impi ward unlock --from ~/.impi/ward-recovery.txt`.
- **`impi ward init` says the store is already initialised** — it is, and no
  flag here rotates its keys (`--force` only replaces the certificate
  authority). Use `impi ward rotate` for the credential, or remove the store's
  volume to start over, which deletes every value in it.
- **An agent is refused and you expected it to work** — `impi ward audit
  --agent <name>`. `no_policy` means the name is wrong or nothing is configured;
  `not_permitted` means the agent is not in `--subjects`.
- **No card arrives** — `no_approver` in the ledger means `WARD_APPROVERS` is
  empty, names somebody the platform doesn't resolve, or the broker has no chat
  account of its own (`WARD_MATTERMOST_TOKEN`). If its account cannot open a
  direct message, send the cards to a channel with `WARD_APPROVAL_CHANNEL`.
- **The card arrives but the buttons do nothing** — Mattermost refuses to call
  an address that is not in `AllowedUntrustedInternalConnections`; `ward` has to
  be in that list.
- **The agent's turn dies while you are deciding** — `WARD_APPROVAL_TIMEOUT_S`
  is too close to the agent's `runtime.timeout`. Lower the former, or raise the
  latter in the agent's `agent.yaml`.

## Where things live

| | |
|---|---|
| values | Vault (KV v2), in the broker's container, reachable only on its loopback |
| policies, windows, ledger | the broker's own SQLite database — not the engine's |
| the store's credential | the broker's memory, unless you chose the key files |
| the unseal key and that credential | `~/.impi/ward-recovery.txt` on the host (600), mounted into nothing |
| the certificate authority | the broker's container; its key goes nowhere else |
| the agent's access | `secret-exec`, over mutual TLS, and nothing else |
| the operator's access | `ward-admin` (what `impi ward …` runs), with a different certificate |
| the engine's access | none. It installs the tool and tells each agent its own name |

Both programs are configured by two variables the compose overlay puts in the
engine container's environment — `SECRET_BROKER_URL` and
`SECRET_BROKER_CERTS_DIR` — and work the rest out: an agent presents
`<certs>/<its own name>.crt`, the operator presents `<certs>/operator.crt`, and
both verify the broker against `<certs>/ca.crt`. The engine's part in that is one
generic fact it gives every agent, `AGENT_NAME`; it holds no setting that so much
as mentions a broker. (A deployment that mounts identities elsewhere can say so
with `SECRET_BROKER_CERT`, `_KEY` and `_CA`.)

Vault holds bytes; the broker owns the authorization layer on top of it. That
split is why revoking a window or editing a policy takes effect on the next call
rather than the next restart, and why nothing in either database can leak a
value.
