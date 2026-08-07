"""Proxmox provider and its read-only tool implementations."""

import asyncio
from datetime import UTC, datetime
from urllib.parse import quote, urlencode

from app.providers.base import Provider, ProviderHealth
from app.providers.errors import ProviderError
from app.providers.httpclient import HEALTH_STATUS_MAP
from app.providers.proxmox.client import ProxmoxClient
from app.providers.proxmox import normalizers
from app.providers.proxmox.models import ProxmoxTopologySnapshot
from app.services.inventory import provider_config

_TASK_POLL_INTERVAL_SECONDS = 0.25
_TASK_POLL_ATTEMPTS = 80


class ProxmoxProvider(Provider):
    id = "proxmox"
    display_name = "Proxmox VE"
    credential_requirements = ("proxmox.base_url", "proxmox.api_token_id", "proxmox.api_token_secret")

    def client(self) -> ProxmoxClient:
        return ProxmoxClient()

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
                detail="API token not configured", checked_at=now,
            )
        try:
            await client.get("/api2/json/version", timeout=4.0)
        except ProviderError as exc:
            return ProviderHealth(
                provider_id=self.id,
                status=HEALTH_STATUS_MAP.get(exc.code, "unknown"),
                detail=exc.message,
                checked_at=now,
            )
        return ProviderHealth(provider_id=self.id, status="healthy", checked_at=now)


async def version() -> dict:
    raw = await ProxmoxClient().get("/api2/json/version")
    return {"version": normalizers.normalize_version(raw).model_dump()}


async def cluster_status() -> dict:
    raw = await ProxmoxClient().get("/api2/json/cluster/status")
    return {"entries": [item.model_dump() for item in normalizers.normalize_cluster_status(raw)]}


async def nodes_list() -> dict:
    raw = await ProxmoxClient().get("/api2/json/nodes")
    return {"nodes": [item.model_dump() for item in normalizers.normalize_nodes(raw)]}


def _guest_inventory(guests, guest_type: str | None = None) -> dict:
    guest_rows = [item.model_dump() for item in guests]
    running_rows = [item for item in guest_rows if item.get("status") == "running"]
    lxc_rows = [item for item in guest_rows if item.get("guest_type") == "lxc"]
    qemu_rows = [item for item in guest_rows if item.get("guest_type") == "qemu"]
    return {
        "metrics": {
            "guest_type": guest_type or "all",
            "total": len(guest_rows),
            "running_total": len(running_rows),
            "stopped_total": len(guest_rows) - len(running_rows),
            "lxc_total": len(lxc_rows),
            "lxc_running": len(
                [item for item in lxc_rows if item.get("status") == "running"]
            ),
            "qemu_total": len(qemu_rows),
            "qemu_running": len(
                [item for item in qemu_rows if item.get("status") == "running"]
            ),
        },
        "running_guests": [
            {
                "vmid": item.get("vmid"),
                "name": item.get("name"),
                "guest_type": item.get("guest_type"),
                "node": item.get("node"),
            }
            for item in running_rows
        ],
        "guests": guest_rows,
    }


def _verified_guest_counts(result: dict) -> dict:
    metrics = result["metrics"]
    return {
        "lxc_running": metrics["lxc_running"],
        "qemu_running": metrics["qemu_running"],
    }


async def resources_list() -> dict:
    raw = await ProxmoxClient().get("/api2/json/cluster/resources")
    guests = normalizers.normalize_guests(raw)
    storage = normalizers.normalize_storage(raw)
    result = _guest_inventory(guests)
    result["storage"] = [item.model_dump() for item in storage]
    result["verified_counts"] = _verified_guest_counts(result)
    return result


async def guests_list(node: str | None = None, guest_type: str | None = None) -> dict:
    raw = await ProxmoxClient().get("/api2/json/cluster/resources")
    guests = normalizers.normalize_guests(raw, guest_type=guest_type)
    if node:
        guests = [guest for guest in guests if guest.node == node]
    result = _guest_inventory(guests, guest_type)
    result["verified_counts"] = _verified_guest_counts(result)
    return result


