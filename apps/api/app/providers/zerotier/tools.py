"""ZeroTier provider and narrow read-only capability implementations."""

import asyncio
from datetime import UTC, datetime

from app.providers.base import Provider, ProviderHealth
from app.providers.errors import ProviderError
from app.providers.httpclient import HEALTH_STATUS_MAP
from app.providers.zerotier import normalizers
from app.providers.zerotier.client import ZeroTierClient


class ZeroTierProvider(Provider):
    id = "zerotier"
    display_name = "ZeroTier Central"
    credential_requirements = ("zerotier.network_ids", "zerotier.api_token")

    def client(self) -> ZeroTierClient:
        return ZeroTierClient()

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
        except ProviderError as exc:
            return ProviderHealth(
                provider_id=self.id,
                status="misconfigured",
                detail=exc.message,
                checked_at=now,
            )
        if not client.is_configured():
            return ProviderHealth(
                provider_id=self.id,
                status="unavailable",
                detail="no ZeroTier network_ids are configured",
                checked_at=now,
            )
        if not client.has_credentials():
            return ProviderHealth(
                provider_id=self.id,
                status="misconfigured",
                detail=client.credentials_error(),
                checked_at=now,
            )
        try:
            await client.account_status()
        except ProviderError as exc:
            return ProviderHealth(
                provider_id=self.id,
                status=HEALTH_STATUS_MAP.get(exc.code, "unknown"),
                detail=exc.message,
                checked_at=now,
            )
        return ProviderHealth(provider_id=self.id, status="healthy", checked_at=now)


async def status() -> dict:
    client = ZeroTierClient()
    await client.account_status()
    return {
        "provider_id": "zerotier",
        "driver_id": "zerotier_central_legacy_v1",
        "status": "available",
        "networks_declared": len(client.declared_network_ids()),
    }


async def networks_list() -> dict:
    client = ZeroTierClient()
    network_ids = client.require_network_ids()
    raw_networks = await asyncio.gather(
        *(client.network(network_id) for network_id in network_ids)
    )
    networks = [
        normalizers.normalize_network(raw, network_id)
        for raw, network_id in zip(raw_networks, network_ids)
    ]
    return {
        "networks": [network.model_dump() for network in networks],
        "total": len(networks),
    }


async def members_list() -> dict:
    client = ZeroTierClient()
    network_ids = client.require_network_ids()
    raw_groups = await asyncio.gather(
        *(client.members(network_id) for network_id in network_ids)
    )
    members = [
        member
        for raw, network_id in zip(raw_groups, network_ids)
        for member in normalizers.normalize_members(
            raw,
            network_id,
            offline_after_seconds=client.offline_after_seconds,
        )
    ]
    authorized = [member for member in members if member.authorized]
    online = [member for member in authorized if member.online]
    stale = [member for member in authorized if member.stale]
    members_by_id = {member.id: member for member in members}
    required_ids = client.required_online_member_ids
    required_online = sum(
        1
        for member_id in required_ids
        if (member := members_by_id.get(member_id)) is not None
        and member.authorized
        and member.online
    )
    required_missing = sum(1 for member_id in required_ids if member_id not in members_by_id)
    return {
        "members": [member.model_dump() for member in members],
        "total": len(members),
        "authorized": len(authorized),
        "online": len(online),
        "stale": len(stale),
        "unauthorized": len(members) - len(authorized),
        "required_total": len(required_ids),
        "required_online": required_online,
        "required_unavailable": len(required_ids) - required_online,
        "required_missing": required_missing,
    }


async def summary() -> dict:
    health, networks, members = await asyncio.gather(
        ZeroTierProvider().health(), networks_list(), members_list()
    )
    findings: list[dict[str, str]] = []
    if health.status != "healthy":
        severity = (
            "critical"
            if health.status in {"unreachable", "misconfigured", "unavailable"}
            else "warning"
        )
        findings.append({"severity": severity, "message": health.detail or health.status})
    if members["required_unavailable"]:
        findings.append(
            {
                "severity": "warning",
                "message": (
                    f"{members['required_unavailable']} required ZeroTier member(s) "
                    "are unavailable"
                ),
            }
        )
    status_value = health.status
    if health.status == "healthy" and findings:
        status_value = "degraded"
    severity = "ok"
    if any(item["severity"] == "critical" for item in findings):
        severity = "critical"
    elif findings or status_value in {"degraded", "unknown"}:
        severity = "warning"
    return {
        "summary": {
            "provider_id": "zerotier",
            "status": status_value,
            "severity": severity,
            "generated_at": datetime.now(UTC).isoformat(),
            "metrics": {
                "networks": networks["total"],
                "members_total": members["total"],
                "members_authorized": members["authorized"],
                "members_online": members["online"],
                "members_stale": members["stale"],
                "members_unauthorized": members["unauthorized"],
                "members_required": members["required_total"],
                "members_required_online": members["required_online"],
                "members_required_unavailable": members["required_unavailable"],
            },
            "findings": findings,
            "next_actions": [f"Check: {item['message']}" for item in findings[:5]],
        }
    }
