import { useMemo, useRef, useState } from "react";
import { Button } from "react95";
import { fetchTopologySnapshot } from "../lib/api";
import { formatDateTime } from "../lib/format";
import { shortId } from "../lib/ui";
import {
  topologyNodeIncidents,
  topologyNodeStatus,
  topologyNodeStatusDetail,
  topologyStaleSources,
} from "../lib/topology";
import { usePanelQuery } from "../lib/usePanelQuery";
import { EmptyState } from "../components/EmptyState";
import { PanelLoadingScreen } from "../components/PanelLoadingScreen";
import { KeyValueGrid, ProviderIcon } from "./shared";
import type { Incident, TopologyEdge, TopologyNode } from "../lib/types";

type TopologyView = "physical" | "impact";
type NodeVisualState =
  | "root"
  | "dependent"
  | "alert"
  | "degraded"
  | "healthy"
  | "path"
  | "unknown";

const LAYER_LABELS: Record<string, string> = {
  wan: "WAN",
  edge: "Perimetro e accesso",
  compute: "Calcolo",
  services: "Servizi",
};

const STATUS_LABELS: Record<string, string> = {
  healthy: "Operational",
  degraded: "Warning",
  unreachable: "Unreachable",
  unavailable: "Unavailable",
  misconfigured: "Misconfigured",
  error: "Error",
  path: "Path",
  unknown: "Unknown",
};

function statusLabel(status: string): string {
  return STATUS_LABELS[status] ?? status;
}

