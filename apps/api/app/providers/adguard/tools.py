"""AdGuard Home provider and tool implementations.

Write operations (ADR 0004) are limited to pausing/resuming DNS protection:
the pause is bounded, AdGuard re-enables itself at expiry, and both calls
read the status back so the returned post_state is observed, not assumed.
"""

from datetime import UTC, datetime

from app.providers.adguard import normalizers
from app.providers.adguard.client import AdguardClient
from app.providers.base import Provider, ProviderHealth
from app.providers.errors import ProviderError
from app.providers.httpclient import HEALTH_STATUS_MAP


class AdguardProvider(Provider):
    id = "adguard"
    display_name = "AdGuard Home"
    credential_requirements = ("adguard.base_url", "adguard.username", "adguard.password")

    def client(self) -> AdguardClient:
        return AdguardClient()

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
        if not client.has_credentials():
            return ProviderHealth(
                provider_id=self.id, status="misconfigured",
                detail="username/password not configured", checked_at=now,
            )
        try:
            await client.get("/control/status", timeout=4.0)
        except ProviderError as exc:
            return ProviderHealth(
                provider_id=self.id,
                status=HEALTH_STATUS_MAP.get(exc.code, "unknown"),
                detail=exc.message,
                checked_at=now,
            )
        return ProviderHealth(provider_id=self.id, status="healthy", checked_at=now)


async def status() -> dict:
    raw = await AdguardClient().get("/control/status")
    return {"status": normalizers.normalize_status(raw).model_dump()}


async def stats() -> dict:
    raw = await AdguardClient().get("/control/stats")
    return {"stats": normalizers.normalize_stats(raw).model_dump()}


async def filtering_status() -> dict:
    raw = await AdguardClient().get("/control/filtering/status")
    return {"filtering": normalizers.normalize_filtering_status(raw).model_dump()}


async def _protection_post_state(client: AdguardClient) -> dict:
    raw = await client.get("/control/status")
    return {
        "protection_enabled": bool(raw.get("protection_enabled")),
        "disabled_duration_ms": int(raw.get("protection_disabled_duration") or 0),
    }


async def protection_pause(duration_minutes: int) -> dict:
    client = AdguardClient()
    await client.post(
        "/control/protection",
        response_mode="auto",
        json_body={"enabled": False, "duration": duration_minutes * 60_000},
    )
    post_state = await _protection_post_state(client)
    return {
        "requested_duration_minutes": duration_minutes,
        "post_state": post_state,
        "verified": post_state["protection_enabled"] is False,
    }


async def protection_resume() -> dict:
    client = AdguardClient()
    await client.post(
        "/control/protection",
        response_mode="auto",
        json_body={"enabled": True},
    )
    post_state = await _protection_post_state(client)
    return {
        "post_state": post_state,
        "verified": post_state["protection_enabled"] is True,
    }
