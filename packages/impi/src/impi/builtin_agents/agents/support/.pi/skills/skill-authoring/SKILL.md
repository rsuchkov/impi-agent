---
name: skill-authoring
description: Write a pi skill (a SKILL.md capability package) for another agent or for yourself. Use when the operator asks to add a skill, teach an agent a procedure, or package a reusable workflow/script for an agent.
---

# Authoring a skill

A skill is a directory with a `SKILL.md` (required) plus optional `scripts/`,
`references/`, and `assets/`. The runtime shows only the skill's `name` +
`description` to the model up front; the full `SKILL.md` is read on demand
(progressive disclosure), so keep the front matter tight and put the detail in the
body. Skills are driven by the model through the `bash` tool — so the target agent
needs `read` + `bash` in its `runtime.tools`.

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
# allowed-tools: [read, bash]      # optional; narrows what the skill may call
# metadata: { ... }                # optional free-form
---
```

Write the `description` so the model knows **when** to reach for the skill (the
trigger), not just what it is — that one line is all it sees before deciding to
open the skill. Then the body: concrete, numbered steps; name any script in
`scripts/` and how to invoke it; keep it operational, not narrative.

## 2. Where to put it, and wire it up

- **For another agent:** `$AGENTS_PATH/agents/<agent>/.pi/skills/<skill>/`.
- **For yourself (support):** `$IMPI_ROOT` is read-only, so you cannot add your own
  bundled skills at runtime — propose the `SKILL.md` to the operator to add under
  the engine package. You CAN freely author skills for the user's agents under
  `$AGENTS_PATH`.

Then enable it in that agent's `agent.yaml`:

```yaml
runtime:
  tools: [read, bash, ...]     # read + bash are required for skills to run
  skills: [<skill>, ...]       # a bare name resolves to .pi/skills/<skill>
```

Apply with a **reload** (`pkill -HUP -n -f '[i]mpi\.main'` or `make reload`).
The operator can also toggle skills without editing `agent.yaml` via
`AGENTS_SKILLS__<AGENT>` (CSV; empty = none, unset = the agent.yaml list).

## 3. Good skills

- One capability per skill; a sharp `description` with its trigger.
- Prefer a small script in `scripts/` over prose when the steps are mechanical —
  the model runs it via `bash` and reads its output.
- Reference material the model needs only sometimes goes in `references/`, not the
  body, so the up-front cost stays low.
- Test it: give the target agent a task that should trigger the skill and confirm
  it opens `SKILL.md` and follows it.