export function TopologyApp() {
  const forceNextRefresh = useRef(false);
  const topologyQuery = usePanelQuery(
    ["topology-snapshot"],
    () => {
      const force = forceNextRefresh.current;
      forceNextRefresh.current = false;
      return fetchTopologySnapshot(force);
    },
    { refetchInterval: 15_000 },
  );
  const snapshot = topologyQuery.data;
  const graph = snapshot?.graph;
  const nodes = useMemo(() => graph?.nodes ?? [], [graph?.nodes]);
  const incidents = useMemo(() => snapshot?.incidents ?? [], [snapshot?.incidents]);
  const providers = useMemo(() => snapshot?.providers ?? [], [snapshot?.providers]);
  const observations = useMemo(() => snapshot?.observations ?? [], [snapshot?.observations]);
  const loadState = topologyQuery.loadState;
  const errorMessage = topologyQuery.errorMessage;
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [hoveredNodeId, setHoveredNodeId] = useState<string | null>(null);
  const [view, setView] = useState<TopologyView>("physical");

  const providersById = useMemo(
    () => new Map(providers.map((provider) => [provider.id, provider])),
    [providers],
  );
  const observationsById = useMemo(
    () => new Map(observations.map((observation) => [observation.id, observation])),
    [observations],
  );
  const nodesById = useMemo(() => new Map(nodes.map((node) => [node.id, node])), [nodes]);
  const focusNodeId = hoveredNodeId ?? selectedNodeId;
  const relatedNodeIds = useMemo(() => {
    if (!focusNodeId) return new Set<string>();
    const related = new Set<string>([focusNodeId]);
    for (const edge of graph?.edges ?? []) if (edge.source === focusNodeId || edge.target === focusNodeId) { related.add(edge.source); related.add(edge.target); }
    return related;
  }, [focusNodeId, graph?.edges]);
  const incidentsByProvider = useMemo(() => {
    const map = new Map<string, Incident[]>();
    for (const incident of incidents) {
      const list = map.get(incident.provider_id) ?? [];
      list.push(incident);
      map.set(incident.provider_id, list);
    }
    return map;
  }, [incidents]);
  const visibleEdges = useMemo(
    () => (graph?.edges ?? []).filter((edge) => view === "physical" || edge.affects_rca),
    [graph?.edges, view],
  );
  const groupsById = useMemo(
    () => new Map((graph?.availability_groups ?? []).map((group) => [group.id, group])),
    [graph?.availability_groups],
  );

  const effectiveSelectedNodeId = nodes.some((node) => node.id === selectedNodeId)
    ? selectedNodeId
    : nodes[0]?.id ?? null;
  const selectedNode = nodesById.get(effectiveSelectedNodeId ?? "") ?? null;
  const selectedProvider = selectedNode?.provider_id
    ? providersById.get(selectedNode.provider_id)
    : undefined;
  const selectedObservation = selectedNode?.observation_id
    ? observationsById.get(selectedNode.observation_id)
    : undefined;
  const selectedAvailability = selectedNode?.availability_observation_id
    ? observationsById.get(selectedNode.availability_observation_id)
    : undefined;
  const selectedIncidents = selectedNode
    ? topologyNodeIncidents(selectedNode, incidentsByProvider)
    : [];
  const selectedIncoming = selectedNode
    ? visibleEdges.filter((edge) => edge.target === selectedNode.id)
    : [];
  const selectedOutgoing = selectedNode
    ? visibleEdges.filter((edge) => edge.source === selectedNode.id)
    : [];
  const selectedStatusDetail = selectedNode
    ? topologyNodeStatusDetail(selectedNode, providersById, observationsById)
    : "";

  function refresh() {
    forceNextRefresh.current = true;
    topologyQuery.refresh();
  }

  const staleSources = topologyStaleSources(snapshot?.freshness);

  function nodeIncidents(node: TopologyNode): Incident[] {
    return topologyNodeIncidents(node, incidentsByProvider);
  }

  function nodeVisualState(node: TopologyNode): NodeVisualState {
    const currentIncidents = nodeIncidents(node);
    if (currentIncidents.some((incident) => !incident.root_cause_incident_id)) return "root";
    if (currentIncidents.some((incident) => incident.root_cause_incident_id)) return "dependent";
    const status = topologyNodeStatus(node, providersById, observationsById);
    if (["unreachable", "unavailable", "misconfigured", "error"].includes(status)) return "alert";
    if (status === "degraded") return "degraded";
    if (status === "healthy") return "healthy";
    if (["network", "wireless", "vpn"].includes(node.kind)) return "path";
    return "unknown";
  }

  function edgeText(edge: TopologyEdge, direction: "in" | "out"): string {
    const otherId = direction === "in" ? edge.source : edge.target;
    const other = nodesById.get(otherId);
    const group = edge.availability_group ? groupsById.get(edge.availability_group) : undefined;
    return [edge.kind, other?.label ?? otherId, edge.label, group ? `${group.label}: ${group.mode}` : ""]
      .filter(Boolean)
      .join(" · ");
  }

  function openTask(taskId: string | null) {
    if (!taskId) return;
    window.dispatchEvent(new CustomEvent("homelab:open-task", { detail: { taskId } }));
  }

  if (loadState === "loading") {
    return <PanelLoadingScreen label="Loading topology…" />;
  }

  return (
    <div className="panel-app topology-app">
      <div className="panel-toolbar topology-toolbar">
        <Button onClick={refresh} disabled={topologyQuery.isFetching}>
          Refresh
        </Button>
        <div className="topology-view-switch" role="radiogroup" aria-label="Topology view">
          <Button role="radio" aria-checked={view === "physical"} active={view === "physical"} onClick={() => setView("physical")}>Physical view</Button>
          <Button role="radio" aria-checked={view === "impact"} active={view === "impact"} onClick={() => setView("impact")}>Failure impact</Button>
        </div>
      </div>
      <div className="topology-statusbar">
        <span>
          {nodes.length} nodes · {visibleEdges.length} relationships · {incidents.length} open incidents · {graph?.source_status ?? "no data"}
          {snapshot ? ` · ${staleSources.length ? "partial" : "updated"} · ${formatDateTime(snapshot.generated_at)}` : ""}
        </span>
      </div>
      <div className="topology-legend" aria-label="Topology legend">
        <span><i className="legend-swatch legend-healthy" />Operational</span>
        <span><i className="legend-swatch legend-warning" />Warning</span>
        <span><i className="legend-swatch legend-danger" />Error or root cause</span>
        <span><i className="legend-line" />Dependency</span>
        <span><i className="legend-line legend-line-dashed" />Observational relationship</span>
        <span><i className="legend-swatch legend-path" />Selected path</span>
        <span><i className="legend-swatch legend-group" />Logical group</span>
      </div>
      {loadState === "error" && <p className="login-error">{errorMessage}</p>}
      {staleSources.length > 0 && (
        <div className="topology-warning" role="status">
          {staleSources
            .map(([name, source]) => `${name} ${source.status}${source.error || source.warning ? `: ${source.error || source.warning}` : ""}`)
            .join(" · ")}
        </div>
      )}
      {(graph?.warnings.length ?? 0) > 0 && (
        <div className="topology-warning" role="status">
          {graph?.warnings.join(" · ")}
        </div>
      )}
      <div className="topology-layout">
        <div className="topology-canvas sunken-panel">
          {(graph?.layer_order ?? []).map((layer) => {
            const layerNodes = nodes.filter((node) => node.layer === layer);
            return (
              <section className="topology-level" key={layer}>
                <h3>{LAYER_LABELS[layer] ?? layer}</h3>
                {layerNodes.map((node) => {
                  const state = nodeVisualState(node);
                  const currentIncidents = nodeIncidents(node);
                  const status = topologyNodeStatus(node, providersById, observationsById);
                  const outgoing = visibleEdges.filter((edge) => edge.source === node.id);
                  return (
                    <article
                      className={`topology-card topology-node-${state} ${node.id === effectiveSelectedNodeId ? "topology-node-selected" : ""} ${focusNodeId && relatedNodeIds.has(node.id) ? "topology-node-related" : ""} ${focusNodeId && !relatedNodeIds.has(node.id) ? "topology-node-dimmed" : ""}`}
                      key={node.id}
                      onMouseEnter={() => setHoveredNodeId(node.id)}
                      onMouseLeave={() => setHoveredNodeId(null)}
                    >
                      <button type="button" className="topology-node" onClick={() => setSelectedNodeId(node.id)}>
                        <span className={`topology-status topology-status-${status}`}>{statusLabel(status)}</span>
                        <strong>{node.label}</strong>
                        <span>{node.id}</span>
                        <small>
                          {node.kind}
                          {node.role ? ` · ${node.role}` : ""}
                          {node.dynamic ? " · live" : ""}
                          {currentIncidents.length ? ` · ${currentIncidents.length} alert` : ""}
                        </small>
                        {node.runtime_node && <em>runs on {node.runtime_node}</em>}
                      </button>
                      {outgoing.length > 0 && (
                        <div className="topology-connections" aria-label={`Connections from ${node.label}`}>
                          {outgoing.map((edge) => {
                            const target = nodesById.get(edge.target);
                            const availabilityGroup = edge.availability_group
                              ? groupsById.get(edge.availability_group)
                              : undefined;
                            return (
                              <button
                                type="button"
                                className={`topology-connection ${edge.affects_rca ? "" : "topology-connection-observer"}`}
                                key={edge.id}
                                title={edge.label || edge.kind}
                                onClick={() => setSelectedNodeId(edge.target)}
                              >
                                <span>→ {edge.kind}</span>
                                <strong>{target?.label ?? edge.target}</strong>
                                {availabilityGroup && <small>{availabilityGroup.mode}-of group</small>}
                              </button>
                            );
                          })}
                        </div>
                      )}
                    </article>
                  );
                })}
                {loadState === "ready" && layerNodes.length === 0 && (
                  <p className="topology-empty">No nodes.</p>
                )}
              </section>
            );
          })}
          {loadState === "ready" && nodes.length === 0 && (
            <EmptyState
              title="Topology contains no nodes"
              description="No topology was returned. Refresh after provider and source discovery completes."
              actionLabel="Refresh topology"
              onAction={refresh}
            />
          )}
        </div>
        <aside className="topology-detail">
          {selectedNode ? (
            <>
              <div className="topology-detail-head">
                <ProviderIcon providerId={selectedNode.provider_id || selectedNode.kind} />
                <div>
                  <h3>{selectedNode.label}</h3>
                  <span>{selectedNode.id} · {selectedNode.kind}</span>
                </div>
              </div>
              <KeyValueGrid
                items={[
                  ["Layer", LAYER_LABELS[selectedNode.layer] ?? selectedNode.layer],
                  ["Status", statusLabel(topologyNodeStatus(selectedNode, providersById, observationsById))],
                  ["Role / group", [selectedNode.role, selectedNode.group].filter(Boolean).join(" / ") || "None"],
                  ["Provider", (selectedProvider?.name ?? selectedNode.provider_id) || "None"],
                  ["Observation", (selectedObservation?.label ?? selectedNode.observation_id) || "None"],
                  ["Availability", (selectedAvailability?.label ?? selectedNode.availability_monitor) || "None"],
                  ["Open incidents", selectedIncidents.length],
                ]}
              />
              <details className="topology-technical-details"><summary>Technical details</summary><KeyValueGrid items={[["ID", selectedNode.id], ["Parent", selectedNode.parent_id || "None"], ["Runtime", selectedNode.runtime_node || "Not observed"], ["Guest", selectedNode.vmid ? `${selectedNode.guest_type.toUpperCase()} ${selectedNode.vmid}` : "None"], ["Upstream", selectedIncoming.length ? selectedIncoming.map((edge) => edgeText(edge, "in")).join(" | ") : "None"], ["Downstream", selectedOutgoing.length ? selectedOutgoing.map((edge) => edgeText(edge, "out")).join(" | ") : "None"]]} /></details>
              {selectedStatusDetail && (
                <p className="topology-status-detail">
                  {selectedStatusDetail}
                </p>
              )}
              <div className="topology-incident-list">
                {selectedIncidents.map((incident) => (
                  <article key={incident.id}>
                    <mark className={`finding-severity finding-${incident.severity}`}>{incident.severity}</mark>
                    <strong>{incident.title}</strong>
                    <span>{incident.root_cause_incident_id ? `Dependent of ${shortId(incident.root_cause_incident_id)}` : "RCA root"}</span>
                    <Button type="button" disabled={!incident.task_id} onClick={() => openTask(incident.task_id)}>
                      Task {shortId(incident.task_id)}
                    </Button>
                  </article>
                ))}
                {selectedIncidents.length === 0 && <p>No open incidents on this node.</p>}
              </div>
            </>
          ) : (
            <p>Select a node.</p>
          )}
        </aside>
      </div>
    </div>
  );
}
