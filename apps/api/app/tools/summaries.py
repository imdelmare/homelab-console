"""Agent-friendly provider summaries.

These tools compose the narrow read-only provider tools into compact, stable
outputs. They are designed for LLM callers: low token count, predictable keys,
clear findings and suggested next actions.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, Awaitable

from app.providers.adguard import tools as adguard_tools
from app.providers.cloudflaretunnel import tools as cloudflaretunnel_tools
from app.providers.emqx import tools as emqx_tools
from app.providers.errors import ProviderError
from app.providers.frigate import tools as frigate_tools
from app.providers.fritzbox import tools as fritzbox_tools
from app.providers.homeassistant import tools as homeassistant_tools
from app.providers.mikrotik import tools as mikrotik_tools
from app.providers.nextcloud import tools as nextcloud_tools
from app.providers.nutups import tools as nutups_tools
from app.providers.opnsense import tools as opnsense_tools
from app.providers.pbs import tools as pbs_tools
from app.providers.proxmox import tools as proxmox_tools
from app.providers.registry import get_provider, list_providers
from app.providers.uptimekuma import tools as uptimekuma_tools
from app.providers.vps import tools as vps_tools
from app.services.inventory import provider_config
from app.tools import netcheck


Severity = str


async def _section(coro: Awaitable[dict[str, Any]]) -> dict[str, Any]:
    try:
        return await coro
    except ProviderError as exc:
        return {"unavailable": exc.code, "detail": exc.message}
    except Exception as exc:
        return {"unavailable": "error", "detail": exc.__class__.__name__}


async def _health(provider_id: str) -> dict[str, Any]:
    provider = get_provider(provider_id)
    if provider is None:
        return {"status": "unavailable", "detail": "provider not registered"}
    result = await provider.health()
    return {"status": result.status, "detail": result.detail}


def _finding(
    findings: list[dict[str, str]],
    severity: Severity,
    message: str,
    *,
    code: str = "",
) -> None:
    finding = {"severity": severity, "message": message}
    if code:
        finding["code"] = code
    findings.append(finding)


def _severity(status: str, findings: list[dict[str, str]]) -> Severity:
    if status in {"unreachable", "misconfigured", "unavailable"}:
        return "critical"
    if any(item["severity"] == "critical" for item in findings):
        return "critical"
    if status in {"degraded", "unknown"}:
        return "warning"
    if any(item["severity"] == "warning" for item in findings):
        return "warning"
    return "ok"


def _next_actions(findings: list[dict[str, str]]) -> list[str]:
    actions = []
    for item in findings[:5]:
        if item["severity"] == "critical":
            actions.append(f"Investigate now: {item['message']}")
        elif item["severity"] == "warning":
            actions.append(f"Check: {item['message']}")
    return actions


def _summary(provider_id: str, health: dict[str, Any], metrics: dict[str, Any], findings: list[dict[str, str]]) -> dict:
    status = str(health.get("status", "unknown"))
    if status != "healthy":
        detail = str(health.get("detail") or status)
        _finding(
            findings,
            "critical" if status in {"unreachable", "misconfigured", "unavailable"} else "warning",
            detail,
            code="provider_health",
        )
    elif any(item["severity"] in {"critical", "warning"} for item in findings):
        status = "degraded"
    return {
        "summary": {
            "provider_id": provider_id,
            "status": status,
            "severity": _severity(status, findings),
            "generated_at": datetime.now(UTC).isoformat(),
            "metrics": metrics,
            "findings": findings[:12],
            "next_actions": _next_actions(findings),
        }
    }


def _unavailable(section: dict[str, Any]) -> bool:
    return "unavailable" in section


def _pct(used: Any, total: Any) -> float | None:
    if not isinstance(used, (int, float)) or not isinstance(total, (int, float)) or total <= 0:
        return None
    return round(used / total * 100, 1)


def _positive_int(value: Any, default: int = 1) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


async def proxmox_summary() -> dict:
    health, guests, storage, failed_tasks, nodes = await asyncio.gather(
        _health("proxmox"),
        _section(proxmox_tools.guests_list()),
        _section(proxmox_tools.storage_list()),
        _section(proxmox_tools.tasks_failed()),
        _section(proxmox_tools.nodes_list()),
    )
    findings: list[dict[str, str]] = []
    guest_rows = [] if _unavailable(guests) else guests.get("guests", [])
    lxc_rows = [item for item in guest_rows if item.get("guest_type") == "lxc"]
    qemu_rows = [item for item in guest_rows if item.get("guest_type") == "qemu"]
    running_rows = [item for item in guest_rows if item.get("status") == "running"]
    storage_rows = [] if _unavailable(storage) else storage.get("storage", [])
    failed_rows = [] if _unavailable(failed_tasks) else failed_tasks.get("failed_tasks", [])
    node_rows = [] if _unavailable(nodes) else nodes.get("nodes", [])
    config = provider_config("proxmox")
    ignored_stopped_vmids = {
        int(item)
        for item in config.get("ignored_stopped_vmids", [])
        if str(item).isdigit()
    }
    stopped_rows = [item for item in guest_rows if item.get("status") != "running"]
    actionable_stopped = [
        item for item in stopped_rows if item.get("vmid") not in ignored_stopped_vmids
    ]
    stopped = [
        item.get("name") or str(item.get("vmid")) for item in actionable_stopped
    ]
    if stopped:
        _finding(
            findings,
            "warning",
            f"{len(stopped)} Proxmox guest(s) not running",
            code="guests_not_running",
        )
    failed_task_threshold = _positive_int(
        config.get("failed_task_warning_threshold"), 1
    )
    failed_task_max_age_hours = _positive_int(
        config.get("failed_task_max_age_hours"), 24
    )
    failed_cutoff = datetime.now(UTC).timestamp() - failed_task_max_age_hours * 3600
    recent_failed_rows = [
        item
        for item in failed_rows
        if isinstance(item.get("started_at"), (int, float))
        and item["started_at"] >= failed_cutoff
    ]
    if len(recent_failed_rows) >= failed_task_threshold:
        _finding(
            findings,
            "warning",
            f"{len(recent_failed_rows)} Proxmox task(s) failed in the last "
            f"{failed_task_max_age_hours} hour(s)",
            code="recent_failed_tasks",
        )
    high_storage = [
        item.get("id")
        for item in storage_rows
        if (_pct(item.get("used_bytes"), item.get("total_bytes")) or 0) >= 90
    ]
    if high_storage:
        _finding(
            findings,
            "critical",
            f"storage above 90%: {', '.join(map(str, high_storage[:4]))}",
            code="storage_high",
        )
    offline_nodes = [
        item.get("node") for item in node_rows if item.get("status") not in {"online", "unknown"}
    ]
    if offline_nodes:
        _finding(
            findings,
            "critical",
            f"node(s) not online: {', '.join(map(str, offline_nodes[:4]))}",
            code="nodes_offline",
        )
    cpu_threshold = _positive_int(config.get("node_cpu_warning_percent"), 85)
    memory_threshold = _positive_int(config.get("node_memory_warning_percent"), 90)
    hot_cpu_nodes = [
        item.get("node")
        for item in node_rows
        if item.get("status") == "online"
        and ((item.get("cpu_usage") or 0) * 100) >= cpu_threshold
    ]
    if hot_cpu_nodes:
        _finding(
            findings,
            "warning",
            f"node CPU above {cpu_threshold}%: {', '.join(map(str, hot_cpu_nodes[:4]))}",
            code="node_cpu_high",
        )
    hot_memory_nodes = [
        item.get("node")
        for item in node_rows
        if item.get("status") == "online"
        and (_pct(item.get("memory_used_bytes"), item.get("memory_total_bytes")) or 0) >= memory_threshold
    ]
    if hot_memory_nodes:
        _finding(
            findings,
            "warning",
            f"node memory above {memory_threshold}%: {', '.join(map(str, hot_memory_nodes[:4]))}",
            code="node_memory_high",
        )
    result = _summary("proxmox", health, {
        "guests_total": len(guest_rows),
        "guests_running": len(running_rows),
        "guests_stopped": len(stopped_rows),
        "guests_stopped_actionable": len(actionable_stopped),
        "guests_stopped_ignored": len(stopped_rows) - len(actionable_stopped),
        "lxc_total": len(lxc_rows),
        "lxc_running": len([item for item in lxc_rows if item.get("status") == "running"]),
        "qemu_total": len(qemu_rows),
        "qemu_running": len([item for item in qemu_rows if item.get("status") == "running"]),
        "storage_total": len(storage_rows),
        "failed_tasks": len(recent_failed_rows),
        "nodes_total": len(node_rows),
        "nodes_online": len([item for item in node_rows if item.get("status") == "online"]),
        "nodes_cpu_high": len(hot_cpu_nodes),
        "nodes_memory_high": len(hot_memory_nodes),
    }, findings)
    result["running_guests"] = [
        {
            "vmid": item.get("vmid"),
            "name": item.get("name"),
            "guest_type": item.get("guest_type"),
            "node": item.get("node"),
        }
        for item in running_rows
    ]
    return result


async def opnsense_summary() -> dict:
    health, gateways, wireguard, services, firmware = await asyncio.gather(
        _health("opnsense"),
        _section(opnsense_tools.gateway_status()),
        _section(opnsense_tools.wireguard_status()),
        _section(opnsense_tools.services_list()),
        _section(opnsense_tools.firmware_status()),
    )
    findings: list[dict[str, str]] = []
    offline = [] if _unavailable(gateways) else gateways.get("offline", [])
    stale = [] if _unavailable(wireguard) else wireguard.get("peers_stale", [])
    stopped = [] if _unavailable(services) else services.get("stopped", [])
    fw = {} if _unavailable(firmware) else firmware.get("firmware", {})
    if offline:
        _finding(
            findings,
            "critical",
            f"offline gateway(s): {', '.join(offline)}",
            code="gateway_offline",
        )
    if stale:
        _finding(
            findings,
            "warning",
            f"stale WireGuard peer(s): {', '.join(stale)}",
            code="wireguard_stale",
        )
    if stopped:
        _finding(
            findings,
            "warning",
            f"stopped service(s): {', '.join(stopped[:5])}",
            code="services_stopped",
        )
    if fw.get("needs_reboot"):
        _finding(findings, "warning", "OPNsense reboot required", code="reboot_required")
    if fw.get("upgrade_packages"):
        _finding(
            findings,
            "warning",
            f"{fw.get('upgrade_packages')} firmware package(s) can be upgraded",
            code="firmware_updates",
        )
    return _summary("opnsense", health, {
        "gateways_total": 0 if _unavailable(gateways) else gateways.get("total", 0),
        "gateways_offline": len(offline),
        "wireguard_peers_total": 0 if _unavailable(wireguard) else wireguard.get("peers_total", 0),
        "wireguard_peers_connected": 0 if _unavailable(wireguard) else wireguard.get("peers_connected", 0),
        "services_total": 0 if _unavailable(services) else services.get("total", 0),
        "services_stopped": len(stopped),
        "upgrade_packages": fw.get("upgrade_packages", 0),
    }, findings)


async def homeassistant_summary() -> dict:
    health, states, errors = await asyncio.gather(
        _health("homeassistant"),
        _section(homeassistant_tools.states_summary()),
        _section(homeassistant_tools.error_log_tail(lines=40)),
    )
    findings: list[dict[str, str]] = []
    state_summary = {} if _unavailable(states) else states.get("summary", {})
    problem_count = int(state_summary.get("problem_entities") or 0)
    config = provider_config("homeassistant")
    problem_threshold = _positive_int(
        config.get("problem_entity_warning_threshold"), 1
    )
    error_threshold = _positive_int(config.get("error_log_warning_threshold"), 1)
    if problem_count >= problem_threshold:
        _finding(
            findings,
            "warning",
            f"{problem_count} Home Assistant entity/entities unavailable or unknown",
            code="problem_entities",
        )
    error_lines = [] if _unavailable(errors) else errors.get("lines", [])
    if len(error_lines) >= error_threshold:
        _finding(
            findings,
            "warning",
            f"{len(error_lines)} recent Home Assistant error log line(s)",
            code="recent_error_log",
        )
    return _summary("homeassistant", health, {
        "entities_total": state_summary.get("entities_total", 0),
        "problem_entities": problem_count,
        "domains_total": len(state_summary.get("domains", {})),
        "error_lines": len(error_lines),
    }, findings)


async def frigate_summary() -> dict:
    health, stats, config = await asyncio.gather(
        _health("frigate"),
        _section(frigate_tools.stats()),
        _section(frigate_tools.config_summary()),
    )
    findings: list[dict[str, str]] = []
    cameras = [] if _unavailable(config) else config.get("cameras", [])
    disabled = [item.get("name") for item in cameras if item.get("enabled") is False]
    if disabled:
        _finding(
            findings,
            "warning",
            f"disabled camera(s): {', '.join(map(str, disabled[:5]))}",
            code="cameras_disabled",
        )
    stalled = [
        item.get("name")
        for item in cameras
        if item.get("enabled") is not False
        and item.get("camera_fps") is not None
        and float(item.get("camera_fps") or 0) <= 0
    ]
    if stalled:
        _finding(
            findings,
            "warning",
            f"camera(s) enabled but producing no frames: {', '.join(map(str, stalled[:5]))}",
            code="cameras_stalled",
        )
    cfg = {} if _unavailable(config) else config.get("config", {})
    service = {} if _unavailable(stats) else stats.get("service", {})
    if cfg.get("safe_mode"):
        _finding(findings, "critical", "Frigate safe mode is active", code="safe_mode")
    return _summary("frigate", health, {
        "cameras_total": cfg.get("cameras_total", len(cameras)),
        "cameras_disabled": len(disabled),
        "cameras_stalled": len(stalled),
        "detection_fps": service.get("detection_fps"),
        "record_enabled": cfg.get("record_enabled"),
        "snapshots_enabled": cfg.get("snapshots_enabled"),
    }, findings)


async def adguard_summary() -> dict:
    health, status, stats = await asyncio.gather(
        _health("adguard"),
        _section(adguard_tools.status()),
        _section(adguard_tools.stats()),
    )
    findings: list[dict[str, str]] = []
    state = {} if _unavailable(status) else status.get("status", {})
    dns = {} if _unavailable(stats) else stats.get("stats", {})
    if state.get("running") is False:
        _finding(findings, "critical", "AdGuard is not running")
    if state.get("protection_enabled") is False:
        _finding(findings, "critical", "AdGuard protection is disabled")
    return _summary("adguard", health, {
        "running": state.get("running"),
        "protection_enabled": state.get("protection_enabled"),
        "dns_queries": dns.get("dns_queries"),
        "blocked_filtering": dns.get("blocked_filtering"),
        "avg_processing_time_ms": dns.get("avg_processing_time_ms"),
    }, findings)


async def nextcloud_summary() -> dict:
    health, status, serverinfo = await asyncio.gather(
        _health("nextcloud"),
        _section(nextcloud_tools.status()),
        _section(nextcloud_tools.serverinfo()),
    )
    findings: list[dict[str, str]] = []
    state = {} if _unavailable(status) else status.get("status", {})
    info = {} if _unavailable(serverinfo) else serverinfo.get("serverinfo", {})
    if state.get("maintenance"):
        _finding(findings, "critical", "Nextcloud maintenance mode is active")
    if state.get("needs_db_upgrade"):
        _finding(findings, "critical", "Nextcloud needs database upgrade")
    return _summary("nextcloud", health, {
        "version": state.get("version") or info.get("version", ""),
        "maintenance": state.get("maintenance"),
        "needs_db_upgrade": state.get("needs_db_upgrade"),
        "files_total": info.get("files_total"),
        "users_total": info.get("users_total"),
        "active_users_last_day": info.get("active_users_last_day"),
        "freespace_bytes": info.get("freespace_bytes"),
    }, findings)


async def mikrotik_summary() -> dict:
    health, resource, interfaces, lte = await asyncio.gather(
        _health("mikrotik"),
        _section(mikrotik_tools.system_resource()),
        _section(mikrotik_tools.interfaces_list()),
        _section(mikrotik_tools.lte_status()),
    )
    findings: list[dict[str, str]] = []
    res = {} if _unavailable(resource) else resource.get("resource", {})
    rows = [] if _unavailable(interfaces) else interfaces.get("interfaces", [])
    disabled = [item.get("name") for item in rows if item.get("disabled")]
    down = [item.get("name") for item in rows if not item.get("running") and not item.get("disabled")]
    if down:
        _finding(findings, "warning", f"interface(s) down: {', '.join(map(str, down[:5]))}")
    memory_used = None
    if isinstance(res.get("memory_total_bytes"), (int, float)) and isinstance(res.get("memory_free_bytes"), (int, float)):
        memory_used = res["memory_total_bytes"] - res["memory_free_bytes"]
    return _summary("mikrotik", health, {
        "version": res.get("version", ""),
        "board_name": res.get("board_name", ""),
        "cpu_load_percent": res.get("cpu_load_percent"),
        "memory_used_percent": _pct(memory_used, res.get("memory_total_bytes")),
        "interfaces_total": len(rows),
        "interfaces_down": len(down),
        "interfaces_disabled": len(disabled),
        "lte_interfaces": 0 if _unavailable(lte) else lte.get("total", 0),
    }, findings)


async def uptimekuma_summary() -> dict:
    health, monitors = await asyncio.gather(_health("uptimekuma"), _section(uptimekuma_tools.monitors_status()))
    findings: list[dict[str, str]] = []
    by_status = {} if _unavailable(monitors) else monitors.get("by_status", {})
    monitor_rows = [] if _unavailable(monitors) else monitors.get("monitors", [])
    down_monitors = [
        {
            "name": item.get("name", ""),
            "type": item.get("type", ""),
            "target": item.get("target", ""),
            "status": item.get("status", ""),
        }
        for item in monitor_rows
        if item.get("status") == "down"
    ]
    down = int(by_status.get("down") or 0)
    maintenance = int(by_status.get("maintenance") or 0)
    if down:
        names = ", ".join(item["name"] or item["target"] or "unnamed" for item in down_monitors[:5])
        _finding(
            findings,
            "critical",
            f"{down} Uptime Kuma monitor(s) down: {names}",
            code="monitors_down",
        )
    if maintenance:
        _finding(
            findings,
            "warning",
            f"{maintenance} monitor(s) in maintenance",
            code="monitors_maintenance",
        )
    return _summary("uptimekuma", health, {
        "monitors_total": 0 if _unavailable(monitors) else monitors.get("total", 0),
        "monitors_up": int(by_status.get("up") or 0),
        "monitors_down": down,
        "monitors_maintenance": maintenance,
        "down_monitors": down_monitors,
    }, findings)


async def emqx_summary() -> dict:
    health, nodes, stats = await asyncio.gather(
        _health("emqx"),
        _section(emqx_tools.nodes_list()),
        _section(emqx_tools.stats()),
    )
    findings: list[dict[str, str]] = []
    node_rows = [] if _unavailable(nodes) else nodes.get("nodes", [])
    stopped = [item.get("node") for item in node_rows if item.get("status") != "running"]
    if stopped:
        _finding(findings, "critical", f"EMQX node(s) not running: {', '.join(map(str, stopped))}")
    values = {} if _unavailable(stats) else stats.get("stats", {})
    return _summary("emqx", health, {
        "nodes_total": len(node_rows),
        "nodes_running": len(node_rows) - len(stopped),
        "connections": values.get("connections"),
        "live_connections": values.get("live_connections"),
        "subscriptions": values.get("subscriptions"),
        "topics": values.get("topics"),
    }, findings)


async def cloudflare_summary() -> dict:
    health, tunnels, connectors = await asyncio.gather(
        _health("cloudflaretunnel"),
        _section(cloudflaretunnel_tools.all_tunnels_status()),
        _section(cloudflaretunnel_tools.connectors_list()),
    )
    findings: list[dict[str, str]] = []
    tunnel_rows = [] if _unavailable(tunnels) else tunnels.get("tunnels", [])
    unavailable = [
        item for item in tunnel_rows if item.get("status") == "unavailable"
    ]
    degraded = [item for item in tunnel_rows if item.get("status") == "degraded"]
    if unavailable:
        names = ", ".join(
            str(item.get("name") or item.get("id") or "unnamed")
            for item in unavailable[:5]
        )
        _finding(
            findings,
            "critical",
            f"Cloudflare tunnel(s) down or inactive: {names}",
            code="tunnel_unavailable",
        )
    if degraded:
        names = ", ".join(
            str(item.get("name") or item.get("id") or "unnamed")
            for item in degraded[:5]
        )
        _finding(
            findings,
            "warning",
            f"Cloudflare tunnel(s) degraded: {names}",
            code="tunnel_degraded",
        )
    connector_values = {} if _unavailable(connectors) else connectors
    active_connections = int(connector_values.get("connections_active") or 0)
    pending_reconnect = int(
        connector_values.get("connections_pending_reconnect") or 0
    )
    if tunnel_rows and active_connections == 0:
        _finding(
            findings,
            "critical",
            "No active cloudflared connections",
            code="no_active_connections",
        )
    if pending_reconnect:
        _finding(
            findings,
            "warning",
            f"{pending_reconnect} cloudflared connection(s) pending reconnect",
            code="connections_pending_reconnect",
        )
    return _summary(
        "cloudflaretunnel",
        health,
        {
            "tunnels_total": len(tunnel_rows),
            "tunnels_healthy": len(
                [item for item in tunnel_rows if item.get("status") == "healthy"]
            ),
            "tunnels_degraded": len(degraded),
            "tunnels_unavailable": len(unavailable),
            "connectors_total": int(connector_values.get("connectors_total") or 0),
            "connections_total": int(connector_values.get("connections_total") or 0),
            "connections_active": active_connections,
            "connections_pending_reconnect": pending_reconnect,
        },
        findings,
    )


async def fritzbox_summary(provider_id: str) -> dict:
    health, device, wan, wifi = await asyncio.gather(
        _health(provider_id),
        _section(fritzbox_tools.device_info(provider_id)),
        _section(fritzbox_tools.wan_status(provider_id)),
        _section(fritzbox_tools.wifi_summary(provider_id)),
    )
    findings: list[dict[str, str]] = []
    dev = {} if _unavailable(device) else device.get("device", {})
    wan_values = {} if _unavailable(wan) else wan.get("wan", {})
    radios = [] if _unavailable(wifi) else wifi.get("wifi", [])
    ignored_disabled = _ignored_fritzbox_disabled_wifi_indexes(provider_id)
    disabled_all = [str(item.get("index")) for item in radios if item.get("enabled") is False]
    disabled_actionable = [
        str(item.get("index"))
        for item in radios
        if item.get("enabled") is False and item.get("index") not in ignored_disabled
    ]
    if wan.get("available") is False:
        if provider_id != "fritzbox_secondary":
            _finding(findings, "warning", "WAN section is not exposed by this FritzBox")
    elif wan_values.get("physical_link_status") not in ("", "Up"):
        _finding(findings, "critical", f"WAN physical link is {wan_values.get('physical_link_status')}")
    if disabled_actionable:
        _finding(
            findings,
            "warning",
            f"{len(disabled_actionable)} disabled Wi-Fi radio(s), indexes: {', '.join(disabled_actionable)}",
        )
    return _summary(provider_id, health, {
        "model": dev.get("model", ""),
        "software_version": dev.get("software_version", ""),
        "uptime_seconds": dev.get("uptime_seconds"),
        "wan_available": wan.get("available", True) is not False,
        "wan_link": wan_values.get("physical_link_status", ""),
        "downstream_max_mbps": wan_values.get("downstream_max_mbps"),
        "wifi_radios": len(radios),
        "wifi_radios_disabled": len(disabled_all),
        "wifi_radios_disabled_actionable": len(disabled_actionable),
        "wifi_radios_disabled_ignored": len(disabled_all) - len(disabled_actionable),
    }, findings)


def _ignored_fritzbox_disabled_wifi_indexes(provider_id: str) -> set[int]:
    configured = provider_config(provider_id).get("ignored_disabled_wifi_indexes", [3])
    if not isinstance(configured, list):
        return {3}
    ignored = set()
    for item in configured:
        try:
            ignored.add(int(item))
        except (TypeError, ValueError):
            continue
    return ignored


async def fritzbox_primary_summary() -> dict:
    return await fritzbox_summary("fritzbox_primary")


async def fritzbox_secondary_summary() -> dict:
    return await fritzbox_summary("fritzbox_secondary")


async def pbs_summary() -> dict:
    return await pbs_tools.summary()


async def nutups_summary() -> dict:
    return await nutups_tools.summary()


async def vps_summary() -> dict:
    return await vps_tools.summary()


async def lab_summary() -> dict:
    providers = list_providers()
    provider_status = await asyncio.gather(*(provider.health() for provider in providers))
    sections = await asyncio.gather(
        _section(proxmox_summary()),
        _section(pbs_summary()),
        _section(vps_summary()),
        _section(opnsense_summary()),
        _section(homeassistant_summary()),
        _section(frigate_summary()),
        _section(adguard_summary()),
        _section(nextcloud_summary()),
        _section(nutups_summary()),
        _section(mikrotik_summary()),
        _section(uptimekuma_summary()),
        _section(emqx_summary()),
        _section(cloudflare_summary()),
        _section(fritzbox_primary_summary()),
        _section(fritzbox_secondary_summary()),
    )
    summaries = [item.get("summary", item) for item in sections]
    findings = [
        {"provider_id": item.get("provider_id", ""), **finding}
        for item in summaries
        for finding in item.get("findings", [])
    ]
    by_status: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    for health in provider_status:
        by_status[health.status] = by_status.get(health.status, 0) + 1
    for item in summaries:
        severity = str(item.get("severity", "unknown"))
        by_severity[severity] = by_severity.get(severity, 0) + 1
    return {
        "summary": {
            "provider_id": "lab",
            "status": "healthy" if by_status.get("healthy", 0) == len(provider_status) else "degraded",
            "severity": "critical" if by_severity.get("critical") else "warning" if by_severity.get("warning") else "ok",
            "generated_at": datetime.now(UTC).isoformat(),
            "metrics": {
                "providers_total": len(provider_status),
                "providers_by_status": by_status,
                "sections_by_severity": by_severity,
                "findings_total": len(findings),
            },
            "findings": findings[:20],
            "next_actions": _next_actions(findings),
        },
        "sections": summaries,
    }


async def lab_network_summary() -> dict:
    health, opnsense, mikrotik, adguard, fritz_primary, fritz_secondary = await asyncio.gather(
        _health("opnsense"),
        _section(opnsense_summary()),
        _section(mikrotik_summary()),
        _section(adguard_summary()),
        _section(fritzbox_primary_summary()),
        _section(fritzbox_secondary_summary()),
    )
    sections = [item.get("summary", item) for item in (opnsense, mikrotik, adguard, fritz_primary, fritz_secondary)]
    findings = [
        {"provider_id": section.get("provider_id", ""), **finding}
        for section in sections
        for finding in section.get("findings", [])
    ]
    metrics = {
        "gateways_offline": opnsense.get("summary", {}).get("metrics", {}).get("gateways_offline", 0),
        "wireguard_peers_connected": opnsense.get("summary", {}).get("metrics", {}).get("wireguard_peers_connected", 0),
        "mikrotik_interfaces_down": mikrotik.get("summary", {}).get("metrics", {}).get("interfaces_down", 0),
        "dns_protection_enabled": adguard.get("summary", {}).get("metrics", {}).get("protection_enabled"),
        "fritzbox_primary_wan": fritz_primary.get("summary", {}).get("metrics", {}).get("wan_link", ""),
        "fritzbox_secondary_wan_available": fritz_secondary.get("summary", {}).get("metrics", {}).get("wan_available"),
    }
    return _summary("lab.network", health, metrics, findings)


async def lab_security_summary() -> dict:
    health, opnsense, adguard, nextcloud, uptimekuma, certificates = await asyncio.gather(
        _health("opnsense"),
        _section(opnsense_summary()),
        _section(adguard_summary()),
        _section(nextcloud_summary()),
        _section(uptimekuma_summary()),
        _section(netcheck.tls_certificates()),
    )
    sections = [item.get("summary", item) for item in (opnsense, adguard, nextcloud, uptimekuma)]
    findings = [
        {"provider_id": section.get("provider_id", ""), **finding}
        for section in sections
        for finding in section.get("findings", [])
    ]
    certificate_rows = [] if _unavailable(certificates) else certificates.get("certificates", [])
    expiring_certificates = [
        item
        for item in certificate_rows
        if isinstance(item, dict)
        and isinstance(item.get("days_until_expiry"), (int, float))
        and item["days_until_expiry"] <= item.get("warning_days", 21)
    ]
    critical_certificates = [
        item
        for item in expiring_certificates
        if item["days_until_expiry"] <= item.get("critical_days", 7)
    ]
    unreachable_certificates = [] if _unavailable(certificates) else certificates.get("unreachable", [])
    if not _unavailable(certificates) and not certificate_rows:
        _finding(
            findings,
            "warning",
            "TLS certificate coverage has no configured targets",
            code="tls_coverage_empty",
        )
    if unreachable_certificates:
        _finding(
            findings,
            "warning",
            f"{len(unreachable_certificates)} TLS certificate target(s) could not be observed",
            code="tls_targets_unreachable",
        )
    if expiring_certificates:
        _finding(
            findings,
            "critical" if critical_certificates else "warning",
            f"{len(expiring_certificates)} TLS certificate(s) are expired or nearing expiry",
            code="tls_certificates_expiring",
        )
    metrics = {
        "dns_protection_enabled": adguard.get("summary", {}).get("metrics", {}).get("protection_enabled"),
        "opnsense_upgrade_packages": opnsense.get("summary", {}).get("metrics", {}).get("upgrade_packages", 0),
        "nextcloud_maintenance": nextcloud.get("summary", {}).get("metrics", {}).get("maintenance"),
        "nextcloud_needs_db_upgrade": nextcloud.get("summary", {}).get("metrics", {}).get("needs_db_upgrade"),
        "monitors_down": uptimekuma.get("summary", {}).get("metrics", {}).get("monitors_down", 0),
        "tls_targets_total": len(certificate_rows),
        "tls_targets_unreachable": len(unreachable_certificates),
        "tls_certificates_expiring": len(expiring_certificates),
        "tls_certificates_critical": len(critical_certificates),
    }
    return _summary("lab.security", health, metrics, findings)


async def lab_storage_summary() -> dict:
    health, proxmox, pbs, nextcloud, nutups, disks, backups = await asyncio.gather(
        _health("proxmox"),
        _section(proxmox_summary()),
        _section(pbs_summary()),
        _section(nextcloud_summary()),
        _section(nutups_summary()),
        _section(proxmox_tools.disks_temperatures()),
        _section(proxmox_tools.backups_list()),
    )
    sections = [item.get("summary", item) for item in (proxmox, pbs, nextcloud, nutups)]
    findings = [
        {"provider_id": section.get("provider_id", ""), **finding}
        for section in sections
        for finding in section.get("findings", [])
    ]
    disk_rows = [] if _unavailable(disks) else disks.get("disks", [])
    unhealthy_disks = [
        item
        for item in disk_rows
        if isinstance(item, dict)
        and str(item.get("health") or "").strip().lower()
        not in {"", "passed", "ok", "unknown"}
    ]
    wearout_values = [
        float(item["wearout"])
        for item in disk_rows
        if isinstance(item, dict) and isinstance(item.get("wearout"), (int, float))
    ]
    wearout_threshold = _positive_int(
        provider_config("proxmox").get("disk_wearout_warning_percent"), 20
    )
    low_wearout_disks = [value for value in wearout_values if value <= wearout_threshold]
    if unhealthy_disks:
        _finding(
            findings,
            "critical",
            f"{len(unhealthy_disks)} disk(s) report failing SMART health",
            code="disk_health_failed",
        )
    if low_wearout_disks:
        _finding(
            findings,
            "warning",
            f"{len(low_wearout_disks)} disk(s) at or below {wearout_threshold}% remaining life",
            code="disk_wearout_low",
        )
    proxmox_backup_rows = [] if _unavailable(backups) else backups.get("backups_by_guest", [])
    pbs_metrics = pbs.get("summary", {}).get("metrics", {})
    metrics = {
        "proxmox_storage_total": proxmox.get("summary", {}).get("metrics", {}).get("storage_total", 0),
        "proxmox_failed_tasks": proxmox.get("summary", {}).get("metrics", {}).get("failed_tasks", 0),
        "proxmox_disks_total": len(disk_rows),
        "proxmox_disks_unhealthy": len(unhealthy_disks),
        "proxmox_disks_wearout_low": len(low_wearout_disks),
        "proxmox_disks_wearout_min": min(wearout_values) if wearout_values else None,
        "proxmox_backup_guests": len(proxmox_backup_rows),
        "pbs_backup_groups_total": pbs_metrics.get("backup_groups_total", 0),
        "pbs_backup_groups_stale": pbs_metrics.get("backup_groups_stale", 0),
        "pbs_backup_oldest_age_days": pbs_metrics.get("backup_oldest_age_days"),
        "nextcloud_files_total": nextcloud.get("summary", {}).get("metrics", {}).get("files_total"),
        "nextcloud_freespace_bytes": nextcloud.get("summary", {}).get("metrics", {}).get("freespace_bytes"),
        "ups_status": nutups.get("summary", {}).get("metrics", {}).get("status", "unknown"),
        "ups_battery_charge_percent": nutups.get("summary", {}).get("metrics", {}).get("battery_charge_percent"),
    }
    return _summary("lab.storage", health, metrics, findings)


async def lab_automation_summary() -> dict:
    health, homeassistant, frigate, emqx = await asyncio.gather(
        _health("homeassistant"),
        _section(homeassistant_summary()),
        _section(frigate_summary()),
        _section(emqx_summary()),
    )
    sections = [item.get("summary", item) for item in (homeassistant, frigate, emqx)]
    findings = [
        {"provider_id": section.get("provider_id", ""), **finding}
        for section in sections
        for finding in section.get("findings", [])
    ]
    metrics = {
        "ha_entities_total": homeassistant.get("summary", {}).get("metrics", {}).get("entities_total", 0),
        "ha_problem_entities": homeassistant.get("summary", {}).get("metrics", {}).get("problem_entities", 0),
        "frigate_cameras_total": frigate.get("summary", {}).get("metrics", {}).get("cameras_total", 0),
        "frigate_cameras_disabled": frigate.get("summary", {}).get("metrics", {}).get("cameras_disabled", 0),
        "frigate_cameras_stalled": frigate.get("summary", {}).get("metrics", {}).get("cameras_stalled", 0),
        "emqx_nodes_running": emqx.get("summary", {}).get("metrics", {}).get("nodes_running", 0),
        "emqx_live_connections": emqx.get("summary", {}).get("metrics", {}).get("live_connections"),
    }
    return _summary("lab.automation", health, metrics, findings)


async def lab_alerts_recent() -> dict:
    lab = await lab_summary()
    summary = lab.get("summary", {})
    findings = [
        item for item in summary.get("findings", [])
        if not _owned_by_direct_watcher(item)
    ]
    critical = [item for item in findings if item.get("severity") == "critical"]
    warning = [item for item in findings if item.get("severity") == "warning"]
    status = "degraded" if findings else "healthy"
    return {
        "summary": {
            "provider_id": "lab.alerts",
            "status": status,
            "severity": "critical" if critical else "warning" if warning else "ok",
            "generated_at": datetime.now(UTC).isoformat(),
            "metrics": {
                "alerts_total": len(findings),
                "critical": len(critical),
                "warning": len(warning),
            },
            "findings": findings[:20],
            "next_actions": _next_actions(findings),
        }
    }


def _owned_by_direct_watcher(finding: dict[str, Any]) -> bool:
    provider_id = str(finding.get("provider_id") or "")
    code = str(finding.get("code") or "")
    if provider_id in {"cloudflaretunnel", "nutups", "uptimekuma"}:
        return True
    return provider_id == "opnsense" and code in {
        "gateway_offline",
        "wireguard_stale",
    }
