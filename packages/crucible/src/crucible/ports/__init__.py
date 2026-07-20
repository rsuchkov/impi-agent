"""Neutral port layers — the two pure vocabularies the middle of the system
depends on, one per side of the hexagon:

- ``chat``  — the platform/gateway side (messages, conversations, widgets); chat
  gateway adapters (Mattermost, Slack) implement these ports.
- ``agent`` — the runtime side (turns, sessions, profiles, results, UI bridge); a
  concrete runtime implements these ports.

Both are dependency-free leaves; neither imports the other. Ports that are
co-located with their implementation (store, profiles, tools) live in their own
packages, not here."""
