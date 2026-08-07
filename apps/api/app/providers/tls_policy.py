"""Central TLS verification policy for inventory-declared providers."""

import logging
from ipaddress import ip_address
from urllib.parse import urlsplit

from app.core.settings import get_settings
from app.providers.errors import ProviderError

logger = logging.getLogger("homelab.providers")
_warned_providers: set[str] = set()


def _is_local_endpoint(base_url: str) -> bool:
    hostname = urlsplit(base_url).hostname
    if not hostname:
        return False
    if hostname.lower() == "localhost":
        return True
    try:
        address = ip_address(hostname)
    except ValueError:
        return False
    return address.is_private or address.is_loopback


def enforce_tls_policy(*, provider_id: str, base_url: str, verify_tls: bool) -> None:
    if verify_tls:
        return
    settings = get_settings()
    if settings.is_live and not settings.allow_insecure_local_tls:
        raise ProviderError(
            "configuration_missing",
            f"TLS verification is disabled in the {provider_id} configuration; "
            "set ALLOW_INSECURE_LOCAL_TLS=true only for a trusted local endpoint",
        )
    if settings.is_live and not _is_local_endpoint(base_url):
        raise ProviderError(
            "configuration_missing",
            f"TLS verification is disabled in the {provider_id} configuration; "
            "the exception is restricted to private IP endpoints",
        )
    if provider_id not in _warned_providers:
        logger.warning(
            "%s TLS verification is DISABLED for a local endpoint. "
            "Certificate identity is not being verified.",
            provider_id,
        )
        _warned_providers.add(provider_id)
