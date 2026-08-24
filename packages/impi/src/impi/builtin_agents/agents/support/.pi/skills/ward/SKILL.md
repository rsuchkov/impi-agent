---
name: ward
description: Help with ward, the secret broker — giving an agent a credential it can use but never read, writing the secret-exec line into its profile, choosing a policy, and working out why a request was refused. Use whenever the operator mentions a token, an API key, a password, a credential, Vault, an approval prompt, or asks how an agent should authenticate to something.
---

# Secrets an agent can use but never read

A deployment can hold credentials on an agent's behalf — in the broker, which
runs in its own container and not in the engine. The agent asks for one by name,
the operator approves it in chat, and the value is injected straight into the
process the agent wanted to run; it never enters the agent's context.

Reference: `$IMPI_ROOT/docs/secrets.md`. Read it before answering anything
detailed; this skill is the operating procedure, not the whole story.

## What you cannot do here

You have no access to the broker yourself. There is no tool that reads a value,
and `secret-exec` is not in your allowlist. That is deliberate — say so plainly
rather than trying, and never suggest a workaround that puts a value in a chat
message, a profile file or a `.env` key "just for now".

Everything below is something you either **tell the operator to run** or
**write into another agent's profile**.

## Is it even on?

Ask the operator for `impi ward status`. Three answers matter:

- **no broker in this deployment** — the store was never enabled, so there is no
  address to ask at. Turning it on in a deployment that already runs takes
  several steps in a particular order (the broker's env file has to exist before
  the stack will come up); send the operator to the "Turning it on in a
  deployment that already runs" section of `$IMPI_ROOT/docs/secrets.md` rather
  than improvising it.
- **sealed, or the broker holds no credential** — normal after a restart, every
  one of them. `impi ward unlock --from ~/.impi/ward-recovery.txt` (that file is
  what the ceremony wrote; without `--from` it prompts, which means a key typed
  into a terminal). Until then every request is refused.
- **"secrets: open"** — working; move on to the policy.

## Giving an agent a credential

Two separate things, and both are needed. A value with no policy is reachable by
nobody, which is the intended default.

```
impi ward set github-token                      # prompts, never echoes
impi ward policy set github-token \
    --subjects <agent> --approval always --max-grant 15m
```

Guidance worth giving on the policy:

- `--subjects` is an allowlist of agent names, comma-separated. Name only the
  agents that actually need it.
- `--approval always` asks a human each time (unless a window is open).
  `--approval never` asks nobody, ever — right for a key a scheduled task needs
  at 3am, wrong for anything that can act on the operator's behalf outside.
- `--max-grant` caps the "leave it open for a while" answer. `0` means every
  single use is asked about. Suggest the shortest window that fits the job:
  minutes for one deploy, an hour only for a long session of work.

A secret with several fields is one secret:
`impi ward set smtp --field username=bot --field password=hunter2`, then
`vault://smtp#username` and `vault://smtp#password`.

## Teaching an agent to use it

Put the exact command in the agent's `.pi/SYSTEM.md` or in a skill. The agent
cannot discover what exists — there is no listing, by design — so if it is not
written down, the agent cannot use it.

```
To publish a release, run:
  secret-exec --env GITHUB_TOKEN=vault://github-token \
              --reason "<why, in a few words>" -- gh release create <tag>
```

Points to get right when you write one of these:

- One invocation may name several secrets (up to five). They are served
  together or not at all, one card covers them all, and the window offered is
  the shortest any of their policies allows.
- `--reason` is shown to the human deciding. Tell the agent to write a real one.
- The **whole command** is shown on the approval card. That is the operator's
  only defence, so an agent should run the tool it actually needs, not a shell
  that then does something else.
- The agent needs `bash` in its allowlist, and an identity: `impi ward cert
  <agent>` mints one, and the agent picks it up on the next restart. The tool
  finds it by the agent's own name, so the certificate has to be named after the
  agent — which `impi ward cert` does. Without one `secret-exec` cannot prove who
  it is and exits 78.
- Exit codes: `77` refused, `75` the store is unavailable right now. Nothing
  about a value is ever printed.

## Why a request was refused

The caller is told one identical thing for every authorization outcome — that is
on purpose, so an agent cannot map the store by trying names. The real reason is
only in the ledger:

```
impi ward audit --limit 20
impi ward audit --agent <name>
impi ward audit --secret <name>
```

| decision | what to do about it |
|---|---|
| `no_policy` | the name is wrong, or nothing is configured — `impi ward policy show` |
| `not_permitted` | the agent is not in `--subjects` — re-run `policy set` with it added |
| `denied` | a human said no; ask them why before changing anything |
| `timeout` | nobody clicked in time — see the turn-timeout trap below |
| `no_approver` | `WARD_APPROVERS` is empty or names someone unresolvable |
| `locked` / `sealed` | `impi ward unlock --from ~/.impi/ward-recovery.txt` |
| `backend_error` | approved, but the read failed — the secret may not be stored |

**The turn-timeout trap.** The wait for a human blocks a command inside the
agent's turn, and the turn has its own timeout. If turns die while the operator
is still deciding, either lower `WARD_APPROVAL_TIMEOUT_S` or raise
`runtime.timeout` in that agent's `agent.yaml`. Say which one you changed.

**No card arrived at all.** Cards are posted by the broker's **own** chat
account — not by the agent that asked — as a direct message to the first name in
`WARD_APPROVERS`. So check three things: that the account exists at all
(`WARD_MATTERMOST_TOKEN` in the broker's `conf/ward.env`), that the approver
resolves, and that the account can open a direct message. On a gateway without
channel administration it cannot, and the deployment needs
`WARD_APPROVAL_CHANNEL` instead.

**The card arrives and the buttons do nothing.** The click goes from the chat
platform to the broker's receiver, and Mattermost will not call an address
missing from `AllowedUntrustedInternalConnections`. `ward` has to be in it.

## Windows

`Allow for…` leaves a window open: that agent, that secret, no more questions
until it closes. `impi ward grants` lists them; `impi ward revoke <id>`
closes one immediately, and the very next request asks again. The value is read
fresh every time, so rotating a secret takes effect at once too.

If an operator is being asked too often, the fix is a longer `--max-grant`, not
`--approval never`.

## Never handle the material yourself

The unseal key and the broker's credential live in `~/.impi/ward-recovery.txt`
on the operator's host, which no container mounts — you cannot read it, and you
must not ask for its contents to be pasted to you. Everything you need is a
command the operator runs: `impi ward unlock --from …` opens the store,
`impi ward rotate` replaces the credential if one leaks. If an operator offers
to paste a key, say plainly that it belongs in their password manager and that
a key in a chat message is a key in the transcript.

## What this does not protect against

Be honest about this when asked, and do not oversell the feature. The broker
keeps a value out of the model's context, out of the logs and out of the
transcript, and makes every use approved and recorded. It runs in its own
container, so taking over the engine no longer means taking the secrets — but it
does still mean being able to *ask* as any agent, which is why the card matters.
Within an agent's own container there is no separation at all. `$IMPI_ROOT/docs/secrets.md`
has the full statement — quote it rather than paraphrasing it loosely.
