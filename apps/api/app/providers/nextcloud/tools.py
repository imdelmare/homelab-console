"""Nextcloud provider and read-only tool implementations."""

from datetime import UTC, datetime

from app.providers.base import Provider, ProviderHealth
from app.providers.errors import ProviderError
from app.providers.httpclient import HEALTH_STATUS_MAP
from app.providers.nextcloud import normalizers
from app.providers.nextcloud.client import NextcloudClient


class NextcloudProvider(Provider):
    id = "nextcloud"
    display_name = "Nextcloud"
    credential_requirements = ("nextcloud.base_url", "nextcloud.username", "nextcloud.app_password")

    def client(self) -> NextcloudClient:
        return NextcloudClient()

    def ready(self) -> bool:
        client = self.client()
        return client.is_configured() and client.has_credentials()

    async def health(self) -> ProviderHealth:
        now = datetime.now(UTC)
        client = self.client()
        if not client.is_configured():
            return ProviderHealth(
                provider_id=self.id, status="unavailable",
                detail="not configured", checked_at=now,
            )
        try:
            raw = await client.get("/status.php", timeout=4.0)
        except ProviderError as exc:
            return ProviderHealth(
                provider_id=self.id,
                status=HEALTH_STATUS_MAP.get(exc.code, "unknown"),
                detail=exc.message,
                checked_at=now,
            )
        if isinstance(raw, dict) and raw.get("maintenance"):
            return ProviderHealth(
                provider_id=self.id, status="degraded",
                detail="maintenance mode is active", checked_at=now,
            )
        return ProviderHealth(provider_id=self.id, status="healthy", checked_at=now)


async def status() -> dict:
    raw = await NextcloudClient().get("/status.php")
    return {"status": normalizers.normalize_status(raw).model_dump()}


async def capabilities() -> dict:
    raw = await NextcloudClient().get("/ocs/v2.php/cloud/capabilities?format=json")
    return {"capabilities": normalizers.normalize_capabilities(raw).model_dump()}


async def serverinfo() -> dict:
    raw = await NextcloudClient().get(
        "/ocs/v2.php/apps/serverinfo/api/v1/info?format=json&skipUpdate=true"
    )
    return {"serverinfo": normalizers.normalize_serverinfo(raw).model_dump()}
