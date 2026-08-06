"""Convert raw Proxmox API payloads into normalized internal models."""

from typing import Any

import re
import time

from app.providers.proxmox.models import (
    ClusterStatusEntry,
    DiskTemperature,
    FailedTaskInfo,
    GuestBackupInfo,
    GuestInfo,
    NodeInfo,
    NodeStatusInfo,
    ProxmoxVersion,
    StorageInfo,
)


def normalize_version(raw: dict[str, Any]) -> ProxmoxVersion:
    return ProxmoxVersion(
        version=str(raw.get("version", "")),
        release=str(raw.get("release", "")),
        repoid=str(raw.get("repoid", "")),
    )


def normalize_cluster_status(raw: list[dict[str, Any]]) -> list[ClusterStatusEntry]:
    entries = []
    for item in raw or []:
        entries.append(
            ClusterStatusEntry(
                id=str(item.get("id", "")),
                name=str(item.get("name", "")),
                kind=str(item.get("type", "")),
                online=bool(item["online"]) if "online" in item else None,
                quorate=bool(item["quorate"]) if "quorate" in item else None,
                nodes=item.get("nodes"),
            )
        )
    return entries


def normalize_nodes(raw: list[dict[str, Any]]) -> list[NodeInfo]:
    nodes = []
    for item in raw or []:
        nodes.append(
            NodeInfo(
                node=str(item.get("node", "")),
                status=str(item.get("status", "unknown")),
                uptime_seconds=item.get("uptime"),
                cpu_usage=item.get("cpu"),
                max_cpu=item.get("maxcpu"),
                memory_used_bytes=item.get("mem"),
                memory_total_bytes=item.get("maxmem"),
            )
        )
    return nodes


def normalize_guest(item: dict[str, Any]) -> GuestInfo:
    return GuestInfo(
        vmid=int(item.get("vmid", 0)),
        name=str(item.get("name", "")),
        guest_type=str(item.get("type", "")),
        status=str(item.get("status", "unknown")),
        node=str(item.get("node", "")),
        cpu_usage=item.get("cpu"),
        max_cpu=item.get("maxcpu"),
        memory_used_bytes=item.get("mem"),
        memory_total_bytes=item.get("maxmem"),
        uptime_seconds=item.get("uptime"),
        tags=str(item.get("tags", "") or ""),
    )


def normalize_guests(raw: list[dict[str, Any]], guest_type: str | None = None) -> list[GuestInfo]:
    guests = []
    for item in raw or []:
        if item.get("type") not in ("qemu", "lxc"):
            continue
        if guest_type and item.get("type") != guest_type:
            continue
        guests.append(normalize_guest(item))
    return guests


def normalize_storage(raw: list[dict[str, Any]]) -> list[StorageInfo]:
    stores = []
    for item in raw or []:
        if item.get("type") != "storage":
            continue
        stores.append(
            StorageInfo(
                id=str(item.get("storage", item.get("id", ""))),
                node=str(item.get("node", "")),
                storage_type=str(item.get("plugintype", "")),
                total_bytes=item.get("maxdisk"),
                used_bytes=item.get("disk"),
                available_bytes=(
                    item["maxdisk"] - item["disk"]
                    if item.get("maxdisk") is not None and item.get("disk") is not None
                    else None
                ),
                active=bool(item["status"] == "available") if item.get("status") else None,
                shared=bool(item["shared"]) if "shared" in item else None,
            )
        )
    return stores


