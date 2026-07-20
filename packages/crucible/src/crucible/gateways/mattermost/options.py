"""MATTERMOST_URL -> mattermostautodriver options dict (scheme/url/port split)."""

from urllib.parse import urlparse


def driver_options(mattermost_url: str, token: str, *, verify: bool = True) -> dict:
    parsed = urlparse(mattermost_url)
    if not parsed.scheme or not parsed.hostname:
        raise ValueError(
            f"MATTERMOST_URL must look like http(s)://host[:port], got {mattermost_url!r}"
        )
    scheme = parsed.scheme
    port = parsed.port or (443 if scheme == "https" else 80)
    return {
        "scheme": scheme,
        "url": parsed.hostname,
        "port": port,
        "token": token,
        "verify": verify,
        # Reconnect the WS loop on drops instead of returning.
        "keepalive": True,
        "keepalive_delay": 5,
    }
