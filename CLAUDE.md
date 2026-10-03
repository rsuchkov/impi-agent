# impi

See [AGENTS.md](AGENTS.md) for project overview, commands, and development
principles. It is the single source of truth for working in this repository.

One rule repeated here because it gates an action rather than describing one:
**before cutting a MINOR or MAJOR release, review the bundled `support` agent**
— its tool allowlist, `SYSTEM.md` and skills — against what is shipping. The
checklist is in [AGENTS.md](AGENTS.md#commits-and-changes).

**Backward compatibility is not owed before 1.0.** While the major version is
0 — the release in progress included — a change may break compatibility
(a renamed setting, a changed key, a dropped shim) rather than carry a legacy
alias for it: a crutch kept for a version that never had a stable contract is
worse than the break. Two conditions: discuss each specific break with the
maintainer **before** making it, and say what breaks in `CHANGELOG.md`.
