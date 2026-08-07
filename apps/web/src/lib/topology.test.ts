import { describe, expect, it } from "vitest";
import { topologyNodeIncidents, topologyNodeStatus, topologyStaleSources } from "./topology";
import type { CapabilityObservation, Incident, Provider, TopologyNode } from "./types";

function node(overrides: Partial<TopologyNode> = {}): TopologyNode {
  return {
    id: "vpn.wireguard",
    label: "WireGuard",
    kind: "vpn",
    layer: "edge",
    provider_id: "opnsense",
    observation_id: "opnsense.wireguard",
    availability_monitor: "",
    availability_observation_id: "",
    inherit_provider_status: false,
    incident_watcher_ids: ["network.wireguard"],
    role: "",
    group: "",
    parent_id: "",
    status: "unknown",
    status_detail: "",
    dynamic: false,
    vmid: null,
    runtime_node: "",
    guest_type: "",
    ...overrides,
  };
}

function observation(status: CapabilityObservation["status"]): CapabilityObservation {
  return {
    id: "opnsense.wireguard",
    provider_id: "opnsense",
    capability_id: "wireguard",
    label: "WireGuard peers",
    tool_id: "opnsense.wireguard.status",
    status,
    detail: "Observed independently from the firewall",
    checked_at: "2026-07-16T16:00:00Z",
    error_code: "",
    summary: {},
  };
}

function incident(watcherId: string): Incident {
  return {
    id: watcherId,
    dedupe_key: watcherId,
    watcher_id: watcherId,
    status: "open",
    severity: "critical",
    provider_id: "opnsense",
    title: watcherId,
    description: watcherId,
    task_id: null,
    first_seen_at: "2026-07-16T16:00:00Z",
    last_seen_at: "2026-07-16T16:00:00Z",
    resolved_at: null,
    resolution_reason: "",
    missing_runs: 0,
    last_missing_at: null,
    occurrences: 1,
    payload: {},
    root_cause_incident_id: null,
  };
}

function provider(): Provider {
  return {
    id: "opnsense",
    name: "OPNsense",
    status: "healthy",
    last_ok_at: null,
    checked_at: null,
    detail: null,
    tool_count: 1,
    watchers: [],
    last_error: null,
  };
}

describe("topology node attribution", () => {
  it("does not make a capability node healthy from its host provider", () => {
    const providers = new Map([["opnsense", provider()]]);
    expect(topologyNodeStatus(node(), providers)).toBe("unknown");
    expect(topologyNodeStatus(node({ inherit_provider_status: true }), providers)).toBe("healthy");
  });

  it("labels structural network nodes as paths instead of unknown", () => {
    const structural = node({
      id: "wan.primary_isp",
      kind: "network",
      provider_id: "",
      observation_id: "",
      inherit_provider_status: false,
    });

    expect(topologyNodeStatus(structural, new Map())).toBe("path");
  });

  it("uses the linked capability observation before provider health", () => {
    const providers = new Map([["opnsense", provider()]]);
    const observations = new Map([["opnsense.wireguard", observation("unavailable")]]);

    expect(topologyNodeStatus(node(), providers, observations)).toBe("unavailable");
  });

  it("uses Kuma availability when no capability observation is declared", () => {
    const availability = observation("degraded");
    availability.id = "uptimekuma.monitor.service.homeassistant";
    const service = node({
      id: "service.homeassistant",
      provider_id: "homeassistant",
      observation_id: "",
      availability_monitor: "Home Assistant",
      availability_observation_id: availability.id,
      status: "healthy",
    });

    expect(topologyNodeStatus(service, new Map(), new Map([[availability.id, availability]]))).toBe(
      "degraded",
    );
  });

  it("keeps a stopped guest authoritative over a stale healthy monitor", () => {
    const availability = observation("healthy");
    availability.id = "uptimekuma.monitor.service.homeassistant";
    const service = node({
      id: "service.homeassistant",
      provider_id: "homeassistant",
      observation_id: "",
      availability_monitor: "Home Assistant",
      availability_observation_id: availability.id,
      status: "unavailable",
    });

    expect(topologyNodeStatus(service, new Map(), new Map([[availability.id, availability]]))).toBe(
      "unavailable",
    );
  });

  it("shows only incidents emitted by the node's watcher scope", () => {
    const presence = incident("network.presence");
    const wireguard = incident("network.wireguard");
    const incidents = new Map([["opnsense", [presence, wireguard]]]);

    expect(topologyNodeIncidents(node(), incidents)).toEqual([wireguard]);
    expect(topologyNodeIncidents(node({ incident_watcher_ids: [] }), incidents)).toEqual([
      presence,
      wireguard,
    ]);
  });

  it("attributes a Kuma incident to its declared service monitor", () => {
    const kumaIncident = {
      ...incident("uptimekuma.monitors"),
      provider_id: "uptimekuma",
      payload: { monitor: { name: "Home Assistant" } },
    };
    const service = node({
      id: "service.homeassistant",
      provider_id: "homeassistant",
      observation_id: "",
      availability_monitor: "Home Assistant",
      availability_observation_id: "uptimekuma.monitor.service.homeassistant",
      incident_watcher_ids: [],
    });

    expect(topologyNodeIncidents(service, new Map([["uptimekuma", [kumaIncident]]]))).toEqual([
      kumaIncident,
    ]);
  });

  it("attributes cross-provider incidents by exact observation id", () => {
    const gatewayIncident = {
      ...incident("network.gateway"),
      payload: { observation_id: "opnsense.gateway.primary" },
    };
    const gateway = node({
      id: "wan.primary_isp",
      kind: "network",
      provider_id: "",
      observation_id: "opnsense.gateway.primary",
      incident_watcher_ids: ["network.gateway"],
    });

    expect(topologyNodeIncidents(gateway, new Map([["opnsense", [gatewayIncident]]]))).toEqual([
      gatewayIncident,
    ]);
  });

  it("attributes a group outage to every declared observation", () => {
    const groupIncident = {
      ...incident("network.gateway"),
      payload: {
        observation_ids: ["opnsense.gateway.primary", "opnsense.gateway.backup"],
      },
    };
    const backup = node({
      id: "wan.mikrotik",
      kind: "network",
      observation_id: "opnsense.gateway.backup",
      incident_watcher_ids: ["network.gateway"],
    });

    expect(topologyNodeIncidents(backup, new Map([["opnsense", [groupIncident]]]))).toEqual([
      groupIncident,
    ]);
  });
});

describe("topology snapshot freshness", () => {
  it("reports a fully fresh snapshot as live", () => {
    expect(
      topologyStaleSources({
        inventory: { status: "fresh" },
        runtime: { status: "fresh" },
      }),
    ).toEqual([]);
  });

  it("identifies stale and failed sources for the partial-state warning", () => {
    const stale = topologyStaleSources({
      inventory: { status: "fresh" },
      runtime: { status: "stale", error: "temporarily unavailable" },
      incidents: { status: "error", error: "database unavailable" },
    });

    expect(stale.map(([name]) => name)).toEqual(["runtime", "incidents"]);
  });
});
