"""Tool definitions: the complete, explicit catalog of capabilities.

Every tool declares a Pydantic input model with ``extra="forbid"``. There are
no generic tools: no shell, no SSH, no arbitrary HTTP, no raw API forwarding.
"""

from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.providers.adguard import tools as adguard_tools
from app.providers.asterisk import tools as asterisk_tools
from app.providers.cloudflaretunnel import tools as cloudflaretunnel_tools
from app.providers.emqx import tools as emqx_tools
from app.providers.frigate import tools as frigate_tools
from app.providers.fritzbox import tools as fritzbox_tools
from app.providers.glances import tools as glances_tools
from app.providers.homeassistant import tools as homeassistant_tools
from app.providers.mikrotik import tools as mikrotik_tools
from app.providers.nextcloud import tools as nextcloud_tools
from app.providers.nutups import tools as nutups_tools
from app.providers.opnsense import tools as opnsense_tools
from app.providers.pbs import tools as pbs_tools
from app.providers.proxmox import tools as proxmox_tools
from app.providers.proxmox.models import ProxmoxTopologySnapshot
from app.providers.uptimekuma import tools as uptimekuma_tools
from app.providers.vps import tools as vps_tools
from app.providers.zerotier import tools as zerotier_tools
from app.providers.api_ready import tools as api_ready_tools
from app.services.inventory import list_api_provider_instances, tool_overrides
from app.services.notification_outbox import list_recent as notification_list_recent
from app.services.notification_outbox import status_summary as notification_status_summary
from app.tools import dnscheck, egress, netcheck, overview, summaries
from app.tools.governance import APPROVED_WRITE_TOOLS

ToolMode = Literal["read", "write"]
ToolRisk = Literal["low", "medium", "high", "critical"]


class EmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NotificationListInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=25, ge=1, le=100)


class GuestFilterInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")


class ProxmoxLxcPowerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vmid: int = Field(ge=1, le=999_999_999)


class ProxmoxLxcState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vmid: int
    name: str
    node: str
    status: str


class ProxmoxLxcPowerOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["start", "shutdown"]
    changed: bool
    critical_target: bool
    previous_state: ProxmoxLxcState
    post_state: ProxmoxLxcState
    verified: bool


class WolWakeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_id: str = Field(max_length=64, pattern=r"^[a-z][a-z0-9._-]*$")


class WolWakeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_id: str
    sent: bool
    provider: Literal["opnsense"]


class GatewayTransitionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["failover", "restore"]
    changed: bool
    target_gateway: str
    primary_enabled: bool
    backup_enabled: bool
    default_route_gateway: str
    verified: bool


class EgressSwitchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: Literal["direct", "vpn_de"]


class EgressSwitchOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: Literal["direct", "vpn_de"]
    changed: bool
    target_gateway: str
    default_route_gateway: str
    vpn_connected: bool
    verified: bool


class HostCheckInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host_id: str = Field(max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    ports: list[int] | None = Field(default=None, max_length=netcheck.MAX_PORTS)
    timeout: float = Field(default=1.5, ge=0.1, le=5.0)

    def validated_ports(self) -> list[int] | None:
        if self.ports is None:
            return None
        return [port for port in self.ports if 1 <= port <= 65535]


class NetworkClientsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    tag: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    limit: int = Field(default=100, ge=1, le=300)


class DnsResolveInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_id: str = Field(max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    resolver_id: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")


class DnsTargetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_id: str = Field(max_length=64, pattern=r"^[A-Za-z0-9._-]+$")


class AdguardPauseInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    duration_minutes: Literal[5, 15, 30, 60] = 5


class AdguardProtectionState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protection_enabled: bool
    disabled_duration_ms: int


class AdguardPauseOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requested_duration_minutes: int
    post_state: AdguardProtectionState
    verified: bool


class AdguardResumeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    post_state: AdguardProtectionState
    verified: bool


class HomeAssistantStatesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain: str | None = Field(default=None, max_length=48, pattern=r"^[a-zA-Z0-9_]+$")
    query: str | None = Field(default=None, max_length=80)
    limit: int = Field(default=100, ge=1, le=300)


class HomeAssistantLogInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lines: int = Field(default=80, ge=1, le=300)


class HomeAssistantLogbookInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hours: int = Field(default=2, ge=1, le=24)
    limit: int = Field(default=100, ge=1, le=300)


class FrigateRecentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=20, ge=1, le=100)


class StatusPageSlugInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(max_length=64, pattern=r"^[a-z0-9-]+$")


class NodeStatusInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node: str = Field(max_length=64, pattern=r"^[A-Za-z0-9._-]+$")


class ToolDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    id: str
    name: str
    description: str
    provider_id: str
    category: str
    mode: ToolMode
    risk: ToolRisk
    enabled: bool = True
    timeout_seconds: float = 10.0
    requires_confirmation: bool = False
    input_model: type[BaseModel] = EmptyInput
    output_model: type[BaseModel] | None = None
    runner: Callable[[Any], Awaitable[dict]] | None = None

    def public_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "provider_id": self.provider_id,
            "category": self.category,
            "mode": self.mode,
            "risk": self.risk,
            "enabled": self.enabled,
            "timeout_seconds": self.timeout_seconds,
            "requires_confirmation": self.requires_confirmation,
            "input_schema": self.input_model.model_json_schema(),
        }


async def _run_proxmox_version(_: BaseModel) -> dict:
    return await proxmox_tools.version()


async def _run_proxmox_cluster_status(_: BaseModel) -> dict:
    return await proxmox_tools.cluster_status()


async def _run_proxmox_nodes(_: BaseModel) -> dict:
    return await proxmox_tools.nodes_list()


async def _run_proxmox_resources(_: BaseModel) -> dict:
    return await proxmox_tools.resources_list()


async def _run_proxmox_guests(payload: GuestFilterInput) -> dict:
    return await proxmox_tools.guests_list(node=payload.node)


async def _run_proxmox_topology(_: BaseModel) -> dict:
    return await proxmox_tools.topology_snapshot()


async def _run_proxmox_vms(payload: GuestFilterInput) -> dict:
    return await proxmox_tools.guests_list(node=payload.node, guest_type="qemu")


async def _run_proxmox_lxc(payload: GuestFilterInput) -> dict:
    return await proxmox_tools.guests_list(node=payload.node, guest_type="lxc")


async def _run_proxmox_storage(_: BaseModel) -> dict:
    return await proxmox_tools.storage_list()


async def _run_proxmox_tasks_failed(_: BaseModel) -> dict:
    return await proxmox_tools.tasks_failed()


async def _run_pbs_version(_: BaseModel) -> dict:
    return await pbs_tools.version()