def _critical_lxc_vmids() -> set[int]:
    configured = provider_config("proxmox").get("critical_lxc_vmids", []) or []
    return {
        int(vmid)
        for vmid in configured
        if isinstance(vmid, int) and not isinstance(vmid, bool) and vmid > 0
    }


async def _resolve_lxc(client: ProxmoxClient, vmid: int):
    resources = await client.get("/api2/json/cluster/resources")
    matches = [
        guest
        for guest in normalizers.normalize_guests(resources)
        if guest.vmid == vmid
    ]
    if not matches:
        raise ProviderError("invalid_response", "requested LXC is not present in live inventory")
    if len(matches) != 1:
        raise ProviderError("invalid_response", "requested guest id is ambiguous in live inventory")
    guest = matches[0]
    if guest.guest_type != "lxc":
        raise ProviderError("invalid_response", "requested guest is not an LXC container")
    if not guest.node:
        raise ProviderError("invalid_response", "requested LXC has no inventory node")
    return guest


async def _lxc_state(client: ProxmoxClient, node: str, vmid: int) -> dict:
    raw = await client.get(f"/api2/json/nodes/{node}/lxc/{vmid}/status/current")
    if not isinstance(raw, dict):
        raise ProviderError("invalid_response", "unexpected LXC status response")
    return {
        "vmid": vmid,
        "name": str(raw.get("name", "")),
        "node": node,
        "status": str(raw.get("status", "unknown")),
    }


async def _wait_for_task(client: ProxmoxClient, node: str, upid: str) -> None:
    encoded_upid = quote(upid, safe="")
    for _ in range(_TASK_POLL_ATTEMPTS):
        raw = await client.get(f"/api2/json/nodes/{node}/tasks/{encoded_upid}/status")
        if not isinstance(raw, dict):
            raise ProviderError("invalid_response", "unexpected Proxmox task response")
        if raw.get("status") == "stopped":
            if raw.get("exitstatus") != "OK":
                raise ProviderError("degraded", "Proxmox LXC power task failed")
            return
        await asyncio.sleep(_TASK_POLL_INTERVAL_SECONDS)
    raise ProviderError("timeout", "Proxmox LXC power task did not finish in time")


async def _lxc_power_action(vmid: int, action: str) -> dict:
    reader = ProxmoxClient()
    guest = await _resolve_lxc(reader, vmid)
    previous_state = {
        "vmid": guest.vmid,
        "name": guest.name,
        "node": guest.node,
        "status": guest.status,
    }
    desired_status = "running" if action == "start" else "stopped"
    changed = guest.status != desired_status

    if changed:
        power = ProxmoxClient(credential_profile="power")
        upid = await power.post(
            f"/api2/json/nodes/{guest.node}/lxc/{vmid}/status/{action}",
            json_body={},
        )
        if not isinstance(upid, str) or not upid.startswith("UPID:"):
            raise ProviderError("invalid_response", "unexpected Proxmox task identifier")
        await _wait_for_task(power, guest.node, upid)

    post_state = await _lxc_state(reader, guest.node, vmid)
    return {
        "action": action,
        "changed": changed,
        "critical_target": vmid in _critical_lxc_vmids(),
        "previous_state": previous_state,
        "post_state": post_state,
        "verified": post_state["status"] == desired_status,
    }


async def lxc_start(vmid: int) -> dict:
    return await _lxc_power_action(vmid, "start")


async def lxc_shutdown(vmid: int) -> dict:
    # The dedicated Proxmox shutdown endpoint is graceful. Do not add
    # forceStop, stop, kill or caller-controlled timeout parameters here.
    return await _lxc_power_action(vmid, "shutdown")


