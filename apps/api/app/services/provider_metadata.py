"""Static relationships between infrastructure providers and watcher inputs."""

LAB_ALERT_PROVIDER_IDS = frozenset(
    {
        "adguard",
        "emqx",
        "frigate",
        "fritzbox_primary",
        "fritzbox_secondary",
        "homeassistant",
        "mikrotik",
        "nextcloud",
        "opnsense",
        "pbs",
        "proxmox",
        "vps",
    }
)

DIRECT_PROVIDER_WATCHERS: dict[str, tuple[str, ...]] = {
    "cloudflaretunnel": ("cloudflare.tunnel",),
    "frigate": ("thermal.sensors",),
    "fritzbox_primary": ("thermal.sensors",),
    "fritzbox_secondary": ("thermal.sensors",),
    "glances": ("thermal.sensors",),
    "mikrotik": ("thermal.sensors",),
    "nutups": ("power.ups", "thermal.sensors"),
    "opnsense": ("network.gateway", "network.presence", "network.wireguard", "thermal.sensors"),
    "proxmox": ("thermal.sensors",),
    "uptimekuma": ("uptimekuma.monitors",),
    "zerotier": ("network.zerotier",),
}


def watcher_ids_for_provider(provider_id: str) -> list[str]:
    watcher_ids = list(DIRECT_PROVIDER_WATCHERS.get(provider_id, ()))
    if provider_id in LAB_ALERT_PROVIDER_IDS:
        watcher_ids.append("lab.alerts")
    return sorted(watcher_ids)
