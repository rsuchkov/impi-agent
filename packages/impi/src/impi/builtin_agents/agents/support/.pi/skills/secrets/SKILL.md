---
name: secrets
description: Help with the secret broker — giving an agent a credential it can use but never read, writing the secret-exec line into its profile, choosing a policy, and working out why a request was refused. Use whenever the operator mentions a token, an API key, a password, a credential, Vault, an approval prompt, or asks how an agent should authenticate to something.
---

# Secrets an agent can use but never read

The engine can hold credentials on an agent's behalf. The agent asks for one by
name, the operator approves it in chat, and the value is injected straight into
the process the agent wanted to run — it never enters the agent's context.

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

Ask the operator for `impi secret status`. Three answers matter:

- **"secrets: off"** — `SECRETS_ENABLED` is false and there is no Vault. Adding
  it to a running deployment: `SECRETS_ENABLED=true` in the config, `IMPI_VAULT=1`
  in `~/.impi/compose.env`, `impi restart`, then `impi secret init` once.
- **"the store is sealed" / "the engine holds no credential"** — normal after a
  restart. `impi secret unlock`. Until then every request is refused.
- **"secrets: open"** — working; move on to the policy.

## Giving an agent a credential

Two separate things, and both are needed. A value with no policy is reachable by
nobody, which is the intended default.

```
impi secret set github-token                      # prompts, never echoes
impi secret policy set github-token \
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
`impi secret set smtp --field username=bot --field password=hunter2`, then
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

- One invocation is about **one** secret. Several fields of it are fine; two
  different secrets nest —
  `secret-exec --env A=vault://one -- secret-exec --env B=vault://two -- cmd`.
- `--reason` is shown to the human deciding. Tell the agent to write a real one.
- The **whole command** is shown on the approval card. That is the operator's
  only defence, so an agent should run the tool it actually needs, not a shell
  that then does something else.
- The agent needs `bash` in its allowlist, and nothing else — `secret-exec` is
  on `PATH` in the container.
- Exit codes: `77` refused, `75` the store is unavailable right now. Nothing
  about a value is ever printed.

## Why a request was refused

The caller is told one identical thing for every authorization outcome — that is
on purpose, so an agent cannot map the store by trying names. The real reason is
only in the ledger:

```
impi secret audit --limit 20
impi secret audit --agent <name>
impi secret audit --secret <name>
```

| decision | what to do about it |
|---|---|
| `no_policy` | the name is wrong, or nothing is configured — `impi secret policy show` |
| `not_permitted` | the agent is not in `--subjects` — re-run `policy set` with it added |
| `denied` | a human said no; ask them why before changing anything |
| `timeout` | nobody clicked in time — see the turn-timeout trap below |
| `no_approver` | `SECRETS_APPROVERS` is empty or names someone unresolvable |
| `locked` / `sealed` | `impi secret unlock` |
| `backend_error` | approved, but the read failed — the secret may not be stored |

**The turn-timeout trap.** The wait for a human blocks a command inside the
agent's turn, and the turn has its own timeout. If turns die while the operator
is still deciding, either lower `SECRETS_APPROVAL_TIMEOUT_S` or raise
`runtime.timeout` in that agent's `agent.yaml`. Say which one you changed.

**No card arrived at all.** Requests go by direct message from the requesting
agent to the first name in `SECRETS_APPROVERS`. That agent needs an account that
can open a direct message; on a gateway without channel administration it
cannot, and the deployment needs `SECRETS_APPROVAL_CHANNEL` instead.

## Windows

`Allow for…` leaves a window open: that agent, that secret, no more questions
until it closes. `impi secret grants` lists them; `impi secret revoke <id>`
closes one immediately, and the very next request asks again. The value is read
fresh every time, so rotating a secret takes effect at once too.

If an operator is being asked too often, the fix is a longer `--max-grant`, not
`--approval never`.

## What this does not protect against

Be honest about this when asked, and do not oversell the feature. The broker
keeps a value out of the model's context, out of the logs and out of the
transcript, and makes every use approved and recorded. It does **not** defend
against someone who has taken over the container: the engine and the agents run
as the same user, and the config file is readable there. `$IMPI_ROOT/docs/secrets.md`
has the full statement — quote it rather than paraphrasing it loosely.
