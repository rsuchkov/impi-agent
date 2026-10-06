---
description: Review whether the docs, the changelog, .env.example and the support agent's skills still say what the code does — findings only, no edits
argument-hint: "[base ref — default: the last release tag]"
---

Review the documentation of a change for accuracy. Do not edit files; report
findings.

The docs are not only for people: the bundled `support` agent reads
`$IMPI_ROOT/docs` at run time and answers operators from it, so a stale
sentence becomes a wrong answer in chat.

## Scope

- Base: `$ARGUMENTS` if given, else the last release tag
  (`git describe --tags --abbrev=0`).
- The change is `git diff <base>` (commits since the base and the working tree)
  plus untracked files.
- Rules: `AGENTS.md` (*Commits and changes*: the CHANGELOG rule and the
  pre-release `support` checklist), `CLAUDE.md` (breaks are named in the
  changelog).

## What to check

Cite `file:line` for every finding and say what the text should say instead.

1. **`CHANGELOG.md` → `## Unreleased` against the code.**
   - Every setting, command, default, URL shape and file path it names exists
     with that name and that default (`grep` the code).
   - Every user-visible change in the diff has a bullet (features, fixes,
     behaviour changes; not internal refactors).
   - Every break is marked **Breaks:** and says what to do. Hunt for breaks the
     changelog omits: removed or renamed settings and env vars, changed URL or
     path shapes, changed defaults, refused input that used to be accepted, a
     file or volume that moved, a command that must now be re-run.
2. **Settings ↔ docs ↔ `.env.example`.** Every settings field added or changed
   (`crucible/config.py`, `impi/config.py`, `ward/config.py`) appears in
   `docs/configuration.md` with the right default, and — if an operator would
   set it — in `.env.example`. Nothing documented there has disappeared from the
   code.
3. **Stale statements in `docs/*.md`, `README.md`, `packages/*/README.md`.**
   For every name, path, key or behaviour the diff removed or changed, grep the
   docs for the old one. Check examples (commands, URLs, YAML) still work as
   written.
4. **Indexes.** `docs/README.md` and `README.md` link every page in `docs/`;
   new pages are listed with a one-line description.
5. **The `support` agent** (`packages/impi/src/impi/builtin_agents/agents/support/`):
   `agent.yaml` allowlist, `.pi/SYSTEM.md`, every `.pi/skills/*/SKILL.md`.
   Ask the three questions from AGENTS.md:
   1. Does a new capability need a tool in its allowlist, or a new/updated skill?
   2. Did anything it describes change — a path, a setting, a command, a default?
   3. Does a doc it points at still say what the skill claims it says?
   Also: does every new operator-facing symptom (a new refusal, a new log line,
   a new `impi health`/`impi doctor` line) have an entry an operator's question
   would find?
6. **Code comments the support agent will read as truth** — module docstrings
   in the diff that describe behaviour: do they match the code?
7. **Writing rules.** English only. Neutral example names (`assistant`,
   `alice`, `<agent>`), no personal data, no real hosts, IPs or tokens from a
   stand. Docs describe the current state, not the history of how it got there
   (that is the changelog's job).

## Output

A numbered list, grouped as *Changelog*, *Settings/.env.example*, *Docs*,
*Support agent*, *Comments*. Each finding: `file:line` — the wrong or missing
statement — what it should say. Then one short paragraph of what you checked
and found accurate.
