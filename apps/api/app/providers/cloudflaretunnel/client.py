"""Configured Cloudflare Tunnel API client."""

from typing import Any

from app.providers.cloudflare_api import CloudflareApiClient
from app.providers.errors import ProviderError
from app.services.inventory import provider_config
from app.services.secrets import get_provider_secrets


class CloudflareTunnelClient(CloudflareApiClient):
    provider_id = "cloudflaretunnel"

    def __init__(self) -> None:
        config = provider_config(self.provider_id)
        secrets = get_provider_secrets(self.provider_id)
        tunnel_ids = _configured_tunnel_ids(config.get("tunnel_ids"))
        super().__init__(
            provider_id=self.provider_id,
            account_id=str(config.get("account_id") or ""),
            tunnel_ids=tunnel_ids,
            bearer_token=str(secrets.get("bearer_token") or ""),
            timeout_seconds=_timeout(config.get("timeout_seconds")),
        )


def _configured_tunnel_ids(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ProviderError(
            "configuration_missing", "cloudflaretunnel tunnel_ids must be a list"
        )
    return tuple(str(item) for item in value)


def _timeout(value: Any) -> float:
    if value in (None, ""):
        return 8.0
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        raise ProviderError(
            "configuration_missing", "cloudflaretunnel timeout_seconds is invalid"
        ) from None
    if not 0.5 <= timeout <= 30:
        raise ProviderError(
            "configuration_missing",
            "cloudflaretunnel timeout_seconds must be between 0.5 and 30",
        )
    return timeout
