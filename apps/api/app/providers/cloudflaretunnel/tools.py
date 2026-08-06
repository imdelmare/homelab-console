"""Cloudflare Tunnel provider and API-backed read-only tools."""

import asyncio
from datetime import UTC, datetime

from app.providers.base import Provider, ProviderHealth
from app.providers.cloudflaretunnel import normalizers
from app.providers.cloudflaretunnel.client import CloudflareTunnelClient
from app.providers.errors import ProviderError
from app.providers.httpclient import HEALTH_STATUS_MAP


class CloudflareTunnelProvider(Provider):
    id = "cloudflaretunnel"
    display_name = "Cloudflare Tunnel"
    credential_requirements = (
        "cloudflaretunnel.account_id",
        "cloudflaretunnel.tunnel_ids",
        "cloudflaretunnel.bearer_token",
    )

    def client(self) -> CloudflareTunnelClient:
        return CloudflareTunnelClient()

    def ready(self) -> bool:
        try:
            client = self.client()
        except ProviderError:
            return False
        return client.is_configured() and client.has_credentials()

    async def health(self) -> ProviderHealth:
        now = datetime.now(UTC)
        try:
            client = self.client()
            tunnel_ids = client.declared_tunnel_ids()
            payloads = await asyncio.gather(
                *(client.tunnel_status(tunnel_id) for tunnel_id in tunnel_ids)
            )
            tunnels = [
                normalizers.normalize_tunnel(payload, tunnel_id)
                for payload, tunnel_id in zip(payloads, tunnel_ids)
            ]
        except ProviderError as exc:
            return ProviderHealth(
                provider_id=self.id,
                status=HEALTH_STATUS_MAP.get(exc.code, "unknown"),
                detail=exc.message,
                checked_at=now,
            )
        unavailable = sum(1 for tunnel in tunnels if tunnel.status == "unavailable")
        degraded = sum(1 for tunnel in tunnels if tunnel.status == "degraded")
        if unavailable == len(tunnels):
            return ProviderHealth(
                provider_id=self.id,
                status="unavailable",
                detail="all declared Cloudflare tunnels are down or inactive",
                checked_at=now,
            )
        if unavailable or degraded:
            return ProviderHealth(
                provider_id=self.id,
                status="degraded",
                detail=f"{unavailable + degraded} of {len(tunnels)} tunnel(s) unhealthy",
                checked_at=now,
            )
        return ProviderHealth(provider_id=self.id, status="healthy", checked_at=now)


async def all_tunnels_status() -> dict:
    client = CloudflareTunnelClient()
    tunnel_ids = client.declared_tunnel_ids()
    payloads = await asyncio.gather(
        *(client.tunnel_status(tunnel_id) for tunnel_id in tunnel_ids)
    )
    tunnels = [
        normalizers.normalize_tunnel(payload, tunnel_id)
        for payload, tunnel_id in zip(payloads, tunnel_ids)
    ]
    by_status = {
        status: sum(1 for tunnel in tunnels if tunnel.status == status)
        for status in ("healthy", "degraded", "unavailable")
    }
    return {
        "tunnels": [tunnel.model_dump() for tunnel in tunnels],
        "total": len(tunnels),
        "by_status": by_status,
        "unhealthy_ids": [
            tunnel.id for tunnel in tunnels if tunnel.status != "healthy"
        ],
    }


async def connectors_list() -> dict:
    client = CloudflareTunnelClient()
    tunnel_ids = client.declared_tunnel_ids()
    payloads = await asyncio.gather(
        *(client.tunnel_connections(tunnel_id) for tunnel_id in tunnel_ids)
    )
    connectors = [
        connector
        for payload, tunnel_id in zip(payloads, tunnel_ids)
        for connector in normalizers.normalize_connectors(payload, tunnel_id)
    ]
    return {
        "connectors": [connector.model_dump() for connector in connectors],
        "tunnels_total": len(tunnel_ids),
        "connectors_total": len(connectors),
        "connections_total": sum(item.connections_total for item in connectors),
        "connections_active": sum(item.connections_active for item in connectors),
        "connections_pending_reconnect": sum(
            item.connections_pending_reconnect for item in connectors
        ),
    }
