---
description: Review a change against the project's architecture rules (layers, ports, neutrality, naming) — findings only, no edits
argument-hint: "[base ref — default: the last release tag]"
---

Review the architecture of a change in this repository. Do not edit files;
report findings.

## Scope

- Base: `$ARGUMENTS` if given, else the last release tag
  (`git describe --tags --abbrev=0`).
- The change is `git diff <base>` — committed work since the base AND the
  working tree — plus untracked files (`git ls-files --others --exclude-standard`).
- Read the rules first: `AGENTS.md` (sections *Development principles*,
  *Testing*, *Commits and changes*) and `CLAUDE.md`. They are the source of
  truth; this command only says how to apply them.

## Run first

- `make lint` — ruff, import-linter (the layer contracts in `pyproject.toml`),
  `scripts/check_names.py`, `scripts/check_facades.py`, pyright. Report any
  failure verbatim. A green lint is the floor, not the review.
- `make test`.

## What to check

For each item, look at the diff AND the code around it; cite `file:line`.

1. **Layers and ports.** Dependencies point inward through ports (Protocols in
   `crucible/ports/`). A backend's specifics live in one adapter. Composition
   happens in `impi/app.py` (or the application's own root), wiring by
   constructor injection — no globals, no service locators, no module-level
   singletons that a test cannot replace.
   - Does every NEW module fall under an existing import-linter contract? If it
     is a new layer or leaf, does it need a contract of its own (`pyproject.toml`
     `[[tool.importlinter.contracts]]`)? Name the contract it should join.
   - A new edge between layers: is it in the direction the contracts intend, or
     does it only pass because no contract covers it yet?
   - A cycle avoided by an import inside a function is a dependency pointing the
     wrong way: report it (ruff `PLC0415` catches the syntax, not the design).
2. **Runtime-neutral core.** The neutral layers (`ports`, `flows`, `tools`,
   `interactions`, `store`, `profiles`, `gateways`, `scheduler`, `approvals`,
   `skills`, `attachments`, `containment`) name no concrete runtime — not in
   comments, docstrings, names or strings. `pi` belongs in `runtimes/pi/` and
   the composition root. `check_names.py` catches the word; you catch the
   concept (a neutral module that knows a pi file layout, a pi flag, a pi event
   name under another name).
3. **The library names no application.** Nothing under `crucible` knows
   `impi`, `ward`, `wardline`, `runtime-relay` — including in messages it logs
   and advice it prints. A list of an application's settings or key families in
   the library is the same leak in data form.
4. **No facades.** No module of nothing but imports (`__init__.py` aside).
   Import from where a thing lives.
5. **Names** (AGENTS.md: *A name says what the thing does, and one name means
   one thing*). List every name the change adds:
   `git diff -U0 <base> | grep -E '^\+\s*(async )?def |^\+class |^\+[A-Z_]{3,} = '`,
   plus new files and new settings fields. For each: does it say what the thing
   does or returns? Does the same word already mean something else in the repo
   (`grep -rn '\b<word>\b' packages`)? Flag catch-alls (`_guard`, `_open`,
   `problems`, `_BASE_*`), a function returning a reason-or-empty instead of a
   `check_*` that raises, a module named broader than its content. Service
   instances end in `_svc`.
6. **Settings.** New knobs are flat env names on the settings model (not
   nested `X__Y`), have a default that keeps an existing deployment working or
   a CHANGELOG **Breaks:** line, and are validated where a bad value would fail
   late. Per-agent overrides follow the existing `AGENTS_<KIND>__<AGENT>` shape.
   The one other family is `RUNTIME_ENV__<AXIS>` (compose overlays cannot merge
   one list); a new family needs the same reason, written down in AGENTS.md.
7. **Breaking changes before 1.0** (CLAUDE.md). A break needs (a) evidence it
   was discussed with the maintainer and (b) a **Breaks:** clause in
   `CHANGELOG.md` *Unreleased*. No legacy aliases or shims kept "for
   compatibility".
8. **Tests.** Every behaviour change has a test that fails without it (ask:
   which single-line mutation of the change would this test catch?). Tests stay
   offline. A test that asserts the exact text of something user-visible is
   fine; one that pins an implementation detail nobody relies on is not.
9. **English only** in code, strings, logs, comments, docstrings (exceptions:
   marked non-ASCII test data, an agent's own `SYSTEM.md`).
10. **Docs move with the code** — a changed behaviour, setting or command is
    reflected in `docs/` in the same change (detail: `/docs-review`).

## Output

A numbered list, most important first. Each finding: `file:line` — the rule it
breaks — what is wrong — why it matters — the smallest fix. Mark each
**must-fix** (breaks a stated rule or a contract) or **should-fix** (drift,
naming, clarity). Then one short paragraph of what you checked and found clean.
If the change is large, split the review by package with subagents and merge
their findings, de-duplicated.
