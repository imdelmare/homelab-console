from __future__ import annotations

from typing import Any

from app.providers.vps.models import (
    VpsDeployStatus,
    VpsDeployTarget,
    VpsGlancesStatus,
    VpsResourceStatus,
    VpsSystemStatus,
    VpsWireGuardInterface,
    VpsWireGuardStatus,
)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def normalize_glances_all(raw: dict[str, Any]) -> VpsGlancesStatus:
    system = _dict(raw.get("system"))
    cpu = _dict(raw.get("cpu"))
    mem = _dict(raw.get("mem"))
    load = _dict(raw.get("load"))
    fs = _list(raw.get("fs"))

    disks = [_number(item.get("percent")) for item in fs if isinstance(item, dict)]
    disk_high = [
        str(item.get("mnt_point") or item.get("device_name") or "")
        for item in fs
        if isinstance(item, dict) and (_number(item.get("percent")) or 0) >= 85
    ]

    return VpsGlancesStatus(
        ok=True,
        system=VpsSystemStatus(
            hostname=str(system.get("hostname") or ""),
            os_name=str(system.get("os_name") or system.get("os") or ""),
            linux_distro=str(system.get("linux_distro") or ""),
            platform=str(system.get("platform") or ""),
            uptime_seconds=_int(raw.get("uptime", {}).get("seconds")) if isinstance(raw.get("uptime"), dict) else _int(raw.get("uptime")),
        ),
        resources=VpsResourceStatus(
            cpu_percent=_number(cpu.get("total") if "total" in cpu else cpu.get("user")),
            load_1m=_number(load.get("min1") or load.get("cpucore")),
            load_5m=_number(load.get("min5")),
            load_15m=_number(load.get("min15")),
            memory_percent=_number(mem.get("percent")),
            disk_percent_max=max([item for item in disks if item is not None], default=None),
            disk_paths_high=[item for item in disk_high if item],
        ),
    )


def normalize_wireguard(raw: dict[str, Any], interface_names: list[str], route_targets: list[dict]) -> VpsWireGuardStatus:
    network = _list(raw.get("network"))
    by_name: dict[str, dict[str, Any]] = {
        str(item.get("interface_name") or item.get("name") or ""): item
        for item in network
        if isinstance(item, dict)
    }
    interfaces = []
    for name in interface_names:
        item = by_name.get(name)
        interfaces.append(
            VpsWireGuardInterface(
                name=name,
                present=item is not None,
                rx_bytes=_int((item or {}).get("rx") or (item or {}).get("bytes_recv")),
                tx_bytes=_int((item or {}).get("tx") or (item or {}).get("bytes_sent")),
            )
        )
    targets_ok = [target for target in route_targets if target.get("ok")]
    return VpsWireGuardStatus(
        ok=bool(interfaces and all(item.present for item in interfaces) and len(targets_ok) == len(route_targets)),
        interfaces=interfaces,
        route_targets=route_targets,
        limitations=["peer handshake age is not exposed by Glances; use a narrow VPS-side agent if that is required"],
    )


def normalize_deploy_targets(targets: list[dict[str, Any]]) -> VpsDeployStatus:
    rows = [VpsDeployTarget(**item) for item in targets]
    unhealthy = [item for item in rows if not item.ok]
    return VpsDeployStatus(ok=not unhealthy and bool(rows), targets=rows, total=len(rows), unhealthy=len(unhealthy))


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _int(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None
