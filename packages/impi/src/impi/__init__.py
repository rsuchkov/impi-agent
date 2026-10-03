"""impi — a multi-agent Mattermost assistant built on crucible. Adds the agent
registry, cascade guard, engine-owned `support` agent and chat-management tools."""

from importlib import metadata


def _installed_version() -> str:
    try:
        return metadata.version("impi")
    except metadata.PackageNotFoundError:  # a source tree nothing installed
        return "0+unknown"


__version__ = _installed_version()
