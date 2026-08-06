"""OPNsense provider and read-only tool implementations.

Vendor responses are normalized into stable shapes; unknown or extra vendor
fields are dropped. Numeric values arriving as annotated strings ("12.3 ms",
"0.0 %") are converted to plain numbers.
"""

from datetime import UTC, datetime
from uuid import UUID

from app.providers.base import Provider, ProviderHealth
from app.providers.errors import ProviderError
from app.providers.httpclient import HEALTH_STATUS_MAP
from app.providers.opnsense import normalizers
from app.providers.opnsense.client import OpnsenseClient
from app.services.inventory import provider_config


class OpnsenseProvider(Provider):
    id = "opnsense"
    display_name = "OPNsense"
    credential_requirements = ("opnsense.base_url", "opnsense.api_key", "opnsense.api_secret")

    def client(self) -> OpnsenseClient:
        return OpnsenseClient()

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
                detail="API key/secret not configured", checked_at=now,
            )
        try:
            await client.get("/api/core/system/status", timeout=4.0)
        except ProviderError as exc:
            return ProviderHealth(
                provider_id=self.id,
                status=HEALTH_STATUS_MAP.get(exc.code, "unknown"),
                detail=exc.message,
                checked_at=now,
            )
        return ProviderHealth(provider_id=self.id, status="healthy", checked_at=now)


async def firmware_status() -> dict:
    raw = await OpnsenseClient().get("/api/core/firmware/status")
    return {"firmware": normalizers.normalize_firmware(raw).model_dump()}


async def system_status() -> dict:
    raw = await OpnsenseClient().get("/api/core/system/status")
    subsystems = normalizers.normalize_subsystems(raw)
    return {
        "status": {
            "subsystems": [item.model_dump() for item in subsystems],
            "total": len(subsystems),
        }
    }


async def system_information() -> dict:
    raw = await OpnsenseClient().get("/api/diagnostics/system/system_information")
    return {"system": normalizers.normalize_system_information(raw).model_dump()}


async def system_resources() -> dict:
    raw = await OpnsenseClient().get("/api/diagnostics/system/system_resources")
    return {"resources": normalizers.normalize_system_resources(raw).model_dump()}


async def system_temperature() -> dict:
    raw = await OpnsenseClient().get("/api/diagnostics/system/system_temperature")
    return {"temperature": normalizers.normalize_system_temperature(raw).model_dump()}


async def interface_names() -> dict:
    raw = await OpnsenseClient().get("/api/diagnostics/interface/get_interface_names")
    interfaces = normalizers.normalize_interface_names(raw)
    return {"interfaces": [item.model_dump() for item in interfaces], "total": len(interfaces)}


async def interface_statistics() -> dict:
    raw = await OpnsenseClient().get("/api/diagnostics/interface/getInterfaceStatistics")
    interfaces = normalizers.normalize_interface_statistics(raw)
    return {"statistics": [item.model_dump() for item in interfaces], "total": len(interfaces)}


async def arp_table() -> dict:
    raw = await OpnsenseClient().get("/api/diagnostics/interface/get_arp")
    entries = normalizers.normalize_arp_entries(raw)
    return {"devices": [item.model_dump() for item in entries], "total": len(entries)}


async def kea_leases() -> dict:
    client = OpnsenseClient()
    # IPv4 leases live under .../leases4/... (there's a separate leases6
    # endpoint for DHCPv6) — the unversioned .../leases/search path 404s.
    endpoint = "/api/kea/leases4/search"
    try:
        raw = await client.get(endpoint)
    except ProviderError as exc:
        if exc.code != "invalid_response":
            raise
        endpoint = "/api/dhcpv4/leases/searchLease"
        raw = await client.get(endpoint)
    leases = normalizers.normalize_kea_leases(raw)
    return {
        "leases": [item.model_dump() for item in leases],
        "total": len(leases),
        "source_endpoint": endpoint,
    }


async def wireguard_status() -> dict:
    """WireGuard tunnel state. On the VPS deployment this reflects the
    console's own link to the homelab."""
    raw = await OpnsenseClient().get("/api/wireguard/service/show")
    interfaces, orphan_peers = normalizers.normalize_wireguard(raw)

    all_peers = [
        peer for interface in interfaces for peer in interface.peers
    ] + orphan_peers
    return {
        "interfaces": [item.model_dump() for item in interfaces],
        "peers_total": len(all_peers),
        "peers_connected": len([peer for peer in all_peers if peer.connected]),
        "peers_stale": [peer.name or peer.endpoint for peer in all_peers if not peer.connected],
    }