async def _run_pbs_datastores_status(_: BaseModel) -> dict:
    return await pbs_tools.datastores_status()


async def _run_pbs_tasks_recent(_: BaseModel) -> dict:
    return await pbs_tools.tasks_recent()


async def _run_pbs_verify_jobs_health(_: BaseModel) -> dict:
    return await pbs_tools.verify_jobs_health()


async def _run_pbs_backup_jobs_health(_: BaseModel) -> dict:
    return await pbs_tools.backup_jobs_health()


async def _run_pbs_summary(_: BaseModel) -> dict:
    return await pbs_tools.summary()


async def _run_host_check(payload: HostCheckInput) -> dict:
    return await netcheck.host_check(payload.host_id, payload.validated_ports(), payload.timeout)


async def _run_network_clients(payload: NetworkClientsInput) -> dict:
    return await netcheck.clients_list(kind=payload.kind, tag=payload.tag, limit=payload.limit)


async def _run_tls_certificates(_: BaseModel) -> dict:
    return await netcheck.tls_certificates()


async def _run_dns_resolve(payload: DnsResolveInput) -> dict:
    return await dnscheck.dns_resolve(payload.target_id, payload.resolver_id)


async def _run_dns_path_check(payload: DnsTargetInput) -> dict:
    return await dnscheck.dns_path_check(payload.target_id)


async def _run_dns_adguard_health(_: BaseModel) -> dict:
    return await dnscheck.adguard_dns_health()


async def _run_egress_status(_: BaseModel) -> dict:
    return await egress.status()


async def _run_dns_summary(_: BaseModel) -> dict:
    return await dnscheck.dns_summary()


async def _run_proxmox_node_status(payload: NodeStatusInput) -> dict:
    return await proxmox_tools.node_status(payload.node)


async def _run_proxmox_disks_temperatures(_: BaseModel) -> dict:
    return await proxmox_tools.disks_temperatures()


async def _run_proxmox_backups(_: BaseModel) -> dict:
    return await proxmox_tools.backups_list()


async def _run_opnsense_wireguard(_: BaseModel) -> dict:
    return await opnsense_tools.wireguard_status()


async def _run_opnsense_services(_: BaseModel) -> dict:
    return await opnsense_tools.services_list()


async def _run_lab_overview(_: BaseModel) -> dict:
    return await overview.lab_overview()


async def _run_lab_summary(_: BaseModel) -> dict:
    return await summaries.lab_summary()


async def _run_lab_network_summary(_: BaseModel) -> dict:
    return await summaries.lab_network_summary()


async def _run_lab_security_summary(_: BaseModel) -> dict:
    return await summaries.lab_security_summary()


async def _run_lab_storage_summary(_: BaseModel) -> dict:
    return await summaries.lab_storage_summary()


async def _run_lab_automation_summary(_: BaseModel) -> dict:
    return await summaries.lab_automation_summary()


async def _run_lab_alerts_recent(_: BaseModel) -> dict:
    return await summaries.lab_alerts_recent()


async def _run_homeassistant_api_status(_: BaseModel) -> dict:
    return await homeassistant_tools.api_status()


async def _run_homeassistant_config(_: BaseModel) -> dict:
    return await homeassistant_tools.config()


async def _run_homeassistant_states_summary(_: BaseModel) -> dict:
    return await homeassistant_tools.states_summary()


async def _run_homeassistant_states_list(payload: HomeAssistantStatesInput) -> dict:
    return await homeassistant_tools.states_list(domain=payload.domain, query=payload.query, limit=payload.limit)


async def _run_homeassistant_services(_: BaseModel) -> dict:
    return await homeassistant_tools.services()


async def _run_homeassistant_error_log(payload: HomeAssistantLogInput) -> dict:
    return await homeassistant_tools.error_log_tail(lines=payload.lines)


async def _run_homeassistant_logbook(payload: HomeAssistantLogbookInput) -> dict:
    return await homeassistant_tools.logbook_recent(hours=payload.hours, limit=payload.limit)


async def _run_opnsense_firmware_status(_: BaseModel) -> dict:
    return await opnsense_tools.firmware_status()


async def _run_opnsense_system_status(_: BaseModel) -> dict:
    return await opnsense_tools.system_status()


async def _run_opnsense_system_information(_: BaseModel) -> dict:
    return await opnsense_tools.system_information()


async def _run_opnsense_system_resources(_: BaseModel) -> dict:
    return await opnsense_tools.system_resources()


async def _run_opnsense_system_temperature(_: BaseModel) -> dict:
    return await opnsense_tools.system_temperature()


async def _run_opnsense_interface_names(_: BaseModel) -> dict:
    return await opnsense_tools.interface_names()


async def _run_opnsense_interface_statistics(_: BaseModel) -> dict:
    return await opnsense_tools.interface_statistics()


async def _run_opnsense_arp_table(_: BaseModel) -> dict:
    return await opnsense_tools.arp_table()


async def _run_opnsense_kea_leases(_: BaseModel) -> dict:
    return await opnsense_tools.kea_leases()


async def _run_opnsense_gateway_status(_: BaseModel) -> dict:
    return await opnsense_tools.gateway_status()


async def _run_opnsense_gateway_configuration(_: BaseModel) -> dict:
    return await opnsense_tools.gateway_configuration()


async def _run_opnsense_gateway_failover(_: BaseModel) -> dict:
    return await opnsense_tools.gateway_transition("failover")


async def _run_opnsense_gateway_restore(_: BaseModel) -> dict:
    return await opnsense_tools.gateway_transition("restore")


async def _run_opnsense_egress_switch(payload: EgressSwitchInput) -> dict:
    return await opnsense_tools.egress_switch(payload.profile)


async def _run_opnsense_wol_wake(payload: WolWakeInput) -> dict:
    return await opnsense_tools.wol_wake(payload.target_id)


async def _run_frigate_version(_: BaseModel) -> dict:
    return await frigate_tools.version()


async def _run_frigate_stats(_: BaseModel) -> dict:
    return await frigate_tools.stats()


async def _run_frigate_config_summary(_: BaseModel) -> dict:
    return await frigate_tools.config_summary()


async def _run_frigate_cameras(_: BaseModel) -> dict:
    return await frigate_tools.cameras_list()


async def _run_frigate_events(payload: FrigateRecentInput) -> dict:
    return await frigate_tools.events_recent(limit=payload.limit)


async def _run_frigate_review(payload: FrigateRecentInput) -> dict:
    return await frigate_tools.review_recent(limit=payload.limit)


async def _run_frigate_sub_labels(_: BaseModel) -> dict:
    return await frigate_tools.sub_labels()


