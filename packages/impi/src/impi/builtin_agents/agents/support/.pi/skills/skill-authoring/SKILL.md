---
name: skill-authoring
description: Write a pi skill (a SKILL.md capability package) for another agent or for yourself. Use when the operator asks to add a skill, teach an agent a procedure, or package a reusable workflow/script for an agent.
---

# Authoring a skill

A skill is a directory with a `SKILL.md` (required) plus optional `scripts/`,
`references/`, and `assets/`. The runtime shows only the skill's `name` +
`description` to the model up front; the full `SKILL.md` is read on demand
(progressive disclosure), so keep the front matter tight and put the detail in
the body. Skills are driven by the model through the `bash` tool — so the target
agent needs `read` + `bash` in its `runtime.tools`.

This skill is about **writing** one. Installing an existing skill from a
directory or a repository, and handing it to agents, is the **skill-library**
skill.

## 1. Shape on disk

```
<skill>/
  SKILL.md                 # required
  scripts/                 # optional helper scripts the model runs via bash
  references/              # optional docs the model reads on demand
  assets/                  # optional templates/fixtures
```

`SKILL.md` front matter (YAML):

```yaml
---
name: <lowercase-a-z0-9-hyphens>   # required, <= 64 chars; matches the dir name
description: <what it does + WHEN to use it>   # required, <= 1024 chars
# requires_tools: [read, bash]     # optional; checked when the skill is assigned
# version: 1.2.0                   # optional
---
```

Write the `description` so the model knows **when** to reach for the skill (the
trigger), not just what it is — that one line is all it sees before deciding to
open the skill. Then the body: concrete, numbered steps; name any script in
`scripts/` and how to invoke it; keep it operational, not narrative.

`requires_tools` is worth filling in: assigning the skill then warns when the
target agent's allowlist is missing something, instead of the skill installing
cleanly and quietly doing nothing.

## 2. Where it goes

Two homes, and the choice is about reuse, not about content:

- **Private to one agent** — `$AGENTS_PATH/agents/<agent>/.pi/skills/<skill>/`,
  referenced by its bare name. Right for anything specific to that agent.
- **The shared library** — `$SKILLS_PATH/<skill>/`, referenced as
  `registry:<skill>` by any number of agents. Right for something reusable; use
  `install_skill` to put it there rather than writing into the library by hand,
  so its provenance is recorded. See the **skill-library** skill.

**For yourself (support):** `$IMPI_ROOT` is read-only, so you cannot add your own
bundled skills at runtime — propose the `SKILL.md` to the operator to add under
the engine package. You can freely author skills for the user's agents.

## 3. Wire it up

```yaml
runtime:
  tools: [read, bash, ...]        # read + bash are required for skills to run
  skills:
    - <skill>                     # private: .pi/skills/<skill>
    - registry:<skill>            # from the shared library
```

Editing the profile is one way; `assign_skill` does the same edit for library
skills and reloads for you. Apply a hand edit with a reload
(`pkill -HUP -n -f '[i]mpi\.main'`, or ask for `impi reload`).

The profile is the single source of truth — removing the line is how a skill is
turned off. `AGENTS_SKILLS__<AGENT>` in `.env` **replaces** the whole list and
overrides both.

## 4. Good skills

- One capability per skill; a sharp `description` with its trigger.
- Prefer a small script in `scripts/` over prose when the steps are mechanical —
  the model runs it via `bash` and reads its output.
- Reference material needed only sometimes goes in `references/`, not the body,
  so the up-front cost stays low.
- Test it: give the target agent a task that should trigger the skill and
  confirm it opens `SKILL.md` and follows it.
