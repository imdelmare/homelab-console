"""Glances hosts provider and read-only tool implementations."""

import asyncio
from datetime import UTC, datetime

from app.providers.base import Provider, ProviderHealth
from app.providers.errors import ProviderError
from app.providers.glances import normalizers
from app.providers.glances.client import GlancesHostsClient, GlancesTarget


class GlancesProvider(Provider):
    id = "glances"
    display_name = "Glances Hosts"
    credential_requirements = ()

    def client(self) -> GlancesHostsClient:
        return GlancesHostsClient()

    def ready(self) -> bool:
        return self.client().is_configured()

    async def health(self) -> ProviderHealth:
        now = datetime.now(UTC)
        client = self.client()
        if not client.is_configured():
            return ProviderHealth(provider_id=self.id, status="unavailable", detail="not configured", checked_at=now)

        async def probe(target: GlancesTarget) -> str | None:
            try:
                await client.get(target, "/api/4/status")
            except ProviderError:
                return target.id
            return None

        failed = [item for item in await asyncio.gather(*(probe(t) for t in client.targets)) if item]
        if failed:
            return ProviderHealth(
                provider_id=self.id,
                status="degraded",
                detail=f"glances unreachable on: {', '.join(sorted(failed))}",
                checked_at=now,
            )
        return ProviderHealth(provider_id=self.id, status="healthy", checked_at=now)


async def _read_host(client: GlancesHostsClient, target: GlancesTarget) -> dict:
    try:
        raw = await client.sensors(target)
    except ProviderError as exc:
        return normalizers.normalize_host_sensors(target.id, target.base_url, []).model_copy(
            update={"error": exc.message}
        ).model_dump()
    return normalizers.normalize_host_sensors(target.id, target.base_url, raw).model_dump()


async def temperatures() -> dict:
    client = GlancesHostsClient()
    if not client.is_configured():
        raise ProviderError("configuration_missing", "glances hosts are not configured")
    hosts = await asyncio.gather(*(_read_host(client, target) for target in client.targets))
    temperatures_seen = [
        host["maximum_temperature_c"] for host in hosts if host.get("maximum_temperature_c") is not None
    ]
    return {
        "hosts": list(hosts),
        "maximum_temperature_c": max(temperatures_seen) if temperatures_seen else None,
    }
