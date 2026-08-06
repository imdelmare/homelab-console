from types import SimpleNamespace

from app.services.inventory import (
    TopologyAvailabilityGroupEntry,
    TopologyEdgeEntry,
    TopologyNodeEntry,
)
from app.services.topology import build_topology
from tests.conftest import do_login


def _declared(monkeypatch):
    nodes = [
        TopologyNodeEntry(
            id="wan.primary ISP",
            label="primary ISP FWA",
            kind="uplink",
            layer="wan",
            observation_id="opnsense.gateway.primary ISP",
            inherit_provider_status=False,
        ),
        TopologyNodeEntry(
            id="wan.mikrotik",
            label="MikroTik 4G",
            kind="uplink",
            layer="wan",
            provider_id="mikrotik",
            observation_id="opnsense.gateway.backup",
            inherit_provider_status=False,
        ),
        TopologyNodeEntry(
            id="edge.opnsense",
            label="OPNsense",
            kind="firewall",
            layer="edge",
            provider_id="opnsense",
        ),
        TopologyNodeEntry(
            id="vpn.wireguard",
            label="WireGuard",
            kind="vpn",
            layer="edge",
            provider_id="opnsense",
            observation_id="opnsense.wireguard",
            inherit_provider_status=False,
            incident_watcher_ids=["network.wireguard"],
        ),
        TopologyNodeEntry(id="access.wifi", label="Wi-Fi mesh", kind="wireless", layer="edge"),
        TopologyNodeEntry(id="access.fritz", label="Fritz mesh", kind="mesh_ap", layer="edge", parent_id="access.wifi"),
        TopologyNodeEntry(
            id="compute.proxmox",
            label="Proxmox",
            kind="cluster",
            layer="compute",
            observation_id="proxmox.cluster",
        ),
        TopologyNodeEntry(id="compute.qdevice", label="QDevice", kind="qdevice", layer="compute"),
        TopologyNodeEntry(id="compute.vps", label="VPS", kind="vps", layer="compute"),
        TopologyNodeEntry(
            id="service.adguard",
            label="AdGuard",
            layer="services",
            provider_id="adguard",
            availability_monitor="AdGuard DNS",
            guest_match=["adguard"],
        ),
    ]
    edges = [
        TopologyEdgeEntry(source="wan.primary ISP", target="edge.opnsense", kind="uplink", availability_group="home_wan"),
        TopologyEdgeEntry(source="wan.mikrotik", target="edge.opnsense", kind="uplink", availability_group="home_wan"),
        TopologyEdgeEntry(source="access.wifi", target="access.fritz", kind="member", affects_rca=False),
        TopologyEdgeEntry(source="compute.proxmox", target="service.adguard", kind="hosts"),
    ]
    groups = [TopologyAvailabilityGroupEntry(id="home_wan", label="Home WAN", mode="any")]
    monkeypatch.setattr("app.services.topology.list_topology_nodes", lambda: nodes)
    monkeypatch.setattr("app.services.topology.list_topology_edges", lambda: edges)
    monkeypatch.setattr("app.services.topology.list_topology_availability_groups", lambda: groups)


def test_declared_topology_keeps_backup_semantics_and_fritz_off_wan(monkeypatch):
    _declared(monkeypatch)
    graph = build_topology()

    assert graph.layer_order == ["wan", "edge", "compute", "services"]
    assert graph.availability_groups[0].mode == "any"
    assert {node.id for node in graph.nodes if node.layer == "wan"} == {"wan.primary ISP", "wan.mikrotik"}
    primary ISP = next(node for node in graph.nodes if node.id == "wan.primary ISP")
    assert primary ISP.observation_id == "opnsense.gateway.primary ISP"
    assert primary ISP.inherit_provider_status is False
    mikrotik = next(node for node in graph.nodes if node.id == "wan.mikrotik")
    assert mikrotik.observation_id == "opnsense.gateway.backup"
    assert mikrotik.inherit_provider_status is False
    opnsense = next(node for node in graph.nodes if node.id == "edge.opnsense")
    assert opnsense.observation_id == ""
    fritz = next(node for node in graph.nodes if node.id == "access.fritz")
    assert fritz.kind == "mesh_ap"
    assert fritz.parent_id == "access.wifi"
    fritz_edge = next(edge for edge in graph.edges if edge.target == "access.fritz")
    assert fritz_edge.affects_rca is False
    wireguard = next(node for node in graph.nodes if node.id == "vpn.wireguard")
    assert wireguard.inherit_provider_status is False
    assert wireguard.incident_watcher_ids == ["network.wireguard"]
    assert wireguard.observation_id == "opnsense.wireguard"
    adguard = next(node for node in graph.nodes if node.id == "service.adguard")
    assert adguard.availability_observation_id == "uptimekuma.monitor.service.adguard"


def test_live_topology_enriches_cluster_nodes_and_guest_placement(monkeypatch):
    _declared(monkeypatch)
    graph = build_topology(
        {
            "entries": [{"id": "cluster", "kind": "cluster", "quorate": True}],
            "nodes": [{"node": "pve1", "status": "online"}, {"node": "pve2", "status": "online"}],
            "guests": [{"vmid": 103, "name": "adguard", "guest_type": "lxc", "status": "running", "node": "pve2"}],
        }
    )

    assert graph.source_status == "live"
    assert next(node for node in graph.nodes if node.id == "compute.proxmox").status == "healthy"
    assert {node.id for node in graph.nodes if node.kind == "hypervisor_node"} == {
        "compute.proxmox.node.pve1",
        "compute.proxmox.node.pve2",
    }
    adguard = next(node for node in graph.nodes if node.id == "service.adguard")
    assert adguard.vmid == 103
    assert adguard.runtime_node == "pve2"
    assert adguard.status == "healthy"
    assert any(
        edge.source == "compute.proxmox.node.pve2"
        and edge.target == "service.adguard"
        and edge.kind == "runs_on"
        for edge in graph.edges
    )


async def test_topology_endpoint_uses_shared_execution_core(
    client, user, capture_adapter, monkeypatch
):
    _declared(monkeypatch)
    calls = []

    async def fake_execute(tool_id, raw_input, actor, *, source):
        calls.append((tool_id, raw_input, actor.kind, source))
        return SimpleNamespace(
            ok=True,
            result={"entries": [], "nodes": [], "guests": []},
            error=None,
        )

    monkeypatch.setattr("app.api.routes_control.execute_tool", fake_execute)
    await do_login(client, capture_adapter)
    response = await client.get("/api/topology")

    assert response.status_code == 200
    assert response.json()["source_status"] == "live"
    assert calls == [("proxmox.topology", {}, "user", "rest")]


async def test_topology_snapshot_endpoint_supports_forced_refresh(
    client, user, capture_adapter, monkeypatch
):
    calls = []

    async def fake_snapshot(actor, *, force=False):
        calls.append((actor.kind, force))
        return {"graph": {"nodes": []}, "generated_at": "2026-07-20T12:00:00Z"}

    monkeypatch.setattr("app.api.routes_control.get_topology_snapshot", fake_snapshot)
    await do_login(client, capture_adapter)

    response = await client.get("/api/topology/snapshot?force=true")

    assert response.status_code == 200
    assert response.json()["graph"]["nodes"] == []
    assert calls == [("user", True)]
