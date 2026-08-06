"""MikroTik provider and read-only tool implementations."""

from datetime import UTC, datetime

from app.providers.base import Provider, ProviderHealth
from app.providers.errors import ProviderError
from app.providers.httpclient import HEALTH_STATUS_MAP
from app.providers.mikrotik import normalizers
from app.providers.mikrotik.client import MikrotikClient


class MikrotikProvider(Provider):
    id = "mikrotik"
    display_name = "MikroTik"
    credential_requirements = ("mikrotik.base_url", "mikrotik.username", "mikrotik.password")

    def client(self) -> MikrotikClient:
        return MikrotikClient()

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
            await client.get("/rest/system/resource", timeout=4.0)
        except ProviderError as exc:
            return ProviderHealth(
                provider_id=self.id,
                status=HEALTH_STATUS_MAP.get(exc.code, "unknown"),
                detail=exc.message,
                checked_at=now,
            )
        return ProviderHealth(provider_id=self.id, status="healthy", checked_at=now)


async def system_resource() -> dict:
    raw = await MikrotikClient().get("/rest/system/resource")
    return {"resource": normalizers.normalize_resource(raw).model_dump()}


async def system_health() -> dict:
    raw = await MikrotikClient().get("/rest/system/health")
    return {"health": normalizers.normalize_health(raw).model_dump()}


async def interfaces_list() -> dict:
    raw = await MikrotikClient().get("/rest/interface")
    interfaces = normalizers.normalize_interfaces(raw)
    return {"interfaces": [item.model_dump() for item in interfaces], "total": len(interfaces)}


async def lte_status() -> dict:
    try:
        raw = await MikrotikClient().get("/rest/interface/lte")
    except ProviderError as exc:
        if exc.code == "invalid_response":
            return {"lte_interfaces": [], "total": 0, "note": "no LTE interface on this router"}
        raise
    interfaces = normalizers.normalize_interfaces(raw)
    return {"lte_interfaces": [item.model_dump() for item in interfaces], "total": len(interfaces)}