async def services_list() -> dict:
    raw = await OpnsenseClient().get("/api/core/service/search")
    services = normalizers.normalize_services(raw)
    stopped = [service.name or service.id for service in services if not service.running]
    return {
        "services": [item.model_dump() for item in services],
        "total": len(services),
        "running": len(services) - len(stopped),
        "stopped": stopped,
    }


async def gateway_status() -> dict:
    raw = await OpnsenseClient().get("/api/routes/gateway/status")
    gateways = normalizers.normalize_gateways(raw)
    offline = [gateway.name for gateway in gateways if not gateway.online]
    return {
        "gateways": [item.model_dump() for item in gateways],
        "total": len(gateways),
        "offline": offline,
    }


async def gateway_configuration() -> dict:
    """List only the gateway fields needed by the controlled failover tools."""
    client = OpnsenseClient("wol")
    raw = await client.get("/api/routing/settings/search_gateway")
    rows = raw.get("rows", []) if isinstance(raw, dict) else []
    gateways = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        uuid = str(row.get("uuid") or "").strip()
        name = str(row.get("name") or "").strip()
        if not uuid or not name:
            continue
        gateways.append(
            {
                "uuid": uuid,
                "name": name,
                "enabled": (
                    str(row.get("enabled")).strip().lower() in {"1", "true", "yes"}
                    if "enabled" in row
                    else str(row.get("disabled", "0")).strip().lower()
                    not in {"1", "true", "yes"}
                ),
                "priority": str(
                    row.get("priority") or row.get("weight") or ""
                ).strip(),
                "upstream": str(row.get("upstream") or "").strip().lower()
                in {"1", "true", "yes"},
            }
        )
    routes_raw = await client.get("/api/diagnostics/interface/get_routes")
    route_rows = []

    def collect_route_rows(value) -> None:
        if isinstance(value, list):
            for item in value:
                collect_route_rows(item)
            return
        if not isinstance(value, dict):
            return
        if any(key in value for key in ("destination", "dst", "network")):
            route_rows.append(value)
            return
        for nested in value.values():
            if isinstance(nested, (dict, list)):
                collect_route_rows(nested)

    collect_route_rows(routes_raw)
    default_routes = []
    endpoint_ip = str(
        (provider_config("opnsense").get("egress_switch") or {}).get(
            "vpn_endpoint_ip", ""
        )
    ).strip()
    protected_routes = []
    for row in route_rows:
        if not isinstance(row, dict):
            continue
        destination = str(
            row.get("destination") or row.get("dst") or row.get("network") or ""
        ).strip()
        normalized = {
            "destination": destination,
            "gateway": str(row.get("gateway") or "").strip(),
            "interface": str(
                row.get("interface")
                or row.get("netif")
                or row.get("interface_name")
                or ""
            ).strip(),
        }
        if destination in {"default", "0.0.0.0/0", "::/0"}:
            default_routes.append(normalized)
        if endpoint_ip and destination.split("/", 1)[0] == endpoint_ip:
            protected_routes.append(normalized)
    return {
        "gateways": gateways,
        "total": len(gateways),
        "default_routes": default_routes,
        "protected_endpoint_routes": protected_routes,
    }


def _gateway_failover_boundary() -> dict[str, dict[str, str]]:
    boundary = provider_config("opnsense").get("gateway_failover") or {}
    result = {}
    for role in ("primary", "backup"):
        item = boundary.get(role) if isinstance(boundary, dict) else None
        name = str(item.get("name") or "").strip() if isinstance(item, dict) else ""
        uuid = str(item.get("uuid") or "").strip() if isinstance(item, dict) else ""
        try:
            uuid = str(UUID(uuid))
        except ValueError:
            raise ProviderError(
                "configuration_missing",
                f"configured OPNsense {role} gateway UUID is invalid",
            ) from None
        if not name:
            raise ProviderError(
                "configuration_missing",
                f"configured OPNsense {role} gateway name is missing",
            )
        result[role] = {"name": name, "uuid": uuid}
    if result["primary"]["uuid"] == result["backup"]["uuid"]:
        raise ProviderError(
            "configuration_missing",
            "primary and backup OPNsense gateways must be different",
        )
    return result


