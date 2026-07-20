"""Agent profiles: the hybrid format from the agents directory — agent.yaml
(machine settings) + a personality/system-prompt half — loaded into the neutral
:class:`crucible.ports.agent.AgentSpec`. Runtime-agnostic: a concrete runtime maps the spec
onto its own profile, not here."""

from crucible.profiles.base import ProfileStore
from crucible.profiles.composite import CompositeProfileStore
from crucible.profiles.errors import ProfileError
from crucible.profiles.loader import FsProfileStore

__all__ = ["CompositeProfileStore", "FsProfileStore", "ProfileError", "ProfileStore"]

