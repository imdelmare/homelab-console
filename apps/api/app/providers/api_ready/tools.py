from datetime import UTC, datetime

from app.providers.api_ready.client import (
    CloudflareTunnelV1Client,
    JsonHealthV1Client,
)
from app.providers.base import Provider, ProviderHealth, ProviderStatusValue
from app.providers.cloudflaretunnel.normalizers import normalize_tunnel
from app.providers.errors import ProviderError
from app.providers.httpclient import HEALTH_STATUS_MAP
from app.services.inventory import ApiProviderInstanceEntry, get_api_provider_instance

_HEALTHY_VALUES = {"ok", "healthy", "up", "ready"}
_DEGRADED_VALUES = {"degraded", "warning"}
def _normalized_health(payload: object) -> tuple[ProviderStatusValue, str]:
    if not isinstance(payload, dict):
        raise ProviderError("invalid_response", "health endpoint did not return a JSON object")
    raw_status = str(payload.get("status") or "").strip().lower()
    if raw_status in _HEALTHY_VALUES:
        return "healthy", raw_status
    if raw_status in _DEGRADED_VALUES:
        return "degraded", raw_status
    if raw_status:
        return "unavailable", raw_status
    raise ProviderError("invalid_response", "health response has no supported status field")


async def health_status(instance_id: str) -> dict:
    instance = get_api_provider_instance(instance_id)
    if instance is None:
        raise ProviderError("configuration_missing", "API provider instance is not declared")
    if instance.driver == "cloudflare_tunnel_v1":
        return await cloudflare_tunnel_status(instance_id)
    payload = await JsonHealthV1Client(instance).get("/health")
    status, reported_status = _normalized_health(payload)
    return {
        "instance_id": instance.id,
        "status": status,
        "reported_status": reported_status,
    }


def _normalized_cloudflare_tunnel(payload: object, instance_id: str) -> dict:
    if not isinstance(payload, dict):
        raise ProviderError("invalid_response", "Cloudflare API returned an invalid response")
    result = payload.get("result")
    declared_id = str(result.get("id") or "") if isinstance(result, dict) else ""
    tunnel = normalize_tunnel(payload, declared_id)
    return {
        "instance_id": instance_id,
        "tunnel_id": tunnel.id,
        "name": tunnel.name,
        "status": tunnel.status,
        "reported_status": tunnel.reported_status,
        "config_source": tunnel.config_source,
        "connected_at": tunnel.connected_at or None,
        "disconnected_at": tunnel.disconnected_at or None,
    }


async def cloudflare_tunnel_status(instance_id: str) -> dict:
    instance = get_api_provider_instance(instance_id)
    if instance is None or instance.driver != "cloudflare_tunnel_v1":
        raise ProviderError("configuration_missing", "Cloudflare Tunnel instance is not declared")
    payload = await CloudflareTunnelV1Client(instance).tunnel_status()
    result = _normalized_cloudflare_tunnel(payload, instance.id)
    if result["tunnel_id"] != instance.tunnel_id:
        raise ProviderError("invalid_response", "Cloudflare API returned a different tunnel")
    return result


class ApiReadyProvider(Provider):
    def __init__(self, instance: ApiProviderInstanceEntry) -> None:
        self.instance = instance
        self.id = instance.id
        self.display_name = instance.name or instance.id
        self.credential_requirements = (
            (f"{instance.id}.bearer_token",)
            if instance.driver == "cloudflare_tunnel_v1"
            else (f"{instance.id}.bearer_token (optional)",)
        )

    def ready(self) -> bool:
        if self.instance.driver == "cloudflare_tunnel_v1":
            client = CloudflareTunnelV1Client(self.instance)
            return client.is_configured() and client.has_credentials()
        return bool(self.instance.base_url)

    async def health(self) -> ProviderHealth:
        now = datetime.now(UTC)
        try:
            result = await health_status(self.id)
        except ProviderError as exc:
            return ProviderHealth(
                provider_id=self.id,
                status=HEALTH_STATUS_MAP.get(exc.code, "unknown"),
                detail=exc.message,
                checked_at=now,
            )
        driver_label = self.instance.driver
        return ProviderHealth(
            provider_id=self.id,
            status=result["status"] if result["status"] in {
                "healthy", "degraded", "unreachable", "unavailable", "misconfigured", "unknown"
            } else "unknown",
            detail=f"{driver_label} reported {result['reported_status']}",
            checked_at=now,
        )
