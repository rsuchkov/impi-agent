# Security policy

## Reporting a vulnerability

Report privately, not in a public issue: use **Report a vulnerability** on the
repository's Security tab (GitHub private vulnerability reporting). If that is
not available to you, open an issue that says only "security — please contact
me" and leave the details out; a maintainer will reach you.

Include what you can: the version (`VERSION`, or `impi doctor`), the deployment
shape (default, a container per agent, the broker on or off), and the steps.
A proof of concept is welcome; running it against somebody else's deployment
is not.

You will get an acknowledgement within a week. Fixes ship as a release with a
`CHANGELOG.md` entry that names the issue; credit is given unless you ask
otherwise.

## Supported versions

The current minor release line only. The major version is 0: a fix may change
behaviour or a setting rather than carry a compatible shim, and the changelog
says what changed.

## What counts

The engine's threat model is written down in [`docs/security.md`](docs/security.md).
In scope is anything that crosses a boundary that page says holds: a tool
running for an agent that was not given it, a confirmation that can be
answered without a human, a credential value reaching the model or the
inventory, one agent's container reaching another's, a door the engine opens
answering without its token.

Out of scope is what that page lists as a known limit — a neighbour process
reading another's environment inside one container, egress from an agent's
shell, the shared browser — and the runtime itself (`pi`), which has its own
reporting channel upstream.
