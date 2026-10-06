---
description: Full pre-release review — architecture, docs, security and the end-to-end install, in parallel, with one consolidated report
argument-hint: "[base ref — default: the last release tag]"
---

Run every review this project has over one change, and the end-to-end install,
then hand back one report. Do not edit repository files.

Base: `$ARGUMENTS` if given, else the last release tag
(`git describe --tags --abbrev=0`). Pass the same base to every part.

## 1. Gate

Run `make lint`, `make test`, `make installer-lint`, `make installer-test`. If
any fails, report it first; still run the rest — the reviews are worth having
either way.

## 2. In parallel

Start these at once (subagents for the three reviews, a background shell for
the e2e) and do not duplicate their work yourself:

- **Architecture** — a subagent that reads `.claude/commands/arch-review.md`
  and follows it with the base above.
- **Docs** — a subagent that reads `.claude/commands/docs-review.md` and follows
  it with the base above.
- **Security** — the `security-review` skill (invoke it through the Skill
  tool), over the same change. Tell it the threat model is written down in
  `docs/security.md` and `SECURITY.md`: a finding that page already lists as a
  known limit is not new, one that contradicts what the page says holds is.
- **End to end** — the throwaway compose install with every axis that can run
  locally:
  1. Remove leftovers of an earlier run first, or Mattermost never gets healthy:
     containers, volumes and networks whose names start with `impi-e2e`
     (`podman ps -a --filter label=com.docker.compose.project=impi-e2e`,
     `podman volume ls -q | grep '^impi-e2e_'`, `podman network ls`), and
     `/tmp/impi-e2e.*`. Touch nothing else.
  2. `AGENTS=1 BROWSER=1 E2E_LLM=1 E2E_PI_AUTH=$HOME/.pi/agent/auth.json make e2e-install`
     (drop `E2E_LLM`/`E2E_PI_AUTH` if there is no pi login on this machine and
     say so). It takes 15–20 minutes; wait for it to finish.
  3. On a failure, re-run once with `KEEP=1` and read the engine's and the
     agent's logs (`IMPI_HOME=<dir> ~/.local/bin/impi logs`, `podman logs`)
     before calling it a defect. A check that depends on what the model chose
     to answer is reported as such, not as a code defect. Remove the kept stack
     afterwards, the same way as in step 1.

## 3. Report

One consolidated report:

- **Gate** — the four commands, pass/fail, with failing output.
- **End to end** — passed/failed counts, every failed check with its cause
  (defect, environment, or model behaviour) and the evidence.
- **Findings** — the three reviews merged and de-duplicated, ranked
  must-fix first: `file:line` — what — why — smallest fix — which review found
  it.
- **Release readiness** — one sentence: what blocks a release, if anything.
