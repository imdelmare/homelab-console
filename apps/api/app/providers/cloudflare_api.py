"""Shared narrow transport for Cloudflare Tunnel read operations.

The API origin, TLS policy and endpoint shapes are fixed. Callers may only
query tunnel UUIDs declared when the client is constructed.
"""

from typing import Any
from uuid import UUID

import httpx

from app.providers.errors import ProviderError
from app.providers.httpclient import BaseJsonClient

CLOUDFLARE_API_BASE_URL = "https://api.cloudflare.com/client/v4"


class CloudflareApiClient(BaseJsonClient):
    def __init__(
        self,
        *,
        provider_id: str,
        account_id: str,
        tunnel_ids: tuple[str, ...],
        bearer_token: str,
        timeout_seconds: float,
    ) -> None:
        super().__init__()
        self.provider_id = provider_id
        self.base_url = CLOUDFLARE_API_BASE_URL
        self.verify_tls = True
        self.timeout_seconds = timeout_seconds
        self.account_id = _account_id(account_id)
        self.tunnel_ids = _tunnel_ids(tunnel_ids)
        self._token = bearer_token.strip()
        if self._token:
            self.headers = {"Authorization": f"Bearer {self._token}"}

    def is_configured(self) -> bool:
        return bool(self.account_id and self.tunnel_ids)

    def has_credentials(self) -> bool:
        return bool(self._token)

    def credentials_error(self) -> str:
        return f"{self.provider_id} bearer_token is not configured"

    def declared_tunnel_ids(self) -> tuple[str, ...]:
        if not self.account_id or not self.tunnel_ids:
            raise ProviderError(
                "configuration_missing",
                f"{self.provider_id} account_id and tunnel_ids are required",
            )
        if not self.has_credentials():
            raise ProviderError("credentials_missing", self.credentials_error())
        return self.tunnel_ids

    def _error_detail(self, response: httpx.Response) -> str:
        detail = super()._error_detail(response)
        try:
            errors = response.json().get("errors", [])
        except (ValueError, AttributeError):
            return detail
        if not isinstance(errors, list):
            return detail
        normalized = [_cloudflare_error(item) for item in errors[:3]]
        safe_errors = [item for item in normalized if item]
        return f"{detail}: {'; '.join(safe_errors)}" if safe_errors else detail

    def _declared_tunnel_id(self, tunnel_id: str) -> str:
        try:
            normalized = str(UUID(str(tunnel_id)))
        except (ValueError, AttributeError):
            raise ProviderError(
                "configuration_missing", "Cloudflare tunnel ID is invalid"
            ) from None
        if normalized not in self.tunnel_ids:
            raise ProviderError(
                "configuration_missing",
                "Cloudflare tunnel is not declared in provider configuration",
            )
        return normalized

    async def tunnel_status(self, tunnel_id: str) -> object:
        declared = self._declared_tunnel_id(tunnel_id)
        return await self._get_tunnel_resource(
            declared,
            suffix="",
        )

    async def tunnel_connections(self, tunnel_id: str) -> object:
        declared = self._declared_tunnel_id(tunnel_id)
        return await self._get_tunnel_resource(
            declared,
            suffix="/connections",
        )

    async def _get_tunnel_resource(self, tunnel_id: str, *, suffix: str) -> object:
        legacy_path = f"/accounts/{self.account_id}/cfd_tunnel/{tunnel_id}{suffix}"
        try:
            return await self.get(legacy_path)
        except ProviderError as exc:
            if exc.code != "invalid_response" or "HTTP 400" not in exc.message:
                raise
        # Cloudflare's unified Tunnel API replaced the legacy cfd_tunnel path.
        return await self.get(
            f"/accounts/{self.account_id}/tunnels/{tunnel_id}{suffix}"
        )


def _account_id(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    if len(normalized) > 32 or not normalized.isalnum():
        raise ProviderError(
            "configuration_missing", "Cloudflare account_id is invalid"
        )
    return normalized


def _cloudflare_error(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    code = str(value.get("code") or "").strip()[:32]
    message = str(value.get("message") or "").strip()[:240]
    if code and message:
        return f"{code}: {message}"
    return message or code


def _tunnel_ids(values: tuple[str, ...]) -> tuple[str, ...]:
    normalized: list[str] = []
    for value in values:
        try:
            tunnel_id = str(UUID(str(value)))
        except (ValueError, AttributeError):
            raise ProviderError(
                "configuration_missing",
                "Cloudflare tunnel_ids must contain UUID values",
            ) from None
        if tunnel_id not in normalized:
            normalized.append(tunnel_id)
    return tuple(normalized)