async def _set_gateway_enabled(
    client: OpnsenseClient, uuid: str, enabled: bool
) -> None:
    raw = await client.post(
        f"/api/routing/settings/toggle_gateway/{uuid}/{1 if enabled else 0}"
    )
    if not isinstance(raw, dict):
        raise ProviderError("invalid_response", "unexpected OPNsense gateway response")
    status = str(raw.get("status") or raw.get("result") or "").strip().lower()
    if status in {"failed", "error", "invalid"}:
        raise ProviderError("degraded", "OPNsense rejected the gateway state change")


async def _set_gateway_upstream(
    client: OpnsenseClient, uuid: str, upstream: bool
) -> None:
    value = "1" if upstream else "0"
    response = await client.post(
        f"/api/routing/settings/set_gateway/{uuid}",
        json_body={"gateway_item": {"defaultgw": value}},
    )
    if not isinstance(response, dict):
        raise ProviderError("invalid_response", "unexpected OPNsense gateway response")
    status = str(response.get("status") or response.get("result") or "").lower()
    if status in {"failed", "error", "invalid"}:
        raise ProviderError("degraded", "OPNsense rejected the upstream change")


async def gateway_transition(action: str) -> dict:
    """Force the declared backup path or restore the declared primary path."""
    if action not in {"failover", "restore"}:
        raise ProviderError("invalid_input", "unsupported gateway transition")
    boundary = _gateway_failover_boundary()
    before = await gateway_configuration()
    by_uuid = {item["uuid"]: item for item in before["gateways"]}
    for role in ("primary", "backup"):
        expected = boundary[role]
        actual = by_uuid.get(expected["uuid"])
        if actual is None or actual["name"] != expected["name"]:
            raise ProviderError(
                "permission_denied",
                f"configured {role} gateway does not match live OPNsense inventory",
            )

    client = OpnsenseClient("wol")
    changed = False
    if not by_uuid[boundary["backup"]["uuid"]]["enabled"]:
        await _set_gateway_enabled(client, boundary["backup"]["uuid"], True)
        changed = True
    primary_should_be_enabled = action == "restore"
    if by_uuid[boundary["primary"]["uuid"]]["enabled"] != primary_should_be_enabled:
        await _set_gateway_enabled(
            client, boundary["primary"]["uuid"], primary_should_be_enabled
        )
        changed = True
    if changed:
        raw = await client.post("/api/routing/settings/reconfigure")
        if not isinstance(raw, dict):
            raise ProviderError(
                "invalid_response", "unexpected OPNsense routing apply response"
            )

    after = await gateway_configuration()
    post_by_uuid = {item["uuid"]: item for item in after["gateways"]}
    target_role = "backup" if action == "failover" else "primary"
    target = boundary[target_role]
    status = await gateway_status()
    status_by_name = {item["name"]: item for item in status["gateways"]}
    target_status = status_by_name.get(target["name"], {})
    default_gateways = {
        route["gateway"] for route in after["default_routes"] if route["gateway"]
    }
    verified = (
        post_by_uuid.get(boundary["backup"]["uuid"], {}).get("enabled") is True
        and post_by_uuid.get(boundary["primary"]["uuid"], {}).get("enabled")
        is primary_should_be_enabled
        and target_status.get("online") is True
        and target_status.get("address") in default_gateways
    )
    if not verified:
        raise ProviderError(
            "degraded",
            "OPNsense gateway configuration changed but default-route verification failed",
        )
    return {
        "action": action,
        "changed": changed,
        "target_gateway": target["name"],
        "primary_enabled": primary_should_be_enabled,
        "backup_enabled": True,
        "default_route_gateway": str(target_status.get("address") or ""),
        "verified": True,
    }


