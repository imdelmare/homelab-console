"""Definitions and evaluators for capability-level provider observations.

All input reaches this module only after the corresponding narrow read tool
has run through the shared execution core. Evaluators consume normalized tool
results, never raw provider payloads.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
import re
from typing import Any

from app.domain.observations import CapabilityObservation, ObservationScalar, ProviderStatusValue
from app.domain.actors import Actor
from app.services.inventory import list_topology_nodes, provider_config
from app.tools.execution import ExecutionResult, execute_tool

ObservationEvaluation = tuple[ProviderStatusValue, str, dict[str, ObservationScalar]]
ObservationEvaluator = Callable[[dict[str, Any]], ObservationEvaluation]
_SAFE_GATEWAY_OBSERVATION_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


async def collect_capability_observations(
    actor: Actor,
    *,
    provider_id: str | None = None,
    source: str = "rest",
) -> list[dict[str, Any]]:
    definitions = list_observation_definitions(provider_id)
    tool_ids = list(dict.fromkeys(definition.tool_id for definition in definitions))
    executions = await asyncio.gather(
        *(execute_tool(tool_id, {}, actor, source=source) for tool_id in tool_ids)
    )
    executions_by_tool = dict(zip(tool_ids, executions))
    observations = [
        evaluate_observation(
            definition, executions_by_tool[definition.tool_id]
        ).model_dump(mode="json")
        for definition in definitions
    ]
    for definition in definitions:
        if definition.id == "uptimekuma.monitors":
            observations.extend(
                item.model_dump(mode="json")
                for item in evaluate_availability_observations(
                    executions_by_tool[definition.tool_id]
                )
            )
    return observations


@dataclass(frozen=True)
class CapabilityObservationDefinition:
    provider_id: str
    capability_id: str
    label: str
    tool_id: str
    evaluator: ObservationEvaluator

    @property
    def id(self) -> str:
        return f"{self.provider_id}.{self.capability_id}"


def _opnsense_gateways(result: dict[str, Any]) -> ObservationEvaluation:
    gateways = _list_value(result.get("gateways"))
    offline = _list_value(result.get("offline"))
    total = len(gateways)
    offline_count = len(offline)
    summary: dict[str, ObservationScalar] = {
        "total": total,
        "online": max(0, total - offline_count),
        "offline": offline_count,
    }
    if total == 0:
        return "unknown", "No gateways were returned", summary
    if offline_count == total:
        return "unavailable", "All declared gateways are offline", summary
    if offline_count:
        return "degraded", f"{offline_count} of {total} gateways are offline", summary
    return "healthy", f"All {total} gateways are online", summary


def _opnsense_gateway(gateway_name: str) -> ObservationEvaluator:
    """Build an evaluator for one explicitly configured OPNsense gateway."""

    def evaluate(result: dict[str, Any]) -> ObservationEvaluation:
        gateways = _list_value(result.get("gateways"))
        matches = [
            item
            for item in gateways
            if isinstance(item, dict)
            and str(item.get("name") or "").strip().casefold()
            == gateway_name.casefold()
        ]
        summary: dict[str, ObservationScalar] = {"gateway": gateway_name}
        if not matches:
            return (
                "unknown",
                f"Configured gateway '{gateway_name}' was not returned",
                summary,
            )
        if len(matches) > 1:
            return (
                "unknown",
                f"Configured gateway '{gateway_name}' is ambiguous",
                summary,
            )
        gateway = matches[0]
        summary.update(
            {
                "reported_status": str(gateway.get("status") or "unknown"),
                "online": gateway.get("online") is True,
                "loss_percent": gateway.get("loss_percent"),
                "rtt_ms": gateway.get("rtt_ms"),
                "rtt_stddev_ms": gateway.get("rtt_stddev_ms"),
            }
        )
        if gateway.get("online") is True:
            return "healthy", f"Gateway {gateway_name} is online", summary
        return (
            "unavailable",
            f"Gateway {gateway_name} is {gateway.get('status') or 'offline'}",
            summary,
        )

    return evaluate


def _opnsense_wireguard(result: dict[str, Any]) -> ObservationEvaluation:
    total = int(result.get("peers_total") or 0)
    connected = int(result.get("peers_connected") or 0)
    stale = _list_value(result.get("peers_stale"))
    summary: dict[str, ObservationScalar] = {
        "peers_total": total,
        "peers_connected": connected,
        "peers_stale": len(stale),
    }
    if total == 0:
        return "unknown", "No WireGuard peers are configured or visible", summary
    if connected == 0:
        return "unavailable", "No WireGuard peers are connected", summary
    if connected < total or stale:
        return "degraded", f"{connected} of {total} WireGuard peers are connected", summary
    return "healthy", f"All {total} WireGuard peers are connected", summary


def _proxmox_cluster(result: dict[str, Any]) -> ObservationEvaluation:
    entries = _list_value(result.get("entries"))
    cluster = next(
        (item for item in entries if isinstance(item, dict) and item.get("kind") == "cluster"),
        None,
    )
    nodes = [item for item in entries if isinstance(item, dict) and item.get("kind") == "node"]
    online_nodes = sum(1 for item in nodes if item.get("online") is True)
    summary: dict[str, ObservationScalar] = {
        "nodes_total": len(nodes),
        "nodes_online": online_nodes,
        "quorate": cluster.get("quorate") if cluster else None,
    }
    if cluster is None:
        return "unknown", "No cluster record was returned", summary
    if cluster.get("quorate") is False:
        return "unavailable", "Cluster quorum is unavailable", summary
    if online_nodes < len(nodes):
        return "degraded", f"{online_nodes} of {len(nodes)} cluster nodes are online", summary
    return "healthy", "Cluster quorum and node membership are healthy", summary


def _uptimekuma_monitors(result: dict[str, Any]) -> ObservationEvaluation:
    total = int(result.get("total") or 0)
    by_status = _dict_value(result.get("by_status"))
    down = int(by_status.get("down") or 0)
    pending = int(by_status.get("pending") or 0)
    summary: dict[str, ObservationScalar] = {
        "total": total,
        "up": int(by_status.get("up") or 0),
        "down": down,
        "pending": pending,
        "maintenance": int(by_status.get("maintenance") or 0),
    }
    if total == 0:
        return "unknown", "No Uptime Kuma monitors were returned", summary
    if down:
        return "degraded", f"{down} of {total} monitors are down", summary
    if pending:
        return "degraded", f"{pending} of {total} monitors are pending", summary
    return "healthy", f"All {total} monitors are up or in maintenance", summary


def _zerotier_members(result: dict[str, Any]) -> ObservationEvaluation:
    total = int(result.get("total") or 0)
    authorized = int(result.get("authorized") or 0)
    online = int(result.get("online") or 0)
    stale = int(result.get("stale") or 0)
    unauthorized = int(result.get("unauthorized") or 0)
    required_total = int(result.get("required_total") or 0)
    required_online = int(result.get("required_online") or 0)
    required_unavailable = int(result.get("required_unavailable") or 0)
    summary: dict[str, ObservationScalar] = {
        "members_total": total,
        "members_authorized": authorized,
        "members_online": online,
        "members_stale": stale,
        "members_unauthorized": unauthorized,
        "members_required": required_total,
        "members_required_online": required_online,
        "members_required_unavailable": required_unavailable,
    }
    if required_total and required_online == 0:
        return "unavailable", "No required ZeroTier members are online", summary
    if required_unavailable:
        return (
            "degraded",
            f"{required_online} of {required_total} required ZeroTier members are online",
            summary,
        )
    if required_total:
        return (
            "healthy",
            f"All {required_total} required ZeroTier members are online",
            summary,
        )
    return (
        "healthy",
        f"ZeroTier is reachable with {online} authorized member(s) online; "
        "no always-on members are required",
        summary,
    )


def _cloudflare_tunnels(result: dict[str, Any]) -> ObservationEvaluation:
    total = int(result.get("total") or 0)
    by_status = _dict_value(result.get("by_status"))
    healthy = int(by_status.get("healthy") or 0)
    degraded = int(by_status.get("degraded") or 0)
    unavailable = int(by_status.get("unavailable") or 0)
    summary: dict[str, ObservationScalar] = {
        "tunnels_total": total,
        "tunnels_healthy": healthy,
        "tunnels_degraded": degraded,
        "tunnels_unavailable": unavailable,
    }
    if total == 0:
        return "unknown", "No declared Cloudflare tunnels were returned", summary
    if unavailable == total:
        return "unavailable", "All declared Cloudflare tunnels are down or inactive", summary
    if degraded or unavailable:
        return "degraded", f"{degraded + unavailable} of {total} tunnels are unhealthy", summary
    return "healthy", f"All {total} Cloudflare tunnels are healthy", summary


_DEFINITIONS = (
    CapabilityObservationDefinition(
        provider_id="cloudflaretunnel",
        capability_id="tunnel",
        label="Cloudflare Tunnel",
        tool_id="cloudflare.tunnels.status",
        evaluator=_cloudflare_tunnels,
    ),
    CapabilityObservationDefinition(
        provider_id="opnsense",
        capability_id="gateways",
        label="Gateway availability",
        tool_id="opnsense.gateways.status",
        evaluator=_opnsense_gateways,
    ),
    CapabilityObservationDefinition(
        provider_id="opnsense",
        capability_id="wireguard",
        label="WireGuard peers",
        tool_id="opnsense.wireguard.status",
        evaluator=_opnsense_wireguard,
    ),
    CapabilityObservationDefinition(
        provider_id="proxmox",
        capability_id="cluster",
        label="Cluster quorum",
        tool_id="proxmox.cluster.status",
        evaluator=_proxmox_cluster,
    ),
    CapabilityObservationDefinition(
        provider_id="uptimekuma",
        capability_id="monitors",
        label="Monitor availability",
        tool_id="uptimekuma.monitors.status",
        evaluator=_uptimekuma_monitors,
    ),
    CapabilityObservationDefinition(
        provider_id="zerotier",
        capability_id="members",
        label="ZeroTier members",
        tool_id="zerotier.members.list",
        evaluator=_zerotier_members,
    ),
)


def _configured_opnsense_gateway_observations() -> list[CapabilityObservationDefinition]:
    configured = provider_config("opnsense").get("gateway_observations", [])
    if not isinstance(configured, list):
        return []
    definitions: list[CapabilityObservationDefinition] = []
    seen_ids: set[str] = set()
    for item in configured:
        if not isinstance(item, dict):
            continue
        observation_id = str(item.get("id") or "").strip().lower()
        gateway_name = str(item.get("gateway_name") or "").strip()
        if (
            not _SAFE_GATEWAY_OBSERVATION_ID.fullmatch(observation_id)
            or observation_id in seen_ids
            or not gateway_name
        ):
            continue
        seen_ids.add(observation_id)
        definitions.append(
            CapabilityObservationDefinition(
                provider_id="opnsense",
                capability_id=f"gateway.{observation_id}",
                label=str(item.get("label") or gateway_name).strip() or gateway_name,
                tool_id="opnsense.gateways.status",
                evaluator=_opnsense_gateway(gateway_name),
            )
        )
    return definitions


def list_observation_definitions(
    provider_id: str | None = None,
) -> list[CapabilityObservationDefinition]:
    definitions = [*_DEFINITIONS, *_configured_opnsense_gateway_observations()]
    return [
        definition
        for definition in definitions
        if provider_id is None or definition.provider_id == provider_id
    ]


def evaluate_observation(
    definition: CapabilityObservationDefinition,
    execution: ExecutionResult,
) -> CapabilityObservation:
    checked_at = execution.finished_at or datetime.now(UTC)
    if not execution.ok:
        error_code = execution.error.code if execution.error else "provider_error"
        status: ProviderStatusValue = "unreachable" if error_code == "provider_timeout" else "unknown"
        detail = execution.error.message if execution.error else "Capability observation failed"
        return CapabilityObservation(
            id=definition.id,
            provider_id=definition.provider_id,
            capability_id=definition.capability_id,
            label=definition.label,
            tool_id=definition.tool_id,
            status=status,
            detail=detail,
            checked_at=checked_at,
            error_code=error_code,
        )

    status, detail, summary = definition.evaluator(execution.result or {})
    return CapabilityObservation(
        id=definition.id,
        provider_id=definition.provider_id,
        capability_id=definition.capability_id,
        label=definition.label,
        tool_id=definition.tool_id,
        status=status,
        detail=detail,
        checked_at=checked_at,
        summary=summary,
    )


def evaluate_availability_observations(
    execution: ExecutionResult,
) -> list[CapabilityObservation]:
    """Project explicit Kuma monitor bindings onto declared topology nodes."""
    bindings = [node for node in list_topology_nodes() if node.availability_monitor]
    if not bindings:
        return []
    checked_at = execution.finished_at or datetime.now(UTC)
    error_code = execution.error.code if execution.error else "provider_error"
    if not execution.ok:
        status: ProviderStatusValue = "unreachable" if error_code == "provider_timeout" else "unknown"
        detail = execution.error.message if execution.error else "Uptime Kuma observation failed"
        return [
            CapabilityObservation(
                id=f"uptimekuma.monitor.{binding.id}",
                provider_id="uptimekuma",
                capability_id=f"monitor.{binding.id}",
                label=f"Availability: {binding.label or binding.id}",
                tool_id="uptimekuma.monitors.status",
                status=status,
                detail=detail,
                checked_at=checked_at,
                error_code=error_code,
                summary={"monitor": binding.availability_monitor},
            )
            for binding in bindings
        ]

    raw_monitors = _list_value(execution.result.get("monitors") if execution.result else None)
    monitors = [item for item in raw_monitors if isinstance(item, dict)]
    by_name: dict[str, list[dict[str, Any]]] = {}
    for monitor in monitors:
        name = str(monitor.get("name") or "").strip().casefold()
        if name:
            by_name.setdefault(name, []).append(monitor)

    observations: list[CapabilityObservation] = []
    for binding in bindings:
        matches = by_name.get(binding.availability_monitor.strip().casefold(), [])
        status: ProviderStatusValue = "unknown"
        detail = f"Declared monitor '{binding.availability_monitor}' was not returned"
        summary: dict[str, ObservationScalar] = {"monitor": binding.availability_monitor}
        if len(matches) > 1:
            detail = f"Declared monitor '{binding.availability_monitor}' is ambiguous"
        elif matches:
            monitor = matches[0]
            monitor_status = str(monitor.get("status") or "unknown").strip().lower()
            monitor_statuses: dict[str, ProviderStatusValue] = {
                "up": "healthy",
                "down": "unavailable",
                "pending": "degraded",
                "maintenance": "unknown",
            }
            status = monitor_statuses.get(monitor_status, "unknown")
            detail = f"Uptime Kuma reports {monitor_status}"
            summary = {
                "monitor": binding.availability_monitor,
                "monitor_status": monitor_status,
                "monitor_type": str(monitor.get("type") or ""),
                "target": str(monitor.get("target") or ""),
            }
        observations.append(
            CapabilityObservation(
                id=f"uptimekuma.monitor.{binding.id}",
                provider_id="uptimekuma",
                capability_id=f"monitor.{binding.id}",
                label=f"Availability: {binding.label or binding.id}",
                tool_id="uptimekuma.monitors.status",
                status=status,
                detail=detail,
                checked_at=checked_at,
                summary=summary,
            )
        )
    return observations
