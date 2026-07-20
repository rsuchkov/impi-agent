"""CompositeProfileStore: merge several profile stores into one.

Lets an application load its own engine-bundled agents alongside the user's agents
repo, presented as ONE store to the composition root and the reloader. Agent names
must be unique across sources.
"""

from crucible.ports.agent import AgentSpec
from crucible.profiles.base import ProfileStore
from crucible.profiles.errors import ProfileError


class CompositeProfileStore:
    """Merge several ``ProfileStore``s into one, rejecting duplicate agent names."""

    def __init__(self, stores: list[ProfileStore]) -> None:
        self._stores = stores
        self._reject_duplicate_names()

    def reload(self) -> None:
        for store in self._stores:
            store.reload()
        self._reject_duplicate_names()

    def list(self) -> list[AgentSpec]:
        return [spec for store in self._stores for spec in store.list()]

    def get(self, name: str) -> AgentSpec:
        for store in self._stores:
            try:
                return store.get(name)
            except ProfileError:
                continue
        raise ProfileError(f"Unknown agent {name!r} in any profile source")

    def _reject_duplicate_names(self) -> None:
        seen: set[str] = set()
        for store in self._stores:
            for spec in store.list():
                if spec.name in seen:
                    raise ProfileError(
                        f"duplicate agent name {spec.name!r} across profile sources"
                    )
                seen.add(spec.name)
