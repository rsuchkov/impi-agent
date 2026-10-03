"""crucible — a reusable agent runtime over the pi coding agent: the runtime
driver, profile loading, chat gateways (Mattermost, Slack), the typed-tool
framework and the interactivity/confirmation machinery. Applications compose
these ports into a bot."""

from importlib import metadata


def _installed_version() -> str:
    # The one place the number lives is the package metadata the release script
    # stamps; a copy here would be the one that drifts.
    try:
        return metadata.version("crucible")
    except metadata.PackageNotFoundError:  # a source tree nothing installed
        return "0+unknown"


__version__ = _installed_version()
