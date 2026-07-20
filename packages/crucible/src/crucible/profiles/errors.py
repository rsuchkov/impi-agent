"""Errors raised while loading or composing agent profiles — shared by every
profile store so a caller (e.g. the reloader) catches one type."""


class ProfileError(Exception):
    """A profile source is malformed (e.g. bad agent.yaml), or a requested agent
    is missing / duplicated across sources."""
