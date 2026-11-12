"""The account a provider's "who am I" answer names — what an OAuth userinfo
endpoint and a credential provider's own endpoint have in common: a JSON
object whose fields the provider names as it likes, four of which the engine
keeps (``ProviderAccount``) and more it is asked to."""

from collections.abc import Mapping

import httpx

from crucible.ports.connections import ProviderAccount, ProviderUnavailable

# The provider's names for the four fields the engine keeps, unless told otherwise.
ACCOUNT_FIELDS = {"id": "id", "username": "username", "name": "name", "email": "email"}


def account_of(response: httpx.Response, fields: Mapping[str, str], extra: tuple[str, ...]) -> ProviderAccount:
    """The account a "who am I" answer names, by the field names the provider
    uses for the four the engine keeps, with ``extra`` kept beside them."""
    try:
        data = response.json()
        return ProviderAccount(
            id=str(data[fields["id"]]),
            username=str(data.get(fields["username"]) or ""),
            name=str(data.get(fields["name"]) or ""),
            email=str(data.get(fields["email"]) or ""),
            extra={name: data[name] for name in extra if name in data},
        )
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ProviderUnavailable("the account answer does not parse") from exc
