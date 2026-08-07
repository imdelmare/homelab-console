"""Build the operator topology from declared structure and live placement.

The declared graph owns physical semantics. Provider observations may enrich
status and runtime placement, but can never create arbitrary network targets.
"""

from datetime import UTC, datetime
import re

from pydantic import BaseModel, ConfigDict, Field

from app.services.inventory import (
    list_topology_availability_groups,
    list_topology_edges,
    list_topology_nodes,
)

TOPOLOGY_LAYERS = ("wan", "edge", "compute", "services")
_SAFE_ID = re.compile(r"[^a-z0-9]+")


class TopologyNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    label: str
    kind: str
    layer: str
    provider_id: str = ""
    observation_id: str = ""
    availability_monitor: str = ""
    availability_observation_id: str = ""
    inherit_provider_status: bool = True
    incident_watcher_ids: list[str] = Field(default_factory=list)
    role: str = ""
    group: str = ""
    parent_id: str = ""
    status: str = "unknown"
    status_detail: str = ""
    dynamic: bool = False
    vmid: int | None = None
    runtime_node: str = ""
    guest_type: str = ""


class TopologyEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    source: str
    target: str
    kind: str
    label: str = ""
    affects_rca: bool = True
    availability_group: str = ""
    dynamic: bool = False


class TopologyAvailabilityGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    label: str
    mode: str