async def egress_switch(profile: str) -> dict:
    """Switch the default egress between direct primary ISP and WG_DE."""
    if profile not in {"direct", "vpn_de"}:
        raise ProviderError("invalid_input", "unsupported egress profile")
    config = provider_config("opnsense").get("egress_switch") or {}
    vpn = config.get("vpn_gateway") if isinstance(config, dict) else None
    vpn_name = str(vpn.get("name") or "").strip() if isinstance(vpn, dict) else ""
    vpn_uuid = str(vpn.get("uuid") or "").strip() if isinstance(vpn, dict) else ""
    direct_names = config.get("direct_gateway_names") if isinstance(config, dict) else []
    direct_names = [str(item).strip() for item in direct_names or [] if str(item).strip()]
    try:
        vpn_uuid = str(UUID(vpn_uuid))
    except ValueError:
        raise ProviderError(
            "configuration_missing", "configured VPN gateway UUID is invalid"
        ) from None
    if not vpn_name or not direct_names:
        raise ProviderError(
            "configuration_missing", "OPNsense egress switch boundary is incomplete"
        )

    before = await gateway_configuration()
    by_name = {item["name"]: item for item in before["gateways"]}
    if (
        by_name.get(vpn_name, {}).get("uuid") != vpn_uuid
        or any(name not in by_name for name in direct_names)
    ):
        raise ProviderError(
            "permission_denied",
            "configured egress gateways do not match live OPNsense inventory",
        )
    if not before["protected_endpoint_routes"]:
        raise ProviderError(
            "configuration_missing",
            "WireGuard endpoint has no protected direct route",
        )

    client = OpnsenseClient("wol")
    target_name = vpn_name if profile == "vpn_de" else direct_names[0]
    changed = False
    if profile == "vpn_de":
        if not by_name[vpn_name]["enabled"]:
            await _set_gateway_enabled(client, vpn_uuid, True)
            changed = True
        if not by_name[vpn_name]["upstream"]:
            await _set_gateway_upstream(client, vpn_uuid, True)
            changed = True
        for name in direct_names:
            if by_name[name]["upstream"]:
                await _set_gateway_upstream(client, by_name[name]["uuid"], False)
                changed = True
    else:
        primary = by_name[direct_names[0]]
        if not primary["enabled"]:
            await _set_gateway_enabled(client, primary["uuid"], True)
            changed = True
        if not primary["upstream"]:
            await _set_gateway_upstream(client, primary["uuid"], True)
            changed = True
        if by_name[vpn_name]["upstream"]:
            await _set_gateway_upstream(client, vpn_uuid, False)
            changed = True
        for name in direct_names[1:]:
            if by_name[name]["upstream"]:
                await _set_gateway_upstream(client, by_name[name]["uuid"], False)
                changed = True
    if changed:
        response = await client.post("/api/routing/settings/reconfigure")
        if not isinstance(response, dict):
            raise ProviderError(
                "invalid_response", "unexpected OPNsense routing apply response"
            )

    after = await gateway_configuration()
    status = await gateway_status()
    status_by_name = {item["name"]: item for item in status["gateways"]}
    target_status = status_by_name.get(target_name, {})
    default_gateways = {
        route["gateway"] for route in after["default_routes"] if route["gateway"]
    }
    vpn_connected = True
    if profile == "vpn_de":
        wg = await wireguard_status()
        vpn_connected = any(
            peer.get("connected") is True
            for interface in wg["interfaces"]
            if interface.get("name") == "WG_DE"
            for peer in interface.get("peers", [])
        )
    verified = (
        target_status.get("online") is True
        and target_status.get("address") in default_gateways
        and bool(after["protected_endpoint_routes"])
        and vpn_connected
    )
    if not verified:
        raise ProviderError(
            "degraded",
            "OPNsense egress changed but route or tunnel verification failed",
        )
    return {
        "profile": profile,
        "changed": changed,
        "target_gateway": target_name,
        "default_route_gateway": str(target_status.get("address") or ""),
        "vpn_connected": vpn_connected,
        "verified": True,
    }


def _wol_target_uuid(target_id: str) -> str:
    targets = provider_config("opnsense").get("wol_targets", []) or []
    matches = [
        str(target.get("uuid", "")).strip()
        for target in targets
        if isinstance(target, dict) and target.get("id") == target_id
    ]
    if len(matches) != 1 or not matches[0]:
        raise ProviderError(
            "permission_denied",
            "requested Wake-on-LAN target is not uniquely declared in configuration",
        )
    try:
        return str(UUID(matches[0]))
    except ValueError:
        raise ProviderError(
            "configuration_missing",
            "configured Wake-on-LAN target UUID is invalid",
        ) from None


async def wol_wake(target_id: str) -> dict:
    """Wake one preconfigured os-wol host; never accept a caller-supplied MAC."""
    target_uuid = _wol_target_uuid(target_id)
    raw = await OpnsenseClient("wol").post(
        "/api/wol/wol/set",
        json_body={"uuid": target_uuid},
    )
    if not isinstance(raw, dict):
        raise ProviderError("invalid_response", "unexpected OPNsense WoL response")
    if str(raw.get("status", "")) != "OK":
        raise ProviderError("degraded", "OPNsense did not confirm the WoL packet")
    return {"target_id": target_id, "sent": True, "provider": "opnsense"}
