"""EMQX provider and read-only tool implementations."""

from datetime import UTC, datetime

from app.providers.base import Provider, ProviderHealth
from app.providers.emqx import normalizers
from app.providers.emqx.client import EmqxClient
from app.providers.errors import ProviderError
from app.providers.httpclient import HEALTH_STATUS_MAP


class EmqxProvider(Provider):
    id = "emqx"
    display_name = "EMQX"
    credential_requirements = ("emqx.base_url", "emqx.api_key", "emqx.api_secret")

    def client(self) -> EmqxClient:
        return EmqxClient()

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
                detail="api_key/api_secret not configured", checked_at=now,
            )
        try:
            nodes = await client.get("/api/v5/nodes", timeout=4.0)
        except ProviderError as exc:
            return ProviderHealth(
                provider_id=self.id,
                status=HEALTH_STATUS_MAP.get(exc.code, "unknown"),
                detail=exc.message,
                checked_at=now,
            )
        stopped = [
            node for node in (nodes if isinstance(nodes, list) else [])
            if isinstance(node, dict) and node.get("node_status") != "running"
        ]
        if stopped:
            return ProviderHealth(
                provider_id=self.id, status="degraded",
                detail=f"{len(stopped)} node(s) not running", checked_at=now,
            )
        return ProviderHealth(provider_id=self.id, status="healthy", checked_at=now)


async def nodes_list() -> dict:
    raw = await EmqxClient().get("/api/v5/nodes")
    nodes = normalizers.normalize_nodes(raw)
    return {"nodes": [item.model_dump() for item in nodes], "total": len(nodes)}


async def stats() -> dict:
    raw = await EmqxClient().get("/api/v5/stats")
    # Single-node returns a dict; clusters return a list of per-node dicts.
    entries = raw if isinstance(raw, list) else [raw]
    return {
        "stats": normalizers.normalize_stats(entries).model_dump(),
        "nodes_reporting": len(entries),
    }
