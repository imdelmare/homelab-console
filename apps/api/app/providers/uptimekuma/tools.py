"""Uptime Kuma provider and read-only tool implementations."""

from datetime import UTC, datetime

from app.providers.base import Provider, ProviderHealth
from app.providers.errors import ProviderError
from app.providers.httpclient import HEALTH_STATUS_MAP
from app.providers.uptimekuma import normalizers
from app.providers.uptimekuma.client import UptimeKumaClient


class UptimeKumaProvider(Provider):
    id = "uptimekuma"
    display_name = "Uptime Kuma"
    credential_requirements = ("uptimekuma.base_url",)

    def client(self) -> UptimeKumaClient:
        return UptimeKumaClient()

    def ready(self) -> bool:
        client = self.client()
        return client.is_configured()

    async def health(self) -> ProviderHealth:
        now = datetime.now(UTC)
        client = self.client()
        if not client.is_configured():
            return ProviderHealth(
                provider_id=self.id, status="unavailable",
                detail="not configured", checked_at=now,
            )
        try:
            if client.has_api_key():
                await client.get("/metrics", timeout=4.0, response_mode="text")
            else:
                await client.get("/api/status-page/heartbeat/default", timeout=4.0)
        except ProviderError as exc:
            return ProviderHealth(
                provider_id=self.id,
                status=HEALTH_STATUS_MAP.get(exc.code, "unknown"),
                detail=exc.message,
                checked_at=now,
            )
        return ProviderHealth(provider_id=self.id, status="healthy", checked_at=now)


async def monitors_status() -> dict:
    client = UptimeKumaClient()
    if not client.has_api_key():
        raise ProviderError("credentials_missing", "uptimekuma api_key is not configured")
    text = await client.get("/metrics", response_mode="text")
    if not isinstance(text, str):
        raise ProviderError("invalid_response", "uptimekuma /metrics returned unexpected content")
    monitors = normalizers.parse_monitor_metrics(text)
    by_status: dict[str, int] = {}
    for monitor in monitors:
        by_status[monitor.status] = by_status.get(monitor.status, 0) + 1
    return {
        "monitors": [item.model_dump() for item in monitors],
        "total": len(monitors),
        "by_status": by_status,
    }


async def statuspage_heartbeat(slug: str) -> dict:
    raw = await UptimeKumaClient().get(f"/api/status-page/heartbeat/{slug}")
    if not isinstance(raw, dict):
        raise ProviderError("invalid_response", "unexpected status-page response")
    monitors = normalizers.normalize_heartbeats(raw)
    return {"monitors": [item.model_dump() for item in monitors], "total": len(monitors)}