async def topology_snapshot() -> dict:
    """Read only the three Proxmox collections needed for cluster topology.

    Keeping this as one narrow tool gives REST/MCP the same normalized view and
    avoids reconstructing placement from raw provider payloads in an adapter.
    """
    client = ProxmoxClient()
    cluster_raw, nodes_raw, resources_raw = await asyncio.gather(
        client.get("/api2/json/cluster/status"),
        client.get("/api2/json/nodes"),
        client.get("/api2/json/cluster/resources"),
    )
    snapshot = ProxmoxTopologySnapshot(
        entries=normalizers.normalize_cluster_status(cluster_raw),
        nodes=normalizers.normalize_nodes(nodes_raw),
        guests=normalizers.normalize_guests(resources_raw),
    )
    return snapshot.model_dump()


async def storage_list() -> dict:
    raw = await ProxmoxClient().get("/api2/json/cluster/resources")
    return {"storage": [item.model_dump() for item in normalizers.normalize_storage(raw)]}


async def node_status(node: str) -> dict:
    raw = await ProxmoxClient().get(f"/api2/json/nodes/{node}/status")
    return {"node_status": normalizers.normalize_node_status(node, raw).model_dump()}


async def backups_list() -> dict:
    """Aggregate vzdump backups across all online nodes and backup-capable
    storages, summarized per guest."""
    client = ProxmoxClient()
    nodes_raw = await client.get("/api2/json/nodes")
    nodes = [
        str(item.get("node"))
        for item in nodes_raw or []
        if item.get("node") and item.get("status") == "online"
    ]

    seen_storages: set[str] = set()
    volumes: list[tuple[str, str, dict]] = []
    for node in nodes:
        storages = await client.get(f"/api2/json/nodes/{node}/storage")
        for storage in storages or []:
            name = str(storage.get("storage", ""))
            if not name or "backup" not in str(storage.get("content", "")):
                continue
            if not storage.get("active"):
                continue
            # Shared storages appear on every node: query them once.
            key = name if storage.get("shared") else f"{node}/{name}"
            if key in seen_storages:
                continue
            seen_storages.add(key)
            contents = await client.get(
                f"/api2/json/nodes/{node}/storage/{name}/content?content=backup"
            )
            for item in contents or []:
                if isinstance(item, dict):
                    volumes.append((node, name, item))

    guests = normalizers.normalize_backups(volumes)
    return {
        "backups_by_guest": [item.model_dump() for item in guests],
        "guests_with_backups": len(guests),
        "total_backups": len(volumes),
    }


async def tasks_failed() -> dict:
    raw = await ProxmoxClient().get("/api2/json/cluster/tasks")
    return {"failed_tasks": [item.model_dump() for item in normalizers.normalize_failed_tasks(raw)]}


async def disks_temperatures() -> dict:
    """Read SMART temperature for every physical disk on every online node."""
    client = ProxmoxClient()
    nodes_raw = await client.get("/api2/json/nodes")
    nodes = [
        str(item.get("node"))
        for item in nodes_raw or []
        if item.get("node") and item.get("status") == "online"
    ]

    disks: list = []
    for node in nodes:
        for disk in await client.get(f"/api2/json/nodes/{node}/disks/list") or []:
            devpath = str(disk.get("devpath", "")) if isinstance(disk, dict) else ""
            if not devpath:
                continue
            try:
                smart = await client.get(f"/api2/json/nodes/{node}/disks/smart?{urlencode({'disk': devpath})}")
            except ProviderError as exc:
                if exc.code in {"invalid_response", "permission_denied", "degraded"}:
                    smart = {}
                else:
                    raise
            disks.append(normalizers.normalize_disk_temperature(node, disk, smart))

    temperatures = [disk.temperature_c for disk in disks if disk.temperature_c is not None]
    return {
        "disks": [item.model_dump() for item in disks],
        "maximum_temperature_c": max(temperatures) if temperatures else None,
    }