async def _run_adguard_status(_: BaseModel) -> dict:
    return await adguard_tools.status()


async def _run_adguard_stats(_: BaseModel) -> dict:
    return await adguard_tools.stats()


async def _run_adguard_filtering(_: BaseModel) -> dict:
    return await adguard_tools.filtering_status()


async def _run_adguard_protection_pause(payload: AdguardPauseInput) -> dict:
    return await adguard_tools.protection_pause(payload.duration_minutes)


async def _run_adguard_protection_resume(_: BaseModel) -> dict:
    return await adguard_tools.protection_resume()


async def _run_proxmox_lxc_start(payload: ProxmoxLxcPowerInput) -> dict:
    return await proxmox_tools.lxc_start(payload.vmid)


async def _run_proxmox_lxc_shutdown(payload: ProxmoxLxcPowerInput) -> dict:
    return await proxmox_tools.lxc_shutdown(payload.vmid)


async def _run_nextcloud_status(_: BaseModel) -> dict:
    return await nextcloud_tools.status()


async def _run_nextcloud_capabilities(_: BaseModel) -> dict:
    return await nextcloud_tools.capabilities()


async def _run_nextcloud_serverinfo(_: BaseModel) -> dict:
    return await nextcloud_tools.serverinfo()


async def _run_nutups_devices(_: BaseModel) -> dict:
    return await nutups_tools.devices_list()


async def _run_nutups_status(_: BaseModel) -> dict:
    return await nutups_tools.status()


async def _run_mikrotik_resource(_: BaseModel) -> dict:
    return await mikrotik_tools.system_resource()


async def _run_mikrotik_health(_: BaseModel) -> dict:
    return await mikrotik_tools.system_health()


async def _run_mikrotik_interfaces(_: BaseModel) -> dict:
    return await mikrotik_tools.interfaces_list()


async def _run_mikrotik_lte(_: BaseModel) -> dict:
    return await mikrotik_tools.lte_status()


async def _run_asterisk_core_status(_: BaseModel) -> dict:
    return await asterisk_tools.core_status()


async def _run_asterisk_channels(_: BaseModel) -> dict:
    return await asterisk_tools.channels_list()


async def _run_asterisk_peers(_: BaseModel) -> dict:
    return await asterisk_tools.peers_list()


async def _run_uptimekuma_monitors(_: BaseModel) -> dict:
    return await uptimekuma_tools.monitors_status()


async def _run_uptimekuma_heartbeat(payload: StatusPageSlugInput) -> dict:
    return await uptimekuma_tools.statuspage_heartbeat(payload.slug)


async def _run_emqx_nodes(_: BaseModel) -> dict:
    return await emqx_tools.nodes_list()


async def _run_emqx_stats(_: BaseModel) -> dict:
    return await emqx_tools.stats()


async def _run_cloudflare_tunnels_status(_: BaseModel) -> dict:
    return await cloudflaretunnel_tools.all_tunnels_status()


async def _run_cloudflare_connectors(_: BaseModel) -> dict:
    return await cloudflaretunnel_tools.connectors_list()


async def _run_vps_reachability_status(_: BaseModel) -> dict:
    return await vps_tools.reachability_status()


async def _run_vps_glances_status(_: BaseModel) -> dict:
    return await vps_tools.glances_status()


async def _run_vps_wireguard_status(_: BaseModel) -> dict:
    return await vps_tools.wireguard_status()


async def _run_vps_deploy_status(_: BaseModel) -> dict:
    return await vps_tools.deploy_status()


async def _run_vps_summary(_: BaseModel) -> dict:
    return await vps_tools.summary()


async def _run_zerotier_status(_: BaseModel) -> dict:
    return await zerotier_tools.status()


async def _run_zerotier_networks(_: BaseModel) -> dict:
    return await zerotier_tools.networks_list()


async def _run_zerotier_members(_: BaseModel) -> dict:
    return await zerotier_tools.members_list()


async def _run_zerotier_summary(_: BaseModel) -> dict:
    return await zerotier_tools.summary()


async def _run_fritzbox_primary_device(_: BaseModel) -> dict:
    return await fritzbox_tools.device_info("fritzbox_primary")


async def _run_fritzbox_primary_wan(_: BaseModel) -> dict:
    return await fritzbox_tools.wan_status("fritzbox_primary")


async def _run_fritzbox_primary_wifi(_: BaseModel) -> dict:
    return await fritzbox_tools.wifi_summary("fritzbox_primary")


async def _run_fritzbox_secondary_device(_: BaseModel) -> dict:
    return await fritzbox_tools.device_info("fritzbox_secondary")


async def _run_fritzbox_secondary_wan(_: BaseModel) -> dict:
    return await fritzbox_tools.wan_status("fritzbox_secondary")


async def _run_fritzbox_secondary_wifi(_: BaseModel) -> dict:
    return await fritzbox_tools.wifi_summary("fritzbox_secondary")


async def _run_glances_temperatures(_: BaseModel) -> dict:
    return await glances_tools.temperatures()


async def _run_fritzbox_primary_temperature(_: BaseModel) -> dict:
    return await fritzbox_tools.system_temperature("fritzbox_primary")


async def _run_fritzbox_secondary_temperature(_: BaseModel) -> dict:
    return await fritzbox_tools.system_temperature("fritzbox_secondary")


async def _run_proxmox_summary(_: BaseModel) -> dict:
    return await summaries.proxmox_summary()


async def _run_opnsense_summary(_: BaseModel) -> dict:
    return await summaries.opnsense_summary()


async def _run_homeassistant_summary(_: BaseModel) -> dict:
    return await summaries.homeassistant_summary()


async def _run_frigate_summary(_: BaseModel) -> dict:
    return await summaries.frigate_summary()


async def _run_adguard_summary(_: BaseModel) -> dict:
    return await summaries.adguard_summary()


async def _run_nextcloud_summary(_: BaseModel) -> dict:
    return await summaries.nextcloud_summary()


async def _run_nutups_summary(_: BaseModel) -> dict:
    return await summaries.nutups_summary()


async def _run_mikrotik_summary(_: BaseModel) -> dict:
    return await summaries.mikrotik_summary()


async def _run_uptimekuma_summary(_: BaseModel) -> dict:
    return await summaries.uptimekuma_summary()


async def _run_emqx_summary(_: BaseModel) -> dict:
    return await summaries.emqx_summary()


async def _run_cloudflare_summary(_: BaseModel) -> dict:
    return await summaries.cloudflare_summary()


async def _run_fritzbox_primary_summary(_: BaseModel) -> dict:
    return await summaries.fritzbox_primary_summary()


async def _run_fritzbox_secondary_summary(_: BaseModel) -> dict:
    return await summaries.fritzbox_secondary_summary()


