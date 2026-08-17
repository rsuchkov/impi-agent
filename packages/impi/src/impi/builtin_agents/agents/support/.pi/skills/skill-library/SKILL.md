---
name: skill-library
description: Install, assign, update and remove skills in the shared library ($SKILLS_PATH). Use when the operator asks to install a skill from a repository or folder, give an existing skill to an agent, see what skills exist, or take one away.
---

# The shared skill library

One directory (`$SKILLS_PATH`, `/app/skills` in a container), one skill per
subdirectory. A library skill is referenced as `registry:<name>` and can go to
any number of agents; a skill inside one agent's profile is private to it and
referenced by its bare name. Writing a new skill is the **skill-authoring**
skill; this one is about distribution.

**The directory name is the skill's identity.** A `name:` in the front matter
that disagrees is ignored — a skill installed under a different name must not
answer to two.

Reference: `$IMPI_ROOT/docs/skills.md`.

## 1. See what is there

`list_skills` — every installed skill, its description, and which agents have
it. Start here; installing something that already exists under another name is
the usual mistake.

## 2. Install

`install_skill(source, name?, force?)` where `source` is:

- a directory on disk — `/app/agents/_staging/my-skill`;
- `owner/repo[/path][@ref]` — `anthropics/skills/pdf@v2`;
- a git URL — `https://git.example.com/s.git#skills/greek@main`.

The operator confirms before anything is copied, and the confirmation lists
**every file, its size, and which are executable**. Say plainly what you are
about to install and from where — a skill's scripts run inside the engine with
the agent's tools, so this is running someone else's code on the operator's
machine. Only public repositories work; no credentials are passed to git.

A git install is pinned to the exact commit, recorded in `.skill-source.json`
beside the skill.

`name` installs under a different name; `force` overwrites an existing one —
use it only when the operator asked to replace that skill.

Installing gives the skill to **nobody**. It is a two-step on purpose.

## 3. Give it to an agent

`assign_skill(skill, agent)` — edits that agent's `agent.yaml` (comments and
layout survive) to add `registry:<skill>`, and reloads so it applies on the
agent's next turn. `assign_skill(skill, agent, remove=true)` takes it away.

Removing the line is how a skill is turned off — there is no separate
enabled/disabled state. If the skill declares `requires_tools` the agent does
not have, the result says so: fix the agent's `runtime.tools` (skills always
need at least `read` + `bash`), otherwise the skill installs cleanly and does
nothing.

If `AGENTS_SKILLS__<AGENT>` is set in `.env`, it **replaces** that agent's whole
list and your assignment will not show up. Tell the operator rather than
fighting it.

## 4. Update and remove

There is no update tool — the CLI does it, and it shows the old and new commit
before replacing anything:

```bash
impi skill update <name>     # re-fetch from the recorded source
impi skill show <name>
```

`remove_skill(name)` deletes it from the library, with a confirmation. It
refuses while an agent still has it assigned — unassign first, so an agent is
never left pointing at a skill that no longer exists.

## 5. The operator's own paths

They may prefer doing this themselves; these surfaces do the same thing:

- `impi skill list|show|install|update|remove|assign` from a terminal;
- `/skills` in chat — a browser with the same install/assign actions, answered
  by the engine with no agent turn involved;
- asking an agent that has the `open_screen` tool to show the skills, which
  opens that same browser where the conversation is.
