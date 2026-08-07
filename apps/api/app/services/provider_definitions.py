"""Catalog metadata for providers using shared bounded transports.

Membership is explicit: transport inference must never pull special protocol
providers into the standard lifecycle by accident.
"""

from app.domain.provider_definitions import ProviderDefinition
from app.providers.registry import RESERVED_PROVIDER_IDS, list_providers
from app.services.capability_observations import list_observation_definitions
from app.services.inventory import list_api_provider_instances
from app.tools.registry import list_tools

STANDARD_HTTP_PROVIDER_IDS = frozenset(
    {
        "adguard",
        "cloudflaretunnel",
        "emqx",
        "frigate",
        "homeassistant",
        "mikrotik",
        "nextcloud",
        "opnsense",
        "pbs",
        "proxmox",
        "uptimekuma",
        "vps",
        "zerotier",
    }
)

STANDARD_HTTP_PROVIDER_DRIVERS = {
    "cloudflaretunnel": "cloudflare_tunnel_v1",
    "zerotier": "zerotier_central_legacy_v1",
}

STANDARD_TCP_PROVIDER_DRIVERS = {
    "asterisk": "asterisk_ami_v1",
    "nutups": "nut_upsd_v1",
}
STANDARD_TCP_PROVIDER_IDS = frozenset(STANDARD_TCP_PROVIDER_DRIVERS)
STANDARD_PROVIDER_IDS = STANDARD_HTTP_PROVIDER_IDS | STANDARD_TCP_PROVIDER_IDS

SPECIAL_PROVIDER_IDS = frozenset(
    {
        "fritzbox_primary",
        "fritzbox_secondary",
    }
)


def list_provider_definitions() -> list[ProviderDefinition]:
    instances = {
        instance.id: instance
        for instance in list_api_provider_instances()
        if instance.id not in STANDARD_PROVIDER_IDS
        and instance.id not in SPECIAL_PROVIDER_IDS
        and instance.id not in RESERVED_PROVIDER_IDS
    }
    tools_by_provider: dict[str, list[str]] = {}
    for tool in list_tools():
        if tool.provider_id in STANDARD_PROVIDER_IDS or tool.provider_id in instances:
            tools_by_provider.setdefault(tool.provider_id, []).append(tool.id)
    observations_by_provider: dict[str, list[str]] = {}
    for observation in list_observation_definitions():
        observations_by_provider.setdefault(observation.provider_id, []).append(observation.id)

    return sorted(
        (
            ProviderDefinition(
                id=provider.id,
                name=provider.display_name,
                transport=(
                    "tcp_text" if provider.id in STANDARD_TCP_PROVIDER_IDS else "http_json"
                ),
                driver_id=(
                    instances[provider.id].driver
                    if provider.id in instances
                    else STANDARD_HTTP_PROVIDER_DRIVERS.get(
                        provider.id,
                        STANDARD_TCP_PROVIDER_DRIVERS.get(provider.id, provider.id),
                    )
                ),
                configuration_keys=(
                    (
                        ["account_id", "tunnel_id", "bearer_token"]
                        if instances[provider.id].driver == "cloudflare_tunnel_v1"
                        else ["base_url", "verify_tls", "timeout_seconds", "bearer_token (optional)"]
                    )
                    if provider.id in instances
                    else sorted(provider.credential_requirements)
                ),
                capability_tool_ids=sorted(tools_by_provider.get(provider.id, [])),
                observation_ids=sorted(observations_by_provider.get(provider.id, [])),
                supports_instances=provider.id in instances,
            )
            for provider in list_providers()
            if provider.id in STANDARD_PROVIDER_IDS or provider.id in instances
        ),
        key=lambda item: item.id,
    )