async def _run_notifications_status(_: BaseModel) -> dict:
    return await notification_status_summary()


async def _run_notifications_outbox(payload: NotificationListInput) -> dict:
    return await notification_list_recent(payload.limit)


def _api_ready_runner(instance_id: str):
    async def run(_: BaseModel) -> dict:
        return await api_ready_tools.health_status(instance_id)

    return run


def _api_ready_tools() -> list[ToolDefinition]:
    tools: list[ToolDefinition] = []
    static_provider_ids = {tool.provider_id for tool in _TOOLS}
    for instance in list_api_provider_instances():
        risk: ToolRisk = "low"
        if instance.id in static_provider_ids:
            continue
        if instance.driver == "cloudflare_tunnel_v1":
            suffix = "tunnel.status"
            name = f"{instance.name or instance.id} Tunnel"
            description = "Read normalized tunnel state from Cloudflare's official API."
            runner = _api_ready_runner(instance.id)
            output_model = None
        else:
            suffix = "health.status"
            name = f"{instance.name or instance.id} Health"
            description = "Read normalized health from the declared json_health_v1 /health endpoint."
            runner = _api_ready_runner(instance.id)
            output_model = None
            risk = "low"
        if instance.driver == "cloudflare_tunnel_v1":
            risk = "low"
        tools.append(
            _simple_tool(
                instance.id,
                f"{instance.id}.{suffix}",
                name,
                description,
                runner,
                timeout_seconds=instance.timeout_seconds,
                output_model=output_model,
                risk=risk,
            )
        )
    return tools


def _simple_tool(provider_id: str, tool_id: str, name: str, description: str,
                 runner: Callable[[Any], Awaitable[dict]],
                 input_model: type[BaseModel] = EmptyInput, timeout_seconds: float = 10.0,
                 output_model: type[BaseModel] | None = None,
                 risk: ToolRisk = "low") -> ToolDefinition:
    return ToolDefinition(
        id=tool_id,
        name=name,
        description=description,
        provider_id=provider_id,
        category=provider_id,
        mode="read",
        risk=risk,
        timeout_seconds=timeout_seconds,
        input_model=input_model,
        output_model=output_model,
        runner=runner,
    )


def _proxmox_tool(
    tool_id: str,
    name: str,
    description: str,
    runner: Callable[[Any], Awaitable[dict]],
    input_model: type[BaseModel] = EmptyInput,
    output_model: type[BaseModel] | None = None,
    timeout_seconds: float = 10.0,
) -> ToolDefinition:
    return ToolDefinition(
        id=tool_id,
        name=name,
        description=description,
        provider_id="proxmox",
        category="proxmox",
        mode="read",
        risk="low",
        timeout_seconds=timeout_seconds,
        input_model=input_model,
        output_model=output_model,
        runner=runner,
    )


def _opnsense_tool(
    tool_id: str, name: str, description: str, runner: Callable[[Any], Awaitable[dict]]
) -> ToolDefinition:
    return ToolDefinition(
        id=tool_id,
        name=name,
        description=description,
        provider_id="opnsense",
        category="opnsense",
        mode="read",
        risk="low",
        timeout_seconds=10.0,
        input_model=EmptyInput,
        runner=runner,
    )


def _homeassistant_tool(
    tool_id: str,
    name: str,
    description: str,
    runner: Callable[[Any], Awaitable[dict]],
    input_model: type[BaseModel] = EmptyInput,
) -> ToolDefinition:
    return ToolDefinition(
        id=tool_id,
        name=name,
        description=description,
        provider_id="homeassistant",
        category="homeassistant",
        mode="read",
        risk="low",
        timeout_seconds=12.0,
        input_model=input_model,
        runner=runner,
    )


def _frigate_tool(
    tool_id: str,
    name: str,
    description: str,
    runner: Callable[[Any], Awaitable[dict]],
    input_model: type[BaseModel] = EmptyInput,
) -> ToolDefinition:
    return ToolDefinition(
        id=tool_id,
        name=name,
        description=description,
        provider_id="frigate",
        category="frigate",
        mode="read",
        risk="low",
        timeout_seconds=12.0,
        input_model=input_model,
        runner=runner,
    )


