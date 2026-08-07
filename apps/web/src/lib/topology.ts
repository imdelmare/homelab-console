import type {
  CapabilityObservation,
  Incident,
  Provider,
  TopologyFreshnessSource,
  TopologyNode,
} from "./types";

export function topologyStaleSources(
  freshness: Readonly<Record<string, TopologyFreshnessSource>> | undefined,
): [string, TopologyFreshnessSource][] {
  return Object.entries(freshness ?? {}).filter(([, source]) => source.status !== "fresh");
}

export function topologyNodeStatus(
  node: TopologyNode,
  providers: ReadonlyMap<string, Provider>,
  observations: ReadonlyMap<string, CapabilityObservation> = new Map(),
): string {
  const runtimeStatus = node.status && node.status !== "unknown" ? node.status : "";
  if (runtimeStatus && runtimeStatus !== "healthy") return runtimeStatus;
  const observation = observations.get(node.observation_id);
  if (observation) return observation.status;
  const availability = observations.get(node.availability_observation_id);
  if (availability) return availability.status;
  if (runtimeStatus) return runtimeStatus;
  if (!node.inherit_provider_status) {
    return ["network", "wireless"].includes(node.kind) ? "path" : "unknown";
  }
  const providerStatus = providers.get(node.provider_id)?.status;
  if (providerStatus) return providerStatus;
  if (["network", "wireless"].includes(node.kind)) return "path";
  return "unknown";
}

export function topologyNodeStatusDetail(
  node: TopologyNode,
  providers: ReadonlyMap<string, Provider>,
  observations: ReadonlyMap<string, CapabilityObservation> = new Map(),
): string {
  const status = topologyNodeStatus(node, providers, observations);
  if (node.status === status && node.status_detail) return node.status_detail;
  const capability = observations.get(node.observation_id);
  if (capability?.status === status) return capability.detail;
  const availability = observations.get(node.availability_observation_id);
  if (availability?.status === status) return availability.detail;
  const provider = providers.get(node.provider_id);
  if (provider?.status === status) return provider.detail ?? "";
  return node.status_detail;
}

export function topologyNodeIncidents(
  node: TopologyNode,
  incidentsByProvider: ReadonlyMap<string, Incident[]>,
): Incident[] {
  const incidents = node.provider_id ? incidentsByProvider.get(node.provider_id) ?? [] : [];
  const observationIncidents = node.observation_id
    ? Array.from(incidentsByProvider.values()).flat().filter((incident) => {
        const observationId = String(incident.payload.observation_id ?? "");
        const observationIds = Array.isArray(incident.payload.observation_ids)
          ? incident.payload.observation_ids.map(String)
          : [];
        return observationId === node.observation_id || observationIds.includes(node.observation_id);
      })
    : [];
  const availabilityIncidents = node.availability_monitor
    ? (incidentsByProvider.get("uptimekuma") ?? []).filter((incident) => {
        if (incident.watcher_id !== "uptimekuma.monitors") return false;
        const monitor = incident.payload.monitor;
        if (!monitor || typeof monitor !== "object") return false;
        const name = String((monitor as Record<string, unknown>).name ?? "");
        return name.localeCompare(node.availability_monitor, undefined, {
          sensitivity: "accent",
        }) === 0;
      })
    : [];
  const combined = [...incidents, ...observationIncidents, ...availabilityIncidents].filter(
    (incident, index, all) => all.findIndex((item) => item.id === incident.id) === index,
  );
  if (node.incident_watcher_ids.length === 0) return combined;
  const allowedWatchers = new Set(node.incident_watcher_ids);
  return combined.filter(
    (incident) =>
      allowedWatchers.has(incident.watcher_id) || incident.watcher_id === "uptimekuma.monitors",
  );
}
