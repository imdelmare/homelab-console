"""Narrow client for the official ZeroTier Central Legacy v1 API.

The API origin and endpoint paths are fixed. Network IDs come only from the
server-side inventory, so callers cannot turn this provider into an arbitrary
HTTP client or enumerate undeclared networks.
"""

import re
from typing import Any

from app.providers.errors import ProviderError
from app.providers.httpclient import BaseJsonClient
from app.services.inventory import provider_config
from app.services.secrets import get_provider_secrets

ZEROTIER_LEGACY_BASE_URL = "https://api.zerotier.com/api/v1"
NETWORK_ID_PATTERN = re.compile(r"^[0-9a-f]{16}$")
MEMBER_ID_PATTERN = re.compile(r"^(?:[0-9a-f]{10}|[0-9a-f]{16}-[0-9a-f]{10})$")


class ZeroTierClient(BaseJsonClient):
    provider_id = "zerotier"

    def __init__(self) -> None:
        super().__init__()
        config = provider_config("zerotier")
        secrets = get_provider_secrets("zerotier")

        self.base_url = ZEROTIER_LEGACY_BASE_URL
        self.verify_tls = True
        self.timeout_seconds = _bounded_float(
            config.get("timeout_seconds"), default=8.0, minimum=0.5, maximum=30.0
        )
        self.offline_after_seconds = _bounded_float(
            config.get("offline_after_seconds"),
            default=600.0,
            minimum=60.0,
            maximum=86400.0,
        )
        self.api_token = str(secrets.get("api_token") or "").strip()
        self.headers = (
            {"Authorization": f"token {self.api_token}"} if self.api_token else {}
        )
        self.network_ids = _network_ids(config.get("network_ids"))
        self.required_online_member_ids = _member_ids(
            config.get("required_online_member_ids")
        )

    def is_configured(self) -> bool:
        return bool(self.network_ids)

    def has_credentials(self) -> bool:
        return bool(self.api_token)

    def credentials_error(self) -> str:
        return "zerotier api_token is not configured"

    def declared_network_ids(self) -> tuple[str, ...]:
        return self.network_ids

    def require_network_ids(self) -> tuple[str, ...]:
        if not self.network_ids:
            raise ProviderError(
                "configuration_missing",
                "no ZeroTier network_ids are configured",
            )
        if not self.has_credentials():
            raise ProviderError("credentials_missing", self.credentials_error())
        return self.network_ids

    def _declared_network_id(self, network_id: str) -> str:
        normalized = str(network_id).strip().lower()
        if normalized not in self.network_ids:
            raise ProviderError(
                "configuration_missing",
                "zerotier network is not declared in the provider configuration",
            )
        return normalized

    async def account_status(self) -> Any:
        return await self.get("/status")

    async def network(self, network_id: str) -> Any:
        declared = self._declared_network_id(network_id)
        return await self.get(f"/network/{declared}")

    async def members(self, network_id: str) -> Any:
        declared = self._declared_network_id(network_id)
        return await self.get(f"/network/{declared}/member")


def _network_ids(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ProviderError(
            "configuration_missing", "zerotier network_ids must be a list"
        )
    normalized: list[str] = []
    for item in value:
        network_id = str(item).strip().lower()
        if not NETWORK_ID_PATTERN.fullmatch(network_id):
            raise ProviderError(
                "configuration_missing",
                "zerotier network_ids must contain 16-character hexadecimal IDs",
            )
        if network_id not in normalized:
            normalized.append(network_id)
    return tuple(normalized)


def _member_ids(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ProviderError(
            "configuration_missing",
            "zerotier required_online_member_ids must be a list",
        )
    normalized: list[str] = []
    for item in value:
        member_id = str(item).strip().lower()
        if not MEMBER_ID_PATTERN.fullmatch(member_id):
            raise ProviderError(
                "configuration_missing",
                "zerotier required_online_member_ids contains an invalid member ID",
            )
        if member_id not in normalized:
            normalized.append(member_id)
    return tuple(normalized)


def _bounded_float(
    value: Any, *, default: float, minimum: float, maximum: float
) -> float:
    if value in (None, ""):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ProviderError(
            "configuration_missing", "zerotier numeric configuration is invalid"
        ) from None
    if not minimum <= number <= maximum:
        raise ProviderError(
            "configuration_missing",
            f"zerotier numeric configuration must be between {minimum:g} and {maximum:g}",
        )
    return number