def normalize_node_status(node: str, raw: dict[str, Any]) -> NodeStatusInfo:
    raw = raw if isinstance(raw, dict) else {}
    memory = raw.get("memory") or {}
    swap = raw.get("swap") or {}
    rootfs = raw.get("rootfs") or {}
    cpuinfo = raw.get("cpuinfo") or {}
    loadavg = []
    for item in raw.get("loadavg") or []:
        try:
            loadavg.append(float(item))
        except (TypeError, ValueError):
            continue
    return NodeStatusInfo(
        node=node,
        uptime_seconds=raw.get("uptime"),
        loadavg=loadavg,
        cpu_usage=raw.get("cpu"),
        cpu_count=cpuinfo.get("cpus"),
        cpu_model=str(cpuinfo.get("model", "")),
        kernel_version=str(raw.get("kversion", "")),
        memory_total_bytes=memory.get("total"),
        memory_used_bytes=memory.get("used"),
        swap_total_bytes=swap.get("total"),
        swap_used_bytes=swap.get("used"),
        rootfs_total_bytes=rootfs.get("total"),
        rootfs_used_bytes=rootfs.get("used"),
    )


def normalize_backups(volumes: list[tuple[str, str, dict[str, Any]]]) -> list[GuestBackupInfo]:
    """Aggregate raw backup volumes (node, storage, item) per guest vmid,
    keeping the most recent backup as the reference."""
    by_vmid: dict[int, GuestBackupInfo] = {}
    counts: dict[int, int] = {}
    now = time.time()

    for node, storage, item in volumes:
        try:
            vmid = int(item.get("vmid", 0))
        except (TypeError, ValueError):
            continue
        if vmid <= 0:
            continue
        ctime = item.get("ctime")
        counts[vmid] = counts.get(vmid, 0) + 1
        current = by_vmid.get(vmid)
        if current is None or (ctime or 0) > (current.latest_backup_at or 0):
            by_vmid[vmid] = GuestBackupInfo(
                vmid=vmid,
                latest_backup_at=ctime,
                latest_age_days=round((now - ctime) / 86400, 1) if ctime else None,
                latest_size_bytes=item.get("size"),
                storage=storage,
                node=node,
            )

    result = []
    for vmid in sorted(by_vmid):
        info = by_vmid[vmid]
        info.backups_count = counts[vmid]
        result.append(info)
    return result


def normalize_failed_tasks(raw: list[dict[str, Any]]) -> list[FailedTaskInfo]:
    tasks = []
    for item in raw or []:
        status = str(item.get("status", ""))
        if status in ("OK", "RUNNING", ""):
            continue
        tasks.append(
            FailedTaskInfo(
                node=str(item.get("node", "")),
                task_type=str(item.get("type", "")),
                status=status,
                started_at=item.get("starttime"),
                finished_at=item.get("endtime"),
                vmid=str(item.get("id", "") or ""),
            )
        )
    return tasks


_SMART_TEXT_TEMPERATURE = re.compile(r"^Temperature:\s+(\d+)\s+Celsius", re.MULTILINE)
# Prefer the canonical drive temperature over airflow when both exist.
_SMART_TEMPERATURE_ATTRIBUTE_IDS = (194, 190)


def normalize_disk_temperature(node: str, disk: dict[str, Any], smart_raw: Any) -> DiskTemperature:
    smart = smart_raw if isinstance(smart_raw, dict) else {}
    temperature: int | float | None = None

    attributes = [item for item in smart.get("attributes") or [] if isinstance(item, dict)]
    for wanted_id in _SMART_TEMPERATURE_ATTRIBUTE_IDS:
        for attribute in attributes:
            # The HTTP API serializes the attribute id as a string ("194").
            if str(attribute.get("id", "")).strip() != str(wanted_id):
                continue
            match = re.match(r"\s*(\d+)", str(attribute.get("raw", "")))
            if match:
                temperature = int(match.group(1))
                break
        if temperature is not None:
            break

    if temperature is None:
        # NVMe devices report SMART as free text instead of ATA attributes.
        match = _SMART_TEXT_TEMPERATURE.search(str(smart.get("text", "")))
        if match:
            temperature = int(match.group(1))

    wearout = disk.get("wearout")
    return DiskTemperature(
        node=node,
        devpath=str(disk.get("devpath", "")),
        disk_model=str(disk.get("model", "")),
        disk_type=str(disk.get("type", "")),
        temperature_c=temperature,
        health=str(disk.get("health") or ""),
        wearout=wearout if isinstance(wearout, (int, float)) else None,
    )
