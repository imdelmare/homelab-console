"""lab.overview — one-call synthesis of the whole homelab state.

Composes provider healths and a handful of key indicators. Every section is
independent: a failing provider marks its own section as unavailable and
never breaks the rest of the overview.
"""

import asyncio
from datetime import UTC, datetime
from typing import Any

from app.providers.adguard import tools as adguard_tools
from app.providers.errors import ProviderError
from app.providers.frigate import tools as frigate_tools
from app.providers.homeassistant import tools as homeassistant_tools
from app.providers.opnsense import tools as opnsense_tools
from app.providers.proxmox import tools as proxmox_tools
from app.providers.registry import list_providers


async def _section(coro) -> dict[str, Any]:
    try:
        return await coro
    except ProviderError as exc:
        return {"unavailable": exc.code}
    except Exception:
        return {"unavailable": "error"}


def _is_ok(section: dict[str, Any]) -> bool:
    return "unavailable" not in section


async def lab_overview() -> dict:
    providers = list_providers()
    ready = {provider.id: provider.ready() for provider in providers}

    async def _skip() -> dict[str, Any]:
        return {"unavailable": "not_configured"}

    health_task = asyncio.gather(*(provider.health() for provider in providers))
    section_tasks = {
        "proxmox_guests": _section(proxmox_tools.guests_list()) if ready.get("proxmox") else _skip(),
        "gateways": _section(opnsense_tools.gateway_status()) if ready.get("opnsense") else _skip(),
        "wireguard": _section(opnsense_tools.wireguard_status()) if ready.get("opnsense") else _skip(),
        "ha_states": _section(homeassistant_tools.states_summary()) if ready.get("homeassistant") else _skip(),
        "frigate_stats": _section(frigate_tools.stats()) if ready.get("frigate") else _skip(),
        "adguard_stats": _section(adguard_tools.stats()) if ready.get("adguard") else _skip(),
    }

    healths, *section_results = await asyncio.gather(health_task, *section_tasks.values())
    sections = dict(zip(section_tasks.keys(), section_results))

    providers_by_status: dict[str, list[str]] = {}
    for health in healths:
        providers_by_status.setdefault(health.status, []).append(health.provider_id)

    overview: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "providers": {
            "total": len(providers),
            "healthy": len(providers_by_status.get("healthy", [])),
            "by_status": providers_by_status,
        },
    }

    guests = sections["proxmox_guests"]
    if _is_ok(guests):
        items = guests.get("guests", [])
        overview["proxmox"] = {
            "guests_total": len(items),
            "guests_running": len([item for item in items if item.get("status") == "running"]),
            "guests_stopped": [
                item.get("name") or str(item.get("vmid"))
                for item in items
                if item.get("status") != "running"
            ],
        }
    else:
        overview["proxmox"] = guests

    network: dict[str, Any] = {}
    gateways = sections["gateways"]
    if _is_ok(gateways):
        network["gateways_total"] = gateways.get("total", 0)
        network["gateways_offline"] = gateways.get("offline", [])
    wireguard = sections["wireguard"]
    if _is_ok(wireguard):
        network["wireguard_peers_total"] = wireguard.get("peers_total", 0)
        network["wireguard_peers_connected"] = wireguard.get("peers_connected", 0)
        network["wireguard_peers_stale"] = wireguard.get("peers_stale", [])
    if not network:
        network = {"unavailable": gateways.get("unavailable", "not_configured")}
    overview["network"] = network

    ha_states = sections["ha_states"]
    if _is_ok(ha_states):
        summary = ha_states.get("summary", {})
        overview["homeassistant"] = {
            "entities_total": summary.get("entities_total"),
            "problem_entities": summary.get("problem_entities"),
        }
    else:
        overview["homeassistant"] = ha_states

    frigate_stats = sections["frigate_stats"]
    if _is_ok(frigate_stats):
        cameras = frigate_stats.get("cameras", [])
        overview["frigate"] = {
            "cameras_total": len(cameras),
            "detection_fps": frigate_stats.get("service", {}).get("detection_fps"),
        }
    else:
        overview["frigate"] = frigate_stats

    adguard_stats = sections["adguard_stats"]
    if _is_ok(adguard_stats):
        stats = adguard_stats.get("stats", {})
        overview["adguard"] = {
            "dns_queries": stats.get("dns_queries"),
            "blocked_filtering": stats.get("blocked_filtering"),
        }
    else:
        overview["adguard"] = adguard_stats

    return overview