_TOOLS: list[ToolDefinition] = [
    _simple_tool("console", "notifications.status", "Notification Status", "Read notification worker state, queue counts and last successful delivery.", _run_notifications_status),
    _simple_tool("console", "notifications.outbox.list", "Notification Outbox", "Read recent redacted notification delivery records without message text or secrets.", _run_notifications_outbox, NotificationListInput),
    _proxmox_tool("proxmox.version", "Proxmox Version", "Read the Proxmox VE API version.", _run_proxmox_version),
    _proxmox_tool("proxmox.cluster.status", "Proxmox Cluster Status", "Read cluster quorum and node membership.", _run_proxmox_cluster_status),
    _proxmox_tool("proxmox.nodes.list", "Proxmox Nodes", "List cluster nodes with load and memory.", _run_proxmox_nodes),
    _proxmox_tool(
        "proxmox.topology",
        "Proxmox Topology",
        "Read cluster membership, nodes and guest placement as one normalized snapshot.",
        _run_proxmox_topology,
        output_model=ProxmoxTopologySnapshot,
        timeout_seconds=15.0,
    ),
    _proxmox_tool("proxmox.resources.list", "Proxmox Resources", "List guests and storage in one view.", _run_proxmox_resources),
    _proxmox_tool("proxmox.guests.list", "Proxmox Guests", "List all guests (VMs and containers).", _run_proxmox_guests, GuestFilterInput),
    _proxmox_tool("proxmox.vms.list", "Proxmox VMs", "List QEMU virtual machines.", _run_proxmox_vms, GuestFilterInput),
    _proxmox_tool("proxmox.lxc.list", "Proxmox LXC", "List LXC containers.", _run_proxmox_lxc, GuestFilterInput),
    _proxmox_tool("proxmox.storage.list", "Proxmox Storage", "List storage pools with usage.", _run_proxmox_storage),
    _proxmox_tool("proxmox.tasks.failed", "Proxmox Failed Tasks", "List recent failed cluster tasks.", _run_proxmox_tasks_failed),
    _simple_tool("proxmox", "proxmox.summary", "Proxmox Summary", "Preferred compact inventory for guest counts: precomputed LXC/QEMU totals and running guest list, plus storage and failed tasks.", _run_proxmox_summary, timeout_seconds=18.0),
    _proxmox_tool("proxmox.node.status", "Proxmox Node Status", "Read load, memory, swap and rootfs usage for one node.", _run_proxmox_node_status, NodeStatusInput),
    _proxmox_tool("proxmox.disks.temperatures", "Proxmox Disk Temperatures", "Read SMART temperature, health and wearout for physical disks on every online node.", _run_proxmox_disks_temperatures, timeout_seconds=20.0),
    ToolDefinition(
        id="proxmox.lxc.start",
        name="Proxmox LXC Start",
        description=(
            "Start one LXC container selected by vmid and validated against live cluster "
            "inventory. Idempotent, approval-bound and verified by post-action read-back."
        ),
        provider_id="proxmox",
        category="proxmox",
        mode="write",
        risk="medium",
        requires_confirmation=True,
        timeout_seconds=30.0,
        input_model=ProxmoxLxcPowerInput,
        output_model=ProxmoxLxcPowerOutput,
        runner=_run_proxmox_lxc_start,
    ),
    ToolDefinition(
        id="proxmox.lxc.shutdown",
        name="Proxmox LXC Graceful Shutdown",
        description=(
            "Gracefully shut down one LXC container selected by vmid and validated against "
            "live cluster inventory. Never performs a forced stop; approval-bound and "
            "verified by post-action read-back."
        ),
        provider_id="proxmox",
        category="proxmox",
        mode="write",
        risk="high",
        requires_confirmation=True,
        timeout_seconds=30.0,
        input_model=ProxmoxLxcPowerInput,
        output_model=ProxmoxLxcPowerOutput,
        runner=_run_proxmox_lxc_shutdown,
    ),
    _simple_tool("pbs", "pbs.version", "PBS Version", "Read Proxmox Backup Server version.", _run_pbs_version),
    _simple_tool("pbs", "pbs.datastores.status", "PBS Datastores", "Read PBS datastore capacity and usage.", _run_pbs_datastores_status, timeout_seconds=15.0),
    _simple_tool("pbs", "pbs.tasks.recent", "PBS Recent Tasks", "Read recent PBS task outcomes.", _run_pbs_tasks_recent, timeout_seconds=15.0),
    _simple_tool("pbs", "pbs.verify.jobs.health", "PBS Verify Jobs", "Read PBS verify job health.", _run_pbs_verify_jobs_health, timeout_seconds=15.0),
    _simple_tool("pbs", "pbs.backup.jobs.health", "PBS Backup Groups", "Summarize visible PBS backup groups and latest snapshots.", _run_pbs_backup_jobs_health, timeout_seconds=25.0),
    _simple_tool("pbs", "pbs.summary", "PBS Summary", "Compact agent summary for datastores, recent tasks, verify jobs and backup groups.", _run_pbs_summary, timeout_seconds=30.0),
    ToolDefinition(
        id="proxmox.backups.list",
        name="Proxmox Backups",
        description="Summarize vzdump backups per guest: latest backup age, size, count.",
        provider_id="proxmox",
        category="proxmox",
        mode="read",
        risk="low",
        timeout_seconds=25.0,
        input_model=EmptyInput,
        runner=_run_proxmox_backups,
    ),
    _opnsense_tool("opnsense.firmware.status", "OPNsense Firmware", "Read firmware/update status.", _run_opnsense_firmware_status),
    _opnsense_tool("opnsense.system.status", "OPNsense System Status", "Read dashboard system status.", _run_opnsense_system_status),
    _opnsense_tool("opnsense.system.information", "OPNsense System Information", "Read hostname and platform versions.", _run_opnsense_system_information),
    _opnsense_tool("opnsense.system.resources", "OPNsense System Resources", "Read memory and system resource counters.", _run_opnsense_system_resources),
    _opnsense_tool("opnsense.system.temperature", "OPNsense System Temperature", "Read CPU/platform temperatures from the OPNsense diagnostics API.", _run_opnsense_system_temperature),
    _opnsense_tool("opnsense.interfaces.names", "OPNsense Interfaces", "Read interface name mappings.", _run_opnsense_interface_names),
    _opnsense_tool("opnsense.interfaces.statistics", "OPNsense Interface Statistics", "Read interface packet and byte counters.", _run_opnsense_interface_statistics),
    _opnsense_tool("opnsense.devices.arp", "OPNsense ARP Devices", "Read devices currently known in the OPNsense ARP table.", _run_opnsense_arp_table),
    _opnsense_tool("opnsense.kea.leases", "OPNsense Kea Leases", "Read current Kea DHCP leases from OPNsense.", _run_opnsense_kea_leases),
    _opnsense_tool("opnsense.gateways.status", "OPNsense Gateways", "Read gateway monitoring status.", _run_opnsense_gateway_status),
    _opnsense_tool(
        "opnsense.gateways.configuration",
        "OPNsense Gateway Configuration",
        "List normalized gateway ids, names and enabled state using the scoped network-action identity.",
        _run_opnsense_gateway_configuration,
    ),
    ToolDefinition(
        id="opnsense.gateway.failover",
        name="OPNsense Gateway Failover",
        description=(
            "Force the preconfigured backup gateway and verify the active "
            "default route. No caller-selected gateway is accepted."
        ),
        provider_id="opnsense",
        category="opnsense",
        mode="write",
        risk="high",
        requires_confirmation=True,
        timeout_seconds=20.0,
        input_model=EmptyInput,
        output_model=GatewayTransitionOutput,
        runner=_run_opnsense_gateway_failover,
    ),
    ToolDefinition(
        id="opnsense.gateway.restore",
        name="OPNsense Gateway Restore",
        description=(
            "Restore the preconfigured primary gateway and verify the active "
            "default route. No caller-selected gateway is accepted."
        ),
        provider_id="opnsense",
        category="opnsense",
        mode="write",
        risk="high",
        requires_confirmation=True,
        timeout_seconds=20.0,
        input_model=EmptyInput,
        output_model=GatewayTransitionOutput,
        runner=_run_opnsense_gateway_restore,
    ),
    ToolDefinition(
        id="opnsense.egress.switch",
        name="OPNsense Egress Switch",
        description=(
            "Switch the default egress between the declared direct primary_isp "
            "path and the declared German WireGuard gateway."
        ),
        provider_id="opnsense",
        category="opnsense",
        mode="write",
        risk="high",
        requires_confirmation=True,
        timeout_seconds=25.0,
        input_model=EgressSwitchInput,
        output_model=EgressSwitchOutput,
        runner=_run_opnsense_egress_switch,
    ),
    ToolDefinition(
        id="opnsense.wol.wake",
        name="OPNsense Wake-on-LAN",
        description=(
            "Send a Wake-on-LAN packet to one target already declared in both "
            "Homelab Console configuration and the OPNsense os-wol plugin."
        ),
        provider_id="opnsense",
        category="opnsense",
        mode="write",
        risk="medium",
        requires_confirmation=True,
        timeout_seconds=10.0,
        input_model=WolWakeInput,
        output_model=WolWakeOutput,
        runner=_run_opnsense_wol_wake,
    ),
    _opnsense_tool("opnsense.wireguard.status", "OPNsense WireGuard", "Read WireGuard tunnel and peer handshake state (the VPS-to-homelab link).", _run_opnsense_wireguard),
    _opnsense_tool("opnsense.services.list", "OPNsense Services", "List firewall services with running state.", _run_opnsense_services),
    _simple_tool("opnsense", "opnsense.summary", "OPNsense Summary", "Compact agent summary for gateways, WireGuard, services and firmware.", _run_opnsense_summary, timeout_seconds=18.0),
    _homeassistant_tool("homeassistant.api.status", "Home Assistant API", "Read API availability.", _run_homeassistant_api_status),
    _homeassistant_tool("homeassistant.config", "Home Assistant Config", "Read core configuration metadata.", _run_homeassistant_config),
    _homeassistant_tool("homeassistant.states.summary", "Home Assistant State Summary", "Read entity counts by domain and unavailable/unknown entities.", _run_homeassistant_states_summary),
    _homeassistant_tool("homeassistant.states.list", "Home Assistant Entities", "List entities, optionally filtered by domain or text query.", _run_homeassistant_states_list, HomeAssistantStatesInput),
    _homeassistant_tool("homeassistant.services.list", "Home Assistant Services", "List service domains and service names.", _run_homeassistant_services),
    _homeassistant_tool("homeassistant.error_log.tail", "Home Assistant Error Log", "Read the tail of the Home Assistant error log.", _run_homeassistant_error_log, HomeAssistantLogInput),
    _homeassistant_tool("homeassistant.logbook.recent", "Home Assistant Logbook", "Read recent logbook events.", _run_homeassistant_logbook, HomeAssistantLogbookInput),
    _simple_tool("homeassistant", "homeassistant.summary", "Home Assistant Summary", "Compact agent summary for entities and recent errors.", _run_homeassistant_summary, timeout_seconds=18.0),
    _frigate_tool("frigate.version", "Frigate Version", "Read the Frigate API version.", _run_frigate_version),
    _frigate_tool("frigate.stats", "Frigate Stats", "Read Frigate runtime and camera statistics.", _run_frigate_stats),
    _frigate_tool("frigate.config.summary", "Frigate Config Summary", "Read a redacted Frigate configuration summary.", _run_frigate_config_summary),
    _frigate_tool("frigate.cameras.list", "Frigate Cameras", "List configured Frigate cameras.", _run_frigate_cameras),
    _frigate_tool("frigate.events.recent", "Frigate Events", "Read recent Frigate events.", _run_frigate_events, FrigateRecentInput),
    _frigate_tool("frigate.review.recent", "Frigate Review", "Read recent Frigate review items.", _run_frigate_review, FrigateRecentInput),
    _frigate_tool("frigate.sub_labels", "Frigate Sub Labels", "List Frigate sub-labels.", _run_frigate_sub_labels),
    _simple_tool("frigate", "frigate.summary", "Frigate Summary", "Compact agent summary for cameras, config and detection health.", _run_frigate_summary, timeout_seconds=18.0),
    _simple_tool("adguard", "adguard.status", "AdGuard Status", "Read AdGuard Home version and protection state.", _run_adguard_status),
    _simple_tool("adguard", "adguard.stats", "AdGuard Statistics", "Read DNS query and blocking statistics.", _run_adguard_stats),
    _simple_tool("adguard", "adguard.filtering.status", "AdGuard Filtering", "Read filtering state and blocklist summary.", _run_adguard_filtering),
    _simple_tool("adguard", "adguard.summary", "AdGuard Summary", "Compact agent summary for DNS protection and query statistics.", _run_adguard_summary),
    ToolDefinition(
        id="adguard.protection.pause",
        name="AdGuard Pause Protection",
        description=(
            "Pause AdGuard DNS filtering for a bounded duration (5/15/30/60 minutes). "
            "AdGuard re-enables protection by itself at expiry; the result includes the "
            "observed post_state. Write tool under ADR 0004: requires a single-use, "
            "input-bound operator approval."
        ),
        provider_id="adguard",
        category="adguard",
        mode="write",
        risk="medium",
        requires_confirmation=True,
        timeout_seconds=10.0,
        input_model=AdguardPauseInput,
        output_model=AdguardPauseOutput,
        runner=_run_adguard_protection_pause,
    ),
    ToolDefinition(
        id="adguard.protection.resume",
        name="AdGuard Resume Protection",
        description=(
            "Re-enable AdGuard DNS filtering immediately (rollback for adguard.protection.pause). "
            "The result includes the observed post_state. Write tool under ADR 0004: requires a "
            "single-use operator approval."
        ),
        provider_id="adguard",
        category="adguard",
        mode="write",
        risk="low",
        requires_confirmation=True,
        timeout_seconds=10.0,
        input_model=EmptyInput,
        output_model=AdguardResumeOutput,
        runner=_run_adguard_protection_resume,
    ),
    _simple_tool("nextcloud", "nextcloud.status", "Nextcloud Status", "Read version and maintenance state.", _run_nextcloud_status),
    _simple_tool("nextcloud", "nextcloud.capabilities", "Nextcloud Capabilities", "Read version and enabled capability groups.", _run_nextcloud_capabilities),
    _simple_tool("nextcloud", "nextcloud.serverinfo", "Nextcloud Server Info", "Read system, storage and active-user metrics.", _run_nextcloud_serverinfo),
    _simple_tool("nextcloud", "nextcloud.summary", "Nextcloud Summary", "Compact agent summary for version, maintenance and usage.", _run_nextcloud_summary),
    _simple_tool("nutups", "nutups.devices.list", "NUT UPS Devices", "List UPS devices exposed by the configured NUT upsd endpoint.", _run_nutups_devices, timeout_seconds=8.0),
    _simple_tool("nutups", "nutups.status", "NUT UPS Status", "Read normalized UPS battery, load and line-power status from NUT.", _run_nutups_status, timeout_seconds=8.0),
    _simple_tool("nutups", "nutups.summary", "NUT UPS Summary", "Compact agent summary for UPS health, battery and runtime.", _run_nutups_summary, timeout_seconds=10.0),
    _simple_tool("mikrotik", "mikrotik.system.resource", "MikroTik System Resource", "Read RouterOS version, CPU, memory and disk usage.", _run_mikrotik_resource),
    _simple_tool("mikrotik", "mikrotik.system.health", "MikroTik System Health", "Read RouterOS hardware health sensors such as board temperature and voltage.", _run_mikrotik_health),
    _simple_tool("mikrotik", "mikrotik.interfaces.list", "MikroTik Interfaces", "List interfaces with state and traffic counters.", _run_mikrotik_interfaces),
    _simple_tool("mikrotik", "mikrotik.lte.status", "MikroTik LTE Status", "Read LTE interface state.", _run_mikrotik_lte),
    _simple_tool("mikrotik", "mikrotik.summary", "MikroTik Summary", "Compact agent summary for resources and interface state.", _run_mikrotik_summary),
    _simple_tool("asterisk", "asterisk.core.status", "Asterisk Core Status", "Read version, uptime and current call count.", _run_asterisk_core_status, timeout_seconds=12.0),
    _simple_tool("asterisk", "asterisk.channels.list", "Asterisk Channels", "List active channels.", _run_asterisk_channels, timeout_seconds=12.0),
    _simple_tool("asterisk", "asterisk.peers.list", "Asterisk Peers", "List SIP/PJSIP endpoints with registration state.", _run_asterisk_peers, timeout_seconds=12.0),
    _simple_tool("uptimekuma", "uptimekuma.monitors.status", "Uptime Kuma Monitors", "Read monitor up/down state from the metrics endpoint.", _run_uptimekuma_monitors),
    _simple_tool("uptimekuma", "uptimekuma.statuspage.heartbeat", "Uptime Kuma Status Page", "Read heartbeat and uptime for a public status page.", _run_uptimekuma_heartbeat, StatusPageSlugInput),
    _simple_tool("uptimekuma", "uptimekuma.summary", "Uptime Kuma Summary", "Compact agent summary for monitor status.", _run_uptimekuma_summary),
    _simple_tool("emqx", "emqx.nodes.list", "EMQX Nodes", "List broker nodes with version, uptime and connections.", _run_emqx_nodes),
    _simple_tool("emqx", "emqx.stats", "EMQX Statistics", "Read connection, session, subscription and topic counts.", _run_emqx_stats),
    _simple_tool("emqx", "emqx.summary", "EMQX Summary", "Compact agent summary for broker nodes and messaging counters.", _run_emqx_summary),
    _simple_tool("cloudflaretunnel", "cloudflare.tunnels.status", "Cloudflare Tunnels", "Read normalized state for declared tunnels from the official Cloudflare API.", _run_cloudflare_tunnels_status, timeout_seconds=15.0),
    _simple_tool("cloudflaretunnel", "cloudflare.connectors.list", "Cloudflare Connectors", "Read redacted cloudflared connector and connection counters for declared tunnels.", _run_cloudflare_connectors, timeout_seconds=15.0),
    _simple_tool("cloudflaretunnel", "cloudflare.summary", "Cloudflare Summary", "Compact summary of tunnel and cloudflared connector health.", _run_cloudflare_summary, timeout_seconds=20.0),
    _simple_tool("vps", "vps.reachability.status", "VPS Reachability", "Check configured VPS inventory host ports.", _run_vps_reachability_status, timeout_seconds=8.0),
    _simple_tool("vps", "vps.glances.status", "VPS Glances Status", "Read VPS system, CPU, memory and disk data from the configured Glances API.", _run_vps_glances_status, timeout_seconds=10.0),
    _simple_tool("vps", "vps.wireguard.status", "VPS WireGuard Status", "Read VPS WireGuard interface presence from Glances and route-target reachability from inventory.", _run_vps_wireguard_status, timeout_seconds=15.0),
    _simple_tool("vps", "vps.deploy.status", "VPS Deploy Status", "Check configured public deploy HTTP targets owned by the VPS.", _run_vps_deploy_status, timeout_seconds=15.0),
    _simple_tool("vps", "vps.summary", "VPS Summary", "Compact agent summary for VPS resources, WireGuard observation and deploy targets.", _run_vps_summary, timeout_seconds=20.0),
    _simple_tool("zerotier", "zerotier.status", "ZeroTier Status", "Verify access to the fixed ZeroTier Central Legacy API without exposing account data.", _run_zerotier_status),
    _simple_tool("zerotier", "zerotier.networks.list", "ZeroTier Networks", "List normalized metadata for only the ZeroTier networks declared in inventory.", _run_zerotier_networks, timeout_seconds=15.0),
    _simple_tool("zerotier", "zerotier.members.list", "ZeroTier Members", "List normalized member authorization, freshness and assigned ZeroTier IPs for declared networks.", _run_zerotier_members, timeout_seconds=20.0),
    _simple_tool("zerotier", "zerotier.summary", "ZeroTier Summary", "Compact summary of declared networks and authorized member freshness.", _run_zerotier_summary, timeout_seconds=25.0),
    _simple_tool("fritzbox_primary", "fritzbox.primary.device", "FritzBox Primary Device", "Read model, firmware, serial and uptime via TR-064.", _run_fritzbox_primary_device, timeout_seconds=12.0),
    _simple_tool("fritzbox_primary", "fritzbox.primary.wan", "FritzBox Primary WAN", "Read WAN link status, rates and byte counters via TR-064.", _run_fritzbox_primary_wan, timeout_seconds=12.0),
    _simple_tool("fritzbox_primary", "fritzbox.primary.wifi", "FritzBox Primary Wi-Fi", "Read WLAN radio state and SSIDs via TR-064.", _run_fritzbox_primary_wifi, timeout_seconds=12.0),
    _simple_tool("fritzbox_primary", "fritzbox.primary.summary", "FritzBox Primary Summary", "Compact agent summary for model, WAN and Wi-Fi state.", _run_fritzbox_primary_summary, timeout_seconds=15.0),
    _simple_tool("fritzbox_secondary", "fritzbox.secondary.device", "FritzBox Secondary Device", "Read model, firmware, serial and uptime via TR-064.", _run_fritzbox_secondary_device, timeout_seconds=12.0),
    _simple_tool("fritzbox_secondary", "fritzbox.secondary.wan", "FritzBox Secondary WAN", "Read WAN link status, rates and byte counters via TR-064.", _run_fritzbox_secondary_wan, timeout_seconds=12.0),
    _simple_tool("fritzbox_secondary", "fritzbox.secondary.wifi", "FritzBox Secondary Wi-Fi", "Read WLAN radio state and SSIDs via TR-064.", _run_fritzbox_secondary_wifi, timeout_seconds=12.0),
    _simple_tool("fritzbox_secondary", "fritzbox.secondary.summary", "FritzBox Secondary Summary", "Compact agent summary for model, WAN and Wi-Fi state.", _run_fritzbox_secondary_summary, timeout_seconds=15.0),
    _simple_tool("glances", "hosts.temperatures", "Host Temperatures", "Read CPU/SoC temperatures from the Glances API of configured hosts (Proxmox nodes, Raspberry Pis).", _run_glances_temperatures, timeout_seconds=20.0),
    _simple_tool("fritzbox_primary", "fritzbox.primary.temperature", "FritzBox Primary Temperature", "Read CPU temperature from the web UI ecoStat page (undocumented AVM endpoint).", _run_fritzbox_primary_temperature, timeout_seconds=12.0),
    _simple_tool("fritzbox_secondary", "fritzbox.secondary.temperature", "FritzBox Secondary Temperature", "Read CPU temperature from the web UI ecoStat page; reports supported=false when the hardware has no sensor.", _run_fritzbox_secondary_temperature, timeout_seconds=12.0),
    ToolDefinition(
        id="lab.overview",
        name="Lab Overview",
        description="One-call synthesis: provider health, guests, gateways, WireGuard, entities, cameras, DNS stats.",
        provider_id="console",
        category="overview",
        mode="read",
        risk="low",
        timeout_seconds=30.0,
        input_model=EmptyInput,
        runner=_run_lab_overview,
    ),
    ToolDefinition(
        id="lab.summary",
        name="Lab Summary",
        description="Agent-friendly synthesis of all main provider summaries with findings and next actions.",
        provider_id="console",
        category="overview",
        mode="read",
        risk="low",
        timeout_seconds=45.0,
        input_model=EmptyInput,
        runner=_run_lab_summary,
    ),
    ToolDefinition(
        id="lab.network.summary",
        name="Lab Network Summary",
        description="Agent-friendly network synthesis across OPNsense, MikroTik, AdGuard and FritzBox.",
        provider_id="console",
        category="overview",
        mode="read",
        risk="low",
        timeout_seconds=35.0,
        input_model=EmptyInput,
        runner=_run_lab_network_summary,
    ),
    ToolDefinition(
        id="lab.security.summary",
        name="Lab Security Summary",
        description="Agent-friendly security posture summary across firewall, DNS protection, Nextcloud and monitoring.",
        provider_id="console",
        category="overview",
        mode="read",
        risk="low",
        timeout_seconds=35.0,
        input_model=EmptyInput,
        runner=_run_lab_security_summary,
    ),
    ToolDefinition(
        id="lab.storage.summary",
        name="Lab Storage Summary",
        description="Agent-friendly storage summary across Proxmox and Nextcloud.",
        provider_id="console",
        category="overview",
        mode="read",
        risk="low",
        timeout_seconds=35.0,
        input_model=EmptyInput,
        runner=_run_lab_storage_summary,
    ),
    ToolDefinition(
        id="lab.automation.summary",
        name="Lab Automation Summary",
        description="Agent-friendly automation summary across Home Assistant, Frigate and EMQX.",
        provider_id="console",
        category="overview",
        mode="read",
        risk="low",
        timeout_seconds=35.0,
        input_model=EmptyInput,
        runner=_run_lab_automation_summary,
    ),
    ToolDefinition(
        id="lab.alerts.recent",
        name="Lab Recent Alerts",
        description="Recent agent-friendly findings aggregated across all main provider summaries.",
        provider_id="console",
        category="overview",
        mode="read",
        risk="low",
        timeout_seconds=45.0,
        input_model=EmptyInput,
        runner=_run_lab_alerts_recent,
    ),
    ToolDefinition(
        id="network.clients.list",
        name="Network Clients",
        description="List configured network clients from inventory without scanning the network.",
        provider_id="network",
        category="network",
        mode="read",
        risk="low",
        timeout_seconds=5.0,
        input_model=NetworkClientsInput,
        runner=_run_network_clients,
    ),
    ToolDefinition(
        id="network.host.check",
        name="Host Reachability",
        description="TCP reachability check against a configured inventory host.",
        provider_id="network",
        category="network",
        mode="read",
        risk="low",
        timeout_seconds=15.0,
        input_model=HostCheckInput,
        runner=_run_host_check,
    ),
    ToolDefinition(
        id="network.tls.certificates",
        name="TLS Certificates",
        description="Read certificate validity windows for declared TLS targets from inventory.",
        provider_id="network",
        category="network",
        mode="read",
        risk="low",
        timeout_seconds=30.0,
        input_model=EmptyInput,
        runner=_run_tls_certificates,
    ),
    ToolDefinition(
        id="network.egress.status",
        name="Public Egress Status",
        description=(
            "Read the current public IP and country through a fixed "
            "ipwho.is metadata endpoint."
        ),
        provider_id="network",
        category="network",
        mode="read",
        risk="low",
        timeout_seconds=10.0,
        input_model=EmptyInput,
        runner=_run_egress_status,
    ),
    ToolDefinition(
        id="network.dns.resolve",
        name="DNS Resolve",
        description="Resolve a configured DNS target through configured resolvers and the system resolver.",
        provider_id="network",
        category="network",
        mode="read",
        risk="low",
        timeout_seconds=12.0,
        input_model=DnsResolveInput,
        runner=_run_dns_resolve,
    ),
    ToolDefinition(
        id="network.dns.path.check",
        name="DNS Path Check",
        description="Compare configured resolver answers for one configured DNS target.",
        provider_id="network",
        category="network",
        mode="read",
        risk="low",
        timeout_seconds=12.0,
        input_model=DnsTargetInput,
        runner=_run_dns_path_check,
    ),
    ToolDefinition(
        id="network.dns.adguard.health",
        name="AdGuard DNS Health",
        description="Read AdGuard DNS protection state and query counters.",
        provider_id="network",
        category="network",
        mode="read",
        risk="low",
        timeout_seconds=12.0,
        input_model=EmptyInput,
        runner=_run_dns_adguard_health,
    ),
    ToolDefinition(
        id="lab.dns.summary",
        name="Lab DNS Summary",
        description="Agent-friendly DNS and resolution summary across configured targets and resolvers.",
        provider_id="console",
        category="overview",
        mode="read",
        risk="low",
        timeout_seconds=25.0,
        input_model=EmptyInput,
        runner=_run_dns_summary,
    ),
]


def list_tools() -> list[ToolDefinition]:
    overrides = tool_overrides()
    tools = []
    for tool in [*_TOOLS, *_api_ready_tools()]:
        override = overrides.get(tool.id)
        if override and "enabled" in override:
            tool = tool.model_copy(update={"enabled": bool(override["enabled"])})
        if tool.mode == "write" and tool.id not in APPROVED_WRITE_TOOLS:
            tool = tool.model_copy(update={"enabled": False})
        tools.append(tool)
    return tools


def get_tool(tool_id: str) -> ToolDefinition | None:
    return next((tool for tool in list_tools() if tool.id == tool_id), None)