class TopologyGraph(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nodes: list[TopologyNode] = Field(default_factory=list)
    edges: list[TopologyEdge] = Field(default_factory=list)
    availability_groups: list[TopologyAvailabilityGroup] = Field(default_factory=list)
    layer_order: list[str] = Field(default_factory=lambda: list(TOPOLOGY_LAYERS))
    source_status: str = "declared"
    warnings: list[str] = Field(default_factory=list)
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def _slug(value: str) -> str:
    return _SAFE_ID.sub("-", value.strip().lower()).strip("-") or "unknown"


def _match_key(value: str) -> str:
    return _SAFE_ID.sub("", value.strip().lower())


def _runtime_status(value: object) -> str:
    normalized = str(value or "").lower()
    if normalized in {"online", "running", "healthy", "available"}:
        return "healthy"
    if normalized in {"offline", "stopped", "unavailable", "error"}:
        return "unavailable"
    return "unknown"


def _declared_nodes(warnings: list[str]) -> tuple[list[TopologyNode], dict[str, set[str]]]:
    nodes: list[TopologyNode] = []
    matchers: dict[str, set[str]] = {}
    seen: set[str] = set()
    for entry in list_topology_nodes():
        if entry.id in seen:
            warnings.append(f"Duplicate topology node ignored: {entry.id}")
            continue
        seen.add(entry.id)
        layer = entry.layer if entry.layer in TOPOLOGY_LAYERS else "services"
        if layer != entry.layer:
            warnings.append(f"Invalid layer '{entry.layer}' on {entry.id}; using services")
        nodes.append(
            TopologyNode(
                id=entry.id,
                label=entry.label or entry.id,
                kind=entry.kind,
                layer=layer,
                provider_id=entry.provider_id,
                observation_id=entry.observation_id,
                availability_monitor=entry.availability_monitor,
                availability_observation_id=(
                    f"uptimekuma.monitor.{entry.id}" if entry.availability_monitor else ""
                ),
                inherit_provider_status=entry.inherit_provider_status,
                incident_watcher_ids=entry.incident_watcher_ids,
                role=entry.role,
                group=entry.group,
                parent_id=entry.parent_id,
                vmid=entry.guest_vmid,
            )
        )
        aliases = {
            _match_key(alias)
            for alias in (
                *entry.guest_match,
                entry.provider_id,
                entry.id.rsplit(".", 1)[-1],
                entry.label,
            )
            if alias
        }
        matchers[entry.id] = {alias for alias in aliases if alias}
    return nodes, matchers


def _declared_edges(node_ids: set[str], warnings: list[str]) -> list[TopologyEdge]:
    edges: list[TopologyEdge] = []
    seen: set[str] = set()
    for entry in list_topology_edges():
        edge_id = entry.id or f"{entry.source}:{entry.kind}:{entry.target}"
        if entry.source not in node_ids or entry.target not in node_ids:
            warnings.append(f"Dangling topology edge ignored: {edge_id}")
            continue
        if edge_id in seen:
            warnings.append(f"Duplicate topology edge ignored: {edge_id}")
            continue
        seen.add(edge_id)
        edges.append(
            TopologyEdge(
                id=edge_id,
                source=entry.source,
                target=entry.target,
                kind=entry.kind,
                label=entry.label,
                affects_rca=entry.affects_rca,
                availability_group=entry.availability_group,
            )
        )
    return edges


def _availability_groups(warnings: list[str]) -> list[TopologyAvailabilityGroup]:
    groups: list[TopologyAvailabilityGroup] = []
    seen: set[str] = set()
    for entry in list_topology_availability_groups():
        if entry.id in seen:
            warnings.append(f"Duplicate availability group ignored: {entry.id}")
            continue
        seen.add(entry.id)
        mode = entry.mode if entry.mode in {"all", "any"} else "all"
        if mode != entry.mode:
            warnings.append(f"Invalid availability mode '{entry.mode}' on {entry.id}; using all")
        groups.append(
            TopologyAvailabilityGroup(id=entry.id, label=entry.label or entry.id, mode=mode)
        )
    return groups


def _find_guest_node(
    guest: dict,
    nodes_by_id: dict[str, TopologyNode],
    matchers: dict[str, set[str]],
) -> TopologyNode | None:
    vmid_value = guest.get("vmid")
    try:
        vmid = int(vmid_value) if vmid_value is not None else None
    except (TypeError, ValueError):
        vmid = None
    guest_name = _match_key(str(guest.get("name", "")))
    for node in nodes_by_id.values():
        if node.layer != "services":
            continue
        if vmid is not None and node.vmid == vmid:
            return node
        if guest_name and guest_name in matchers.get(node.id, set()):
            return node
    return None


def build_topology(
    proxmox_snapshot: dict | None = None,
    *,
    observation_error: str = "",
) -> TopologyGraph:
    """Merge validated declared topology with a normalized Proxmox snapshot."""
    warnings: list[str] = []
    nodes, matchers = _declared_nodes(warnings)
    nodes_by_id = {node.id: node for node in nodes}
    edges = _declared_edges(set(nodes_by_id), warnings)
    groups = _availability_groups(warnings)

    if observation_error:
        warnings.append(observation_error)
    if not proxmox_snapshot:
        return TopologyGraph(
            nodes=nodes,
            edges=edges,
            availability_groups=groups,
            source_status="declared",
            warnings=warnings,
        )

    cluster_node = nodes_by_id.get("compute.proxmox")
    qdevice_node = nodes_by_id.get("compute.qdevice")
    for entry in proxmox_snapshot.get("entries", []) or []:
        kind = str(entry.get("kind", "")).lower()
        if kind == "cluster" and cluster_node:
            quorate = entry.get("quorate")
            cluster_node.status = "healthy" if quorate is True else "degraded" if quorate is False else "unknown"
            cluster_node.status_detail = "quorum available" if quorate is True else "quorum unavailable" if quorate is False else ""
        elif "qdevice" in kind and qdevice_node:
            online = entry.get("online")
            if online is not None:
                qdevice_node.status = _runtime_status("online" if online else "offline")
                qdevice_node.status_detail = "observed in Proxmox cluster status"

    runtime_nodes: dict[str, str] = {}
    for item in proxmox_snapshot.get("nodes", []) or []:
        name = str(item.get("node", "")).strip()
        if not name:
            continue
        node_id = f"compute.proxmox.node.{_slug(name)}"
        runtime_nodes[name] = node_id
        if node_id in nodes_by_id:
            continue
        runtime_node = TopologyNode(
            id=node_id,
            label=name,
            kind="hypervisor_node",
            layer="compute",
            parent_id="compute.proxmox",
            group="home",
            status=_runtime_status(item.get("status")),
            status_detail="live Proxmox node",
            dynamic=True,
        )
        nodes.append(runtime_node)
        nodes_by_id[node_id] = runtime_node
        edges.append(
            TopologyEdge(
                id=f"{node_id}:member_of:compute.proxmox",
                source=node_id,
                target="compute.proxmox",
                kind="member_of",
                affects_rca=False,
                dynamic=True,
            )
        )

    for guest in proxmox_snapshot.get("guests", []) or []:
        try:
            vmid = int(guest.get("vmid"))
        except (TypeError, ValueError):
            continue
        node = _find_guest_node(guest, nodes_by_id, matchers)
        if node is None:
            guest_name = str(guest.get("name", "")).strip() or f"Guest {vmid}"
            node = TopologyNode(
                id=f"guest.{vmid}",
                label=guest_name,
                kind="guest",
                layer="services",
                parent_id="compute.proxmox",
                dynamic=True,
            )
            nodes.append(node)
            nodes_by_id[node.id] = node
        node.vmid = vmid
        node.runtime_node = str(guest.get("node", ""))
        node.guest_type = str(guest.get("guest_type", ""))
        node.status = _runtime_status(guest.get("status"))
        node.status_detail = f"{node.guest_type.upper()} {vmid}".strip()

        source = runtime_nodes.get(node.runtime_node, "compute.proxmox")
        edge_id = f"{source}:runs_on:{node.id}"
        if not any(edge.id == edge_id for edge in edges):
            edges.append(
                TopologyEdge(
                    id=edge_id,
                    source=source,
                    target=node.id,
                    kind="runs_on",
                    label="live placement",
                    dynamic=True,
                )
            )

    return TopologyGraph(
        nodes=nodes,
        edges=edges,
        availability_groups=groups,
        source_status="live",
        warnings=warnings,
    )
